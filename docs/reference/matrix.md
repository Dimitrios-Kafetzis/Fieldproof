# Run matrix and run database

Task P2-T06 takes the runner to CI scale: `fieldproof matrix` runs a scenario over parameter
sweeps and seeds, several runs at once, failed runs retried and flagged when flaky, with one
JUnit document, a Markdown and an HTML report for the whole; every run records itself in a
SQLite **run database** that `fieldproof runs list|show|diff|index` reads; `fieldproof run
--set` varies a scenario without a copy of the file; and every run writes an HTML report next
to the Markdown one. The architecture names the pieces
([01-architecture.md](../plan/01-architecture.md#l4-scenario-engine): the run database, the
reporter, `fieldproof matrix`).

## Scenario overrides

`fieldproof run SCENARIO --set path=value` (and `fieldproof validate --set`) replaces a value of
the scenario document before it is validated. A path is a dotted sequence of keys; a list is
entered by an index (`vehicles[0]`) or by the `name` of one of its items (`vehicles[ugv1]`,
`timeline[drive_ahead]`, `assertions[reached_goal]`); the value is YAML, so `2`, `2.5`, `true`,
`"2"`, `[1, 2]`, `{x: 1, y: 2}` and `null` are what they look like. A missing mapping key is
created (`outputs.video=true` on a scenario without an `outputs` section works); a missing
list item is an error. `--set` is repeatable and applies in order.

```bash
fieldproof run examples/scenarios/ugv_nav_to_goal.yaml --set seed=3 --set time.rtf=2
fieldproof run examples/scenarios/ugv_nav_to_goal.yaml --set 'vehicles[ugv1].resources.cpus=1'
fieldproof validate examples/scenarios/ugv_nav_to_goal.yaml --set 'timeline[drive_ahead].goal.x=12'
```

The run is of the effective document: `scenario.yaml` in the run directory holds it (not the
file as it is on disk), its text is what the run id's scenario hash is taken from (two runs with
different overrides never share a hash), `run.json` lists the `overrides` by path, and the
JUnit document and the database carry them. Relative paths in the scenario (vehicle manifests)
still resolve from the file's own directory.

## The matrix file

A matrix is a YAML file, `apiVersion: fieldproof.dev/v0alpha1`, `kind: Matrix`, next to or
away from the scenario it varies; `examples/matrices/ugv_nav_matrix.yaml` is the first:

```yaml
apiVersion: fieldproof.dev/v0alpha1
kind: Matrix
metadata:
  name: ugv_nav_matrix
  description: The UGV navigation example over six seeds and two vehicle CPU budgets
  tags: [smoke, nav, ugv, matrix]
scenario: ../scenarios/ugv_nav_to_goal.yaml   # relative to the matrix file
seeds: [1, 2, 3, 4, 5, 6]                     # one more axis
parameters:                                   # values per path; the cases are the product
  vehicles[ugv1].resources.cpus: [1, 2]
retries: 1                                    # a failed case runs once more
parallel: auto                                # or a number
fail_on_flaky: false
```

