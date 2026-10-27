# Reference

Reference pages describe the public surface of Fieldproof: every CLI command, scenario field,
event, assertion and manifest field, generated from the schemas wherever possible
([04-engineering-standards.md](../plan/04-engineering-standards.md#documentation)). They arrive
with the tasks that define each surface:

| Page | Produced by | Contents |
| --- | --- | --- |
| [CLI](cli.md) | P0-T07, P1-T03, P1-T04, P1-T05, P1-T06, P1-T07, P2-T03, P2-T04, P2-T05 | `fieldproof` commands, options and exit codes |
| [Reference vehicles](robots.md) | P1-T01, P4-T03, P5-T02 | the reference UGV, UAVs and USV: frames, topics, rates, noise, launch arguments, manifest |
| [Worlds](worlds.md) | P1-T02, P5-T03 | the worlds, their physics settings, spawn frames and models; the Fuel lock and asset convention |
| [Scenarios](scenarios.md) | P1-T05, P2-T01, P2-T02, P2-T04, P2-T05 | the YAML scenario format, versioned ([ADR-0005](../adr/0005-declarative-yaml-scenarios.md)): schema v0, the workspace configuration `fieldproof.toml`, `fieldproof validate` and `fieldproof run`, the runner's phases, the run directory, exit codes |
| [Radio links](network.md) | P2-T02 | the `network` section: the topology (link holders, onboard and radio networks, the ground station), the link profiles and presets, `link.set`/`link.drop`/`link.restore`, the measurement and its verification, a vehicle's `runtime.link`, DDS behaviour over a lossy link and the recommended QoS |
| [Time and determinism](time.md) | P2-T04 | the clock of a run: `time.mode` (`sim`, `realtime`, `lockstep`), the real-time-locked mode and its drift verdict, lockstep (the clock agents, the acknowledgement protocol, the timeline in simulation time), `seed`, the `sim.pause`/`sim.resume`/`sim.step` events, `fieldproof clock` and `fieldproof compare`, the determinism test |
| [Events](events.md) | P2-T03 | the event injection framework: the timeline DSL (`at`, `after`, `when`, `every`), the built-in events (the engine-side ones, the world events on the `fieldproof::WorldEvents` plugin, the sensor and vehicle faults on the bridge's injectors), the record of every occurrence, the plugin API and the example plugin |
| [Assertions, KPIs, recording and reports](assertions.md) | P1-T06, P1-T07, P2-T05 | the assertion library: the five primitives and the eleven types of v1 (`geofence`, `min_distance`, `path_efficiency`, `mission_time`, `message_latency`, `sequence`, `resource_usage`, `lifecycle_state`, `always`/`never`/`eventually` over signals) with their parameters, evidence and metrics, `on_fail: abort`, the KPI collectors and the run's KPI map, the assertion plugin API and the example plugin, `fieldproof assertions`; the recorder container, the resource monitor and the MCAP recording, the `/fieldproof/*` side channels, the report in JSON, Markdown (with evidence excerpts) and JUnit, `fieldproof report` |
| [Vehicle manifest](manifest.md) | P1-T04, P2-T08 | `robot.yaml` schema v0: model sections, `extends`, the `runtime` section (image or build, entrypoint, params, driver-swap map, readiness, budget), the generated vehicle images, the readiness probe, the example vehicle and the integration test |
| [Bridge contract](bridge.md) | P1-T03 | `fieldproof bridge gen`, the bundle, the adapters, the topic, type, frame, QoS and rate contract and its tests |
| Run database and reports | P2-T06 | the run database, `fieldproof runs list\|show\|diff`, the matrix and the HTML report |

Until then, the [architecture](../plan/01-architecture.md) is the reference: it shows the scenario
format, the driver-swap map and the layer interfaces as they are planned.
