#!/usr/bin/env bash
# Print the build context the base image takes its apt package lists from (issue #4).
#
# Usage: docker/apt-lists-context.sh [--tag]
#
# The lists for the snapshot ids pinned in docker/base-ros2/Dockerfile are published by CI as the
# image <FIELDPROOF_IMAGE_PREFIX>apt-lists:<APT_SNAPSHOT>-<ROS_SNAPSHOT>. When that image exists
# locally or on the registry the script prints `docker-image://<image>`, and the base image copies
# the lists from it instead of refreshing them from the slow Ubuntu snapshot host; otherwise it
# prints the absolute path of the empty directory docker/base-ros2/apt-lists-empty, which leaves
# the refresh from the snapshot hosts in place. FIELDPROOF_APT_LISTS=off forces the empty
# directory (a cold build as the first CI run sees it). `--tag` prints only the tag.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="${HERE}/base-ros2/Dockerfile"
EMPTY="${HERE}/base-ros2/apt-lists-empty"

prefix="${FIELDPROOF_IMAGE_PREFIX:-ghcr.io/dimitrios-kafetzis/fieldproof-}"
apt_snapshot="$(sed -n 's/^ARG APT_SNAPSHOT=//p' "$DOCKERFILE")"
ros_snapshot="$(sed -n 's/^ARG ROS_SNAPSHOT=//p' "$DOCKERFILE")"
[[ -n "$apt_snapshot" && -n "$ros_snapshot" ]] || { echo "apt-lists-context: snapshot ids not found in $DOCKERFILE" >&2; exit 1; }
tag="${apt_snapshot}-${ros_snapshot}"
image="${prefix}apt-lists:${tag}"

case "${1:-}" in
  --tag)
    echo "$tag"
    exit 0
    ;;
  -h | --help)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *)
    echo "apt-lists-context: unknown argument '$1'" >&2
    exit 2
    ;;
esac

if [[ "${FIELDPROOF_APT_LISTS:-on}" == "off" ]]; then
  echo "apt-lists-context: FIELDPROOF_APT_LISTS=off, lists come from the snapshot hosts" >&2
  echo "$EMPTY"
elif docker image inspect "$image" >/dev/null 2>&1; then
  echo "apt-lists-context: using the local image ${image}" >&2
  echo "docker-image://${image}"
elif docker buildx imagetools inspect "$image" >/dev/null 2>&1; then
  echo "apt-lists-context: using the published image ${image}" >&2
  echo "docker-image://${image}"
else
  echo "apt-lists-context: ${image} not found, lists come from the snapshot hosts" >&2
  echo "$EMPTY"
fi
