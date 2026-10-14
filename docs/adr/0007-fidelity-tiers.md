# ADR-0007 — Three explicit fidelity tiers: SIL, emulated target, HIL

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P1-T04, P6-T01, P6-T02, P6-T04

## Context

"Runs in the emulator" means different things: same source rebuilt for x86; the same ARM64 binaries
under QEMU; or real autopilot hardware on the bench. Users must know which claim a passing run makes.
The dev workstation has KVM and QEMU but no NVIDIA GPU, so Jetson CUDA code cannot be emulated.

## Decision

We will define three fidelity tiers, declared per scenario and per vehicle in the manifest:
T1 SIL (x86 rebuild, sim time allowed), T2 emulated target (ARM64 binaries under QEMU user-mode for
containers or system-mode with KVM for full images, compute budgets, real-time-locked), and T3 HIL
(real flight controller and optionally companion computer). Reports state the tier and known
approximations. Mixed tiers within one vehicle (perception SIL, control emulated) are permitted and
labelled. Log replay is a scenario source available at any tier.

## Alternatives considered

- **SIL only** — insufficient for the project's goal of field-equivalent confidence.
- **Always emulate the target** — too slow for CI and impossible for GPU nodes.

## Consequences

- Phase 1–5 deliver on T1; Phase 6 adds T2 and T3.
- Documentation must be explicit about what each tier does not prove.
- The orchestrator needs a per-vehicle execution backend abstraction (container, QEMU VM,
  passthrough device).
