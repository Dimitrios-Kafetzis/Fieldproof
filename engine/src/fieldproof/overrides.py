"""Scenario overrides (task P2-T06): ``--set path=value`` and the matrix's parameters.

An override names a value of the scenario document by a path and replaces it before the
document is validated, so a sweep (``fieldproof matrix``) or a one-off run (``fieldproof run
--set time.rtf=2``) varies a scenario without a copy of the file. A path is a dotted sequence of
keys; a list is entered by an index (``vehicles[0]``) or by the ``name`` of one of its items
(``vehicles[ugv1]``, ``timeline[drive_ahead]``, ``assertions[reached_goal]``). The value is
YAML, so ``2``, ``2.5``, ``true``, ``"2"``, ``[1, 2]`` and ``{x: 1, y: 2}`` are what they look
like. A missing mapping key is created (``outputs.video=true`` on a scenario without an
``outputs`` section works); a missing list item is an error, since a new vehicle or timeline
entry wants more than one field.

The effective document is what the run records (``scenario.yaml`` in the run directory holds it,
``run.json`` lists the ``overrides``), and its text is what the run id's digest is taken from,
so two runs with different overrides never share a scenario hash.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from fieldproof.scenario import Scenario, ScenarioError, validate_data

SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)((?:\[[^\[\]]+\])*)$")
SELECTOR = re.compile(r"\[([^\[\]]+)\]")


class OverrideError(ValueError):
    """An override cannot be parsed or applied."""


def parse_assignment(text: str) -> tuple[str, Any]:
    """`path=value` as the path and the value parsed as YAML."""
    path, separator, raw = text.partition("=")
    if not separator or not path.strip():
        raise OverrideError(f"{text!r}: expected path=value")
    try:
        value = yaml.safe_load(raw) if raw.strip() else None
    except yaml.YAMLError as error:
        raise OverrideError(f"{text!r}: the value is not valid YAML: {error}") from error
    parse_path(path.strip())
    return path.strip(), value


def parse_path(path: str) -> list[str | int]:
    """The path's steps: a key, or a list selector (an index, or an item's name)."""
    steps: list[str | int] = []
    for part in path.split("."):
        match = SEGMENT.fullmatch(part)
        if match is None:
            raise OverrideError(
                f"{path!r}: {part!r} is not a key (letters, digits, underscores, then "
                "optional [index] or [name] selectors)"
            )
        steps.append(match.group(1))
        for selector in SELECTOR.findall(match.group(2)):
            steps.append(int(selector) if selector.lstrip("-").isdigit() else selector)
    return steps


def _describe(steps: list[str | int], upto: int) -> str:
    """The path up to a step, for error messages."""
    text = ""
    for step in steps[:upto]:
        if isinstance(step, int):
            text += f"[{step}]"
        else:
            text = f"{text}.{step}" if text else step
    return text


def _select(items: list[Any], step: str | int, where: str) -> int:
    """The index of a list item named by `step`."""
    if isinstance(step, int):
        if not -len(items) <= step < len(items):
            raise OverrideError(f"{where}: index {step} is out of range ({len(items)} item(s))")
        return step % len(items)
    names = [item.get("name") if isinstance(item, Mapping) else None for item in items]
    if step not in names:
        known = ", ".join(str(n) for n in names if n) or "none named"
        raise OverrideError(f"{where}: no item named {step!r} ({known})")
    return names.index(step)


def apply_overrides(data: Any, overrides: Mapping[str, Any]) -> Any:
    """A copy of the document with every override applied, in order."""
    document = copy.deepcopy(data)
    if not isinstance(document, dict):
        raise OverrideError("a scenario is a YAML mapping")
    for path, value in overrides.items():
        steps = parse_path(path)
        _assign(document, steps, value, path)
    return document


def _assign(document: dict[str, Any], steps: list[str | int], value: Any, path: str) -> None:
    node: Any = document
    for index, step in enumerate(steps[:-1]):
        where = f"{path}: at {_describe(steps, index + 1)}"
        if isinstance(node, list):
            node = node[_select(node, step, where)]
            continue
        if not isinstance(node, dict):
            raise OverrideError(f"{where}: not a mapping or a list")
        if not isinstance(step, str):
            raise OverrideError(f"{where}: a mapping takes a key, not a selector")
        if step not in node or node[step] is None:
            node[step] = [] if isinstance(steps[index + 1], int) else {}
        node = node[step]
    last = steps[-1]
    where = f"{path}: at {_describe(steps, len(steps))}"
    if isinstance(node, list):
        node[_select(node, last, where)] = copy.deepcopy(value)
    elif isinstance(node, dict):
        if not isinstance(last, str):
            raise OverrideError(f"{where}: a mapping takes a key, not a selector")
        node[last] = copy.deepcopy(value)
    else:
        raise OverrideError(f"{where}: not a mapping or a list")


def dump_document(data: Any) -> str:
    """The document as YAML, keys in their order."""
    return str(yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True))


def load_scenario_with(path: Path | str, overrides: Mapping[str, Any]) -> Scenario:
    """The scenario at `path` with `overrides` applied, validated as if read from `path`."""
    file = Path(path).resolve()
    try:
        text = file.read_text()
    except OSError as error:
        raise ScenarioError(f"{file}: {error.strerror}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ScenarioError(f"{file}: not valid YAML: {error}") from error
    if not overrides:
        return validate_data(data, file, text)
    try:
        document = apply_overrides(data, overrides)
    except OverrideError as error:
        raise ScenarioError(f"{file}: --set {error}") from error
    scenario = validate_data(document, file, dump_document(document))
    scenario._overrides = dict(overrides)
    return scenario


def format_value(value: Any) -> str:
    """A value the way `--set` takes it back (JSON, which YAML reads)."""
    return json.dumps(value)
