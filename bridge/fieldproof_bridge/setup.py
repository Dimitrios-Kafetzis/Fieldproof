# Name, version, dependencies and console scripts live in pyproject.toml; this file keeps the
# setuptools-only options ament_python relies on (colcon runs `setup.py` directly, and its pytest
# step looks for pytest in tests_require).
from pathlib import Path

from setuptools import find_packages, setup

package_name = "fieldproof_bridge"

setup(
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", ["config/README.md"]),
        (
            f"share/{package_name}/launch",
            [*sorted(str(f) for f in Path("launch").glob("*.launch.py")), "launch/README.md"],
        ),
    ],
    zip_safe=True,
    tests_require=["pytest"],
)
