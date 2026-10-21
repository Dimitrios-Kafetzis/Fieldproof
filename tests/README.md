# tests

Cross-layer tests. Each layer keeps its unit tests next to its code (`engine/tests`,
`cockpit/apps/cockpit/src/*.test.ts`, the `test/` directory of every ROS package); this directory
holds what spans layers or the repository itself.

| Directory | Suite | Target |
| --- | --- | --- |
| `tooling/` | quality gates: hooks pass, each gate catches a bad file, commitlint rules | `make test-tooling` |
| `ci/` | workflows: SHA pins, gate job, path filter, release-please and Dependabot configuration | `make test-ci` |
| `docs/` | documentation site: ADR index, strict build, navigation, links | `make test-docs` |
| `layout/` | monorepo layout: directories per the standards, one version everywhere, lock files and pins in sync | `make test-layout` |
| `host/` | host preparation scripts | `make test-host` |
| `images/` | container images, compose stack, rendering | `make test-images` |
| `assets/` | asset convention, `assets/fuel.lock`, the Fuel fetch script against a local stand-in | `make test-assets` |
| `worlds/` | the depot world with the UGV: load time, sensors, real-time factor (pytest, dev container) | `make test-worlds` |
| `vehicles/` | vehicle manifests against schema v0, the generated Dockerfile, the example vehicle image: build, contents, Nav2 lifecycle on a Docker network | `make test-vehicles` |
| `integration/` | the vertical slice: sim, bridge and the example vehicle as three containers in the depot; `fieldproof vehicle wait` until Nav2 is active, a `NavigateToPose` goal reached within tolerance; then `fieldproof run` of the example scenario end to end (the run passes, the run directory is complete, no container is left) and a run interrupted with SIGINT that leaves nothing behind; and the fleet (`test_fleet.py`, task P2-T01): three example UGVs at once with `--check-contracts`, their cgroup limits read from `docker inspect` and `docker stats` while they run, the run's domain and each vehicle's namespace and DDS profile on the containers, no cross-talk seen from inside `ugv1`, every vehicle at its goal; skipped on hosts with fewer than eight cores unless `FIELDPROOF_IT_FLEET=1`; the radio links (`test_radio_links.py`, task P2-T02): ping and iperf on a link the engine shapes with every preset, then the link-loss scenario; the events (`test_event_injection.py`, task P2-T03): the events scenario with every trigger of the timeline DSL and every engine-side event, the vehicle container seen paused and then killed, the recording with the plugin's messages (pytest, host) | `make test-integration` |

The pytest suites here use the root `pyproject.toml` configuration. `worlds/` runs inside the
dev container with the ROS install spaces sourced (`tools/ros/colcon.sh run python3 -m pytest
tests/<suite>`); `integration/` runs on the host from the engine's environment and drives Docker
(the host has no ROS: every ROS command runs inside a container). Scenario regression tests
arrive with Phase 2 under `tests/scenarios/`
([04-engineering-standards.md](../docs/plan/04-engineering-standards.md#testing-pyramid)).
