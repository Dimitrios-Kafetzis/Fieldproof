# Container images

The three images built by task P0-T03, how they are pinned, and how to run the local stack. Nothing
from ROS 2 or Gazebo installs on the host ([ADR-0004](../adr/0004-one-container-per-vehicle.md));
everything below runs in containers driven by `docker/compose.yaml` and the root `Makefile`.

## Quick start

```bash
make images     # build base-ros2, sim and dev (under 15 minutes cold, a few minutes when cached)
make up         # headless Gazebo server (gz sim -s -r empty.sdf) + dev container
make shell      # bash in the dev container, ROS 2 and Gazebo on PATH, repo at /workspace
make down       # stop the stack
make prune      # also remove the project's images and dangling build layers
```

`make test-images` runs `tests/images/test_images.sh`, the verification for this task
(`--quick` reuses the build cache, `--no-build` tests images that are already present, which is
what CI does after building them with `docker buildx bake`).

## Images

| Image | Dockerfile | Contents | Size |
| --- | --- | --- | --- |
| `ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2` | `docker/base-ros2/Dockerfile` | Ubuntu 24.04, ROS 2 Jazzy `ros-base`, `tini`, non-root user `fieldproof` (uid 1000) | 0.9 GB |
| `ghcr.io/dimitrios-kafetzis/fieldproof-sim` | `docker/sim/Dockerfile` | base + `ros-jazzy-ros-gz` (Gazebo Harmonic via the Jazzy vendor packages), `xacro` for the vehicle descriptions, Mesa EGL stack, headless defaults | 3.2 GB |
| `ghcr.io/dimitrios-kafetzis/fieldproof-dev` | `docker/dev/Dockerfile` | sim + build-essential, cmake, ninja, clang/clang-format/clang-tidy/clangd, gdb, colcon with mixins, rosdep, vcstool, uv, passwordless sudo | 4.1 GB |
| `ghcr.io/dimitrios-kafetzis/fieldproof-apt-lists` | `docker/base-ros2/Dockerfile`, stage `apt-lists` | only `/var/lib/apt/lists` of the base image, tagged `<APT_SNAPSHOT>-<ROS_SNAPSHOT>`; seeds later builds (see [Package lists](#package-lists-from-ghcr)) | 46 MB |

Local builds are tagged `:local`. CI pushes `sha-<short>`, `main` and `vX.Y.Z` tags after a
syft SBOM and a Trivy scan ([ci.md](ci.md#published-images)). Override the name or tag with
`FIELDPROOF_IMAGE_PREFIX` and `FIELDPROOF_IMAGE_TAG`; `FIELDPROOF_IMAGE_TAG=main make up` runs
the published images instead of local builds.

Each image runs as the non-root user `fieldproof`, starts through `tini` and an entry point that
sources `/opt/ros/jazzy/setup.bash`, and carries OCI labels (`org.opencontainers.image.source`,
`revision` = git SHA, `created`, `licenses`, `title`, `version`, `base.name`/`base.digest`) plus
`io.fieldproof.apt-snapshot` and `io.fieldproof.ros-snapshot`. `make images` fills the revision and
date; `docker buildx build` without arguments leaves them at `unknown`.

### Gazebo Harmonic through the ROS vendor packages

ROS 2 Jazzy ships Gazebo Harmonic as `ros-jazzy-gz-*-vendor` packages, which `ros-jazzy-ros-gz`
pulls in. The simulator therefore comes from the same snapshot-pinned ROS repository as ROS itself
and no third apt source (`packages.osrfoundation.org`) is needed. The `gz` CLI, the simulation
systems and the bundled worlds (`empty.sdf` and friends) land under `/opt/ros/jazzy/opt/` and are
put on `PATH` and `GZ_SIM_RESOURCE_PATH` by the ROS environment.

## Pinning

Every apt source inside the images is a dated snapshot, so a rebuild months later resolves the same
package versions:

| Pin | Where | Value |
| --- | --- | --- |
| Ubuntu base image digest | `UBUNTU_DIGEST` in `docker/base-ros2/Dockerfile` | `sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55` (`ubuntu:24.04`, 2026-09-18) |
| Ubuntu archive snapshot | `APT_SNAPSHOT` | `20261010T000000Z` at `https://snapshot.ubuntu.com/ubuntu/<id>` (main, updates, backports, security) |
| ROS 2 apt snapshot | `ROS_SNAPSHOT` | `2026-10-05` at `http://snapshots.ros.org/jazzy/<date>/ubuntu` |
| ROS snapshot signing key | `docker/base-ros2/ros-snapshot-keyring.gpg` | ROS Snapshot builder, fingerprint `4B63 CF8F DE49 746E 98FA 01DD AD19 BAB3 CBF1 25EA` |
| uv | `UV_IMAGE` in `docker/dev/Dockerfile` | `ghcr.io/astral-sh/uv:0.13.1` by digest, same version as the host |
| colcon mixins | `COLCON_MIXIN_COMMIT` in `docker/dev/Dockerfile` | commit of `colcon/colcon-mixin-repository` |
| Lint containers | `tests/images/test_images.sh` | `hadolint/hadolint:v2.12.0`, `koalaman/shellcheck:v0.10.0` by digest |
| Scanners in CI | `.github/workflows/images.yml`, `tests/ci/test_workflows.sh` | Trivy `v0.76.0` (policy in `docker/trivy-policy.rego`), syft through `anchore/sbom-action` |

The only packages fetched from the live Ubuntu archive are `ca-certificates` and its dependencies,
installed before the sources are switched to the HTTPS snapshot host (the base image ships without
a CA bundle, so this bootstrap step cannot itself use the snapshot).

The ROS snapshot is signed by a dedicated key, not the `packages.ros.org` key from `ros.key`. The
public key is vendored as a minimal keyring and the test checks its fingerprint. It expires on
2027-06-01; refresh it from `keyserver.ubuntu.com` (key `AD19BAB3CBF125EA`) when that date nears.

The Ubuntu snapshot host is a single origin that answers each request with seconds of latency,
and apt fetches from one host serially, so installing the ~800 packages of the sim image would
take far longer than the 15-minute cold-build budget. `fieldproof-apt-install` (from
`docker/base-ros2/apt-install.sh`) therefore resolves versions from the snapshot index as usual,
asks apt for the file URIs, and downloads them with 16 parallel connections through `apt-helper`:
Ubuntu pool files are tried on the regular archive mirror first (`FIELDPROOF_APT_MIRROR`, default
`http://archive.ubuntu.com/ubuntu`, which carries the same files under the same paths for as long
as the version is current) and fall back to the snapshot host with retries. The real
`apt-get install` then verifies every cached file against the signed snapshot index before
unpacking, so the mirror cannot change what gets installed. The package lists fetched from the
snapshots are kept in the base image (about 30 MB): they describe immutable snapshots, so the sim
and dev images, and any vehicle image built on the base, install from them without another index
refresh from the slow host; `fieldproof-apt-install` only runs `apt-get update` when a configured
source has no lists yet. `FIELDPROOF_APT_PARALLEL` and `FIELDPROOF_APT_MIRROR` are environment
variables read by the script, so pass them as `ENV` or `ARG` lines in a derived Dockerfile.

### Package lists from GHCR

The one request the mitigations above cannot avoid is the index refresh itself (about 30 MB in
eight requests to the slow snapshot host, with the odd 500 to retry). Since the snapshots never
change, the lists are fetched once and published: the `apt-lists` stage of the base Dockerfile is
a scratch image holding `/var/lib/apt/lists`, pushed by CI as
`ghcr.io/dimitrios-kafetzis/fieldproof-apt-lists:<APT_SNAPSHOT>-<ROS_SNAPSHOT>` on every push to
`main`. At build time `docker/apt-lists-context.sh` looks for that image (locally first, then on
GHCR) and hands it to the base build as the `fieldproof-apt-lists` context; the Dockerfile copies
it into `/var/lib/apt/lists` before the first install, so `fieldproof-apt-install` finds lists
for every configured source and skips `apt-get update`. When the image does not exist (a new
snapshot id, no network, or `FIELDPROOF_APT_LISTS=off`), the context is the empty directory
`docker/base-ros2/apt-lists-empty` and the refresh from the snapshot hosts happens as before.
The tag carries both snapshot ids, and the helper only skips the refresh when lists for every
configured source are present, so lists from another snapshot can never be used by mistake.
Changing a snapshot pin therefore costs one slow refresh on the first build, after which CI
publishes the new lists image.

Downloaded `.deb` files live in a BuildKit cache mount, so a rebuild after changing a Dockerfile
line does not re-download the ROS and Gazebo packages. `NO_CACHE=1 make images` ignores the layer
cache and uses a fresh cache-mount namespace, which is what a new machine sees. CI keeps a layer
cache in GHCR instead ([ci.md](ci.md#image-build-and-registry-cache)).

## Rendering

The sim image renders sensors headless with Gazebo's `ogre2` engine over EGL (`EGL_PLATFORM=surfaceless`,
`QT_QPA_PLATFORM=offscreen`, `gz sim --headless-rendering`). Two modes:

- **GPU through `/dev/dri`.** The container gets the host render node (`devices: /dev/dri`) and the
  host's `render` group id (`group_add`). Mesa picks the hardware driver (radeonsi on the reference
  workstation). `docker/compose.gpu.yaml` adds both; the Makefile includes it automatically when
  `/dev/dri/renderD*` exists and exports `FIELDPROOF_RENDER_GID` from `getent group render`.
- **Software.** `LIBGL_ALWAYS_SOFTWARE=1` forces Mesa's llvmpipe. This is the default in
  `docker/compose.yaml`, what CI uses, and what `RENDER=software make up` selects on a GPU host.

`fieldproof-render-info` inside the sim or dev container prints the render nodes, the
`LIBGL_ALWAYS_SOFTWARE` setting and the EGL devices Mesa sees. Ogre writes the renderer it
initialised to `~/.gz/rendering/ogre2.log` (`GL_RENDERER` line).

## Compose profiles and networking

| Profile | Service | Purpose |
| --- | --- | --- |
| `sim` | `sim` | `gz sim -s -r --headless-rendering ${FIELDPROOF_WORLD:-empty.sdf}` |
| `dev` | `dev` | development container, repository bind-mounted at `/workspace`, `sleep infinity` |
| `images` | `base-ros2`, `apt-lists` | build-only services (the base image the other two build on, and its package lists); never started |

`make up` enables `sim` and `dev`. The services share the `fieldproof` bridge network; Gazebo
transport and DDS discovery use multicast on it, and `GZ_PARTITION=fieldproof` is set in every
container so Gazebo topics are visible across containers regardless of hostname. `ROS_DOMAIN_ID`
defaults to 0 and can be overridden from the environment.

Inside the dev container the ROS environment is sourced for interactive shells (`.bashrc`) and for
login shells (`/etc/profile.d/10-ros2.sh`), so both of these work:

```bash
docker compose -f docker/compose.yaml exec dev bash            # interactive
docker compose -f docker/compose.yaml exec -T dev bash -lc 'ros2 topic list'
```

## Dev container

`.devcontainer/devcontainer.json` opens the repository in the `dev` service with VS Code's
Dev Containers extension and also starts `sim`. It uses `docker/compose.yaml` alone, so rendering is
in software; to use the GPU add `"../docker/compose.gpu.yaml"` to `dockerComposeFile` and set
`FIELDPROOF_RENDER_GID` in your environment (`docker/.env` is git-ignored and a good place for it).

`rosdep` is initialised in the image but not updated, because `rosdep update` fetches the live
rosdistro index. Run it in the container when you need rosdep key resolution.

## Updating a pin

`make pins` (`docker/pins-check.sh`, run every Monday by the `Pins` workflow) reports which pins are
behind their upstream; Dependabot does not cover them ([ci.md](ci.md#dependabot)).

1. Pick the new value: `docker buildx imagetools inspect ubuntu:24.04` for the digest; the Ubuntu
   snapshot id is any `YYYYMMDDTHHMMSSZ` timestamp; ROS snapshot dates are listed at
   `http://snapshots.ros.org/jazzy/`.
2. Change the `ARG` default in the Dockerfile and the table above in the same commit.
3. `make images` then `make test-images`. The Trivy scan in CI fails on a fixed CRITICAL
   vulnerability, which is the usual reason to move a snapshot forward. The first build after a
   snapshot change refreshes the lists from the snapshot hosts; once the change is on `main`, CI
   publishes the matching `fieldproof-apt-lists` image and later builds skip the refresh.

## Troubleshooting

**`make up` fails with `error gathering device information while adding custom device "/dev/dri"`.**
The host has no render node but `RENDER=gpu` was forced. Use `RENDER=software` or let the Makefile
detect it.

**Camera topics never publish, `gz-sim.log` shows `Unable to create the rendering window` or EGL
errors.** With `/dev/dri` mapped, the container user must be in the host's `render` group:
check `FIELDPROOF_RENDER_GID` matches `getent group render` on the host. Without a usable device
Mesa still has the software path, so setting `LIBGL_ALWAYS_SOFTWARE=1` isolates GPU problems.

**`gz topic -l` in the dev container is empty while the sim is running.** Both containers must
share the `fieldproof` network and `GZ_PARTITION`. `docker compose ps` shows whether `sim` is up;
`docker compose logs sim` shows the server output.

**Build cannot reach `snapshot.ubuntu.com` or `snapshots.ros.org`.** Both are single-origin
services without mirrors; retry (apt is configured with three retries) or check the proxy. A
corporate proxy that rewrites HTTPS breaks the snapshot host's certificate check.
