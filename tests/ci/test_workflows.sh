#!/usr/bin/env bash
# Tests for the CI/CD pipeline (task P0-T05): .github/workflows, .github/dependabot.yml, the
# release-please configuration, docker/trivy-policy.rego and the helpers in tools/ci.
#
# 1. Static checks: bash -n and shellcheck on the helpers and this script; actionlint, the
#    GitHub workflow and Dependabot schemas through the pre-commit hooks.
# 2. Workflow rules: every external action is pinned to a 40-character commit SHA with a version
#    comment; every workflow declares top-level permissions and a concurrency group; every job
#    has timeout-minutes; the required status checks that tools/ci/branch-protection.sh wires
#    exist as job names; ci.yml's gate job needs every other job and runs `if: always()`.
# 3. Helpers: changed-areas.sh in a scratch repository (no base, docs-only change, docker change),
#    bake-cache.sh output, free-disk.sh outside GitHub Actions.
# 4. Configuration: release-please config and manifest agree with version.txt and produce a
#    pull request title that commitlint accepts; Dependabot covers github-actions, docker, pip and
#    npm with grouped weekly updates and a `build(deps)` prefix; `docker buildx bake --print`
#    on docker/compose.yaml lists the three image targets the workflows build.
# 5. Trivy policy: when the base-ros2 image is present locally, the pinned Trivy container with
#    docker/trivy-policy.rego reports no fixed CRITICAL vulnerability (skipped otherwise).
#
# Needs pre-commit, Docker and python3 on the host, as installed by tools/host-setup.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
TRIVY_IMAGE="aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
SCRIPTS=(tools/ci/changed-areas.sh tools/ci/bake-cache.sh tools/ci/free-disk.sh tools/ci/branch-protection.sh
  tests/ci/test_workflows.sh)
