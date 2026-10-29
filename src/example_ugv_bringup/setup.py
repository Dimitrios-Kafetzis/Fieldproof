from pathlib import Path

from setuptools import find_packages, setup

package_name = "example_ugv_bringup"
share = Path("share") / package_name

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (str(share), ["package.xml"]),
        (str(share / "launch"), [str(p) for p in Path("launch").glob("*.launch.py")]),
        (str(share / "config"), [str(p) for p in Path("config").glob("*.yaml")]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Dimitris Kafetzis",
    maintainer_email="dimitrioskafetzis@gmail.com",
    description="Bringup of the example UGV: the field launch, the waypoint navigator, parameters",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "waypoint_navigator = example_ugv_bringup.waypoint_navigator:main",
        ],
    },
)