| Field | Meaning |
| --- | --- |
| `metadata` | `name` (lower case, digits, underscores), `description`, `tags`, as a scenario's |
| `scenario` | the scenario file, relative to the matrix file |
| `parameters` | a list of values per scenario path (the paths of [`--set`](#scenario-overrides)); the **cases** are the Cartesian product, the last parameter varying fastest |
| `seeds` | seeds to run every case with: one more axis (`seed=N` on every case); a run with no seed uses the scenario's. `parameters.seed` and `seeds` are two ways to say the same and cannot both be given |
| `cases` | explicit cases, each a `name` and a `set` of overrides by path, added to the product (or alone, without `parameters`); each runs with every seed too |
| `retries` | how many times a case whose attempt failed, timed out or errored is run again (default 1; 0 disables); an interrupted attempt and a usage error are never retried |
| `parallel` | cases at once: `auto` (default, the host's cores and memory decide, see [Scheduling](#scheduling)) or a number |
| `resources` | `cpus` and `memory` (`4g`) a run of this matrix asks of the host, instead of the estimate from its plan |
| `fail_on_flaky` | a case that passed only on a retry fails the matrix (default false: flagged, counted as passed) |
| `outputs.directory` | where the matrix directory goes, relative to the workspace (default: the workspace's runs directory) |

A matrix with no `parameters`, `seeds` or `cases` has one case, `default`. Cases are named by
their overrides (`vehicles[ugv1].resources.cpus=1, seed=3`; an explicit case by its name plus
the seed) and numbered `c01`, `c02`, ... in the order they run (the width grows past 99).

## Running a matrix

```bash
fieldproof matrix examples/matrices/ugv_nav_matrix.yaml                 # make matrix
fieldproof matrix examples/matrices/ugv_nav_matrix.yaml --dry-run       # the cases and the schedule, as JSON
fieldproof matrix examples/matrices/ugv_nav_matrix.yaml --parallel 2 --retries 0
fieldproof matrix examples/matrices/ugv_nav_matrix.yaml --only 'c0[1-3]' --only '*seed=6'
```

Options: `--config`, `--runs-dir` (where the matrix directory goes), `--parallel N|auto`,
`--retries N` and `--fail-on-flaky`/`--allow-flaky` (each overriding the file's),
`--render auto|gpu|software` (passed to every run), `--only PATTERN` (run the cases whose id
or name matches the glob; repeatable), `--summary FILE` (append the Markdown report;
`$GITHUB_STEP_SUMMARY` by default when set), `--dry-run`.

The command reads the file, expands the cases, resolves every case against the workspace
(the scenario with its overrides, planned as `fieldproof validate` plans it, so a bad case
fails the matrix before anything runs, exit code 2) and measures the host through the Docker
daemon. Then it drives the cases: **each case is its own `fieldproof run` process** (the
engine's own command line with `--runs-dir`, `--run-id`, `--domain-id`, `--quiet` and a
`--set` per override; `FIELDPROOF_COMMAND` names another command, the tests substitute a
stub), in its own session, its console in `console.log` of its run directory. A crash of one
run is one failed case; Ctrl-C or SIGTERM on the matrix interrupts every running case (each
tears its containers down and reports `interrupted`), skips the pending ones and exits 130.

The console shows the matrix's log (one line per case started, retried and ended), then one
line per case with its verdict, attempts, duration and run id, the matrix's verdict and the
paths of the JUnit document, the reports and the record. On GitHub Actions a flaky case is a
`::warning::` annotation and a failed one an `::error::`. Exit code 0 when every case passed
(flaky ones included unless `fail_on_flaky`), 1 otherwise, 130 when interrupted, 2 when the
matrix or a case cannot be resolved.

### Scheduling

Every case's **demand** is estimated from its plan: the vehicles' budgets (`resources.cpus`
and `memory` from the manifest or the scenario; 2 cores and 2 GiB for a vehicle without one),
plus allowances for the simulator (2 cores, 2 GiB), each bridge (0.5 core, 512 MiB), the
recorder (0.5 core, 512 MiB) and the radio links' containers (0.5 core, 256 MiB); the matrix
file's `resources` replaces the estimate. The host's **capacity** is the daemon's cores and
memory less a reserve of one core and 1 GiB for the engine. With `parallel: auto` the next
case starts whenever its demand fits next to the running ones; one case always runs, so a
case larger than the host runs alone. With `parallel: N` up to N cases run at once whatever
their demand. `--dry-run` prints the demands, the capacity and the parallelism.

Every running case gets a ROS domain of its own, taken from a pool derived from the matrix
id, so cases running at once never share one (two unrelated runs on a host still derive their
domains from their ids, [scenarios.md](scenarios.md#fleets)); every run's Gazebo transport
partition is the run's own (`fieldproof-<short id>`), so two simulators on the same Docker
network never see each other's topics.

### Retries and flakiness

A case whose attempt ends `failed`, `timeout` or `error` runs again, up to `retries` times, as
a new run (`<scenario hash>-<timestamp>-<matrix token><case>a<attempt>`: the attempts of a
case are told apart by their run ids, every attempt's run directory stays in the matrix
directory). A case that passes after a failed attempt is **flaky**: counted as passed, flagged
in the record (`flaky: true`), in the log (`case.end` at warning level), in the console, in the
reports, in the JUnit properties (`flaky`, `attempts`, `retried_run_ids`) and in the database;
`fail_on_flaky` turns the flag into a failure of the matrix. An attempt that exits 2 (a usage
error: a bad `--set`, a scenario that does not validate) is never retried, nor is an
interrupted one. The outcome of a case is its last attempt's.

### The matrix directory

`matrix_id = <matrix-hash>-<timestamp>-<token>`, the shape of a run id (the first eight hex
digits of the SHA-256 of the matrix file). Everything the matrix did is in
`runs/<matrix_id>/`:

| File | Contents |
| --- | --- |
| `matrix.yaml` | the matrix file as given |
| `matrix.json` | the record, rewritten as cases start and end: the matrix (name, path, digest), the scenario, the `status`, `verdict`, `exit_code` and `reason`, start, end and duration, the `schedule` (parallelism, the capacity and the host, the most cases at once, retries, the command), the `counts` (cases, passed, failed, flaky, interrupted, skipped, attempts) and every case: id, name, overrides, seed, scenario digest, demand, status, verdict, flaky, its final run id and KPIs, and every attempt (number, run id and directory, domain, start, end, exit code, status, verdict, reason, duration, assertion counts) |
| `matrix.log` | one JSON object per line (`component: matrix`): `matrix.start`, `case.start`, `case.retry`, `case.end`, `case.skipped`, `matrix.interrupt`, `database.recorded`, `matrix.end` |
| `junit.xml` | one JUnit document for the matrix: a `testsuite` per case named `<scenario>[<case name>]`, the suite of the case's final attempt with its assertions, timeline entries and run as test cases ([assertions.md](assertions.md#reports)) and the case's properties (`matrix_id`, `case`, `case_name`, `parameters`, `case_seed`, `attempt`, `attempts`, `flaky`, `retried_run_ids`); a case that never ran or left no record is a suite with one `run` test case, `skipped` or an `error`; the document's counts are the sum of the suites' |
| `report.md`, `report.html` | the matrix report: the verdict and counts, the cases with their verdicts, attempts and run ids (the HTML links every case's own report), and a table of the KPIs across the cases |
| `<run_id>/` | every attempt's run directory, with everything a run writes and `console.log` |

## The run database

Every run records itself at its end in `fieldproof.sqlite` in the runs directory in effect
(`fieldproof.toml` `[runs] database`, relative to `[runs] directory`, `--runs-dir` or a
scenario's `outputs.directory`; an empty value disables it), after its reports are written
(the log says `database.recorded`; a database that cannot be written is a warning, never a
failed run). A matrix records its runs with their case, attempt and flakiness, and itself.
`fieldproof runs index [ROOT]` reads run directories (any directory holding a `run.json`,
three levels deep, and the `matrix.json` records) into the database, for runs from before the
database or copied from CI artefacts.

The schema is versioned: `schema_version` records every migration applied and when; the
engine applies the migrations a file has not seen when it opens it, one transaction each, and
refuses a file newer than it knows. Version 1 has the tables `runs` (one row per run: the
scenario's name, path and digest, status, verdict, exit code, reason, start, end and durations,
seed, time mode, world, engine version, run directory, the overrides as JSON, the matrix id,
case, attempt and flaky flag, the assertion counts, and the whole report as JSON),
`assertions` (name, type, status, reason, time per run), `kpis` (name, value, unit, source,
type per run), `artifacts` (path, size per run) and `matrices` (one row per matrix with its
counts and record).

```bash
fieldproof runs list [--scenario NAME] [--status passed|failed|...] [--matrix ID] [-n 20] [--json]
fieldproof runs show RUN [--json]
fieldproof runs diff RUN_A RUN_B [--json]
fieldproof runs index [ROOT] [--json]
```

All four take `--config`, `--runs-dir` (whose database) or `--database FILE`. `list` prints
the runs newest first, one line each with the run id, verdict, scenario, start, duration,
seed, assertion counts (passed/failed/skipped) and, for a matrix's run, its case, attempt and
flakiness. `show` prints one run's facts, assertions, KPIs and artefacts. `diff` compares two
runs: the scenario (the same document or not), the seeds, the overrides that differ, the
durations (wall clock, timeline, simulation) with their deltas, every assertion whose verdict
changed (regressed, fixed) and every KPI of either run with both values, the delta and the
percentage. A run is named by its id, a unique prefix of it, or a run directory (or its
`run.json`): `show` and `diff` read a directory directly, so they work without the database.

## HTML reports

Every run writes `report.html` next to `report.md` (`fieldproof report RUN --format html`
renders it again): one self-contained page, no script and no external resource, with what the
Markdown report shows (the verdict, the facts of the run, the assertions with their evidence
and, folded under a failed one, the stream excerpt, the KPIs, the timeline, the phases and the
artefacts linked relative to the page). A matrix writes its own `report.html` with the cases
and the KPI table, every case linked to its run's page.

## Tests

`engine/tests/test_overrides.py` covers the paths, the values and their application;
`test_rundb.py` the migrations (a fresh file, a new migration on an existing file, a newer file
refused, a broken migration rolled back), the records, the queries and the indexing;
`test_matrix.py` the file and its expansion (the example's twelve cases among them), the
demand and the scheduler, and the matrix run against a stub `fieldproof run`
(`engine/tests/fake_run.py`: passing, failing, flaky, crashing, usage-error, slow and hanging
runs; retries and flake flags; parallel cases by capacity with distinct domains; an interrupt;
the JUnit, Markdown, HTML and database outputs; the command's options); `test_runs_cli.py` the
four `runs` commands. `tests/integration/test_run_matrix.py` runs a two-case matrix of the failing
example scenario against the real stack (`make test-integration`). The Scenario tests
workflow's `matrix` job runs the example matrix nightly and on demand
([ci.md](../setup/ci.md#scenario-tests)).

## Known limits

- The demand estimate is static (the plan's budgets and fixed allowances); the resource
  monitor's samples of earlier runs are not consulted yet.
- Cases run as separate processes on one host; a matrix across hosts is not in scope.
- `runs diff` compares two runs; a trend over a matrix or over time is what the cockpit's run
  history (Phase 3) is for.
- The database holds one workspace's runs directory; `runs index` merges another directory's
  runs into it by hand.
