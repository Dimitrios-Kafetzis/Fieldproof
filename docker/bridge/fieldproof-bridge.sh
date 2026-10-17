#!/usr/bin/env bash
# Default command of the Fieldproof bridge image: the bridge launch of fieldproof_bridge on the
# manifest named by FIELDPROOF_MANIFEST (bridge instances, adapters and robot_state_publisher of
# that vehicle, task P1-T03). Extra arguments go to the launch (`namespace:=ugv1`, `without:=gps`).
#
#   docker run --rm --network fieldproof -e FIELDPROOF_MANIFEST=/workspace/examples/vehicles/ugv_nav2/robot.yaml \
#       -v "$PWD:/workspace:ro" ghcr.io/dimitrios-kafetzis/fieldproof-bridge:local
set -euo pipefail
if [[ -z "${FIELDPROOF_MANIFEST:-}" ]]; then
  echo "fieldproof-bridge: set FIELDPROOF_MANIFEST to the vehicle manifest (robot.yaml) to bridge" >&2
  exit 2
fi
if [[ ! -f "$FIELDPROOF_MANIFEST" ]]; then
  echo "fieldproof-bridge: $FIELDPROOF_MANIFEST is not a file (mount the repository or the vehicle directory)" >&2
  exit 2
fi
exec ros2 launch fieldproof_bridge bridge.launch.py "manifest:=${FIELDPROOF_MANIFEST}" "$@"
