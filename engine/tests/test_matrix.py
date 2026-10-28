"""Tests of the run matrix (task P2-T06): the file, the cases, the scheduler, the run."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import v0_text
from rich.console import Console
from typer.testing import CliRunner

from fieldproof.backend import HostInfo
from fieldproof.cli import app
from fieldproof.config import Config, load_config
from fieldproof.matrix import (
    BRIDGE_DEMAND,
    CONSOLE_LOG,
    ENGINE_RESERVE,
    MATRIX_COPY,
    MATRIX_HTML,
    MATRIX_JUNIT,
    MATRIX_LOG,
    MATRIX_MD,
    MATRIX_RECORD,
    RECORDER_DEMAND,
    SIM_DEMAND,
    Capacity,
    Demand,
    Matrix,
    MatrixError,
    MatrixRun,
    Scheduler,
    command_from_env,
    estimate_demand,
    host_info,
    load_matrix,
    make_matrix_id,
    render_matrix_junit,
    resolve_cases,
    validate_matrix_data,
)
from fieldproof.matrix_cli import parse_parallel, select_cases
from fieldproof.rundb import RunDatabase
from fieldproof.runner import RunLog

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"
EXAMPLE_MATRIX = REPO / "examples/matrices/ugv_nav_matrix.yaml"
STUB = Path(__file__).resolve().parent / "fake_run.py"
COMMAND = (sys.executable, str(STUB))
GIB = 1024**3
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """The output without ANSI codes (CI sets FORCE_COLOR) and with its whitespace folded."""
    return " ".join(ANSI.sub("", text).split())


def matrix_text(**fields: Any) -> str:
    data: dict[str, Any] = {
        "apiVersion": "fieldproof.dev/v0alpha1",
        "kind": "Matrix",
        "metadata": {"name": "sweep", "description": "a test sweep"},
        "scenario": str(EXAMPLE),
        "retries": 1,
        "parallel": "auto",
    }
    data.update(fields)
    return str(yaml.safe_dump(data, sort_keys=False))


def write_matrix(path: Path, **fields: Any) -> Matrix:
    path.write_text(matrix_text(**fields))
    return load_matrix(path)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(REPO / "fieldproof.toml", env={})


@pytest.fixture
def capacity() -> Capacity:
    return Capacity.of(HostInfo(16, 32 * GIB))


def console(tmp_path: Path) -> Console:
    return Console(file=(tmp_path / "console.txt").open("a"), width=200)


def execute(
    spec: Matrix,
    config: Config,
    tmp_path: Path,
    capacity: Capacity,
    *,
    env: dict[str, str] | None = None,
    **options: Any,
) -> tuple[dict[str, Any], MatrixRun, Path]:
    """Run the matrix with the stub command; return the record, the run and the directory."""
    cases = resolve_cases(spec, config, spec.expand(), render="software")
    matrix_id = make_matrix_id(
        spec,
        options.pop("now", None) or __import__("datetime").datetime.now(__import__("datetime").UTC),
        options.pop("token", "abc123"),
    )
    matrix_dir = tmp_path / "runs" / matrix_id
    log = RunLog(
        matrix_id, file=matrix_dir / MATRIX_LOG, console=console(tmp_path), component="matrix"
    )
    for key, value in (env or {}).items():
        os.environ[key] = value
    try:
        run = MatrixRun(
            spec,
            cases,
            matrix_id=matrix_id,
            matrix_dir=matrix_dir,
            capacity=capacity,
            log=log,
            command=COMMAND,
            database=tmp_path / "runs" / "fieldproof.sqlite",
            poll_s=0.02,
            **options,
        )
        record = run.execute()
    finally:
        for key in env or {}:
            os.environ.pop(key, None)
    return record, run, matrix_dir


# ------------------------------------------------------------------------------------ the file
def test_load_and_expand(tmp_path: Path) -> None:
    spec = write_matrix(
        tmp_path / "m.yaml",
        seeds=[1, 2],
        parameters={"time.rtf": [1.0, 2.0], "vehicles[ugv1].resources.cpus": [1, 2]},
        cases=[{"name": "tight", "set": {"assertions[reached_goal].tolerance_m": 0.2}}],
    )
    assert spec.name == "sweep"
    assert spec.scenario_path() == EXAMPLE.resolve()
    assert spec.digest
    assert len(spec.digest) == 8
    assert spec.text == (tmp_path / "m.yaml").read_text()
    cases = spec.expand()
    assert len(cases) == 10, "2 x 2 parameters plus one explicit case, each with two seeds"
    assert [c.id for c in cases][:3] == ["c01", "c02", "c03"]
    assert cases[0].name == "time.rtf=1.0, vehicles[ugv1].resources.cpus=1, seed=1"
    assert cases[0].overrides == {"time.rtf": 1.0, "vehicles[ugv1].resources.cpus": 1, "seed": 1}
    assert cases[1].seed == 2
    assert cases[2].overrides["time.rtf"] == 1.0
    assert cases[2].overrides["vehicles[ugv1].resources.cpus"] == 2, "the last axis varies fastest"
    assert cases[8].name == "tight, seed=1"
    assert cases[8].overrides == {"assertions[reached_goal].tolerance_m": 0.2, "seed": 1}
    assert cases[9].to_dict()["index"] == 9
    # Seeds only, explicit cases only, nothing at all.
    assert [c.name for c in write_matrix(tmp_path / "s.yaml", seeds=[5]).expand()] == ["seed=5"]
    only = write_matrix(tmp_path / "c.yaml", cases=[{"name": "one"}, {"name": "two"}]).expand()
    assert [c.name for c in only] == ["one", "two"]
    assert [c.name for c in write_matrix(tmp_path / "n.yaml").expand()] == ["default"]
    wide = write_matrix(tmp_path / "w.yaml", parameters={"seed": list(range(120))}).expand()
    assert wide[0].id == "c001"
    assert wide[-1].id == "c120"
    assert wide[0].seed is None, "a `seed` parameter is an override like any other"


def test_file_errors(tmp_path: Path) -> None:
    with pytest.raises(MatrixError, match="No such file"):
        load_matrix(tmp_path / "missing.yaml")
    (tmp_path / "bad.yaml").write_text("a: [")
    with pytest.raises(MatrixError, match="not valid YAML"):
        load_matrix(tmp_path / "bad.yaml")
    (tmp_path / "list.yaml").write_text("- 1\n")
    with pytest.raises(MatrixError, match="a matrix is a YAML mapping"):
        load_matrix(tmp_path / "list.yaml")
    with pytest.raises(MatrixError, match="kind"):
        validate_matrix_data({"apiVersion": "fieldproof.dev/v0alpha1", "kind": "Scenario"})
    for fields, message in (
        ({"seeds": [1, 1]}, "unique"),
        ({"seeds": [-1]}, "between 0 and"),
        ({"parameters": {"time.rtf": []}}, "at least one value"),
        ({"parameters": {"a-b": [1]}}, "is not a key"),
        ({"cases": [{"name": "x", "set": {"a[": 1}}]}, "is not a key"),
        ({"cases": [{"name": "Bad Name"}]}, "name"),
        ({"retries": -1}, "retries"),
        ({"parallel": 0}, "parallel"),
        ({"parallel": "lots"}, "parallel"),
        ({"resources": {"memory": "much"}}, "memory"),
        ({"seeds": [1], "parameters": {"seed": [2]}}, "two ways to say the same"),
        ({"extra": 1}, "extra"),
    ):
        with pytest.raises(MatrixError, match=message):
            write_matrix(tmp_path / "e.yaml", **fields)
    with pytest.raises(MatrixError, match="not loaded from a file"):
        validate_matrix_data(yaml.safe_load(matrix_text())).directory  # noqa: B018


def test_the_example_matrix(config: Config) -> None:
    spec = load_matrix(EXAMPLE_MATRIX)
    cases = spec.expand()
    assert len(cases) == 12, "the definition of done: a matrix of 12 runs"
    assert spec.scenario_path() == EXAMPLE.resolve()
    resolved = resolve_cases(spec, config, cases, render="software")
    assert {r.plan.seed for r in resolved} == set(spec.seeds)
    assert {r.plan.vehicles[0].resources.cpus for r in resolved} == {
        float(v) for v in spec.parameters["vehicles[ugv1].resources.cpus"]
    }
    assert all(r.scenario.digest != resolved[0].scenario.digest for r in resolved[1:])


def test_resolve_cases_names_the_bad_case(config: Config, tmp_path: Path) -> None:
    spec = write_matrix(
        tmp_path / "m.yaml",
        cases=[{"name": "ok"}, {"name": "bad", "set": {"vehicles[ugv9].env": {}}}],
    )
    with pytest.raises(MatrixError, match=r"case c02 \(bad\): .*no item named 'ugv9'"):
        resolve_cases(spec, config, spec.expand())


# -------------------------------------------------------------------------------- scheduling
def test_demand_and_capacity(config: Config, tmp_path: Path) -> None:
    spec = write_matrix(tmp_path / "m.yaml", parameters={"vehicles[ugv1].resources.cpus": [1, 3]})
    first, second = resolve_cases(spec, config, spec.expand())
    expected = SIM_DEMAND + RECORDER_DEMAND + Demand(1.0, 2 * GIB) + BRIDGE_DEMAND
    assert first.demand == expected, "the vehicle's budget (2g memory from the manifest)"
    assert second.demand.cpus == expected.cpus + 2.0
    assert first.to_dict()["demand"] == {"cpus": 4.0, "memory_bytes": expected.memory_bytes}
    override = write_matrix(tmp_path / "o.yaml", resources={"cpus": 6, "memory": "6g"})
    (only,) = resolve_cases(override, config, override.expand())
    assert only.demand == Demand(6.0, 6 * GIB)
    assert estimate_demand(first.plan) == expected
    capacity = Capacity.of(HostInfo(16, 32 * GIB))
    assert capacity.cpus == 16 - ENGINE_RESERVE.cpus
    assert capacity.memory_bytes == 32 * GIB - ENGINE_RESERVE.memory_bytes
    assert capacity.to_dict()["host"] == {"cpus": 16, "memory_bytes": 32 * GIB}
    unknown = Capacity.of(HostInfo(0, 0))
    assert math.isinf(unknown.cpus)
    assert math.isinf(unknown.memory_bytes)
    assert unknown.to_dict()["cpus"] is None
    host = host_info()
    assert host.cpus >= 1
    assert host.memory_bytes > 0


def test_scheduler() -> None:
    capacity = Capacity.of(HostInfo(8, 16 * GIB))  # 7 CPUs, 15 GiB
    auto = Scheduler(capacity, "auto")
    small = Demand(3.0, 4 * GIB)
    assert auto.can_start(small, [])
    assert auto.can_start(small, [small]), "6 of 7 CPUs"
    assert not auto.can_start(small, [small, small]), "9 CPUs would exceed 7"
    heavy = Demand(10.0, 1 * GIB)
    assert auto.can_start(heavy, []), "one case always runs"
    assert not auto.can_start(small, [heavy])
    assert not auto.can_start(Demand(1.0, 12 * GIB), [small]), "memory counts too"
    fixed = Scheduler(capacity, 3)
    assert fixed.can_start(heavy, [heavy, heavy]), "an explicit count is honoured"
    assert not fixed.can_start(heavy, [heavy, heavy, heavy])
    assert fixed.to_dict() == {"parallel": 3, "capacity": capacity.to_dict()}
    assert parse_parallel(None) is None
    assert parse_parallel("auto") is None
    assert parse_parallel("3") == 3
    for bad in ("0", "-1", "two"):
        with pytest.raises(MatrixError, match="--parallel"):
            parse_parallel(bad)


# ------------------------------------------------------------------------------------ running
def test_matrix_passes(config: Config, tmp_path: Path, capacity: Capacity) -> None:  # noqa: PLR0915
    spec = write_matrix(
        tmp_path / "m.yaml",
        seeds=[1, 2],
        parameters={"vehicles[ugv1].resources.cpus": [1, 2]},
        retries=1,
    )
    log_file = tmp_path / "fake.log"
    record, run, matrix_dir = execute(
        spec, config, tmp_path, capacity, env={"FAKE_RUN_LOG": str(log_file)}, parallel=2
    )
    assert record["status"] == "passed"
    assert record["verdict"] == "PASS"
    assert record["exit_code"] == 0
    assert run.exit_code() == 0
    assert record["counts"] == {
        "cases": 4,
        "passed": 4,
        "failed": 0,
        "flaky": 0,
        "interrupted": 0,
        "skipped": 0,
        "attempts": 4,
    }
    assert record["reason"] == "4 of 4 case(s) passed; 4 attempt(s)"
    assert record["schedule"]["parallel"] == 2
    assert record["schedule"]["max_concurrent"] <= 2
    assert record["schedule"]["retries"] == 1
    assert record["matrix"] == {
        "name": "sweep",
        "description": "a test sweep",
        "tags": [],
        "path": str(tmp_path / "m.yaml"),
        "digest": spec.digest,
    }
    assert record["scenario"] == {"name": "ugv_nav_to_goal", "path": str(EXAMPLE.resolve())}
    assert record["started_at"]
    assert record["finished_at"]
    assert record["duration_s"] is not None
    cases = record["cases"]
    assert [c["id"] for c in cases] == ["c01", "c02", "c03", "c04"]
    first = cases[0]
    assert first["status"] == "passed"
    assert first["verdict"] == "PASS"
    assert not first["flaky"]
    assert first["seed"] == 1
    assert first["overrides"] == {"vehicles[ugv1].resources.cpus": 1, "seed": 1}
    assert first["demand"]["cpus"] == 4.0
    assert first["kpis"]["drive.length_m"]["value"] == pytest.approx(4.1)
    assert first["kpis"]["stack.cpu_pct"]["value"] == 100.0
    (attempt,) = first["attempts"]
    assert attempt["number"] == 1
    assert attempt["run_id"].endswith("-abc123c01a1")
    assert attempt["run_id"].startswith(run.cases[0].resolved.scenario.digest)
    assert attempt["status"] == "passed"
    assert attempt["exit_code"] == 0
    assert attempt["assertion_counts"]["passed"] == 2
    assert attempt["duration_s"] == pytest.approx(60.0), "the report's duration once there is one"
    assert first["run_id"] == attempt["run_id"]
    # Every attempt got its own domain and run directory with the run's files and its console.
    domains = [c["attempts"][0]["domain_id"] for c in cases]
    assert all(1 <= d <= 100 for d in domains)
    for case in cases:
        run_dir = matrix_dir / case["run_id"]
        assert (run_dir / "run.json").is_file()
        assert (run_dir / "report.json").is_file()
        assert (run_dir / CONSOLE_LOG).is_file()
    # The stub saw the command line the matrix builds.
    invocations = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(invocations) == 4
    (invocation,) = [i for i in invocations if i["run_id"].endswith("c01a1")]
    argv = invocation["argv"]
    assert argv[0] == "run"
    assert argv[1] == str(EXAMPLE.resolve())
    assert argv[argv.index("--runs-dir") + 1] == str(matrix_dir)
    assert argv[argv.index("--run-id") + 1] == invocation["run_id"]
    assert "--quiet" in argv
    assert "--domain-id" in argv
    assert argv[argv.index("--database") + 1] == str(tmp_path / "runs" / "fieldproof.sqlite")
    sets = [argv[i + 1] for i, a in enumerate(argv) if a == "--set"]
    assert sorted(sets) == ["seed=1", "vehicles[ugv1].resources.cpus=1"]
    # The directory: the file as given, the record, the log, the JUnit, the reports.
    assert (matrix_dir / MATRIX_COPY).read_text() == spec.text
    assert json.loads((matrix_dir / MATRIX_RECORD).read_text()) == record
    events = [json.loads(line) for line in (matrix_dir / MATRIX_LOG).read_text().splitlines()]
    assert events[0]["event"] == "matrix.start"
    assert events[0]["component"] == "matrix"
    assert events[0]["run_id"] == record["matrix_id"]
    assert [e["event"] for e in events].count("case.start") == 4
    assert [e["event"] for e in events].count("case.end") == 4
    assert events[-1]["event"] == "matrix.end"
    assert "database.recorded" in [e["event"] for e in events]
    assert (matrix_dir / MATRIX_MD).read_text().startswith("# Fieldproof matrix report: sweep")
    html = (matrix_dir / MATRIX_HTML).read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert "Fieldproof matrix report: sweep" in html
    assert f'href="{first["run_id"]}/report.html"' in html
    assert "drive.length_m (m)" in html
    # One JUnit document: a suite per case with the case's properties and the run's cases.
    root = ET.fromstring((matrix_dir / MATRIX_JUNIT).read_text())  # noqa: S314 (our own document)
    assert root.get("name") == "fieldproof matrix sweep"
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == [f"ugv_nav_to_goal[{c['name']}]" for c in cases]
    assert root.get("tests") == str(sum(int(s.get("tests") or 0) for s in suites))
    assert root.get("failures") == "0"
    assert root.get("errors") == "0"
    properties = {p.get("name"): p.get("value") for p in suites[0].iter("property")}
    assert properties["case"] == "c01"
    assert properties["case_name"] == first["name"]
    assert properties["flaky"] == "false"
    assert properties["attempts"] == "1"
    assert properties["matrix_id"] == record["matrix_id"]
    assert json.loads(properties["parameters"] or "") == first["overrides"]
    assert properties["overrides"] == json.dumps(first["overrides"], sort_keys=True)
    assert "kpi.drive.length_m" in properties
    assert [c.get("name") for c in suites[0].findall("testcase")][:2] == [
        "reached_goal",
        "lidar_streams",
    ]
    # The database has the matrix and its runs with their case columns.
    with RunDatabase(tmp_path / "runs" / "fieldproof.sqlite") as db:
        assert db.counts() == {"runs": 4, "matrices": 1}
        matrix_row = db.matrix(record["matrix_id"])
        assert matrix_row is not None
        assert matrix_row["verdict"] == "PASS"
        assert matrix_row["cases"] == 4
        rows = db.runs(matrix_id=record["matrix_id"])
        assert {r["case_id"] for r in rows} == {"c01", "c02", "c03", "c04"}
        assert all(r["attempt"] == 1 and r["flaky"] is False for r in rows)
        assert db.kpis(first["run_id"])["drive.length_m"]["value"] == pytest.approx(4.1)


def test_retries_flakes_and_failures(config: Config, tmp_path: Path, capacity: Capacity) -> None:  # noqa: PLR0915
    spec = write_matrix(
        tmp_path / "m.yaml",
        parameters={"metadata.description": ["pass", "flaky", "fail", "crash", "usage"]},
        retries=1,
    )
    record, run, matrix_dir = execute(spec, config, tmp_path, capacity, parallel=1)
    assert record["status"] == "failed"
    assert record["verdict"] == "FAIL"
    assert record["exit_code"] == 1
    assert record["counts"] == {
        "cases": 5,
        "passed": 2,
        "failed": 3,
        "flaky": 1,
        "interrupted": 0,
        "skipped": 0,
        "attempts": 8,
    }
    assert record["reason"] == "2 of 5 case(s) passed, 3 failed, 1 flaky; 8 attempt(s)"
    by_name = {c["name"].split("=")[1].strip('"'): c for c in record["cases"]}
    assert by_name["pass"]["status"] == "passed"
    assert len(by_name["pass"]["attempts"]) == 1
    flaky = by_name["flaky"]
    assert flaky["status"] == "passed"
    assert flaky["flaky"] is True
    assert [a["status"] for a in flaky["attempts"]] == ["failed", "passed"]
    assert flaky["attempts"][0]["run_id"].endswith("c02a1")
    assert flaky["attempts"][1]["run_id"].endswith("c02a2")
    assert flaky["run_id"] == flaky["attempts"][1]["run_id"], "the case's run is the final attempt"
    failed = by_name["fail"]
    assert failed["status"] == "failed"
    assert not failed["flaky"]
    assert [a["status"] for a in failed["attempts"]] == ["failed", "failed"], "retried once"
    assert failed["attempts"][1]["reason"] == "assertion reached_goal failed: 1.20 m from the goal"
    assert failed["attempts"][1]["exit_code"] == 1
    crashed = by_name["crash"]
    assert crashed["status"] == "error"
    assert [a["exit_code"] for a in crashed["attempts"]] == [3, 3], "a crash is retried too"
    assert crashed["attempts"][0]["reason"] == "fieldproof run exited 3 without a verdict"
    assert crashed["kpis"] == {}
    usage = by_name["usage"]
    assert usage["status"] == "error"
    assert len(usage["attempts"]) == 1, "a usage error is never retried"
    assert usage["attempts"][0]["exit_code"] == 2
    assert "usage error" in usage["attempts"][0]["reason"]
    assert "not a number" in usage["attempts"][0]["reason"], "the console's tail"
    events = [json.loads(line) for line in (matrix_dir / MATRIX_LOG).read_text().splitlines()]
    retries = [e for e in events if e["event"] == "case.retry"]
    assert [e["case"] for e in retries] == ["c02", "c03", "c04"]
    assert retries[0]["level"] == "warning"
    ends = {e["case"]: e for e in events if e["event"] == "case.end"}
    assert ends["c02"]["flaky"] is True
    assert ends["c02"]["level"] == "warning"
    assert ends["c03"]["level"] == "error"
    assert ends["c01"]["level"] == "info"
    # The JUnit: the flaky case is green with its flag, the failed one red, the crashed and
    # the usage error are errors on a suite of their own.
    root = ET.fromstring((matrix_dir / MATRIX_JUNIT).read_text())  # noqa: S314
    suites = {s.get("name"): s for s in root.findall("testsuite")}
    flaky_suite = suites['ugv_nav_to_goal[metadata.description="flaky"]']
    assert flaky_suite.get("failures") == "0"
    props = {p.get("name"): p.get("value") for p in flaky_suite.iter("property")}
    assert props["flaky"] == "true"
    assert props["attempts"] == "2"
    assert props["attempt"] == "2"
    assert props["retried_run_ids"] == flaky["attempts"][0]["run_id"]
    assert suites['ugv_nav_to_goal[metadata.description="fail"]'].get("failures") == "2"
    crash_suite = suites['ugv_nav_to_goal[metadata.description="crash"]']
    assert crash_suite.get("tests") == "1"
    assert crash_suite.get("errors") == "1"
    error = crash_suite.find("testcase/error")
    assert error is not None
    assert error.get("type") == "error"
    assert root.get("errors") == "2"
    assert int(root.get("failures") or 0) >= 2
    # The Markdown report lists the cases and the KPIs of those that ran.
    markdown = (matrix_dir / MATRIX_MD).read_text()
    assert "| `c02` |" in markdown
    assert "PASS (flaky)" in markdown
    assert "| `c04` |" in markdown
    assert "ERROR" in markdown
    assert "`drive.length_m`" in markdown
    # fail_on_flaky turns the flag into a failure of a matrix that would otherwise pass.
    strict = write_matrix(
        tmp_path / "s.yaml", parameters={"metadata.description": ["flaky"]}, fail_on_flaky=True
    )
    record, run, _ = execute(strict, config, tmp_path, capacity)
    assert record["status"] == "failed"
    assert record["counts"]["flaky"] == 1
    assert record["counts"]["passed"] == 1
    assert "(fail_on_flaky)" in record["reason"]
    assert run.exit_code() == 1
    lenient = write_matrix(
        tmp_path / "l.yaml", parameters={"metadata.description": ["flaky"]}, retries=0
    )
    record, run, _ = execute(lenient, config, tmp_path, capacity)
    assert record["status"] == "failed", "no retry, so the flaky case fails"
    assert record["counts"]["attempts"] == 1


def test_parallel_runs_by_capacity(config: Config, tmp_path: Path) -> None:
    spec = write_matrix(
        tmp_path / "m.yaml", parameters={"metadata.description": ["slow"]}, seeds=[1, 2, 3, 4]
    )
    log_file = tmp_path / "fake.log"
    # Each case asks for 5 CPUs and 5 GiB; 12 CPUs and 32 GiB fit two at once after the reserve.
    record, _, _ = execute(
        spec,
        config,
        tmp_path,
        Capacity.of(HostInfo(12, 32 * GIB)),
        env={"FAKE_RUN_LOG": str(log_file), "FAKE_RUN_SLEEP_S": "0.4"},
    )
    assert record["status"] == "passed"
    assert record["schedule"]["parallel"] == "auto"
    assert record["schedule"]["max_concurrent"] == 2
    invocations = [json.loads(line) for line in log_file.read_text().splitlines()]
    overlaps = sum(
        1
        for a in invocations
        for b in invocations
        if a is not b and a["started"] < b["started"] < a["ended"]
    )
    assert overlaps >= 1, "two cases ran at the same time"
    domains = [c["attempts"][0]["domain_id"] for c in record["cases"]]
    assert len(set(domains)) >= 2, "cases running at once never share a ROS domain"
    assert len(set(domains[:2])) == 2
    serial = write_matrix(
        tmp_path / "s.yaml", parameters={"metadata.description": ["slow"]}, seeds=[1, 2], parallel=1
    )
    record, _, _ = execute(
        serial,
        config,
        tmp_path,
        Capacity.of(HostInfo(64, 128 * GIB)),
        env={"FAKE_RUN_SLEEP_S": "0.1"},
    )
    assert record["schedule"]["max_concurrent"] == 1


def test_interrupt(config: Config, tmp_path: Path, capacity: Capacity) -> None:
    spec = write_matrix(
        tmp_path / "m.yaml", parameters={"metadata.description": ["hang"]}, seeds=[1, 2, 3]
    )
    cases = resolve_cases(spec, config, spec.expand())
    matrix_id = make_matrix_id(
        spec, __import__("datetime").datetime.now(__import__("datetime").UTC), "int123"
    )
    matrix_dir = tmp_path / "runs" / matrix_id
    log = RunLog(matrix_id, file=matrix_dir / MATRIX_LOG, console=console(tmp_path))
    run = MatrixRun(
        spec,
        cases,
        matrix_id=matrix_id,
        matrix_dir=matrix_dir,
        capacity=capacity,
        log=log,
        command=COMMAND,
        parallel=2,
        poll_s=0.02,
        grace_s=5.0,
    )

    def interrupt_soon() -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and run.max_concurrent < 2:
            time.sleep(0.02)
        time.sleep(0.3)
        run.interrupt()

    threading.Thread(target=interrupt_soon, daemon=True).start()
    record = run.execute()
    assert record["status"] == "interrupted"
    assert record["verdict"] == "INTERRUPTED"
    assert record["exit_code"] == 130
    counts = record["counts"]
    assert counts["interrupted"] == 2, "the two running cases got SIGINT and said so"
    assert counts["skipped"] == 1, "the third never started"
    assert counts["attempts"] == 2
    statuses = [c["status"] for c in record["cases"]]
    assert statuses == ["interrupted", "interrupted", "skipped"]
    assert record["cases"][0]["attempts"][0]["exit_code"] == 130
    assert record["cases"][0]["attempts"][0]["reason"] == "interrupted (Ctrl-C or SIGTERM)"
    assert record["cases"][2]["attempts"] == []
    root = ET.fromstring((matrix_dir / MATRIX_JUNIT).read_text())  # noqa: S314
    suites = root.findall("testsuite")
    assert suites[2].get("skipped") == "1"
    assert suites[2].find("testcase/skipped") is not None
    events = [json.loads(line) for line in (matrix_dir / MATRIX_LOG).read_text().splitlines()]
    assert "matrix.interrupt" in [e["event"] for e in events]
    assert "case.skipped" in [e["event"] for e in events]


def test_launch_failure_is_an_error(config: Config, tmp_path: Path, capacity: Capacity) -> None:
    spec = write_matrix(tmp_path / "m.yaml", retries=0)
    cases = resolve_cases(spec, config, spec.expand())
    matrix_id = make_matrix_id(
        spec, __import__("datetime").datetime.now(__import__("datetime").UTC), "bad123"
    )
    run = MatrixRun(
        spec,
        cases,
        matrix_id=matrix_id,
        matrix_dir=tmp_path / matrix_id,
        capacity=capacity,
        log=RunLog(matrix_id, console=console(tmp_path)),
        command=(str(tmp_path / "no-such-command"),),
        poll_s=0.02,
    )
    record = run.execute()
    assert record["status"] == "failed"
    assert record["cases"][0]["status"] == "error"
    assert "cannot start" in record["cases"][0]["attempts"][0]["reason"]
    assert record["cases"][0]["attempts"][0]["exit_code"] is None


def test_outputs_directory_is_cleared(config: Config, tmp_path: Path, capacity: Capacity) -> None:
    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        v0_text(EXAMPLE.read_text())
        .replace(
            "../vehicles/ugv_nav2/robot.yaml", str(REPO / "examples/vehicles/ugv_nav2/robot.yaml")
        )
        .replace("outputs:\n", "outputs:\n  directory: elsewhere\n")
    )
    spec = write_matrix(tmp_path / "m.yaml", scenario=str(scenario), retries=0)
    log_file = tmp_path / "fake.log"
    record, _, matrix_dir = execute(
        spec, config, tmp_path, capacity, env={"FAKE_RUN_LOG": str(log_file)}
    )
    assert record["status"] == "passed"
    argv = json.loads(log_file.read_text().splitlines()[0])["argv"]
    assert "outputs.directory=null" in argv, "the case runs under the matrix directory"
    assert (matrix_dir / record["cases"][0]["run_id"] / "run.json").is_file()


def test_render_matrix_junit_without_reports() -> None:
    record = {
        "matrix_id": "m1",
        "matrix": {"name": "x"},
        "scenario": {"name": "s"},
        "cases": [
            {"id": "c01", "name": "a", "status": "skipped", "attempts": []},
            {
                "id": "c02",
                "name": "b",
                "status": "error",
                "attempts": [{"number": 1, "run_id": "r", "reason": "boom", "duration_s": 2.5}],
            },
        ],
    }
    root = ET.fromstring(render_matrix_junit(record, {}))  # noqa: S314
    assert root.get("name") == "fieldproof matrix x"
    assert root.get("tests") == "2"
    assert root.get("skipped") == "1"
    assert root.get("errors") == "1"
    suites = root.findall("testsuite")
    assert suites[0].get("name") == "s[a]"
    assert suites[0].get("time") == "0.000"
    assert suites[1].get("time") == "2.500"
    error = suites[1].find("testcase/error")
    assert error is not None
    assert error.get("message") == "boom"


def test_command_from_env() -> None:
    assert command_from_env({}) == (sys.executable, "-m", "fieldproof")
    assert command_from_env({"FIELDPROOF_COMMAND": "uv run --project engine fieldproof"}) == (
        "uv",
        "run",
        "--project",
        "engine",
        "fieldproof",
    )
    assert command_from_env({"FIELDPROOF_COMMAND": "  "}) == (sys.executable, "-m", "fieldproof")


def test_select_cases(tmp_path: Path) -> None:
    spec = write_matrix(
        tmp_path / "m.yaml", seeds=[1, 2], cases=[{"name": "alpha"}, {"name": "beta"}]
    )
    cases = spec.expand()
    assert [c.id for c in select_cases(cases, None)] == ["c01", "c02", "c03", "c04"]
    assert [c.name for c in select_cases(cases, ["alpha*"])] == ["alpha, seed=1", "alpha, seed=2"]
    assert [c.id for c in select_cases(cases, ["c02", "*seed=2"])] == ["c02", "c04"]
    assert select_cases(cases, ["gamma"]) == []


# ---------------------------------------------------------------------------------- the command
def test_matrix_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: PLR0915
    matrix_file = tmp_path / "m.yaml"
    write_matrix(
        matrix_file, parameters={"metadata.description": ["pass", "flaky"]}, seeds=[3], retries=1
    )
    monkeypatch.setenv("FIELDPROOF_COMMAND", f"{sys.executable} {STUB}")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    summary = tmp_path / "summary.md"
    # The dry run plans the cases without Docker and prints them with the schedule.
    result = runner.invoke(
        app, ["matrix", str(matrix_file), "--dry-run", "--runs-dir", str(tmp_path / "runs")]
    )
    assert result.exit_code == 0, result.output
    planned = json.loads(result.output)
    assert planned["status"] is None
    assert [c["name"] for c in planned["cases"]] == [
        'metadata.description="pass", seed=3',
        'metadata.description="flaky", seed=3',
    ]
    assert planned["cases"][0]["demand"]["cpus"] == 5.0
    assert planned["schedule"]["parallel"] == "auto"
    assert planned["schedule"]["capacity"]["host"]["cpus"] >= 1
    assert not (tmp_path / "runs").exists(), "a dry run writes nothing"
    # Without a daemon the real run cannot measure the host.
    monkeypatch.setattr(
        "fieldproof.docker_backend.DockerBackend.__init__",
        lambda self: (_ for _ in ()).throw(
            __import__("fieldproof.backend", fromlist=["BackendError"]).BackendError("no daemon")
        ),
    )
    result = runner.invoke(app, ["matrix", str(matrix_file), "--runs-dir", str(tmp_path / "runs")])
    assert result.exit_code == 2, result.output
    assert "no daemon" in result.output
    # With the host measured, the cases run through the stub.
    monkeypatch.setattr("fieldproof.docker_backend.DockerBackend.__init__", lambda self: None)
    monkeypatch.setattr(
        "fieldproof.docker_backend.DockerBackend.host", lambda self: HostInfo(16, 32 * GIB)
    )
    result = runner.invoke(
        app,
        [
            "matrix",
            str(matrix_file),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--parallel",
            "2",
            "--summary",
            str(summary),
        ],
    )
    assert result.exit_code == 0, result.output
    text = plain(result.output)
    assert "PASS sweep (" in text
    assert "2 of 2 case(s) passed, 1 flaky; 3 attempt(s)" in text
    assert "c02" in text
    assert "(flaky)" in text
    assert "::warning title=Flaky case c02 of sweep::" in result.output
    assert "junit:" in text
    assert summary.read_text().startswith("# Fieldproof matrix report: sweep")
    (matrix_dir,) = [p for p in (tmp_path / "runs").iterdir() if p.is_dir()]
    record = json.loads((matrix_dir / MATRIX_RECORD).read_text())
    assert record["schedule"]["parallel"] == 2
    assert record["schedule"]["capacity"]["host"] == {"cpus": 16, "memory_bytes": 32 * GIB}
    assert (tmp_path / "runs" / "fieldproof.sqlite").is_file()
    # Options: --only, --retries, --fail-on-flaky, the usage errors.
    result = runner.invoke(
        app,
        [
            "matrix",
            str(matrix_file),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--only",
            "*flaky*",
            "--retries",
            "0",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "FAIL sweep" in plain(result.output)
    assert "::error title=Case c02 of sweep FAIL::" in result.output
    result = runner.invoke(
        app,
        [
            "matrix",
            str(matrix_file),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--only",
            "*flaky*",
            "--fail-on-flaky",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "(fail_on_flaky)" in plain(result.output)
    result = runner.invoke(app, ["matrix", str(matrix_file), "--only", "nothing"])
    assert result.exit_code == 2
    assert "no case matches" in result.output
    result = runner.invoke(app, ["matrix", str(matrix_file), "--parallel", "many"])
    assert result.exit_code == 2
    assert "--parallel" in result.output
    result = runner.invoke(app, ["matrix", str(matrix_file), "--render", "tv"])
    assert result.exit_code == 2
    result = runner.invoke(app, ["matrix", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 2
    assert "No such file" in result.output
    bad = tmp_path / "bad.yaml"
    write_matrix(bad, cases=[{"name": "bad", "set": {"vehicles[ugv9].env": {}}}])
    result = runner.invoke(app, ["matrix", str(bad), "--dry-run"])
    assert result.exit_code == 2
    assert "case c01 (bad)" in result.output


def test_help_lists_matrix_and_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "matrix" in result.output
    assert "runs" in result.output
    result = runner.invoke(app, ["runs", "--help"])
    assert result.exit_code == 0
    for command in ("list", "show", "diff", "index"):
        assert command in result.output
