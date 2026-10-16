"""Unit tests for the fieldproof_bridge package skeleton (task P0-T07)."""

import re

import fieldproof_bridge


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+([-+].*)?", fieldproof_bridge.__version__)


def test_package_xml_version_matches_module() -> None:
    assert fieldproof_bridge.package_version() == fieldproof_bridge.__version__


def test_package_name() -> None:
    assert fieldproof_bridge.PACKAGE_NAME == "fieldproof_bridge"
