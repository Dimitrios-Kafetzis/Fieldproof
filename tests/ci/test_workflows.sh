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
#    pull request title that commitlint accepts; Dependabot covers github-actions, pip (engine and
#    the docs toolchain) and npm with grouped weekly updates and a `build(deps)` prefix, and every
#    directory it watches exists; `docker buildx bake --print` on docker/compose.yaml lists the
#    three image targets the workflows build.
# 5. Trivy policy: when the base-ros2 image is present locally, the pinned Trivy container with
#    docker/trivy-policy.rego reports no fixed CRITICAL vulnerability (skipped otherwise).
# 6. Scenario tests (task P1-T07): scenario-tests.yml runs on main, nightly, on manual dispatch
#    with an image tag, pulls the published images, runs tools/ci/scenario-run.sh and uploads the
#    run whatever the outcome; scenario-run.sh against a stub `fieldproof`: a pass needs one
#    attempt, a failure is retried once and flagged as flaky when the retry passes, a second
#    failure fails, an interrupted run and a usage error are not retried, --retries 0 disables
#    the retry, the final attempt's report files and the step outputs land where the workflow
#    reads them; examples/demo_m1.sh prints its usage and, in a dry run, the commands of the
#    quick start with the image tag asked for.
#
# Needs pre-commit, Docker and python3 on the host, as installed by tools/host-setup.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
TRIVY_IMAGE="aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
SCRIPTS=(tools/ci/changed-areas.sh tools/ci/bake-cache.sh tools/ci/free-disk.sh tools/ci/branch-protection.sh
  tools/ci/scenario-run.sh examples/demo_m1.sh tests/ci/test_workflows.sh)
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
jobs = yaml.safe_load(open(".github/workflows/ci.yml"))["jobs"]
def runs(job):
    return "\n".join(str(s.get("run", "")) for s in jobs[job]["steps"])
assert "make test-engine" in runs("engine"), "engine job"
assert "make test-cockpit" in runs("cockpit"), "cockpit job"
assert "make test-layout" in runs("tooling-tests"), "layout tests"
assert "make test-ros" in runs("images") and "make tidy" in runs("images"), "ROS tests in the image job"
assert any("pnpm/action-setup" in str(s.get("uses", "")) for s in jobs["cockpit"]["steps"]), "pnpm setup"
PY
then pass "ci.yml: engine (uv), cockpit (pnpm), layout and ROS workspace (dev container) tests are wired"; else fail "ci.yml layer test jobs"; fi

if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/images.yml"))
jobs = doc["jobs"]
assert jobs["scan"]["needs"] == "build"
assert sorted(jobs["tag"]["needs"]) == ["build", "scan"]
assert jobs["scan"]["strategy"]["matrix"]["image"] == ["base-ros2", "sim", "dev", "bridge"]
assert "workflow_call" in (doc[True] if True in doc else doc["on"])
PY
then pass "images.yml: sha tag pushed first, floating tag only after the scan"; else fail "images.yml job order"; fi

