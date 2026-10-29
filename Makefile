# Fieldproof developer entry points. Run `make` for the list of targets.
#
# Quality gates (task P0-T04) delegate to pre-commit (hooks pinned in .pre-commit-config.yaml) or
# to a container, so nothing beyond Docker, Node and pre-commit itself is installed on the host.
# Containers (task P0-T03) are driven through docker/compose.yaml; see docs/setup/containers.md.
# CI (task P0-T05) runs the same targets from .github/workflows; see docs/setup/ci.md.
# The documentation site (task P0-T06) builds from mkdocs.yml with the toolchain pinned in
# tools/docs; see docs/setup/docs.md.
# The layer workspaces (task P0-T07) have one target each: `test-engine` (uv, host), `test-cockpit`
# (pnpm, host), `test-ros` (colcon in the dev container) and `tidy` (clang-tidy in the dev
# container); see docs/setup/workspaces.md.
# Assets (task P1-T02): `assets` fetches the Gazebo Fuel models pinned in assets/fuel.lock,
# `test-assets` checks the convention, `test-worlds` runs the world tests in the dev container;
# see docs/reference/worlds.md.
# Vehicles (task P1-T04): `manifest-schema` exports the manifest JSON Schema from the engine,
# `vehicle-image` builds the example vehicle image from its manifest, `test-vehicles` runs the
# vehicle tests (manifests, generated Dockerfile, image build and smoke), `test-integration`
# runs the vertical slice (sim, bridge and the vehicle in the depot, a navigation goal); see
# docs/reference/manifest.md.
# Scenarios (task P1-T05): `scenario-schema` exports the scenario JSON Schema, `run` runs a
# scenario with `fieldproof run` (SCENARIO=..., the example by default); `test-integration`
# also runs the example scenario end to end; see docs/reference/scenarios.md.
# The M1 demo (task P1-T07): `demo` runs examples/demo_m1.sh, the quick start of the README
# (the published images, the vehicle image, the example scenario, the report and the MCAP).
# The M2 demo (task P2-T08): `demo-m2` runs examples/demo_m2.sh, the scenario engine's
# milestone end to end (fleet, links, events, two clocks, matrix, replay, onboarding).

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PRE_COMMIT ?= pre-commit
GITLEAKS_IMAGE ?= ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
# Hooks that rewrite files; `make fmt` runs only these and ignores their "files were modified" exit.
FORMAT_HOOKS := trailing-whitespace end-of-file-fixer mixed-line-ending ruff-check ruff-format \
	clang-format cmake-format prettier adr-index
# Base ref for the commit message check of the current branch.
BASE_REF ?= origin/main

# --- Documentation ----------------------------------------------------------------------------
# MkDocs runs from the locked project in tools/docs (uv creates tools/docs/.venv on first use).
UV ?= uv
MKDOCS := $(UV) run --locked --project tools/docs mkdocs
DOCS_ADDR ?= 127.0.0.1:8000
# Path of the site under its host (site_url in mkdocs.yml is https://dimitrios-kafetzis.github.io/Fieldproof/).
DOCS_BASE_PATH ?= Fieldproof
LYCHEE_IMAGE ?= lycheeverse/lychee:0.24.2@sha256:e2d19e57cf6ab037026f20b8e449a1f30d9d7f81eef4194763aab2eab20bd28d
# Extra lychee flags, for example LYCHEE_ARGS=--offline to check only the internal links.
LYCHEE_ARGS ?=

