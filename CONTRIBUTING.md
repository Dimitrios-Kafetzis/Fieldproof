# Contributing to Fieldproof

Thank you for considering a contribution. Fieldproof is built task by task against a public plan;
please read the plan before proposing changes.

## Before you start

- Read [docs/plan/00-overview.md](docs/plan/00-overview.md) and
  [docs/plan/04-engineering-standards.md](docs/plan/04-engineering-standards.md).
- Prepare your machine with [docs/setup/host.md](docs/setup/host.md) and install the hooks with
  `make setup` ([docs/setup/tooling.md](docs/setup/tooling.md)).
- Check [docs/plan/STATUS.md](docs/plan/STATUS.md) and the open issues so work is not duplicated.
- Architectural changes need an ADR first. Copy
  [docs/adr/0000-adr-template.md](docs/adr/0000-adr-template.md), open a PR on a branch named
  `adr/<nnnn>-<slug>`, and wait for it to be accepted before implementing.

## Workflow

1. Fork (external contributors) or branch (maintainers): `task/<id>-<slug>`, `fix/<slug>` or `adr/<nnnn>-<slug>`.
2. Make small commits using [Conventional Commits](https://www.conventionalcommits.org/):
   `type(scope): summary`, where `scope` is one of `engine`, `sim`, `bridge`, `robots`, `cockpit`,
   `docker`, `docs`, `ci`, or `repo`, `plan`, `tools`, `deps`, `release` for repository-wide
   changes. Add `!` for breaking changes. The `commit-msg` hook rejects anything else.
3. Add or update tests at the appropriate level of the testing pyramid. Code without tests is not merged.
4. Run `make setup` once to install the git hooks, then `make lint` and `make test` before
   pushing; `make fmt` applies the formatters. See [docs/setup/tooling.md](docs/setup/tooling.md).
5. Update documentation in the same PR.
6. Open a pull request using the template. Fill every checklist item honestly; "not applicable" is
   acceptable with a reason.
7. One approval from a code owner and green CI are required. PRs are squash-merged.

## Pull request expectations

- Title: a Conventional Commits summary, `type(scope): summary`, because it becomes the squash
  commit on `main` and drives the changelog. CI checks it (`make lint-title TITLE="..."`).
- Body: what changed, why, how it was verified (paste real command output), and links to the task,
  issue or ADR (`Task: P0-T05` for planned tasks).
- Keep PRs focused. Unrelated improvements go in their own PR or an issue.

## Reporting bugs and requesting features

Use the issue templates. For security problems, follow [SECURITY.md](SECURITY.md) instead of opening
a public issue.

## Licence

By contributing you agree that your contributions are licensed under the Apache License 2.0 that
covers the project. Vendored third-party assets must carry their own licence file under `LICENSES/`.
