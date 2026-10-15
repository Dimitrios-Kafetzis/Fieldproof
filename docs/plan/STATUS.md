# Status board

Updated at the end of every session. States: `todo` · `in-progress` · `in-review` · `done` · `blocked`.

**Current milestone:** M0 Hello, CI — in progress
**Repository:** https://github.com/Dimitrios-Kafetzis/Fieldproof
**Next task:** P0-T06 Documentation site (in progress); P0-T07 follows.

## Phase 0 — Foundations

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
| P0-T01 | Repository bootstrap and GitHub setup | done | #1 | 2026-10-14: bootstrap commit pushed to `main`; Apache-2.0, NOTICE, CoC 2.1, CONTRIBUTING, SECURITY, CODEOWNERS, templates, editorconfig, session guide added; `main` protected (PR required, linear history, enforce admins, conversation resolution, no force push); squash-only merges; 24 project labels; private vulnerability reporting on. Required status checks list is empty until P0-T05 wires CI. |
| P0-T02 | Host environment setup | done | #2 | 2026-10-14: host prepared and verified. Installed (pinned): Docker 29.9.0 + Compose 5.6.0 + Buildx 0.38.0, containerd 2.4.2, qemu-user-static 1:8.2.2 (binfmt `POF`), gh 2.45.0, nvm v0.40.8, Node 22.23.3, pnpm 12.11.2, uv 0.13.1, pre-commit 4.7.0. `tools/host-setup.sh` (idempotent, one sudo), `tools/host-check.sh` (passes twice), `tests/host/test_host_check.sh` (18 pass), `docs/setup/host.md`, `.nvmrc`. Both definition-of-done container commands pass. Hand-off: the maintainer must log out and back in once so the `docker` group is active without `sg docker`. Sudo from editor tasks works through `SUDO_ASKPASS` with a clean-env zenity helper (VS Code snap breaks GTK); documented in the troubleshooting section. |
| P0-T03 | Base container images | done | #5 | 2026-10-14: three images from snapshot-pinned apt sources (Ubuntu `20261010T000000Z`, ROS `2026-10-05`): `base-ros2` (Jazzy ros-base, non-root `fieldproof`, tini), `sim` (Gazebo Harmonic 8.15 through the Jazzy `gz-*-vendor` packages pulled by `ros_gz`, Mesa EGL headless), `dev` (colcon + mixins, clang 18 tooling, uv 0.13.1, rosdep, vcstool, sudo). `docker/compose.yaml` with profiles `sim`/`dev`/`images` and `compose.gpu.yaml` for `/dev/dri`; root `Makefile` (`images`, `up`, `down`, `shell`, `prune`, `test-images`); `.devcontainer/devcontainer.json`; `docs/setup/containers.md`. Verified: `make up` runs `gz sim -s -r --headless-rendering empty.sdf`, `ros2 topic list` and `gz topic -l` work from `dev`; the camera world renders 320x240 frames on the Radeon 860M through `/dev/dri` (radeonsi) and with `LIBGL_ALWAYS_SOFTWARE=1` (llvmpipe); cold build 406 s (`NO_CACHE=1 make images`, fresh cache-mount namespace) with the Ubuntu snapshot host healthy; the same host spent part of the session returning 502/503 and up to 16 s per request, during which cold builds took 17 to 20 min, hence `fieldproof-apt-install` (parallel, mirror-first package fetch) and the pinned lists kept in the base image; follow-up in #4. Hand-off: sizes 0.9/3.1/4.1 GB; the ROS snapshot key expires 2027-06-01; `rosdep update` is a manual step; the shared-checkout collision with the parallel P0-T04 session is noted under open questions. |
| P0-T04 | Developer tooling and quality gates | done | #3 | 2026-10-14: `.pre-commit-config.yaml` with 29 pinned hooks (ruff, mypy, clang-format, cmake-format/lint, eslint, prettier, yamllint, markdownlint-cli2, shellcheck, hadolint container, check-jsonschema, gitleaks, commitlint, pre-commit-hooks); root `pyproject.toml`, `.clang-format`, `.clang-tidy` (config only, runs in the sim container from P0-T07), `.cmake-format.yaml`, `eslint.config.cjs` (CommonJS on purpose, see tooling.md), `.prettierrc.yaml`, `.yamllint.yaml`, `.markdownlint-cli2.yaml`, `.hadolint.yaml`, `commitlint.config.mjs`, `.gitleaks.toml`; `Makefile` (`setup`, `lint`, `lint-files`, `lint-commits`, `secrets`, `fmt`, `test`, `test-tooling`, `test-host`, `clean`); `tests/tooling/test_quality_gates.sh` (29 pass); `docs/setup/tooling.md`; CONTRIBUTING, README and the standards updated. Planning docs reflowed (whitespace only) to pass markdownlint. Both definition-of-done checks pass: `pre-commit run --all-files` is green and `git commit -m 'Added developer tooling'` is rejected by the commit-msg hook. Hand-off: P0-T07 adds the engine's packages to the mypy hook's `additional_dependencies` and wires clang-tidy into the sim container build; P0-T05 runs `make lint` and `make test` in CI. |
| P0-T05 | CI/CD pipeline | done | #6, #11, #12 | 2026-10-14: workflows `ci.yml` (lint, tooling/CI/host tests, `docker buildx bake` build of base-ros2/sim/dev from a GHCR registry cache, image tests; single required gate job `CI`), `pr-title.yml` (commitlint on the title, required check `PR title`), `images.yml` (push `sha-<short>`, syft SBOM, Trivy report to code scanning, fail on a fixed CRITICAL with `docker/trivy-policy.rego` exempting `linux-libc-dev`, then tag `main`/`vX.Y.Z` from the scanned digest; reusable), `release.yml` (release-please `simple`, `version.txt`, pre-1.0 bumps, release PR `chore(release): release X.Y.Z`, calls `images.yml` for the tag), `docs.yml` placeholder; Dependabot (actions, docker, pip `/engine`, npm `/cockpit`, weekly, grouped, `build(deps)`); every action SHA-pinned; actionlint hook; `tools/ci/` (changed-areas, bake-cache, free-disk, branch-protection); `tests/ci/test_workflows.sh` (35 pass); `make lint-title`, `make test-ci`, `make branch-protection`; `test_images.sh --no-build`; `docs/setup/ci.md`; README badges. Verified on GitHub: #6 CI green (lint 2 min, image job 16 min cold, 6 min with the cache, image tests 19 pass with llvmpipe rendering); throwaway #7 (ruff/mypy error) CI red; throwaway #8 (docs only) CI green with the image job skipped; both closed, branches deleted. Three runner fixes were needed: pin buildx v0.38.0 and set `COMPOSE_PROFILES`, run bake from `docker/`, pin Compose v5.6.0. Also resolves issue #4 (option 2, on the maintainer's request): the base Dockerfile's scratch stage `apt-lists` publishes `/var/lib/apt/lists` as `fieldproof-apt-lists:<APT_SNAPSHOT>-<ROS_SNAPSHOT>` from `images.yml`; `docker/apt-lists-context.sh` seeds later builds from it (local or GHCR, `FIELDPROOF_APT_LISTS=off` to force the snapshot hosts); seeded cold build locally 467 s with all three `apt-get update` refreshes skipped; CI fallback build 10.5 min; image tests now 27 checks. Hand-off: (1) run `make branch-protection` (the session's `gh api` writes to repository settings were denied) so `CI` and `PR title` become required and actions SHA pinning is enforced, then merge #6 with its title; (2) on the first push to `main` set the four GHCR packages (`base-ros2`, `sim`, `dev`, `apt-lists`) to public and check they are linked to the repo; (3) add the secret `RELEASE_PLEASE_TOKEN` (fine-grained PAT, contents + pull requests read/write) so the release PR gets checks; (4) Dependabot reports `/engine` and `/cockpit` missing until P0-T07. Resolves open question 4 with the Conventional Commit title option. 2026-10-15: #6 merged with `CI` and `PR title` as required checks; follow-ups #11 (`fix(release)`: first release is 0.1.0, not 1.0.0) and #12 (`fix(ci)`: weekly `Pins` workflow with `docker/pins-check.sh` replaces the Dependabot docker updater, issue #9) merged; issues #4 and #9 closed; v0.1.0 tagged and released by the release pull request #10. |
| P0-T06 | Documentation site | todo | | |
| P0-T07 | Monorepo layout and package skeletons | todo | | |

