# Fieldproof simulation image: the base image plus Gazebo Harmonic, ros_gz and Mesa, able to render
# sensors headless on a GPU render node or in software (task P0-T03, ADR-0001), and this
# repository's Gazebo system plugins (sim/fieldproof_gz_plugins: the world events of task P2-T03)
# built into /opt/fieldproof_sim.
#
# Gazebo Harmonic comes from the ROS 2 Jazzy vendor packages (ros-jazzy-gz-*-vendor), pulled in by
# ros_gz, so it is frozen by the same ROS snapshot as the base image and no third apt repository
# is needed. The `gz` CLI lands on PATH through the ROS environment.
#
# Stage `gazebo`: the base image plus Gazebo, ros_gz, xacro and Mesa. Stage `build`: that plus the
# build tools, where colcon builds the plugin package (the named build context
# fieldproof-gz-plugins-src of docker/compose.yaml) with merge-install, tests off. Stage `runtime`
# (the image): `gazebo` plus the install space, which GZ_SIM_SYSTEM_PLUGIN_PATH points Gazebo at;
# the plugin libraries carry an RPATH to find each other, so no setup script needs sourcing.
ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:local
# hadolint ignore=DL3006
FROM ${BASE_IMAGE} AS gazebo
ARG BASE_IMAGE

USER root

# ros_gz (which vendors Gazebo Harmonic) and the Mesa stack for EGL headless rendering. The
# individual ros_gz packages are listed instead of the ros-jazzy-ros-gz metapackage because the
# latter pulls in ros_gz_sim_demos and with it rviz2, rqt and VTK (about 1.5 GB of GUI tooling).
# xacro expands the reference vehicle descriptions (robots/, task P1-T01) both at build time and
# when a spawn launch file renders them with launch arguments.
#   libegl1 / libegl-mesa0  EGL front end and Mesa's EGL implementation (surfaceless and device platforms)
#   libgl1-mesa-dri          radeonsi, iris, ... hardware drivers plus llvmpipe for software rendering
#   libgbm1                  buffer management for the EGL device platform on /dev/dri
#   mesa-utils-bin           eglinfo, used by the image tests to show which renderer is active
# Versions are fixed by the apt snapshots configured in the base image (DL3008 ignored).
# The pinned package lists come with the base image, so no index refresh is needed (DL3009).
# hadolint ignore=DL3008,DL3009
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    fieldproof-apt-install \
        "ros-${ROS_DISTRO}-ros-gz-sim" \
        "ros-${ROS_DISTRO}-ros-gz-bridge" \
        "ros-${ROS_DISTRO}-ros-gz-image" \
        "ros-${ROS_DISTRO}-xacro" \
        libegl1 \
        libegl-mesa0 \
        libgl1 \
        libgl1-mesa-dri \
        libgbm1 \
        mesa-utils-bin

# Headless defaults: no display, EGL without a window system. Gazebo's ogre2 engine picks an EGL
# device (a /dev/dri render node when one is mapped in, otherwise Mesa's software device).
# Setting LIBGL_ALWAYS_SOFTWARE=1 at run time forces llvmpipe on any node.
ENV EGL_PLATFORM=surfaceless \
    QT_QPA_PLATFORM=offscreen \
    GZ_PARTITION=fieldproof

COPY --chmod=755 render-info.sh /usr/local/bin/fieldproof-render-info

ARG FIELDPROOF_USER=fieldproof
USER ${FIELDPROOF_USER}

# ---------------------------------------------------------------------------------------------
# The Fieldproof Gazebo plugins, built against the vendored Gazebo of the stage above. This stage
# only builds; the runtime stage below switches back to the non-root user (DL3002).
FROM gazebo AS build
# hadolint ignore=DL3002
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The compiler, cmake and colcon from the snapshots the base image is pinned to (DL3008 and
# DL3009 as above).
# hadolint ignore=DL3008,DL3009
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    fieldproof-apt-install build-essential cmake python3-colcon-common-extensions

WORKDIR /opt/fieldproof_ws
# The package sources from the repository (a named build context, not a stage: DL3022).
# hadolint ignore=DL3022
COPY --from=fieldproof-gz-plugins-src / src/fieldproof_gz_plugins/
RUN source "/opt/ros/${ROS_DISTRO}/setup.bash" \
    && colcon build --merge-install --install-base /opt/fieldproof_sim \
        --event-handlers console_cohesion+ desktop_notification- status- \
        --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF

# ---------------------------------------------------------------------------------------------
FROM gazebo AS runtime
ARG BASE_IMAGE
ARG FIELDPROOF_USER=fieldproof

COPY --from=build /opt/fieldproof_sim /opt/fieldproof_sim
# Where Gazebo finds the Fieldproof systems a world loads (`fieldproof::WorldEvents`).
ENV GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/fieldproof_sim/lib

WORKDIR /home/${FIELDPROOF_USER}

ARG GIT_SHA=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="fieldproof-sim" \
      org.opencontainers.image.description="Gazebo Harmonic and ros_gz on ROS 2 Jazzy with Mesa headless rendering and the Fieldproof world plugins" \
      org.opencontainers.image.source="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.url="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Fieldproof" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"
