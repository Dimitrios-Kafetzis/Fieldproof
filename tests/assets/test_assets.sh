#!/usr/bin/env bash
# Tests for the asset pipeline (task P1-T02): the vendoring convention under assets/, the Fuel
# lock file and tools/assets/fetch_fuel.py.
#
# 1. Static checks: bash -n and shellcheck on this script, --help and argument handling of the
#    fetch script, every pinned licence is permissive (no NC or ND terms).
# 2. Convention: every file under assets/ is the README, the lock, a licence file, or has a
#    licence file in assets/LICENSES/; the fetched cache is ignored by git; the pre-commit and
#    lint excludes cover assets/.
# 3. Fetch script against a local Fuel stand-in (file:// server in a scratch directory): fetch
#    downloads, verifies and extracts; a second fetch is a no-op; check passes offline; a
#    tampered archive is refused and reported; a missing model directory is re-extracted from the
#    cached archive; an archive entry escaping the model directory is refused.
# 4. The real lock: every entry has the fields the script needs, the directories match the
#    worlds' model:// URIs, and `fetch_fuel.py check` passes when the models were fetched
#    (skipped otherwise, with a hint to run make assets).
#
# Needs python3 with PyYAML on the host, as installed by tools/host-setup.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
FETCH="$REPO/tools/assets/fetch_fuel.py"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

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
if bash -n tests/assets/test_assets.sh; then pass "bash -n tests/assets/test_assets.sh"; else fail "bash -n"; fi
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=style --shell=bash tests/assets/test_assets.sh; then pass "shellcheck (local)"; else fail "shellcheck (local)"; fi
elif docker run --rm -v "${REPO}:/mnt:ro" "$SHELLCHECK_IMAGE" --severity=style --shell=bash tests/assets/test_assets.sh; then
  pass "shellcheck (container ${SHELLCHECK_IMAGE%%@*})"
else
  fail "shellcheck (container)"
