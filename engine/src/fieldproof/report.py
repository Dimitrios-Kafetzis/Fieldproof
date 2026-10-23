"""Reports of a run (task P1-T06): ``report.json``, ``report.md`` and ``junit.xml``.

The record the runner writes next to them (``run.json``) is the engine's view (phases,
containers, the stream). The report is the reader's: the verdict, every assertion with its
reason and evidence, the timeline, the durations and the artefacts of the run directory (the
MCAP recording first). :func:`write_reports` renders the three at the end of a run;
``fieldproof report`` renders them again from a run
directory. The JUnit document (task P1-T07) is the report for CI: one test suite per run, one
test case per assertion and per timeline entry, and one for the run itself, so a test reporter
shows a failed scenario by the assertion that failed it.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

RECORD_FILE = "run.json"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"
REPORT_JUNIT = "junit.xml"
REPORT_FILES = (REPORT_JSON, REPORT_MD, REPORT_JUNIT)
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
        if p.name not in REPORT_FILES
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
        "clock": record.get("clock"),
        "time": record.get("time"),
        "seed": record.get("seed"),
        "world": (record.get("stack") or {}).get("world"),
        "vehicles": list(record.get("vehicles") or []),
        "assertions": assertions,
        "assertion_counts": counts,
        "timeline": list(record.get("timeline") or []),
        "phases": list(record.get("phases") or []),
        "recording": _recording(artifacts),
        "video": record.get("video"),
        "network": record.get("network"),
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
    lines += _clock_lines(report)
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
    lines += _network_lines(report.get("network"))
    lines += ["", "## Phases", "", "| Phase | Duration |", "| --- | --- |"]
    for phase in report.get("phases") or []:
        lines.append(f"| {phase.get('name')} | {_fmt(phase.get('duration_s'), ' s')} |")
    lines += ["", "## Artefacts", "", *_artefact_lines(report)]
    return "\n".join(lines) + "\n"


def _clock_lines(report: dict[str, Any]) -> list[str]:
    """The clock of the run (task P2-T04): mode, pace, drift, verdict, seed."""
    clock = report.get("clock") or {}
    time_section = report.get("time") or {}
    mode = clock.get("mode") or time_section.get("mode") or "sim"
    parts = [f"- Clock: mode `{mode}`"]
    target = clock.get("target_rtf")
    if target is not None:
        parts.append(f"target RTF {_fmt(target, '', 2)}")
    rtf = clock.get("rtf") or {}
    if rtf.get("windows"):
        parts.append(
            f"measured {_fmt(rtf.get('mean'), '', 2)} (min {_fmt(rtf.get('min'), '', 2)}, "
            f"max {_fmt(rtf.get('max'), '', 2)}) over {rtf['windows']} window(s)"
        )
    if clock.get("drift_s") is not None:
        largest = _fmt(clock.get("max_abs_drift_s"), " s", 3)
        parts.append(f"drift {clock['drift_s']:+.3f} s (largest {largest})")
    if clock.get("violations"):
        parts.append(f"{clock['violations']} window(s) outside the tolerance")
    if clock.get("paused_s"):
        parts.append(f"paused {_fmt(clock['paused_s'], ' s')}")
    verdict = clock.get("verdict")
    if verdict is not None:
        parts.append(
            f"**{verdict}** (drift budget {_fmt(clock.get('max_drift_s'), ' s')}, "
            f"on drift: {clock.get('on_drift')})"
        )
    lockstep = clock.get("lockstep") or {}
    if lockstep:
        parts.append(
            f"lockstep {_fmt(lockstep.get('step_s'), ' s', 3)} steps: {lockstep.get('steps')} "
            f"stepped, {lockstep.get('holds')} hold(s), longest wait "
            f"{_fmt(lockstep.get('max_wait_s'), ' s', 3)}"
        )
    if report.get("seed") is not None:
        parts.append(f"seed {report['seed']}")
    return [", ".join(parts)]


def _network_lines(network: dict[str, Any] | None) -> list[str]:
    """The radio links: each link's periods, the profile against what was measured."""
    if not network:
        return []
    lines = [
        "",
        "## Radio links",
        "",
        f"Radio network `{network.get('radio')}`, ground station `{network.get('ground')}`, "
        f"verification `{network.get('verify')}`. Latency is one way (half the round trip of "
        "the ground station's probes); times are from the start of the timeline.",
        "",
        "| Link | Profile | From | To | Latency (profile) | Latency (measured) | Jitter "
        "(measured) | Loss (measured) | Probes | Verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for link in network.get("periods") or []:
        for period in link.get("periods") or []:
            expected = period.get("expected") or {}
            measured = period.get("measured") or {}
            lines.append(
                f"| `{link.get('vehicle')}` | `{period.get('profile')}` ({period.get('cause')}) | "
                f"{_fmt(period.get('from_s'), ' s')} | {_fmt(period.get('to_s'), ' s')} | "
                f"{_fmt(expected.get('latency_ms'), ' ms', 2)} | "
                f"{_fmt(measured.get('latency_ms'), ' ms', 2)} | "
                f"{_fmt(measured.get('jitter_ms'), ' ms', 2)} | "
                f"{_fmt(measured.get('loss_pct'), ' %', 1)} | "
                f"{measured.get('replies', 0)}/{measured.get('probes', 0)} | "
                f"{ASSERTION_MARKS.get(str(period.get('status')), str(period.get('status')))} |"
            )
    counted = network.get("traffic") or {}
    if counted:
        lines += [
            "",
            "Traffic over the links (the probes excluded; drops are the profile's loss and "
            "the queue overflowing behind the rate):",
            "",
            "| Link | Out | Dropped out | In | Dropped in |",
            "| --- | --- | --- | --- | --- |",
        ]
        for vehicle, directions in counted.items():
            out, inbound = directions.get("out") or {}, directions.get("in") or {}
            lines.append(
                f"| `{vehicle}` | {_size(int(out.get('bytes', 0)))} | {out.get('drops', 0)} | "
                f"{_size(int(inbound.get('bytes', 0)))} | {inbound.get('drops', 0)} |"
            )
    return lines


def _artefact_lines(report: dict[str, Any]) -> list[str]:
    """The artefacts list: the recording, the video, then every other file."""
    lines = []
    recording = report.get("recording")
    if recording:
        files = ", ".join(f"`{p}`" for p in recording["mcap"]) or f"`{recording['directory']}/`"
        lines.append(f"- Recording (MCAP, {_size(recording['bytes'])}): {files}")
    else:
        lines.append("- No recording (`outputs.record` was off).")
    video = report.get("video") or {}
    if video.get("status") == "saved":
        lines.append(
            f"- Video from camera `{video['camera']}` ({_size(int(video['bytes'] or 0))}): "
            f"`{video['file']}`"
        )
    elif video:
        lines.append(
            f"- No video from camera `{video['camera']}`: {video.get('reason') or video['status']}"
        )
    saved_video = video.get("file") if video.get("status") == "saved" else None
    for artifact in report.get("artifacts") or []:
        path = artifact["path"]
        if not path.startswith(f"{RECORDING_DIR}/") and path != saved_video:
            lines.append(f"- `{path}` ({_size(artifact['bytes'])})")
    return lines


def _junit_time(value: Any) -> str:
    try:
        return f"{float(value):.3f}" if value is not None else "0.000"
    except (TypeError, ValueError):
        return "0.000"


def _junit_text(value: Any) -> str:
    """Text safe for an XML document (control characters other than whitespace dropped)."""
    text = str(value)
    return "".join(c for c in text if c in "\t\n\r" or ord(c) >= 0x20)


def _junit_case(
    suite: ET.Element, classname: str, name: str, time: Any, **attributes: str
) -> ET.Element:
    attrib = {"classname": classname, "name": name, "time": _junit_time(time), **attributes}
    return ET.SubElement(suite, "testcase", attrib)


def render_junit(report: dict[str, Any]) -> str:
    """The report as a JUnit XML document.

    One ``testsuite`` named after the scenario, with the run's facts as properties and three
    kinds of ``testcase``: ``<scenario>.assertions`` (one per assertion: a ``failure`` when it
    failed, ``skipped`` when it was skipped or still pending), ``<scenario>.timeline`` (one per
    entry: a ``failure`` unless its status is ``ok``) and ``<scenario>`` / ``run`` for the run
    itself (a ``failure`` when the run failed, an ``error`` when it timed out, errored or was
    interrupted). The counts on the suite are what a test reporter sums.
    """
    scenario = report.get("scenario") or {}
    suite_name = str(scenario.get("name") or report["run_id"])
    suites = ET.Element("testsuites", name="fieldproof")
    suite = ET.SubElement(suites, "testsuite", name=suite_name)
    if report.get("started_at"):
        suite.set("timestamp", str(report["started_at"]))
    properties = ET.SubElement(suite, "properties")
    recording = report.get("recording") or {}
    facts = {
        "run_id": report["run_id"],
        "verdict": report["verdict"],
        "status": report["status"],
        "exit_code": report["exit_code"],
        "reason": report.get("reason"),
        "scenario": scenario.get("path"),
        "scenario_digest": scenario.get("digest"),
        "world": report.get("world"),
        "engine_version": report.get("engine_version"),
        "duration_s": report.get("duration_s"),
        "timeline_duration_s": report.get("timeline_duration_s"),
        "sim_elapsed_s": report.get("sim_elapsed_s"),
        "recording": ", ".join(recording.get("mcap") or []) or None,
        "video": (report.get("video") or {}).get("file")
        if (report.get("video") or {}).get("status") == "saved"
        else None,
        "run_dir": report.get("run_dir"),
    }
    for key, value in facts.items():
        if value is not None:
            ET.SubElement(properties, "property", name=key, value=_junit_text(value))
    failures = errors = skipped = 0
    for assertion in report.get("assertions") or []:
        status = str(assertion.get("status") or "pending")
        case = _junit_case(
            suite,
            f"{suite_name}.assertions",
            str(assertion.get("name")),
            assertion.get("t_s"),
            type=str(assertion.get("type") or ""),
        )
        reason = _junit_text(assertion.get("reason") or status)
        if status == "failed":
            failures += 1
            failure = ET.SubElement(
                case, "failure", message=reason, type=str(assertion.get("type") or "assertion")
            )
            failure.text = json.dumps(assertion.get("evidence") or {}, indent=2)
        elif status != "passed":
            skipped += 1
            ET.SubElement(case, "skipped", message=reason)
    for entry in report.get("timeline") or []:
        case = _junit_case(
            suite,
            f"{suite_name}.timeline",
            str(entry.get("name")),
            entry.get("duration_s"),
            type=str(entry.get("action") or ""),
        )
        if str(entry.get("status")) != "ok":
            failures += 1
            ET.SubElement(
                case,
                "failure",
                message=_junit_text(entry.get("detail") or entry.get("status") or "not ok"),
                type=str(entry.get("action") or "action"),
            )
    run_case = _junit_case(suite, suite_name, "run", report.get("duration_s"))
    status = str(report["status"])
    if status == "failed":
        failures += 1
        ET.SubElement(
            run_case, "failure", message=_junit_text(report.get("reason") or "failed"), type=status
        ).text = f"{report['verdict']}: exit code {report['exit_code']}"
    elif status != "passed":
        errors += 1
        ET.SubElement(
            run_case, "error", message=_junit_text(report.get("reason") or status), type=status
        ).text = f"{report['verdict']}: exit code {report['exit_code']}"
    tests = len(suite.findall("testcase"))
    for element in (suites, suite):
        element.set("tests", str(tests))
        element.set("failures", str(failures))
        element.set("errors", str(errors))
        element.set("skipped", str(skipped))
        element.set("time", _junit_time(report.get("duration_s")))
    ET.indent(suites)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(suites, encoding="unicode") + "\n"
    )


def write_reports(record: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Write `report.json`, `report.md` and `junit.xml` into the run directory.

    Returns the report as data.
    """
    report = build_report(record, run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / REPORT_JSON).write_text(json.dumps(report, indent=2) + "\n")
    (run_dir / REPORT_MD).write_text(render_markdown(report))
    (run_dir / REPORT_JUNIT).write_text(render_junit(report))
    return report
