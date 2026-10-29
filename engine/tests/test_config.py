"""Tests of the workspace configuration, fieldproof.toml (task P1-T05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldproof.config import Config, ConfigError, find_config, load_config, read_config

REPO = Path(__file__).resolve().parents[2]
NO_ENV: dict[str, str] = {}


def test_repository_config_is_the_defaults() -> None:
    config = read_config(REPO / "fieldproof.toml")
    assert config.model_dump() == Config().model_dump(), "fieldproof.toml documents the defaults"
    assert config.root == REPO
    assert config.path == REPO / "fieldproof.toml"
    assert config.image("sim") == "ghcr.io/dimitrios-kafetzis/fieldproof-sim:local"
    assert config.resolve(config.stack.worlds) == REPO / "sim/fieldproof_worlds/worlds"


def test_find_config_walks_up() -> None:
    assert find_config(REPO / "examples/scenarios") == REPO / "fieldproof.toml"
    assert find_config(Path("/")) is None


def test_load_without_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # the current directory is the second place searched
    config = load_config(start=tmp_path, env=NO_ENV)
    assert config.path is None
    assert config.root == tmp_path.resolve()
    assert config.runs.directory == "runs"


def test_load_from_the_scenario_directory(tmp_path: Path) -> None:
    (tmp_path / "fieldproof.toml").write_text('[runs]\ndirectory = "out"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    config = load_config(start=nested, env=NO_ENV)
    assert config.runs.directory == "out"
    assert config.root == tmp_path.resolve()
    assert config.docker.network == "fieldproof"


def test_explicit_path(tmp_path: Path) -> None:
    file = tmp_path / "custom.toml"
    file.write_text('[docker]\nimage_tag = "main"\nrender = "software"\n[timeouts]\nworld_s = 5\n')
    config = load_config(file, env=NO_ENV)
    assert config.image("bridge") == "ghcr.io/dimitrios-kafetzis/fieldproof-bridge:main"
    assert config.docker.render == "software"
    assert config.timeouts.world_s == 5.0
    assert config.timeouts.spawn_s == 300.0
    assert config.timeouts.recorder_s == 60.0


def test_environment_overrides(tmp_path: Path) -> None:
    env = {
        "FIELDPROOF_IMAGE_PREFIX": "example.test/fp-",
        "FIELDPROOF_IMAGE_TAG": "sha-abc",
        "FIELDPROOF_RENDER": "software",
        "ROS_DOMAIN_ID": "7",
        "FIELDPROOF_DISCOVERY": "server",
    }
    config = load_config(start=tmp_path, env=env)
    assert config.image("sim") == "example.test/fp-sim:sha-abc"
    assert config.docker.render == "software"
    assert config.docker.ros_domain_id == 7
    assert config.docker.discovery == "server"
    assert load_config(start=tmp_path, env={"ROS_DOMAIN_ID": "auto"}).docker.ros_domain_id == "auto"
    with pytest.raises(ConfigError, match="ROS_DOMAIN_ID"):
        load_config(start=tmp_path, env={"ROS_DOMAIN_ID": "seven"})
    with pytest.raises(ConfigError, match="ROS_DOMAIN_ID"):
        load_config(start=tmp_path, env={"ROS_DOMAIN_ID": "300"})
    with pytest.raises(ConfigError):
        load_config(start=tmp_path, env={"FIELDPROOF_RENDER": "vulkan"})
    with pytest.raises(ConfigError, match="FIELDPROOF_DISCOVERY"):
        load_config(start=tmp_path, env={"FIELDPROOF_DISCOVERY": "gossip"})


def test_domain_and_discovery_defaults(tmp_path: Path) -> None:
    config = load_config(start=tmp_path, env=NO_ENV)
    assert config.docker.ros_domain_id == "auto", "a domain per run unless configured"
    assert config.docker.discovery == "multicast"
    assert config.timeouts.discovery_s == 60.0
    assert config.timeouts.contract_s == 180.0
    (tmp_path / "fieldproof.toml").write_text('[docker]\nros_domain_id = 5\ndiscovery = "server"\n')
    config = load_config(start=tmp_path, env=NO_ENV)
    assert config.docker.ros_domain_id == 5
    assert config.docker.discovery == "server"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[docker]\nnetwork = 3\n", "docker.network"),
        ("[docker]\nrender = 'metal'\n", "docker.render"),
        ("[docker]\nros_domain_id = 500\n", "ros_domain_id"),
        ("[docker]\nros_domain_id = 'any'\n", "ros_domain_id"),
        ("[docker]\ndiscovery = 'gossip'\n", "docker.discovery"),
        ("[timeouts]\ncontract_s = 0\n", "timeouts.contract_s"),
        ("[timeouts]\nworld_s = 0\n", "timeouts.world_s"),
        ("[runs]\nfolder = 'x'\n", "runs.folder"),
        ("[unknown]\nkey = 1\n", "unknown"),
        ("[docker\n", "not valid TOML"),
    ],
)
def test_config_errors(tmp_path: Path, text: str, message: str) -> None:
    file = tmp_path / "fieldproof.toml"
    file.write_text(text)
    with pytest.raises(ConfigError, match=message):
        read_config(file)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No such file"):
        read_config(tmp_path / "none.toml")


# ------------------------------------------------------------ the stack root (task P2-T08)
def test_stack_root_is_the_checkout_for_the_repository() -> None:
    config = read_config(REPO / "fieldproof.toml")
    assert config.stack_root == REPO
    assert not config.external
    assert config.stack_path("robots") == REPO / "robots/fieldproof_robots/models"
    assert config.stack_path("fuel_lock") == REPO / "assets/fuel.lock"


def test_stack_root_of_an_external_workspace(tmp_path: Path) -> None:
    """A workspace without the stack runs on the checkout the engine runs from."""
    (tmp_path / "fieldproof.toml").write_text('[runs]\ndirectory = "out"\n')
    config = load_config(start=tmp_path, env=NO_ENV)
    assert config.root == tmp_path.resolve()
    assert config.stack_root == REPO
    assert config.external
    assert config.stack_path("worlds") == REPO / "sim/fieldproof_worlds/worlds"
    # `[stack] root` names a checkout explicitly; FIELDPROOF_STACK overrides it.
    other = tmp_path / "checkouts" / "Fieldproof"
    other.mkdir(parents=True)
    (tmp_path / "fieldproof.toml").write_text('[stack]\nroot = "checkouts/Fieldproof"\n')
    config = load_config(start=tmp_path, env=NO_ENV)
    assert config.stack_root == other.resolve()
    assert config.stack_path("models") == other / "sim/fieldproof_worlds/models"
    config = load_config(start=tmp_path, env={"FIELDPROOF_STACK": str(REPO)})
    assert config.stack_root == REPO
    assert load_config(start=tmp_path, env={"FIELDPROOF_STACK": "  "}).stack_root == other
    # A workspace that holds the stack is its own root.
    nested = tmp_path / "ws"
    (nested / "sim/fieldproof_worlds/worlds").mkdir(parents=True)
    (nested / "fieldproof.toml").write_text("")
    config = load_config(start=nested, env=NO_ENV)
    assert config.stack_root == nested.resolve()
    assert not config.external
