# ADR-0008 — Single public monorepo on GitHub under the Apache-2.0 licence

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** maintainer
- **Tasks affected:** P0-T01, P0-T05, P0-T07

## Context
The project spans C++ Gazebo plugins, Python engine, TypeScript cockpit, container definitions and
documentation that evolve together. ROS ecosystem projects are predominantly Apache-2.0 or BSD, and
compatibility with Gazebo (Apache-2.0), PX4 (BSD-3), ArduPilot (GPL-3, used as a separate process) and
VRX (Apache-2.0) matters.

## Decision
We will keep all layers in one repository, `github.com/Dimitrios-Kafetzis/Fieldproof`, licensed Apache-2.0, hosted on GitHub
with GitHub Actions for CI, GHCR for images, GitHub Pages for docs, and release-please for SemVer
releases. Example vehicle repositories used for onboarding tests live in separate repositories.

## Alternatives considered
- **Multi-repo per layer** — independent release cadences, but cross-layer changes (schema → engine → cockpit) become multi-PR coordination for a small team.
- **GPL-3** — would constrain adoption by commercial robotics teams; ArduPilot's GPL is respected by running it as an unmodified separate process.
- **MIT/BSD** — acceptable, but Apache-2.0 adds the explicit patent grant customary in this ecosystem.

## Consequences
- One CI pipeline with path filters to keep PR runtime acceptable.
- Clear `LICENSES/` handling for vendored assets with other licences (CC-BY etc.).
- Repo version follows the CLI; schema and plugin APIs carry their own versions.
