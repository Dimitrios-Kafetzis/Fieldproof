# fieldproof_bridge

`ament_python` package of layer L2 (task P1-T03): the generator that turns a vehicle manifest
(`robot.yaml`) into a ros_gz bridge bundle, the adapter nodes that reshape what the bridge cannot,
the contract checker, and the launch file that starts it all. Reference:
[docs/reference/bridge.md](../../docs/reference/bridge.md).

| Path | Contents |
| --- | --- |
| `fieldproof_bridge/manifest.py` | the manifest as the bridge reads it (validation, interfaces) |
| `fieldproof_bridge/generator.py` | bundle generator; `ros2 run fieldproof_bridge bridge_gen`, also `fieldproof bridge gen` |
| `fieldproof_bridge/adapters/` | `imu_adapter`, `navsat_adapter`, `throttle` (rclpy nodes) |
| `fieldproof_bridge/contract.py` | contract checker; `ros2 run fieldproof_bridge contract_check <bundle>` |
| `launch/bridge.launch.py` | starts a bundle, or generates one from `manifest:=` |
| `test/` | generator and adapter unit tests, the launch-based contract test against `ugv_ref` |

`pyproject.toml` carries the Python metadata (the engine installs the package from this path so
both command lines run one generator); `setup.py` keeps the ament options. The version in
`package.xml` and `fieldproof_bridge/__init__.py` is maintained by release-please.
