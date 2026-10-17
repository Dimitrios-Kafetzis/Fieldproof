# fieldproof_bridge configuration

Nothing is hand-written here: the ros_gz bridge configuration of a vehicle is generated from its
`robot.yaml` manifest by `fieldproof bridge gen` (or `ros2 run fieldproof_bridge bridge_gen`) into
a bundle directory of the caller's choice, and `bridge.launch.py` generates it on the fly when
given `manifest:=`. See [docs/reference/bridge.md](../../../docs/reference/bridge.md).
