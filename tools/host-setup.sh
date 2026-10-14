#!/usr/bin/env bash
# Fieldproof host setup for Ubuntu 24.04 (task P0-T02).
#
# Installs the host toolchain and nothing else: Docker Engine with the Compose and Buildx plugins,
# qemu-user-static with binfmt registered for arm64, Node 22 LTS via nvm, pnpm via corepack, uv,
# pre-commit and the GitHub CLI. ROS and Gazebo never install on the host (ADR-0004).
#
# The script is idempotent: run it again and it only fixes what drifted. Every version is pinned
# below; bump them deliberately and record the change in docs/setup/host.md.
#
# Usage:
#   tools/host-setup.sh            run both phases (asks for sudo once)
#   tools/host-setup.sh --root     run only the root phase (apt, groups, services); needs sudo
#   tools/host-setup.sh --user     run only the user phase (nvm, node, pnpm, uv, pre-commit)
#
# Afterwards run tools/host-check.sh. If the script added you to the `docker` group, log out and
# back in (or use `sg docker -c '<command>'`) before the group is active in your shell.
set -euo pipefail

# ---------------------------------------------------------------------------------------------
# Pinned versions
# ---------------------------------------------------------------------------------------------
UBUNTU_CODENAME_EXPECTED="noble"

DOCKER_APT_SUITE="noble"
DOCKER_APT_KEY_URL="https://download.docker.com/linux/ubuntu/gpg"
DOCKER_APT_KEY_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
DOCKER_CE_VERSION="5:29.9.0-1~ubuntu.24.04~noble"
CONTAINERD_VERSION="2.4.2-1~ubuntu.24.04~noble"
DOCKER_BUILDX_VERSION="0.38.0-1~ubuntu.24.04~noble"
DOCKER_COMPOSE_VERSION="5.6.0-1~ubuntu.24.04~noble"

QEMU_USER_STATIC_VERSION="1:8.2.2+ds-0ubuntu1.18"
BINFMT_SUPPORT_VERSION="2.2.2-7"
GH_VERSION="2.45.0-1ubuntu0.3"

NVM_VERSION="v0.40.8"
NVM_REPO="https://github.com/nvm-sh/nvm.git"
NODE_VERSION="22.23.3"
PNPM_VERSION="12.11.2"

UV_VERSION="0.13.1"
UV_TARGET="x86_64-unknown-linux-gnu"
PRE_COMMIT_VERSION="4.7.0"

# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

installed_version() { dpkg-query -W -f='${Version}' "$1" 2>/dev/null || true; }

require_ubuntu_noble() {
  local codename
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
  [[ "$codename" == "$UBUNTU_CODENAME_EXPECTED" ]] \
    || die "this script targets Ubuntu 24.04 (noble); found '${codename:-unknown}'"
}

