#!/usr/bin/env bash
# Fieldproof host check (task P0-T02).
#
# Verifies every host prerequisite from docs/setup/host.md and prints the installed versions.
# Read-only and idempotent: run it as often as you like. Exit status is 0 when every check passes
# and 1 otherwise. Install or repair the host with tools/host-setup.sh.
#
# Usage:
#   tools/host-check.sh            all checks, including two short container smoke tests
#   tools/host-check.sh --quick    skip the container smoke tests (no image pulls, no docker run)
set -euo pipefail

# The smoke tests use the plain tag so they match the task's definition of done verbatim; digests
# are pinned where images are built (docker/, task P0-T03), not here.
SMOKE_IMAGE="ubuntu:24.04"
EXPECTED_NODE_MAJOR=22

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    -h | --help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument '$arg' (expected --quick or --help)" >&2
      exit 2
      ;;
  esac
done

FAILURES=0
pass() { printf '  \033[1;32mok\033[0m    %-24s %s\n' "$1" "$2"; }
fail() {
  printf '  \033[1;31mFAIL\033[0m  %-24s %s\n' "$1" "$2"
  FAILURES=$((FAILURES + 1))
}
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# Run docker as the current user, or through `sg docker` when the user was added to the group but
# has not logged in again yet (the common state right after tools/host-setup.sh).
DOCKER_VIA_SG=0
if ! id -nG | tr ' ' '\n' | grep -qx docker; then
  if getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$(id -un)"; then
    DOCKER_VIA_SG=1
  fi
fi
dkr() {
  if [[ "$DOCKER_VIA_SG" -eq 1 ]]; then
    sg docker -c "$(printf '%q ' docker "$@")"
  else
    docker "$@"
  fi
}

# ---------------------------------------------------------------------------------------------
section "Operating system"
# shellcheck disable=SC1091
os_pretty="$(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")"
# shellcheck disable=SC1091
os_id="$(. /etc/os-release && echo "${ID:-}:${VERSION_ID:-}")"
if [[ "$os_id" == "ubuntu:24.04" ]]; then
  pass "ubuntu 24.04" "$os_pretty, $(uname -m), kernel $(uname -r)"
else
  fail "ubuntu 24.04" "found '$os_pretty'; the host toolchain is pinned for Ubuntu 24.04"
fi

# ---------------------------------------------------------------------------------------------
section "Docker"
if have docker; then
  pass "docker cli" "$(docker --version)"
else
  fail "docker cli" "not found; run tools/host-setup.sh"
fi

if id -nG | tr ' ' '\n' | grep -qx docker; then
  pass "docker group" "$(id -un) is in docker"
elif [[ "$DOCKER_VIA_SG" -eq 1 ]]; then
  pass "docker group" "$(id -un) is in docker but the group is not active in this shell yet: log out and back in (checks below use 'sg docker')"
else
  fail "docker group" "$(id -un) is not in the docker group; run tools/host-setup.sh and log in again"
fi

if have docker && server="$(dkr info --format '{{.ServerVersion}}' 2>/dev/null)"; then
  pass "docker daemon" "server $server, $(dkr info --format 'storage {{.Driver}}, cgroup {{.CgroupDriver}} v{{.CgroupVersion}}' 2>/dev/null)"
else
  fail "docker daemon" "cannot talk to the Docker daemon (is docker.service running? are you in the docker group?)"
fi

if have docker && compose="$(dkr compose version --short 2>/dev/null)"; then
  pass "docker compose" "v$compose"
else
  fail "docker compose" "compose plugin missing (docker-compose-plugin)"
fi

if have docker && buildx="$(dkr buildx version 2>/dev/null)"; then
  pass "docker buildx" "$buildx"
else
  fail "docker buildx" "buildx plugin missing (docker-buildx-plugin)"
fi

# ---------------------------------------------------------------------------------------------
section "ARM64 emulation"
if qemu_ver="$(dpkg-query -W -f='${Version}' qemu-user-static 2>/dev/null)"; then
  pass "qemu-user-static" "$qemu_ver"
else
  fail "qemu-user-static" "not installed"
fi

binfmt=/proc/sys/fs/binfmt_misc/qemu-aarch64
if [[ -r "$binfmt" ]] && [[ "$(head -n1 "$binfmt")" == "enabled" ]]; then
  flags="$(awk '/^flags:/ { print $2 }' "$binfmt")"
  if [[ "$flags" == *F* ]]; then
    pass "binfmt qemu-aarch64" "enabled, flags $flags (F = interpreter works inside containers)"
  else
    fail "binfmt qemu-aarch64" "enabled but flags '$flags' lack F; re-run tools/host-setup.sh"
  fi
else
  fail "binfmt qemu-aarch64" "not registered; re-run tools/host-setup.sh"
