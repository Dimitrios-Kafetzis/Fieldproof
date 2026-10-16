# Repository layout and workspaces

Task P0-T07 lays out the monorepo of
[04-engineering-standards.md](../plan/04-engineering-standards.md#repository-layout-monorepo)
([ADR-0008](../adr/0008-monorepo-apache2-github.md)) and gives every layer a buildable, tested
package in its own language ([ADR-0009](../adr/0009-languages-per-layer.md)). `ARCHITECTURE.md`
at the repository root maps the directories to the layers; this page explains how each workspace
is built, tested and versioned.

```text
fieldproof/
├── sim/                     colcon workspace: fieldproof_worlds, fieldproof_gz_plugins (C++17)
├── bridge/                  colcon workspace: fieldproof_bridge (ament_python)
├── robots/                  colcon workspace: fieldproof_robots (descriptions, manifests)
├── engine/                  uv project: Python package `fieldproof`, the CLI
├── cockpit/                 pnpm workspace: apps/cockpit (Vite + React), packages/
├── docker/                  images and the compose stack
├── examples/                scenarios/, vehicles/, plugins/
├── assets/                  vendored assets with LICENSES/, Fuel lock file
├── tools/                   host, ci, ros, docs helpers
├── tests/                   cross-layer suites (tooling, ci, docs, layout, host, images)
└── docs/                    this site
```

## One command per layer

| Command | Toolchain | Runs | Where |
| --- | --- | --- | --- |
| `make test-engine` | uv | `uv sync --locked`, pytest in `engine/` | host |
| `make test-cockpit` | pnpm, Node 22 | `pnpm install --frozen-lockfile`, `tsc --noEmit`, Vitest | host |
| `make test-ros` | colcon, ROS 2 Jazzy, Gazebo Harmonic | `colcon build` and `colcon test` in `sim/`, `bridge/`, `robots/` | dev container |
| `make tidy` | clang-tidy 18 | `run-clang-tidy` over the C++ packages with the compile commands of the last build | dev container |
| `make test` | all of the above plus tooling, CI, docs, layout, host and image tests | | |

The host runs only what [host.md](host.md) installs (Docker, Node with pnpm, uv, pre-commit).
Everything that needs ROS or Gazebo runs in the `dev` container of
[containers.md](containers.md): `make test-ros` starts it (`make images` or
`FIELDPROOF_IMAGE_TAG=main` must have provided the image first) and leaves it running for the
next call; `make down` stops it. `ROS_WORKSPACES="sim"` limits a run to one workspace.

## The engine (`engine/`)

A uv project with a `src/` layout: `engine/pyproject.toml` declares the package `fieldproof`, its
exact dependencies (Pydantic, Typer, Rich) and the `fieldproof` console script;
`engine/uv.lock` pins the resolution. The version is dynamic: hatchling reads it from the
repository's `version.txt`, and uv's `cache-keys` rebuild the editable install when that file
changes. `uv sync --locked --project engine` creates `engine/.venv`,
and `uv run --project engine fieldproof --version` runs the CLI (`python -m fieldproof` works
too). Tests live in `engine/tests/` and run with the `pytest` of the dev dependency group.

Lint and type settings come from the root `pyproject.toml`: ruff and mypy strict run through the
pre-commit hooks on every Python file in the repository. The mypy hook lists the engine's
dependencies in `.pre-commit-config.yaml` at the versions of `uv.lock`, so the hook sees the same
packages the code runs with; `tests/layout/test_layout.sh` fails when they drift. Add a
dependency in three places: `engine/pyproject.toml`, `uv lock --project engine`, and the hook.

## The cockpit (`cockpit/`)

A pnpm workspace (`pnpm-workspace.yaml`: `apps/*`, `packages/*`) whose root `package.json` names
the pnpm release in `packageManager` (the same release `tools/host-setup.sh` enables through
corepack; CI reads it with `pnpm/action-setup`). `apps/cockpit` is the Vite + React + TypeScript
strict application: `pnpm dev` serves it on `127.0.0.1:5173`, `pnpm test` runs Vitest,
`pnpm typecheck` runs `tsc --noEmit`, `pnpm build` produces `dist/`. The version in the app's
`package.json` is injected at build time as `__COCKPIT_VERSION__`. Shared packages go under
`packages/` as `@fieldproof/<name>` from P3-T02 on.

Dependencies are exact versions (`.npmrc` sets `save-exact`), and `pnpm-workspace.yaml` sets a
`minimumReleaseAge` of seven days: pnpm refuses a release younger than that, which keeps a
package hijacked on the registry from landing in the lock file before it is noticed. To add a
dependency: `pnpm --filter @fieldproof/cockpit add <name>@<version>`, then commit
`pnpm-lock.yaml`. The root ESLint and Prettier configuration applies through the pre-commit
hooks, with TypeScript at the same version as the app (checked by the layout test).

## The ROS workspaces (`sim/`, `bridge/`, `robots/`)

Each directory is a colcon workspace whose packages sit at its top level, built with
`tools/ros/colcon.sh` inside the dev container:

| Package | Type | Tests |
| --- | --- | --- |
| `sim/fieldproof_worlds` | `ament_cmake` | `gz sdf --check` on every world in `worlds/`, `ament_xmllint` on `package.xml` |
| `sim/fieldproof_gz_plugins` | `ament_cmake`, C++17 | GoogleTest (`ament_add_gtest`), `ament_xmllint`; `fieldproof::HelloSystem` is the first system plugin |
| `bridge/fieldproof_bridge` | `ament_python` | pytest in `test/` |
| `robots/fieldproof_robots` | `ament_cmake` | `check_urdf` and `gz sdf --check` on the generated descriptions, pytest through `ament_cmake_pytest` on the installed layout and the manifest, a `launch_testing` spawn-and-drive test, `ament_xmllint` ([robots.md](../reference/robots.md)) |

`tools/ros/colcon.sh build|test|tidy [workspace ...]` builds with
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` into `<workspace>/build`, `install` and `log` (ignored by
git), runs `colcon test` with `--return-code-on-test-failure` and prints
`colcon test-result --verbose`. `tidy` runs `run-clang-tidy` with the repository `.clang-tidy`
(warnings are errors) over each package's own translation units, with headers filtered to the
`fieldproof_*` packages so Gazebo's headers are not diagnosed. The workspaces default to
`sim bridge robots`, and that order is also the overlay chain: before a workspace is built or
tested, the install space of every workspace earlier in the list is sourced, so `robots` sees
`fieldproof_worlds` (its launch test spawns the UGV into the empty world). The Python packages
keep a `pytest.ini` of their own because the distribution pytest in the container (7.4) does not
accept the root `pyproject.toml` settings written for the engine's pytest 9; for an `ament_cmake`
package it sits at the workspace root (`robots/pytest.ini`), above `build/`, because
`ament_add_pytest_test` probes pytest from the build directory at configure time and silently
registers no test when that probe rejects the root settings.

The plugin, world and robot packages install environment hooks: sourcing `sim/install/setup.bash`
in the container puts the plugin library on `GZ_SIM_SYSTEM_PLUGIN_PATH` and the worlds on
`GZ_SIM_RESOURCE_PATH`, so `gz sim -s fieldproof_empty.sdf` finds them; `robots/install` adds
the vehicle models, so `model://ugv_ref` resolves. The vehicle descriptions are xacro, rendered
to URDF and SDF at build time by `scripts/generate_model.py` (`xacro`, then `gz sdf -p`), which
needs the `xacro` package of the sim image.

## One version everywhere

`version.txt` is the repository version, maintained by release-please
([ci.md](ci.md#releases)). The engine reads it directly (dynamic version), so `engine/uv.lock`,
which records no version for a dynamic project, stays valid across releases. The `extra-files`
list in `release-please-config.json` makes the release pull request update the same number in
both cockpit `package.json` files, the four `package.xml` files,
`bridge/fieldproof_bridge/setup.py` and the bridge module (the last two through
`x-release-please-version` markers); `pnpm-lock.yaml` records no version for workspace packages
either. `fieldproof --version`,
the cockpit banner and the Gazebo plugin library all report that number, and
`tests/layout/test_layout.sh` fails when any file disagrees with `version.txt`.

## Adding a package

- **ROS package**: create it under the workspace directory with `package.xml`, a `BUILD_TESTING`
  block with at least `ament_xmllint`, and tests in `test/`; add its `package.xml` to the
  release-please `extra-files`. `make test-ros` picks it up.
- **Python package outside ROS**: only the engine so far; new modules go under
  `engine/src/fieldproof/`.
- **Cockpit package**: `cockpit/packages/<name>/package.json` named `@fieldproof/<name>` with
  `test` and `typecheck` scripts; the workspace's recursive scripts run them.

## Continuous integration

`ci.yml` ([ci.md](ci.md)) runs `make test-engine` (job *Engine tests*, uv cache keyed on
`engine/uv.lock`), `make test-cockpit` plus `pnpm build` (job *Cockpit tests*, pnpm store cache
keyed on `pnpm-lock.yaml`) and `make test-layout` (in the tooling job) on every pull request. The
ROS workspaces need the dev image, so `make test-ros` and `make tidy` run at the end of the
*Container images and ROS workspaces* job, which the path filter enables when `sim/`, `bridge/`,
`robots/`, `tools/ros/` or `.clang-tidy` change (as well as for image changes).

## Troubleshooting

**`colcon build` fails with `PermissionError: [Errno 13] Permission denied: 'log'`.** The dev
container runs as uid 1000 and writes `build/`, `install/` and `log/` into the bind-mounted
workspace, so the checkout must be writable by that uid. Developer accounts prepared by
[host.md](host.md) are uid 1000; CI runs `sudo chown -R 1000:1000 sim bridge robots` before
`make test-ros` because the runner user is uid 1001.

**`make test-ros` says the dev image is missing.** It never builds: run `make images`, or pull
the published image with `FIELDPROOF_IMAGE_TAG=main`.

## Tests

`tests/layout/test_layout.sh` (`make test-layout`) checks the layout against the standards: the
layer directories, READMEs and `ARCHITECTURE.md` exist; every package has a test; every version
file equals `version.txt` and is listed in the release-please `extra-files`; `engine/uv.lock` and
`cockpit/pnpm-lock.yaml` match their manifests; the mypy hook's packages, the eslint hook's
TypeScript, the `packageManager` field, `.nvmrc` and the Python version agree with their
counterparts; `make test` runs the layer suites; build output is ignored; and
`fieldproof --version` prints the repository version.
