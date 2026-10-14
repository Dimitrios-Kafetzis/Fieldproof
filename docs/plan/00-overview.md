# 00 — Overview

## Vision

Fieldproof lets a robotics team run the exact software that will ship on their vehicles inside a
controlled, repeatable, observable environment, and gives them quantified evidence that a mission will
behave in the field as it did in emulation. It is a test harness first and a visualisation tool second.

## Problem statement

Field testing is expensive, slow, weather-dependent and unsafe to fail in. Teams today either test
too little, or test against hand-written mocks that drift from the real stack, or use a simulator so
far from the vehicle's software architecture that passing it proves little. Fieldproof removes the
drift by running the real stack and removes the proof gap by recording, asserting and comparing.

## Principles

1. **Real code, untouched.** Only hardware driver nodes are replaced. Same launch files, same
   parameters, same message types, same DDS configuration as the vehicle.
2. **Scenario as test.** Every scenario has assertions and a pass/fail result. If it cannot fail, it
   is a demo, not a test.
3. **Reproducible by default.** Pinned images, seeded randomness, simulated time, recorded inputs.
   Two runs of the same scenario on the same commit produce comparable results.
4. **Observable everything.** All topics, events, assertions and resource metrics are recorded to
   MCAP and visible live in the cockpit.
5. **Decoupled layers.** The simulator does not know about the scenario engine, the scenario engine
   does not know about the cockpit, the robot code knows about none of them.
6. **Honest fidelity.** Every scenario declares which fidelity tier it needs. Reports state the tier
   and the known approximations so nobody mistakes a SIL pass for a field pass.
7. **Boring technology where possible.** Gazebo, ROS 2, Docker, Python, TypeScript. Novelty is spent
   only on the scenario engine and the cockpit, which are the product.

## Fidelity tiers

| Tier | What runs | Timing | Use | Limits |
|---|---|---|---|---|
| **T1 SIL** (software in the loop) | Real source rebuilt for x86-64 in a container | Sim time, faster or slower than real time | CI on every commit, regression, parameter sweeps | Different CPU architecture and compiler output. Scheduling differs from target |
| **T2 Emulated target** | The actual ARM64 image under QEMU (user-mode binfmt for containers, system-mode with KVM for full images) | Real-time locked, with compute budget matching the target | Pre-release validation, timing bugs, binary-identical confidence | GPU code (CUDA on Jetson) cannot be emulated. Slower |
| **T3 HIL** (hardware in the loop) | Real flight controller and optionally companion computer on the bench | Wall clock | Final gate before field | Needs hardware present. One run at a time |

A fourth input, **log replay**, feeds recorded field data (MCAP) into the stack open-loop. It is not a
tier but a scenario source, used for regression against real sensor data.

## Domains

| Domain | Vehicles | Physics needs | Autopilot/stack |
|---|---|---|---|
| Ground (UGV) | Differential drive, skid steer, Ackermann | Rigid body, wheel friction, terrain | Nav2, custom |
| Aerial (UAV) | Quadrotor, fixed wing, VTOL | Aerodynamics, wind, propulsion | PX4, ArduPilot via SITL, MAVROS or MAVSDK companions |
| Surface (USV) | Catamaran, monohull | Hydrodynamics, buoyancy, waves, wind, currents | ArduPilot Rover marine frames, custom |
| Underwater (UUV) | ROV, AUV | Added mass, drag, buoyancy, acoustics | Out of scope for v1, architecture leaves room |

## Non-goals for v1

- Photorealistic rendering for synthetic training data. Gazebo's ogre2 is sufficient for sensor
  simulation; neural rendering and Unreal integrations are not planned.
- Replacing the autopilot's own SITL. PX4 and ArduPilot SITL are used as-is.
- Cloud multi-tenant SaaS. Single workstation first, multi-machine scale-out later.
- Hardware design or electrical simulation.

## Glossary

- **Scenario** — a YAML document describing world, vehicles, timeline of events and assertions.
- **Run** — one execution of a scenario, with a unique ID, MCAP recording and report.
- **Vehicle manifest** — `robot.yaml` describing how an external robot repository plugs in: image,
  launch entrypoint, parameters, driver-swap map, resource budget, fidelity tier.
- **Driver swap** — replacing a hardware driver node by a bridge node publishing the same interface.
- **Event** — a timed or conditional perturbation injected during a run (wind gust, GPS denial,
  obstacle spawn, link loss).
- **Assertion** — a predicate evaluated over the run that determines pass/fail.
- **KPI** — a measured quantity reported without pass/fail semantics (mission time, path length).
- **Cockpit** — the browser-based 3D GUI.
- **RTF** — real-time factor, simulated seconds per wall second.
