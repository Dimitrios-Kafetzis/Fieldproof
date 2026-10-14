# Security policy

## Supported versions

Until `v1.0.0` only the `main` branch is supported. After `v1.0.0`, the latest minor release receives
security fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability), or email dimitrioskafetzis@gmail.com with:

- a description of the issue and its impact,
- steps or a scenario file to reproduce it,
- the commit or version affected.

You will receive an acknowledgement within 72 hours and a status update at least every two weeks
until the issue is resolved. Fixes are released with a security advisory and credit to the reporter
unless anonymity is requested.

## Scope

Fieldproof runs third-party robot code in containers on the operator's machine. Issues in the
isolation between vehicle containers, the host, and the cockpit's network surface are in scope.
Vulnerabilities in upstream projects (Gazebo, ROS 2, PX4, ArduPilot) should be reported upstream;
we will track and pin fixed versions.
