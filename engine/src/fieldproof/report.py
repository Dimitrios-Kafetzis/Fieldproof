"""Reports of a run (task P1-T06): ``report.json`` and ``report.md`` next to ``run.json``.

The record the runner writes is the engine's view (phases, containers, the stream). The report
is the reader's: the verdict, every assertion with its reason and evidence, the timeline, the
durations and the artefacts of the run directory (the MCAP recording first). :func:`write_reports`
renders both at the end of a run; ``fieldproof report`` renders them again from a run directory.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

RECORD_FILE = "run.json"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"
RECORDING_DIR = "recording"
VERDICTS = {
    "passed": "PASS",
    "failed": "FAIL",
    "timeout": "TIMEOUT",
    "error": "ERROR",
    "interrupted": "INTERRUPTED",
}
EXIT_CODES = {"passed": 0, "failed": 1, "timeout": 1, "error": 1, "interrupted": 130}
ASSERTION_MARKS = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "pending": "PENDING"}


class ReportError(ValueError):
    """The run cannot be reported (no record)."""


def load_record(run: Path) -> dict[str, Any]:
    """The record of a run directory (or of a `run.json` path)."""
    path = run / RECORD_FILE if run.is_dir() else run
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise ReportError(f"{path}: {error.strerror}") from error
    except json.JSONDecodeError as error:
        raise ReportError(f"{path}: not valid JSON: {error}") from error
    if not isinstance(data, dict) or "run_id" not in data:
        raise ReportError(f"{path}: not a run record")
    return data


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3
        )
    except ValueError:
        return None


def _artifacts(run_dir: Path) -> list[dict[str, Any]]:
    if not run_dir.is_dir():
        return []
    files = sorted(p for p in run_dir.rglob("*") if p.is_file())
    return [
        {"path": str(p.relative_to(run_dir)), "bytes": p.stat().st_size}
        for p in files
        if p.name not in (REPORT_JSON, REPORT_MD)
    ]


def _recording(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    files = [a for a in artifacts if a["path"].startswith(f"{RECORDING_DIR}/")]
    if not files:
        return None
    mcaps = [a for a in files if a["path"].endswith(".mcap")]
    return {
        "directory": RECORDING_DIR,
        "mcap": [a["path"] for a in mcaps],
        "bytes": sum(a["bytes"] for a in files),
    }


def build_report(record: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """The report as data."""
    status = str(record.get("status") or "error")
    assertions = list(record.get("assertions") or [])
    counts = dict.fromkeys(ASSERTION_MARKS, 0)
    for assertion in assertions:
        counts[str(assertion.get("status", "pending"))] = (
            counts.get(str(assertion.get("status", "pending")), 0) + 1
        )
    artifacts = _artifacts(run_dir)
    running = next((p for p in record.get("phases", []) if p.get("name") == "running"), None)
    return {
        "run_id": record["run_id"],
        "verdict": VERDICTS.get(status, status.upper()),
        "status": status,
        "exit_code": EXIT_CODES.get(status, 1),
        "reason": record.get("reason"),
        "scenario": record.get("scenario", {}),
        "engine_version": record.get("engine_version"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_s": _seconds_between(record.get("started_at"), record.get("finished_at")),
        "timeline_duration_s": running.get("duration_s") if running else None,
        "sim_elapsed_s": (record.get("observation") or {}).get("sim_elapsed_s"),
        "world": (record.get("stack") or {}).get("world"),
        "vehicles": list(record.get("vehicles") or []),
        "assertions": assertions,
        "assertion_counts": counts,
        "timeline": list(record.get("timeline") or []),
        "phases": list(record.get("phases") or []),
        "recording": _recording(artifacts),
        "artifacts": artifacts,
        "run_dir": str(run_dir),
    }


def _fmt(value: Any, unit: str = "", digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}{unit}"
    return f"{value}{unit}"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _evidence(evidence: dict[str, Any]) -> str:
    parts = []
    for key, value in evidence.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{k} {_fmt(v, digits=2)}" for k, v in value.items() if v is not None)
            parts.append(f"{key}: {inner}")
        elif value is not None:
            parts.append(f"{key} {_fmt(value, digits=2)}")
    return "; ".join(parts) or "-"


def _size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def render_markdown(report: dict[str, Any]) -> str:
    """The report as Markdown."""
    scenario = report.get("scenario") or {}
    lines = [
        f"# Fieldproof run report: {scenario.get('name', report['run_id'])}",
        "",
        f"**{report['verdict']}** (status `{report['status']}`, exit code {report['exit_code']})"
        + (f": {report['reason']}" if report.get("reason") else ""),
        "",
        f"- Run `{report['run_id']}`, engine {report.get('engine_version') or '-'}",
        f"- Scenario `{scenario.get('path', '-')}` (sha `{scenario.get('digest', '-')}`), "
        f"world `{report.get('world') or '-'}`",
    ]
    vehicles = report.get("vehicles") or []
    if vehicles:
        described = ", ".join(
            f"`{v.get('name')}` ({v.get('manifest', '-')}, `{v.get('image', '-')}`)"
            for v in vehicles
        )
        lines.append(f"- Vehicles: {described}")
    started, finished = report.get("started_at") or "-", report.get("finished_at") or "-"
    lines.append(
        f"- Started {started}, finished {finished}; "
        f"{_fmt(report.get('duration_s'), ' s')} wall clock, timeline "
        f"{_fmt(report.get('timeline_duration_s'), ' s')}"
        + (
            f", {_fmt(report.get('sim_elapsed_s'), ' s')} of simulation"
            if report.get("sim_elapsed_s") is not None
            else ""
        )
    )
    counts = report.get("assertion_counts") or {}
    lines += ["", "## Assertions", ""]
    assertions = report.get("assertions") or []
    if assertions:
        summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
        lines += [
            f"{len(assertions)} assertion(s): {summary}.",
            "",
            "| Assertion | Type | Verdict | At | Reason | Evidence |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for a in assertions:
            lines.append(
                f"| `{a.get('name')}` | `{a.get('type')}` | "
                f"{ASSERTION_MARKS.get(str(a.get('status')), str(a.get('status')))} | "
                f"{_fmt(a.get('t_s'), ' s')} | {_cell(a.get('reason') or '-')} | "
                f"{_cell(_evidence(a.get('evidence') or {}))} |"
            )
    else:
        lines.append("The scenario declares no assertions.")
    lines += ["", "## Timeline", ""]
    timeline = report.get("timeline") or []
    if timeline:
        lines += [
            "| Action | Vehicle | At | Status | Duration | Detail |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for entry in timeline:
            lines.append(
                f"| `{entry.get('name')}` ({entry.get('action')}) | `{entry.get('vehicle')}` | "
                f"{_fmt(float(entry.get('at_s', 0.0)), ' s', 0)} | {entry.get('status')} | "
                f"{_fmt(entry.get('duration_s'), ' s')} | {_cell(entry.get('detail') or '-')} |"
            )
    else:
        lines.append("The scenario has no timeline.")
    lines += ["", "## Phases", "", "| Phase | Duration |", "| --- | --- |"]
    for phase in report.get("phases") or []:
        lines.append(f"| {phase.get('name')} | {_fmt(phase.get('duration_s'), ' s')} |")
    lines += ["", "## Artefacts", ""]
    recording = report.get("recording")
    if recording:
        files = ", ".join(f"`{p}`" for p in recording["mcap"]) or f"`{recording['directory']}/`"
        lines.append(f"- Recording (MCAP, {_size(recording['bytes'])}): {files}")
    else:
        lines.append("- No recording (`outputs.record` was off).")
    for artifact in report.get("artifacts") or []:
        if not artifact["path"].startswith(f"{RECORDING_DIR}/"):
            lines.append(f"- `{artifact['path']}` ({_size(artifact['bytes'])})")
    return "\n".join(lines) + "\n"


def write_reports(record: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Write `report.json` and `report.md` into the run directory; return the report."""
    report = build_report(record, run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / REPORT_JSON).write_text(json.dumps(report, indent=2) + "\n")
    (run_dir / REPORT_MD).write_text(render_markdown(report))
    return report
