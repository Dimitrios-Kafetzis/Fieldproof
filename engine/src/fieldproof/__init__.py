"""Fieldproof scenario engine.

Real ROS 2 stacks in containers, fed by simulated sensors, driven by declarative scenarios with
assertions. This package holds the scenario schema, the runner and the ``fieldproof`` CLI.
"""

from importlib.metadata import version

__all__ = ["__version__"]

__version__: str = version("fieldproof")
