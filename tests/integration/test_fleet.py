"""Integration test of the fleet (task P2-T01, its verification): three vehicles at once.

``fieldproof run examples/scenarios/ugv_fleet.yaml --check-contracts`` brings the simulator,
three bridges, the recorder and three example vehicles up through the Docker API, each vehicle
under its own namespace (``/ugv1``, ``/ugv2``, ``/ugv3``) and budget, with the bridge contract
of each vehicle checked before the recorder starts, sends the three navigation goals at once
and tears everything down. While the vehicles run, the test reads what Docker applied: the
cgroup limits in ``docker inspect`` (the CPU quota, the memory limit, the scenario's override
for ``ugv2``) and the memory limit ``docker stats`` shows; that every vehicle container carries
the run's ``ROS_DOMAIN_ID`` (not the compose stack's 0), its namespace and the mounted DDS
profile; and, from inside ``ugv1``, that no node of another vehicle subscribes to its scan or
odometry, that its own stack does, and that the vehicles' TF trees are separate topics (no
cross-talk: the definition of done). Afterwards: exit code 0, every vehicle at its own goal,
the twelve assertions passed, three bridge contracts passed, the MCAP holding the three
vehicles' namespaced topics, three vehicle and three bridge logs, and no container left.

Three Nav2 stacks and a simulator rendering three lidars need a workstation: the test skips
hosts with fewer than ``FLEET_MIN_CPUS`` cores (the GitHub-hosted runner has four) unless
``FIELDPROOF_IT_FLEET=1`` insists. Needs the stack images (``make images``), the vehicle image
(``make vehicle-image``) and the Fuel models (``make assets``).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_fleet.yaml"
VEHICLES = ("ugv1", "ugv2", "ugv3")
RUN_LABEL = "io.fieldproof.run"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUN_TIMEOUT_S = 1800.0
VEHICLES_UP_TIMEOUT_S = 900.0
FLEET_MIN_CPUS = 8
ENV = {**os.environ}


def docker(*args: str, timeout: float = 120.0) -> str:
    completed = subprocess.run(  # noqa: S603  (fixed argument list)
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return completed.stdout


def containers_of(run_id: str, role: str | None = None, running_only: bool = False) -> list[str]:
    args = ["ps", "--filter", f"label={RUN_LABEL}={run_id}", "--format", "{{.Names}}"]
    if not running_only:
        args.insert(1, "-a")
    if role is not None:
        args += ["--filter", f"label=io.fieldproof.role={role}"]
    return docker(*args).split()


def plain(text: str) -> str:
    return ANSI.sub("", text)


def print_run_logs(run_dir: Path, tail: int = 120) -> None:
    for path in sorted(run_dir.glob("*.log")) + sorted(run_dir.glob("logs/*.log")):
        print(f"\n===== {path.relative_to(run_dir)} (last {tail} lines) =====", flush=True)  # noqa: T201
        print("\n".join(path.read_text().splitlines()[-tail:]), flush=True)  # noqa: T201


def wait_for_vehicles(run_id: str, process: subprocess.Popen[str]) -> list[str]:
    """The three vehicle containers, once all are running (the run may still be probing them)."""
    deadline = time.monotonic() + VEHICLES_UP_TIMEOUT_S
    while True:
        names = containers_of(run_id, "vehicle", running_only=True)
        if len(names) == len(VEHICLES):
            return sorted(names)
        if process.poll() is not None:
            pytest.fail(f"fieldproof run exited {process.returncode} before the vehicles started")
        if time.monotonic() > deadline:
            process.terminate()
            pytest.fail(f"the vehicles never all started (running: {names})")
        time.sleep(2.0)


def inspect(name: str) -> dict[str, Any]:
    return dict(json.loads(docker("inspect", name))[0])


def exec_in(name: str, script: str, timeout: float = 180.0) -> str:
    """A shell script run through the vehicle image's entry point (ROS sourced)."""
    completed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            name,
            "/usr/local/bin/fieldproof-vehicle-entrypoint",
            "bash",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    return completed.stdout + completed.stderr


def check_limits(names: list[str], run_dir: Path) -> None:
    """`docker inspect` and `docker stats` show the budgets the plan applied."""
    expected_memory = {"ugv1": 2 * 1024**3, "ugv2": 3 * 1024**3, "ugv3": 2 * 1024**3}
    stats = docker(
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}",
        *names,
        timeout=60,
    )
    print(f"docker stats:\n{stats}", flush=True)  # noqa: T201
    limits = {
        line.split("\t")[0]: line.split("\t")[1].split("/")[-1].strip()
        for line in stats.strip().splitlines()
    }
    for name in names:
        vehicle = name.rsplit("-", 1)[-1]
        host_config = inspect(name)["HostConfig"]
        assert host_config["NanoCpus"] == 2_000_000_000, (name, host_config["NanoCpus"])
        assert host_config["Memory"] == expected_memory[vehicle], (name, host_config["Memory"])
        assert host_config["CpusetCpus"] == "", "the example pins no cores"
        assert limits[name] == ("3GiB" if vehicle == "ugv2" else "2GiB"), (name, limits[name])


