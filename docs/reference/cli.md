# CLI

The `fieldproof` command line interface is the entry point of the scenario engine
([01-architecture.md](../plan/01-architecture.md#l4-scenario-engine)). It is installed by the
engine package: `uv run --project engine fieldproof ...` from the repository, or `fieldproof ...`
inside an environment where the package is installed.

| Command | Produced by | Description |
| --- | --- | --- |
| `fieldproof --version`, `-V` | P0-T07 | print `fieldproof <version>` and exit; the version is the repository version of `version.txt` |
| `fieldproof --help` | P0-T07 | list the commands and options; running `fieldproof` without arguments prints the same |
| `fieldproof validate <scenario>...` | P1-T05 | validate scenario files against schema v0 and resolve them against the workspace: the world file and its frames, every vehicle manifest, the timeline's goals ([scenarios.md](scenarios.md)); no container is started; option `--config` |
| `fieldproof run <scenario>` | P1-T05, P1-T06, P1-T07 | run a scenario with the Docker backend: simulator, bridges, the recorder and vehicles with readiness gates, the timeline with the assertions evaluated as it runs, teardown, the reports (Markdown, JSON, JUnit); writes `runs/<run_id>/` ([scenarios.md](scenarios.md#running-a-scenario), [assertions.md](assertions.md)); prints the verdict of every assertion after the status line; options `--config`, `--runs-dir`, `--run-id`, `--render`, `--domain-id`, `--network`, `--keep`, `--quiet`, `--dry-run` |
| `fieldproof schema` | P1-T05 | print the JSON Schema of a scenario file (`make scenario-schema` writes `engine/schemas/scenario.schema.json`) |
| `fieldproof report <run>` | P1-T06, P1-T07 | render the report of a run from its `run.json`: a run directory, the file itself, or a run id under the workspace's runs directory ([assertions.md](assertions.md#reports)); options `--format md\|json\|junit` (Markdown by default; `junit` is the JUnit XML document CI consumes, [ci.md](../setup/ci.md#scenario-tests)), `--write` (rewrite `report.md`, `report.json` and `junit.xml` in the run directory), `--config`; HTML arrives with P2-T06 |
| `fieldproof replay <run-id>` | P2-T07 | replay a recording |
| `fieldproof matrix <sweep>` | P2-T06 | run a parameter sweep |
| `fieldproof bridge gen <robot.yaml>` | P1-T03 | write the ros_gz bridge bundle of a vehicle: bridge configurations, adapter parameters, contract and launch file ([bridge.md](bridge.md#generating-a-bundle)); options `--out`, `--namespace`, `--frame-prefix`, `--no-clock`, `--no-robot-state-publisher`, `--without` |
| `fieldproof manifest validate <robot.yaml>...` | P1-T04 | validate vehicle manifests against schema v0 with their `extends` chain resolved ([manifest.md](manifest.md)); `--partial` accepts a manifest meant only to be extended |
| `fieldproof manifest schema` | P1-T04 | print the JSON Schema of a manifest file (`make manifest-schema` writes `engine/schemas/robot.schema.json`) |
| `fieldproof vehicle dockerfile <robot.yaml>` | P1-T04 | write the build context of a vehicle image: Dockerfile, entry point, resolved manifest, `vehicle.repos` ([manifest.md](manifest.md#vehicle-images)); option `--out` |
| `fieldproof vehicle build <robot.yaml>` | P1-T04 | write the context and run `docker buildx build`; options `--context`, `--tag`, `--base-image`, `--no-cache`, `--progress`, `--docker-arg`, `--dry-run` |
| `fieldproof vehicle run <robot.yaml> [-- CMD...]` | P1-T04 | `docker run` the vehicle image with the manifest's budget and environment on the `fieldproof` network; options `--name`, `--network`, `--domain-id`, `--detach`, `--keep`, `--image`, `--env`, `--docker-arg`, `--dry-run` |
| `fieldproof vehicle wait <robot.yaml>` | P1-T04 | wait until the running vehicle container satisfies the manifest's `runtime.ready` conditions, through the image's readiness probe ([manifest.md](manifest.md#the-readiness-probe)); exit code 1 with the pending conditions on timeout; options `--name`, `--timeout`, `--interval`, `--quiet`, `--dry-run` |
| `fieldproof cockpit` | P3-T01 | serve the cockpit against the local engine |

Exit codes: `0` on success, `1` on a failed scenario or assertion (also a run that timed out or
could not start), `2` on a usage error (Typer's convention), `130` on a run interrupted by Ctrl-C
or SIGTERM. Output is Rich-formatted on a terminal and plain when piped.
