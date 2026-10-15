#!/usr/bin/env bash
# Free disk space on a GitHub-hosted Ubuntu runner before building the Fieldproof images
# (task P0-T05). The runner image ships toolchains the build never uses (.NET, Android, Haskell,
# CodeQL bundles, preloaded Docker images) that together take 25 to 30 GB; the three Fieldproof
# images and their layer cache need roughly 12 GB. Safe to run elsewhere: every path is checked
# and the script only removes directories that exist under the runner's well-known locations.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-false}" != "true" ]]; then
  echo "free-disk: not running on GitHub Actions, nothing to do"
  exit 0
fi

echo "Before:"
df -h / | tail -n 1

for dir in /usr/share/dotnet /usr/local/lib/android /opt/ghc /usr/local/.ghcup \
  /opt/hostedtoolcache/CodeQL /usr/share/swift /usr/local/share/powershell; do
  if [[ -d "$dir" ]]; then
    sudo rm -rf "$dir"
    echo "removed $dir"
  fi
done
if command -v docker >/dev/null 2>&1; then
  docker image prune -af >/dev/null 2>&1 || true
fi

echo "After:"
df -h / | tail -n 1
