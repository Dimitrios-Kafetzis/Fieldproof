# Launch files

`spawn_<robot>.launch.py` spawns a reference vehicle into a running Gazebo server and starts its
`robot_state_publisher`. The server must already run a world with the sensor systems loaded, for
example `fieldproof_worlds/fieldproof_empty.sdf` with `gz sim -s -r --headless-rendering`.

| Launch file | Task | Arguments |
| --- | --- | --- |
| `spawn_ugv_ref.launch.py` | P1-T01 | `name`, `namespace`, `frame_prefix`, `world`, `x`, `y`, `z`, `yaw`, `gps`, `use_sim_time` |