fi

# ---------------------------------------------------------------------------------------------
section "Node"
export NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"
if [[ -s "${NVM_DIR}/nvm.sh" ]]; then
  # nvm.sh is not written for `set -u`; relax it while sourcing and selecting the default version.
  set +u
  # shellcheck disable=SC1091
  . "${NVM_DIR}/nvm.sh" --no-use
  nvm use --silent default >/dev/null 2>&1 || true
  nvm_ver="$(nvm --version)"
  set -u
  pass "nvm" "$nvm_ver at $NVM_DIR"
else
  fail "nvm" "not found at $NVM_DIR; run tools/host-setup.sh"
fi

if have node; then
  node_ver="$(node --version)"
  node_major="${node_ver#v}"
  node_major="${node_major%%.*}"
  if [[ "$node_major" -eq "$EXPECTED_NODE_MAJOR" ]] && [[ "$(command -v node)" == "$NVM_DIR"/* ]]; then
    pass "node $EXPECTED_NODE_MAJOR lts" "$node_ver at $(command -v node)"
  elif [[ "$node_major" -eq "$EXPECTED_NODE_MAJOR" ]]; then
    fail "node $EXPECTED_NODE_MAJOR lts" "$node_ver is on PATH from $(command -v node), not from nvm"
  else
    fail "node $EXPECTED_NODE_MAJOR lts" "found $node_ver at $(command -v node); expected major $EXPECTED_NODE_MAJOR via nvm"
  fi
else
  fail "node $EXPECTED_NODE_MAJOR lts" "not found"
fi

if have corepack; then
  pass "corepack" "$(corepack --version)"
else
  fail "corepack" "not found (ships with Node 22)"
fi

if have pnpm && pnpm_ver="$(COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm --version 2>/dev/null)"; then
  pass "pnpm" "$pnpm_ver via $(command -v pnpm)"
else
  fail "pnpm" "not found or not activated through corepack"
fi

# ---------------------------------------------------------------------------------------------
section "Python tooling"
if have uv; then
  pass "uv" "$(uv --version)"
else
  fail "uv" "not found; run tools/host-setup.sh"
fi

if have pre-commit; then
  pass "pre-commit" "$(pre-commit --version)"
else
  fail "pre-commit" "not found; run tools/host-setup.sh"
fi

# ---------------------------------------------------------------------------------------------
section "GitHub CLI"
if have gh; then
  pass "gh" "$(gh --version | head -n1)"
  if gh auth status >/dev/null 2>&1; then
    account="$(gh auth status 2>&1 | grep -m1 -oE 'account [^ ]+' || echo "account unknown")"
    pass "gh auth" "logged in to github.com, $account"
  else
    fail "gh auth" "not authenticated; run: gh auth login --web --git-protocol https"
  fi
else
  fail "gh" "not found"
fi

# ---------------------------------------------------------------------------------------------
section "GPU render node"
render_nodes="$(find /dev/dri -maxdepth 1 -name 'renderD*' 2>/dev/null | sort | tr '\n' ' ' || true)"
if [[ -n "$render_nodes" ]]; then
  pass "/dev/dri render node" "$render_nodes"
else
  fail "/dev/dri render node" "no /dev/dri/renderD* device; headless GPU rendering in containers is unavailable"
fi

# ---------------------------------------------------------------------------------------------
section "Container smoke tests"
if [[ "$QUICK" -eq 1 ]]; then
  printf '  skipped (--quick)\n'
elif ! have docker; then
  fail "smoke tests" "docker is not installed"
else
  if dri_out="$(dkr run --rm --device /dev/dri "$SMOKE_IMAGE" ls /dev/dri 2>/dev/null)" \
    && grep -q '^renderD' <<<"$dri_out"; then
    pass "/dev/dri in container" "$(tr '\n' ' ' <<<"$dri_out")"
  else
    fail "/dev/dri in container" "docker run --rm --device /dev/dri $SMOKE_IMAGE ls /dev/dri did not list a render node"
  fi

  if arm_out="$(dkr run --rm --platform linux/arm64 "$SMOKE_IMAGE" uname -m 2>/dev/null)" \
    && [[ "$arm_out" == "aarch64" ]]; then
    pass "arm64 container" "uname -m = $arm_out"
  else
    fail "arm64 container" "docker run --rm --platform linux/arm64 $SMOKE_IMAGE uname -m printed '${arm_out:-nothing}', expected aarch64"
  fi
fi

# ---------------------------------------------------------------------------------------------
printf '\n'
if [[ "$FAILURES" -eq 0 ]]; then
  printf '\033[1;32mAll host checks passed.\033[0m\n'
else
  printf '\033[1;31m%d host check(s) failed.\033[0m See docs/setup/host.md.\n' "$FAILURES"
  exit 1
fi
