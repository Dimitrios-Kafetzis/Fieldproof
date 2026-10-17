# CLI

The `fieldproof` command line interface is the entry point of the scenario engine
([01-architecture.md](../plan/01-architecture.md#l4-scenario-engine)). It is installed by the
engine package: `uv run --project engine fieldproof ...` from the repository, or `fieldproof ...`
inside an environment where the package is installed.

| Command | Produced by | Description |
| --- | --- | --- |
| `fieldproof --version`, `-V` | P0-T07 | print `fieldproof <version>` and exit; the version is the repository version of `version.txt` |
| `fieldproof --help` | P0-T07 | list the commands and options; running `fieldproof` without arguments prints the same |
| `fieldproof validate <scenario>` | P1-T05 | validate a scenario file against the schema |
| `fieldproof run <scenario>` | P1-T05 | run a scenario headless or with the cockpit attached |
| `fieldproof report <run-id>` | P1-T06 | render the report of a run (Markdown, HTML, JUnit) |
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

Exit codes: `0` on success, `1` on a failed scenario or assertion, `2` on a usage error (Typer's
convention). Output is Rich-formatted on a terminal and plain when piped.
