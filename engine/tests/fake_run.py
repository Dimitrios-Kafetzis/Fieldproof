"""A stand-in for ``fieldproof run`` the matrix tests launch (task P2-T06).

It takes the command line the matrix builds (``run SCENARIO --runs-dir D --run-id ID
--domain-id N --quiet [--config C] [--render R] --set path=value ...``), writes the run
directory a real run leaves (``run.json`` and ``report.json`` through the engine's own
report builder) and exits as a run would. What it does is told by the override of
``metadata.description`` (the matrix's parameter): ``pass``, ``fail`` (an assertion fails,
exit 1), ``flaky`` (fails on the first attempt, passes on the second), ``crash`` (exit 3 with
no record), ``usage`` (exit 2 with no record), ``slow`` (sleeps ``FAKE_RUN_SLEEP_S`` then
passes), ``hang`` (sleeps until SIGINT, then records ``interrupted`` and exits 130). Every
invocation appends its argument list and its start and end times to ``FAKE_RUN_LOG`` when set.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from fieldproof.report import write_reports

EXIT = {"passed": 0, "failed": 1, "timeout": 1, "error": 1, "interrupted": 130}


def main(argv: list[str]) -> int:  # noqa: PLR0912, PLR0915
    """Run the stub."""
    started = time.time()
    overrides: dict[str, Any] = {}
    options: dict[str, str] = {}
    positional: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--set":
            path, _, raw = argv[index + 1].partition("=")
            overrides[path] = json.loads(raw)
            index += 2
        elif (
            arg.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--")
        ):
            options[arg] = argv[index + 1]
            index += 2
        elif arg.startswith("--"):
            options[arg] = "true"
            index += 1
        else:
            positional.append(arg)
            index += 1
    run_id = options["--run-id"]
    run_dir = Path(options["--runs-dir"]) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    behaviour = str(overrides.get("metadata.description", "pass"))
    attempt = int(run_id.rsplit("a", 1)[-1]) if run_id.rsplit("a", 1)[-1].isdigit() else 1
    seed = int(overrides.get("seed", 0) or 0)
    status = "passed"
    reason = None
    if behaviour == "fail" or (behaviour == "flaky" and attempt == 1):
        status, reason = "failed", "assertion reached_goal failed: 1.20 m from the goal"
    elif behaviour == "crash":
        return 3
    elif behaviour == "usage":
        sys.stderr.write("error: --set time.rtf: not a number\n")
        return 2
    elif behaviour == "slow":
        time.sleep(float(os.environ.get("FAKE_RUN_SLEEP_S", "0.5")))
    elif behaviour == "hang":
        interrupted = {"yes": False}

        def on_sigint(_signum: int, _frame: Any) -> None:
            interrupted["yes"] = True

        signal.signal(signal.SIGINT, on_sigint)
        for _ in range(600):
            if interrupted["yes"]:
                break
            time.sleep(0.05)
        status, reason = "interrupted", "interrupted (Ctrl-C or SIGTERM)"
    cpus = float(overrides.get("vehicles[ugv1].resources.cpus", 2.0) or 2.0)
    record = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "scenario": {"name": "ugv_nav_to_goal", "path": positional[-1], "digest": run_id[:8]},
        "engine_version": "test",
        "started_at": "2026-10-28T10:00:00.000+00:00",
        "finished_at": "2026-10-28T10:01:00.000+00:00",
        "phase": "finished",
        "status": status,
        "reason": reason,
        "phases": [{"name": "running", "duration_s": 30.0}],
        "vehicles": [{"name": "ugv1", "manifest": "ugv_nav2", "image": "img:local"}],
        "timeline": [
            {
                "name": "drive_ahead",
                "action": "navigate_to_pose",
                "vehicle": "ugv1",
                "at_s": 0.0,
                "status": "ok",
                "duration_s": 20.0,
                "detail": "goal SUCCEEDED",
            }
        ],
        "assertions": [
            {
                "name": "reached_goal",
                "type": "reach_pose",
                "status": "passed" if status == "passed" else "failed",
                "reason": reason or "0.20 m from the goal",
                "t_s": 20.0,
                "evidence": {"samples": 10},
            },
            {
                "name": "lidar_streams",
                "type": "topic_rate",
                "status": "passed",
                "reason": "10.0 Hz",
                "t_s": 6.0,
                "evidence": {},
            },
        ],
        "kpis": {
            "drive.length_m": {
                "value": 4.0 + seed / 10.0,
                "unit": "m",
                "source": "drive",
                "type": "path",
            },
            "stack.cpu_pct": {
                "value": 100.0 * cpus,
                "unit": "%",
                "source": "stack",
                "type": "resources",
            },
        },
        "kpi_collectors": [],
        "time": {"mode": "sim"},
        "seed": seed,
        "overrides": overrides,
        "stack": {"world": "depot", "ros_domain_id": int(options.get("--domain-id", "0"))},
        "observation": {"sim_elapsed_s": 25.0},
    }
    (run_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n")
    write_reports(record, run_dir)
    (run_dir / "engine.log").write_text(json.dumps({"event": "run.end", "status": status}) + "\n")
    log = os.environ.get("FAKE_RUN_LOG")
    if log:
        with Path(log).open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "argv": argv,
                        "run_id": run_id,
                        "started": started,
                        "ended": time.time(),
                        "status": status,
                        "pid": os.getpid(),
                    }
                )
                + "\n"
            )
    return EXIT[status]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
