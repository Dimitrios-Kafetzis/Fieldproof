"""What the adapter nodes share: QoS from parameters and the run loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

RELIABILITY = {
    "reliable": ReliabilityPolicy.RELIABLE,
    "best_effort": ReliabilityPolicy.BEST_EFFORT,
}
DURABILITY = {
    "volatile": DurabilityPolicy.VOLATILE,
    "transient_local": DurabilityPolicy.TRANSIENT_LOCAL,
}


def declare_io_parameters(node: Node) -> tuple[str, str, QoSProfile]:
    """Declare and read ``input``, ``output`` and the ``qos_*`` parameters of an adapter.

    Returns the input topic, the output topic and the QoS profile the output is published with
    (the manifest's declaration for that topic). The input, a hidden topic the bridge publishes,
    is read with the bridge's default profile.
    """
    node.declare_parameter("input", "")
    node.declare_parameter("output", "")
    node.declare_parameter("qos_reliability", "reliable")
    node.declare_parameter("qos_durability", "volatile")
    node.declare_parameter("qos_depth", 10)
    input_topic = str(node.get_parameter("input").value)
    output_topic = str(node.get_parameter("output").value)
    if not input_topic or not output_topic:
        msg = f"{node.get_name()}: the parameters input and output are required"
        raise ValueError(msg)
    reliability = str(node.get_parameter("qos_reliability").value)
    durability = str(node.get_parameter("qos_durability").value)
    if reliability not in RELIABILITY or durability not in DURABILITY:
        msg = f"{node.get_name()}: unknown QoS {reliability!r}/{durability!r}"
        raise ValueError(msg)
    qos = QoSProfile(
        depth=int(node.get_parameter("qos_depth").value),
        reliability=RELIABILITY[reliability],
        durability=DURABILITY[durability],
        history=HistoryPolicy.KEEP_LAST,
    )
    return input_topic, output_topic, qos


def run(make_node: Callable[[], Any], argv: list[str] | None = None) -> int:
    """Initialise rclpy, spin the node ``make_node`` builds until shutdown, and clean up."""
    rclpy.init(args=argv)
    node = None
    try:
        node = make_node()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.try_shutdown()
    return 0
