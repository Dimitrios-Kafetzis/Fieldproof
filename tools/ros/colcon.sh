#!/usr/bin/env bash
# Build, test and lint the ROS 2 workspaces (task P0-T07). Runs inside the dev container, where
# ROS 2 Jazzy, Gazebo Harmonic, colcon and clang-tidy are installed; the Makefile wraps it
# (`make test-ros`, `make tidy`). Each workspace builds into its own build/, install/ and log/
# directories, which git ignores.
#
# Usage: tools/ros/colcon.sh build|test|tidy [workspace ...]
#        tools/ros/colcon.sh run <command> [argument ...]
#
#   build   colcon build in every workspace (compile_commands.json exported for clang-tidy)
#   test    build, then colcon test and colcon test-result --verbose; fails on any failed test
#   tidy    run-clang-tidy over the C++ packages with the compile commands of the last build,
#           using the repository's .clang-tidy (warnings are errors)
#   run     run a command with the install space of every built workspace sourced, in the
#           default order (the cross-layer tests under tests/ use it: make test-worlds)
#
# Workspaces default to sim robots bridge, and that order is also the overlay chain: before a
# workspace is built or tested, the install space of every workspace earlier in the list is
# sourced (robots builds on sim: its launch test loads a fieldproof_worlds world; bridge builds on
# both: its contract test spawns the UGV of fieldproof_robots).
# Set COLCON_JOBS to cap the parallelism.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFAULT_WORKSPACES=(sim robots bridge)

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

verb="${1:-}"
case "$verb" in
  build | test | tidy | run) shift ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
if [[ "$verb" == run ]]; then
  [[ $# -gt 0 ]] || {
    echo "colcon.sh run: no command given" >&2
    exit 2
  }
  workspaces=("${DEFAULT_WORKSPACES[@]}")
elif [[ $# -gt 0 ]]; then workspaces=("$@"); else workspaces=("${DEFAULT_WORKSPACES[@]}"); fi

if ! command -v colcon >/dev/null 2>&1; then
  echo "colcon.sh: colcon is not on PATH; run this inside the dev container (make test-ros)" >&2
  exit 1
fi
# The ROS setup scripts are not written for `set -u`.
if [[ -z "${AMENT_PREFIX_PATH:-}" && -f "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash" ]]; then
  set +u
  # shellcheck disable=SC1090
  source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
  set -u
fi

jobs_flag=()
[[ -n "${COLCON_JOBS:-}" ]] && jobs_flag=(--parallel-workers "$COLCON_JOBS")
handlers=(--event-handlers console_cohesion+ desktop_notification- status-)

# Source the install spaces of the workspaces that precede "$1" in the default order (every
# workspace when "$1" is empty), so that their packages are visible as an underlay (ament index,
# environment hooks) to the one being built or tested. Runs in the subshell of the caller; the
# setup scripts are not written for `set -u`.
source_underlays() {
  local ws="${1:-}" other
  for other in "${DEFAULT_WORKSPACES[@]}"; do
    [[ "$other" == "$ws" ]] && break
    if [[ -f "$REPO/$other/install/setup.bash" ]]; then
      set +u
      # shellcheck disable=SC1090,SC1091
      source "$REPO/$other/install/setup.bash"
      set -u
    fi
  done
}

build_ws() {
  local ws="$1"
  echo "==> colcon build ($ws)"
  (cd "$REPO/$ws" && source_underlays "$ws" && colcon build "${jobs_flag[@]}" "${handlers[@]}" \
    --cmake-args -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo)
}

test_ws() {
  local ws="$1"
  echo "==> colcon test ($ws)"
  (cd "$REPO/$ws" && source_underlays "$ws" \
    && colcon test "${jobs_flag[@]}" "${handlers[@]}" --return-code-on-test-failure)
  (cd "$REPO/$ws" && colcon test-result --verbose)
}

tidy_ws() {
  local ws="$1" db pkg sources status=0
  shopt -s nullglob
  for db in "$REPO/$ws"/build/*/compile_commands.json; do
    pkg="$(basename "$(dirname "$db")")"
    [[ -d "$REPO/$ws/$pkg" ]] || continue
    # Only this package's own translation units (the database also lists generated files).
    sources="$(python3 -c 'import json,sys; print("\n".join(sorted({e["file"] for e in json.load(open(sys.argv[1])) if e["file"].startswith(sys.argv[2] + "/")})))' "$db" "$REPO/$ws/$pkg")"
    [[ -n "$sources" ]] || continue
    echo "==> clang-tidy ($ws/$pkg)"
    # shellcheck disable=SC2086  # one argument per source path
    if run-clang-tidy -quiet -p "$(dirname "$db")" $sources; then
      echo "    clean"
    else
      status=1
    fi
  done
  shopt -u nullglob
  return "$status"
}

for ws in "${workspaces[@]}"; do
  [[ -d "$REPO/$ws" ]] || {
    echo "colcon.sh: no workspace '$ws' under $REPO" >&2
    exit 2
  }
done
case "$verb" in
  build) for ws in "${workspaces[@]}"; do build_ws "$ws"; done ;;
  test) for ws in "${workspaces[@]}"; do build_ws "$ws" && test_ws "$ws"; done ;;
  tidy)
    rc=0
    for ws in "${workspaces[@]}"; do tidy_ws "$ws" || rc=1; done
    exit "$rc"
    ;;
  run)
    source_underlays ""
    cd "$REPO"
    exec "$@"
    ;;
esac
