# Fieldproof engine

The scenario engine and the `fieldproof` command line interface: layer L4 of the
[architecture](../docs/plan/01-architecture.md). Python 3.12, managed with uv, Pydantic v2 models,
Typer CLI and Rich output ([ADR-0009](../docs/adr/0009-languages-per-layer.md)).

```bash
uv sync --locked --project engine            # create engine/.venv from uv.lock
uv run --project engine fieldproof --version  # the CLI
make test-engine                              # pytest (from the repository root)
```

The layout is `src/fieldproof/` for the package, `tests/` for its unit tests and `schemas/` for
the JSON Schema exports of its Pydantic models (`robot.schema.json`, written by
`make manifest-schema`). P0-T07 ships the package skeleton and `fieldproof --version`, P1-T03
`fieldproof bridge gen` (the bridge generator, installed from `bridge/fieldproof_bridge`), P1-T04
the vehicle manifest schema v0 (`fieldproof.manifest`, `fieldproof manifest validate|schema`) and
the vehicle image generator (`fieldproof.vehicle`, `fieldproof vehicle dockerfile|build|run`,
[manifest.md](../docs/reference/manifest.md)). The scenario schema, runner, orchestrator, event
injector, assertions and reports arrive with the Phase 1 and Phase 2 tasks of the
[backlog](../docs/plan/03-task-backlog.md). Lint and type-check settings (ruff, mypy strict) come
from the repository root `pyproject.toml`; the hooks run through `make lint`.