if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/scenario-tests.yml"))
on = doc[True] if True in doc else doc["on"]
assert on["push"]["branches"] == ["main"], on["push"]
assert "schedule" in on and "workflow_dispatch" in on, on.keys()
assert on["workflow_dispatch"]["inputs"]["image-tag"]["default"] == "main"
assert ".github/workflows/scenario-tests.yml" in on["pull_request"]["paths"]
assert "examples/**" in on["pull_request"]["paths"]
assert list(doc["jobs"]) == ["scenario", "determinism", "matrix"], list(doc["jobs"])
assert on["workflow_dispatch"]["inputs"]["matrix"]["default"] == "examples/matrices/ugv_nav_matrix.yaml"
job = doc["jobs"]["scenario"]
assert job["runs-on"] == "ubuntu-24.04"
steps = job["steps"]
runs = "\n".join(str(s.get("run", "")) for s in steps)
assert "docker pull" in runs and "FIELDPROOF_IMAGE_TAG" in runs, "pulls the published images"
assert "make assets" in runs and "make vehicle-image" in runs
assert "tools/ci/scenario-run.sh" in runs and "--render software" in runs
uploads = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
assert len(uploads) == 2 and all(str(s.get("if", "")).strip() == "always()" for s in uploads)
paths = sorted(s["with"]["path"] for s in uploads)
assert paths == ["runs", "runs/junit.xml"], paths
assert any(
    "io.fieldproof.run" in str(s.get("run", "")) and str(s.get("if", "")).strip() == "always()"
    for s in steps
), "checks nothing is left"
# The determinism job (task P2-T04): two lockstep runs, not on pull requests.
det = doc["jobs"]["determinism"]
assert det["runs-on"] == "ubuntu-24.04"
assert det["if"].strip() == "github.event_name != 'pull_request'", det.get("if")
det_runs = "\n".join(str(s.get("run", "")) for s in det["steps"])
assert "docker pull" in det_runs and "make vehicle-image" in det_runs and "make assets" in det_runs
assert "make test-determinism" in det_runs and "--basetemp runs/determinism" in det_runs
det_uploads = [s for s in det["steps"] if "upload-artifact" in str(s.get("uses", ""))]
assert len(det_uploads) == 1 and det_uploads[0]["with"]["path"] == "runs/determinism"
assert str(det_uploads[0].get("if", "")).strip() == "always()"
assert any(
    "io.fieldproof.run" in str(s.get("run", "")) and str(s.get("if", "")).strip() == "always()"
    for s in det["steps"]
), "the determinism job checks nothing is left"
# The matrix job (task P2-T06): the example matrix through `fieldproof matrix`, not on pull requests.
mat = doc["jobs"]["matrix"]
assert mat["runs-on"] == "ubuntu-24.04"
assert mat["if"].strip() == "github.event_name != 'pull_request'", mat.get("if")
mat_runs = "\n".join(str(s.get("run", "")) for s in mat["steps"])
assert "docker pull" in mat_runs and "make vehicle-image" in mat_runs and "make assets" in mat_runs
assert "fieldproof matrix" in mat_runs and "--dry-run" in mat_runs and "--render software" in mat_runs
assert "fieldproof runs list" in mat_runs
mat_uploads = [s for s in mat["steps"] if "upload-artifact" in str(s.get("uses", ""))]
assert len(mat_uploads) == 1 and mat_uploads[0]["with"]["path"] == "runs"
assert str(mat_uploads[0].get("if", "")).strip() == "always()"
assert any(
    "io.fieldproof.run" in str(s.get("run", "")) and str(s.get("if", "")).strip() == "always()"
    for s in mat["steps"]
), "the matrix job checks nothing is left"
PY
then pass "scenario-tests.yml: main, nightly and dispatch; published images, scenario-run.sh, artefacts always uploaded; the determinism and matrix jobs"; else fail "scenario-tests.yml rules"; fi

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
ros_base="$(git -C "$scratch_repo" rev-parse HEAD)"
mkdir -p "$scratch_repo/sim/fieldproof_worlds"
echo c >"$scratch_repo/sim/fieldproof_worlds/CMakeLists.txt"
git -C "$scratch_repo" add -A && git -C "$scratch_repo" commit -q -m "feat(sim): change"
if [[ "$(cd "$scratch_repo" && "$changed" "$ros_base" 2>/dev/null)" == "images=true" ]]; then
  pass "changed-areas: a ROS workspace change runs the image job (colcon tests need the dev image)"
else
  fail "changed-areas: a ROS workspace change runs the image job"
fi

expected_cache="base-ros2.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:buildcache
base-ros2.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:pr,mode=max
sim.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-sim:buildcache
sim.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-sim:pr,mode=max
dev.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-dev:buildcache
dev.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-dev:pr,mode=max
bridge.cache-from=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-bridge:buildcache
bridge.cache-to=type=registry,ref=ghcr.io/dimitrios-kafetzis/fieldproof-bridge:pr,mode=max"
if [[ "$(tools/ci/bake-cache.sh --read buildcache --write pr)" == "$expected_cache" ]]; then
  pass "bake-cache: cache-from and cache-to lines for the four targets"
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
rp_initial="$(python3 -c 'import json; print(json.load(open("release-please-config.json")).get("initial-version", "1.0.0"))')"
if [[ "$rp_initial" == 0.* ]]; then
  pass "release-please starts below 1.0 (initial-version $rp_initial)"
