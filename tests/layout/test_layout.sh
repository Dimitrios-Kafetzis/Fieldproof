#!/usr/bin/env bash
# Tests for the monorepo layout (task P0-T07): the directories and packages of
# docs/plan/04-engineering-standards.md exist, every layer has a unit test, one version number is
# used everywhere and release-please maintains it, the lock files match their manifests, and the
# tool pins that must agree across files do.
#
# 1. Static checks: bash -n and shellcheck on tools/ros/colcon.sh and this script.
# 2. Layout: the layer directories and packages, their READMEs, ARCHITECTURE.md, examples/,
#    assets/LICENSES/; every package has a test next to its code.
# 3. Versions: version.txt equals the version of the engine, the cockpit packages, every
#    package.xml and the bridge module; release-please's extra-files list covers each of them.
# 4. Lock files: engine/uv.lock matches engine/pyproject.toml (uv lock --check);
#    cockpit/pnpm-lock.yaml carries the exact specifiers of cockpit/apps/cockpit/package.json.
# 5. Pins: the mypy hook's dependencies match engine/uv.lock; the eslint hook's TypeScript
#    matches the cockpit's; pnpm in packageManager matches the host setup; the engine's Python
#    matches the root tooling.
# 6. Makefile and git: `make test` runs the engine, cockpit and ROS suites; build output
#    directories are ignored; the CLI prints the repository version.
#
# Needs uv, Node and python3 (with PyYAML) on the host, as installed by tools/host-setup.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
SCRIPTS=(tools/ros/colcon.sh tests/layout/test_layout.sh)

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
if tools/ros/colcon.sh --help | grep -q '^Usage:'; then pass "tools/ros/colcon.sh --help"; else fail "tools/ros/colcon.sh --help"; fi
status=0
tools/ros/colcon.sh bogus >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "tools/ros/colcon.sh: unknown verb exits 2"; else fail "tools/ros/colcon.sh: unknown verb exits 2 (got $status)"; fi

# ---------------------------------------------------------------------------------------------
section "2. Layout"
for dir in sim/fieldproof_worlds sim/fieldproof_gz_plugins bridge/fieldproof_bridge robots/fieldproof_robots \
  engine/src/fieldproof engine/tests cockpit/apps/cockpit cockpit/packages docker examples/scenarios \
  examples/vehicles examples/plugins assets/LICENSES tools tests docs .github/workflows; do
  if [[ -d "$dir" ]]; then pass "directory $dir"; else fail "directory $dir is missing"; fi
done
for readme in sim bridge robots engine cockpit examples assets tests; do
  if [[ -f "$readme/README.md" ]]; then pass "$readme/README.md"; else fail "$readme/README.md is missing"; fi
done
if [[ -f ARCHITECTURE.md ]] && grep -q "docs/plan/01-architecture.md" ARCHITECTURE.md \
  && grep -q "docs/setup/workspaces.md" ARCHITECTURE.md; then
  pass "ARCHITECTURE.md points to the architecture and workspace documentation"
else
  fail "ARCHITECTURE.md is missing or does not point to docs/"
fi
for pkg in sim/fieldproof_worlds sim/fieldproof_gz_plugins robots/fieldproof_robots; do
  if [[ -f "$pkg/package.xml" && -f "$pkg/CMakeLists.txt" ]] && grep -q "BUILD_TESTING" "$pkg/CMakeLists.txt"; then
    pass "$pkg is an ament_cmake package with tests"
  else
    fail "$pkg: package.xml, CMakeLists.txt or a BUILD_TESTING block is missing"
  fi
done
if [[ -f bridge/fieldproof_bridge/setup.py && -f bridge/fieldproof_bridge/package.xml ]] \
  && compgen -G "bridge/fieldproof_bridge/test/test_*.py" >/dev/null; then
  pass "bridge/fieldproof_bridge is an ament_python package with tests"
else
  fail "bridge/fieldproof_bridge: setup.py, package.xml or test/test_*.py is missing"
fi
if compgen -G "sim/fieldproof_gz_plugins/test/*.cpp" >/dev/null && grep -q "ament_add_gtest" sim/fieldproof_gz_plugins/CMakeLists.txt; then
  pass "sim/fieldproof_gz_plugins has a GoogleTest suite"
else
  fail "sim/fieldproof_gz_plugins has no GoogleTest suite"
fi
if compgen -G "sim/fieldproof_worlds/worlds/*.sdf" >/dev/null && grep -q "sdf --check" sim/fieldproof_worlds/CMakeLists.txt; then
  pass "sim/fieldproof_worlds ships a world and checks it with gz sdf --check"
else
  fail "sim/fieldproof_worlds: no world or no gz sdf --check test"
