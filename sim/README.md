# sim

colcon workspace for layer L1: Gazebo Harmonic worlds and C++ system plugins
([ADR-0001](../docs/adr/0001-gazebo-harmonic-as-physics-and-sensor-engine.md)). It builds inside
the `dev` container, never on the host:

```bash
make test-ros    # colcon build + colcon test for sim/, bridge/ and robots/ in the dev container
make tidy        # clang-tidy over the C++ packages with the compile commands of that build
make shell       # then: cd /workspace/sim && colcon build && colcon test
```

| Package | Type | Contents |
| --- | --- | --- |
| `fieldproof_worlds` | `ament_cmake` | SDF worlds under `worlds/`, each checked with `gz sdf --check` at test time |
| `fieldproof_gz_plugins` | `ament_cmake`, C++17 | Gazebo system plugins; P0-T07 ships `fieldproof::HelloSystem` and a GoogleTest suite |

`build/`, `install/` and `log/` are ignored by git. See
[docs/setup/workspaces.md](../docs/setup/workspaces.md).
