"""HTML reports (task P2-T06): a run's ``report.html`` and a matrix's.

One self-contained page each, no script and no external resource, so the file opens from a
run directory, a CI artefact or a mail attachment as it is. The run page shows what the
Markdown report shows (:mod:`fieldproof.report`): the verdict, the facts of the run, every
assertion with its reason and evidence (the stream excerpt of a failed one folded under it),
the KPIs, the timeline, the phases and the artefacts, linked relative to the page. The matrix
page lists the cases with their parameters, verdicts, attempts and flakiness, links every
case's own report, and tabulates the KPIs across the cases.
"""

from __future__ import annotations

import html
import json
from typing import Any

STYLE = """
:root { color-scheme: light dark; --pass: #1a7f37; --fail: #b42318; --warn: #9a6700;
  --muted: #6b7280; --line: #d0d7de; --bg: #f6f8fa; }
body { font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem auto;
  max-width: 72rem; padding: 0 1rem; }
h1 { font-size: 1.6rem; margin: 0 0 .5rem; }
h2 { font-size: 1.2rem; margin: 2rem 0 .5rem; border-bottom: 1px solid var(--line); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .92em; }
pre { background: var(--bg); padding: .6rem .8rem; overflow-x: auto; border-radius: 4px; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
th, td { border: 1px solid var(--line); padding: .3rem .5rem; text-align: left;
  vertical-align: top; }
th { background: var(--bg); }
td.num, th.num { text-align: right; white-space: nowrap; }
.badge { display: inline-block; padding: .1rem .6rem; border-radius: 1rem; color: #fff;
  font-weight: 600; font-size: .9em; }
.pass { background: var(--pass); } .fail { background: var(--fail); }
.warn { background: var(--warn); }
.other { background: var(--muted); }
.muted { color: var(--muted); }
details { margin: .3rem 0 .8rem; } summary { cursor: pointer; }
ul.facts { padding-left: 1.2rem; } ul.facts li { margin: .15rem 0; }
"""

BADGES = {
    "PASS": "pass",
    "FAIL": "fail",
    "TIMEOUT": "fail",
    "ERROR": "fail",
    "INTERRUPTED": "warn",
    "FLAKY": "warn",
    "SKIP": "other",
    "SKIPPED": "other",
    "PENDING": "other",
}
ASSERTION_MARKS = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "pending": "PENDING"}


def esc(value: Any) -> str:
    """`value` as HTML text."""
    return html.escape(str(value), quote=True)


def fmt(value: Any, unit: str = "", digits: int = 1) -> str:
    """A number with its unit, `-` for none."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return f"{value:.{digits}f}{unit}" if isinstance(value, float) else f"{value}{unit}"
    return esc(value)


def badge(verdict: Any) -> str:
    """A coloured verdict."""
    text = str(verdict or "-")
    return f'<span class="badge {BADGES.get(text, "other")}">{esc(text)}</span>'


def code(value: Any) -> str:
    """`value` in a code span."""
    return f"<code>{esc(value)}</code>"


def table(headers: list[str], rows: list[list[str]], *, numeric: set[int] | None = None) -> str:
    """A table of cells already rendered as HTML."""
    numeric = numeric or set()

    def cell(tag: str, index: int, content: str) -> str:
        cls = ' class="num"' if index in numeric else ""
        return f"<{tag}{cls}>{content}</{tag}>"

    head = "".join(cell("th", i, esc(h)) for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(cell("td", i, c) for i, c in enumerate(row)) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def page(title: str, body: str) -> str:
    """A complete document."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>{esc(title)}</title>\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<style>{STYLE}</style></head>\n<body>\n{body}\n</body></html>\n"
    )


def _facts(items: list[str]) -> str:
    return '<ul class="facts">' + "".join(f"<li>{item}</li>" for item in items if item) + "</ul>"


def _evidence(evidence: dict[str, Any]) -> str:
    """The evidence of an assertion without its excerpt, as compact JSON."""
    data = {k: v for k, v in evidence.items() if k != "excerpt"}
    if not data:
        return "-"
    return code(json.dumps(data, sort_keys=True, default=str))


def _excerpt(assertion: dict[str, Any]) -> str:
    excerpt = (assertion.get("evidence") or {}).get("excerpt") or []
    if not excerpt:
        return ""
    lines = "\n".join(json.dumps(sample, sort_keys=True, default=str) for sample in excerpt)
    return (
        f"<details><summary>The stream before <code>{esc(assertion.get('name'))}</code> "
        f"failed ({len(excerpt)} sample(s))</summary><pre>{esc(lines)}</pre></details>"
    )


def _overrides(overrides: dict[str, Any]) -> str:
    if not overrides:
        return ""
    return "Overrides: " + ", ".join(
        f"{code(k)} = {code(json.dumps(v))}" for k, v in sorted(overrides.items())
    )


