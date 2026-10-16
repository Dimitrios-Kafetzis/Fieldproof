"""Fieldproof bridge: ros_gz configuration generator and adapter nodes (layer L2).

Task P0-T07 ships the package skeleton; the manifest-driven generator, the adapter nodes and the
contract test harness arrive with P1-T03.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

from ament_index_python.packages import get_package_share_directory

__all__ = ["PACKAGE_NAME", "__version__", "package_version"]

PACKAGE_NAME = "fieldproof_bridge"

__version__ = "0.3.0"  # x-release-please-version


def package_version() -> str:
    """Return the version recorded in the installed ``package.xml`` of this package.

    ROS packages carry their version in ``package.xml``; the ament index exposes the installed
    share directory, so the test suite can check that the module and the manifest agree.
    """
    manifest = Path(get_package_share_directory(PACKAGE_NAME)) / "package.xml"
    version = ET.parse(manifest).getroot().findtext("version")  # noqa: S314  (our own file)
    if not version:
        msg = f"{manifest} has no <version> element"
        raise ValueError(msg)
    return version