fi
if compgen -G "engine/tests/test_*.py" >/dev/null; then pass "engine/tests has pytest tests"; else fail "engine/tests has no tests"; fi
if compgen -G "cockpit/apps/cockpit/src/*.test.ts*" >/dev/null; then pass "cockpit/apps/cockpit has Vitest tests"; else fail "cockpit/apps/cockpit has no tests"; fi
if [[ -f cockpit/pnpm-workspace.yaml && -f cockpit/pnpm-lock.yaml && -f engine/uv.lock ]]; then
  pass "pnpm workspace and lock files exist"
else
  fail "cockpit/pnpm-workspace.yaml, cockpit/pnpm-lock.yaml or engine/uv.lock is missing"
fi

# ---------------------------------------------------------------------------------------------
section "3. Versions"
version="$(cat version.txt)"
# One line per version file: `<path> <version>`; the Python keeps the list in one place.
VERSIONS_FILE="$(mktemp)"
trap 'rm -f "$VERSIONS_FILE"' EXIT
python3 - >"$VERSIONS_FILE" <<'PY'
import json, re, tomllib, xml.etree.ElementTree as ET
rows = []
rows.append(("engine/pyproject.toml", tomllib.load(open("engine/pyproject.toml", "rb"))["project"]["version"]))
for p in ("cockpit/package.json", "cockpit/apps/cockpit/package.json"):
    rows.append((p, json.load(open(p))["version"]))
for p in ("sim/fieldproof_worlds/package.xml", "sim/fieldproof_gz_plugins/package.xml",
          "bridge/fieldproof_bridge/package.xml", "robots/fieldproof_robots/package.xml"):
    rows.append((p, ET.parse(p).getroot().findtext("version")))
m = re.search(r'version="([^"]+)",\s*# x-release-please-version', open("bridge/fieldproof_bridge/setup.py").read())
rows.append(("bridge/fieldproof_bridge/setup.py", m.group(1) if m else "missing"))
m = re.search(r'__version__ = "([^"]+)"\s*# x-release-please-version', open("bridge/fieldproof_bridge/fieldproof_bridge/__init__.py").read())
rows.append(("bridge/fieldproof_bridge/fieldproof_bridge/__init__.py", m.group(1) if m else "missing"))
for path, v in rows:
    print(path, v)
PY
while read -r path v; do
  if [[ "$v" == "$version" ]]; then pass "$path is at $version"; else fail "$path is at '$v', version.txt says $version"; fi
done <"$VERSIONS_FILE"
if python3 - "$VERSIONS_FILE" <<'PY'
import json, sys
cfg = json.load(open("release-please-config.json"))["packages"]["."]
extra = {e if isinstance(e, str) else e["path"] for e in cfg.get("extra-files", [])}
files = {line.split()[0] for line in open(sys.argv[1]) if line.strip()}
missing = files - extra
assert not missing, f"not maintained by release-please: {sorted(missing)}"
PY
then pass "release-please extra-files cover every version file"; else fail "release-please extra-files miss a version file"; fi

# ---------------------------------------------------------------------------------------------
section "4. Lock files"
if uv lock --check --project engine >/dev/null 2>&1; then
  pass "engine/uv.lock matches engine/pyproject.toml"
else
  fail "engine/uv.lock is out of date (uv lock --project engine)"
fi
if python3 - <<'PY'
import json, yaml
# pnpm 12 writes several YAML documents (one holds pnpm's own binaries); take the workspace's.
lock = next(d for d in yaml.safe_load_all(open("cockpit/pnpm-lock.yaml")) if "apps/cockpit" in d.get("importers", {}))
for importer, manifest in (("apps/cockpit", "cockpit/apps/cockpit/package.json"), (".", "cockpit/package.json")):
    pkg = json.load(open(manifest))
    locked = lock["importers"][importer]
    for section in ("dependencies", "devDependencies"):
        for name, spec in pkg.get(section, {}).items():
            assert locked[section][name]["specifier"] == spec, (name, spec, locked[section][name])
            assert locked[section][name]["version"].split("(")[0] == spec, (name, spec)
PY
then pass "cockpit/pnpm-lock.yaml carries the exact versions of the package manifests"; else fail "cockpit/pnpm-lock.yaml is out of date (pnpm install)"; fi
if ! grep -qE '^\s+"?[~^]' cockpit/apps/cockpit/package.json; then
  pass "cockpit dependencies are exact versions (no ^ or ~ ranges)"
else
  fail "cockpit dependencies use version ranges"
fi
if grep -qE '^dependencies = \[' engine/pyproject.toml && ! grep -qE '^\s+"[a-z0-9_-]+(>=|~=|<|>)' engine/pyproject.toml; then
  pass "engine dependencies are exact versions (==)"
else
  fail "engine dependencies use version ranges"
fi

