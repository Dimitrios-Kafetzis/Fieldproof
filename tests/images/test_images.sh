#!/usr/bin/env bash
# Tests for the container images (task P0-T03): docker/base-ros2, docker/sim, docker/dev,
# docker/compose.yaml and the Makefile targets.
#
# 1. Static checks: shellcheck and hadolint (pinned containers when no local binary), compose
#    config validation, the vendored ROS snapshot keyring fingerprint.
# 2. Build: `make images` cold (NO_CACHE=1) must finish in under 15 minutes. With --quick the
#    build reuses the cache and the time limit is not asserted.
# 3. Stack: `make up` starts `gz sim -s -r empty.sdf`; `ros2 topic list` and `gz topic -l` work
#    from the dev container; `make down` stops it.
# 4. Rendering: a camera sensor renders a frame headless through /dev/dri (skipped when the host
#    has no render node) and with software rendering (LIBGL_ALWAYS_SOFTWARE=1).
#
# Usage: tests/images/test_images.sh [--quick] [--help]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
HADOLINT_IMAGE="hadolint/hadolint:v2.12.0@sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e"
ROS_SNAPSHOT_FINGERPRINT="4B63CF8FDE49746E98FA01DDAD19BAB3CBF125EA"
COLD_BUILD_LIMIT_S=900
RENDER_TIMEOUT_S=180

SCRIPTS=(docker/base-ros2/entrypoint.sh docker/base-ros2/apt-install.sh docker/sim/render-info.sh tests/images/test_images.sh)
DOCKERFILES=(docker/base-ros2/Dockerfile docker/sim/Dockerfile docker/dev/Dockerfile)

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    -h | --help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument '$arg' (expected --quick or --help)" >&2
      exit 2
      ;;
  esac
done

PASSED=0
FAILED=0
pass() {
  printf '  \033[1;32mok\033[0m    %s\n' "$1"
  PASSED=$((PASSED + 1))
}
fail() {
  printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"
  FAILED=$((FAILED + 1))
}
skip() { printf '  \033[1;33mskip\033[0m  %s\n' "$1"; }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cd "$REPO"
RENDER_GID="$(getent group render | cut -d: -f3 || true)"
export FIELDPROOF_RENDER_GID="${RENDER_GID:-992}"
RENDER_NODE="$(find /dev/dri -maxdepth 1 -name 'renderD*' 2>/dev/null | head -n1 || true)"
SIM_IMAGE="$(docker compose -f docker/compose.yaml --profile sim config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["sim"]["image"])')"

