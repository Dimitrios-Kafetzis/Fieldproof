# ADR-0003 — A browser-based cockpit built with Three.js, speaking the Foxglove WebSocket protocol

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P3-*, P5-T05, P7-T02

## Context

The product needs a 3D GUI for running and observing scenarios. Gazebo GUI and RViz 2 exist but are
desktop Qt applications bound to one machine, hard to extend into a scenario cockpit, and not
shareable. The GUI must render a digital twin, sensor overlays, timelines and controls, and replay
recordings.

## Decision

We will build our own cockpit as a TypeScript web application (React, Three.js via react-three-fiber)
served by the engine, consuming topics through the Foxglove WebSocket protocol and controlling the
engine through a small REST/WebSocket API. The cockpit renders a twin for humans; sensor images used
by the robot stack are rendered by Gazebo.

## Alternatives considered

- **Gazebo GUI + RViz 2 + Foxglove Studio** — zero build cost, but no scenario control, no unified
  product, desktop-only.
- **Native Qt/Qt3D** — closer to RViz, harder to distribute, smaller contributor pool.
- **Unity/Godot front-end** — heavy runtime for a visualiser, duplicate asset pipeline.
- **Custom WebSocket protocol** — would lose Foxglove Studio compatibility for free during development.

## Consequences

- Foxglove Studio works as a secondary client from day one; our protocol surface stays standard.
- Two renderers (Gazebo for sensors, Three.js for humans) must share assets; a glTF conversion
  pipeline is required (P3-T03).
- Cockpit work is deferred to Phase 3 so the headless engine is proven first.
