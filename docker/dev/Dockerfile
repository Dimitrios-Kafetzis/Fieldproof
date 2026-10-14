# Fieldproof development image: the simulation image plus build, debug and lint tooling for the
# colcon workspaces and the Python engine (task P0-T03). Used by docker/compose.yaml (`dev`
# service) and .devcontainer/devcontainer.json.
ARG BASE_IMAGE=ghcr.io/dimitrios-kafetzis/fieldproof-sim:local
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336
# hadolint ignore=DL3006
FROM ${UV_IMAGE} AS uv

# hadolint ignore=DL3006
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG UV_IMAGE
ARG USERNAME=fieldproof
# Commit of colcon/colcon-mixin-repository that the default mixins are taken from.
ARG COLCON_MIXIN_COMMIT=7558e35befbff0d88d9b8f701b3ab1b073cbcaba

USER root

# Compilers and build systems, ROS developer tools (colcon, rosdep, vcstool), clang tooling for the
# C++ quality gates, debugging and everyday shell utilities. Every package comes from the snapshots
# configured in the base image (DL3008 ignored for that reason).
# The pinned package lists come with the base image, so no index refresh is needed (DL3009).
# hadolint ignore=DL3008,DL3009
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    fieldproof-apt-install \
        bash-completion \
        build-essential \
        ccache \
        clang \
        clang-format \
        clang-tidy \
        clangd \
        cmake \
        gdb \
        git \
        git-lfs \
        jq \
        less \
        lld \
        nano \
        ninja-build \
        openssh-client \
        python3-colcon-common-extensions \
        python3-colcon-mixin \
        python3-pip \
        python3-rosdep \
        python3-vcstool \
        python3-venv \
        ros-dev-tools \
        shellcheck \
        sudo \
        wget \
    && rosdep init \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-${USERNAME} \
    && chmod 0440 /etc/sudoers.d/90-${USERNAME}

# uv (Python environments and tools) copied from the pinned upstream image.
COPY --from=uv /uv /uvx /usr/local/bin/

USER ${USERNAME}
WORKDIR /home/${USERNAME}

# colcon mixins from a pinned commit of the mixin repository.
RUN colcon mixin add default \
        "https://raw.githubusercontent.com/colcon/colcon-mixin-repository/${COLCON_MIXIN_COMMIT}/index.yaml" \
    && colcon mixin update default

ENV CCACHE_DIR=/home/${USERNAME}/.cache/ccache \
    UV_LINK_MODE=copy

WORKDIR /workspace

ARG GIT_SHA=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="fieldproof-dev" \
      org.opencontainers.image.description="Fieldproof development container: simulation image plus colcon, clang tooling and uv" \
      org.opencontainers.image.source="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.url="https://github.com/Dimitrios-Kafetzis/Fieldproof" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Fieldproof" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"
