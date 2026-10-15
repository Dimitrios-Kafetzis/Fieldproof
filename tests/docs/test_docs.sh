#!/usr/bin/env bash
# Tests for the documentation site (task P0-T06): mkdocs.yml, tools/docs, docs/ and the Docs
# workflow.
#
# 1. Static checks: bash -n and shellcheck on this script; the ruff, mypy and yamllint hooks on
#    tools/docs and mkdocs.yml.
# 2. ADR index generator: the committed docs/adr/README.md is current; in a scratch directory of
#    ADRs the generator writes the table, skips the template and unrelated files, links a
#    "Superseded by ADR-NNNN" status, rewrites idempotently and rejects malformed ADRs.
# 3. Site build: `make docs` (strict) succeeds and produces a page for every Markdown file under
#    docs/, the ADR table and navigation entries, a rendered Mermaid block, the search index and a
#    sitemap under the site URL.
# 4. Strictness: in a scratch copy of the site sources a broken link, a missing anchor and a page
#    outside the navigation each fail `mkdocs build --strict`.
# 5. Configuration: tools/docs/uv.lock matches pyproject.toml; the Makefile builds with --strict;
#    docs.yml builds on pull requests and pushes to main, uploads site/ and deploys only outside
#    pull requests with the Pages permissions; ci.yml has a Docs job running make test-docs.
# 6. Links: when Docker is available, lychee (offline, image pulled with retries) finds no broken
#    internal link or fragment in the built site.
#
# Needs uv, pre-commit and python3 on the host (tools/host-setup.sh); Docker for section 6.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

SHELLCHECK_IMAGE="koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"
MKDOCS=(uv run --locked --project "$REPO/tools/docs" mkdocs)
ADR_INDEX="$REPO/tools/docs/adr_index.py"

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
show() { sed 's/^/        /' "$1" | tail -n "${2:-15}"; }

cd "$REPO"

# ---------------------------------------------------------------------------------------------
section "1. Static checks"
if bash -n tests/docs/test_docs.sh; then pass "bash -n tests/docs/test_docs.sh"; else fail "bash -n tests/docs/test_docs.sh"; fi
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck --severity=style --shell=bash tests/docs/test_docs.sh; then pass "shellcheck (local)"; else fail "shellcheck (local)"; fi
elif docker run --rm -v "${REPO}:/mnt:ro" "$SHELLCHECK_IMAGE" --severity=style --shell=bash tests/docs/test_docs.sh; then
  pass "shellcheck (container ${SHELLCHECK_IMAGE%%@*})"
else
  fail "shellcheck (container)"
fi
for hook in ruff-check ruff-format mypy yamllint check-yaml; do
  if pre-commit run "$hook" --files tools/docs/adr_index.py tools/docs/mkdocs_hooks.py mkdocs.yml \
    .lychee.toml tools/docs/pyproject.toml >"$SCRATCH/$hook.log" 2>&1; then
    pass "pre-commit hook $hook on tools/docs and mkdocs.yml"
  else
    fail "pre-commit hook $hook on tools/docs and mkdocs.yml"
    show "$SCRATCH/$hook.log"
  fi
done

# ---------------------------------------------------------------------------------------------
section "2. ADR index generator"
if "$ADR_INDEX" --check >"$SCRATCH/adr-check.log" 2>&1; then
  pass "docs/adr/README.md is current (adr_index.py --check)"
else
  fail "docs/adr/README.md is stale; run tools/docs/adr_index.py --write"
  show "$SCRATCH/adr-check.log" 30
fi

ADRS="$SCRATCH/adr"
mkdir -p "$ADRS"
cp docs/adr/0000-adr-template.md "$ADRS/"
write_adr() { # number slug title status
  cat >"$ADRS/$1-$2.md" <<ADR
# ADR-$1 — $3

- **Status:** $4
- **Date:** 2026-10-15
- **Deciders:** maintainer
- **Tasks affected:** P0-T06

## Context

Test fixture.
ADR
}
write_adr 0001 first "First decision" "Accepted"
write_adr 0002 second "Second decision, with a comma" "Superseded by ADR-0003"
write_adr 0003 third "Third decision" "Accepted"
echo "not an ADR" >"$ADRS/notes.md"

printed="$("$ADR_INDEX" --print --adr-dir "$ADRS")"
if grep -qF "| [0001](0001-first.md) | First decision | Accepted | 2026-10-15 |" <<<"$printed" &&
  grep -qF "| [0003](0003-third.md) | Third decision | Accepted | 2026-10-15 |" <<<"$printed"; then
  pass "--print renders one table row per ADR with number, title, status and date"