# ---------------------------------------------------------------------------------------------
# Root phase: apt packages, docker group, services
# ---------------------------------------------------------------------------------------------
root_phase() {
  [[ "$(id -u)" -eq 0 ]] || die "the root phase must run as root (use sudo)"
  local target_user="${SUDO_USER:-}"
  [[ -n "$target_user" && "$target_user" != "root" ]] \
    || die "run via sudo from your own account so SUDO_USER names the user to add to the docker group"
  export DEBIAN_FRONTEND=noninteractive

  log "Installing apt prerequisites"
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg >/dev/null
  ok "ca-certificates, curl, gnupg"

  log "Configuring the Docker apt repository (${DOCKER_APT_SUITE} stable, pinned key)"
  install -m 0755 -d /etc/apt/keyrings
  local key_tmp
  key_tmp="$(mktemp)"
  curl -fsSL "$DOCKER_APT_KEY_URL" -o "$key_tmp"
  local fpr
  fpr="$(gpg --batch --with-colons --show-keys "$key_tmp" | awk -F: '$1 == "fpr" { print $10; exit }')"
  [[ "$fpr" == "$DOCKER_APT_KEY_FINGERPRINT" ]] \
    || die "Docker apt key fingerprint mismatch: expected ${DOCKER_APT_KEY_FINGERPRINT}, got ${fpr}"
  install -m 0644 "$key_tmp" /etc/apt/keyrings/docker.asc
  rm -f "$key_tmp"
  local arch
  arch="$(dpkg --print-architecture)"
  local source_line="deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${DOCKER_APT_SUITE} stable"
  if [[ ! -f /etc/apt/sources.list.d/docker.list ]] \
    || [[ "$(cat /etc/apt/sources.list.d/docker.list)" != "$source_line" ]]; then
    echo "$source_line" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
  fi
  ok "key fingerprint ${fpr}"

  log "Installing Docker Engine, containerd, Buildx and Compose (pinned)"
  apt-get install -y -qq --no-install-recommends --allow-downgrades \
    "docker-ce=${DOCKER_CE_VERSION}" \
    "docker-ce-cli=${DOCKER_CE_VERSION}" \
    "containerd.io=${CONTAINERD_VERSION}" \
    "docker-buildx-plugin=${DOCKER_BUILDX_VERSION}" \
    "docker-compose-plugin=${DOCKER_COMPOSE_VERSION}" >/dev/null
  ok "docker-ce $(installed_version docker-ce)"
  ok "containerd.io $(installed_version containerd.io)"
  ok "docker-buildx-plugin $(installed_version docker-buildx-plugin)"
  ok "docker-compose-plugin $(installed_version docker-compose-plugin)"

  log "Installing qemu-user-static and binfmt-support (pinned)"
  apt-get install -y -qq --no-install-recommends --allow-downgrades \
    "qemu-user-static=${QEMU_USER_STATIC_VERSION}" \
    "binfmt-support=${BINFMT_SUPPORT_VERSION}" >/dev/null
  ok "qemu-user-static $(installed_version qemu-user-static)"
  ok "binfmt-support $(installed_version binfmt-support)"

  log "Installing GitHub CLI (pinned)"
  apt-get install -y -qq --no-install-recommends --allow-downgrades "gh=${GH_VERSION}" >/dev/null
  ok "gh $(installed_version gh)"

  log "Enabling and starting the Docker service"
  systemctl enable --now docker.service containerd.service >/dev/null
  ok "docker.service $(systemctl is-active docker.service)"

  log "Ensuring binfmt registration for arm64 uses the fix-binary flag (required inside containers)"
  if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]]; then
    update-binfmts --enable qemu-aarch64
  fi
  if [[ "$(awk '/^flags:/ { print $2 }' /proc/sys/fs/binfmt_misc/qemu-aarch64)" != *F* ]]; then
    update-binfmts --disable qemu-aarch64 || true
    update-binfmts --import qemu-aarch64
  fi
  ok "qemu-aarch64 flags: $(awk '/^flags:/ { print $2 }' /proc/sys/fs/binfmt_misc/qemu-aarch64)"

  log "Adding ${target_user} to the docker group"
  if id -nG "$target_user" | tr ' ' '\n' | grep -qx docker; then
    ok "${target_user} already in docker"
  else
    usermod -aG docker "$target_user"
    ok "${target_user} added to docker (log out and back in to activate)"
  fi
}

# ---------------------------------------------------------------------------------------------
# User phase: nvm, Node, pnpm, uv, pre-commit
# ---------------------------------------------------------------------------------------------
ensure_bashrc_block() {
  # Append a marked block to ~/.bashrc once.
  local marker="$1" block="$2" rc="${HOME}/.bashrc"
  touch "$rc"
  if ! grep -qF "$marker" "$rc"; then
    printf '\n%s\n%s\n' "$marker" "$block" >> "$rc"
  fi
}

