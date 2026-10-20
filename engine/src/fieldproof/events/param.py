"""The built-in parameter event: `param.set` (task P2-T03).

`ros2 param set` runs inside the vehicle's container against a node of its stack; the entry
ends `ok` when the node accepted the value (a node may refuse one through its parameter
callback, which ends the entry `failed`). Whether the node *uses* a changed value at run
time is the node's business, as in the field.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from fieldproof.dsl import Duration, TimelineBase
from fieldproof.events import FAILED, OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name

PARAM_SET_OK = "Set parameter successful"
# A node name: relative to the vehicle's namespace (`controller_server`) or absolute.
NODE_PATTERN = r"^/?[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*$"
PARAM_PATTERN = r"^[A-Za-z_][A-Za-z0-9_./]*$"


class ParamSet(TimelineBase):
    """Set a parameter of a node of the vehicle's stack with `ros2 param set`."""

    vehicle: Name = Field(description="The vehicle whose node gets the parameter.")
    action: Literal["param.set"]
    node: str = Field(
        pattern=NODE_PATTERN,
        description="The node (relative to the vehicle's namespace, or absolute with `/`).",
    )
    parameter: str = Field(pattern=PARAM_PATTERN, description="The parameter's name.")
    value: bool | int | float | str = Field(
        description="The value (a string stays a string, however it looks)."
    )
    timeout_s: Duration = Field(default=30.0, description="Give up on the node after this long.")


def value_text(value: bool | int | float | str) -> str:
    """The value as `ros2 param set` reads it (YAML): a string quoted so it stays one."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value)


class ParamSetEvent(Event):
    """`param.set`."""

    action: ClassVar[str] = "param.set"
    Entry: ClassVar[type[TimelineBase]] = ParamSet
    summary: ClassVar[str] = "Set a parameter of a node of the vehicle's stack (ros2 param set)."

    def plan(self, entry: ParamSet, context: PlanContext) -> dict[str, Any]:
        """The node's absolute name."""
        vehicle = context.vehicle(entry.vehicle)
        node = entry.node if entry.node.startswith("/") else vehicle.topic(entry.node)
        return {"node": node, "value": value_text(entry.value)}

    def apply(
        self,
        entry: ParamSet,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """`ros2 param set <node> <parameter> <value>` in the vehicle's container."""
        vehicle = context.vehicle(entry.vehicle)
        node, value = str(resolved["node"]), str(resolved["value"])
        limit_s = min(entry.timeout_s, float(resolved.get("limit_s", entry.timeout_s)))
        result = context.ros2(
            vehicle,
            ("param", "set", node, entry.parameter, value),
            timeout_s=limit_s,
            on_line=lambda line: context.log(
                "action.output", line.rstrip(), level="debug", action=record.name
            ),
        )
        if result.exit_code == 124:
            record.status, record.detail = TIMEOUT, f"{node} did not answer within {limit_s:.0f} s"
        elif result.ok and PARAM_SET_OK in result.output:
            record.status, record.detail = OK, f"{node} {entry.parameter} = {value}"
        else:
            record.status = FAILED
            record.detail = f"{node} refused {entry.parameter} (exit {result.exit_code}): " + (
                result.tail(3) or "no output"
            )


EVENTS = (ParamSetEvent,)
