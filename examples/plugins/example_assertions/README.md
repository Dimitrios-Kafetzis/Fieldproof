# Example assertion plugin: `example.visits_waypoints`

A Fieldproof assertion plugin (task P2-T05,
[assertions.md](../../../docs/reference/assertions.md#writing-a-plugin)) small enough to copy
as the starting point of your own. It adds the assertion type `example.visits_waypoints`: the
vehicle's ground truth comes within `tolerance_m` of every placement of `waypoints`, in any
order, before `within_s` or the end of the timeline. Its evidence is when each waypoint was
visited and which are missing; its metrics are `visited` and `completed_at_s`.

```yaml
assertions:
  - name: patrolled
    type: example.visits_waypoints
    vehicle: ugv1
    waypoints:
      - { frame: spawn_dock }
      - { x: 10.0, y: -1.0 }
    tolerance_m: 0.5
    within_s: 2m
```

The engine's development environment installs it (`uv sync --project engine`), so
`fieldproof assertions` lists it next to the built-ins and its tests run with the engine's
(`make test-engine`). To use a plugin of your own with the engine, install its distribution
into the same environment: the engine finds assertion types through the
`fieldproof.assertions` entry point group of `pyproject.toml`.