def _kpi_rows(kpis: dict[str, Any]) -> list[list[str]]:
    rows = []
    for name, kpi in sorted(kpis.items()):
        if not isinstance(kpi, dict):
            continue
        value = kpi.get("value")
        rows.append(
            [
                code(name),
                fmt(value, digits=3) if isinstance(value, float) else fmt(value),
                esc(kpi.get("unit") or "-"),
                code(kpi.get("source") or "-"),
                esc(kpi.get("type") or "-"),
            ]
        )
    return rows


def render_html(report: dict[str, Any]) -> str:
    """A run's report as an HTML page."""
    scenario = report.get("scenario") or {}
    name = scenario.get("name") or report["run_id"]
    parts = [
        f"<h1>Fieldproof run report: {esc(name)}</h1>",
        f"<p>{badge(report.get('verdict'))} status {code(report.get('status'))}, exit code "
        f"{esc(report.get('exit_code'))}"
        + (f": {esc(report['reason'])}" if report.get("reason") else "")
        + "</p>",
    ]
    vehicles = ", ".join(
        f"{code(v.get('name'))} ({esc(v.get('manifest', '-'))}, {code(v.get('image', '-'))})"
        for v in report.get("vehicles") or []
    )
    clock = report.get("clock") or {}
    facts = [
        f"Run {code(report['run_id'])}, engine {esc(report.get('engine_version') or '-')}",
        f"Scenario {code(scenario.get('path', '-'))} (sha {code(scenario.get('digest', '-'))}), "
        f"world {code(report.get('world') or '-')}",
        f"Vehicles: {vehicles}" if vehicles else "",
        f"Started {esc(report.get('started_at') or '-')}, finished "
        f"{esc(report.get('finished_at') or '-')}; {fmt(report.get('duration_s'), ' s')} wall "
        f"clock, timeline {fmt(report.get('timeline_duration_s'), ' s')}"
        + (
            f", {fmt(report.get('sim_elapsed_s'), ' s')} of simulation"
            if report.get("sim_elapsed_s") is not None
            else ""
        ),
        f"Time mode {code((report.get('time') or {}).get('mode', '-'))}, seed "
        f"{code(report.get('seed') if report.get('seed') is not None else '-')}"
        + (
            f", real-time factor {fmt(clock.get('measured_rtf'), '', 2)} "
            f"(target {fmt(clock.get('target_rtf'), '', 2)})"
            if clock.get("measured_rtf") is not None
            else ""
        ),
        _overrides(report.get("overrides") or {}),
    ]
    parts.append(_facts(facts))
    assertions = report.get("assertions") or []
    parts.append("<h2>Assertions</h2>")
    if assertions:
        counts = report.get("assertion_counts") or {}
        summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
        parts.append(f"<p>{len(assertions)} assertion(s): {esc(summary)}.</p>")
        parts.append(
            table(
                ["Assertion", "Type", "Verdict", "At", "Reason", "Evidence"],
                [
                    [
                        code(a.get("name")),
                        code(a.get("type")),
                        badge(ASSERTION_MARKS.get(str(a.get("status")), str(a.get("status")))),
                        fmt(a.get("t_s"), " s"),
                        esc(a.get("reason") or "-"),
                        _evidence(a.get("evidence") or {}),
                    ]
                    for a in assertions
                ],
                numeric={3},
            )
        )
        parts += [_excerpt(a) for a in assertions if str(a.get("status")) == "failed"]
    else:
        parts.append("<p>The scenario declares no assertions.</p>")
    kpis = report.get("kpis") or {}
    parts.append("<h2>KPIs</h2>")
    if kpis:
        parts.append(
            table(["KPI", "Value", "Unit", "Source", "Type"], _kpi_rows(kpis), numeric={1})
        )
    else:
        parts.append("<p>No KPI was measured.</p>")
    timeline = report.get("timeline") or []
    parts.append("<h2>Timeline</h2>")
    if timeline:
        parts.append(
            table(
                ["Action", "Vehicle", "At", "Status", "Duration", "Detail"],
                [
                    [
                        f"{code(e.get('name'))} ({esc(e.get('action'))})",
                        code(e.get("vehicle") or "-"),
                        fmt(float(e.get("at_s") or 0.0), " s", 0),
                        esc(e.get("status")),
                        fmt(e.get("duration_s"), " s"),
                        esc(e.get("detail") or "-"),
                    ]
                    for e in timeline
                ],
                numeric={2, 4},
            )
        )
    else:
        parts.append("<p>The scenario has no timeline.</p>")
    parts.append("<h2>Phases</h2>")
    parts.append(
        table(
            ["Phase", "Duration"],
            [
                [esc(p.get("name")), fmt(p.get("duration_s"), " s")]
                for p in report.get("phases") or []
            ],
            numeric={1},
        )
    )
    parts.append("<h2>Artefacts</h2>")
    artifacts = report.get("artifacts") or []
    if artifacts:
        items = [
            f'<a href="{esc(a.get("path"))}">{code(a.get("path"))}</a> '
            f'<span class="muted">({esc(a.get("kind") or "file")}, {esc(a.get("bytes"))} bytes)'
            "</span>"
            for a in artifacts
        ]
        parts.append(_facts(items))
    else:
        parts.append("<p>No artefact in the run directory.</p>")
    return page(f"Fieldproof run report: {name}", "\n".join(parts))


