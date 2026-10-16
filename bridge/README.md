# bridge

colcon workspace for layer L2: the ros_gz bridge configuration generator and the adapter nodes
that make the ROS 2 interface of a simulated vehicle identical to its real drivers
([ADR-0002](../docs/adr/0002-ros2-jazzy-primary-ros1-via-bridge.md)). It builds inside the `dev`
container with `make test-ros`.

| Package | Type | Contents |
| --- | --- | --- |
| `fieldproof_bridge` | `ament_python` | generator, adapter nodes, contract tests (P1-T03); P0-T07 ships the package skeleton |

See [docs/setup/workspaces.md](../docs/setup/workspaces.md).
