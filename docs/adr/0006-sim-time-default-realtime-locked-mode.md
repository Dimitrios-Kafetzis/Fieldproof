# ADR-0006 — Simulated time by default, with a real-time-locked mode

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P1-T03, P1-T05, P2-T04, P6-*

## Context

Reproducibility and CI speed favour simulated time published on `/clock` with `use_sim_time`. Field
vehicles run on wall clock, and some bugs (timeouts, watchdogs, timer drift) only appear with real
timing. Heavy worlds may not sustain real-time factor 1.0.

## Decision

We will run scenarios in simulated time by default with a target real-time factor and optional
lockstep, and provide a real-time-locked mode that pins RTF to 1.0, monitors drift, and fails or warns
according to the scenario's declared tolerance. Both modes record RTF and drift into the run, and the
report states which mode was used. `use_sim_time` is injected by parameter override, never by editing
the vehicle's launch files.

## Alternatives considered

- **Wall clock only** — mirrors the field, but non-reproducible and slow in CI.
- **Sim time only** — reproducible, but hides timing bugs and misrepresents field behaviour.

## Consequences

- Two code paths in the clock manager, both tested (P2-T04).
- Fidelity tiers T2 and T3 require real-time-locked mode.
- Scenarios declare `time.mode` and `time.tolerance`; the default is `sim`.
