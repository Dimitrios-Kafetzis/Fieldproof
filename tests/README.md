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

The pytest suites here run inside the dev container with the ROS install spaces sourced
(`tools/ros/colcon.sh run python3 -m pytest tests/<suite>`) and use the root `pyproject.toml`
configuration. Integration tests (sim + bridge + one vehicle) and scenario regression tests
arrive with Phase 1 and Phase 2 under `tests/integration/` and `tests/scenarios/`
([04-engineering-standards.md](../docs/plan/04-engineering-standards.md#testing-pyramid)).
