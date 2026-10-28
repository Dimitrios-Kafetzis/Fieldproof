# Fieldproof engine

The scenario engine and the `fieldproof` command line interface: layer L4 of the
[architecture](../docs/plan/01-architecture.md). Python 3.12, managed with uv, Pydantic v2 models,
Typer CLI and Rich output ([ADR-0009](../docs/adr/0009-languages-per-layer.md)).

```bash
uv sync --locked --project engine            # create engine/.venv from uv.lock
uv run --project engine fieldproof --version  # the CLI
make test-engine                              # pytest with coverage (from the repository root)
```

The layout is `src/fieldproof/` for the package, `tests/` for its unit tests and `schemas/` for
the JSON Schema exports of its Pydantic models (`robot.schema.json`, written by
`make manifest-schema`). P0-T07 ships the package skeleton and `fieldproof --version`, P1-T03
`fieldproof bridge gen` (the bridge generator, installed from `bridge/fieldproof_bridge`), P1-T04
the vehicle manifest schema v0 (`fieldproof.manifest`, `fieldproof manifest validate|schema`) and
the vehicle image generator (`fieldproof.vehicle`, `fieldproof vehicle dockerfile|build|run`,
[manifest.md](../docs/reference/manifest.md)), P1-T05 the scenario schema v0
(`fieldproof.scenario`, `fieldproof validate`, `fieldproof schema`), the workspace configuration
(`fieldproof.config`, `fieldproof.toml`) and the runner v0 (`fieldproof.runner` on the
`fieldproof.backend` protocol, `fieldproof.docker_backend` with the Docker SDK, `fieldproof run`,
[scenarios.md](../docs/reference/scenarios.md)), P1-T06 the assertions, the recorder's observer
and the reports (`fieldproof.assertions`, `fieldproof.observer`, `fieldproof.report`), P2-T01
the fleet: namespaces end to end, a ROS domain per run, the discovery server, DDS profiles,
budgets, the parallel start and concurrent actions in `fieldproof.runner`
([scenarios.md#fleets](../docs/reference/scenarios.md#fleets)), P2-T02 the radio links
(`fieldproof.network`, `fieldproof.ground`, [network.md](../docs/reference/network.md)), P2-T03
the event injection framework: the timeline DSL (`fieldproof.dsl`), the scheduler
(`fieldproof.schedule`), the event plugin API, registry and built-in events
(`fieldproof.events`, `fieldproof events`) and the example plugin under `examples/plugins`
([events.md](../docs/reference/events.md)), P2-T06 the run matrix (`fieldproof.matrix`,
`fieldproof matrix`), the scenario overrides (`fieldproof.overrides`, `fieldproof run --set`),
the run database (`fieldproof.rundb`, `fieldproof runs list|show|diff|index`) and the HTML
reports (`fieldproof.html`, [matrix.md](../docs/reference/matrix.md)). The remaining Phase 2
tasks of the [backlog](../docs/plan/03-task-backlog.md) follow. Lint and type-check settings
(ruff, mypy strict) come from the repository root `pyproject.toml`; the hooks run through
`make lint`.
