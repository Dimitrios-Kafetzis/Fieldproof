"""Integration tests of the world events (task P2-T03, session 2): the WorldEvents plugin in
the simulation image, and the obstacle scenario of the definition of done.

1. The plugin's services against a bare simulator (the compose ``sim`` service with the depot,
   no vehicle), called with the requests the engine's events compose: ``spawn`` puts a crate
   where asked (``gz model`` reports its pose), ``teleport`` moves it, ``set_wind`` changes the
   wind the WindEffects system reports, ``remove`` deletes it, and a second removal is refused
   with the plugin's reason.
2. ``fieldproof run examples/scenarios/ugv_obstacle_replan.yaml``: the vehicle drives 4.5 m
   ahead from the loading dock; half a metre into the drive the ``when`` fires and
   ``spawn_object`` puts a crate on the straight line, 1.7 m ahead; Nav2 replans around it and
   the vehicle reaches the goal without a contact; ``remove_object`` clears the crate. The
   checks: exit code 0, every entry ``ok`` with the plugin's reports, the six assertions
   passed, the crate spawned while the vehicle was still north of it, the ground truth showing
   the vehicle leaving the straight line to pass the crate (the replanning, as the simulator
   saw it), the ``world.changed`` lines of the log, nothing left behind.

Needs the stack images (``make images``), the vehicle image (``make vehicle-image``) and the
Fuel models (``make assets``), like the other integration tests. Wall-clock limits are generous
for a CI runner under software rendering.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_ugv_nav2 import COMPOSE, REPO, run, sim_exec, wait_until, world_ready

from fieldproof.events.world import SpawnObject, pose_text, service_name
from fieldproof.world import Pose

SCENARIO = REPO / "examples/scenarios/ugv_obstacle_replan.yaml"
WORLD = "depot"
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
RUN_TIMEOUT_S = 1500.0
WORLD_TIMEOUT_S = 300.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}
CRATE_X, CRATE_Y, CRATE_HALF = 10.0, 0.8, 0.3
# How far from the straight line (x = 10) the vehicle's centre must get to pass a 0.6 m crate:
# half the crate plus half the vehicle's width (0.44 m) and a little more.
DETOUR_M = CRATE_HALF + 0.22 + 0.05


def gz_service(verb: str, request_type: str, request: str) -> str:
    """Call one of the plugin's services from the sim container, as the engine does."""
    service = service_name(WORLD, verb)
    command = (
        f"timeout 20 gz service -s {service} --reqtype {request_type} --reptype "
        f"gz.msgs.StringMsg --timeout 10000 --req {shlex.quote(request)}"
    )
    return sim_exec(command, timeout=60)


