#!/usr/bin/env python3
"""The clock agent of a vehicle in lockstep mode (task P2-T04, ADR-0006).

The runner starts one agent per vehicle in a container of the bridge image that shares the
vehicle container's network namespace (so it hears exactly the ``/clock`` the vehicle hears,
through the same link when the run has radio links), once the vehicle is ready. The agent
subscribes to ``/clock`` and publishes an acknowledgement on ``/fieldproof/lockstep``
(a ``std_msgs/msg/String`` carrying ``{"vehicle": ..., "sim": ..., "step": ...}``) every time
the clock crosses a step boundary, and repeats its last acknowledgement once a second in case
one was lost. The observer's clock manager requests the next step only once every vehicle has
acknowledged the current one.

An acknowledgement says the clock reached the vehicle's network namespace: the vehicle's
stack, on simulation time, cannot run ahead of it, and the world does not advance until every
stack has been given the step. It does not say the stack finished the work the step gave it,
which only the stack could know.

Like the observer, this file depends on nothing but the standard library inside the container
(rclpy, rosgraph_msgs and std_msgs come from the image); the pure part is tested in the
engine, the ROS adapter by the integration test.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from collections.abc import Callable, Sequence

NODE_NAME = "fieldproof_lockstep"
REPEAT_S = 1.0
"""How often the last acknowledgement is repeated."""
EPS = 1e-6
DEFAULT_TOPIC = "/fieldproof/lockstep"


class Agent:
    """Turns clock values into acknowledgements, one per step boundary."""

    def __init__(self, vehicle: str, step_s: float, publish: Callable[[str], None]) -> None:
        self.vehicle = vehicle
        self.step_s = step_s
        self.publish = publish
        self.last_step = -1
        self.last_ack: str | None = None
        self.acks = 0

    def on_clock(self, sim: float) -> str | None:
        """A clock value; the acknowledgement published if it crossed a boundary."""
        step = math.floor(sim / self.step_s + EPS)
        if step <= self.last_step:
            return None
        self.last_step = step
        self.last_ack = json.dumps({"vehicle": self.vehicle, "sim": round(sim, 6), "step": step})
        self.acks += 1
        self.publish(self.last_ack)
        return self.last_ack

    def repeat(self) -> None:
        """Publish the last acknowledgement again (a lost message is then made up for)."""
        if self.last_ack is not None:
            self.publish(self.last_ack)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point inside the agent's container."""
    parser = argparse.ArgumentParser(description="Fieldproof lockstep clock agent")
    parser.add_argument("--vehicle", required=True, help="the vehicle acknowledged for")
    parser.add_argument("--step-s", required=True, type=float, help="the lockstep step")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="the acknowledgement topic")
    args = parser.parse_args(argv)

    import rclpy  # noqa: PLC0415  (inside the container only)
    from rclpy.qos import (  # noqa: PLC0415
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from rosgraph_msgs.msg import Clock  # noqa: PLC0415
    from std_msgs.msg import String  # noqa: PLC0415

    rclpy.init()
    node = rclpy.create_node(f"{NODE_NAME}_{args.vehicle}")
    publisher = node.create_publisher(
        String,
        args.topic,
        QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        ),
    )
    agent = Agent(args.vehicle, args.step_s, lambda text: publisher.publish(String(data=text)))

    def on_clock(message: Clock) -> None:
        agent.on_clock(message.clock.sec + message.clock.nanosec / 1e9)

    node.create_subscription(
        Clock,
        "/clock",
        on_clock,
        QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        ),
    )
    node.create_timer(REPEAT_S, agent.repeat)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    print(  # noqa: T201  (the container's log)
        f"ready: {args.vehicle} acknowledges {args.step_s:g} s steps on {args.topic}", flush=True
    )
    started = time.monotonic()
    reported = 0
    try:
        while not stopping:
            rclpy.spin_once(node, timeout_sec=0.2)
            if agent.acks and agent.acks // 100 > reported:
                reported = agent.acks // 100
                print(  # noqa: T201
                    f"{agent.acks} acknowledgement(s), last {agent.last_ack}, "
                    f"{time.monotonic() - started:.0f} s",
                    flush=True,
                )
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
