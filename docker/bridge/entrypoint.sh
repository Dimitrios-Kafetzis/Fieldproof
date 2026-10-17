#!/usr/bin/env bash
# Entry point of the Fieldproof bridge image: source ROS 2 and the bridge install space
# (fieldproof_bridge, fieldproof_robots), then run the command. The setup scripts are not written
# for `set -u`.
set -eo pipefail
# shellcheck disable=SC1090,SC1091
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
# shellcheck disable=SC1091
source /opt/fieldproof_bridge/setup.bash
set -u
exec "$@"
