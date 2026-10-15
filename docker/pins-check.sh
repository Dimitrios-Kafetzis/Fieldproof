#!/usr/bin/env bash
# Compare the version pins of the container images with their upstreams (issue #9).
#
# Usage: docker/pins-check.sh [--summary FILE] [--help]
#
# Dependabot cannot update these pins: every base image is referenced through a build argument
# (`FROM ubuntu:24.04@${UBUNTU_DIGEST}`, `FROM ${UV_IMAGE}`) and the apt sources are dated
# snapshots, none of which its docker updater understands. This script is the replacement signal:
# it prints one line per pin with the pinned and the current upstream value and exits 1 when any
# upstream has moved, so the weekly `Pins` workflow turns red until the pins are updated as
# described in docs/setup/containers.md. `--summary FILE` appends a Markdown table (the workflow
# passes $GITHUB_STEP_SUMMARY).
#
# Pins checked: UBUNTU_DIGEST (ubuntu:24.04 on Docker Hub), ROS_SNAPSHOT (newest directory under
# http://snapshots.ros.org/jazzy/), UV_IMAGE (newest astral-sh/uv release on GitHub). APT_SNAPSHOT
# is reported with its age but never fails the check: any timestamp is a valid Ubuntu snapshot,
# and it is moved together with ROS_SNAPSHOT.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DOCKERFILE="${HERE}/base-ros2/Dockerfile"
DEV_DOCKERFILE="${HERE}/dev/Dockerfile"
ROS_DISTRO=jazzy

summary=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --summary)
      [[ $# -ge 2 ]] || { echo "pins-check: --summary needs a file" >&2; exit 2; }
      summary="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "pins-check: unknown argument '$1'" >&2
      exit 2
      ;;
  esac
done

pin() { sed -n "s/^ARG $1=//p" "$2" | head -n 1; }
ubuntu_pinned="$(pin UBUNTU_DIGEST "$BASE_DOCKERFILE")"
apt_pinned="$(pin APT_SNAPSHOT "$BASE_DOCKERFILE")"
ros_pinned="$(pin ROS_SNAPSHOT "$BASE_DOCKERFILE")"
uv_pinned="$(pin UV_IMAGE "$DEV_DOCKERFILE")"
uv_version_pinned="${uv_pinned#*:}"
uv_version_pinned="${uv_version_pinned%@*}"
[[ -n "$ubuntu_pinned" && -n "$apt_pinned" && -n "$ros_pinned" && -n "$uv_pinned" ]] \
  || { echo "pins-check: could not read every pin from the Dockerfiles" >&2; exit 1; }

ubuntu_current="$(docker buildx imagetools inspect ubuntu:24.04 --format '{{.Manifest.Digest}}' 2>/dev/null || echo unavailable)"
ros_current="$(curl -fsSL "http://snapshots.ros.org/${ROS_DISTRO}/" 2>/dev/null \
  | grep -oE 'href="[0-9]{4}-[0-9]{2}-[0-9]{2}/"' | tr -d 'href="/' | sort | tail -n 1 || true)"
ros_current="${ros_current:-unavailable}"
uv_current="$(curl -fsSL https://api.github.com/repos/astral-sh/uv/releases/latest 2>/dev/null \
  | sed -n 's/^ *"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1 || true)"
uv_current="${uv_current:-unavailable}"
apt_age_days="$(( ($(date -u +%s) - $(date -u -d "${apt_pinned:0:8} ${apt_pinned:9:2}:${apt_pinned:11:2}" +%s)) / 86400 ))"

ROWS=()
stale=0
row() {
  local name="$1" pinned="$2" current="$3" verdict="$4"
  printf '  %-14s pinned %-72s current %-72s %s\n' "$name" "$pinned" "$current" "$verdict"
  ROWS+=("| \`$name\` | \`$pinned\` | \`$current\` | $verdict |")
}
verdict() {
  local pinned="$1" current="$2"
  if [[ "$current" == "unavailable" ]]; then echo "unknown (upstream not reachable)"
  elif [[ "$pinned" == "$current" ]]; then echo "ok"
  else stale=1; echo "STALE"
  fi
}

echo "Pins in ${BASE_DOCKERFILE#"$HERE"/} and ${DEV_DOCKERFILE#"$HERE"/}:"
row UBUNTU_DIGEST "$ubuntu_pinned" "$ubuntu_current" "$(verdict "$ubuntu_pinned" "$ubuntu_current")"
row APT_SNAPSHOT "$apt_pinned" "${apt_age_days} days old" "ok (any timestamp is valid)"
row ROS_SNAPSHOT "$ros_pinned" "$ros_current" "$(verdict "$ros_pinned" "$ros_current")"
row UV_IMAGE "$uv_version_pinned" "$uv_current" "$(verdict "$uv_version_pinned" "$uv_current")"

if [[ -n "$summary" ]]; then
  {
    echo "## Container image pins"
    echo
    echo "| Pin | Pinned | Upstream | Verdict |"
    echo "| --- | --- | --- | --- |"
    printf '%s\n' "${ROWS[@]}"
    echo
    if [[ "$stale" -eq 1 ]]; then
      echo "At least one upstream moved. Update the pins as described in docs/setup/containers.md (Updating a pin)."
    else
      echo "Every pin matches its upstream."
    fi
  } >> "$summary"
fi

if [[ "$stale" -eq 1 ]]; then
  echo "pins-check: at least one pin is stale (see docs/setup/containers.md, Updating a pin)" >&2
  exit 1
fi
echo "pins-check: every pin matches its upstream"