# --- Layer workspaces ------------------------------------------------------------------------
# The engine is a uv project (engine/.venv from engine/uv.lock); the cockpit is a pnpm workspace
# (cockpit/node_modules from cockpit/pnpm-lock.yaml). Both toolchains come from the host setup
# (uv, Node 22 with pnpm). The ROS workspaces build only inside the dev container.
PNPM ?= pnpm
ROS_WORKSPACES ?= sim robots bridge
# tools/assets/fetch_fuel.py needs python3 with PyYAML, as the layout tests do.
PYTHON ?= python3
# Limits of the world tests (tests/worlds); CI relaxes them for software rendering.
WORLD_TEST_ENV := $(foreach v,FIELDPROOF_WORLD_LOAD_LIMIT_S FIELDPROOF_WORLD_RTF_MIN,$(if $($(v)),-e $(v)=$($(v))))
# The bridge contract test's rate tolerance for the rendering sensors (issue #69); CI widens it
# under software rendering.
ROS_TEST_ENV := $(if $(FIELDPROOF_CONTRACT_RENDER_TOLERANCE),-e FIELDPROOF_CONTRACT_RENDER_TOLERANCE=$(FIELDPROOF_CONTRACT_RENDER_TOLERANCE))
# The example vehicle (task P1-T04): its manifest, and the base image its build starts from
# (the same prefix and tag as the stack's images, so `FIELDPROOF_IMAGE_TAG=main` builds on the
# published base). `make test-vehicles VEHICLE_TEST_FLAGS=--no-build` tests an image already built.
VEHICLE_MANIFEST ?= examples/vehicles/ugv_nav2/robot.yaml
VEHICLE_BASE_IMAGE ?= $(or $(FIELDPROOF_IMAGE_PREFIX),ghcr.io/dimitrios-kafetzis/fieldproof-)base-ros2:$(or $(FIELDPROOF_IMAGE_TAG),local)
VEHICLE_TEST_FLAGS ?=
FIELDPROOF := $(UV) run --locked --project engine fieldproof
# The scenario `make run` runs (task P1-T05) and extra `fieldproof run` arguments (RUN_FLAGS=--keep).
SCENARIO ?= examples/scenarios/ugv_nav_to_goal.yaml
MATRIX ?= examples/matrices/ugv_nav_matrix.yaml
MATRIX_FLAGS ?=
RUN_FLAGS ?=
# Extra pytest arguments of `make test-determinism`.
PYTEST_FLAGS ?=

# --- Containers -------------------------------------------------------------------------------
# Rendering: when the host has a /dev/dri render node, compose.gpu.yaml is added so the sim and
# dev containers use the GPU through Mesa; otherwise everything renders in software. Force one or
# the other with RENDER=gpu or RENDER=software.
GIT_SHA    := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
# version.txt is maintained by release-please (see docs/setup/ci.md).
VERSION    ?= $(shell cat $(CURDIR)/version.txt 2>/dev/null || echo 0.0.0)
export GIT_SHA BUILD_DATE VERSION

RENDER_NODE := $(firstword $(wildcard /dev/dri/renderD*))
RENDER      ?= $(if $(RENDER_NODE),gpu,software)
export FIELDPROOF_RENDER_GID ?= $(shell getent group render | cut -d: -f3)

# Package lists for the base image (issue #4): docker/apt-lists-context.sh looks for the published
# fieldproof-apt-lists image for the pinned snapshots at build time; FIELDPROOF_APT_LISTS=off
# forces the refresh from the snapshot hosts. Expanded lazily, only by the targets that build.
export FIELDPROOF_APT_LISTS_TAG ?= $(shell docker/apt-lists-context.sh --tag)
APT_LISTS_CONTEXT = $(shell docker/apt-lists-context.sh)

COMPOSE_FILES := -f docker/compose.yaml
ifeq ($(RENDER),gpu)
COMPOSE_FILES += -f docker/compose.gpu.yaml
endif
COMPOSE     := docker compose $(COMPOSE_FILES)
# `make up` starts sim and dev; the bridge (started on demand) is included where every service
# counts: image builds, `make down` and `make prune`.
PROFILES    := --profile sim --profile dev
ALL_PROFILES := --profile images $(PROFILES) --profile bridge
BUILD_FLAGS ?=
# NO_CACHE=1 ignores the layer cache and starts from empty apt cache mounts (a fresh BuildKit cache
# mount namespace), which is what a cold build on a new machine or in CI sees.
ifeq ($(NO_CACHE),1)
NO_CACHE_FLAGS := --no-cache --build-arg BUILDKIT_CACHE_MOUNT_NS=cold-$(BUILD_DATE)
endif
# `make test` runs the image tests with the cached build; `make test-images IMAGE_TEST_FLAGS=`
# adds the timed cold build.
IMAGE_TEST_FLAGS ?= --quick

.PHONY: help setup lint lint-files lint-commits lint-title secrets fmt test test-host test-tooling \
	test-images test-ci test-docs test-layout test-engine test-cockpit test-ros test-assets test-worlds \
	test-vehicles test-integration test-determinism tidy docs docs-serve docs-links branch-protection clean pins assets images \
	manifest-schema scenario-schema vehicle-image replay-log run demo demo-m2 up down shell prune

help: ## Show this help
	@awk 'BEGIN { FS = ":.*## "; printf "Targets:\n" } /^[a-zA-Z_-]+:.*## / { printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

setup: ## Install the git hooks (pre-commit and commit-msg) and their environments
	$(PRE_COMMIT) install --install-hooks

lint: lint-files lint-commits secrets ## Run every quality gate: hooks on all files, commit messages, secret scan

lint-files: ## Run all pre-commit hooks on every tracked file
	$(PRE_COMMIT) run --all-files --show-diff-on-failure

