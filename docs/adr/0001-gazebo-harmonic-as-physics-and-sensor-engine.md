# ADR-0001 — Gazebo Harmonic as the physics and sensor engine

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P0-T03, P1-*, P4-*, P5-*

## Context
Fieldproof must simulate ground, aerial and surface vehicles with realistic sensors on a workstation
with an AMD integrated GPU and no NVIDIA hardware. The engine must integrate natively with ROS 2,
with PX4 and ArduPilot SITL, and have a credible hydrodynamics story for USVs. It must run headless
in CI without a GPU.

## Decision
We will use Gazebo Harmonic (gz-sim 8) as the single physics and sensor engine, run as a headless
server in a container with Mesa rendering (software fallback), integrated through `ros_gz`.

## Alternatives considered
- **NVIDIA Isaac Sim** — best rendering and RTX sensors, but requires an NVIDIA GPU; excluded by hardware.
- **Unreal Engine (AirSim/Colosseum, Project AirSim)** — strong visuals, but AirSim is archived, the ecosystem is fragmented, builds are heavy, and USV hydrodynamics are immature.
- **Unity** — good tooling, licensing friction, weaker ROS 2 and autopilot integrations, no marine physics.
- **Webots** — simple and fast, but weaker marine support and smaller autopilot ecosystem.
- **O3DE** — promising ROS 2 gem, but a small community and no mature USV or autopilot glue.
- **CARLA** — automotive-only.

## Consequences
- Single engine covers all three domains via DiffDrive plugins, PX4/ArduPilot Gazebo glue, and VRX plugins.
- Officially paired with ROS 2 Jazzy on Ubuntu 24.04, which fixes our base OS.
- Rendering fidelity is sufficient for sensors, not for photorealistic datasets (explicit non-goal).
- Engine access is wrapped in a thin adapter in the engine layer so a second engine could be added later; we do not design for it now.
- Revisit if the maintainer acquires NVIDIA hardware and photorealism becomes a requirement.
