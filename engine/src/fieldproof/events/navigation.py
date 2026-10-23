"""The built-in navigation event: `navigate_to_pose` (task P1-T05, ported in P2-T03).

The goal is given in the world and sent in the vehicle's `map` frame, which starts at its
spawn pose (one TF tree per vehicle, task P2-T01); `ros2 action send_goal` runs inside the
vehicle's container and the entry ends with the goal's result.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from fieldproof.backend import ExecResult
from fieldproof.dsl import Duration, Placement, TimelineBase
from fieldproof.events import FAILED, OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name
from fieldproof.world import Pose, place

NAV2_ACTION = "navigate_to_pose"
NAV2_ACTION_TYPE = "nav2_msgs/action/NavigateToPose"
GOAL_STATUS = re.compile(r"Goal finished with status: (\w+)")
GOAL_ACCEPTED = re.compile(r"Goal accepted with ID")


class NavigateToPose(TimelineBase):
    """Send a Nav2 `NavigateToPose` goal to a vehicle and wait for its result."""

    vehicle: Name = Field(description="The vehicle the goal goes to.")
    action: Literal["navigate_to_pose"]
    goal: Placement = Field(description="The goal, in the world (frame or coordinates).")
    timeout_s: Duration = Field(default=300.0, description="Give up on the result after this long.")


def goal_status(result: ExecResult) -> str | None:
    """The goal status `ros2 action send_goal` printed, if any."""
    match = GOAL_STATUS.search(result.output)
    return match.group(1) if match else None


def goal_message(goal: Pose) -> str:
    """The `NavigateToPose` goal as the YAML `ros2 action send_goal` takes."""
    return json.dumps(
        {
            "pose": {
                "header": {"frame_id": "map"},
                "pose": {
                    "position": {"x": goal.x, "y": goal.y, "z": 0.0},
                    "orientation": {"z": math.sin(goal.yaw / 2), "w": math.cos(goal.yaw / 2)},
                },
            }
        }
    )


class NavigateToPoseEvent(Event):
    """`navigate_to_pose`."""

    action: ClassVar[str] = "navigate_to_pose"
    Entry: ClassVar[type[TimelineBase]] = NavigateToPose
    summary: ClassVar[str] = (
        "Send a Nav2 NavigateToPose goal (in the world) to the vehicle and wait for its result."
    )

    def plan(self, entry: NavigateToPose, context: PlanContext) -> dict[str, Any]:
        """The goal in the world and in the vehicle's map frame."""
        goal_world = place(entry.goal, context.world, f"{context.where}.goal")
        spawn = context.vehicle(entry.vehicle).spawn
        return {"goal_world": goal_world, "goal_map": spawn.relative(goal_world)}

    def apply(
        self,
        entry: NavigateToPose,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Send the goal with `ros2 action send_goal` inside the vehicle and read the result."""
        vehicle = context.vehicle(entry.vehicle)
        goal: Pose = resolved["goal_map"]
        goal_world: Pose = resolved["goal_world"]
        limit_s = resolved.get("limit_s", entry.timeout_s)
        context.log(
            "action.goal",
            f"{record.name}: {vehicle.name} to ({goal_world.x:g}, {goal_world.y:g}) "
            f"in the world, ({goal.x:.2f}, {goal.y:.2f}, yaw {goal.yaw:.2f}) in map, "
            f"limit {limit_s:.0f} s",
            action=record.name,
            vehicle=vehicle.name,
        )

        def on_line(line: str) -> None:
            context.log("action.output", line.rstrip(), level="debug", action=record.name)
            if GOAL_ACCEPTED.search(line):
                context.started(record)  # the lockstep clock may advance (task P2-T04)

        result = context.ros2(
            vehicle,
            (
                "action",
                "send_goal",
                vehicle.topic(NAV2_ACTION),
                NAV2_ACTION_TYPE,
                goal_message(goal),
            ),
            timeout_s=limit_s,
            on_line=on_line,
        )
        status = goal_status(result)
        if result.exit_code == 124:
            record.status, record.detail = TIMEOUT, f"no result within {limit_s:.0f} s"
        elif status == "SUCCEEDED":
            record.status, record.detail = OK, "goal SUCCEEDED"
        elif status is not None:
            record.status, record.detail = FAILED, f"goal {status}"
        else:
            record.status = FAILED
            record.detail = f"no goal result (exit {result.exit_code}): {result.tail(3)}"


EVENTS = (NavigateToPoseEvent,)
