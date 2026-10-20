#!/usr/bin/env bash
# Tests for the quality gates (task P0-T04): .pre-commit-config.yaml, the tool configurations,
# commitlint.config.mjs and the Makefile.
#
# 1. The pre-commit configuration validates and every hook passes on the repository (definition
#    of done).
# 2. In a scratch copy of the repository the installed git hook rejects non-conventional commit
#    messages and accepts conventional ones (definition of done), driven through `git commit`.
# 3. In the same scratch copy, one deliberately bad file per tool makes exactly that tool's hook
#    fail: ruff, mypy, clang-format, cmake-format, eslint, prettier, yamllint and markdownlint
#    plus shellcheck, hadolint, gitleaks and the GitHub workflow schema check.
# 4. commitlint accepts and rejects the documented message shapes (scope rules, breaking changes,
#    squash-merge titles) when driven directly.
#
# Needs pre-commit, Node 22 and Docker (hadolint) on the host, as installed by tools/host-setup.sh.
# The hook environments are shared with the real repository through pre-commit's cache.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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

# Copy the tracked files of the repository into an independent git repository so hooks can be
# installed and bad files committed without touching the real checkout.
make_scratch_repo() {
  git -C "$REPO" ls-files -z | (cd "$REPO" && tar --null -cf - -T -) | tar -xf - -C "$SCRATCH"
  git -C "$SCRATCH" init -q -b main
  git -C "$SCRATCH" config user.name "Fieldproof tests"
  git -C "$SCRATCH" config user.email "tests@fieldproof.invalid"
  git -C "$SCRATCH" config commit.gpgsign false
  git -C "$SCRATCH" add -A
  git -C "$SCRATCH" commit -q -m "chore(repo): scratch copy for the quality gate tests"
}

# Run the commitlint hook on a message without going through git.
commitlint_msg() {
  local file
  file="$(mktemp)"
  printf '%s\n' "$1" > "$file"
  local rc=0
  (cd "$REPO" && pre-commit run commitlint --hook-stage commit-msg --commit-msg-filename "$file") >/dev/null 2>&1 || rc=$?
  rm -f "$file"
  return "$rc"
}

cd "$REPO"

printf '\n\033[1m1. Hooks on the repository\033[0m\n'
if pre-commit validate-config >/dev/null 2>&1; then pass "pre-commit validate-config"; else fail "pre-commit validate-config"; fi
OUT="$SCRATCH/all-files.log"
if pre-commit run --all-files > "$OUT" 2>&1; then
  pass "pre-commit run --all-files passes ($(grep -c 'Passed$' "$OUT") passed, $(grep -c 'Skipped$' "$OUT") skipped for lack of files)"
else
  fail "pre-commit run --all-files"
  grep -E 'Failed$|^- hook id' "$OUT" | sed 's/^/        /'
fi

printf '\n\033[1m2. Installed commit-msg hook\033[0m\n'
make_scratch_repo
(cd "$SCRATCH" && pre-commit install --install-hooks >/dev/null 2>&1)
if (cd "$SCRATCH" && git commit -q --allow-empty -m "Added some stuff") >/dev/null 2>&1; then
  fail "non-conventional message 'Added some stuff' is rejected by git commit"
else
  pass "non-conventional message 'Added some stuff' is rejected by git commit"
fi
if (cd "$SCRATCH" && git commit -q --allow-empty -m "feat(engine): add scenario loader") >/dev/null 2>&1; then
  pass "conventional message 'feat(engine): add scenario loader' is accepted by git commit"
else
  fail "conventional message 'feat(engine): add scenario loader' is accepted by git commit"
fi