def check_isolation(names: list[str], run_id: str) -> None:
    """The run's domain and each vehicle's namespace and DDS profile; no cross-talk."""
    record_domain: str | None = None
    for name in names:
        vehicle = name.rsplit("-", 1)[-1]
        config = inspect(name)["Config"]
        env = dict(item.split("=", 1) for item in config["Env"] if "=" in item)
        assert env["FIELDPROOF_NAMESPACE"] == vehicle
        assert env["FASTRTPS_DEFAULT_PROFILES_FILE"] == "/opt/fieldproof/dds/profile.xml"
        assert env["ROS_DOMAIN_ID"] != "0", "a domain of the run, not the compose stack's"
        record_domain = record_domain or env["ROS_DOMAIN_ID"]
        assert env["ROS_DOMAIN_ID"] == record_domain
        assert config["Labels"]["io.fieldproof.domain"] == record_domain
        mounts = {m["Destination"]: m for m in inspect(name)["Mounts"]}
        profile = mounts["/opt/fieldproof/dds/profile.xml"]
        assert profile["RW"] is False
        assert profile["Source"].endswith("ugv_nav2_bringup/config/fastdds.xml")
        assert "namespace:=" + vehicle in " ".join(config["Cmd"])
    ugv1 = next(n for n in names if n.endswith("-ugv1"))
    graph = exec_in(
        ugv1,
        "ros2 node list --no-daemon --spin-time 5; echo ===; "
        "ros2 topic info -v /ugv1/scan --no-daemon --spin-time 5; echo ===; "
        "ros2 topic info -v /ugv1/odom --no-daemon --spin-time 5; echo ===; "
        "ros2 topic list --no-daemon --spin-time 5; echo ===; "
        "ros2 topic info /tf --no-daemon --spin-time 5; ros2 topic info /map --no-daemon; "
        "ros2 topic info /scan --no-daemon",
    )
    nodes, scan_info, odom_info, topics, root = graph.split("===")
    root_topics = [t for t in topics.split() if not t.startswith(("/ugv1/", "/ugv2/", "/ugv3/"))]
    print(f"nodes seen from ugv1: {nodes.split()}", flush=True)  # noqa: T201
    print(f"topics outside the namespaces: {root_topics}", flush=True)  # noqa: T201
    print(f"root TF, map and scan:\n{root}", flush=True)  # noqa: T201
    for vehicle in VEHICLES:
        assert f"/{vehicle}/bt_navigator" in nodes, vehicle
        assert f"/{vehicle}/slam_toolbox" in nodes, vehicle
        assert f"/{vehicle}/tf" in topics, "one TF tree per vehicle"
        assert f"/{vehicle}/map" in topics
    # The readiness probes subscribe to the global TF topics too (a stack keeping one shared
    # tree), so `/tf` may be listed; what matters is that nobody publishes a global tree, a
    # global map or a root scan.
    publishers = re.findall(r"Publisher count: (\d+)", root)
    assert all(count == "0" for count in publishers), f"root publishers:\n{root}"
    for info, what in ((scan_info, "/ugv1/scan"), (odom_info, "/ugv1/odom")):
        subscribers = re.findall(r"Node namespace: (\S+)", info.split("Subscription count")[-1])
        assert subscribers, f"nobody subscribes to {what}:\n{info}"
        foreign = [ns for ns in subscribers if ns.startswith(("/ugv2", "/ugv3"))]
        assert not foreign, f"{what} is read by another vehicle's nodes: {foreign}"
        assert any(ns == "/ugv1" for ns in subscribers), f"ugv1's own stack reads {what}"


def mcap_topics(path: Path) -> dict[str, int]:
    from mcap.reader import make_reader  # noqa: PLC0415

    with path.open("rb") as file:
        reader = make_reader(file)
        summary = reader.get_summary()
        assert summary is not None
        assert summary.statistics is not None
        counts = summary.statistics.channel_message_counts
        return {c.topic: counts.get(cid, 0) for cid, c in summary.channels.items()}