else
  fail "release-please initial-version is '$rp_initial'; the first release must be below 1.0 (v1.0.0 is P7-T06)"
fi
if python3 - <<'PY'
import json
cfg = json.load(open("release-please-config.json"))
assert cfg.get("bump-minor-pre-major") is True, cfg
assert not cfg.get("bump-patch-for-minor-pre-major", False), "feat must bump the minor version below 1.0"
PY
then pass "release-please: below 1.0 a feat bumps the minor version and a breaking change does too"; else fail "release-please bump options"; fi
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
assert sorted(set(eco)) == ["github-actions", "npm", "uv"], eco  # no docker: issue #9, see pins.yml
dirs = sorted(u["directory"] for u in doc["updates"] if u["package-ecosystem"] == "uv")
assert dirs == ["/engine", "/tools/docs"], dirs
import os
for u in doc["updates"]:
    assert u["schedule"]["interval"] == "weekly", u
    assert u["commit-message"]["prefix"] == "build(deps)", u
    assert u["groups"], u
    assert os.path.isdir("." + u["directory"]), u["directory"]
PY
then pass "Dependabot: github-actions, uv (engine, tools/docs) and npm, weekly, grouped, build(deps) prefix, directories exist"; else fail "Dependabot configuration"; fi

# Run from docker/ with every profile enabled, exactly as the workflows do: bake resolves the
# build contexts relative to its working directory and profile-aware versions drop base-ros2.
if targets="$(cd docker && COMPOSE_PROFILES=images,sim,dev,bridge docker buildx bake -f compose.yaml --print 2>/dev/null \
  | python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin)["target"])))')" \
  && [[ "$targets" == "apt-lists base-ros2 bridge dev sim" ]]; then
  pass "docker buildx bake reads docker/compose.yaml: targets $targets"
else
  fail "docker buildx bake targets: '${targets:-none}'"
fi
if contexts="$(cd docker && COMPOSE_PROFILES=images,sim,dev,bridge docker buildx bake -f compose.yaml --print 2>/dev/null \
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
section "6. Scenario tests (scenario-run.sh with a stub fieldproof, demo_m1.sh dry run)"
stub_dir="$SCRATCH/stub"
mkdir -p "$stub_dir"
# The stub takes the exit code of each call from STUB_EXITS (one per attempt) and writes the
# three report files the runner would, so the wrapper has a verdict to read.
cat >"$stub_dir/fieldproof" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == run ]] || exit 2
shift
scenario="$1"
shift
runs_dir=""
run_id=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs-dir) runs_dir="$2"; shift 2 ;;
    --run-id) run_id="$2"; shift 2 ;;
    *) shift ;;
  esac
done
n="$(cat "$STUB_COUNT" 2>/dev/null || echo 0)"
n=$((n + 1))
echo "$n" >"$STUB_COUNT"
read -r -a exits <<<"$STUB_EXITS"
rc="${exits[$((n - 1))]}"
mkdir -p "$runs_dir/$run_id"
case "$rc" in 0) v=PASS ;; 1) v=FAIL ;; 130) v=INTERRUPTED ;; *) v=ERROR ;; esac
printf '{"verdict": "%s", "exit_code": %s}\n' "$v" "$rc" >"$runs_dir/$run_id/report.json"
printf '# Fieldproof run report: %s\n\n**%s**\n' "$(basename "$scenario")" "$v" >"$runs_dir/$run_id/report.md"
echo "<testsuites name=\"$v\"/>" >"$runs_dir/$run_id/junit.xml"
exit "$rc"
STUB
chmod +x "$stub_dir/fieldproof"
scenario_run="$REPO/tools/ci/scenario-run.sh"
scenario_file="$REPO/examples/scenarios/ugv_nav_to_goal.yaml"
# attempt EXITS NAME [ARGS...]: runs the wrapper in a fresh directory with the stub's exit codes;
# sets RC, OUT (the step outputs), SUMMARY, LOG and ATTEMPT_DIR.
attempt() {
  local exits="$1" name="$2"
  shift 2
  local dir="$SCRATCH/attempt-$name"
  rm -rf "$dir"
  mkdir -p "$dir"
  rm -f "$stub_dir/count"
  RC=0
  FIELDPROOF="$stub_dir/fieldproof" STUB_COUNT="$stub_dir/count" STUB_EXITS="$exits" \
    GITHUB_OUTPUT="$dir/output" GITHUB_STEP_SUMMARY="$dir/summary" \
    "$scenario_run" --runs-dir "$dir/runs" "$@" "$scenario_file" >"$dir/log" 2>&1 || RC=$?
  OUT="$(cat "$dir/output" 2>/dev/null || true)"
  SUMMARY="$(cat "$dir/summary" 2>/dev/null || true)"
  LOG="$(cat "$dir/log")"
  ATTEMPT_DIR="$dir"
}