printf '\n\033[1m3. Each gate catches a bad file\033[0m\n'
BAD="$SCRATCH/bad"
mkdir -p "$BAD" "$SCRATCH/.github/workflows"
printf 'import os\n\n\ndef f(x):\n    return x\n' > "$BAD/lint.py"            # ruff F401/D103, mypy no-untyped-def
printf 'x  =  1\n' > "$BAD/format.py"                                           # ruff-format rewrites
printf 'int main(){return 0;}\n' > "$BAD/style.cpp"                             # clang-format rewrites
printf 'cmake_minimum_required(VERSION 3.16)\nproject(   Bad )\n' > "$BAD/CMakeLists.txt"  # cmake-format rewrites
printf 'var x = 1;\nconsole.log(x == 1);\n' > "$BAD/lint.ts"                    # eslint no-var, eqeqeq, no-console
printf 'const a = {b:1}\n' > "$BAD/format.ts"                                   # prettier rewrites
printf 'key:  value\n' > "$BAD/lint.yaml"                                       # yamllint colons
printf '#Heading\n' > "$BAD/lint.md"                                            # markdownlint MD018
# shellcheck disable=SC2016  # the literal $(ls) and $foo are the bad script's content
printf '#!/usr/bin/env bash\nfoo=$(ls)\necho $foo\n' > "$BAD/lint.sh"           # SC2086 unquoted variable
chmod +x "$BAD/lint.sh"
printf 'FROM ubuntu:latest\nRUN apt-get install curl\n' > "$BAD/Dockerfile"     # hadolint DL3007, DL3008
printf 'token = "ghp_%s"\n' "$(head -c 256 /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 36)" > "$BAD/secret.txt"  # gitleaks
printf 'on: push\njobs:\n  build:\n    steps: []\n' > "$SCRATCH/.github/workflows/bad.yml"  # schema: runs-on missing
printf '# ADR-0099 — Wrong number\n\n- **Status:** Proposed\n- **Date:** 2026-10-15\n' > "$SCRATCH/docs/adr/0042-bad.md"  # adr-index: heading number differs
git -C "$SCRATCH" add -A
OUT="$SCRATCH/bad-files.log"
if (cd "$SCRATCH" && pre-commit run --all-files) > "$OUT" 2>&1; then
  fail "pre-commit run --all-files fails on the bad files"
else
  pass "pre-commit run --all-files fails on the bad files"
fi
for id in ruff-check ruff-format mypy clang-format cmake-format eslint prettier yamllint \
  markdownlint-cli2 shellcheck hadolint-docker gitleaks check-github-workflows adr-index; do
  if grep -qx -- "- hook id: $id" "$OUT"; then pass "$id catches its bad file"; else fail "$id catches its bad file"; fi
done

printf '\n\033[1m4. commitlint rules\033[0m\n'
for msg in \
  "feat(engine): add scenario loader" \
  "fix(cockpit)!: drop the legacy protocol" \
  "docs(plan): mark P0-T04 in review" \
  "build(deps): bump ruff to 0.17.0" \
  "P0-T02 Host environment setup (#2)"; do
  if commitlint_msg "$msg"; then pass "accepts '$msg'"; else fail "accepts '$msg'"; fi
done
for msg in \
  "Added some stuff" \
  "feat: scope is required" \
  "feat(kitchen): unknown scope" \
  "feat(engine): Capitalised subject" \
  "feat(engine): trailing full stop." \
  "wip(engine): unknown type"; do
  if commitlint_msg "$msg"; then fail "rejects '$msg'"; else pass "rejects '$msg'"; fi
done
# Body lines wrap at 100 characters, except in Dependabot's commits (its sign-off is the mark):
# their release notes and compare links are longer, and nobody wraps them by hand.
long_line="$(printf 'x%.0s' $(seq 1 140))"
dependabot=$'build(deps): bump the actions group with 2 updates\n\nBumps the actions group: '"$long_line"$'\n\nSigned-off-by: dependabot[bot] <support@github.com>'
if commitlint_msg "$dependabot"; then pass "accepts a Dependabot commit with a 160-character body line"; else fail "accepts a Dependabot commit with a long body line"; fi
long_body=$'feat(engine): add a loader\n\nA line nobody wrapped: '"$long_line"$'\n\nTask: P0-T04'
if commitlint_msg "$long_body"; then fail "rejects a long body line in an ordinary commit"; else pass "rejects a long body line in an ordinary commit"; fi
bad_dependabot=$'build(deps): Bump the actions group.\n\nSigned-off-by: dependabot[bot] <support@github.com>'
if commitlint_msg "$bad_dependabot"; then fail "rejects a Dependabot header breaking the subject rules"; else pass "rejects a Dependabot header breaking the subject rules"; fi

printf '\n%d passed, %d failed\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]
