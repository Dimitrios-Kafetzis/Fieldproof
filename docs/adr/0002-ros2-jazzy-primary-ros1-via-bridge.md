# ADR-0002 — ROS 2 Jazzy is the primary interface; ROS 1 is supported through a bridge

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P0-T03, P1-T03, P6-T05

## Context
Vehicle codebases exist in both ROS 1 and ROS 2. ROS 1 Noetic reached end of life in May 2025 and
only runs on Ubuntu 20.04. Gazebo Harmonic's native ROS integration targets ROS 2.

## Decision
We will make ROS 2 Jazzy (LTS until 2029) on Ubuntu 24.04 the primary and only native interface of
the bridge and engine layers. ROS 1 vehicles run in Noetic containers connected through `ros1_bridge`
with a manifest-declared topic mapping.

## Alternatives considered
- **Dual native support** — doubles bridge and test surface for an EOL platform.
- **Drop ROS 1** — excludes real fleets still in the field.
- **ROS 2 Humble** — longer support in some fleets, but Humble pairs with Ubuntu 22.04 and Gazebo Fortress; Jazzy matches the host OS and Harmonic.

## Consequences
- Simpler core; ROS 1 cost is paid once in P6-T05.
- QoS semantics do not exist in ROS 1; the bridge documents the mapping and its limits.
- Vehicles on Humble can still be onboarded: their containers use Humble images and talk DDS to a Jazzy bridge, with interface compatibility verified by the contract tests.