else
  fail "--print table rows"
  printf '%s\n' "$printed" | sed 's/^/        /'
fi
if grep -qF "| Second decision, with a comma | Superseded by [ADR-0003](0003-third.md) |" <<<"$printed"; then
  pass "a 'Superseded by ADR-NNNN' status links to that ADR"
else
  fail "a 'Superseded by ADR-NNNN' status links to that ADR"
fi
if ! grep -q "| \[0000\]" <<<"$printed" && ! grep -q "notes.md" <<<"$printed" &&
  grep -qF "[0000-adr-template.md](0000-adr-template.md)" <<<"$printed"; then
  pass "the template and unrelated files are not rows; the template is linked in the footer"
else
  fail "the template and unrelated files are not rows"
fi
status=0
"$ADR_INDEX" --check --adr-dir "$ADRS" >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 1 ]]; then pass "--check exits 1 when README.md is missing"; else fail "--check exits 1 when README.md is missing (got $status)"; fi
if "$ADR_INDEX" --write --adr-dir "$ADRS" | grep -q "updated" && [[ -f "$ADRS/README.md" ]]; then
  pass "--write creates README.md"
else
  fail "--write creates README.md"
fi
if "$ADR_INDEX" --write --adr-dir "$ADRS" | grep -q "up to date" && "$ADR_INDEX" --check --adr-dir "$ADRS" >/dev/null; then
  pass "a second --write changes nothing and --check passes"
else
  fail "a second --write changes nothing and --check passes"
fi
if [[ "$(cat "$ADRS/README.md")" == "$printed" ]]; then
  pass "the written file equals the printed index"
else
  fail "the written file equals the printed index"
fi

printf '# ADR-0005 — Wrong number\n\n- **Status:** Accepted\n- **Date:** 2026-10-15\n' >"$ADRS/0004-bad.md"
status=0
"$ADR_INDEX" --check --adr-dir "$ADRS" >"$SCRATCH/bad1.log" 2>&1 || status=$?
if [[ "$status" -eq 1 ]] && grep -q "numbered 0004" "$SCRATCH/bad1.log"; then
  pass "a heading number that differs from the file name is rejected"
else
  fail "a heading number that differs from the file name is rejected (exit $status)"
  show "$SCRATCH/bad1.log"
fi
printf '# ADR-0004 — No status\n\n- **Date:** 2026-10-15\n\n## Context\n' >"$ADRS/0004-bad.md"
status=0
"$ADR_INDEX" --check --adr-dir "$ADRS" >"$SCRATCH/bad2.log" 2>&1 || status=$?
if [[ "$status" -eq 1 ]] && grep -q "Status" "$SCRATCH/bad2.log"; then
  pass "an ADR without a Status line is rejected"
else
  fail "an ADR without a Status line is rejected (exit $status)"
  show "$SCRATCH/bad2.log"
fi
printf '## Not a title\n' >"$ADRS/0004-bad.md"
status=0
"$ADR_INDEX" --check --adr-dir "$ADRS" >"$SCRATCH/bad3.log" 2>&1 || status=$?
if [[ "$status" -eq 1 ]] && grep -q "first line must be" "$SCRATCH/bad3.log"; then
  pass "an ADR whose first line is not '# ADR-NNNN — Title' is rejected"
else
  fail "an ADR whose first line is not '# ADR-NNNN — Title' is rejected (exit $status)"
fi
rm -f "$ADRS/0004-bad.md"
status=0
"$ADR_INDEX" --bogus >/dev/null 2>&1 || status=$?
if [[ "$status" -eq 2 ]]; then pass "unknown flag exits 2"; else fail "unknown flag exits 2 (got $status)"; fi

# ---------------------------------------------------------------------------------------------
section "3. Site build"
if make -s docs >"$SCRATCH/build.log" 2>&1; then
  pass "make docs (mkdocs build --strict) succeeds"
else
  fail "make docs (mkdocs build --strict) fails"
  show "$SCRATCH/build.log" 30
fi
missing=()
while IFS= read -r page; do
  rel="${page#docs/}"
  case "$rel" in
    */index.md | index.md | */README.md) out="site/${rel%/*}/index.html"; [[ "$rel" == index.md ]] && out="site/index.html" ;;
    *) out="site/${rel%.md}/index.html" ;;
  esac
  [[ -f "$out" ]] || missing+=("$rel -> $out")
