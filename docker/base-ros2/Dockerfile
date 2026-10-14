# Fieldproof base image: Ubuntu 24.04 + ROS 2 Jazzy ros-base, non-root user, every apt source
# frozen to a dated snapshot (task P0-T03, ADR-0002).
#
# Pins (change them here and in docs/setup/containers.md in the same commit):
#   UBUNTU_DIGEST  ubuntu:24.04 image digest
#   APT_SNAPSHOT   Ubuntu archive snapshot id served by https://snapshot.ubuntu.com
#   ROS_SNAPSHOT   ROS 2 apt snapshot date served by http://snapshots.ros.org/jazzy/<date>
ARG UBUNTU_DIGEST=sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
FROM ubuntu:24.04@${UBUNTU_DIGEST}

ARG UBUNTU_DIGEST
ARG APT_SNAPSHOT=20261010T000000Z
ARG ROS_SNAPSHOT=2026-10-05
ARG ROS_DISTRO=jazzy
ARG USERNAME=fieldproof
ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Etc/UTC \
    ROS_DISTRO=${ROS_DISTRO}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Keep downloaded .debs and package lists in BuildKit cache mounts instead of deleting them.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' > /etc/apt/apt.conf.d/90keep-cache \
    && printf 'Acquire::Retries "3";\n' > /etc/apt/apt.conf.d/90retries

# Bootstrap: the only packages fetched from the live archive are the CA certificates that let apt
# reach the HTTPS snapshot service. Everything after this step comes from the dated snapshots.
# The sources are then rewritten to the snapshot host with only the suites and components
# Fieldproof needs (fewer index files to fetch from a slow host).
# hadolint ignore=DL3008
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/%s\nSuites: noble noble-updates noble-security\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' \
        "${APT_SNAPSHOT}" > /etc/apt/sources.list.d/ubuntu.sources

# Parallel package fetcher used by every apt step from here on (see the script for why).
COPY --chmod=755 apt-install.sh /usr/local/bin/fieldproof-apt-install

# ROS 2 apt repository, frozen to a dated snapshot. Snapshots are signed by the ROS snapshot
# builder key (fingerprint 4B63 CF8F DE49 746E 98FA 01DD AD19 BAB3 CBF1 25EA), vendored next to
# this file as a minimal public keyring; tests/images/test_images.sh checks the fingerprint.
COPY --chmod=644 ros-snapshot-keyring.gpg /usr/share/keyrings/ros-snapshot-keyring.gpg
RUN printf 'Types: deb\nURIs: http://snapshots.ros.org/%s/%s/ubuntu\nSuites: noble\nComponents: main\nSigned-By: /usr/share/keyrings/ros-snapshot-keyring.gpg\n' \
        "${ROS_DISTRO}" "${ROS_SNAPSHOT}" > /etc/apt/sources.list.d/ros2.sources

# ROS 2 ros-base plus the small set of tools every Fieldproof container needs. Versions are fixed
# by the snapshots above, which is why DL3008 (per-package pins) is ignored here and below.
# The package lists stay in the image on purpose (DL3009): they describe immutable snapshots, so
# derived images (sim, dev, vehicle images) install from them without refreshing the index from
# the slow snapshot host. Downloaded .deb files live in a BuildKit cache mount instead.
# hadolint ignore=DL3008,DL3009
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    fieldproof-apt-install \
        "ros-${ROS_DISTRO}-ros-base" \
        tini

# Non-root runtime user. The stock `ubuntu` user holds UID 1000 in the base image; it is removed so
# the Fieldproof user can take the UID that matches a typical developer account for bind mounts.
# `video` (GID 44) grants /dev/dri access; the host's `render` group is added at run time.
# hadolint ignore=SC2016
RUN userdel -r ubuntu \
    && groupadd --gid "${GID}" "${USERNAME}" \
    && useradd --uid "${UID}" --gid "${GID}" --groups video --create-home --shell /bin/bash "${USERNAME}" \
    && printf '\n# ROS 2 environment for interactive shells\nsource /opt/ros/%s/setup.bash\n' "${ROS_DISTRO}" \
        >> "/home/${USERNAME}/.bashrc" \
    && printf '# ROS 2 environment for login shells (docker exec ... bash -lc)\nif [ -n "${BASH_VERSION:-}" ]; then . /opt/ros/%s/setup.bash; else . /opt/ros/%s/setup.sh; fi\n' \
        "${ROS_DISTRO}" "${ROS_DISTRO}" > /etc/profile.d/10-ros2.sh

COPY --chmod=755 entrypoint.sh /usr/local/bin/fieldproof-entrypoint

USER ${USERNAME}
WORKDIR /home/${USERNAME}
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/fieldproof-entrypoint"]
CMD ["bash"]

# OCI annotations. These ARGs sit last so changing the revision or date never invalidates a layer.
ARG GIT_SHA=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="fieldproof-base-ros2" \
      org.opencontainers.image.description="Ubuntu 24.04 with ROS 2 Jazzy ros-base, apt sources frozen to a snapshot" \
      org.opencontainers.image.source="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.url="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Fieldproof" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.base.name="docker.io/library/ubuntu:24.04" \
      org.opencontainers.image.base.digest="${UBUNTU_DIGEST}" \
      io.fieldproof.apt-snapshot="${APT_SNAPSHOT}" \
      io.fieldproof.ros-snapshot="${ROS_SNAPSHOT}"
