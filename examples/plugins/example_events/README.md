# Example event plugin: `example.announce`

A complete event plugin for the Fieldproof engine, small enough to copy
([events.md](../../../docs/reference/events.md#writing-a-plugin)): a Python distribution with one
`fieldproof.events` entry point, an entry model (`Announce`: the fields a scenario writes under
`action: example.announce`), a `plan` hook (the topic resolved under the vehicle's namespace)
and an `apply` hook (`ros2 topic pub --once` inside the vehicle's container, reliable, transient
local and kept alive for 2 s so a recorder that discovers the topic late still gets the message,
the record filled in). The engine's development environment installs it, so it is registered after
`uv sync --project engine`:

```bash
uv run --project engine fieldproof events               # lists example.announce next to the built-ins
uv run --project engine pytest examples/plugins/example_events/tests   # or make test-engine
```

In a scenario:

```yaml
timeline:
  - name: say_hello
    vehicle: ugv1
    at: 2s
    every: 5s
    count: 3
    action: example.announce
    topic: announce        # /ugv1/announce under the vehicle's namespace
    text: fieldproof says hello
    subscribers: 1         # wait for the recorder before publishing
```

To write your own, copy this directory, rename the distribution, the package and the action in
`pyproject.toml`, and install it into the engine's environment (`uv add --project engine --dev
--editable <path>`, or `pip install -e <path>` in that environment).
