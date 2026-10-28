"""Integration test of ``fieldproof matrix`` (task P2-T06) against the real stack.

A two-case matrix of the failing example scenario (``ugv_nav_fails_on_time.yaml``, one second
for the drive, so each run is short and fails by design on ``arrived_in_time``) over two
seeds, with no retry: both cases run as their own ``fieldproof run`` processes under the
matrix directory, each with its own ROS domain, each ``failed`` naming ``arrived_in_time``,
the matrix exits 1 with ``FAIL``, the single JUnit document has one test suite per case with
the case's properties, the Markdown and HTML reports and the record are written, every run
and the matrix are in the run database (``fieldproof runs list``, ``runs diff`` of the two),
and no container of either run is left behind. The example matrix of twelve passing runs is
the nightly job's (ci.md); this test keeps the real chain under ``make test-integration``.

Needs the stack images (``make images``), the vehicle image (``make vehicle-image``) and the
Fuel models (``make assets``), like the other integration tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml
from test_run import containers_of

REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "examples/scenarios/ugv_nav_fails_on_time.yaml"
FIELDPROOF = [sys.executable, "-m", "fieldproof"]
MATRIX_TIMEOUT_S = 2400.0
ENV = {**os.environ, "ROS_DOMAIN_ID": os.environ.get("ROS_DOMAIN_ID", "0")}


def fieldproof(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run the engine's CLI."""
    return subprocess.run(  # noqa: S603  (fixed argument list)
        [*FIELDPROOF, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=ENV,
        cwd=REPO,
    )


def test_matrix_of_two_failing_cases(tmp_path: Path) -> None:  # noqa: PLR0915
    matrix_file = tmp_path / "fails_on_time.yaml"
    matrix_file.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "fieldproof.dev/v0alpha1",
                "kind": "Matrix",
                "metadata": {"name": "fails_on_time_matrix"},
                "scenario": str(SCENARIO),
                "seeds": [1, 2],
                "retries": 0,
                "parallel": 1,
            },
            sort_keys=False,
        )
    )
    runs = tmp_path / "runs"
    completed = fieldproof(
        "matrix",
        str(matrix_file),
        "--runs-dir",
        str(runs),
        "--render",
        "software",
        timeout=MATRIX_TIMEOUT_S,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "FAIL fails_on_time_matrix" in output, output
    assert "0 of 2 case(s) passed, 2 failed; 2 attempt(s)" in output, output
    (matrix_dir,) = [p for p in runs.iterdir() if p.is_dir()]
    record = json.loads((matrix_dir / "matrix.json").read_text())
    assert record["status"] == "failed"
    assert record["exit_code"] == 1
    assert record["counts"]["cases"] == 2
    assert record["counts"]["failed"] == 2
    assert record["schedule"]["parallel"] == 1
    domains = set()
    for case in record["cases"]:
        (attempt,) = case["attempts"]
        assert attempt["status"] == "failed", attempt
        assert "arrived_in_time" in str(attempt["reason"]), attempt
        assert attempt["exit_code"] == 1
        domains.add(attempt["domain_id"])
        run_dir = matrix_dir / attempt["run_id"]
        run_record = json.loads((run_dir / "run.json").read_text())
        assert run_record["status"] == "failed"
        assert run_record["seed"] == case["seed"]
        assert run_record["overrides"] == {"seed": case["seed"]}
        assert run_record["stack"]["ros_domain_id"] == attempt["domain_id"]
        assert yaml.safe_load((run_dir / "scenario.yaml").read_text())["seed"] == case["seed"]
        assert (run_dir / "report.html").is_file()
        assert (run_dir / "console.log").is_file()
        assert containers_of(attempt["run_id"]) == [], "nothing of the run is left"
    assert len(domains) == 2, "every case has a domain of its own"
    root = ET.fromstring((matrix_dir / "junit.xml").read_text())  # noqa: S314 (our own document)
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == [
        "ugv_nav_fails_on_time[seed=1]",
        "ugv_nav_fails_on_time[seed=2]",
    ]
    for suite in suites:
        assert int(suite.get("failures") or 0) >= 2, "the assertion and the run"
        properties = {p.get("name"): p.get("value") for p in suite.iter("property")}
        assert properties["flaky"] == "false"
        assert properties["attempts"] == "1"
        assert properties["matrix_id"] == record["matrix_id"]
    assert (
        (matrix_dir / "report.md")
        .read_text()
        .startswith("# Fieldproof matrix report: fails_on_time_matrix")
    )
    assert "Fieldproof matrix report" in (matrix_dir / "report.html").read_text()
    # The database: both runs with their case columns, the matrix, and a diff of the two.
    listed = fieldproof("runs", "list", "--runs-dir", str(runs), "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    rows = json.loads(listed.stdout)["runs"]
    assert {r["case_id"] for r in rows} == {"c01", "c02"}
    assert all(r["matrix_id"] == record["matrix_id"] and r["status"] == "failed" for r in rows)
    first, second = (c["attempts"][0]["run_id"] for c in record["cases"])
    diffed = fieldproof("runs", "diff", first, second, "--runs-dir", str(runs), "--json")
    assert diffed.returncode == 0, diffed.stdout + diffed.stderr
    diff = json.loads(diffed.stdout)
    assert diff["seeds"] == [1, 2]
    assert diff["overrides"]["changed"] == {"seed": [1, 2]}
    assert diff["counts"]["regressed"] == 0
    assert any(k["name"].startswith("mission") for k in diff["kpis"]), diff["kpis"]
