"""Tests for the fieldproof_robots package skeleton (task P0-T07): the installed layout."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ament_index_python.packages import get_package_share_directory

PACKAGE = "fieldproof_robots"


def share() -> Path:
    return Path(get_package_share_directory(PACKAGE))


def test_package_is_indexed() -> None:
    assert share().is_dir()


def test_models_and_launch_are_installed() -> None:
    assert (share() / "models" / "README.md").is_file()
    assert (share() / "launch" / "README.md").is_file()


def test_manifest_version_is_semver() -> None:
    version = ET.parse(share() / "package.xml").getroot().findtext("version")  # noqa: S314
    assert version is not None
    assert re.fullmatch(r"\d+\.\d+\.\d+([-+].*)?", version), version
