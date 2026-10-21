"""Tests for the Fieldproof worlds (task P1-T02) on the installed layout.

Every world carries the physics settings and systems the reference vehicles rely on; every
model a world includes is either one of this package's primitive models or pinned in
assets/fuel.lock with a permissive licence, and is installed by this package; every pinned
model is used by the world it says; no model carries a mesh collision (the real-time factor
depends on it); the depot has its named frames, rooms and slope; and the committed map is what
scripts/world_map.py generates from the world.
"""

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from ament_index_python.packages import get_package_share_directory

REPO = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parents[1]
SHARE = Path(get_package_share_directory("fieldproof_worlds"))
WORLDS = sorted((SHARE / "worlds").glob("*.sdf"))
MODELS = SHARE / "models"
PACKAGE_MODELS = {p.name for p in (PACKAGE / "models").iterdir() if (p / "model.config").is_file()}
LOCK = REPO / "assets" / "fuel.lock"
MAP = REPO / "docs" / "reference" / "worlds" / "depot-map.svg"
MAP_SCRIPT = PACKAGE / "scripts" / "world_map.py"
PERMISSIVE_LICENCES = {"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "Apache-2.0", "MIT", "BSD-3-Clause"}
REQUIRED_SYSTEMS = {
    "gz::sim::systems::Physics",
    "gz::sim::systems::UserCommands",
    "gz::sim::systems::SceneBroadcaster",
    "gz::sim::systems::Contact",
    "gz::sim::systems::Sensors",
    "gz::sim::systems::Imu",
    "gz::sim::systems::NavSat",
    "gz::sim::systems::WindEffects",
    "fieldproof::WorldEvents",
}
PHYSICS_STEP_S = 0.001
DEPOT_FRAMES = {
    "spawn_dock",
    "spawn_hall",
    "goal_office",
    "goal_workshop",
    "goal_aisle",
    "goal_plateau",
    "goal_gate",
}


def world_element(path: Path) -> ET.Element:
    world = ET.parse(path).getroot().find("world")  # noqa: S314
    assert world is not None, f"{path} holds no <world>"
    return world


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(LOCK.read_text())
    return data


@pytest.fixture(scope="module")
def depot() -> ET.Element:
    return world_element(SHARE / "worlds" / "depot.sdf")


def test_worlds_are_installed() -> None:
    assert {w.name for w in WORLDS} >= {"fieldproof_empty.sdf", "depot.sdf"}


@pytest.mark.parametrize("world_file", WORLDS, ids=[w.stem for w in WORLDS])
def test_world_physics_and_systems(world_file: Path) -> None:
    world = world_element(world_file)
    assert world.get("name") == world_file.stem, "the world name equals the file name"
    assert float(world.findtext("physics/max_step_size", "0")) == PHYSICS_STEP_S
    assert float(world.findtext("physics/real_time_factor", "0")) == 1.0
    systems = {p.get("name") for p in world.findall("plugin")}
    assert systems >= REQUIRED_SYSTEMS, f"missing systems: {REQUIRED_SYSTEMS - systems}"
    assert world.findtext("spherical_coordinates/surface_model") == "EARTH_WGS84"


def test_lock_entries_are_complete_and_permissive(lock: dict[str, Any]) -> None:
    assert lock["apiVersion"] == "fieldproof.dev/v0alpha1"
    assert lock["kind"] == "FuelLock"
    assert lock["server"].startswith("https://")
    dirs = [entry["dir"] for entry in lock["models"]]
    assert dirs == sorted(dirs), "entries are sorted by directory"
    assert len(set(dirs)) == len(dirs), "directories are unique"
    for entry in lock["models"]:
        assert entry["licence"] in PERMISSIVE_LICENCES, (entry["name"], entry["licence"])
        assert len(entry["sha256"]) == 64
        assert entry["size_bytes"] > 0
        assert entry["used_by"], f"{entry['name']} is pinned but used by no world"


def test_included_models_are_pinned_or_local_and_installed(lock: dict[str, Any]) -> None:
    pinned = {entry["dir"]: entry for entry in lock["models"]}
    assert not PACKAGE_MODELS & set(pinned), "a package model shadows a Fuel model"
    used_by: dict[str, set[str]] = {directory: set() for directory in pinned}
    for world_file in WORLDS:
        for include in world_element(world_file).findall("include"):
            uri = (include.findtext("uri") or "").strip()
            assert uri.startswith("model://"), uri
            directory = uri.removeprefix("model://").split("/")[0]
            assert directory in pinned or directory in PACKAGE_MODELS, (
                f"{world_file.name} includes {uri}: neither in assets/fuel.lock nor in models/"
            )
            if directory in pinned:
                used_by[directory].add(world_file.stem)
            assert (MODELS / directory / "model.config").is_file(), f"{directory} is not installed"
    for directory, entry in pinned.items():
        assert used_by[directory] == set(entry["used_by"]), (directory, entry["used_by"])


def model_sdf(directory: Path) -> ET.Element:
    config = ET.parse(directory / "model.config").getroot()  # noqa: S314
    sdf_files = [e.text.strip() for e in config.findall("sdf") if e.text]
    model = ET.parse(directory / sdf_files[-1]).getroot().find("model")  # noqa: S314
    assert model is not None, directory
    return model


def test_no_model_has_a_mesh_collision() -> None:
    """A mesh collision against the ground plane costs a large share of every physics step."""
    offenders = []
    for directory in sorted(p for p in MODELS.iterdir() if (p / "model.config").is_file()):
        for collision in model_sdf(directory).iter("collision"):
            if collision.find("geometry/mesh") is not None:
                offenders.append(directory.name)
    for world_file in WORLDS:
        for collision in world_element(world_file).iter("collision"):
            if collision.find("geometry/mesh") is not None:
                offenders.append(world_file.name)
    assert not offenders, offenders


def test_installed_models_carry_their_stamp(lock: dict[str, Any]) -> None:
    for entry in lock["models"]:
        stamp = MODELS / entry["dir"] / ".fieldproof-fuel.json"
        assert stamp.is_file(), stamp
        assert f'"sha256": "{entry["sha256"]}"' in stamp.read_text()


def test_depot_has_its_named_frames(depot: ET.Element) -> None:
    frames = {f.get("name"): f for f in depot.findall("frame")}
    assert set(frames) == DEPOT_FRAMES
    for name, frame in frames.items():
        pose = [float(v) for v in (frame.findtext("pose") or "").split()]
        assert len(pose) == 6, name
    plateau = [float(v) for v in (frames["goal_plateau"].findtext("pose") or "").split()]
    assert plateau[2] == pytest.approx(0.8), "the plateau goal sits on the 0.8 m plateau"


def test_depot_rooms_corridor_and_slope(depot: ET.Element) -> None:
    building = next(m for m in depot.findall("model") if m.get("name") == "depot_building")
    links = {x.get("name", "") for x in building.findall("link")}
    assert {"north_wall", "west_wall", "east_wall"} <= links
    assert sum(1 for x in links if x.startswith("corridor_south_")) >= 3, "doors along the corridor"
    assert sum(1 for x in links if x.startswith("corridor_north_")) >= 2, "openings into the hall"
    assert {"divider_office_workshop", "divider_workshop_bay_south"} <= links
    slope = next(m for m in depot.findall("model") if m.get("name") == "yard_slope")
    slope_links = {x.get("name"): x for x in slope.findall("link")}
    assert {"ramp_up", "plateau", "ramp_down"} <= set(slope_links)
    for name, sign in (("ramp_up", -1), ("ramp_down", 1)):
        pitch = float((slope_links[name].findtext("pose") or "").split()[4])
        assert sign * pitch > 0, f"{name} must slope the right way"
    racks = [m for m in depot.findall("include") if (m.findtext("name") or "").startswith("rack_")]
    assert len(racks) == 12, "three rows of four rack sections"
    assert all((m.findtext("static") or "").strip() == "true" for m in racks)


def test_depot_map_is_current() -> None:
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(MAP_SCRIPT),
            str(SHARE / "worlds" / "depot.sdf"),
            "--output",
            str(MAP),
            "--resource-path",
            str(MODELS),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
