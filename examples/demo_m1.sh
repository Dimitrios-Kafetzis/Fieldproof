#!/usr/bin/env bash
# The M1 demo (task P1-T07; docs/plan/02-roadmap.md, "Milestone demos"): from a fresh clone to
# `fieldproof run examples/scenarios/ugv_nav_to_goal.yaml` ending with PASS, a report and an
# MCAP recording to open in Foxglove Studio. The same steps run in CI
# (.github/workflows/scenario-tests.yml) on a GitHub-hosted runner.
#
# Usage: examples/demo_m1.sh [OPTIONS]
#
#   --local             use the images built locally with `make images` (tag `local`) instead
#                       of pulling the published `main` images from GHCR
#   --no-pull           do not pull the stack images (they are already present)
#   --rebuild-vehicle   rebuild the example vehicle image even when it is present
#   --scenario FILE     run FILE instead of examples/scenarios/ugv_nav_to_goal.yaml
#   --dry-run           print the commands without running them
#   --help              this text
#
# Environment: FIELDPROOF_IMAGE_TAG (default `main`; `--local` sets `local`) selects the stack
# images, FIELDPROOF_RENDER (`auto`, `gpu`, `software`; default `auto`: the GPU through
# /dev/dri when the host has one) the simulator's rendering, ROS_DOMAIN_ID (default 0) the
# containers' domain.
#
# Needs Docker and uv on the host (tools/host-setup.sh, docs/setup/host.md); nothing from ROS
# or Gazebo installs on the host. The first run pulls about 7 GB of images and builds the vehicle
# image (a few minutes: Nav2 from the pinned apt snapshots); later runs start in seconds.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_PREFIX="${FIELDPROOF_IMAGE_PREFIX:-ghcr.io/dimitrios-kafetzis/fieldproof-}"
STACK_IMAGES=(base-ros2 sim bridge)
VEHICLE_MANIFEST="examples/vehicles/ugv_nav2/robot.yaml"

scenario="examples/scenarios/ugv_nav_to_goal.yaml"
pull=true
rebuild_vehicle=false
dry_run=false
tag="${FIELDPROOF_IMAGE_TAG:-main}"

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)
      tag="local"
      pull=false
      shift
      ;;
    --no-pull)
      pull=false
      shift
      ;;
    --rebuild-vehicle)
      rebuild_vehicle=true
      shift
      ;;
    --scenario)
      [[ $# -ge 2 ]] || { echo "demo_m1: --scenario needs a file" >&2; exit 2; }
      scenario="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      echo "demo_m1: unknown option $1 (--help)" >&2
      exit 2
      ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
run() {
  echo "+ $*"
  if [[ "$dry_run" == false ]]; then "$@"; fi
}

cd "$REPO"
export FIELDPROOF_IMAGE_TAG="$tag"

step "Prerequisites (docker, uv; see docs/setup/host.md)"
for tool in docker uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "demo_m1: $tool is not installed; run tools/host-setup.sh first" >&2
    exit 1
  fi
done
if [[ "$dry_run" == false ]] && ! docker info >/dev/null 2>&1; then
  echo "demo_m1: the Docker daemon is not reachable (is your user in the docker group?)" >&2
  exit 1
fi
[[ -f "$scenario" ]] || { echo "demo_m1: $scenario: no such scenario file" >&2; exit 2; }
images_list="$(IFS=,; echo "${STACK_IMAGES[*]}")"
echo "stack images: ${IMAGE_PREFIX}{${images_list}}:${tag}; scenario: ${scenario}"

if [[ "$pull" == true ]]; then
  step "Pull the published stack images (tag ${tag})"
  for image in "${STACK_IMAGES[@]}"; do
    run docker pull --quiet "${IMAGE_PREFIX}${image}:${tag}"
  done
else
  step "Using the stack images already present (tag ${tag})"
fi

step "Fetch the Gazebo Fuel models pinned in assets/fuel.lock"
run make assets

vehicle_image="$(sed -n 's/^ *image: *//p' "$VEHICLE_MANIFEST" | head -n 1)"
step "The example vehicle image (${vehicle_image})"
if [[ "$rebuild_vehicle" == true ]] || [[ "$dry_run" == true ]] \
  || ! docker image inspect "$vehicle_image" >/dev/null 2>&1; then
  echo "building it from ${VEHICLE_MANIFEST} on ${IMAGE_PREFIX}base-ros2:${tag}"
  run make vehicle-image
else
  echo "already built (--rebuild-vehicle rebuilds it)"
fi

run_id="m1-demo-$(date -u +%Y%m%dT%H%M%SZ)"
step "Run the scenario (run id ${run_id})"
status=0
run uv run --locked --project engine fieldproof run "$scenario" --run-id "$run_id" || status=$?
if [[ "$dry_run" == true ]]; then
  step "Dry run: nothing was executed"
  exit 0
fi

run_dir="runs/${run_id}"
step "Outcome"
if [[ -f "$run_dir/report.json" ]]; then
  python3 - "$run_dir/report.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
print(f"verdict: {report['verdict']} (exit code {report['exit_code']})")
for assertion in report["assertions"]:
    print(f"  {assertion['status']:>7}  {assertion['name']} ({assertion['type']}): {assertion['reason'] or '-'}")
PY
  echo "report:  ${run_dir}/report.md (also report.json and junit.xml)"
fi
mcaps=("$run_dir"/recording/*.mcap)
if [[ -f "${mcaps[0]}" ]]; then
  echo "recording: ${mcaps[*]}"
  echo
  echo "Open the recording in Foxglove Studio (https://foxglove.dev/download or app.foxglove.dev):"
  echo "  Open local file -> ${REPO}/${mcaps[0]}"
  echo "  add a 3D panel (frame 'map': /tf, /scan, /plan) and a Plot of /odom to see the robot's path;"
  echo "  /fieldproof/assertions carries the verdicts, /fieldproof/metrics the real-time factor."
fi
if [[ "$status" -eq 0 ]]; then
  echo
  echo "M1 demo: PASS"
else
  echo
  echo "M1 demo: the run ended with exit code ${status}; see ${run_dir}/engine.log and ${run_dir}/logs/" >&2
fi
exit "$status"