attempt "0" pass
if [[ "$RC" -eq 0 ]] && grep -q '^attempts=1$' <<<"$OUT" && grep -q '^flaky=false$' <<<"$OUT" \
  && grep -q '^verdict=PASS$' <<<"$OUT" && ! grep -q '::warning' <<<"$LOG" \
  && grep -q '^## Scenario .ugv_nav_to_goal.: PASS' <<<"$SUMMARY" && ! grep -q 'Flaky' <<<"$SUMMARY" \
  && [[ "$(cat "$ATTEMPT_DIR/runs/junit.xml")" == '<testsuites name="PASS"/>' ]] \
  && [[ -f "$ATTEMPT_DIR/runs/report.md" && -f "$ATTEMPT_DIR/runs/report.json" ]]; then
  pass "scenario-run: a pass takes one attempt, no warning, the report files copied to the runs directory"
else
  fail "scenario-run: a pass takes one attempt"
  printf '%s\n' "$LOG" "$OUT" | sed 's/^/        /' | tail -n 20
fi

attempt "1 0" flaky
if [[ "$RC" -eq 0 ]] && grep -q '^attempts=2$' <<<"$OUT" && grep -q '^flaky=true$' <<<"$OUT" \
  && grep -q '^run_id=scenario-ugv_nav_to_goal-attempt2$' <<<"$OUT" \
  && grep -q '::warning title=Scenario ugv_nav_to_goal retried::' <<<"$LOG" \
  && grep -q '::warning title=Flaky scenario ugv_nav_to_goal::' <<<"$LOG" \
  && grep -q '\*\*Flaky:\*\* passed on attempt 2 of 2' <<<"$SUMMARY" \
  && grep -q '^- attempt 1: FAIL (scenario-ugv_nav_to_goal-attempt1)$' <<<"$SUMMARY" \
  && grep -q '^- attempt 2: PASS (scenario-ugv_nav_to_goal-attempt2)$' <<<"$SUMMARY" \
  && [[ -d "$ATTEMPT_DIR/runs/scenario-ugv_nav_to_goal-attempt1" ]] \
  && [[ "$(cat "$ATTEMPT_DIR/runs/junit.xml")" == '<testsuites name="PASS"/>' ]]; then
  pass "scenario-run: a failure is retried once; a passing retry is green and flagged flaky, both attempts kept"
else
  fail "scenario-run: flaky run policy"
  printf '%s\n' "$LOG" "$OUT" "$SUMMARY" | sed 's/^/        /' | tail -n 30
fi

attempt "1 1" fail
if [[ "$RC" -eq 1 ]] && grep -q '^attempts=2$' <<<"$OUT" && grep -q '^flaky=false$' <<<"$OUT" \
  && grep -q '^verdict=FAIL$' <<<"$OUT" && grep -q '::error title=Scenario ugv_nav_to_goal FAIL::' <<<"$LOG" \
  && [[ "$(cat "$ATTEMPT_DIR/runs/junit.xml")" == '<testsuites name="FAIL"/>' ]]; then
  pass "scenario-run: two failures fail with the last attempt's exit code and report"
else
  fail "scenario-run: two failures fail (got $RC)"
  printf '%s\n' "$LOG" "$OUT" | sed 's/^/        /' | tail -n 20
fi

attempt "130 0" interrupted
if [[ "$RC" -eq 130 ]] && grep -q '^attempts=1$' <<<"$OUT" && grep -q '^verdict=INTERRUPTED$' <<<"$OUT"; then
  pass "scenario-run: an interrupted run (130) is not retried"