## Phase 1 — Vertical slice

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
| P1-T01 | Reference UGV model | todo | | |
| P1-T02 | Reference world and asset pipeline | todo | | |
| P1-T03 | Bridge configuration generator and contract tests | todo | | |
| P1-T04 | Robot runtime container and vehicle manifest (×2) | todo | | |
| P1-T05 | Scenario schema v0 and runner v0 | todo | | |
| P1-T06 | Assertions v0, recording and reports | todo | | |
| P1-T07 | First scenario in CI and M1 demo | todo | | |

## Phase 2 — Scenario engine

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
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
| --- | --- | --- | --- | --- |
| P3-T01 | Cockpit backend | todo | | |
| P3-T02 | Frontend scaffold and design system | todo | | |
| P3-T03 | 3D world view (×2) | todo | | |
| P3-T04 | Sensor and navigation overlays | todo | | |
| P3-T05 | Scenario control, timeline and replay | todo | | |
| P3-T06 | Fleet dashboard and diagnostics | todo | | |
| P3-T07 | Cockpit packaging, docs and M3 demo | todo | | |

## Phase 4 — Drones

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
| P4-T01 | PX4 SITL integration | todo | | |
| P4-T02 | ArduPilot SITL integration | todo | | |
| P4-T03 | Reference UAV models | todo | | |
| P4-T04 | Aerial events and assertions | todo | | |
| P4-T05 | Companion computer stacks and M4 demo | todo | | |

