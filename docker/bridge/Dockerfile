# Fieldproof bridge image: the simulation image plus this repository's bridge and reference robot
# packages, so the bridge of a vehicle runs as its own container next to the simulator and the
# vehicle (task P1-T04, ADR-0004; the bridge itself is task P1-T03, the robots task P1-T01).
#
# Stage `build`: the simulation image plus colcon builds bridge/fieldproof_bridge and
# robots/fieldproof_robots (the named build contexts of docker/compose.yaml) into
# /opt/fieldproof_bridge with merge-install, tests off. Stage `runtime`: the simulation image plus
# that install space, an entry point that sources it, and `fieldproof-bridge`, which starts the
# bridge launch on the manifest named by FIELDPROOF_MANIFEST (the default command). Every
# run-time dependency of the two packages (ros_gz_bridge, robot_state_publisher, xacro, rclpy,
# the message packages) is already in the simulation image; the runtime stage adds the network
# tools of the radio links (task P2-T02): `tc` and `ip` (iproute2) for the netem profiles a link
# holder applies, `iperf3` and `ping` to measure a link by hand and in the integration tests.
ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-sim:local
# hadolint ignore=DL3006
FROM ${BASE_IMAGE} AS build
# This stage only builds; the runtime stage below switches back to the non-root user.
# hadolint ignore=DL3002
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# colcon and make (cmake comes with the simulation image) from the snapshots the base image is
# pinned to (DL3008 and DL3009 as in the base image).
# hadolint ignore=DL3008,DL3009
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    fieldproof-apt-install make python3-colcon-common-extensions

WORKDIR /opt/fieldproof_ws
# The package sources from the repository (named build contexts, not stages: DL3022).
# hadolint ignore=DL3022
COPY --from=fieldproof-robots-src / src/fieldproof_robots/
# hadolint ignore=DL3022
COPY --from=fieldproof-bridge-src / src/fieldproof_bridge/
RUN source "/opt/ros/${ROS_DISTRO}/setup.bash" \
    && colcon build --merge-install --install-base /opt/fieldproof_bridge \
        --event-handlers console_cohesion+ desktop_notification- status- \
        --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF

# hadolint ignore=DL3006
FROM ${BASE_IMAGE} AS runtime
ARG BASE_IMAGE
ARG FIELDPROOF_USER=fieldproof
USER root

# The network tools, from the snapshots the base image is pinned to (DL3008 and DL3009 as in the
# base image).
# hadolint ignore=DL3008,DL3009
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    fieldproof-apt-install iproute2 iperf3 iputils-ping

COPY --from=build /opt/fieldproof_bridge /opt/fieldproof_bridge
COPY --chmod=755 entrypoint.sh /usr/local/bin/fieldproof-bridge-entrypoint
COPY --chmod=755 fieldproof-bridge.sh /usr/local/bin/fieldproof-bridge

USER ${FIELDPROOF_USER}
WORKDIR /home/${FIELDPROOF_USER}
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/fieldproof-bridge-entrypoint"]
CMD ["fieldproof-bridge"]

ARG GIT_SHA=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="fieldproof-bridge" \
      org.opencontainers.image.description="Fieldproof bridge container: ros_gz bridge, adapters and TF of one vehicle from its manifest" \
      org.opencontainers.image.source="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.url="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Fieldproof" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"