@pytest.mark.integration
def test_fleet_of_three_ugvs(tmp_path: Path) -> None:  # noqa: PLR0915
    cpus = os.cpu_count() or 0
    if cpus < FLEET_MIN_CPUS and os.environ.get("FIELDPROOF_IT_FLEET") != "1":
        pytest.skip(f"three Nav2 stacks want {FLEET_MIN_CPUS} cores, this host has {cpus}")
    run_id = f"it-fleet-{os.getpid()}"
    command = [
        *FIELDPROOF,
        "run",
        str(SCENARIO),
        "--runs-dir",
        str(tmp_path),
        "--run-id",
        run_id,
        "--check-contracts",
        "-q",
    ]
    print(f"$ {shlex.join(command)}", flush=True)  # noqa: T201
    started = time.monotonic()
    with subprocess.Popen(  # noqa: S603
        command, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as process:
        try:
            names = wait_for_vehicles(run_id, process)
            print(f"vehicles up after {time.monotonic() - started:.0f} s: {names}", flush=True)  # noqa: T201
            check_limits(names, tmp_path / run_id)
            check_isolation(names, run_id)
        except BaseException:
            process.terminate()
            process.wait(timeout=180)
            raise
        output, _ = process.communicate(timeout=RUN_TIMEOUT_S)
    print(plain(output)[-6000:], flush=True)  # noqa: T201
    print(f"fieldproof run took {time.monotonic() - started:.0f} s wall-clock", flush=True)  # noqa: T201
    run_dir = tmp_path / run_id
    if process.returncode != 0:
        print_run_logs(run_dir)
    assert process.returncode == 0, plain(output)[-3000:]
    assert containers_of(run_id) == [], "the run left containers behind"
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "passed", record["reason"]
    assert record["stack"]["domain_source"] == "auto"
    assert record["stack"]["discovery"] == "multicast"
    assert [v["namespace"] for v in record["vehicles"]] == list(VEHICLES)
    for vehicle in record["vehicles"]:
        assert vehicle["contract"]["failed"] == 0, vehicle
        assert vehicle["contract"]["passed"] > 10, vehicle
    assert record["vehicles"][1]["resources"] == {"cpus": 2.0, "memory": "3g"}
    assert [a["status"] for a in record["timeline"]] == ["ok"] * 3
    verdicts = {a["name"]: a for a in record["assertions"]}
    assert len(verdicts) == 12
    for name, assertion in verdicts.items():
        print(f"{name}: {assertion['reason']}", flush=True)  # noqa: T201
    assert {a["status"] for a in verdicts.values()} == {"passed"}, {
        n: a["reason"] for n, a in verdicts.items() if a["status"] != "passed"
    }
    for vehicle in VEHICLES:
        assert verdicts[f"{vehicle}_reached"]["evidence"]["reached"]["distance_m"] <= 0.5
        assert verdicts[f"{vehicle}_no_collision"]["evidence"]["messages"] == 0
        assert verdicts[f"{vehicle}_speed_limit"]["evidence"]["samples"] > 10
    assert sorted(p.name for p in (run_dir / "logs").iterdir()) == [
        "bridge-ugv1.log",
        "bridge-ugv2.log",
        "bridge-ugv3.log",
        "recorder.log",
        "sim.log",
        "vehicle-ugv1.log",
        "vehicle-ugv2.log",
        "vehicle-ugv3.log",
    ]
    for vehicle in VEHICLES:
        log = (run_dir / "logs" / f"vehicle-{vehicle}.log").read_text()
        assert "bt_navigator" in log
        assert "Goal reached" in log or "Reached the goal" in log or "succeeded" in log.lower()
    (mcap,) = sorted((run_dir / "recording").glob("*.mcap"))
    topics = mcap_topics(mcap)
    print(f"recorded topics: {sorted(topics)}", flush=True)  # noqa: T201
    for vehicle in VEHICLES:
        assert topics.get(f"/{vehicle}/odom", 0) > 50, vehicle
        assert topics.get(f"/{vehicle}/cmd_vel", 0) > 10, vehicle
        assert topics.get(f"/{vehicle}/tf", 0) > 50, vehicle
    assert "/odom" not in topics, "nothing at the root"
    assert "/tf" not in topics, "nothing at the root"
    events = [json.loads(line) for line in (run_dir / "engine.log").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds.count("contract.ok") == 3
    assert kinds.count("vehicle.ready") == 3
    assert "vehicle.dependency" in kinds, "ugv3 waited for ugv1"
    budget = next(e for e in events if e["event"] == "budget")
    assert budget["cpus"] == 6.0
    assert "PASS ugv_fleet" in plain(output)
