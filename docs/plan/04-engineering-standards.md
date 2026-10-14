# 04 — Engineering standards

These standards are enforced by tooling wherever possible (P0-T04, P0-T05). Where a standard cannot
be
automated, the PR template asks for it explicitly.

## Repository layout (monorepo)

```text
fieldproof/
├── sim/                     # colcon workspace: Gazebo worlds and C++ plugins
│   ├── fieldproof_worlds/
│   └── fieldproof_gz_plugins/
├── bridge/                  # colcon workspace: ros_gz config generator, adapter nodes
│   └── fieldproof_bridge/
├── robots/                  # reference vehicle descriptions and manifests
│   └── fieldproof_robots/
├── engine/                  # Python package `fieldproof` (scenario engine, CLI)
│   ├── src/fieldproof/
│   └── tests/
├── cockpit/                 # pnpm workspace: web GUI
│   ├── apps/cockpit/
│   └── packages/
├── docker/                  # Dockerfiles and compose
├── examples/                # scenarios, vehicles, plugins
├── assets/                  # vendored meshes/textures with LICENSES/, Fuel lockfile
├── tools/                   # host scripts, calibration utilities
├── tests/                   # cross-layer integration and scenario tests
├── docs/                    # MkDocs site: plan, adr, setup, reference, guides
└── .github/                 # workflows, templates, CODEOWNERS
```

## Languages and versions

| Layer | Language | Version | Notes |
| --- | --- | --- | --- |
| Gazebo plugins | C++ | C++17 (Gazebo Harmonic ABI) | `ament_cmake`, clang-format (Google style, 100 cols), clang-tidy |
| Bridge adapters | Python, C++ where rate demands | 3.12 | rclpy nodes; C++ for > 100 Hz paths |
| Engine, CLI, tooling | Python | 3.12 | `uv` managed, Pydantic v2, Typer CLI, Rich output |
| Cockpit | TypeScript | 5.x strict | Node 22 LTS, pnpm, Vite, React 18, Three.js |
| Scripts | Bash | POSIX-ish | shellcheck clean, `set -euo pipefail` |

## Version control

- `main` is always releasable. Protected: PR required, checks required, linear history, no force push.
- Branches: `task/<id>-<slug>` for backlog tasks, `fix/<slug>` for unplanned fixes,
  `adr/<nnnn>-<slug>` for decisions.
- **Conventional Commits** (`feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, `chore`,
  `perf`) with a scope equal to the layer (`engine`, `sim`, `bridge`, `robots`, `cockpit`, `docker`,
  `docs`, `ci`) or, for repository-wide changes, `repo`, `plan`, `tools`, `deps` or `release`.
  Breaking changes carry `!`. Enforced by commitlint (P0-T04).
- Squash-merge; the squash message is the PR title plus a body summarising the change and `Task: P0-T01`.
- Tags `vX.Y.Z` created by release-please. SemVer applies to the CLI, the scenario schema
  (`apiVersion`) and the plugin APIs independently; the repo version tracks the CLI.
- Every PR links its task ID and ticks the checklist: tests added, docs updated, ADR impact
  considered, STATUS.md updated.

## Testing pyramid

| Level | Scope | Tooling | Where it runs |
| --- | --- | --- | --- |
| Unit | Pure functions, schema, DSL, assertions on synthetic streams, React components | pytest, GoogleTest, Vitest | every PR, < 5 min |
| Component | One container or node in isolation (bridge contract, plugin service) | `launch_testing`, Docker SDK fixtures | every PR |
| Integration | Sim + bridge + one vehicle, short scenarios | pytest + `fieldproof run` headless, software rendering | every PR, ≤ 20 min budget |
| Scenario regression | Full library scenarios, matrices, determinism, perf | `fieldproof matrix`, JUnit | nightly and on release branches |
| End-to-end GUI | Browser against a recorded run | Playwright | every PR (smoke), nightly (full) |
| Manual gate | Milestone demo on the maintainer's machine | demo scripts | per milestone |

Rules: new code without tests is not merged. Coverage targets: engine ≥ 85 %, bridge ≥ 80 %, cockpit
≥ 70 % lines. Flaky tests are quarantined with an issue within 24 h and fixed or deleted within a
week.

## Code quality gates (pre-commit and CI)

ruff (lint + format), mypy strict on `engine/`, clang-format, clang-tidy, cmake-format, eslint,
prettier, yamllint, markdownlint, shellcheck, hadolint, gitleaks, commitlint, `ament_lint` suite for
ROS packages, `gz sdf --check` for SDF, JSON Schema validation for scenarios and manifests.

## Containers

- Multi-stage Dockerfiles, non-root runtime user, pinned base image digests, apt snapshot pinning,
  `--no-install-recommends`, cache mounts.
- OCI labels: source, revision, created, licence, title.
- Images published to GHCR as `ghcr.io/dimitrios-kafetzis/fieldproof-<name>:<tag>` with `main`,
  `vX.Y.Z` and `sha-<short>` tags; SBOM (syft) and vulnerability scan (Trivy, fail on critical) in
  CI.
- Nothing from ROS or Gazebo installs on the host.

## Documentation

- Docs live in the repo and ship with the code change. MkDocs Material, deployed on merge to `main`.
- Every public CLI command, scenario field, event, assertion and manifest field has a reference page
  generated from the schema where possible.
- **ADRs** in `docs/adr/` using the template. States: Proposed → Accepted → Superseded/Deprecated.
  An accepted ADR changes only by a new ADR superseding it.
- Diagrams as Mermaid or ASCII in Markdown so they diff.

## Observability and logging

- Structured JSON logs from the engine (`run_id`, component, event); human-readable Rich console on
  the CLI.
- All engine events, assertions and metrics also published as ROS 2 topics under `/fieldproof/*` so
  they land in the MCAP.
- Resource metrics per container sampled at 1 Hz into the recording.

## Dependency management

- Python: `uv.lock`; Node: `pnpm-lock.yaml`; ROS: `.repos` files with pinned commits for vendored
  sources; Gazebo Fuel: `assets/fuel.lock`; container bases: digests. Dependabot and Renovate-style
  weekly updates in a single PR per ecosystem.

## Security

- No secrets in the repo; `gitleaks` hook. Local-only network binding by default. Non-root
  containers. Images scanned. Threat model and auth arrive in P7-T05. `SECURITY.md` defines private
  disclosure.

## Definition of done (every task)

1. All deliverables in the backlog entry exist.
2. Tests at the appropriate level pass locally and in CI.
3. Docs updated (reference, guides, ADRs if a decision was made).
4. PR merged to `main` by squash with the task ID.
5. `docs/plan/STATUS.md` updated with hand-off notes.
6. Commits authored by the maintainer only; no co-author trailers or tool attribution in commits,
   PRs, code or docs.
