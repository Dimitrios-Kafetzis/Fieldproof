"""The workspace configuration, ``fieldproof.toml`` (task P1-T05).

A workspace is a directory with a ``fieldproof.toml`` at its root: where runs are written, which
Docker network and image tag the stack uses, where the worlds, models and assets the simulator
mounts live, and the timeouts of the orchestrator. Every value has a default that matches this
repository, so the file may be short or absent; :func:`load_config` finds it by walking up from a
starting directory (the scenario's, then the current one), the way git finds its repository, and
the directory that holds it is the workspace root that relative paths resolve against.

Precedence, highest first (01-architecture.md): the scenario file, the command line, the
environment (``FIELDPROOF_IMAGE_PREFIX``, ``FIELDPROOF_IMAGE_TAG``, ``FIELDPROOF_RENDER``,
``ROS_DOMAIN_ID``, as the compose stack reads them), ``fieldproof.toml``, the defaults.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PrivateAttr, ValidationError

from fieldproof.manifest import format_errors

CONFIG_FILE = "fieldproof.toml"
DEFAULT_IMAGE_PREFIX = "ghcr.io/dimitrios-kafetzis/fieldproof-"
DEFAULT_IMAGE_TAG = "local"
# Environment variables the compose stack and the Makefile also read.
ENV_IMAGE_PREFIX = "FIELDPROOF_IMAGE_PREFIX"
ENV_IMAGE_TAG = "FIELDPROOF_IMAGE_TAG"
ENV_RENDER = "FIELDPROOF_RENDER"
ENV_DOMAIN_ID = "ROS_DOMAIN_ID"

Render = Literal["auto", "gpu", "software"]


class ConfigError(ValueError):
    """The configuration file cannot be read or validated."""


class Strict(BaseModel):
    """Unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class Runs(Strict):
    """Where runs are written."""

    directory: str = Field(
        default="runs", description="Run directories, relative to the workspace."
    )


class Docker(Strict):
    """How the stack's containers are started."""

    network: str = Field(default="fieldproof", description="Docker network every container joins.")
    image_prefix: str = Field(
        default=DEFAULT_IMAGE_PREFIX, description="Prefix of the stack's images."
    )
    image_tag: str = Field(default=DEFAULT_IMAGE_TAG, description="Tag of the stack's images.")
    render: Render = Field(
        default="auto",
        description="`gpu`: map /dev/dri into the simulator; `software`: Mesa software "
        "rendering; `auto`: gpu when the host has a render node.",
    )
    ros_domain_id: int = Field(
        default=0, ge=0, le=232, description="ROS_DOMAIN_ID of every container."
    )


class Stack(Strict):
    """What the simulator mounts, relative to the workspace (docker/compose.yaml's mounts)."""

    worlds: str = Field(default="sim/fieldproof_worlds/worlds", description="World files.")
    models: str = Field(
        default="sim/fieldproof_worlds/models", description="The worlds' own models."
    )
    fuel: str = Field(
        default="assets/fuel/models", description="Fetched Fuel models (`make assets`)."
    )
    fuel_lock: str = Field(
        default="assets/fuel.lock", description="The Fuel lock the fetch follows."
    )
    robots: str = Field(
        default="robots/fieldproof_robots/models", description="Reference vehicle models."
    )


class Timeouts(Strict):
    """Wall-clock limits of the orchestrator's steps, in seconds."""

    world_s: PositiveFloat = Field(default=300.0, description="The world's services answering.")
    spawn_s: PositiveFloat = Field(default=300.0, description="A vehicle model spawned.")
    bridge_s: PositiveFloat = Field(default=120.0, description="A bridge publishing its topics.")
    teardown_s: PositiveFloat = Field(default=10.0, description="Grace period of a container stop.")


class Config(Strict):
    """A ``fieldproof.toml``."""

    runs: Runs = Runs()
    docker: Docker = Docker()
    stack: Stack = Stack()
    timeouts: Timeouts = Timeouts()

    _root: Path = PrivateAttr(default_factory=Path.cwd)
    _path: Path | None = PrivateAttr(default=None)

    @property
    def root(self) -> Path:
        """The workspace root: the configuration file's directory."""
        return self._root

    @property
    def path(self) -> Path | None:
        """The configuration file, or ``None`` for the defaults."""
        return self._path

    def resolve(self, relative: str) -> Path:
        """A path of the configuration, resolved against the workspace root."""
        return (self._root / relative).resolve()

    def image(self, name: str) -> str:
        """The stack image called `name` (`sim`, `bridge`) at the configured prefix and tag."""
        return f"{self.docker.image_prefix}{name}:{self.docker.image_tag}"


def find_config(start: Path) -> Path | None:
    """The nearest ``fieldproof.toml`` in `start` or its parents."""
    for directory in (start.resolve(), *start.resolve().parents):
        candidate = directory / CONFIG_FILE
        if candidate.is_file():
            return candidate
    return None


def read_config(path: Path) -> Config:
    """Read and validate the file at `path`; its directory is the workspace root."""
    try:
        data = tomllib.loads(path.read_text())
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not valid TOML: {error}") from error
    try:
        config = Config.model_validate(data)
    except ValidationError as error:
        raise ConfigError(f"{path}: {format_errors(error)}") from error
    config._root = path.resolve().parent
    config._path = path.resolve()
    return config


def load_config(
    path: Path | str | None = None,
    *,
    start: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """The configuration: `path` if given, else the nearest file above `start`, else defaults.

    Without a file the workspace root is `start` (the current directory by default). The
    environment variables the compose stack reads override the file's values.
    """
    environment = os.environ if env is None else env
    if path is not None:
        config = read_config(Path(path))
    else:
        origin = Path(start) if start is not None else Path.cwd()
        found = find_config(origin) or find_config(Path.cwd())
        if found is not None:
            config = read_config(found)
        else:
            config = Config()
            config._root = origin.resolve()
    if ENV_IMAGE_PREFIX in environment:
        config.docker.image_prefix = environment[ENV_IMAGE_PREFIX]
    if ENV_IMAGE_TAG in environment:
        config.docker.image_tag = environment[ENV_IMAGE_TAG]
    if ENV_RENDER in environment:
        try:
            config.docker.render = Docker.model_validate({"render": environment[ENV_RENDER]}).render
        except ValidationError as error:
            raise ConfigError(f"{ENV_RENDER}: {format_errors(error)}") from error
    if ENV_DOMAIN_ID in environment:
        try:
            config.docker.ros_domain_id = int(environment[ENV_DOMAIN_ID])
        except ValueError as error:
            raise ConfigError(
                f"{ENV_DOMAIN_ID}={environment[ENV_DOMAIN_ID]!r}: not an integer"
            ) from error
    return config