else
  fail "scenario-run: an interrupted run is not retried (got $RC)"
fi

attempt "2 0" usage
if [[ "$RC" -eq 2 ]] && grep -q '^attempts=1$' <<<"$OUT"; then
  pass "scenario-run: a usage error (2) is not retried"
else
  fail "scenario-run: a usage error is not retried (got $RC)"
fi

attempt "1 0" noretry --retries 0
if [[ "$RC" -eq 1 ]] && grep -q '^attempts=1$' <<<"$OUT"; then
  pass "scenario-run: --retries 0 disables the retry"
else
  fail "scenario-run: --retries 0 disables the retry (got $RC)"
fi

attempt "1 1 0" tworetries --retries 2 --run-id custom
if [[ "$RC" -eq 0 ]] && grep -q '^attempts=3$' <<<"$OUT" && grep -q '^run_id=custom-attempt3$' <<<"$OUT"; then
  pass "scenario-run: --retries 2 allows a third attempt; --run-id names the attempts"
else
  fail "scenario-run: --retries 2 and --run-id (got $RC)"
fi

status=0
"$scenario_run" >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "scenario-run: no scenario exits 2"; else fail "scenario-run: no scenario exits 2 (got $status)"; fi
status=0
"$scenario_run" --bogus "$scenario_file" >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "scenario-run: unknown option exits 2"; else fail "scenario-run: unknown option exits 2 (got $status)"; fi
if "$scenario_run" --help | grep -q '^Usage: tools/ci/scenario-run.sh'; then
  pass "scenario-run --help"
else
  fail "scenario-run --help"
fi

demo="$REPO/examples/demo_m1.sh"
if "$demo" --help | grep -q '^Usage: examples/demo_m1.sh'; then pass "demo_m1 --help"; else fail "demo_m1 --help"; fi
if dry="$(cd "$REPO" && "$demo" --dry-run 2>&1)" \
  && grep -q '^+ docker pull --quiet ghcr.io/dimitrios-kafetzis/fieldproof-sim:main$' <<<"$dry" \
  && grep -q '^+ docker pull --quiet ghcr.io/dimitrios-kafetzis/fieldproof-bridge:main$' <<<"$dry" \
  && grep -q '^+ docker pull --quiet ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:main$' <<<"$dry" \
  && grep -q '^+ make assets$' <<<"$dry" && grep -q '^+ make vehicle-image$' <<<"$dry" \
  && grep -q '^+ uv run --locked --project engine fieldproof run examples/scenarios/ugv_nav_to_goal.yaml --run-id m1-demo-' <<<"$dry" \
  && grep -q 'Dry run: nothing was executed' <<<"$dry"; then
  pass "demo_m1 --dry-run: pulls the published images, fetches the assets, builds the vehicle image, runs the scenario"
else
  fail "demo_m1 --dry-run"
  printf '%s\n' "$dry" | sed 's/^/        /' | tail -n 20
fi
if dry="$(cd "$REPO" && FIELDPROOF_IMAGE_TAG=v0.9.0 "$demo" --dry-run --scenario examples/scenarios/ugv_nav_fails_on_time.yaml 2>&1)" \
  && grep -q '^+ docker pull --quiet ghcr.io/dimitrios-kafetzis/fieldproof-sim:v0.9.0$' <<<"$dry" \
  && grep -q 'fieldproof run examples/scenarios/ugv_nav_fails_on_time.yaml' <<<"$dry"; then
  pass "demo_m1 --dry-run: FIELDPROOF_IMAGE_TAG and --scenario are honoured"
else
  fail "demo_m1 --dry-run: FIELDPROOF_IMAGE_TAG and --scenario"
fi
if dry="$(cd "$REPO" && "$demo" --dry-run --local 2>&1)" && ! grep -q 'docker pull' <<<"$dry" \
  && grep -q 'Using the stack images already present (tag local)' <<<"$dry"; then
  pass "demo_m1 --dry-run --local: no pull, the local tag"
else
  fail "demo_m1 --dry-run --local"
fi
status=0
"$demo" --scenario >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "demo_m1: --scenario without a file exits 2"; else fail "demo_m1: --scenario without a file exits 2 (got $status)"; fi

# ---------------------------------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
