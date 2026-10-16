# Developer tooling and quality gates

Task P0-T04 makes the rules in [04-engineering-standards.md](../plan/04-engineering-standards.md)
enforceable automatically. One pre-commit configuration drives every linter and formatter for the
three toolchains, commitlint checks commit messages, gitleaks scans for secrets, and a `Makefile`
wraps the common commands. CI ([ci.md](ci.md)) runs the same targets, so a change that passes
locally passes in CI.

Nothing is installed on the host beyond what [host.md](host.md) already provides: pre-commit
creates its own environments in `~/.cache/pre-commit` (Python virtualenvs, a Node prefix, a Go
toolchain for gitleaks) and hadolint runs from its container image.

## Install the hooks once

```bash
make setup          # pre-commit install --install-hooks: pre-commit and commit-msg hooks
```

The first run downloads every hook environment and takes a few minutes. Afterwards each commit
runs the hooks on the staged files only, and the commit message is checked before the commit is
created.

## Everyday commands

| Command | What it does |
| --- | --- |
| `make lint` | Every gate: all hooks on all tracked files, commit messages on the branch, full-history secret scan |
| `make lint-files` | All pre-commit hooks on all tracked files (`pre-commit run --all-files`) |
| `make lint-commits` | commitlint on every commit since `origin/main` (`BASE_REF=<ref>` to change the base) |
| `make secrets` | gitleaks over the whole git history, from the pinned container image |
| `make lint-title TITLE="..."` | commitlint on a pull request title (the squash-merge subject; see [ci.md](ci.md#pull-request-titles)) |
| `make fmt` | Only the formatters: whitespace fixers, ruff, clang-format, cmake-format, prettier, the ADR index |
| `make test` | Every test suite: tooling, CI workflow, documentation, layout, engine (pytest), cockpit (Vitest), ROS workspaces (ctest and pytest in the dev container), host and image tests ([workspaces.md](workspaces.md)) |
| `make test-engine`, `make test-cockpit`, `make test-ros`, `make tidy` | One layer at a time: see [workspaces.md](workspaces.md#one-command-per-layer) |
| `make docs`, `make docs-serve` | Build or preview the documentation site ([docs.md](docs.md)) |
| `make clean` | Delete the hook environments; they are rebuilt on the next run |
| `SKIP=mypy git commit …` | Skip one hook for one commit; use sparingly and say why in the PR |

## What runs on which files

| Gate | Files | Configuration | Notes |
| --- | --- | --- | --- |
| pre-commit-hooks | all | `.pre-commit-config.yaml` | trailing whitespace, final newline, LF endings, large files, merge markers, shebangs, private keys, YAML/JSON/TOML syntax |
| ruff check, ruff format | `*.py`, `*.pyi` | `pyproject.toml` | Python 3.12, 100 columns, Google docstrings, bandit and pylint rule sets, auto-fix on commit |
| mypy | `*.py` | `pyproject.toml` | `strict = true`; `mypy_path` resolves the engine and bridge packages; untyped ROS 2 packages are listed as ignored imports; the engine's dependencies are listed in the hook at the versions of `engine/uv.lock` ([workspaces.md](workspaces.md#the-engine-engine)) |
| clang-format | `*.cpp`, `*.hpp`, `*.c`, `*.h` | `.clang-format` | Google style, 100 columns, C++17, left pointer alignment, grouped includes |
| clang-tidy | C++ in the `fieldproof_*` packages of `sim/`, `bridge/` | `.clang-tidy` | `make tidy`: runs inside the dev container against the `compile_commands.json` of the colcon build, warnings are errors; not a pre-commit hook |
| cmake-format, cmake-lint | `CMakeLists.txt`, `*.cmake` | `.cmake-format.yaml` | 100 columns, lower-case commands |
| eslint | `*.ts`, `*.tsx`, `*.js`, `*.mjs`, `*.cjs` | `eslint.config.cjs` | typescript-eslint strict and stylistic presets, `--max-warnings=0`, TypeScript at the cockpit's version; the cockpit adds React and type-aware rules in its own workspace (P3-T02) |
| prettier | TS, JS, JSON, CSS, HTML | `.prettierrc.yaml`, `.prettierignore` | 100 columns, double quotes, trailing commas; not YAML or Markdown |
| yamllint | `*.yaml`, `*.yml` | `.yamllint.yaml` | strict, 120 columns, two-space indent, `on`/`off` allowed for GitHub workflows; `pnpm-lock.yaml` is generated and skipped |
| markdownlint-cli2 | `*.md` | `.markdownlint-cli2.yaml` | 100-column prose, tables and code exempt; `CODE_OF_CONDUCT.md` is upstream text and skipped |
| adr-index | `docs/adr/*.md` | `tools/docs/adr_index.py` | regenerates `docs/adr/README.md` from the ADRs and fails the commit once so the result gets staged; rejects an ADR whose heading, status or date line is malformed ([docs.md](docs.md#the-adr-index)) |
| shellcheck | `*.sh`, scripts with a shell shebang | hook args | severity `style`, external sources followed |
| hadolint | `Dockerfile*` | `.hadolint.yaml` | container image; `:latest`, unpinned apt packages, missing `--no-install-recommends` and a root final user are errors |
| check-jsonschema | `.github/workflows/*.yml`, `.github/dependabot.yml` | hook defaults | GitHub Actions and Dependabot schemas |
| actionlint | `.github/workflows/*.yml` | hook defaults | workflow semantics: expressions, job references, runner labels, shellcheck on `run` steps |
| gitleaks | staged changes on commit; full history in `make secrets` | `.gitleaks.toml` | default rules plus repository allowlists |
| commitlint | commit message | `commitlint.config.mjs` | see below |

## Commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) and are checked by
the `commit-msg` hook:

```text
<type>(<scope>): <subject>

[body]

[footer]
```

- **type**: `feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, `chore`, `perf`, `revert`.
- **scope** (required): the layer `engine`, `sim`, `bridge`, `robots`, `cockpit`, `docker`, `docs`,
  `ci`, or for repository-wide changes `repo`, `plan`, `tools`, `deps`, `release`.
- **subject**: imperative, lower-case start, no trailing full stop, header at most 100 characters.
- Breaking changes carry `!` after the scope: `feat(engine)!: rename the scenario apiVersion`.
- Body and footer lines wrap at 100 characters. Reference the task in the body: `Task: P0-T04`.
- Squash-merge commits carry the pull request title, so titles follow the same rules and are
  checked by CI ([ci.md](ci.md#pull-request-titles)). The four squash commits that predate the
  rule (`P0-T04 Developer tooling and quality gates (#3)`) stay exempt.

Examples that pass: `feat(engine): add scenario loader`, `fix(cockpit)!: drop the legacy protocol`,
`docs(plan): mark P0-T04 in review`. Examples that fail: `Added some stuff`, `feat: scope is
required`, `feat(kitchen): unknown scope`, `feat(engine): Capitalised subject`.

## Updating the hooks

Every hook is pinned to a tag and every Node package to an exact version. Update them together
in a `build(deps)` PR: run `pre-commit autoupdate`, bump the `additional_dependencies` of the
eslint, prettier and commitlint hooks to the matching npm releases, run `make lint` and
`make test`, and record notable rule changes in the PR body. The mypy hook's packages follow
`engine/uv.lock` and the eslint hook's TypeScript follows the cockpit; `make test-layout` fails
when they drift, so bump them in the same PR as the workspace dependency.

## Tests

`tests/tooling/test_quality_gates.sh` (run by `make test-tooling`) checks that the configuration
validates and passes on the repository, that the installed `commit-msg` hook rejects a
non-conventional message and accepts a conventional one, that each gate catches a deliberately bad
file in a scratch copy of the repository, and that the commitlint rules accept and reject the
message shapes documented above.

## Troubleshooting

- **`eslint` cannot find `@eslint/js`**: the root ESLint configuration is CommonJS
  (`eslint.config.cjs`) because pre-commit exposes the hook's packages through `NODE_PATH`, which
  only `require()` honours. Keep it CommonJS; workspaces with their own `node_modules` may use ESM.
- **hadolint fails with a Docker error**: the hook runs the hadolint image, so the Docker daemon
  must be reachable; see the host guide if your shell lacks the `docker` group.
- **A hook is stale or broken after an update**: `make clean` removes every environment; the next
  run rebuilds them.
- **Skipping a hook**: `SKIP=<hook-id> git commit …` for one commit, or `git commit --no-verify`
  as a last resort. CI runs the full set, so skipped hooks fail there.
