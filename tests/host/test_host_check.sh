#!/usr/bin/env bash
# Tests for the host scripts (task P0-T02): tools/host-check.sh and tools/host-setup.sh.
#
# 1. Both scripts parse and pass shellcheck (local binary, or the pinned container when absent).
# 2. host-check.sh handles its arguments: --help exits 0, an unknown flag exits 2.
# 3. Against an empty toolchain host-check.sh reports each missing prerequisite and exits 1.
# 4. On a prepared host host-check.sh passes twice in a row (idempotence). Set
#    FIELDPROOF_SKIP_HOST=1 to skip this step on machines that are not set up (for example CI).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
SCRIPTS=(tools/host-setup.sh tools/host-check.sh tests/host/test_host_check.sh)

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
strip_colour() { sed 's/\x1b\[[0-9;]*m//g'; }

# Run docker directly or through `sg docker` (membership present but not active in this shell).
dkr() {
  if id -nG | tr ' ' '\n' | grep -qx docker; then
    docker "$@"
  else
    sg docker -c "$(printf '%q ' docker "$@")"
  fi
}

cd "$REPO"

printf '\n\033[1mSyntax and lint\033[0m\n'
for script in "${SCRIPTS[@]}"; do
  if bash -n "$script"; then pass "bash -n $script"; else fail "bash -n $script"; fi
done

if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=style --shell=bash "${SCRIPTS[@]}"; then
    pass "shellcheck (local $(shellcheck --version | awk '/^version:/ { print $2 }'))"
  else
    fail "shellcheck (local)"
  fi
elif command -v docker >/dev/null 2>&1; then
  if dkr run --rm -v "${REPO}:/mnt:ro" "$SHELLCHECK_IMAGE" --severity=style --shell=bash "${SCRIPTS[@]}"; then
    pass "shellcheck (container ${SHELLCHECK_IMAGE%%@*})"
  else
    fail "shellcheck (container)"
  fi
else
  fail "shellcheck: neither a local binary nor docker is available"
fi

printf '\n\033[1mArgument handling\033[0m\n'
if tools/host-check.sh --help >/dev/null 2>&1; then pass "--help exits 0"; else fail "--help exits 0"; fi
if tools/host-check.sh --bogus >/dev/null 2>&1; then
  fail "unknown flag exits 2"
else
  status=$?
  if [[ "$status" -eq 2 ]]; then pass "unknown flag exits 2"; else fail "unknown flag exits 2 (got $status)"; fi
fi

printf '\n\033[1mEmpty toolchain\033[0m\n'
# A fake PATH with only the coreutils the check script itself needs, and an empty HOME, so every
# prerequisite is missing. --quick avoids touching the Docker daemon.
sandbox="$(mktemp -d)"
trap 'rm -rf "$sandbox"' EXIT
mkdir -p "$sandbox/bin" "$sandbox/home"
for tool in bash sh awk sed grep head tr cut sort find id getent uname dpkg-query env; do
  path="$(command -v "$tool")" && ln -s "$path" "$sandbox/bin/$tool"
done
set +e
empty_out="$(env -i HOME="$sandbox/home" PATH="$sandbox/bin" NVM_DIR="$sandbox/home/.nvm" \
  bash tools/host-check.sh --quick 2>&1 | strip_colour)"
empty_status=${PIPESTATUS[0]}
set -e
if [[ "$empty_status" -eq 1 ]]; then pass "exit status 1"; else fail "exit status 1 (got $empty_status)"; fi
for name in "docker cli" "nvm" "node 22 lts" "corepack" "pnpm" "uv" "pre-commit" "gh"; do
  if grep -qE "^  FAIL  ${name} " <<<"$empty_out"; then
    pass "reports missing '${name}'"
  else
    fail "reports missing '${name}'"
  fi
done
if grep -q 'host check(s) failed' <<<"$empty_out"; then pass "prints failure summary"; else fail "prints failure summary"; fi

printf '\n\033[1mPrepared host (idempotence)\033[0m\n'
if [[ "${FIELDPROOF_SKIP_HOST:-0}" == "1" ]]; then
  printf '  skipped (FIELDPROOF_SKIP_HOST=1)\n'
else
  for run in 1 2; do
    if out="$(tools/host-check.sh 2>&1 | strip_colour)" && grep -q 'All host checks passed' <<<"$out"; then
      pass "host-check.sh run $run passes"
    else
      fail "host-check.sh run $run passes"
      printf '%s\n' "$out" | sed 's/^/      /'
    fi
  done
fi

printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
