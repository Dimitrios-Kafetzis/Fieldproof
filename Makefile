# Fieldproof developer entry points (task P0-T04). Run `make` for the list of targets.
#
# Everything here delegates to pre-commit (hooks pinned in .pre-commit-config.yaml) or to a
# container, so nothing beyond Docker, Node and pre-commit itself is installed on the host.
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

.PHONY: help setup lint lint-files lint-commits secrets fmt test test-host test-tooling clean

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

test: test-tooling test-host ## Run every test suite available at this stage of the plan

test-tooling: ## Quality-gate tests: hooks pass on the repo, each gate catches a bad file, commitlint rejects bad messages
	tests/tooling/test_quality_gates.sh

test-host: ## Host prerequisite tests (set FIELDPROOF_SKIP_HOST=1 on machines that are not prepared)
	tests/host/test_host_check.sh

clean: ## Remove pre-commit's installed hook environments (they are rebuilt on the next run)
	$(PRE_COMMIT) clean
