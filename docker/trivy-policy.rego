# Trivy ignore policy for the Fieldproof images (task P0-T05), applied by images.yml through
# `--ignore-policy`. Everything not matched here counts; a fixed CRITICAL vulnerability fails the
# scan, which is the signal to move APT_SNAPSHOT / ROS_SNAPSHOT in docker/base-ros2/Dockerfile.
#
# linux-libc-dev ships the kernel's user-space headers, pulled in through libc6-dev by the ROS
# build dependencies. Its advisories describe kernel bugs; no kernel code from the package runs
# in a container, so they cannot be exploited through the image and would otherwise fail every
# scan a few days after each snapshot date.
package trivy

import rego.v1

default ignore := false

ignore if {
	input.PkgName == "linux-libc-dev"
}