lint-commits: ## Check the branch's commit messages against Conventional Commits (BASE_REF=origin/main)
	@if ! git rev-parse --verify --quiet "$(BASE_REF)" >/dev/null; then \
	    echo "lint-commits: $(BASE_REF) not found, skipping"; exit 0; fi; \
	tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; status=0; \
	for sha in $$(git rev-list --no-merges "$(BASE_REF)..HEAD"); do \
	    git log -1 --format=%B "$$sha" > "$$tmp"; \
	    if $(PRE_COMMIT) run commitlint --hook-stage commit-msg --commit-msg-filename "$$tmp" >/dev/null 2>&1; then \
	        printf '  ok    %s %s\n' "$$(git log -1 --format=%h "$$sha")" "$$(git log -1 --format=%s "$$sha")"; \
	    else \
	        printf '  FAIL  %s %s\n' "$$(git log -1 --format=%h "$$sha")" "$$(git log -1 --format=%s "$$sha")"; status=1; \
	    fi; \
	done; exit $$status

lint-title: ## Check a pull request title against commitlint (TITLE="feat(engine): add x")
	@[ -n "$(TITLE)" ] || { echo "lint-title: pass the title as TITLE=\"...\"" >&2; exit 2; }; \
	tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; printf '%s\n' "$(TITLE)" > "$$tmp"; \
	if $(PRE_COMMIT) run commitlint --hook-stage commit-msg --commit-msg-filename "$$tmp"; then \
	    printf '  ok    %s\n' "$(TITLE)"; \
	else \
	    printf '  FAIL  %s\n' "$(TITLE)"; exit 1; \
	fi

# The checkout and its common git directory are mounted at their own absolute paths so that git
# worktrees (whose .git is a pointer file) resolve inside the container. A scan that sees no
# commits is an error, not a pass.
secrets: ## Scan the full git history for secrets with the pinned gitleaks container
	@common="$$(git rev-parse --path-format=absolute --git-common-dir)"; rc=0; \
	out="$$(docker run --rm -v "$(CURDIR):$(CURDIR):ro" -v "$$common:$$common:ro" \
	    -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
	    "$(GITLEAKS_IMAGE)" git "$(CURDIR)" --redact --no-banner --exit-code 1 2>&1)" || rc=$$?; \
	printf '%s\n' "$$out"; [ "$$rc" -eq 0 ] || exit "$$rc"; \
	grep -q -E '[1-9][0-9]* commits scanned' <<< "$$out" || { echo "secrets: gitleaks scanned no commits" >&2; exit 1; }

fmt: ## Apply the formatters (ruff, clang-format, cmake-format, prettier, whitespace fixers)
	@for hook in $(FORMAT_HOOKS); do $(PRE_COMMIT) run "$$hook" --all-files || true; done

test: test-tooling test-ci test-docs test-layout test-assets test-engine test-cockpit test-ros test-worlds test-host test-images test-vehicles test-integration ## Run every test suite: tooling, CI, docs, layout, assets, engine (pytest), cockpit (Vitest), ROS workspaces (ctest, pytest), worlds, host, images, vehicles, integration

test-tooling: ## Quality-gate tests: hooks pass on the repo, each gate catches a bad file, commitlint rejects bad messages
	tests/tooling/test_quality_gates.sh

test-host: ## Host prerequisite tests (set FIELDPROOF_SKIP_HOST=1 on machines that are not prepared)
	tests/host/test_host_check.sh

test-images: ## Container image tests (IMAGE_TEST_FLAGS=--quick by default; empty for the timed cold build)
	tests/images/test_images.sh $(IMAGE_TEST_FLAGS)

test-ci: ## Workflow tests: actionlint, SHA pins, path filter, release-please and Dependabot configuration
	tests/ci/test_workflows.sh

test-docs: ## Documentation tests: ADR index generator, strict site build, navigation, docs.yml rules, internal links
	tests/docs/test_docs.sh

test-layout: ## Monorepo layout tests: directories per the standards, one version everywhere, lock files current, pins in sync
	tests/layout/test_layout.sh

test-engine: ## Engine unit tests: uv sync from engine/uv.lock, then pytest with coverage (85 % of the engine's lines) in engine/
	$(UV) sync --locked --project engine
	$(UV) run --locked --directory engine pytest --cov

test-cockpit: ## Cockpit tests: pnpm install from the lock file, TypeScript strict type check, Vitest in every package
	$(PNPM) -C cockpit install --frozen-lockfile
	$(PNPM) -C cockpit typecheck
	$(PNPM) -C cockpit test

