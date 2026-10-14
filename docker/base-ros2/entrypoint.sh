#!/usr/bin/env bash
# Entry point shared by every Fieldproof image: source the ROS 2 environment, then run the command.
set -euo pipefail

# The ROS setup scripts are not written for `set -u`; relax it while they run.
set +u
# shellcheck disable=SC1090
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
set -u

exec "$@"
