# fieldproof_bridge launch files

`bridge.launch.py` starts the bridge of one vehicle from its `robot.yaml` manifest (generating the
bundle at launch time) or from a bundle written by `fieldproof bridge gen`; see
[docs/reference/bridge.md](../../../docs/reference/bridge.md). Generated bundles carry their own
`bridge.launch.py`, which includes this one.