# The dev container is started (not built: `make images` or FIELDPROOF_IMAGE_TAG=main first) and
# left running; `make down` stops it. Build output lands in <workspace>/build, install and log.
test-ros: ## ROS workspace tests: colcon build and colcon test of sim/, robots/ and bridge/ in the dev container (ROS_WORKSPACES=...)
	$(COMPOSE) --profile dev up -d --wait --no-build dev
	$(COMPOSE) --profile dev exec -T $(ROS_TEST_ENV) dev tools/ros/colcon.sh test $(ROS_WORKSPACES)

tidy: ## clang-tidy over the C++ packages in the dev container, using the compile commands of the last test-ros build
	$(COMPOSE) --profile dev up -d --wait --no-build dev
	$(COMPOSE) --profile dev exec -T dev tools/ros/colcon.sh tidy $(ROS_WORKSPACES)

test-assets: ## Asset tests: the vendoring convention, fuel.lock and the fetch script against a local Fuel stand-in
	tests/assets/test_assets.sh

# Builds sim and robots first (seconds when up to date), then runs the pytest suites under
# tests/worlds with both install spaces sourced. FIELDPROOF_WORLD_LOAD_LIMIT_S and
# FIELDPROOF_WORLD_RTF_MIN override the definition-of-done limits (10 s, 1.0).
test-worlds: ## World tests in the dev container: the depot loads in time and runs at real time with the UGV
	$(COMPOSE) --profile dev up -d --wait --no-build dev
	$(COMPOSE) --profile dev exec -T dev tools/ros/colcon.sh build sim robots
	$(COMPOSE) --profile dev exec -T $(WORLD_TEST_ENV) dev tools/ros/colcon.sh run python3 -m pytest -s tests/worlds

# The vehicle tests build the example image (minutes: Nav2 from the apt snapshots) unless
# VEHICLE_TEST_FLAGS=--no-build; --static skips the image altogether.
test-vehicles: ## Vehicle tests: every robot.yaml validates, the generated Dockerfile lints, the example image builds and runs
	tests/vehicles/test_vehicles.sh $(VEHICLE_TEST_FLAGS)

# Needs the stack images (make images), the example vehicle image (make vehicle-image) and the
# Fuel models (make assets); runs on the host from the engine's environment and drives Docker.
# FIELDPROOF_IT_KEEP=1 leaves the containers of the vertical-slice test running after a failure.
test-integration: ## Integration tests (tests/integration): the vertical slice by hand, then `fieldproof run` of the example scenario, an interrupted run, the fleet, the radio links, the events, the world events, the faults, the clock modes
	$(UV) sync --locked --project engine
	$(UV) run --locked --project engine pytest -s -v tests/integration

# Two lockstep runs and their comparison (task P2-T04), too slow for every PR: the nightly
# scenario workflow runs it; PYTEST_FLAGS="--basetemp runs/determinism" keeps the run directories
# (pytest creates the base directory but not its parent, so `runs` must exist).
test-determinism: ## Determinism test (tests/integration/test_determinism.py): the lockstep scenario twice, trajectories and odometry compared
	$(UV) sync --locked --project engine
	FIELDPROOF_DETERMINISM=1 $(UV) run --locked --project engine pytest -s -v $(PYTEST_FLAGS) tests/integration/test_determinism.py

manifest-schema: ## Export the vehicle manifest JSON Schema (engine/schemas/robot.schema.json) from the engine's models
	$(UV) sync --locked --project engine
	$(FIELDPROOF) manifest schema > engine/schemas/robot.schema.json

scenario-schema: ## Export the scenario JSON Schema (engine/schemas/scenario.schema.json) from the engine's models
	$(UV) sync --locked --project engine
	$(FIELDPROOF) schema > engine/schemas/scenario.schema.json

# The synthetic field log of the reference UGV (task P2-T07), the stand-in for a real one until
# the maintainer supplies one: examples/scenarios/ugv_replay.yaml replays it (ignored by git,
# like every recording; deterministic for its seed, so every checkout writes the same file).
replay-log: ## Write the synthetic UGV field log examples/logs/ugv_dock_drive.mcap (REPLAY_LOG_FLAGS=--seconds 120 --seed 7)
	$(UV) sync --locked --project engine
	$(FIELDPROOF) log synth examples/logs/ugv_dock_drive.mcap --force $(REPLAY_LOG_FLAGS)

# Needs the images, the vehicle image and the assets like test-integration; writes runs/<run_id>.
run: assets ## Run a scenario end to end with the Docker backend (SCENARIO=examples/scenarios/ugv_nav_to_goal.yaml, RUN_FLAGS=)
	$(UV) sync --locked --project engine
	$(FIELDPROOF) run $(SCENARIO) $(RUN_FLAGS)

