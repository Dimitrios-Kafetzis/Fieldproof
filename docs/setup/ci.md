# CI/CD pipeline

Task P0-T05 runs the quality gates and tests of [tooling.md](tooling.md) and
[containers.md](containers.md) on GitHub Actions, publishes the container images to GHCR, and cuts
releases with release-please ([ADR-0008](../adr/0008-monorepo-apache2-github.md)). A change that
passes `make lint` and `make test` locally passes in CI, because the workflows call the same
Makefile targets.

## Workflows

| Workflow | File | Runs on | Does |
| --- | --- | --- | --- |
| CI | `.github/workflows/ci.yml` | every pull request, push to `main`, manual | `make lint` (all hooks, commit messages, secret scan), `make test-tooling`, `make test-ci`, `make test-layout`, `make test-docs`, `make test-engine`, `make test-cockpit` (plus the Vite build), `make test-host`, and when the change touches the images or the ROS workspaces: build `base-ros2`, `sim`, `dev` from the GHCR layer cache, run `make test-images`, `make test-ros`, `make tidy`, `make test-worlds` and `make test-vehicles` (the example vehicle image built on `base-ros2`) against them |
| PR title | `.github/workflows/pr-title.yml` | pull request opened, edited, synchronised | the title must be a Conventional Commit (`make lint-title`), see [Pull request titles](#pull-request-titles) |
| Images | `.github/workflows/images.yml` | push to `main`, called by Release, manual | build and push `sha-<short>` tags, syft SBOM and Trivy scan per image, then tag `main` (or `vX.Y.Z`) |
| Release | `.github/workflows/release.yml` | push to `main` | release-please keeps the release pull request current; when it merges, tags `vX.Y.Z`, creates the GitHub release and calls Images |
| Docs | `.github/workflows/docs.yml` | pull requests and pushes to `main` that touch `docs/`, `mkdocs.yml` or `tools/docs/`; manual | `make docs` and `make docs-links` (external links included), upload the site as a Pages artifact; on `main`, deploy it to GitHub Pages ([docs.md](docs.md#continuous-integration-and-deployment)) |
| Pins | `.github/workflows/pins.yml` | Mondays, manual | `docker/pins-check.sh`: fails when the pinned `ubuntu:24.04` digest, ROS snapshot or uv release is behind its upstream |

Every action is pinned to a commit SHA with the version in a trailing comment, which Dependabot
keeps in sync; the repository setting *require actions to be pinned to a full-length commit SHA*
is on. Each job declares its permissions (`contents: read` unless it pushes images, uploads scan
results or opens the release pull request) and a timeout.

### Required status checks

Branch protection on `main` requires two checks, both wired by `make branch-protection`
(`tools/ci/branch-protection.sh`, run once by an admin and again whenever the gate job names
change; `--check` reports drift):

- **CI** — the last job of `ci.yml`. It fails when any other job failed or was cancelled and passes
  when the image job was skipped by the path filter, so a skipped build never blocks a merge and a
  failed lint always does.
- **PR title** — the commitlint check of `pr-title.yml`.

Pull requests opened with the default `GITHUB_TOKEN` (release-please without a token of its own,
see [Releases](#releases)) trigger no workflows and therefore show no checks.

### Path filter

`tools/ci/changed-areas.sh` compares the pull request against its base and prints `images=true`
when `docker/`, `tests/images/`, the `Makefile`, `version.txt`, `tools/ci/` or `ci.yml` changed,
or when a ROS workspace did (`sim/`, `bridge/`, `robots/`, `tools/ros/`, `.clang-tidy`), because
the colcon tests and clang-tidy run in the dev container that this job builds
([workspaces.md](workspaces.md#continuous-integration)). Pushes to `main` and manual runs build
the images unconditionally. The other jobs always run; they finish in a few minutes thanks to the
cached pre-commit environments, the uv cache (keyed on `engine/uv.lock`) and the pnpm store cache
(keyed on `cockpit/pnpm-lock.yaml`).

### Image build and registry cache

The image job builds the three targets of `docker/compose.yaml` with `docker buildx bake`
(pinned to v0.38.0, run from `docker/` because bake resolves build contexts relative to its
working directory, with every compose profile enabled so the build-only `base-ros2` service is
not filtered out), which reads the compose file directly (the `service:` build contexts become
`target:` contexts), and loads the result into the runner's Docker (with Compose pinned to the
host's v5.6.0) so `tests/images/test_images.sh --quick --no-build` can run `make up`, the topic
checks and the software-rendering camera check against them.
`tools/ci/bake-cache.sh` adds a registry layer cache to every target:

| Cache tag | Written by | Read by |
| --- | --- | --- |
| `ghcr.io/dimitrios-kafetzis/fieldproof-<name>:buildcache` | Images (push to `main`, releases) | CI, Images |
| `ghcr.io/dimitrios-kafetzis/fieldproof-<name>:buildcache-pr` | CI on pull requests from this repository | CI, Images |

Pull requests from forks and from Dependabot have a read-only token, so they read both caches and
write neither. A cold build on a runner takes 10 to 20 minutes depending on the snapshot hosts
(see [containers.md](containers.md)); with a warm cache the job spends most of its time loading
the images and running the tests. `tools/ci/free-disk.sh` removes unused runner toolchains
first, because the images and their cache need about 12 GB.

## Published images

| Tag | Meaning |
| --- | --- |
| `sha-<short>` | every push to `main` and every release, 7-character commit prefix; the only tag that can exist before the scan passed |
| `main` | the last push to `main` whose images passed the scan |
| `vX.Y.Z` | the release of that version, pointing at the same digest as its `sha-` tag |

Images are `ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2`, `-sim` and `-dev`. The same push
also publishes `fieldproof-apt-lists:<APT_SNAPSHOT>-<ROS_SNAPSHOT>`, the base image's package
lists, which every later build (CI and local) seeds from instead of the slow Ubuntu snapshot host
([containers.md](containers.md#package-lists-from-ghcr), issue #4); it is not scanned or
retagged because it contains no packages and its tag is the immutable snapshot pair. Pull one with
`FIELDPROOF_IMAGE_TAG=main make up` instead of building locally. The `main` and `vX.Y.Z` tags
are created with `docker buildx imagetools create`, so they share the digest of the scanned
`sha-` image; nothing is rebuilt between scan and tag.

### SBOM and vulnerability scan

For each image the `scan` job pulls the `sha-` tag, writes an SPDX JSON SBOM with syft (workflow
artifact `sbom-fieldproof-<name>.spdx.json`, attached to the GitHub release on `vX.Y.Z`), uploads
a Trivy report of fixed HIGH and CRITICAL vulnerabilities to the repository's code scanning tab,
and then fails on any fixed CRITICAL vulnerability. Unfixed advisories are ignored because the
snapshot-pinned images cannot pick up a fix that does not exist. `docker/trivy-policy.rego`
excludes `linux-libc-dev`: its advisories describe kernel bugs and no kernel code from the header
package runs in a container. The policy is the only exemption; a failing scan means it is time to
move `APT_SNAPSHOT` and `ROS_SNAPSHOT` in `docker/base-ros2/Dockerfile` forward.

## Releases

release-please reads the Conventional Commits on `main` since the last tag and maintains a
release pull request titled `chore(release): release X.Y.Z` that updates `version.txt`,
`CHANGELOG.md` and every other version file of the monorepo (the `extra-files` of
`release-please-config.json`: the cockpit packages and the ROS packages; the engine reads
`version.txt` itself, see [workspaces.md](workspaces.md#one-version-everywhere)). The project
is below 1.0: the first release is `0.1.0` (`initial-version`, because release-please ignores the
manifest for a first release and would otherwise start at 1.0.0), `feat` bumps the minor version,
`fix` the patch, and a breaking change (`!`) bumps the minor version too (`bump-minor-pre-major`;
`bump-patch-for-minor-pre-major` stays unset, because it would turn every feature into a patch
bump below 1.0). Merging the release pull request creates the `vX.Y.Z` tag and the
GitHub release; `release.yml` then calls `images.yml` for that tag, which builds from the cache,
scans, tags the images `vX.Y.Z` and attaches the SBOMs to the release. The Makefile reads
`version.txt` into the `VERSION` build argument, so the OCI `version` label of a release image
matches its tag.

Because squash merges carry the pull request title, the title is what release-please sees; see
the next section. Only `feat`, `fix`, `perf`, `revert`, `docs` and `build` commits appear in the
changelog (`ci`, `test`, `refactor` and `chore` are hidden).

**Token.** A release pull request opened with the default `GITHUB_TOKEN` triggers no workflows,
so it has no status checks and cannot be merged under branch protection. Add a fine-grained
personal access token for this repository (permissions: contents and pull requests, read and
write) as the repository secret `RELEASE_PLEASE_TOKEN`; `release.yml` uses it when present and
falls back to `GITHUB_TOKEN` otherwise. Tags and releases created with the token also trigger
workflows normally, which `release.yml` does not rely on (it calls `images.yml` directly).

## Pull request titles

The repository squash-merges, and GitHub uses the pull request title as the commit subject on
`main`. That subject feeds release-please, so a title must be a Conventional Commit exactly like
a commit message (`type(scope): summary`, scopes from `commitlint.config.mjs`); the task ID goes
in the body (`Task: P0-T05`), as it does in every commit. `pr-title.yml` runs commitlint on the
title with the repository's configuration and reruns when the title is edited. Locally:

```bash
make lint-title TITLE="feat(engine): add scenario loader"
```

Earlier squash commits of the form `P0-T02 Host environment setup (#2)` predate this rule;
commitlint keeps exempting that exact shape so `make lint-commits` stays clean on old history.

## Dependabot

`.github/dependabot.yml` opens one grouped pull request per ecosystem every Monday, titled
`build(deps): ...`: GitHub Actions (SHA pins and version comments), pip in `tools/docs/` (the
documentation toolchain, `uv.lock`) and `engine/` (`uv.lock`), and npm in `cockpit/`
(`pnpm-lock.yaml`). A bump of an engine dependency must also update the mypy hook in
`.pre-commit-config.yaml`, and a TypeScript bump the eslint hook; `make test-layout` says which.
Hook pins themselves are updated by hand with `pre-commit autoupdate`
([tooling.md](tooling.md#updating-the-hooks)).

The container images are not covered by Dependabot (issue #9): every base image is referenced
through a build argument (`FROM ubuntu:24.04@${UBUNTU_DIGEST}`, `FROM ${UV_IMAGE}`) and the apt
sources are dated snapshots, which its docker updater cannot bump, and its Docker Hub lookups
fail unauthenticated. The `Pins` workflow is the replacement: every Monday
`docker/pins-check.sh` (`make pins` locally) compares the pinned `ubuntu:24.04` digest, ROS
snapshot date and uv release with the current upstream values, writes a table to the job summary
and fails when one has moved. A red run is the cue to update the pins
([containers.md](containers.md#updating-a-pin)); the Ubuntu snapshot id is reported with its age
but never fails the check, because any timestamp is a valid snapshot and it moves with the ROS one.

## Running the CI checks locally

| Command | Mirrors |
| --- | --- |
| `make lint` | the Lint job |
| `make test-tooling`, `make test-ci`, `make test-layout`, `make test-docs`, `make test-host` | the tooling, docs and host jobs |
| `make test-engine`, `make test-cockpit` | the engine and cockpit jobs |
| `make test-ros`, `make tidy` | the ROS workspace steps of the image job (needs the images) |
| `make docs-links` | the link check of the Docs workflow |
| `make test-images IMAGE_TEST_FLAGS="--quick --no-build"` | the image job's tests against images already built |
| `make test-vehicles` | the image job's vehicle step: manifests, the generated Dockerfile, the example vehicle image built on the local `base-ros2` and started on a Docker network ([manifest.md](../reference/manifest.md)) |
| `make lint-title TITLE="..."` | the PR title check |
| `make branch-protection` / `tools/ci/branch-protection.sh --check` | the required checks wiring |

`tests/ci/test_workflows.sh` (`make test-ci`) checks the workflows themselves: actionlint and the
schema hooks pass, every action is SHA-pinned with a version comment, permissions, concurrency
and timeouts are declared, the required check names exist, the gate job depends on every other
job, the layer test jobs call their Makefile targets, the path filter and cache helpers behave in
a scratch repository, the release-please and Dependabot configurations agree with `version.txt`
and commitlint and watch existing directories, `docker buildx bake` reads the compose file, and
(when the base image is built locally) the Trivy policy passes.

## Troubleshooting

**The image job cannot pull the cache (`buildcache` not found).** Expected on the first run and
after the GHCR packages are deleted; BuildKit falls back to a cold build and the next push to
`main` repopulates the cache.

**GHCR packages are private after the first push.** Packages created by a workflow start private.
In the package settings on GitHub (Packages → `fieldproof-base-ros2`, `-sim`, `-dev`) set the
visibility to public and confirm the repository is linked; `make up` with
`FIELDPROOF_IMAGE_TAG=main` on another machine then works without a login.

**Trivy fails the scan.** Read the table in the job log: a fixed CRITICAL vulnerability in a
package other than `linux-libc-dev` means the snapshots are stale. Move the pins forward as
described in [containers.md](containers.md#updating-a-pin). Do not add CVE-specific ignores.

**The release pull request shows no checks.** See [Releases](#releases): add the
`RELEASE_PLEASE_TOKEN` secret, then close and reopen the pull request once so it is refreshed
with the token.

**`make lint-title` says the pre-commit configuration is unstaged.** pre-commit refuses to run
while `.pre-commit-config.yaml` has unstaged changes; stage the file first.
