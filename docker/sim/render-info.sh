#!/usr/bin/env bash
# Print the render nodes, the software-rendering switch and the EGL devices with their OpenGL
# renderer, as Mesa sees them from inside the container. Diagnostic only.
set -euo pipefail

echo "render nodes: $(find /dev/dri -maxdepth 1 -name 'renderD*' 2>/dev/null | sort | tr '\n' ' ')"
echo "LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-unset}"

# Ubuntu installs the Mesa demos with a multiarch suffix; mesa-utils (not installed) adds the links.
eglinfo="$(find /usr/bin -maxdepth 1 -name 'eglinfo*' | head -n1)"
if [[ -n "$eglinfo" ]]; then
  "$eglinfo" -B 2>/dev/null | grep -E 'platform|renderer|EGL (vendor|version)' || true
fi