fi
if python3 "$FETCH" --help | grep -q '^usage:'; then pass "fetch_fuel.py --help"; else fail "fetch_fuel.py --help"; fi
status=0
python3 "$FETCH" bogus >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "fetch_fuel.py: unknown command exits 2"; else fail "fetch_fuel.py: unknown command exits 2 (got $status)"; fi
if python3 - <<'PY'
import yaml
lock = yaml.safe_load(open("assets/fuel.lock"))
for entry in lock["models"]:
    licence = entry["licence"]
    assert licence in {"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "Apache-2.0", "MIT", "BSD-3-Clause"}, (entry["name"], licence)
    assert "NC" not in licence and "ND" not in licence
PY
then pass "every pinned Fuel model has a permissive licence"; else fail "a pinned Fuel model has a restrictive or unknown licence"; fi

# ---------------------------------------------------------------------------------------------
section "2. Convention"
unlicensed=()
while IFS= read -r file; do
  case "$file" in
    assets/README.md | assets/fuel.lock | assets/LICENSES/*) ;;
    *)
      stem="$(basename "$file")"
      # <asset>.LICENSE.txt for the file itself or for the set it belongs to (its directory).
      if [[ ! -f "assets/LICENSES/${stem}.LICENSE.txt" && ! -f "assets/LICENSES/$(basename "$(dirname "$file")").LICENSE.txt" ]]; then
        unlicensed+=("$file")
      fi
      ;;
  esac
done < <(git ls-files assets)
if [[ ${#unlicensed[@]} -eq 0 ]]; then
  pass "every tracked file under assets/ is the README, the lock, or covered by a licence file"
else
  fail "files under assets/ without a licence file: ${unlicensed[*]}"
fi
if git check-ignore -q assets/fuel/models/x/model.config && git check-ignore -q assets/fuel/archives/x.zip; then
  pass "git ignores the fetched Fuel models and archives (assets/fuel/)"
else
  fail "assets/fuel/ is not ignored"
fi
if grep -q 'assets/\.\*' .pre-commit-config.yaml && grep -q '^  assets/$' .yamllint.yaml && grep -q '"assets/\*\*"' .markdownlint-cli2.yaml; then
  pass "assets/ is excluded from the pre-commit hooks, yamllint and markdownlint"
else
  fail "assets/ is not excluded from every linter"
fi
if [[ -f assets/LICENSES/README.md ]] && grep -q "LICENSE.txt" assets/LICENSES/README.md && grep -q "fuel.lock" assets/README.md; then
  pass "assets/README.md and assets/LICENSES/README.md describe the convention"
else
  fail "the assets READMEs do not describe the convention"
fi

# ---------------------------------------------------------------------------------------------
section "3. fetch_fuel.py against a local Fuel stand-in"
# A file:// server laid out like Fuel: <server>/1.0/<owner>/models/<name>/<version>/<name>.zip
# (the script quotes the name in the URL; urllib unquotes it again for a file:// path).
SERVER="$SCRATCH/server"
DEST="$SCRATCH/cache"
LOCK="$SCRATCH/fuel.lock"
mkdir -p "$SERVER/1.0/Tester/models/Test box/2" "$SCRATCH/src/box"
cat >"$SCRATCH/src/box/model.config" <<'XML'
<?xml version="1.0"?><model><name>Test box</name><sdf version="1.11">model.sdf</sdf></model>
XML
echo '<sdf version="1.11"><model name="test_box"><link name="link"/></model></sdf>' >"$SCRATCH/src/box/model.sdf"
(cd "$SCRATCH/src/box" && python3 -c 'import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],"w"); z.write("model.config"); z.write("model.sdf"); z.close()' "$SERVER/1.0/Tester/models/Test box/2/Test box.zip")
sha="$(sha256sum "$SERVER/1.0/Tester/models/Test box/2/Test box.zip" | cut -d' ' -f1)"
size="$(stat -c %s "$SERVER/1.0/Tester/models/Test box/2/Test box.zip")"
cat >"$LOCK" <<YAML
# scratch lock
apiVersion: fieldproof.dev/v0alpha1
kind: FuelLock
server: file://$SERVER
models:
  - name: Test box
    owner: Tester
    version: 2
    dir: test_box
    licence: CC0-1.0
    licence_url: https://creativecommons.org/publicdomain/zero/1.0/
    size_bytes: $size
    sha256: $sha
    used_by: [scratch]
YAML
if out="$(python3 "$FETCH" --lock "$LOCK" --dest "$DEST" 2>&1)" && [[ -f "$DEST/models/test_box/model.config" && -f "$DEST/archives/test_box-v2.zip" ]] \
  && grep -q '"sha256": "'"$sha"'"' "$DEST/models/test_box/.fieldproof-fuel.json" && grep -q "downloaded and extracted" <<<"$out"; then
  pass "fetch downloads, verifies and extracts into models/<dir> with a stamp"
else
  fail "fetch: $out"
fi
if out="$(python3 "$FETCH" --lock "$LOCK" --dest "$DEST" 2>&1)" && grep -q "test_box: up to date" <<<"$out"; then
  pass "a second fetch is a no-op"
else
  fail "second fetch: $out"
fi
if python3 "$FETCH" check --lock "$LOCK" --dest "$DEST" >/dev/null 2>&1; then pass "check passes offline"; else fail "check fails after a fetch"; fi
rm -rf "$DEST/models/test_box"
if out="$(python3 "$FETCH" --lock "$LOCK" --dest "$DEST" 2>&1)" && grep -q "test_box: extracted" <<<"$out" && [[ -f "$DEST/models/test_box/model.sdf" ]]; then
  pass "a missing model directory is re-extracted from the cached archive without a download"
else
  fail "re-extraction: $out"
fi
sed -i "s/sha256: $sha/sha256: $(printf 'deadbeef%.0s' $(seq 1 8))/" "$LOCK"
status=0
out="$(python3 "$FETCH" check --lock "$LOCK" --dest "$DEST" 2>&1)" || status=$?
if [[ "$status" -eq 1 ]] && grep -q "digest differs" <<<"$out"; then pass "check reports an archive whose digest differs from the lock"; else fail "check with a wrong digest: $status $out"; fi
status=0
out="$(python3 "$FETCH" --lock "$LOCK" --dest "$DEST" 2>&1)" || status=$?
if [[ "$status" -eq 1 ]] && grep -q "refusing it" <<<"$out" && [[ ! -f "$DEST/archives/test_box-v2.zip" ]]; then
  pass "fetch refuses and discards an archive whose digest differs from the lock"
else
  fail "fetch with a wrong digest: $status $out"
fi
sed -i "s/sha256: \(deadbeef\)\{8\}/sha256: $sha/" "$LOCK"
python3 "$FETCH" --lock "$LOCK" --dest "$DEST" >/dev/null 2>&1 || true
# an archive with an entry escaping the model directory
mkdir -p "$SERVER/1.0/Tester/models/Evil/1"
python3 - "$SERVER/1.0/Tester/models/Evil/1/Evil.zip" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("model.config", "<model/>")
    z.writestr("../escape.txt", "x")
PY
esha="$(sha256sum "$SERVER/1.0/Tester/models/Evil/1/Evil.zip" | cut -d' ' -f1)"
cat >>"$LOCK" <<YAML
  - name: Evil
    owner: Tester
    version: 1
    dir: evil
    licence: CC0-1.0
    licence_url: https://creativecommons.org/publicdomain/zero/1.0/
    size_bytes: 1
    sha256: $esha
    used_by: [scratch]
YAML
status=0
out="$(python3 "$FETCH" --lock "$LOCK" --dest "$DEST" 2>&1)" || status=$?
if [[ "$status" -eq 1 ]] && grep -q "escapes the model directory" <<<"$out" && [[ ! -f "$SCRATCH/cache/escape.txt" && ! -f "$DEST/escape.txt" ]]; then
  pass "an archive entry escaping the model directory is refused"
else
  fail "escaping archive entry: $status $out"
fi
status=0
out="$(python3 "$FETCH" check --lock "$REPO/assets/fuel.lock" --dest "$SCRATCH/empty" 2>&1)" || status=$?
if [[ "$status" -eq 1 ]] && grep -q "missing" <<<"$out"; then pass "check on an empty cache fails and names the missing models"; else fail "check on an empty cache: $status $out"; fi

# ---------------------------------------------------------------------------------------------
section "4. The real lock"
if python3 - <<'PY'
import re, xml.etree.ElementTree as ET
from pathlib import Path
import yaml
lock = yaml.safe_load(open("assets/fuel.lock"))
assert lock["apiVersion"] == "fieldproof.dev/v0alpha1" and lock["kind"] == "FuelLock"
assert lock["server"] == "https://fuel.gazebosim.org"
dirs = [m["dir"] for m in lock["models"]]
assert dirs == sorted(dirs) and len(set(dirs)) == len(dirs), "sorted, unique directories"
for m in lock["models"]:
    for key in ("name", "owner", "version", "dir", "licence", "licence_url", "size_bytes", "sha256", "used_by"):
        assert key in m, (m.get("name"), key)
    assert re.fullmatch(r"[0-9a-f]{64}", m["sha256"]), m["name"]
    assert re.fullmatch(r"[a-z0-9_]+", m["dir"]), m["dir"]
    assert m["used_by"], m["name"]
local = {p.name for p in Path("sim/fieldproof_worlds/models").iterdir() if (p / "model.config").is_file()}
used = {d: set() for d in dirs}
for world in Path("sim/fieldproof_worlds/worlds").glob("*.sdf"):
    w = ET.parse(world).getroot().find("world")
    for include in w.findall("include"):
        uri = include.findtext("uri").strip()
        d = uri.removeprefix("model://").split("/")[0]
        assert d in used or d in local, f"{world.name}: {uri} is neither pinned nor a package model"
        if d in used:
            used[d].add(world.stem)
for m in lock["models"]:
    assert used[m["dir"]] == set(m["used_by"]), (m["dir"], m["used_by"], used[m["dir"]])
print(f"{len(lock['models'])} pinned models, {len(local)} package models")
PY
then pass "assets/fuel.lock entries are complete and match the worlds' model:// includes"; else fail "assets/fuel.lock does not match the worlds"; fi
if [[ -d assets/fuel/models ]]; then
  if python3 "$FETCH" check >/dev/null 2>&1; then pass "fetch_fuel.py check: the fetched models match the lock"; else fail "fetch_fuel.py check fails (run make assets)"; fi
else
  skip "fetch_fuel.py check (no fetched models yet; run make assets)"
fi

# ---------------------------------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
