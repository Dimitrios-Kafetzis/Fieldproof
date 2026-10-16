# examples

Worked examples that exercise the public interfaces of Fieldproof. Each is also a test fixture:
the scenario regression suite and the onboarding tests run them in CI.

| Directory | Contents | Arrives with |
| --- | --- | --- |
| `scenarios/` | scenario YAML files, from the first smoke scenario to the matrices of the scenario library | P1-T05, P1-T07, P7-T01 |
| `vehicles/` | vehicle manifests and containers, starting with `ugv_nav2/` (Nav2 as the stand-in codebase) | P1-T04, P2-T08 |
| `plugins/` | event, assertion and cockpit panel plugins written against the plugin SDK | P2-T03, P7-T04 |
