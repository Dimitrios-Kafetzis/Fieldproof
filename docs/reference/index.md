# Reference

Reference pages describe the public surface of Fieldproof: every CLI command, scenario field,
event, assertion and manifest field, generated from the schemas wherever possible
([04-engineering-standards.md](../plan/04-engineering-standards.md#documentation)). They arrive
with the tasks that define each surface:

| Page | Produced by | Contents |
| --- | --- | --- |
| [CLI](cli.md) | P0-T07, P1-T05 | `fieldproof` commands, options and exit codes |
| [Reference vehicles](robots.md) | P1-T01, P4-T03, P5-T02 | the reference UGV, UAVs and USV: frames, topics, rates, noise, launch arguments, manifest |
| [Worlds](worlds.md) | P1-T02, P5-T03 | the worlds, their physics settings, spawn frames and models; the Fuel lock and asset convention |
| Scenario schema | P1-T05, P2-T05 | the YAML scenario format, versioned ([ADR-0005](../adr/0005-declarative-yaml-scenarios.md)) |
| Events | P2-T03 | the event injection framework: types, parameters, timing |
| Assertions and KPIs | P1-T06, P2-T05 | the assertion library and the metrics a run reports |
| Vehicle manifest | P1-T04, P2-T08 | `robot.yaml`: the driver-swap map, topic contract and budgets |
| [Bridge contract](bridge.md) | P1-T03 | `fieldproof bridge gen`, the bundle, the adapters, the topic, type, frame, QoS and rate contract and its tests |
| Run database and reports | P2-T06 | the run record, JUnit output and the report layout |

Until then, the [architecture](../plan/01-architecture.md) is the reference: it shows the scenario
format, the driver-swap map and the layer interfaces as they are planned.
