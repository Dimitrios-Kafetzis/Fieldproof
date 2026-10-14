# ADR-0009 — Python for orchestration, C++ for Gazebo plugins, TypeScript for the cockpit

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P0-T04, P0-T07 and every implementation task

## Context
Gazebo plugins require C++ against the gz ABI. The scenario engine is I/O-bound orchestration with a
rich schema and plugin ecosystem. The cockpit is a browser application.

## Decision
We will write Gazebo plugins and high-rate bridge adapters in C++17, the scenario engine, CLI, bridge
configuration and tooling in Python 3.12 (uv, Pydantic v2, Typer, rclpy), and the cockpit in
TypeScript (strict) with React and Three.js. Shell scripts are limited to host setup and demos.

## Alternatives considered
- **C++ engine** — faster, but the engine is not CPU-bound and Python's ecosystem (Docker SDK, Pydantic, pytest) accelerates development substantially.
- **Rust engine** — attractive safety and performance, but ROS 2 Rust bindings and team familiarity are weaker; may be reconsidered for hot paths.
- **Python cockpit (Dash/Streamlit)** — insufficient for a real-time 3D twin.

## Consequences
- Three toolchains in one repo; enforced by shared pre-commit and CI (P0-T04).
- Python performance-sensitive paths (assertion evaluation at high rates) use NumPy or move to C++ nodes if profiling demands.
- Type checking everywhere: mypy strict, clang-tidy, TypeScript strict.
