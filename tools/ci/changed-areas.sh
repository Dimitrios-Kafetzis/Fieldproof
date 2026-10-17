#!/usr/bin/env bash
# Decide which optional CI areas a change touches (task P0-T05, ADR-0008 path filters).
#
# Usage: tools/ci/changed-areas.sh [BASE_SHA]
#
# Prints one `<area>=true|false` line per area in GitHub Actions output format. Without a base
# (push to main, manual run) every area is true. With a base, the files changed between the base
# and HEAD decide; for a pull request HEAD is GitHub's merge commit, whose first parent is the
# base, so `git diff BASE HEAD` is exactly the pull request's diff.
#
# Areas:
#   images   docker/, tests/images/, the Makefile, version.txt, the CI helpers and ci.yml itself,
#            plus the ROS workspaces (sim/, bridge/, robots/), tools/ros/ and the clang-tidy
#            configuration, whose tests run in the dev container this job builds (P0-T07), the
#            assets, their fetch script and the world tests (P1-T02), and the example vehicles,
#            the engine's vehicle image generator and the vehicle tests, whose image builds on
#            the base image this job builds (P1-T04)
set -euo pipefail

base="${1:-}"

IMAGES_PATTERNS=(
  '^docker/'
  '^tests/images/'
  '^Makefile$'
  '^version\.txt$'
  '^tools/ci/'
  '^\.github/workflows/ci\.yml$'
  '^sim/'
  '^bridge/'
  '^robots/'
  '^tools/ros/'
  '^\.clang-tidy$'
  '^assets/'
  '^tools/assets/'
  '^tests/worlds/'
  '^examples/vehicles/'
  '^engine/src/fieldproof/(manifest|vehicle|vehicle_cli)\.py$'
  '^engine/schemas/'
  '^tests/vehicles/'
)

if [[ -z "$base" ]]; then
  echo "images=true"
  exit 0
fi

if ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
  echo "changed-areas: base commit '$base' is not available; running every area" >&2
  echo "images=true"
  exit 0
fi

changed="$(git diff --name-only "$base" HEAD)"
images=false
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  for pattern in "${IMAGES_PATTERNS[@]}"; do
    if [[ "$file" =~ $pattern ]]; then
      images=true
      break 2
    fi
  done
done <<<"$changed"

echo "changed-areas: $(grep -c . <<<"$changed" || true) file(s) changed since ${base:0:7}" >&2
echo "images=$images"
