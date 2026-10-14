# Status board

Updated at the end of every session. States: `todo` · `in-progress` · `in-review` · `done` · `blocked`.

**Current milestone:** M0 Hello, CI — in progress
**Repository:** https://github.com/Dimitrios-Kafetzis/Fieldproof
**Next task:** P0-T02 Host environment setup (needs sudo approval in-session). P0-T04 may run in parallel.

## Phase 0 — Foundations

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P0-T01 | Repository bootstrap and GitHub setup | in-review | #1 | 2026-10-14: bootstrap commit pushed to `main`; Apache-2.0, NOTICE, CoC 2.1, CONTRIBUTING, SECURITY, CODEOWNERS, templates, editorconfig, session guide added; `main` protected (PR required, linear history, enforce admins, conversation resolution, no force push); squash-only merges; 24 project labels; private vulnerability reporting on. Required status checks list is empty until P0-T05 wires CI. |
| P0-T02 | Host environment setup | todo | | Maintainer has confirmed sudo availability; ask once at session start |
| P0-T03 | Base container images | todo | | |
| P0-T04 | Developer tooling and quality gates | todo | | |
| P0-T05 | CI/CD pipeline | todo | | |
| P0-T06 | Documentation site | todo | | |
| P0-T07 | Monorepo layout and package skeletons | todo | | |

## Phase 1 — Vertical slice

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P1-T01 | Reference UGV model | todo | | |
| P1-T02 | Reference world and asset pipeline | todo | | |
| P1-T03 | Bridge configuration generator and contract tests | todo | | |
| P1-T04 | Robot runtime container and vehicle manifest (×2) | todo | | |
| P1-T05 | Scenario schema v0 and runner v0 | todo | | |
| P1-T06 | Assertions v0, recording and reports | todo | | |
| P1-T07 | First scenario in CI and M1 demo | todo | | |

## Phase 2 — Scenario engine

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P2-T01 | Multi-vehicle orchestration and resource budgets | todo | | |
| P2-T02 | Network emulation | todo | | |
| P2-T03 | Event injection framework (×2) | todo | | |
| P2-T04 | Clock management and determinism | todo | | |
| P2-T05 | Assertion library v1 and KPIs | todo | | |
| P2-T06 | Runner hardening, run matrix and run database | todo | | |
| P2-T07 | Field log replay source | todo | | |
| P2-T08 | Vehicle onboarding SDK and M2 demo | todo | | |

## Phase 3 — Cockpit

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P3-T01 | Cockpit backend | todo | | |
| P3-T02 | Frontend scaffold and design system | todo | | |
| P3-T03 | 3D world view (×2) | todo | | |
| P3-T04 | Sensor and navigation overlays | todo | | |
| P3-T05 | Scenario control, timeline and replay | todo | | |
| P3-T06 | Fleet dashboard and diagnostics | todo | | |
| P3-T07 | Cockpit packaging, docs and M3 demo | todo | | |

## Phase 4 — Drones

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P4-T01 | PX4 SITL integration | todo | | |
| P4-T02 | ArduPilot SITL integration | todo | | |
| P4-T03 | Reference UAV models | todo | | |
| P4-T04 | Aerial events and assertions | todo | | |
| P4-T05 | Companion computer stacks and M4 demo | todo | | |

## Phase 5 — USVs

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P5-T01 | Hydrodynamics and environment plugins (×2) | todo | | |
| P5-T02 | Reference USV model | todo | | |
| P5-T03 | Marine worlds | todo | | |
| P5-T04 | Marine events, assertions and scenarios | todo | | |
| P5-T05 | Cockpit marine layer and M5 demo | todo | | |

## Phase 6 — Fidelity

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P6-T01 | ARM64 target emulation (×2) | todo | | |
| P6-T02 | Compute budget profiles | todo | | |
| P6-T03 | Sensor noise calibration from field logs | todo | | |
| P6-T04 | Hardware in the loop | todo | | |
| P6-T05 | ROS 1 compatibility | todo | | |
| P6-T06 | Sim-versus-field comparison and M6 demo | todo | | |

## Phase 7 — Product

| Task | Title | State | PR | Notes |
|---|---|---|---|---|
| P7-T01 | Scenario library and templates | todo | | |
| P7-T02 | World editor in the cockpit | todo | | |
| P7-T03 | Run history dashboard and trends | todo | | |
| P7-T04 | Plugin SDK and versioned APIs | todo | | |
| P7-T05 | Security hardening and supply chain | todo | | |
| P7-T06 | Release v1.0 | todo | | |

## Open questions for the maintainer

1. ~~GitHub owner and visibility~~ Resolved 2026-10-14: public repo at https://github.com/Dimitrios-Kafetzis/Fieldproof.
2. Any real vehicle repository, URDF, field logs or flight controller available for the optional inputs.
3. Preferred PX4 and ArduPilot releases (defaults: latest stable at the time of P4).