## Phase 5 — USVs

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
| P5-T01 | Hydrodynamics and environment plugins (×2) | todo | | |
| P5-T02 | Reference USV model | todo | | |
| P5-T03 | Marine worlds | todo | | |
| P5-T04 | Marine events, assertions and scenarios | todo | | |
| P5-T05 | Cockpit marine layer and M5 demo | todo | | |

## Phase 6 — Fidelity

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
| P6-T01 | ARM64 target emulation (×2) | todo | | |
| P6-T02 | Compute budget profiles | todo | | |
| P6-T03 | Sensor noise calibration from field logs | todo | | |
| P6-T04 | Hardware in the loop | todo | | |
| P6-T05 | ROS 1 compatibility | todo | | |
| P6-T06 | Sim-versus-field comparison and M6 demo | todo | | |

## Phase 7 — Product

| Task | Title | State | PR | Notes |
| --- | --- | --- | --- | --- |
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
4. ~~Squash-merge commits on `main` carry the PR title, which is not a Conventional Commit~~
   Resolved by P0-T05 (2026-10-14), confirmed by the maintainer with the merge of #6: PR titles are
   Conventional Commits (`type(scope): summary`), checked by the `PR title` workflow; the task ID
   goes in the body. commitlint keeps exempting the four historical `P0-Txx ... (#N)` commits.
   Consequence: #6 is titled `feat(ci): ...`, so release-please opened the `0.1.0` release PR, merged
   as #10 on 2026-10-15 (v0.1.0).
5. commitlint requires a scope and accepts the repository-wide scopes `repo`, `plan`, `tools`,
   `deps`, `release` in addition to the layers (the history already used `repo` and `plan`). Confirm
   or trim the list in `commitlint.config.mjs`.
6. Parallel sessions share one clone. On 2026-10-14 the P0-T04 session switched branches inside the
   main working tree while P0-T03 was on its branch; both sessions then moved to `git worktree`
   checkouts under an ignored directory. Should `05-working-model.md` require a worktree per session
   (`git worktree add ../fieldproof-<task> task/<id>-<slug>`)? Proposed as a one-line amendment for
   the maintainer to approve.
7. GHCR package visibility: packages created by a workflow start private. After the first push to
   `main`, set `fieldproof-base-ros2`, `fieldproof-sim`, `fieldproof-dev` and `fieldproof-apt-lists`
   to public (needed for `FIELDPROOF_IMAGE_TAG=main make up` without a login, and for local
   builds to seed their package lists from GHCR). The session's `gh` token has no `read:packages`
   scope, so this could not be checked.
8. ~~Repository settings changes (`make branch-protection`) must be run by the maintainer~~ Done
   2026-10-15: `CI` and `PR title` are required checks on `main`; `tools/ci/branch-protection.sh
   --check` shows any later drift.