cleanup() { make down >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ---------------------------------------------------------------------------------------------
section "Static checks"
for script in "${SCRIPTS[@]}"; do
  if bash -n "$script"; then pass "bash -n $script"; else fail "bash -n $script"; fi
done

if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=style --shell=bash "${SCRIPTS[@]}"; then pass "shellcheck (local)"; else fail "shellcheck (local)"; fi
elif docker run --rm -v "${REPO}:/mnt:ro" "$SHELLCHECK_IMAGE" --severity=style --shell=bash "${SCRIPTS[@]}"; then
  pass "shellcheck (container ${SHELLCHECK_IMAGE%%@*})"
else
  fail "shellcheck (container)"
fi

hadolint_ok=1
for df in "${DOCKERFILES[@]}"; do
  if command -v hadolint >/dev/null 2>&1; then
    hadolint "$df" || hadolint_ok=0
  else
    docker run --rm -i "$HADOLINT_IMAGE" hadolint - <"$df" || hadolint_ok=0
  fi
done
if [[ "$hadolint_ok" -eq 1 ]]; then pass "hadolint ${DOCKERFILES[*]}"; else fail "hadolint"; fi

if docker compose -f docker/compose.yaml --profile '*' config --quiet \
  && docker compose -f docker/compose.yaml -f docker/compose.gpu.yaml --profile '*' config --quiet; then
  pass "docker compose config (default and gpu override)"
else
  fail "docker compose config"
fi

if command -v gpg >/dev/null 2>&1; then
  fpr="$(gpg --show-keys --with-colons docker/base-ros2/ros-snapshot-keyring.gpg 2>/dev/null | awk -F: '/^fpr:/ { print $10; exit }')"
  if [[ "$fpr" == "$ROS_SNAPSHOT_FINGERPRINT" ]]; then
    pass "ROS snapshot keyring fingerprint $fpr"
  else
    fail "ROS snapshot keyring fingerprint is '${fpr:-unreadable}', expected $ROS_SNAPSHOT_FINGERPRINT"
  fi
else
  skip "ROS snapshot keyring fingerprint (gpg not installed on the host)"
fi

# ---------------------------------------------------------------------------------------------
section "Build"
if [[ "$QUICK" -eq 1 ]]; then
  start=$SECONDS
  if make images >/dev/null 2>&1; then pass "make images (cached) in $((SECONDS - start)) s"; else fail "make images"; fi
else
  start=$SECONDS
  if NO_CACHE=1 make images >/dev/null 2>&1; then
    elapsed=$((SECONDS - start))
    if [[ "$elapsed" -le "$COLD_BUILD_LIMIT_S" ]]; then
      pass "make images cold (NO_CACHE=1) in $elapsed s (limit $COLD_BUILD_LIMIT_S s)"
    else
      fail "make images cold (NO_CACHE=1) took $elapsed s, limit $COLD_BUILD_LIMIT_S s"
    fi
  else
    fail "make images cold (NO_CACHE=1)"
  fi
fi

for name in base-ros2 sim dev; do
  image="${SIM_IMAGE/sim:/${name}:}"
  rev="$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
  if [[ "$rev" == "$(git rev-parse HEAD)" ]]; then
    pass "$image labelled with revision ${rev:0:7}"
  else
    fail "$image revision label is '${rev:-missing}'"
  fi
done

if [[ "$(docker run --rm "$SIM_IMAGE" id -u)" == "1000" ]]; then
  pass "sim image runs as uid 1000 (non-root)"
else
  fail "sim image does not run as uid 1000"
fi

# ---------------------------------------------------------------------------------------------
section "Stack (make up / make down)"
if make up >/dev/null 2>&1; then pass "make up"; else fail "make up"; fi

compose_dev() { docker compose -f docker/compose.yaml --profile dev exec -T dev bash -lc "$1"; }

sim_id="$(docker compose -f docker/compose.yaml --profile sim ps -q sim 2>/dev/null || true)"
sim_cmd="$(docker inspect --format '{{join .Config.Cmd " "}}' "${sim_id:-none}" 2>/dev/null || true)"
if [[ "$sim_cmd" == "gz sim -s -r"*"empty.sdf" ]]; then
  pass "sim container runs: $sim_cmd"
else
  fail "sim container command is '${sim_cmd:-not running}'"
fi

if topics="$(compose_dev 'ros2 topic list' 2>&1)" && grep -q '^/rosout$' <<<"$topics"; then
  pass "ros2 topic list from dev: $(tr '\n' ' ' <<<"$topics")"
else
  fail "ros2 topic list from dev: ${topics:-no output}"
fi

gz_topics=""
for _ in $(seq 1 20); do
  gz_topics="$(compose_dev 'gz topic -l' 2>/dev/null || true)"
  grep -q '^/world/empty/clock$' <<<"$gz_topics" && break
  sleep 1
done
if grep -q '^/world/empty/clock$' <<<"$gz_topics"; then
  pass "gz topic -l from dev sees the sim ($(grep -c . <<<"$gz_topics") topics)"
else
  fail "gz topic -l from dev does not list /world/empty/clock: ${gz_topics:-no output}"
fi

if make down >/dev/null 2>&1 && [[ -z "$(docker compose -f docker/compose.yaml --profile '*' ps -q)" ]]; then
  pass "make down"
else
  fail "make down left containers running"
fi

# ---------------------------------------------------------------------------------------------
section "Headless camera rendering"
# render_check <label> <docker run args...>: start the camera world headless, grab one frame on
# /camera and report its size, the EGL device Ogre initialised and the OpenGL renderer Mesa
# exposes on the EGL device platform. Sets RENDERER_LINE for the caller.
render_check() {
  local label="$1"
  shift
  local out
  out="$(docker run --rm "$@" -v "${REPO}/tests/images/worlds:/worlds:ro" "$SIM_IMAGE" bash -c '
    gz sim -s -r --headless-rendering -v 1 /worlds/camera_check.sdf >/tmp/gz-sim.log 2>&1 &
    if ! timeout '"$RENDER_TIMEOUT_S"' gz topic -e -t /camera -n 1 >/tmp/frame.txt 2>/dev/null; then
      echo "no frame on /camera within '"$RENDER_TIMEOUT_S"' s"; cat /tmp/gz-sim.log; exit 1
    fi
    width=$(awk "/^width:/ { print \$2; exit }" /tmp/frame.txt)
    height=$(awk "/^height:/ { print \$2; exit }" /tmp/frame.txt)
    nonzero=$(grep -m1 "^data:" /tmp/frame.txt | sed "s/\\\\000//g" | wc -c)
    device=$(grep -oE "Created GL [0-9.]+ context for device [A-Za-z0-9_]+" ~/.gz/rendering/ogre2.log | tail -n1 | sed "s/.*for device //")
    eglinfo=$(find /usr/bin -maxdepth 1 -name "eglinfo*" | head -n1)
    renderer=$("$eglinfo" -B 2>/dev/null | awk "/^Device platform:/ { f = 1 } f && /OpenGL core profile renderer:/ { sub(/.*renderer: /, \"\"); print; exit }")
    echo "frame=${width}x${height} nonzero_bytes=${nonzero} egl_device=${device:-unknown} renderer=${renderer:-unknown}"
    [ "$width" = "320" ] && [ "$height" = "240" ] && [ "$nonzero" -gt 1000 ]
  ' 2>&1)" && { pass "$label: $out"; RENDERER_LINE="$out"; return 0; }
  fail "$label: $(tail -n 3 <<<"$out" | tr '\n' ' ')"
  RENDERER_LINE=""
  return 1
}

RENDERER_LINE=""
if [[ -n "$RENDER_NODE" ]]; then
  if render_check "camera via /dev/dri" --device /dev/dri --group-add "$FIELDPROOF_RENDER_GID" -e LIBGL_ALWAYS_SOFTWARE=0; then
    if grep -q 'egl_device=EGL_EXT_device_drm' <<<"$RENDERER_LINE" && ! grep -qi 'renderer=llvmpipe' <<<"$RENDERER_LINE"; then
      pass "camera via /dev/dri used the DRM device with a hardware driver"
    else
      fail "camera via /dev/dri did not use a hardware driver on the DRM device"
    fi
  fi
else
  skip "camera via /dev/dri (no /dev/dri/renderD* on this host)"
fi

if render_check "camera with LIBGL_ALWAYS_SOFTWARE=1" -e LIBGL_ALWAYS_SOFTWARE=1; then
  if grep -qi 'renderer=llvmpipe' <<<"$RENDERER_LINE"; then
    pass "software rendering used llvmpipe"
  else
    fail "software rendering did not report llvmpipe: $RENDERER_LINE"
  fi
fi

# ---------------------------------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
