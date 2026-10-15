#!/usr/bin/env bash
# Print `docker buildx bake --set` overrides that attach a registry layer cache to every image
# target of docker/compose.yaml (task P0-T05).
#
# Usage: tools/ci/bake-cache.sh [--read TAG]... [--write TAG]
#
#   --read TAG    add `<target>.cache-from=type=registry,ref=<prefix><target>:TAG` (repeatable;
#                 BuildKit tries the caches in order)
#   --write TAG   add `<target>.cache-to=type=registry,ref=<prefix><target>:TAG,mode=max`
#
# The image prefix comes from FIELDPROOF_IMAGE_PREFIX (default ghcr.io/dimitrios-kafetzis/fieldproof-),
# the same variable docker/compose.yaml uses for the image names. ci.yml reads the caches written by
# main (`buildcache`) and by earlier pull requests (`buildcache-pr`); images.yml writes `buildcache`.
set -euo pipefail

TARGETS=(base-ros2 sim dev)
prefix="${FIELDPROOF_IMAGE_PREFIX:-ghcr.io/dimitrios-kafetzis/fieldproof-}"
reads=()
write=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --read)
      [[ $# -ge 2 ]] || { echo "bake-cache: --read needs a tag" >&2; exit 2; }
      reads+=("$2")
      shift 2
      ;;
    --write)
      [[ $# -ge 2 ]] || { echo "bake-cache: --write needs a tag" >&2; exit 2; }
      write="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "bake-cache: unknown argument '$1'" >&2
      exit 2
      ;;
  esac
done

for target in "${TARGETS[@]}"; do
  for tag in "${reads[@]+"${reads[@]}"}"; do
    echo "${target}.cache-from=type=registry,ref=${prefix}${target}:${tag}"
  done
  if [[ -n "$write" ]]; then
    echo "${target}.cache-to=type=registry,ref=${prefix}${target}:${write},mode=max"
  fi
done