WORKFLOWS=(.github/workflows/*.yml)

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

# ---------------------------------------------------------------------------------------------
section "1. Static checks"
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
for hook in actionlint check-github-workflows check-dependabot; do
  if pre-commit run "$hook" --all-files >"$SCRATCH/$hook.log" 2>&1; then
    pass "pre-commit hook $hook"
  else
    fail "pre-commit hook $hook"
    sed 's/^/        /' "$SCRATCH/$hook.log" | tail -n 20
  fi
done

# ---------------------------------------------------------------------------------------------
section "2. Workflow rules"
# One pass over every workflow with PyYAML (ships with the python3 of the host and of the runner).
python3 - "${WORKFLOWS[@]}" >"$SCRATCH/rules.txt" <<'PY'
import re
import sys

import yaml

PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")
problems = []
jobs_by_file = {}
for path in sys.argv[1:]:
    text = open(path).read()
    doc = yaml.safe_load(text)
    if "permissions" not in doc:
        problems.append(f"{path}: no top-level permissions")
    if "concurrency" not in doc:
        problems.append(f"{path}: no concurrency group")
    names = []
    for job_id, job in doc["jobs"].items():
        names.append(job.get("name", job_id))
        if "uses" in job:  # reusable workflow call
            continue
        if "timeout-minutes" not in job:
            problems.append(f"{path}: job {job_id} has no timeout-minutes")
    jobs_by_file[path] = names
    for line in text.splitlines():
        m = re.search(r"uses:\s*(\S+)(.*)$", line)
        if not m or m.group(1).startswith("./"):
            continue
        ref, rest = m.group(1), m.group(2)
        if not PIN.match(ref):
            problems.append(f"{path}: {ref} is not pinned to a commit SHA")
        elif not re.search(r"#\s*v?\d+(\.\d+)*", rest):
            problems.append(f"{path}: {ref} has no version comment")
print("JOBS " + " | ".join(f"{p}: {', '.join(n)}" for p, n in jobs_by_file.items()))
for p in problems:
    print("PROBLEM " + p)
PY
if grep -q '^PROBLEM' "$SCRATCH/rules.txt"; then
  fail "workflow rules (SHA pins, permissions, concurrency, timeouts)"
  grep '^PROBLEM' "$SCRATCH/rules.txt" | sed 's/^PROBLEM/        /'
else
  pass "every action is SHA-pinned with a version comment; permissions, concurrency and timeouts declared"
fi

# shellcheck disable=SC1090  # read REQUIRED_CHECKS from the protection script without running it
required="$(bash -c 'source <(grep "^REQUIRED_CHECKS=" tools/ci/branch-protection.sh); printf "%s\n" "${REQUIRED_CHECKS[@]}"')"
while IFS= read -r context; do
  if grep -q "name: ${context}\$" .github/workflows/ci.yml .github/workflows/pr-title.yml; then
    pass "required check '$context' is a job name in the workflows"
  else
    fail "required check '$context' is not a job name in the workflows"
  fi
done <<<"$required"

if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/ci.yml"))
jobs = doc["jobs"]
gate = jobs["ci"]
others = sorted(j for j in jobs if j != "ci")
assert sorted(gate["needs"]) == others, (gate["needs"], others)
assert str(gate.get("if", "")).strip() == "always()", gate.get("if")
assert gate["name"] == "CI"
assert jobs["images"]["if"].strip() == "needs.changes.outputs.images == 'true'"
PY
then pass "ci.yml gate job needs every other job and runs if: always()"; else fail "ci.yml gate job"; fi

if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/images.yml"))
jobs = doc["jobs"]
assert jobs["scan"]["needs"] == "build"
assert sorted(jobs["tag"]["needs"]) == ["build", "scan"]
assert jobs["scan"]["strategy"]["matrix"]["image"] == ["base-ros2", "sim", "dev"]
assert "workflow_call" in (doc[True] if True in doc else doc["on"])
PY
then pass "images.yml: sha tag pushed first, floating tag only after the scan"; else fail "images.yml job order"; fi

# ---------------------------------------------------------------------------------------------
section "3. Helpers"
scratch_repo="$SCRATCH/repo"
mkdir -p "$scratch_repo"
git -C "$scratch_repo" init -q -b main
git -C "$scratch_repo" config user.name "Fieldproof tests"
git -C "$scratch_repo" config user.email "tests@fieldproof.invalid"
git -C "$scratch_repo" config commit.gpgsign false
mkdir -p "$scratch_repo/docs" "$scratch_repo/docker/sim"
echo a >"$scratch_repo/docs/a.md"
echo b >"$scratch_repo/docker/sim/Dockerfile"
git -C "$scratch_repo" add -A && git -C "$scratch_repo" commit -q -m "chore(repo): base"
base="$(git -C "$scratch_repo" rev-parse HEAD)"
changed="$REPO/tools/ci/changed-areas.sh"

if [[ "$(cd "$scratch_repo" && "$changed" 2>/dev/null)" == "images=true" ]]; then
  pass "changed-areas: no base runs every area"
else
  fail "changed-areas: no base runs every area"
fi
echo a2 >"$scratch_repo/docs/a.md"
git -C "$scratch_repo" commit -q -am "docs(docs): change"
if [[ "$(cd "$scratch_repo" && "$changed" "$base" 2>/dev/null)" == "images=false" ]]; then
  pass "changed-areas: docs-only change skips images"
else
  fail "changed-areas: docs-only change skips images"
fi
echo b2 >"$scratch_repo/docker/sim/Dockerfile"
git -C "$scratch_repo" commit -q -am "build(docker): change"
if [[ "$(cd "$scratch_repo" && "$changed" "$base" 2>/dev/null)" == "images=true" ]]; then
  pass "changed-areas: docker change runs images"
else
  fail "changed-areas: docker change runs images"
fi
if [[ "$(cd "$scratch_repo" && "$changed" 0000000000000000000000000000000000000000 2>/dev/null)" == "images=true" ]]; then
  pass "changed-areas: unknown base falls back to every area"
else
  fail "changed-areas: unknown base falls back to every area"
fi

expected_cache="base-ros2.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:buildcache
base-ros2.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:pr,mode=max
sim.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-sim:buildcache
sim.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-sim:pr,mode=max
dev.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-dev:buildcache
dev.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-dev:pr,mode=max"
if [[ "$(tools/ci/bake-cache.sh --read buildcache --write pr)" == "$expected_cache" ]]; then
  pass "bake-cache: cache-from and cache-to lines for the three targets"
else
  fail "bake-cache: unexpected output"
  tools/ci/bake-cache.sh --read buildcache --write pr | sed 's/^/        /'
fi
if [[ "$(FIELDPROOF_IMAGE_PREFIX=example.org/fp- tools/ci/bake-cache.sh --read x | head -n 1)" == \
  "base-ros2.cache-from=type=registry,ref=example.org/fp-base-ros2:x" ]]; then
  pass "bake-cache: honours FIELDPROOF_IMAGE_PREFIX"
else
  fail "bake-cache: honours FIELDPROOF_IMAGE_PREFIX"
fi
status=0
tools/ci/bake-cache.sh --bogus >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "bake-cache: unknown flag exits 2"; else fail "bake-cache: unknown flag exits 2 (got $status)"; fi
if out="$(GITHUB_ACTIONS=false tools/ci/free-disk.sh)" && grep -q "nothing to do" <<<"$out"; then
  pass "free-disk: no-op outside GitHub Actions"
else
  fail "free-disk: no-op outside GitHub Actions"
fi
for helper in tools/ci/bake-cache.sh tools/ci/branch-protection.sh; do
  if "$helper" --help | grep -q '^Usage:'; then pass "$helper --help"; else fail "$helper --help"; fi
done

# ---------------------------------------------------------------------------------------------
section "4. Configuration"
version="$(cat version.txt)"
manifest_version="$(python3 -c 'import json; print(json.load(open(".release-please-manifest.json"))["."])')"
if [[ "$version" == "$manifest_version" ]]; then
  pass "version.txt ($version) matches .release-please-manifest.json"
else
  fail "version.txt ($version) differs from .release-please-manifest.json ($manifest_version)"
fi
rp_version_file="$(python3 -c 'import json; print(json.load(open("release-please-config.json"))["packages"]["."]["version-file"])')"
if [[ "$rp_version_file" == "version.txt" ]]; then
  pass "release-please updates version.txt"
else
  fail "release-please version-file is '$rp_version_file'"
fi
rp_title="$(python3 -c 'import json; print(json.load(open("release-please-config.json"))["pull-request-title-pattern"])')"
rp_title="${rp_title//\$\{version\}/0.1.0}"
if make -s lint-title TITLE="$rp_title" >/dev/null 2>&1; then
  pass "release pull request title '$rp_title' passes commitlint"
else
  fail "release pull request title '$rp_title' fails commitlint"
fi
for t in "feat(engine): add scenario loader" "build(deps): bump the actions group with 3 updates"; do
  if make -s lint-title TITLE="$t" >/dev/null 2>&1; then pass "lint-title accepts '$t'"; else fail "lint-title accepts '$t'"; fi
done
for t in "P0-T06 Documentation site" "Added some stuff"; do
  if make -s lint-title TITLE="$t" >/dev/null 2>&1; then fail "lint-title rejects '$t'"; else pass "lint-title rejects '$t'"; fi
done

if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/dependabot.yml"))
eco = sorted(u["package-ecosystem"] for u in doc["updates"])
assert eco == ["docker", "github-actions", "npm", "pip"], eco
for u in doc["updates"]:
    assert u["schedule"]["interval"] == "weekly", u
    assert u["commit-message"]["prefix"] == "build(deps)", u
    assert u["groups"], u
PY
then pass "Dependabot: github-actions, docker, pip and npm, weekly, grouped, build(deps) prefix"; else fail "Dependabot configuration"; fi

# Run from docker/ with every profile enabled, exactly as the workflows do: bake resolves the
# build contexts relative to its working directory and profile-aware versions drop base-ros2.
if targets="$(cd docker && COMPOSE_PROFILES=images,sim,dev docker buildx bake -f compose.yaml --print 2>/dev/null \
  | python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin)["target"])))')" \
  && [[ "$targets" == "base-ros2 dev sim" ]]; then
  pass "docker buildx bake reads docker/compose.yaml: targets $targets"
else
  fail "docker buildx bake targets: '${targets:-none}'"
fi
if contexts="$(cd docker && COMPOSE_PROFILES=images,sim,dev docker buildx bake -f compose.yaml --print 2>/dev/null \
  | python3 -c 'import json,sys; print(" ".join(sorted(t["context"] for t in json.load(sys.stdin)["target"].values())))')" \
  && (cd docker && for c in $contexts; do [[ -f "$c/Dockerfile" ]] || exit 1; done); then
  pass "bake build contexts exist under docker/: $contexts"
else
  fail "bake build contexts do not resolve from docker/: '${contexts:-none}'"
fi

# ---------------------------------------------------------------------------------------------
section "5. Trivy policy"
base_image="$(docker compose -f docker/compose.yaml --profile images config --format json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["base-ros2"]["image"])')"
if docker image inspect "$base_image" >/dev/null 2>&1; then
  if docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
    -v "${REPO}/docker/trivy-policy.rego:/policy.rego:ro" -v fieldproof-trivy-cache:/root/.cache/trivy \
    "$TRIVY_IMAGE" image --scanners vuln --severity CRITICAL --ignore-unfixed --ignore-policy /policy.rego \
    --exit-code 1 --skip-version-check --quiet "$base_image" >"$SCRATCH/trivy.log" 2>&1; then
    pass "trivy with docker/trivy-policy.rego: no fixed CRITICAL vulnerability in $base_image"
  else
    fail "trivy with docker/trivy-policy.rego reports fixed CRITICAL vulnerabilities in $base_image"
    tail -n 30 "$SCRATCH/trivy.log" | sed 's/^/        /'
  fi
else
  skip "trivy policy ($base_image is not built locally; run make images first)"
fi

# ---------------------------------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
