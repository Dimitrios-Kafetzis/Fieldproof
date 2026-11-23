#!/usr/bin/env bash
# Register this machine as the self-hosted runner of the heavy CI jobs (the image job of ci.yml,
# the four jobs of scenario-tests.yml), which a private repository's 2 CPU GitHub runner cannot
# carry (docs/setup/ci.md, "Runners").
#
# Usage: tools/ci/self-hosted-runner.sh [--check] [--repo OWNER/NAME] [--dir DIR] [--label LABEL]
#
# Without --check: downloads the pinned actions/runner release into DIR (default
# ~/actions-runner), verifies its checksum, registers it with a token from `gh` under LABEL
# (default `fieldproof`), isolates its Docker credentials (DOCKER_CONFIG in the runner's .env),
# loads the netem and ifb kernel modules now and at boot, installs the runner as a systemd
# service running as the current user, and points the repository variable FIELDPROOF_HEAVY_RUNNER
# at LABEL so the workflows pick the runner up. Needs `gh` authenticated as a repository admin,
# Docker usable without sudo, and sudo for the modules and the service. With --check it prints the
# runner's state as the repository sees it, the variable, the modules and the service, and
# changes nothing.
set -euo pipefail

RUNNER_VERSION=2.338.2
RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
MODULES_CONF=/etc/modules-load.d/fieldproof-ci.conf
VARIABLE=FIELDPROOF_HEAVY_RUNNER

repo=""
dir="${HOME}/actions-runner"
label="fieldproof"
check=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) check=1; shift ;;
    --repo)
      [[ $# -ge 2 ]] || { echo "self-hosted-runner: --repo needs OWNER/NAME" >&2; exit 2; }
      repo="$2"; shift 2 ;;
    --dir)
      [[ $# -ge 2 ]] || { echo "self-hosted-runner: --dir needs a directory" >&2; exit 2; }
      dir="$2"; shift 2 ;;
    --label)
      [[ $# -ge 2 ]] || { echo "self-hosted-runner: --label needs a label" >&2; exit 2; }
      label="$2"; shift 2 ;;
    -h | --help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "self-hosted-runner: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [[ -z "$repo" ]]; then
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
fi
tarball="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
url="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${tarball}"

modules_loaded() {
  lsmod | grep -qE '^sch_netem ' && lsmod | grep -qE '^ifb '
}

if [[ "$check" -eq 1 ]]; then
  echo "Repository: $repo"
  echo "Runners:"
  gh api "repos/$repo/actions/runners" \
    -q '.runners[] | "  \(.name): \(.status), busy \(.busy), labels \([.labels[].name] | join(","))"' \
    || echo "  (cannot list the runners; admin access needed)"
  if ! value="$(gh api "repos/$repo/actions/variables/$VARIABLE" -q .value 2>/dev/null)"; then value=""; fi
  echo "Variable $VARIABLE: ${value:-(unset: the heavy jobs run on the GitHub runner)}"
  if modules_loaded; then echo "Modules: sch_netem and ifb loaded"; else echo "Modules: sch_netem or ifb NOT loaded"; fi
  [[ -f "$MODULES_CONF" ]] && echo "Modules at boot: $MODULES_CONF" || echo "Modules at boot: not configured"
  if [[ -f "$dir/.runner" ]]; then
    echo "Runner directory: $dir (configured)"
    (cd "$dir" && sudo -n ./svc.sh status 2>/dev/null) || systemctl list-units --type=service --all 'actions.runner.*' --no-pager 2>/dev/null | sed 's/^/  /' || true
  else
    echo "Runner directory: $dir (not configured)"
  fi
  exit 0
fi

# Prerequisites.
for tool in gh docker git make curl tar sha256sum sudo; do
  command -v "$tool" >/dev/null 2>&1 || { echo "self-hosted-runner: $tool is required" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "self-hosted-runner: docker must work without sudo (add $USER to the docker group)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "self-hosted-runner: gh is not authenticated" >&2; exit 1; }
if [[ -f "$dir/.runner" ]]; then
  echo "self-hosted-runner: $dir already holds a configured runner; run with --check, or remove it" >&2
  echo "  (cd $dir && sudo ./svc.sh stop && sudo ./svc.sh uninstall && ./config.sh remove --token <token>)" >&2
  exit 1
fi
free_gb="$(df -BG --output=avail "$(dirname "$dir")" | tail -n 1 | tr -dc '0-9')"
if [[ "$free_gb" -lt 40 ]]; then
  echo "self-hosted-runner: warning: ${free_gb} GB free under $(dirname "$dir"); the image job needs about 40 GB" >&2
  echo "  (docker builder prune --keep-storage 20GB and docker image prune reclaim the build cache)" >&2
fi
if [[ "$(id -u)" != 1000 ]]; then
  echo "self-hosted-runner: warning: the dev container builds as uid 1000 and this user is uid $(id -u);" >&2
  echo "  the image job then runs 'sudo chown -R 1000:1000 sim bridge robots assets', which needs a" >&2
  echo "  passwordless sudoers rule for that command" >&2
fi

# The runner release, verified.
mkdir -p "$dir"
cd "$dir"
if [[ ! -x ./bin/Runner.Listener ]] || [[ "$(cat .runner-version 2>/dev/null || true)" != "$RUNNER_VERSION" ]]; then
  echo "Downloading actions/runner $RUNNER_VERSION"
  curl -fsSL -o "$tarball" "$url"
  echo "$RUNNER_SHA256  $tarball" | sha256sum -c -
  tar xzf "$tarball"
  rm -f "$tarball"
  echo "$RUNNER_VERSION" > .runner-version
fi

# Registration: the token comes from the API and lives for an hour.
echo "Registering with $repo under the label '$label'"
token="$(gh api -X POST "repos/$repo/actions/runners/registration-token" -q .token)"
./config.sh --unattended --url "https://github.com/$repo" --token "$token" \
  --name "$(hostname)" --labels "$label" --work _work --replace
unset token

# The jobs log in to GHCR with the workflow's token; keep that out of the user's Docker config.
mkdir -p "$dir/.docker"
if ! grep -q '^DOCKER_CONFIG=' .env 2>/dev/null; then
  echo "DOCKER_CONFIG=$dir/.docker" >> .env
fi

# The radio link tests shape traffic with netem on an ifb device.
if ! modules_loaded; then
  sudo modprobe sch_netem ifb
fi
if [[ ! -f "$MODULES_CONF" ]]; then
  printf 'sch_netem\nifb\n' | sudo tee "$MODULES_CONF" >/dev/null
fi

# The service runs as this user and starts at boot.
sudo ./svc.sh install "$USER"
sudo ./svc.sh start

# The workflows read the label from the repository variable.
gh variable set "$VARIABLE" --body "$label" --repo "$repo"

echo
echo "Runner '$(hostname)' registered with label '$label'; $VARIABLE=$label."
echo "The heavy jobs of the next workflow run queue on this machine. Check with: $0 --check"
