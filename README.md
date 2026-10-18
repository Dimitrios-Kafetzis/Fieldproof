# Fieldproof

[![CI](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/ci.yml)
[![Images](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/images.yml/badge.svg?branch=main)](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/images.yml)
[![Scenario tests](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/scenario-tests.yml/badge.svg?branch=main)](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/scenario-tests.yml)
[![Docs](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/docs.yml/badge.svg?branch=main)](https://dimitrios-kafetzis.github.io/Fieldproof/)
[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](LICENSE)

**Run the real stack. Prove it before the field.**

Fieldproof is a multi-domain robotics emulation and simulation platform. Ground robots (UGV), drones
(UAV) and surface vessels (USV) run their *unmodified* ROS 2 (and bridged ROS 1) codebases inside
isolated containers, fed by simulated sensors from a physics engine, orchestrated by a declarative
scenario engine, and observed through a browser-based 3D cockpit. Every scenario is an automated
test with pass/fail assertions, so field testing becomes verification of what already passed.

> Status: milestone M1 (the first scenario passes, in CI too) closed with v0.10.0; Phase 2
> (the scenario engine) is under way with fleets of vehicles (P2-T01). See
> [docs/plan/STATUS.md](docs/plan/STATUS.md).

## See it run

![The example UGV leaves the depot's loading bay through the roller door and reaches its goal in the yard, seen from the world's spectator camera](docs/media/m1-demo.gif)

The first scenario as Fieldproof itself records it: the example UGV, running an unmodified Nav2
and SLAM Toolbox stack in its own container, drives from the depot's loading dock through the
roller door to a goal in the yard while five assertions check the run. The clip comes from a
camera sensor in the world (`fieldproof run --video`, or `outputs.video` in a scenario),
encoded headlessly by the simulator and saved into the run directory next to the MCAP
recording and the reports;
[the full-resolution MP4](https://github.com/Dimitrios-Kafetzis/Fieldproof/releases/download/v0.10.0/fieldproof-m1-demo.mp4)
is attached to the v0.10.0 release. Every run of the quick start below produces its own under
`runs/<run_id>/video/`.

## Quick start

The first scenario on an Ubuntu 24.04 workstation with Docker: the example UGV (a differential-drive
robot running Nav2 and SLAM Toolbox, unmodified, in its own container) drives to a goal in the depot
world while five assertions check the run and its topics are recorded to MCAP.

```bash
git clone https://github.com/Dimitrios-Kafetzis/Fieldproof.git
cd Fieldproof
tools/host-setup.sh      # once: Docker, Node, uv, pre-commit (asks for sudo); log out and back in
examples/demo_m1.sh      # or: make demo
```

The demo pulls the published images (about 7 GB the first time), fetches the pinned Gazebo Fuel
models, builds the example vehicle image on the published base (a few minutes) and runs
`fieldproof run examples/scenarios/ugv_nav_to_goal.yaml`. It ends with `PASS`, one line per
assertion, the report (`runs/<run_id>/report.md`, `report.json`, `junit.xml`), the video of the
drive (`runs/<run_id>/video/spectator.mp4`) and the MCAP recording (`runs/<run_id>/recording/`),
which opens in [Foxglove Studio](https://foxglove.dev/) to show the robot's path. The same chain
runs on every push to `main` and nightly in the
[Scenario tests](https://github.com/Dimitrios-Kafetzis/Fieldproof/actions/workflows/scenario-tests.yml)
workflow ([docs/setup/ci.md](docs/setup/ci.md#scenario-tests)).

`examples/demo_m1.sh --local` uses images built with `make images`
([docs/setup/containers.md](docs/setup/containers.md)), `FIELDPROOF_RENDER=software` forces
software rendering on a host without a GPU render node, and `--help` lists the rest. Then read
[docs/reference/scenarios.md](docs/reference/scenarios.md) to write a scenario of your own and
[docs/reference/manifest.md](docs/reference/manifest.md) to bring your own vehicle. On a
workstation, `make run SCENARIO=examples/scenarios/ugv_fleet.yaml` runs three of the example
UGVs at once, each in its own container, namespace and budget
([docs/reference/scenarios.md#fleets](docs/reference/scenarios.md#fleets)).

## Documentation map

The same documents are published as a site at https://dimitrios-kafetzis.github.io/Fieldproof/
on every merge to `main` ([docs/setup/docs.md](docs/setup/docs.md)).

| Document | Purpose |
| --- | --- |
| [docs/plan/00-overview.md](docs/plan/00-overview.md) | Vision, principles, fidelity tiers, glossary |
| [docs/plan/01-architecture.md](docs/plan/01-architecture.md) | Layers, components, interfaces, data flow |
| [docs/plan/02-roadmap.md](docs/plan/02-roadmap.md) | Phases, milestones, exit criteria |
| [docs/plan/03-task-backlog.md](docs/plan/03-task-backlog.md) | Every task, one working session each |
| [docs/plan/04-engineering-standards.md](docs/plan/04-engineering-standards.md) | Repo layout, branching, testing, CI, quality gates |
| [docs/plan/05-working-model.md](docs/plan/05-working-model.md) | How a task session runs, hand-off, what the maintainer provides |
| [docs/plan/06-session-guide.md](docs/plan/06-session-guide.md) | Rules every working session follows from start to hand-off |
| [docs/plan/STATUS.md](docs/plan/STATUS.md) | Live task board, updated at the end of every session |
| [docs/adr/](docs/adr/) | Architecture Decision Records |
| [docs/setup/host.md](docs/setup/host.md) | Prepare an Ubuntu 24.04 workstation: `tools/host-setup.sh`, `tools/host-check.sh` |
| [docs/setup/tooling.md](docs/setup/tooling.md) | Quality gates: `make setup`, `make lint`, `make fmt`, `make test`, commit message rules |
| [docs/setup/containers.md](docs/setup/containers.md) | Container images, pins, rendering modes, `make images`/`up`/`shell`/`down` |
| [docs/setup/ci.md](docs/setup/ci.md) | GitHub Actions workflows, required checks, published image tags, releases, Dependabot |
| [docs/setup/docs.md](docs/setup/docs.md) | The documentation site: `make docs`, `make docs-serve`, writing pages, the generated ADR index, deployment |
| [docs/setup/workspaces.md](docs/setup/workspaces.md) | Repository layout and workspaces: `make test-engine`, `make test-cockpit`, `make test-ros`, `make tidy`, one version everywhere |
| [docs/reference/](docs/reference/index.md) | Reference pages, starting with the [CLI](docs/reference/cli.md) |
| [examples/](examples/README.md) | The example scenarios and vehicle, and `examples/demo_m1.sh`, the M1 demo of the quick start |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Which directory is which layer, and where its documentation lives |

## Core principle

The robot code never knows it is in Fieldproof. Only hardware driver nodes are swapped for simulated
sensor sources publishing identical topics and message types, and the clock may be simulated. Perception,
localisation, planning, control and mission logic run exactly as they do on the vehicle.

## Repository

https://github.com/Dimitrios-Kafetzis/Fieldproof

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
