# Fieldproof developer entry points. Run `make` for the list of targets.
#
# Quality gates (task P0-T04) delegate to pre-commit (hooks pinned in .pre-commit-config.yaml) or
# to a container, so nothing beyond Docker, Node and pre-commit itself is installed on the host.
# Containers (task P0-T03) are driven through docker/compose.yaml; see docs/setup/containers.md.
# CI (task P0-T05) runs the same targets from .github/workflows; see docs/setup/ci.md.
# Later tasks add per-layer targets (engine, sim, bridge, cockpit) underneath `test` and `lint`.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PRE_COMMIT ?= pre-commit
GITLEAKS_IMAGE ?= ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
# Hooks that rewrite files; `make fmt` runs only these and ignores their "files were modified" exit.
FORMAT_HOOKS := trailing-whitespace end-of-file-fixer mixed-line-ending ruff-check ruff-format \
	clang-format cmake-format prettier
# Base ref for the commit message check of the current branch.
BASE_REF ?= origin/main

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

COMPOSE_FILES := -f docker/compose.yaml
ifeq ($(RENDER),gpu)
COMPOSE_FILES += -f docker/compose.gpu.yaml
endif
COMPOSE     := docker compose $(COMPOSE_FILES)
PROFILES    := --profile sim --profile dev
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
	test-images test-ci branch-protection clean images up down shell prune

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

test: test-tooling test-ci test-host test-images ## Run every test suite available at this stage of the plan

test-tooling: ## Quality-gate tests: hooks pass on the repo, each gate catches a bad file, commitlint rejects bad messages
	tests/tooling/test_quality_gates.sh

test-host: ## Host prerequisite tests (set FIELDPROOF_SKIP_HOST=1 on machines that are not prepared)
	tests/host/test_host_check.sh

test-images: ## Container image tests (IMAGE_TEST_FLAGS=--quick by default; empty for the timed cold build)
	tests/images/test_images.sh $(IMAGE_TEST_FLAGS)

test-ci: ## Workflow tests: actionlint, SHA pins, path filter, release-please and Dependabot configuration
	tests/ci/test_workflows.sh

branch-protection: ## Wire the CI status checks into the protection of main (needs gh as an admin)
	tools/ci/branch-protection.sh

clean: ## Remove pre-commit's installed hook environments (they are rebuilt on the next run)
	$(PRE_COMMIT) clean

images: ## Build the base-ros2, sim and dev images (NO_CACHE=1 for a cold build)
	$(COMPOSE) --profile images $(PROFILES) build $(NO_CACHE_FLAGS) $(BUILD_FLAGS)

up: ## Start the headless Gazebo server and the dev container (RENDER=gpu|software, auto-detected)
	$(COMPOSE) $(PROFILES) up -d --wait

down: ## Stop the container stack
	$(COMPOSE) $(PROFILES) down --remove-orphans

shell: ## Open a shell in the dev container
	$(COMPOSE) exec dev bash

prune: down ## Remove the stack, the project's images and dangling Fieldproof build layers
	$(COMPOSE) --profile images $(PROFILES) down --remove-orphans --volumes --rmi all
	docker image prune -f --filter label=org.opencontainers.image.vendor=Fieldproof
	@echo "Build cache is shared across projects; reclaim it with: docker builder prune"
