# Documentation site

Task P0-T06 publishes the Markdown under `docs/` as this site, built by
[MkDocs](https://www.mkdocs.org/) with the
[Material](https://squidfunk.github.io/mkdocs-material/) theme and deployed to GitHub Pages at
<https://dimitrios-kafetzis.github.io/Fieldproof/> on every merge to `main`
([ADR-0008](../adr/0008-monorepo-apache2-github.md)). The site is a view of the repository, not a
second copy of it: a page is changed by changing the file in `docs/` in the same pull request as
the code it describes ([04-engineering-standards.md](../plan/04-engineering-standards.md#documentation)).

## Layout

| Path | Contents |
| --- | --- |
| `mkdocs.yml` | site configuration: theme, Markdown extensions, navigation, strict validation |
| `docs/index.md` | the landing page |
| `docs/plan/` | the planning documents and the status board |
| `docs/adr/` | the Architecture Decision Records; `README.md` is the generated index |
| `docs/setup/` | this section: host, tooling, containers, CI/CD, documentation |
| `docs/reference/` | reference pages, generated from the schemas as the engine takes shape |
| `tools/docs/` | the pinned toolchain (`pyproject.toml`, `uv.lock`), the ADR index generator and the MkDocs hooks |
| `tests/docs/` | the documentation tests |
| `.lychee.toml` | the link checker configuration |

The toolchain is a locked `uv` project: `mkdocs-material` is pinned in `tools/docs/pyproject.toml`
and everything it pulls in is pinned in `tools/docs/uv.lock`, so `make docs` builds the same site
on every machine and on the runner from an environment under `tools/docs/.venv`. Dependabot
proposes the bumps weekly ([ci.md](ci.md#dependabot)); `uv lock --upgrade --project tools/docs`
does it by hand.

## Everyday commands

| Command | Does |
| --- | --- |
| `make docs` | build the site into `site/` with `mkdocs build --strict` |
| `make docs-serve` | serve it on <http://127.0.0.1:8000> with live reload (also strict) |
| `make docs-links` | build, then check every link of the built site with lychee in a pinned container, external links included; `LYCHEE_ARGS=--offline` checks only the internal ones |
| `make test-docs` | run `tests/docs/test_docs.sh` (part of `make test`) |

`site/` is build output and ignored by git.

## Writing pages

- Prose wraps at 100 columns; markdownlint enforces the rules in `.markdownlint-cli2.yaml`
  ([tooling.md](tooling.md)). Tables, code blocks and headings may run longer.
- Link to other pages with relative Markdown links (`[host](host.md)`,
  `[ADR-0001](../adr/0001-gazebo-harmonic-as-physics-and-sensor-engine.md)`), anchors included
  (`ci.md#releases`). The strict build fails on a link to a missing page or anchor, on an absolute
  link, and on a page that is not in the navigation, so a broken link never reaches `main`.
- Link to files outside `docs/` (scripts, workflows, Dockerfiles) with their full GitHub URL;
  the site cannot resolve a relative path into the repository.
- Diagrams are Mermaid fences so they diff; Material renders them in the browser:

    ````markdown
    ```mermaid
    flowchart LR
        push["push to main"] --> build["mkdocs build --strict"] --> deploy["GitHub Pages"]
    ```
    ````

    ```mermaid
    flowchart LR
        pr["pull request"] --> ci["CI: Docs job<br/>make test-docs"]
        pr --> site["Docs: build + link check<br/>site artifact"]
        push["push to main"] --> deploy["Docs: build + link check<br/>deploy to GitHub Pages"]
    ```

- Admonitions (`!!! note "Title"`), content tabs and the other
  [Material extensions](https://squidfunk.github.io/mkdocs-material/reference/) enabled in
  `mkdocs.yml` are available; the planning documents use plain Markdown so they read well on
  GitHub too.
- A new page goes into the `nav` of `mkdocs.yml`, except ADRs (next section).

## The ADR index

`docs/adr/README.md` is generated: `tools/docs/adr_index.py` reads the number and title from the
first line of every `docs/adr/NNNN-*.md` (`# ADR-NNNN — Title`) and the status and date from
the metadata list below it, and writes the table. Three things keep it current:

1. the `adr-index` pre-commit hook rewrites it whenever an ADR changes (`make fmt` runs it too);
2. the MkDocs hook in `tools/docs/mkdocs_hooks.py` regenerates it before every build and fills
   the *Decisions* section of the navigation with one entry per ADR, so a new ADR needs no edit
   to `mkdocs.yml`;
3. `tests/docs/test_docs.sh` runs `tools/docs/adr_index.py --check` and fails when the committed
   file is stale.

A new ADR is therefore just a copy of `0000-adr-template.md` with the next number, a heading in
the template's shape and a status line; a status such as `Superseded by ADR-0012` becomes a link
in the index when that ADR exists.

## Continuous integration and deployment

| Workflow | Runs on | Does |
| --- | --- | --- |
| CI, job *Docs* | every pull request, push to `main` | `make test-docs`: the strict build and the tests above; part of the required `CI` check ([ci.md](ci.md#required-status-checks)) |
| Docs | pull requests and pushes to `main` that touch `docs/`, `mkdocs.yml`, `tools/docs/` or the workflow; manual | `make docs`, `make docs-links` (external links too, so it needs the network and is not a required check), then uploads the site as a Pages artifact; on `main` the *Deploy* job publishes it |

The Pages deployment uses the repository setting *build and deployment from GitHub Actions*; the
`deploy` job has `pages: write` and `id-token: write` and runs in the `github-pages`
environment, whose URL is the live site. The first deployment after a change to
`docs/` on `main` is visible within a minute of the workflow finishing.

Every action is pinned to a commit SHA, as everywhere in `.github/workflows`
([ci.md](ci.md#workflows)).

## Tests

`tests/docs/test_docs.sh` (`make test-docs`) checks that the ADR index generator handles a
scratch directory of ADRs (table, template excluded, status links, malformed files rejected,
idempotent rewrite) and that the committed index is current; that `make docs` succeeds and
produces a page for every Markdown file under `docs/`, the ADR table, the navigation entries and
a rendered Mermaid block; that a broken link or a page missing from the navigation fails the
strict build; that `uv.lock` matches `pyproject.toml`; that `docs.yml` deploys only from `main`
with the Pages permissions; and, when Docker is available, that lychee finds no broken internal
link or fragment in the built site.

## Troubleshooting

**`make docs` fails with `Doc file 'x.md' contains a link 'y.md', but the target is not found`.**
The link is relative to the page that contains it; fix the path or add the page. For files
outside `docs/` use the GitHub URL.

**`... contains a link to '#anchor' which is not found`.** Anchors are the heading text in lower
case with spaces as hyphens and punctuation removed (`## Required status checks` is
`#required-status-checks`).

**`The following pages exist in the docs directory, but are not included in the "nav"`.** Add
the page to `nav` in `mkdocs.yml`; ADRs are added automatically.

**`make docs-links` reports `429 Too Many Requests` or a timeout.** Rate limits and transient
network errors are accepted by `.lychee.toml` (`accept` and retries); a persistent failure on one
URL means the target moved. Rerun with `LYCHEE_ARGS=--offline` to confirm the internal links.

**`adr-index` fails on commit.** The hook rewrote `docs/adr/README.md`; stage it and commit
again. If it reports a malformed ADR, the first line must be `# ADR-NNNN — Title` with the file's
own number and the metadata list must contain `- **Status:**` and `- **Date:**`.

**`mkdocs serve` shows a stale ADR list.** The index and navigation are rebuilt on every file
change under `docs/`; a new ADR file is picked up as soon as it is saved.