def _parameters(case: dict[str, Any]) -> str:
    overrides = {k: v for k, v in (case.get("overrides") or {}).items() if k != "seed"}
    if not overrides:
        return '<span class="muted">-</span>'
    return ", ".join(f"{code(k)}={code(json.dumps(v))}" for k, v in overrides.items())


def render_matrix_html(record: dict[str, Any]) -> str:
    """A matrix's record (``matrix.json``) as an HTML page."""
    matrix = record.get("matrix") or {}
    name = matrix.get("name") or record["matrix_id"]
    counts = record.get("counts") or {}
    schedule = record.get("schedule") or {}
    capacity = schedule.get("capacity") or {}
    parts = [
        f"<h1>Fieldproof matrix report: {esc(name)}</h1>",
        f"<p>{badge(record.get('verdict'))} status {code(record.get('status'))}, exit code "
        f"{esc(record.get('exit_code'))}"
        + (f": {esc(record['reason'])}" if record.get("reason") else "")
        + "</p>",
        _facts(
            [
                f"Matrix {code(record['matrix_id'])}, engine "
                f"{esc(record.get('engine_version') or '-')}",
                f"Sweep {code(matrix.get('path', '-'))} (sha {code(matrix.get('digest', '-'))}), "
                f"scenario {code((record.get('scenario') or {}).get('path', '-'))}",
                esc(matrix["description"]) if matrix.get("description") else "",
                f"Started {esc(record.get('started_at') or '-')}, finished "
                f"{esc(record.get('finished_at') or '-')}; {fmt(record.get('duration_s'), ' s')} "
                "wall clock",
                f"{esc(counts.get('cases', 0))} case(s): {esc(counts.get('passed', 0))} passed, "
                f"{esc(counts.get('failed', 0))} failed, {esc(counts.get('flaky', 0))} flaky, "
                f"{esc(counts.get('skipped', 0))} skipped; {esc(counts.get('attempts', 0))} "
                f"attempt(s), {esc(schedule.get('retries', 0))} retr"
                f"{'y' if schedule.get('retries', 0) == 1 else 'ies'} per case",
                f"Parallel {code(schedule.get('parallel', '-'))}, at most "
                f"{esc(schedule.get('max_concurrent', '-'))} at once, capacity "
                f"{fmt(capacity.get('cpus'), ' CPU(s)')} and "
                f"{fmt((capacity.get('memory_bytes') or 0) / 1024**3, ' GiB')} after the engine's "
                "reserve",
            ]
        ),
        "<h2>Cases</h2>",
    ]
    rows = []
    kpi_names: set[str] = set()
    for case in record.get("cases") or []:
        attempts = case.get("attempts") or []
        final = attempts[-1] if attempts else {}
        verdict = final.get("verdict") or case.get("status", "-").upper()
        run_id = final.get("run_id")
        link = f'<a href="{esc(run_id)}/report.html">{code(run_id)}</a>' if run_id else "-"
        kpi_names.update((case.get("kpis") or {}).keys())
        rows.append(
            [
                code(case.get("id")),
                esc(case.get("name")),
                _parameters(case),
                esc(case.get("seed") if case.get("seed") is not None else "-"),
                badge(verdict) + (" " + badge("FLAKY") if case.get("flaky") else ""),
                esc(len(attempts)),
                fmt(final.get("duration_s"), " s"),
                link,
                esc(final.get("reason") or "-"),
            ]
        )
    parts.append(
        table(
            [
                "Case",
                "Name",
                "Parameters",
                "Seed",
                "Verdict",
                "Attempts",
                "Duration",
                "Run",
                "Reason",
            ],
            rows,
            numeric={3, 5, 6},
        )
    )
    parts.append("<h2>KPIs</h2>")
    if kpi_names:
        names = sorted(kpi_names)
        kpi_rows = []
        for case in record.get("cases") or []:
            kpis = case.get("kpis") or {}
            row = [code(case.get("id")), esc(case.get("name"))]
            for kpi_name in names:
                kpi = kpis.get(kpi_name) or {}
                value = kpi.get("value")
                row.append(fmt(value, digits=3) if isinstance(value, float) else fmt(value))
            kpi_rows.append(row)
        units = {
            kpi_name: next(
                (
                    (c.get("kpis") or {}).get(kpi_name, {}).get("unit")
                    for c in record.get("cases") or []
                    if (c.get("kpis") or {}).get(kpi_name)
                ),
                None,
            )
            for kpi_name in names
        }
        headers = ["Case", "Name"] + [f"{n} ({units[n]})" if units.get(n) else n for n in names]
        parts.append(table(headers, kpi_rows, numeric=set(range(2, 2 + len(names)))))
    else:
        parts.append("<p>No KPI was measured.</p>")
    return page(f"Fieldproof matrix report: {name}", "\n".join(parts))