user_phase() {
  [[ "$(id -u)" -ne 0 ]] || die "the user phase must run as your own user, not root"
  local bin_dir="${HOME}/.local/bin"
  mkdir -p "$bin_dir"

  log "Installing nvm ${NVM_VERSION}"
  export NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"
  if [[ ! -d "${NVM_DIR}/.git" ]]; then
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$NVM_VERSION" "$NVM_REPO" "$NVM_DIR"
  elif [[ "$(git -C "$NVM_DIR" describe --tags --exact-match 2>/dev/null || true)" != "$NVM_VERSION" ]]; then
    git -C "$NVM_DIR" fetch --quiet --depth 1 origin "refs/tags/${NVM_VERSION}:refs/tags/${NVM_VERSION}"
    git -C "$NVM_DIR" -c advice.detachedHead=false checkout --quiet "$NVM_VERSION"
  fi
  # The block is written to ~/.bashrc verbatim; it must not expand here.
  # shellcheck disable=SC2016
  ensure_bashrc_block "# Fieldproof: nvm (tools/host-setup.sh)" \
    'export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"'
  # nvm.sh is not shellcheck-clean and uses unset variables; relax the shell while sourcing it.
  set +u
  # shellcheck disable=SC1091
  . "${NVM_DIR}/nvm.sh"
  ok "nvm $(nvm --version)"

  log "Installing Node ${NODE_VERSION} and making it the default"
  nvm install "$NODE_VERSION" >/dev/null 2>&1 || nvm install "$NODE_VERSION"
  nvm alias default "$NODE_VERSION" >/dev/null
  nvm use --silent "$NODE_VERSION"
  set -u
  ok "node $(node --version) at $(command -v node)"

  log "Enabling pnpm ${PNPM_VERSION} through corepack"
  export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
  corepack enable pnpm
  corepack install -g "pnpm@${PNPM_VERSION}" >/dev/null
  ok "pnpm $(pnpm --version) via corepack $(corepack --version)"

  log "Installing uv ${UV_VERSION}"
  if [[ "$("$bin_dir/uv" --version 2>/dev/null | awk '{ print $2 }')" != "$UV_VERSION" ]]; then
    local tmp archive base_url
    tmp="$(mktemp -d)"
    archive="uv-${UV_TARGET}.tar.gz"
    base_url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"
    curl -fsSL "${base_url}/${archive}" -o "${tmp}/${archive}"
    curl -fsSL "${base_url}/${archive}.sha256" -o "${tmp}/${archive}.sha256"
    (cd "$tmp" && sha256sum --check --quiet "${archive}.sha256")
    tar -xzf "${tmp}/${archive}" -C "$tmp"
    install -m 0755 "${tmp}/uv-${UV_TARGET}/uv" "${tmp}/uv-${UV_TARGET}/uvx" "$bin_dir/"
    rm -rf "$tmp"
  fi
  ok "$("$bin_dir/uv" --version)"

  log "Installing pre-commit ${PRE_COMMIT_VERSION} as a uv tool"
  "$bin_dir/uv" tool install --quiet --python 3.12 "pre-commit==${PRE_COMMIT_VERSION}"
  ok "$("$bin_dir/pre-commit" --version)"

  log "GitHub CLI authentication"
  if gh auth status >/dev/null 2>&1; then
    ok "gh is authenticated"
  else
    printf '  gh is installed but not authenticated. Run: gh auth login --web --git-protocol https\n'
  fi
}

# ---------------------------------------------------------------------------------------------
main() {
  require_ubuntu_noble
  case "${1:-}" in
    --root) root_phase ;;
    --user) user_phase ;;
    "")
      [[ "$(id -u)" -ne 0 ]] || die "run this script as your own user; it calls sudo for the root phase"
      # -A uses $SUDO_ASKPASS when the script runs without a terminal (for example from an IDE task).
      if [[ -t 0 ]]; then sudo bash "$0" --root; else sudo -A bash "$0" --root; fi
      user_phase
      printf '\nHost setup complete. Run tools/host-check.sh to verify.\n'
      ;;
    *) die "unknown argument '$1' (expected --root, --user or nothing)" ;;
  esac
}

main "$@"
