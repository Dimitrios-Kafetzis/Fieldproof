# ADR-0004 — One container per vehicle, built from the vehicle's own repository

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P0-T02, P0-T03, P1-T04, P2-T01, P2-T02, P6-*

## Context

The core promise is running the real codebase unmodified. Vehicle stacks have their own dependencies,
ROS distributions, DDS configurations and resource footprints. Multiple vehicles share one host.

## Decision

We will run each vehicle in its own container, built from the vehicle's repository at a pinned commit
(or a supplied image), with its own network namespace, DDS profile, CPU and memory budget, and a
manifest (`robot.yaml`) that declares how hardware drivers are swapped for bridge-provided topics.
Nothing from ROS or Gazebo is installed on the host.

## Alternatives considered

- **All nodes in one process space / one container** — simplest, but dependencies collide and
  network and resource isolation are impossible.
- **Virtual machines per vehicle** — strongest isolation, too heavy for fleets on one workstation;
  reserved for ARM64 system emulation in Phase 6.
- **Kubernetes from the start** — premature; the orchestrator uses the Docker API now and keeps a
  topology model that could target Kubernetes later.

## Consequences

- Network emulation (netem) and compute budgets (cgroups) attach naturally to containers.
- Image builds are a first-class concern: generator, caching, registry.
- Container startup ordering and readiness gating become engine responsibilities.
