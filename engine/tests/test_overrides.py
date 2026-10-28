"""Tests of the scenario overrides (task P2-T06): `--set` paths, values and their application."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fakes import v0_text

from fieldproof.config import load_config
from fieldproof.overrides import (
    OverrideError,
    apply_overrides,
    dump_document,
    format_value,
    load_scenario_with,
    parse_assignment,
    parse_path,
)
from fieldproof.runner import plan
from fieldproof.scenario import ScenarioError

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples/scenarios/ugv_nav_to_goal.yaml"


def test_parse_path() -> None:
    assert parse_path("time.rtf") == ["time", "rtf"]
    assert parse_path("vehicles[0].resources.cpus") == ["vehicles", 0, "resources", "cpus"]
    assert parse_path("vehicles[ugv1].spawn.frame") == ["vehicles", "ugv1", "spawn", "frame"]
    assert parse_path("timeline[drive_ahead].goal.x") == ["timeline", "drive_ahead", "goal", "x"]
    assert parse_path("a[1][2]") == ["a", 1, 2]
    assert parse_path("seed") == ["seed"]
    for bad in ("", "1abc", "a..b", "a[", "a[]", "a-b", ".a", "a b"):
        with pytest.raises(OverrideError):
            parse_path(bad)


def test_parse_assignment_values_are_yaml() -> None:
    assert parse_assignment("time.rtf=2") == ("time.rtf", 2)
    assert parse_assignment("time.rtf=2.5") == ("time.rtf", 2.5)
    assert parse_assignment("outputs.video=true") == ("outputs.video", True)
    assert parse_assignment('metadata.description="2"') == ("metadata.description", "2")
    assert parse_assignment("metadata.tags=[a, b]") == ("metadata.tags", ["a", "b"])
    assert parse_assignment("timeline[drive].goal={x: 1, y: 2}") == (
        "timeline[drive].goal",
        {"x": 1, "y": 2},
    )
    assert parse_assignment("outputs.directory=") == ("outputs.directory", None)
    assert parse_assignment("outputs.directory=null") == ("outputs.directory", None)
    assert parse_assignment(" seed = 3 ") == ("seed", 3)
    with pytest.raises(OverrideError, match="expected path=value"):
        parse_assignment("time.rtf")
    with pytest.raises(OverrideError, match="expected path=value"):
        parse_assignment("=3")
    with pytest.raises(OverrideError, match="not valid YAML"):
        parse_assignment("a={")
    with pytest.raises(OverrideError, match="is not a key"):
        parse_assignment("a-b=1")


def test_apply_overrides() -> None:
    data = {
        "time": {"mode": "sim"},
        "vehicles": [{"name": "ugv1", "spawn": {"frame": "a"}}, {"name": "ugv2"}],
        "timeline": [{"name": "drive", "goal": {"x": 1.0}}],
    }
    result = apply_overrides(
        data,
        {
            "time.rtf": 2.0,
            "vehicles[ugv2].spawn.frame": "b",
            "vehicles[0].resources.cpus": 1,
            "timeline[drive].goal.x": 5.0,
            "timeline[-1].goal.y": -1.0,
            "outputs.video": True,
            "seed": 7,
        },
    )
    assert result["time"] == {"mode": "sim", "rtf": 2.0}
    assert result["vehicles"][1] == {"name": "ugv2", "spawn": {"frame": "b"}}
    assert result["vehicles"][0]["resources"] == {"cpus": 1}
    assert result["timeline"][0]["goal"] == {"x": 5.0, "y": -1.0}
    assert result["outputs"] == {"video": True}
    assert result["seed"] == 7
    assert data["time"] == {"mode": "sim"}, "the document given is not touched"
    assert "outputs" not in data
    # A whole item can be replaced.
    replaced = apply_overrides(data, {"timeline[0]": {"name": "x", "action": "noop"}})
    assert replaced["timeline"] == [{"name": "x", "action": "noop"}]
    # Lists are not grown; names must exist; mappings take keys.
    with pytest.raises(OverrideError, match="index 2 is out of range"):
        apply_overrides(data, {"vehicles[2].name": "ugv3"})
    with pytest.raises(OverrideError, match="no item named 'ugv9' \\(ugv1, ugv2\\)"):
        apply_overrides(data, {"vehicles[ugv9].name": "x"})
    with pytest.raises(OverrideError, match="a mapping takes a key"):
        apply_overrides(data, {"time[0]": 1})
    with pytest.raises(OverrideError, match="not a mapping or a list"):
        apply_overrides(data, {"time.mode.x": 1})
    with pytest.raises(OverrideError, match="a scenario is a YAML mapping"):
        apply_overrides([1, 2], {"a": 1})


def test_dump_and_format() -> None:
    text = dump_document({"b": 1, "a": [1, 2], "c": {"x": "y"}})
    assert text.splitlines()[0] == "b: 1", "keys keep their order"
    assert yaml.safe_load(text) == {"b": 1, "a": [1, 2], "c": {"x": "y"}}
    assert format_value(2.5) == "2.5"
    assert format_value("abc") == '"abc"'
    assert format_value(None) == "null"
    assert format_value({"x": 1}) == '{"x": 1}'
    assert parse_assignment(f"a={format_value('2')}") == ("a", "2"), "a string stays a string"


def test_load_scenario_with(tmp_path: Path) -> None:
    plain = load_scenario_with(EXAMPLE, {})
    assert plain.overrides == {}
    assert plain.text == EXAMPLE.read_text()
    varied = load_scenario_with(
        EXAMPLE, {"time.rtf": 2.0, "vehicles[ugv1].resources.cpus": 1, "seed": 3}
    )
    assert varied.path == EXAMPLE.resolve(), "relative paths still resolve from the file"
    assert varied.time.rtf == 2.0
    assert varied.vehicles[0].resources is not None
    assert varied.vehicles[0].resources.cpus == 1
    assert varied.seed == 3
    assert varied.overrides == {"time.rtf": 2.0, "vehicles[ugv1].resources.cpus": 1, "seed": 3}
    assert varied.digest != plain.digest, "the digest is of the effective document"
    assert yaml.safe_load(varied.text)["time"]["rtf"] == 2.0
    assert varied.text.startswith("apiVersion:"), "the effective document, as YAML"
    # The plan sees the overridden budget and seed.
    config = load_config(REPO / "fieldproof.toml", env={})
    resolved = plan(varied, config, runs_dir=tmp_path, render="software")
    assert resolved.seed == 3
    assert resolved.vehicles[0].resources.cpus == 1
    assert resolved.run_id.startswith(varied.digest)
    with pytest.raises(ScenarioError, match=r"--set .*no item named 'ugv7'"):
        load_scenario_with(EXAMPLE, {"vehicles[ugv7].resources.cpus": 1})
    with pytest.raises(ScenarioError, match=r"time\.rtf"):
        load_scenario_with(EXAMPLE, {"time.rtf": "fast"})
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ScenarioError, match="No such file"):
        load_scenario_with(missing, {"seed": 1})
    (tmp_path / "bad.yaml").write_text("a: [")
    with pytest.raises(ScenarioError, match="not valid YAML"):
        load_scenario_with(tmp_path / "bad.yaml", {})


def test_v0_variant_keeps_working() -> None:
    """The fake-backend tests strip the library block; the stripped text validates too."""
    stripped = tmp = v0_text(EXAMPLE.read_text())
    assert "assertion library v1" not in stripped
    assert tmp == stripped