# ---------------------------------------------------------------------------------------------
section "5. Pins that must agree"
if python3 - <<'PY'
import re, tomllib, yaml
lock = {p["name"]: p["version"] for p in tomllib.load(open("engine/uv.lock", "rb"))["package"]}
cfg = yaml.safe_load(open(".pre-commit-config.yaml"))
mypy = next(h for r in cfg["repos"] for h in r["hooks"] if h["id"] == "mypy")
deps = dict(d.split("==") for d in mypy["additional_dependencies"])
assert deps, "the mypy hook lists no dependencies"
for name, version in deps.items():
    assert lock.get(name) == version, f"{name}: hook {version}, uv.lock {lock.get(name)}"
project = tomllib.load(open("engine/pyproject.toml", "rb"))["project"]
for dep in project["dependencies"]:
    name = dep.split("==")[0]
    assert name in deps, f"{name} is an engine dependency but not in the mypy hook"
PY
then pass "mypy hook dependencies equal engine/uv.lock and cover every engine dependency"; else fail "mypy hook dependencies drift from engine/uv.lock"; fi
hook_ts="$(grep -oE 'typescript@[0-9.]+' .pre-commit-config.yaml | head -n 1 | cut -d@ -f2)"
app_ts="$(python3 -c 'import json; print(json.load(open("cockpit/apps/cockpit/package.json"))["devDependencies"]["typescript"])')"
if [[ -n "$hook_ts" && "$hook_ts" == "$app_ts" ]]; then
  pass "eslint hook TypeScript ($hook_ts) equals the cockpit's"
else
  fail "eslint hook TypeScript ($hook_ts) differs from the cockpit's ($app_ts)"
fi
pm="$(python3 -c 'import json; print(json.load(open("cockpit/package.json"))["packageManager"])')"
host_pnpm="pnpm@$(grep -oE '^PNPM_VERSION="?[0-9.]+' tools/host-setup.sh | grep -oE '[0-9.]+' | head -n 1)"
if [[ "$host_pnpm" != "pnpm@" && "$pm" == "$host_pnpm" ]]; then
  pass "packageManager $pm equals the pnpm of tools/host-setup.sh"
else
  fail "packageManager '$pm' differs from tools/host-setup.sh ('$host_pnpm')"
fi
node_major="$(tr -d 'v\n' <.nvmrc | cut -d. -f1)"
engines="$(python3 -c 'import json; print(json.load(open("cockpit/package.json"))["engines"]["node"])')"
if [[ "$engines" == ">=${node_major}."* ]]; then
  pass "cockpit engines.node ($engines) matches .nvmrc ($node_major)"
else
  fail "cockpit engines.node ($engines) does not match .nvmrc ($node_major)"
fi
root_py="$(grep -oE '^python_version = "[0-9.]+"' pyproject.toml | grep -oE '[0-9.]+')"
engine_py="$(grep -oE '^requires-python = ">=[0-9.]+"' engine/pyproject.toml | grep -oE '[0-9.]+')"
if [[ -n "$root_py" && "$root_py" == "$engine_py" ]]; then
  pass "engine requires-python ($engine_py) equals the root mypy python_version"
else
  fail "engine requires-python ($engine_py) differs from the root mypy python_version ($root_py)"
fi
if grep -q 'mypy_path = \["engine/src"' pyproject.toml; then
  pass "root mypy resolves the engine package (mypy_path)"
else
  fail "root mypy_path does not include engine/src"
fi
if grep -q "pnpm-lock.yaml" .yamllint.yaml && grep -q "pnpm-lock" .gitleaks.toml; then
  pass "pnpm-lock.yaml is excluded from yamllint and gitleaks"
else
  fail "pnpm-lock.yaml is not excluded from yamllint or gitleaks"
fi

# ---------------------------------------------------------------------------------------------
section "6. Makefile, git and the CLI"
deps="$(make -n -p 2>/dev/null | grep -E '^test:' | head -n 1)"
for target in test-engine test-cockpit test-ros test-layout; do
  if grep -qw "$target" <<<"$deps"; then pass "make test runs $target"; else fail "make test does not run $target"; fi
done
if make -s help | grep -qE '^\s+\S*tidy'; then pass "make tidy is documented in make help"; else fail "make tidy is missing from make help"; fi
for pattern in build/ install/ log/ node_modules/ .venv/ dist/; do
  if grep -qxF "$pattern" .gitignore; then pass ".gitignore covers $pattern"; else fail ".gitignore does not cover $pattern"; fi
done
for path in sim/build sim/install sim/log engine/.venv cockpit/node_modules cockpit/apps/cockpit/dist; do
  if git check-ignore -q "$path"; then pass "git ignores $path"; else fail "git does not ignore $path"; fi
done
if cli="$(uv run --locked --project engine fieldproof --version 2>/dev/null)" && [[ "$cli" == "fieldproof $version" ]]; then
  pass "fieldproof --version prints 'fieldproof $version'"
else
  fail "fieldproof --version printed '${cli:-nothing}' (expected 'fieldproof $version')"
fi

# ---------------------------------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
