"""An example event plugin (task P2-T03): the timeline action ``example.announce``.

The event publishes a ``std_msgs/msg/String`` once on a topic of the vehicle, from inside the
vehicle's container, with ``ros2 topic pub``: reliable and transient local, the publisher kept
alive for ``KEEP_ALIVE_S`` after the message, so a recorder that discovers the topic late (rosbag2
polls the graph; on a loaded host its subscription comes after a plain ``--once`` publisher has
exited) still receives it (issue #74). It shows the three parts of a plugin: the entry
model (what the scenario file says, validated by the engine's schema and exported to its JSON
Schema), the ``plan`` hook (the topic resolved under the vehicle's namespace before any
container starts) and the ``apply`` hook (the command run through the event context during
the run, the record filled in). The engine finds it through the ``fieldproof.events`` entry
point in ``pyproject.toml``; ``fieldproof events`` lists it next to the built-ins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from fieldproof.dsl import Duration, TimelineBase
from fieldproof.events import FAILED, OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name

MESSAGE_TYPE = "std_msgs/msg/String"
KEEP_ALIVE_S = 2.0


class Announce(TimelineBase):
    """Publish a text once on a topic of the vehicle."""

    vehicle: Name = Field(description="The vehicle whose container publishes.")
    action: Literal["example.announce"]
    topic: str = Field(
        default="announce",
        description="The topic (relative to the vehicle's namespace, or absolute with `/`).",
    )
    text: str = Field(description="The text of the `std_msgs/msg/String` message.")
    subscribers: int = Field(
        default=0,
        ge=0,
        description="Wait for this many subscribers before publishing (0: publish at once; "
        "1 when the topic is recorded, so the recording holds the message).",
    )
    timeout_s: Duration = Field(default=30.0, description="Give up after this long.")


class AnnounceEvent(Event):
    """`example.announce`."""

    action: ClassVar[str] = "example.announce"
    Entry: ClassVar[type[TimelineBase]] = Announce
    summary: ClassVar[str] = "Publish a std_msgs/msg/String once on a topic of the vehicle."

    def plan(self, entry: Announce, context: PlanContext) -> dict[str, Any]:
        """The topic under the vehicle's namespace (the vehicle must exist)."""
        vehicle = context.vehicle(entry.vehicle)
        return {"topic": vehicle.topic(entry.topic)}

    def apply(
        self,
        entry: Announce,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """`ros2 topic pub --once`, reliable, transient local and kept alive, in the container."""
        vehicle = context.vehicle(entry.vehicle)
        topic = str(resolved["topic"])
        limit_s = min(entry.timeout_s, float(resolved.get("limit_s", entry.timeout_s)))
        result = context.ros2(
            vehicle,
            (
                "topic",
                "pub",
                "--once",
                "-w",
                str(entry.subscribers),
                "--keep-alive",
                f"{KEEP_ALIVE_S:g}",
                "--qos-reliability",
                "reliable",
                "--qos-durability",
                "transient_local",
                topic,
                MESSAGE_TYPE,
                json.dumps({"data": entry.text}),
            ),
            timeout_s=limit_s,
            on_line=lambda line: context.log(
                "action.output", line.rstrip(), level="debug", action=record.name
            ),
        )
        if result.exit_code == 124:
            record.status = TIMEOUT
            record.detail = (
                f"no {entry.subscribers} subscriber(s) of {topic} within {limit_s:.0f} s"
            )
        elif result.ok:
            record.status, record.detail = OK, f"published {entry.text!r} on {topic}"
        else:
            record.status = FAILED
            record.detail = f"ros2 topic pub failed (exit {result.exit_code}): {result.tail(3)}"
