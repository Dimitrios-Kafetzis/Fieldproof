# Architecture

Fieldproof is a monorepo ([ADR-0008](docs/adr/0008-monorepo-apache2-github.md)) with one directory
per layer of the [architecture](docs/plan/01-architecture.md). Dependencies point downward only:
the simulator knows nothing about scenarios, and the robot code knows nothing about any of it.

| Directory | Layer | Language and build | Documentation |
| --- | --- | --- | --- |
| [sim/](sim/) | L1 physics and sensors: Gazebo worlds (`fieldproof_worlds`) and system plugins (`fieldproof_gz_plugins`) | C++17, `ament_cmake`, colcon in the dev container | [ADR-0001](docs/adr/0001-gazebo-harmonic-as-physics-and-sensor-engine.md) |
| [bridge/](bridge/) | L2 bridge: ros_gz configuration generator and adapter nodes (`fieldproof_bridge`) | Python 3.12, `ament_python`, colcon | [ADR-0002](docs/adr/0002-ros2-jazzy-primary-ros1-via-bridge.md) |
| [robots/](robots/) | reference vehicle descriptions and manifests (`fieldproof_robots`) | xacro/URDF, SDF, YAML, `ament_cmake` | [ADR-0004](docs/adr/0004-one-container-per-vehicle.md) |
| [engine/](engine/) | L4 scenario engine and the `fieldproof` CLI | Python 3.12, uv, Pydantic, Typer | [ADR-0005](docs/adr/0005-declarative-yaml-scenarios.md), [ADR-0006](docs/adr/0006-sim-time-default-realtime-locked-mode.md) |
| [cockpit/](cockpit/) | L5 browser cockpit | TypeScript strict, pnpm, Vite, React, Three.js | [ADR-0003](docs/adr/0003-web-cockpit-threejs-foxglove-protocol.md) |
| [docker/](docker/) | container images and the local compose stack | Dockerfiles, Compose | [docs/setup/containers.md](docs/setup/containers.md) |
| [examples/](examples/) | scenarios, vehicles and plugins that exercise the public interfaces | YAML, Python, C++ | [docs/plan/03-task-backlog.md](docs/plan/03-task-backlog.md) |
| [assets/](assets/) | vendored meshes and textures with their licences, Gazebo Fuel lock file | binary assets, `LICENSES/` | [assets/README.md](assets/README.md) |
| [tools/](tools/) | host setup, CI helpers, colcon driver, documentation tooling | Bash, Python | [docs/setup/host.md](docs/setup/host.md) |
| [tests/](tests/) | cross-layer tests: tooling, CI, docs, images, layout, later integration and scenario tests | Bash, pytest | [docs/plan/04-engineering-standards.md](docs/plan/04-engineering-standards.md#testing-pyramid) |
| [docs/](docs/) | the plan, the ADRs, setup guides and reference pages, published as a site | MkDocs Material | [docs/setup/docs.md](docs/setup/docs.md) |

Each layer keeps its unit tests next to its code and is run by one Makefile target
(`make test-engine`, `make test-cockpit`, `make test-ros`); `make test` runs everything
([docs/setup/workspaces.md](docs/setup/workspaces.md)). Every version file (`version.txt`, the
engine, the cockpit packages, the ROS `package.xml` files) carries the same number, maintained by
release-please ([docs/setup/ci.md](docs/setup/ci.md#releases)).

The full description of the layers, their interfaces and the data flow of a run is in
[docs/plan/01-architecture.md](docs/plan/01-architecture.md); the decisions behind them are the
[ADRs](docs/adr/README.md).
