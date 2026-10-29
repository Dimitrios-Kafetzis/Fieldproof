# Fieldproof targets of the example vehicle repository. Run `make` for the list.
# The engine runs from a Fieldproof checkout (FIELDPROOF_ROOT, ../Fieldproof by default: a clone
# next to this repository), which also supplies the simulator's worlds, models, Fuel assets and
# reference vehicles; the stack images come from the registry at the tag of fieldproof.toml
# (FIELDPROOF_IMAGE_TAG overrides it). Nothing from ROS or Gazebo installs on the host.
SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

FIELDPROOF_ROOT ?= ../Fieldproof
UV ?= uv
FIELDPROOF := $(UV) run --locked --project $(FIELDPROOF_ROOT)/engine fieldproof
SCENARIO ?= scenarios/dock_to_yard.yaml
RUN_FLAGS ?=
BUILD_FLAGS ?=

.PHONY: help check assets validate image run dockerfile clean

help: ## Show this help
	@awk 'BEGIN { FS = ":.*## "; printf "Targets:\n" } /^[a-zA-Z_-]+:.*## / { printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

check: ## The Fieldproof checkout and the engine are where FIELDPROOF_ROOT says
	@test -f "$(FIELDPROOF_ROOT)/engine/pyproject.toml" || { echo "no Fieldproof checkout at $(FIELDPROOF_ROOT) (FIELDPROOF_ROOT=...)" >&2; exit 1; }
	$(FIELDPROOF) --version

assets: check ## Fetch the Gazebo Fuel models the worlds use into the checkout (cached)
	$(MAKE) -C $(FIELDPROOF_ROOT) assets

validate: check ## Validate the manifest and the scenarios against the workspace
	$(FIELDPROOF) manifest validate robot.yaml
	$(FIELDPROOF) validate scenarios/*.yaml

image: check ## Build the vehicle image from robot.yaml (BUILD_FLAGS=--no-cache)
	$(FIELDPROOF) vehicle build robot.yaml $(BUILD_FLAGS)

run: check ## Run a scenario (SCENARIO=scenarios/dock_to_yard.yaml, RUN_FLAGS=--keep|--video|--render software)
	$(FIELDPROOF) run $(SCENARIO) $(RUN_FLAGS)

dockerfile: check ## Refresh docker/, the generated build context (a build without the engine)
	$(FIELDPROOF) vehicle dockerfile robot.yaml --out docker

clean: ## Remove the runs
	rm -rf runs
