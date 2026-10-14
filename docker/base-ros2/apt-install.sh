#!/usr/bin/env bash
# fieldproof-apt-install PACKAGE...
#
# `apt-get install` with the package files fetched in parallel first.
#
# Package versions are resolved from the snapshot indexes configured in /etc/apt (the pin). The
# Ubuntu snapshot host, however, is a single origin that answers each request with seconds of
# latency, and apt downloads from one host serially, so a cold build installing ~800 packages
# would blow the 15-minute budget. This script therefore:
#   1. runs `apt-get update` only when the lists for every configured source are missing
#      (snapshot sources never change, so present lists are current);
#   2. asks apt which files it would download and fetches them with FIELDPROOF_APT_PARALLEL
#      connections through apt's own download helper: Ubuntu pool files are tried on the fast
#      archive mirror FIELDPROOF_APT_MIRROR first (same file, same path, still verified by apt
#      against the snapshot index) and fall back to the snapshot host with retries;
#   3. runs the real `apt-get install`, which checks every cached file's hash against the signed
#      index before unpacking.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: fieldproof-apt-install PACKAGE..." >&2
  exit 2
fi

PARALLEL="${FIELDPROOF_APT_PARALLEL:-16}"
MIRROR="${FIELDPROOF_APT_MIRROR:-http://archive.ubuntu.com/ubuntu}"
ARCHIVES=/var/cache/apt/archives
LISTS=/var/lib/apt/lists
APT_HELPER=/usr/lib/apt/apt-helper

# apt names list files after the source URI (scheme stripped, slashes replaced by underscores) and
# the Ubuntu image keeps them compressed (Acquire::GzipIndexes), hence the trailing wildcard.
lists_present() {
  local uri prefix
  while read -r uri; do
    prefix="${uri#*://}"
    prefix="${prefix%/}"
    prefix="${prefix//\//_}"
    compgen -G "${LISTS}/${prefix}_*_Packages*" >/dev/null || return 1
  done < <(sed -n 's/^URIs: *//p' /etc/apt/sources.list.d/*.sources)
  return 0
}

# A failed index download is only a warning for apt-get update, after which installs fail with
# "no installation candidate"; Error-Mode=any makes it fatal, and the update is retried because
# the snapshot host also returns the odd 502.
update_lists() {
  local attempt
  for attempt in 1 2 3 4 5; do
    apt-get update -o APT::Update::Error-Mode=any && return 0
    echo "fieldproof-apt-install: apt-get update failed (attempt ${attempt}/5), retrying" >&2
    sleep $((attempt * 5))
  done
  return 1
}

if lists_present; then
  echo "fieldproof-apt-install: package lists already present, skipping apt-get update"
else
  update_lists
fi

mkdir -p "${ARCHIVES}/partial"
# --print-uris lines look like: 'https://host/pool/.../pkg_1.0_amd64.deb' pkg_1.0_amd64.deb 12345 SHA256:...
uris="$(apt-get install -y --no-install-recommends --print-uris "$@" | awk -F"'" '/^'"'"'/ { print $2, $3 }' | awk '{ print $1, $2 }')"
total="$(grep -c . <<<"$uris" || true)"
echo "fieldproof-apt-install: ${total} package file(s) to fetch with ${PARALLEL} connections (mirror ${MIRROR})"

download() {
  local uri="$1" file="$2"
  if "${APT_HELPER}" download-file "${uri}" "${ARCHIVES}/partial/${file}" >/dev/null 2>&1; then
    mv "${ARCHIVES}/partial/${file}" "${ARCHIVES}/${file}"
    return 0
  fi
  rm -f "${ARCHIVES}/partial/${file}"
  return 1
}

fetch() {
  local uri="$1" file="$2" attempt
  if [[ -s "${ARCHIVES}/${file}" ]]; then
    return 0
  fi
  if [[ "$uri" == *"://snapshot.ubuntu.com/ubuntu/"*"/pool/"* ]]; then
    download "${MIRROR}/pool/${uri#*/pool/}" "$file" && return 0
  fi
  # The snapshot host returns the odd HTTP 500 under parallel load, which apt-helper does not
  # retry, so each file gets a few attempts with backoff before the whole install fails.
  for attempt in 1 2 3 4 5; do
    download "$uri" "$file" && return 0
    sleep $((attempt * 2))
  done
  echo "fieldproof-apt-install: giving up on ${uri}" >&2
  return 1
}
export -f download fetch
export ARCHIVES APT_HELPER MIRROR

if [[ "$total" -gt 0 ]]; then
  xargs -P "${PARALLEL}" -n 2 bash -c 'fetch "$@"' _ <<<"$uris"
fi

apt-get install -y --no-install-recommends "$@"
