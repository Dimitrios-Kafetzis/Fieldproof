# Fieldproof

**Run the real stack. Prove it before the field.**

Fieldproof is a multi-domain robotics emulation and simulation platform. Ground robots (UGV), drones
(UAV) and surface vessels (USV) run their *unmodified* ROS 2 (and bridged ROS 1) codebases inside
isolated containers, fed by simulated sensors from a physics engine, orchestrated by a declarative
scenario engine, and observed through a browser-based 3D cockpit. Every scenario is an automated
test with pass/fail assertions, so field testing becomes verification of what already passed.

> Status: Phase 0 (Foundations) in progress. See [docs/plan/STATUS.md](docs/plan/STATUS.md).

## Documentation map

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

## Core principle

The robot code never knows it is in Fieldproof. Only hardware driver nodes are swapped for simulated
sensor sources publishing identical topics and message types, and the clock may be simulated. Perception,
localisation, planning, control and mission logic run exactly as they do on the vehicle.

## Repository

https://github.com/Dimitrios-Kafetzis/Fieldproof

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