done < <(git ls-files 'docs/*.md' 'docs/**/*.md')
if [[ ${#missing[@]} -eq 0 ]]; then
  pass "every tracked Markdown file under docs/ became a page"
else
  fail "pages missing for: ${missing[*]}"
fi
adr_rows=0
adr_nav=0
adr_total=0
for adr in docs/adr/0*.md; do
  name="$(basename "$adr" .md)"
  [[ "$name" == 0000-* ]] && continue
  adr_total=$((adr_total + 1))
  grep -q "href=\"$name/\"" site/adr/index.html && adr_rows=$((adr_rows + 1))
  grep -q "ADR-${name:0:4} " site/index.html && adr_nav=$((adr_nav + 1))
done
if [[ "$adr_rows" -eq "$adr_total" && "$adr_total" -gt 0 ]]; then
  pass "the ADR index page links all $adr_total ADRs"
else
  fail "the ADR index page links $adr_rows of $adr_total ADRs"
fi
if [[ "$adr_nav" -eq "$adr_total" ]] && grep -q "ADR template" site/index.html; then
  pass "the navigation lists every ADR and the template under Decisions"
else
  fail "the navigation lists $adr_nav of $adr_total ADRs"
fi
if grep -q 'class="mermaid"' site/index.html && grep -q 'class="mermaid"' site/setup/docs/index.html; then
  pass "Mermaid fences render as <pre class=\"mermaid\"> blocks"
else
  fail "Mermaid fences render as <pre class=\"mermaid\"> blocks"
fi
if [[ -f site/search/search_index.json ]] && grep -q "Documentation site" site/search/search_index.json; then
  pass "the search index exists and indexes the pages"
else
  fail "the search index exists and indexes the pages"
fi
site_url="$(python3 -c 'import yaml; print(yaml.load(open("mkdocs.yml"), Loader=yaml.BaseLoader)["site_url"])')"
if [[ -f site/sitemap.xml ]] && grep -q "<loc>${site_url}setup/docs/</loc>" site/sitemap.xml; then
  pass "sitemap.xml lists the pages under $site_url"
else
  fail "sitemap.xml lists the pages under $site_url"
fi
if [[ -f site/404.html ]] && grep -q 'href="/Fieldproof/' site/404.html; then
  pass "404.html uses root-relative links under the Pages path"
else
  fail "404.html uses root-relative links under the Pages path"
fi

# ---------------------------------------------------------------------------------------------
section "4. Strict build catches broken documentation"
# A scratch copy of the site sources: mkdocs.yml, docs/ and the hooks (paths in mkdocs.yml are
# relative to the configuration file, so the layout is kept).
SITE_SRC="$SCRATCH/src"
mkdir -p "$SITE_SRC/tools/docs"
cp mkdocs.yml "$SITE_SRC/"
cp -r docs "$SITE_SRC/docs"
cp tools/docs/adr_index.py tools/docs/mkdocs_hooks.py "$SITE_SRC/tools/docs/"
scratch_build() { # label
  (cd "$SITE_SRC" && "${MKDOCS[@]}" build --strict -f mkdocs.yml -d "$SCRATCH/site-$1") >"$SCRATCH/$1.log" 2>&1
}
if scratch_build clean; then pass "the scratch copy builds (control)"; else fail "the scratch copy builds (control)"; show "$SCRATCH/clean.log"; fi
cp "$SITE_SRC/docs/index.md" "$SCRATCH/index.md.orig"
echo '[missing page](no-such-page.md)' >>"$SITE_SRC/docs/index.md"
if ! scratch_build link && grep -q "no-such-page.md" "$SCRATCH/link.log"; then
  pass "a link to a missing page fails the strict build"
else
  fail "a link to a missing page fails the strict build"
  show "$SCRATCH/link.log"
fi
cp "$SCRATCH/index.md.orig" "$SITE_SRC/docs/index.md"
echo '[missing anchor](plan/00-overview.md#no-such-anchor)' >>"$SITE_SRC/docs/index.md"
if ! scratch_build anchor && grep -q "no-such-anchor" "$SCRATCH/anchor.log"; then
  pass "a link to a missing anchor fails the strict build"
else
  fail "a link to a missing anchor fails the strict build"
  show "$SCRATCH/anchor.log"
fi
cp "$SCRATCH/index.md.orig" "$SITE_SRC/docs/index.md"
echo '# Orphan' >"$SITE_SRC/docs/orphan.md"
if ! scratch_build orphan && grep -q "orphan.md" "$SCRATCH/orphan.log"; then
  pass "a page outside the navigation fails the strict build"
else
  fail "a page outside the navigation fails the strict build"
  show "$SCRATCH/orphan.log"
fi
rm -f "$SITE_SRC/docs/orphan.md"
write_adr_to() { # dir number slug
  printf '# ADR-%s — Scratch decision\n\n- **Status:** Proposed\n- **Date:** 2026-10-15\n\n## Context\n\nScratch.\n' "$2" >"$1/$2-$3.md"
}
write_adr_to "$SITE_SRC/docs/adr" 0042 scratch-decision
if scratch_build newadr && grep -q 'href="0042-scratch-decision/"' "$SCRATCH/site-newadr/adr/index.html" &&
  grep -q "ADR-0042 Scratch decision" "$SCRATCH/site-newadr/index.html"; then
  pass "a new ADR appears in the index and the navigation without editing mkdocs.yml"
else
  fail "a new ADR appears in the index and the navigation without editing mkdocs.yml"
  show "$SCRATCH/newadr.log"
fi

# ---------------------------------------------------------------------------------------------
section "5. Configuration"
if uv lock --check --project tools/docs >"$SCRATCH/lock.log" 2>&1; then
  pass "tools/docs/uv.lock matches tools/docs/pyproject.toml"
else
  fail "tools/docs/uv.lock is out of date (uv lock --project tools/docs)"
  show "$SCRATCH/lock.log"
fi
if grep -qE '^docs:.*' Makefile && grep -A1 '^docs:' Makefile | grep -q -- '--strict'; then
  pass "make docs builds with --strict"
else
  fail "make docs builds with --strict"
fi
if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/docs.yml"))
on = doc[True] if True in doc else doc["on"]
assert on["push"]["branches"] == ["main"], on["push"]
for event in ("push", "pull_request"):
    paths = on[event]["paths"]
    for p in ("docs/**", "mkdocs.yml", "tools/docs/**", ".github/workflows/docs.yml"):
        assert p in paths, (event, p, paths)
jobs = doc["jobs"]
build, deploy = jobs["build"], jobs["deploy"]
uploads = [s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/upload-pages-artifact@")]
assert uploads and uploads[0]["with"]["path"] == "site", uploads
runs = [s.get("run", "") for s in build["steps"]]
assert "make docs" in runs and "make docs-links" in runs, runs
assert deploy["needs"] == "build", deploy["needs"]
assert "github.event_name != 'pull_request'" in deploy["if"], deploy["if"]
assert deploy["permissions"] == {"pages": "write", "id-token": "write"}, deploy["permissions"]
assert deploy["environment"]["name"] == "github-pages", deploy["environment"]
assert any(str(s.get("uses", "")).startswith("actions/deploy-pages@") for s in deploy["steps"])
assert doc["permissions"] == {"contents": "read"}, doc["permissions"]
PY
then pass "docs.yml: build on pull requests and main, upload site/, deploy only from main with Pages permissions"; else fail "docs.yml rules"; fi
if python3 - <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/ci.yml"))
job = doc["jobs"]["docs"]
assert job["name"] == "Docs", job["name"]
assert any(s.get("run") == "make test-docs" for s in job["steps"]), job["steps"]
assert "docs" in doc["jobs"]["ci"]["needs"], doc["jobs"]["ci"]["needs"]
PY
then pass "ci.yml: the Docs job runs make test-docs and feeds the CI gate"; else fail "ci.yml Docs job"; fi
if grep -q "mkdocs.yml" .github/workflows/docs.yml && grep -qE "^site/$" .gitignore; then
  pass "site/ is ignored by git"
else
  fail "site/ is ignored by git"
fi

# ---------------------------------------------------------------------------------------------
section "6. Links of the built site (lychee, offline)"
# The image comes from Docker Hub, whose anonymous pulls are rate limited per address and shared
# by the GitHub runners, so the pull is retried before the check runs and its error is shown.
pull_lychee() {
  local image attempt
  image="$(sed -n 's/^LYCHEE_IMAGE ?= //p' Makefile)"
  for attempt in 1 2 3; do
    docker pull -q "$image" >"$SCRATCH/pull.log" 2>&1 && return 0
    [[ "$attempt" -lt 3 ]] && sleep 20
  done
  return 1
}
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if ! pull_lychee; then
    fail "the lychee image cannot be pulled after three attempts"
    show "$SCRATCH/pull.log" 5
  elif make -s docs-links LYCHEE_ARGS=--offline >"$SCRATCH/lychee.log" 2>&1; then
    pass "lychee finds no broken internal link or fragment: $(grep -o '🔍.*' "$SCRATCH/lychee.log" | tail -n 1)"
  elif grep -q '🔍' "$SCRATCH/lychee.log"; then
    fail "lychee reports broken internal links or fragments"
    grep -E "ERR|🔍" "$SCRATCH/lychee.log" | sed 's/^/        /' | tail -n 30
  else
    fail "lychee did not run (build or container error)"
    show "$SCRATCH/lychee.log" 15
  fi
else
  skip "lychee link check (Docker is not available)"
fi

# ---------------------------------------------------------------------------------------------
printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