def reply(text: str) -> str:
    """The `data` of a `gz.msgs.StringMsg` reply as `gz service` prints it."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return str(json.loads(line[len("data: ") :]))
    raise AssertionError(f"no data in the reply:\n{text}")


def model_pose(name: str) -> tuple[float, float, float, float] | None:
    """x, y, z, yaw of a model from `gz model`, or None when there is no such model."""
    text = sim_exec(f"timeout 20 gz model -m {name} --pose", timeout=60, check=False)
    lines = [line.strip() for line in text.splitlines()]
    if f"- Name: {name}" not in lines:
        return None
    xyz = lines[lines.index("- Pose [ XYZ (m) ] [ RPY (rad) ]:") + 1].strip("[]").split()
    rpy = lines[lines.index("- Pose [ XYZ (m) ] [ RPY (rad) ]:") + 2].strip("[]").split()
    return float(xyz[0]), float(xyz[1]), float(xyz[2]), float(rpy[2])


@pytest.fixture
def simulator() -> Iterator[None]:
    """The compose `sim` service with the depot, alone, torn down afterwards."""
    run([sys.executable, str(REPO / "tools/assets/fetch_fuel.py"), "--quiet"], timeout=900)
    run([*COMPOSE, "up", "-d", "--wait", "--no-build", "sim"], timeout=300)
    try:
        wait_until(world_ready, WORLD_TIMEOUT_S, f"the {WORLD} world's services")
        yield
    finally:
        run([*COMPOSE, "stop", "sim"], timeout=120, check=False, quiet=True)
        run([*COMPOSE, "rm", "-f", "sim"], timeout=60, check=False, quiet=True)


@pytest.mark.integration
def test_01_world_services(simulator: None) -> None:
    services = sim_exec("timeout 20 gz service -l", timeout=60).split()
    assert {service_name(WORLD, v) for v in ("spawn", "remove", "teleport", "set_wind")} <= set(
        services
    ), "the sim image serves the plugin"
    assert model_pose("crate") is None

    # Spawn: the request the spawn_object event composes, a box standing on the ground.
    crate = SpawnObject.model_validate(
        {
            "action": "spawn_object",
            "model": "crate",
            "pose": {"x": 10.0, "y": 1.0, "yaw": 0.5},
            "shape": "box",
            "size": [0.6, 0.6, 0.5],
        }
    )
    pose = Pose(10.0, 1.0, 0.25, 0.5)
    request = (
        f'name: "crate" allow_renaming: false sdf: {json.dumps(crate.sdf())} '
        f"pose {{ {pose_text(pose)} }}"
    )
    assert reply(gz_service("spawn", "gz.msgs.EntityFactory", request)) == (
        "spawned model crate at (10, 1, 0.25, yaw 0.5)"
    )
    time.sleep(1.0)
    spawned = model_pose("crate")
    assert spawned is not None, "the crate is in the world"
    assert spawned[:3] == pytest.approx((10.0, 1.0, 0.25), abs=0.01)
    assert spawned[3] == pytest.approx(0.5, abs=0.01)
    assert reply(gz_service("spawn", "gz.msgs.EntityFactory", request)).startswith(
        "error: spawn: a model called crate already exists"
    )

    # Teleport.
    moved = Pose(12.0, -2.0, 0.25, 1.0)
    assert reply(gz_service("teleport", "gz.msgs.Pose", f'name: "crate" {pose_text(moved)}')) == (
        "teleported model crate to (12, -2, 0.25, yaw 1)"
    )
    time.sleep(1.0)
    after = model_pose("crate")
    assert after is not None
    assert after[:3] == pytest.approx((12.0, -2.0, 0.25), abs=0.02)
    assert after[3] == pytest.approx(1.0, abs=0.02)

    # Wind: the plugin sets the world's wind and WindEffects reports it.
    assert (
        reply(
            gz_service(
                "set_wind", "gz.msgs.Wind", "enable_wind: true linear_velocity { x: 4 y: -1 }"
            )
        )
        == "wind set to (4, -1, 0) m/s"
    )
    info = sim_exec(
        f"timeout 20 gz service -s /world/{WORLD}/wind_info --reqtype gz.msgs.Empty --reptype "
        "gz.msgs.Wind --timeout 5000 --req ''",
        timeout=60,
    )
    assert "enable_wind: true" in info
    assert "x: 4" in info
    assert "y: -1" in info
    assert reply(gz_service("set_wind", "gz.msgs.Wind", "enable_wind: false")) == "wind disabled"

    # Remove, twice.
    assert reply(gz_service("remove", "gz.msgs.Entity", 'name: "crate" type: MODEL')) == (
        "removed model crate"
    )
    time.sleep(1.0)
    assert model_pose("crate") is None
    assert reply(gz_service("remove", "gz.msgs.Entity", 'name: "crate" type: MODEL')) == (
        f"error: remove: no model called crate in world {WORLD}"
    )


def docker(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return completed.stdout


def containers_of(run_id: str) -> list[str]:
    return docker(
        "ps", "-a", "--filter", f"label={RUN_LABEL}={run_id}", "--format", "{{.Names}}"
    ).split()


def print_run_logs(run_dir: Path, tail: int = 120) -> None:
    for path in sorted(run_dir.glob("*.log")) + sorted(run_dir.glob("logs/*.log")):
        print(f"\n===== {path.relative_to(run_dir)} (last {tail} lines) =====", flush=True)  # noqa: T201
        print("\n".join(path.read_text().splitlines()[-tail:]), flush=True)  # noqa: T201


def log_events(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run_dir / "engine.log").read_text().splitlines()]


def pose_samples(run_dir: Path) -> list[dict[str, Any]]:
    """The vehicle's ground-truth samples of the observer's stream, in order."""
    samples = []
    for line in (run_dir / "samples.jsonl").read_text().splitlines():
        sample = json.loads(line)
        if sample.get("kind") == "pose" and sample.get("model") == "ugv1":
            samples.append(sample)
    return samples


@pytest.mark.integration
def test_02_obstacle_scenario(tmp_path: Path) -> None:
    run_id = f"it-obstacle-{os.getpid()}"
    command = [*FIELDPROOF, "run", str(SCENARIO), "--runs-dir", str(tmp_path), "--run-id", run_id]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603
        command, env=ENV, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
    )
    elapsed = time.monotonic() - started
    run_dir = tmp_path / run_id
    print(f"exit {completed.returncode} after {elapsed:.0f} s", flush=True)  # noqa: T201
    print(completed.stdout[-6000:], flush=True)  # noqa: T201
    if completed.returncode != 0:
        print(completed.stderr[-4000:], flush=True)  # noqa: T201
        if run_dir.is_dir():
            print_run_logs(run_dir)
    assert completed.returncode == 0, completed.stderr[-2000:]
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]

    timeline = {a["name"]: a for a in record["timeline"]}
    assert set(timeline) == {"drive_ahead", "spawn_crate", "clear_crate"}
    assert {a["status"] for a in record["timeline"]} == {"ok"}
    assert timeline["spawn_crate"]["trigger"]["when"]["metric"] == "distance_to"
    assert timeline["spawn_crate"]["detail"] == "spawned model crate at (10, 0.8, 0.25, yaw 0)"
    assert timeline["spawn_crate"]["params"]["shape"] == "box"
    assert timeline["clear_crate"]["detail"] == "removed model crate"
    assert timeline["clear_crate"]["trigger"] == {"at_s": 0.0, "after": "drive_ahead"}
    drive_end = timeline["drive_ahead"]["t_s"] + timeline["drive_ahead"]["duration_s"]
    assert timeline["clear_crate"]["t_s"] >= drive_end
    spawn_t = timeline["spawn_crate"]["t_s"]
    assert 0.0 < spawn_t < drive_end

    verdicts = {a["name"]: a["status"] for a in record["assertions"]}
    assert verdicts == {
        "crate_spawned": "passed",
        "reached_goal": "passed",
        "arrived_in_time": "passed",
        "kept_planning": "passed",
        "no_collision": "passed",
        "crate_cleared": "passed",
    }

    # The crate appeared while the vehicle was still north of it, and the vehicle left the
    # straight line to pass it (its centre at least DETOUR_M off x = 10 while beside the crate).
    samples = pose_samples(run_dir)
    assert samples, "the observer streamed the ground truth"
    south_of_crate = [s for s in samples if s["y"] < CRATE_Y - CRATE_HALF]
    assert south_of_crate, "the vehicle got past the crate"
    beside = [s for s in samples if CRATE_Y - 1.0 < s["y"] < CRATE_Y + 1.0]
    detour = max(abs(s["x"] - CRATE_X) for s in beside)
    print(f"detour around the crate: {detour:.2f} m off the straight line", flush=True)  # noqa: T201
    assert detour >= DETOUR_M, "the vehicle went around the crate"

    events = log_events(run_dir)
    changed = [e for e in events if e["event"] == "world.changed"]
    assert [e["verb"] for e in changed] == ["spawn", "remove"]
    assert [e["action"] for e in events if e["event"] == "action.triggered"] == [
        "spawn_crate",
        "clear_crate",
    ]
    assert containers_of(run_id) == []
