from setuptools import find_packages, setup

package_name = "fieldproof_bridge"

setup(
    name=package_name,
    version="0.4.0",  # x-release-please-version
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", ["config/README.md"]),
        (f"share/{package_name}/launch", ["launch/README.md"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Dimitris Kafetzis",
    maintainer_email="dimitrioskafetzis@gmail.com",
    description="ros_gz bridge configuration generator and adapter nodes for Fieldproof vehicles",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={"console_scripts": []},
)