# A run matrix (task P2-T06): every case its own `fieldproof run`, as many at once as the host
# fits, failed cases retried and flagged when flaky, one JUnit document for the whole; writes
# runs/<matrix_id>/ and records every run in runs/fieldproof.sqlite.
matrix: assets ## Run a matrix of scenarios (MATRIX=examples/matrices/ugv_nav_matrix.yaml, MATRIX_FLAGS=--parallel 2|--dry-run|--only PATTERN)
	$(UV) sync --locked --project engine
	$(FIELDPROOF) matrix $(MATRIX) $(MATRIX_FLAGS)

# The M1 demo from a fresh clone: pulls the published stack images (DEMO_FLAGS=--local uses
# `make images` instead), fetches the assets, builds the vehicle image and runs the scenario.
demo: ## Run the M1 demo: examples/demo_m1.sh (DEMO_FLAGS=--local|--no-pull|--rebuild-vehicle|--dry-run)
	examples/demo_m1.sh $(DEMO_FLAGS)

# The M2 demo (task P2-T08): every scenario of the milestone in turn, then a table of verdicts;
# --quick skips the fleet and runs two cases of the matrix.
demo-m2: ## Run the M2 demo: examples/demo_m2.sh (DEMO_FLAGS=--quick|--only STEPS|--skip STEPS|--local|--dry-run)
	examples/demo_m2.sh $(DEMO_FLAGS)

vehicle-image: ## Build the example vehicle image from its manifest (VEHICLE_MANIFEST, on VEHICLE_BASE_IMAGE)
	$(UV) sync --locked --project engine
	$(FIELDPROOF) vehicle build $(VEHICLE_MANIFEST) --base-image $(VEHICLE_BASE_IMAGE) $(BUILD_FLAGS)

docs: ## Build the documentation site into site/ (strict: a broken link or a page outside the nav fails)
	$(MKDOCS) build --strict

docs-serve: ## Serve the documentation site with live reload (DOCS_ADDR=127.0.0.1:8000)
	$(MKDOCS) serve --strict --dev-addr $(DOCS_ADDR)

# The built site is mounted read-only under the path it has on GitHub Pages so that the
# root-relative links of the 404 page (/Fieldproof/...) resolve like the relative ones.
docs-links: docs ## Check every link of the built site, external ones included (LYCHEE_ARGS=--offline for internal only)
	docker run --rm --init -v "$(CURDIR)/site:/srv/$(DOCS_BASE_PATH):ro" -v "$(CURDIR)/.lychee.toml:/lychee.toml:ro" \
	    "$(LYCHEE_IMAGE)" --config /lychee.toml --root-dir /srv $(LYCHEE_ARGS) "/srv/$(DOCS_BASE_PATH)/**/*.html"

branch-protection: ## Wire the CI status checks into the protection of main (needs gh as an admin)
	tools/ci/branch-protection.sh

clean: ## Remove pre-commit's installed hook environments (they are rebuilt on the next run)
	$(PRE_COMMIT) clean

pins: ## Compare the image pins (ubuntu digest, ROS snapshot, uv) with their upstreams
	docker/pins-check.sh

assets: ## Fetch the Gazebo Fuel models pinned in assets/fuel.lock into assets/fuel (idempotent, offline once cached)
	$(PYTHON) tools/assets/fetch_fuel.py --quiet

images: ## Build the base-ros2, sim, dev, bridge and apt-lists images (NO_CACHE=1 for a cold build, FIELDPROOF_APT_LISTS=off to skip the published lists)
	FIELDPROOF_APT_LISTS_CONTEXT="$(APT_LISTS_CONTEXT)" $(COMPOSE) $(ALL_PROFILES) build $(NO_CACHE_FLAGS) $(BUILD_FLAGS)

up: assets ## Start the headless Gazebo server with the depot world (FIELDPROOF_WORLD) and the dev container (RENDER=gpu|software, auto-detected)
	$(COMPOSE) $(PROFILES) up -d --wait

down: ## Stop the container stack (the bridge included)
	$(COMPOSE) $(PROFILES) --profile bridge down --remove-orphans

shell: ## Open a shell in the dev container
	$(COMPOSE) exec dev bash

prune: down ## Remove the stack, the project's images and dangling Fieldproof build layers
	$(COMPOSE) $(ALL_PROFILES) down --remove-orphans --volumes --rmi all
	docker image prune -f --filter label=org.opencontainers.image.vendor=Fieldproof
	@echo "Build cache is shared across projects; reclaim it with: docker builder prune"
