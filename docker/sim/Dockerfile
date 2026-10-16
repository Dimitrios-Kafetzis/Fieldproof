# Fieldproof simulation image: the base image plus Gazebo Harmonic, ros_gz and Mesa, able to render
# sensors headless on a GPU render node or in software (task P0-T03, ADR-0001).
#
# Gazebo Harmonic comes from the ROS 2 Jazzy vendor packages (ros-jazzy-gz-*-vendor), pulled in by
# ros_gz, so it is frozen by the same ROS snapshot as the base image and no third apt repository
# is needed. The `gz` CLI lands on PATH through the ROS environment.
ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-base-ros2:local
# hadolint ignore=DL3006
FROM ${BASE_IMAGE}
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
WORKDIR /home/${FIELDPROOF_USER}

ARG GIT_SHA=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="fieldproof-sim" \
      org.opencontainers.image.description="Gazebo Harmonic and ros_gz on ROS 2 Jazzy with Mesa headless rendering" \
      org.opencontainers.image.source="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.url="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Fieldproof" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"
