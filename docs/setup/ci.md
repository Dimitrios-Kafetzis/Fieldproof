# CI/CD pipeline

Task P0-T05 runs the quality gates and tests of [tooling.md](tooling.md) and
[containers.md](containers.md) on GitHub Actions, publishes the container images to GHCR, and cuts
releases with release-please ([ADR-0008](../adr/0008-monorepo-apache2-github.md)). A change that
passes `make lint` and `make test` locally passes in CI, because the workflows call the same
Makefile targets.

## Workflows

| Workflow | File | Runs on | Does |
| --- | --- | --- | --- |
| CI | `.github/workflows/ci.yml` | every pull request, push to `main`, manual | `make lint` (all hooks, commit messages, secret scan), `make test-tooling`, `make test-ci`, `make test-layout`, `make test-docs`, `make test-engine`, `make test-cockpit` (plus the Vite build), `make test-host`, and when the change touches the images or the ROS workspaces: build `base-ros2`, `sim`, `dev` from the GHCR layer cache, run `make test-images`, `make test-ros`, `make tidy`, `make test-worlds`, `make test-vehicles` (the example vehicle image built on `base-ros2`) and `make test-integration` against them |
| Scenario tests | `.github/workflows/scenario-tests.yml` | push to `main`, nightly, manual (an image tag and a scenario), pull requests that touch the workflow, `tools/ci/scenario-run.sh`, `examples/`, `engine/`, `fieldproof.toml` or the `Makefile` | pull the published `base-ros2`, `sim` and `bridge` images, build the example vehicle image on them, fetch the Fuel models, run the UGV scenario headless with software rendering under the flaky-run policy (one retry, flagged), upload the run (MCAP, reports, JUnit, logs) as artefacts; a second job (not on pull requests) runs the lockstep scenario twice and compares the runs (the determinism test of P2-T04); see [Scenario tests](#scenario-tests) |
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
when a ROS workspace did (`sim/`, `bridge/`, `robots/`, `tools/ros/`, `.clang-tidy`), because
the colcon tests and clang-tidy run in the dev container that this job builds
([workspaces.md](workspaces.md#continuous-integration)), or when the assets, the worlds, the
example vehicles, the engine's vehicle and scenario modules, the example scenarios,
`fieldproof.toml`, `tests/vehicles/` or `tests/integration/` did.
Pushes to `main` and manual runs build the images unconditionally. The other jobs always run;
they finish in a few minutes thanks to the cached pre-commit environments, the uv cache (keyed on
`engine/uv.lock`) and the pnpm store cache (keyed on `cockpit/pnpm-lock.yaml`).

### Image build and registry cache

The image job builds the four targets of `docker/compose.yaml` with `docker buildx bake`
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

## Scenario tests

Task P1-T07's proof that the chain is reproducible away from the maintainer's machine:
`.github/workflows/scenario-tests.yml` runs `examples/scenarios/ugv_nav_to_goal.yaml` on a
GitHub-hosted `ubuntu-24.04` runner (two cores, no GPU) the way a fresh clone does, from the
registry rather than from a build:

1. `tools/ci/free-disk.sh`, the Fuel archive cache and uv, as in the image job;
2. `docker pull` of the published `base-ros2`, `sim` and `bridge` images at
   `FIELDPROOF_IMAGE_TAG` (`main` by default: the last push to `main` whose images passed the
   scan; a manual run can name `vX.Y.Z` or `sha-<short>`);
3. `make assets` and `make vehicle-image`: the example vehicle image built on the published base
   image (a few minutes, Nav2 from the apt snapshots);
4. `fieldproof validate`, then `tools/ci/scenario-run.sh --runs-dir runs --render software` on
   the scenario (a manual run can name another file);
5. the run directory (`runs/`: every attempt's `run.json`, `engine.log`, the containers' logs,
   `report.md`, `report.json`, `junit.xml` and `recording/*.mcap`; the example records no
   video, see [scenarios.md](../reference/scenarios.md#video)) as the artefact
   `scenario-run-<run number>-<attempt>` and the final attempt's JUnit document as
   `scenario-junit-<run number>-<attempt>`, uploaded whatever the outcome and kept 14 days;
   then a check that no container with the `io.fieldproof.run` label is left on the runner.

The fleet example (`examples/scenarios/ugv_fleet.yaml`, task P2-T01) is not run here: three
Nav2 stacks and a simulator rendering three lidars exceed the runner's cores and its readiness
gates would time out, so the fleet is verified by `tests/integration/test_fleet.py` on a
workstation (eight cores or more; CI's image job runs the test too and it skips itself on the
runner). A manual dispatch may still name it as the scenario input on a larger runner.

The radio-link example (`examples/scenarios/ugv_link_loss.yaml`, task P2-T02) is run by the
image job's integration tests (`tests/integration/test_radio_links.py`, after the job loads the
`sch_netem` and `ifb` modules), against the images that job builds, since the published bridge
image only carries `tc` from the release after the task; a manual dispatch here can name it once
`main` has it.

**Flaky-run policy.** `tools/ci/scenario-run.sh` runs the scenario once and, when the attempt
exits with anything but 0, 130 (interrupted) or 2 (usage error), prints a `::warning::`
annotation and runs it once more (`--retries N` changes the count, `0` disables it); the
attempts are the run ids `scenario-<name>-attempt1` and `-attempt2`. A run that passes on its
second attempt is green but flagged: the annotation `Flaky scenario`, the job summary (*Flaky:
passed on attempt 2 of 2*) and the step output `flaky=true`; both attempts' run directories are
in the artefact, so the first attempt's logs can be read (the lifecycle stall of issue #32 is
the known cause). The outcome of the step is the last attempt's: its exit code, its
`junit.xml`, `report.md` and `report.json` copied to the top of `runs/`, and the outputs
`run_id`, `run_dir`, `verdict`, `exit_code` and `attempts`. The job summary carries the final
attempt's report (verdict, assertions with evidence, timeline, phases, artefacts).

**JUnit.** `junit.xml` is written with every run
([assertions.md](../reference/assertions.md#reports)): one test suite per run with one test
case per assertion, per timeline entry and for the run itself, so a JUnit consumer (the
artefact, a test reporter) names the assertion that failed a scenario.

The workflow is not a required status check: a flaky run never blocks a merge, the `CI`
workflow's image job covers a change to the images themselves (it builds them and runs
`make test-integration` against the result), and the milestone's definition of done asks for
three consecutive green runs on `main` instead; the nightly schedule (`20 4 * * *` UTC) adds
one a day, and `gh workflow run scenario-tests.yml --ref main` another on demand (one at a time:
runs on `main` share a concurrency group that queues them without cancelling, and GitHub keeps a
single pending run per group, so a second dispatch while one is still queued cancels the first).
On a workstation `examples/demo_m1.sh` (`make demo`) is the same chain with the GPU
when there is one, and `tools/ci/scenario-run.sh examples/scenarios/ugv_nav_to_goal.yaml` the
same policy.

**Determinism.** The workflow's second job (task P2-T04) runs
`tests/integration/test_determinism.py` against the same published images: the lockstep
scenario (`examples/scenarios/ugv_lockstep.yaml`) twice with its seed, then the two runs'
ground-truth trajectories and odometry compared within 0.25 m
([time.md](../reference/time.md#comparing-two-runs)). Two runs of the stack take too long for
every pull request, so the job runs on pushes to `main`, nightly and on demand, and uploads
both run directories as `determinism-runs-<run number>-<attempt>`. `make test-determinism`
runs the same on a workstation (`PYTEST_FLAGS="--basetemp runs/determinism"` keeps the runs).

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
`build(deps): ...`: GitHub Actions (SHA pins and version comments), uv in `tools/docs/` (the
documentation toolchain) and `engine/` (both update `pyproject.toml` and `uv.lock` together; the
`pip` ecosystem bumped the manifest alone, which `uv sync --locked` then refused), and npm in
`cockpit/` (`pnpm-lock.yaml`). A bump of an engine dependency must also update the mypy hook in
`.pre-commit-config.yaml`, and a TypeScript bump the eslint hook; `make test-layout` says which.
Major versions of `typescript` and `@types/node` are ignored in the npm entry: the cockpit
follows Node 22 and TypeScript stays where `typescript-eslint` supports it, so those bumps are
made by hand together with the hook. Dependabot's commit bodies (release notes and compare links
past 100 characters) are exempt from commitlint's line limit
([tooling.md](tooling.md#commit-messages)). Hook pins themselves are updated by hand with
`pre-commit autoupdate` ([tooling.md](tooling.md#updating-the-hooks)).

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
| `make test-integration` | the image job's last step: the simulator, the bridge container and the vehicle in the depot, `fieldproof vehicle wait` until Nav2 is active, a `NavigateToPose` goal reached ([manifest.md](../reference/manifest.md#the-integration-test)); then `fieldproof run` of the example scenario with its assertions, MCAP recording and report, an interrupted run, and the failing example scenario ([scenarios.md](../reference/scenarios.md#tests), [assertions.md](../reference/assertions.md#tests)); the radio links ([network.md](../reference/network.md#tests)), the events scenario, the world events plugin and the obstacle scenario, the fault injectors and the GNSS-denial scenario ([events.md](../reference/events.md#tests)) |
| `FIELDPROOF_IMAGE_TAG=main tools/ci/scenario-run.sh examples/scenarios/ugv_nav_to_goal.yaml`, `make demo` | the run step of the Scenario tests workflow against the published images, with its flaky-run policy; the demo without it ([Scenario tests](#scenario-tests)) |
| `make lint-title TITLE="..."` | the PR title check |
| `make branch-protection` / `tools/ci/branch-protection.sh --check` | the required checks wiring |

`tests/ci/test_workflows.sh` (`make test-ci`) checks the workflows themselves: actionlint and the
schema hooks pass, every action is SHA-pinned with a version comment, permissions, concurrency
and timeouts are declared, the required check names exist, the gate job depends on every other
job, the layer test jobs call their Makefile targets, the path filter and cache helpers behave in
a scratch repository, the release-please and Dependabot configurations agree with `version.txt`
and commitlint and watch existing directories, `docker buildx bake` reads the compose file,
(when the base image is built locally) the Trivy policy passes, the scenario workflow pulls the
published images and uploads the run whatever the outcome, `tools/ci/scenario-run.sh` applies
the flaky-run policy against a stub `fieldproof`, and `examples/demo_m1.sh` prints the quick
start's commands in a dry run.

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
