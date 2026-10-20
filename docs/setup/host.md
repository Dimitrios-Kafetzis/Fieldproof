# Host setup

How to prepare an Ubuntu 24.04 workstation for Fieldproof development (task P0-T02). The host
carries only what cannot run in a container: Docker itself, Node for the cockpit tooling, a Python
tool runner and editor tooling. ROS 2 and Gazebo never install on the host ([ADR-0004](../adr/0004-one-container-per-vehicle.md)).

## Quick start

```bash
git clone https://github.com/Dimitrios-Kafetzis/Fieldproof.git
cd Fieldproof
tools/host-setup.sh          # asks for sudo once, then installs the user-level tools
# log out and back in so the docker group becomes active, then
tools/host-check.sh          # verifies every prerequisite and prints versions
```

`tools/host-setup.sh` is idempotent: run it again after a reboot or a change of pins and it repairs
only what drifted. `tools/host-check.sh` is read-only and exits non-zero when anything is missing;
`tools/host-check.sh --quick` skips the two container smoke tests.

If `gh` is not authenticated yet, run `gh auth login --web --git-protocol https` once.

## What gets installed

| Component | Version pinned in `tools/host-setup.sh` | Source | Why |
| --- | --- | --- | --- |
| Docker Engine, CLI | 5:29.9.0-1~ubuntu.24.04~noble | Docker apt repository (key fingerprint verified) | Runs every simulator, bridge and vehicle container |
| containerd.io | 2.4.2-1~ubuntu.24.04~noble | Docker apt repository | Container runtime |
| Docker Buildx plugin | 0.38.0-1~ubuntu.24.04~noble | Docker apt repository | BuildKit builds, multi-platform images, registry cache |
| Docker Compose plugin | 5.6.0-1~ubuntu.24.04~noble | Docker apt repository | Local stacks in `docker/compose.yaml` (P0-T03) |
| qemu-user-static, binfmt-support | 1:8.2.2+ds-0ubuntu1.18, 2.2.2-7 | Ubuntu noble | Runs `linux/arm64` containers for target emulation (P6-T01) |
| GitHub CLI | 2.45.0-1ubuntu0.3 | Ubuntu noble | PRs, issues and releases from the terminal |
| nvm | v0.40.8 | Git clone of nvm-sh/nvm at the tag | Node version management without touching apt's Node |
| Node | 22.23.3 (LTS "Jod") | nvm | Cockpit toolchain (Vite, React, Three.js) |
| pnpm | 12.11.2 | corepack (bundled with Node 22) | Cockpit workspace package manager |
| uv | 0.13.1 | GitHub release tarball, checksum verified | Python environments and tools for the engine |
| pre-commit | 4.7.0 | `uv tool install` | Quality gates (P0-T04) |

User-level tools land in `~/.nvm` and `~/.local/bin`; the setup script appends an nvm block to
`~/.bashrc` once. The repository's `.nvmrc` selects Node 22, so `nvm use` in the repo picks the right
version.

Nothing from ROS, Gazebo, colcon or any robot stack is installed on the host, and the host-check
does not look for them.

## Verification

`tools/host-check.sh` checks, in order: Ubuntu 24.04; Docker CLI, group membership, daemon,
Compose and Buildx; `qemu-user-static` and the `qemu-aarch64` binfmt entry with the `F` flag;
nvm, Node 22 from nvm, corepack and pnpm; uv and pre-commit; `gh` and its authentication; a
`/dev/dri/renderD*` node; and finally the two smoke tests from the task's definition of done:

```bash
docker run --rm --device /dev/dri ubuntu:24.04 ls /dev/dri            # lists renderD128
docker run --rm --platform linux/arm64 ubuntu:24.04 uname -m          # prints aarch64
```

The script tests live in `tests/host/test_host_check.sh`. They lint both scripts with shellcheck
(local binary or the pinned `koalaman/shellcheck:v0.10.0` container), exercise the flags, run the
check against an empty toolchain to confirm every missing prerequisite is reported, and run the real
check twice to confirm it is idempotent. Set `FIELDPROOF_SKIP_HOST=1` to skip the last step on a
machine that is not set up.

## Troubleshooting

**`permission denied while trying to connect to the Docker daemon socket`.** Your user was added to
the `docker` group but the current session predates that. Log out and back in, or prefix commands
with `sg docker -c '…'` meanwhile. `tools/host-check.sh` detects this state and uses `sg` itself.

**`exec format error` when running an arm64 container.** The `qemu-aarch64` binfmt entry is missing
or lacks the `F` (fix-binary) flag, which is what lets the interpreter work inside a container's
mount namespace. Re-run `tools/host-setup.sh`; it re-imports the registration. Inspect with
`cat /proc/sys/fs/binfmt_misc/qemu-aarch64` (expect `enabled` and `flags: POF`).

**No `/dev/dri/renderD*` node.** Headless GPU rendering in containers needs a Mesa-capable GPU with
a render node. Without one, simulation still runs with software rendering
(`LIBGL_ALWAYS_SOFTWARE=1`, see P0-T03) but camera-heavy scenarios will be slow.

**Running the setup from an editor task or another process without a terminal.** `sudo` needs a
password prompt. Either run `tools/host-setup.sh` from a terminal, or set `SUDO_ASKPASS` to a helper
that shows a dialog; the script then calls `sudo -A`. When the editor is a snap (VS Code from the
Snap Store), GTK helpers such as `zenity` crash because the snap injects its own `GSETTINGS_SCHEMA_DIR`
and `GTK_PATH`; wrap the helper in `env -i` with only `DISPLAY`, `WAYLAND_DISPLAY`,
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` set.

**`Specified qdisc kind is unknown` or `Cannot find device "ifb-radio"` in a run with radio
links.** The `network` phase shapes each vehicle's link with `tc netem` and an `ifb` device
([network.md](../reference/network.md#how-a-profile-is-applied)); Ubuntu loads `sch_netem` and
`ifb` on demand when a container with `NET_ADMIN` asks for them, a hardened or minimal kernel
may not. `sudo modprobe sch_netem ifb` loads them (`lsmod | grep -E 'netem|ifb'` shows them);
`echo -e 'sch_netem\nifb' | sudo tee /etc/modules-load.d/fieldproof.conf` keeps them across
reboots.

**Disk space.** Images for simulation, ROS 2 and development will take several gigabytes under
`/var/lib/docker`. Keep at least 30 GB free before P0-T03; `docker system df` shows usage and
`docker system prune` reclaims build cache.

## Updating a pin

Change the version at the top of `tools/host-setup.sh`, update the table above in the same commit,
re-run the setup, then run `tests/host/test_host_check.sh`. For apt packages, list candidates with
`apt-cache madison docker-ce` (after the Docker repository is configured) or `apt-cache policy <pkg>`.
