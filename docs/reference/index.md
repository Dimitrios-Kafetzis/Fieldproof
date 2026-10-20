# Reference

Reference pages describe the public surface of Fieldproof: every CLI command, scenario field,
event, assertion and manifest field, generated from the schemas wherever possible
([04-engineering-standards.md](../plan/04-engineering-standards.md#documentation)). They arrive
with the tasks that define each surface:

| Page | Produced by | Contents |
| --- | --- | --- |
| [CLI](cli.md) | P0-T07, P1-T03, P1-T04, P1-T05, P1-T06, P1-T07 | `fieldproof` commands, options and exit codes |
| [Reference vehicles](robots.md) | P1-T01, P4-T03, P5-T02 | the reference UGV, UAVs and USV: frames, topics, rates, noise, launch arguments, manifest |
| [Worlds](worlds.md) | P1-T02, P5-T03 | the worlds, their physics settings, spawn frames and models; the Fuel lock and asset convention |
| [Scenarios](scenarios.md) | P1-T05, P2-T01, P2-T02, P2-T05 | the YAML scenario format, versioned ([ADR-0005](../adr/0005-declarative-yaml-scenarios.md)): schema v0, the workspace configuration `fieldproof.toml`, `fieldproof validate` and `fieldproof run`, the runner's phases, the run directory, exit codes |
| [Radio links](network.md) | P2-T02 | the `network` section: the topology (link holders, onboard and radio networks, the ground station), the link profiles and presets, `link.set`/`link.drop`/`link.restore`, the measurement and its verification, a vehicle's `runtime.link`, DDS behaviour over a lossy link and the recommended QoS |
| [Events](events.md) | P2-T03 | the event injection framework: the timeline DSL (`at`, `after`, `when`, `every`), the built-in events, the record of every occurrence, the plugin API and the example plugin; the world and sensor events arrive with the task's second session |
| [Assertions, recording and reports](assertions.md) | P1-T06, P1-T07, P2-T05 | the five assertion primitives and their evidence, the recorder container and the MCAP recording, the `/fieldproof/*` side channels, the report in JSON, Markdown and JUnit, `fieldproof report`; the KPI collectors and the plugin API arrive with P2-T05 |
| [Vehicle manifest](manifest.md) | P1-T04, P2-T08 | `robot.yaml` schema v0: model sections, `extends`, the `runtime` section (image or build, entrypoint, params, driver-swap map, readiness, budget), the generated vehicle images, the readiness probe, the example vehicle and the integration test |
| [Bridge contract](bridge.md) | P1-T03 | `fieldproof bridge gen`, the bundle, the adapters, the topic, type, frame, QoS and rate contract and its tests |
| Run database and reports | P2-T06 | the run database, `fieldproof runs list\|show\|diff`, the matrix and the HTML report |

Until then, the [architecture](../plan/01-architecture.md) is the reference: it shows the scenario
format, the driver-swap map and the layer interfaces as they are planned.
