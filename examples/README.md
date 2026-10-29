# examples

Worked examples that exercise the public interfaces of Fieldproof. Each is also a test fixture:
the scenario regression suite and the onboarding tests run them in CI.

| Directory | Contents | Arrives with |
| --- | --- | --- |
| `scenarios/` | scenario YAML files, from the first smoke scenario to the scenario library | P1-T05, P1-T07, P7-T01 |
| `matrices/` | run matrix files (`kind: Matrix`), starting with `ugv_nav_matrix.yaml`: the UGV scenario over six seeds and two vehicle CPU budgets, twelve runs in one `fieldproof matrix` with a single JUnit document ([matrix.md](../docs/reference/matrix.md)); the Scenario tests workflow's matrix job runs it nightly | P2-T06 |
| `logs/` | field logs for the replay source, written on demand and never committed: `ugv_dock_drive.mcap`, the synthetic UGV log of `make replay-log` that `scenarios/ugv_replay.yaml` plays ([replay.md](../docs/reference/replay.md)) | P2-T07 |
| `vehicles/` | vehicle manifests and containers, starting with `ugv_nav2/` (Nav2 as the stand-in codebase) | P1-T04 |
| `fieldproof-example-vehicle/` | the copy of the repository [fieldproof-example-vehicle](https://github.com/Dimitrios-Kafetzis/fieldproof-example-vehicle), a fictional field team's vehicle onboarded by following [onboarding.md](../docs/reference/onboarding.md) and nothing else: its own workspace (`fieldproof.toml`), a manifest that extends the reference UGV by name (`fieldproof:ugv_ref`), a waypoint navigator of its own (a `NavigateToPose` action server on wheel odometry that holds behind obstacles), two scenarios, a Makefile and its own CI workflow; `tests/integration/test_onboarding_repo.py` and the Scenario tests workflow's `onboarding` job copy it next to the checkout and run it through its Makefile | P2-T08 |
| `plugins/` | event, assertion and cockpit panel plugins written against the plugin SDK, starting with `example_events/` (the timeline action `example.announce`, [events.md](../docs/reference/events.md#writing-a-plugin)) | P2-T03, P7-T04 |
| `demo_m1.sh` | the M1 demo, the quick start of the README from a fresh clone: pull the published images (or `--local`), fetch the assets, build the example vehicle image, run `scenarios/ugv_nav_to_goal.yaml`, print the report and where the MCAP recording is; `make demo`; the Scenario tests workflow runs the same chain in CI ([ci.md](../docs/setup/ci.md#scenario-tests)) | P1-T07 |
| `demo_m2.sh` | the M2 demo, the scenario engine's milestone end to end: the fleet of three (`--quick` skips it), the radio link dropped under a driving vehicle, a world event and a sensor fault, the real-time-locked clock and two lockstep runs compared, the run matrix (two cases with `--quick`), the synthetic field log replayed, the example vehicle repository built and run through its own Makefile; one `fieldproof` command per step, a table of verdicts at the end; `make demo-m2`; `--only` and `--skip` pick steps | P2-T08 |
