"""Fieldproof bridge (layer L2): ros_gz configuration generator, adapter nodes, contract checks.

``fieldproof_bridge.generator`` turns a vehicle manifest (``robot.yaml``) into a bridge bundle:
the ros_gz ``parameter_bridge`` configurations, the parameters of the adapter nodes, the ROS
interface contract and a launch file. ``fieldproof_bridge.adapters`` are the rclpy nodes that
reshape what the bridge cannot (IMU covariance, GNSS fix frame and covariance, joint-state
throttling), and ``fieldproof_bridge.contract`` checks a running stack against the contract.
Only the adapters and the checker import ROS; the generator is plain Python, so the engine's
``fieldproof bridge gen`` runs it on the host.
"""

from __future__ import annotations

__all__ = ["PACKAGE_NAME", "__version__", "package_version"]

PACKAGE_NAME = "fieldproof_bridge"

__version__ = "0.12.1"  # x-release-please-version


def package_version() -> str:
    """Return the version recorded in the installed ``package.xml`` of this package.

    ROS packages carry their version in ``package.xml``; the ament index exposes the installed
    share directory, so the test suite can check that the module and the manifest agree. The
    ament import is local so that the module imports without ROS (the engine installs it on the
    host).
    """
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from ament_index_python.packages import get_package_share_directory  # noqa: PLC0415

    manifest = Path(get_package_share_directory(PACKAGE_NAME)) / "package.xml"
    version = ET.parse(manifest).getroot().findtext("version")  # noqa: S314  (our own file)
    if not version:
        msg = f"{manifest} has no <version> element"
        raise ValueError(msg)
    return version
