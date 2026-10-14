# 06 — Session guide

Instructions for every working session on Fieldproof, whoever or whatever is doing the work. The
project is built one backlog task per session; this guide is the contract a session follows.

## Start of every session

1. Read `docs/plan/STATUS.md` to see which task is in progress and what the last session left behind.
2. Read the task's entry in `docs/plan/03-task-backlog.md`. Its *Definition of done* is the contract.
3. Read `docs/plan/05-working-model.md` for the session protocol, then any ADR the task references.
4. Create or check out the branch `task/<task-id>-<slug>` before changing code.

## Non-negotiables

- Decisions live in `docs/adr/`. Do not contradict an accepted ADR; propose a new ADR instead.
- Conventional Commits. One task per branch, one PR per task, squash-merged to `main`.
- Commits are authored by the maintainer only. No co-author trailers, no tool attribution in commit
  messages, PR descriptions, code comments or documentation.
- Every change ships with tests at the appropriate layer (see `docs/plan/04-engineering-standards.md`).
- Nothing runs on the host that can run in a container, except Docker itself, Node, and editor tooling.
- Pin versions: apt packages, pip, npm, Git commits of vendored dependencies, container base digests.
- Secrets never enter the repo. `.env` files are ignored; CI uses GitHub secrets.

## Stack (summary of accepted ADRs)

ROS 2 Jazzy on Ubuntu 24.04; Gazebo Harmonic; ros_gz bridge; one container per vehicle; scenarios are
YAML validated by a Pydantic schema; recordings are MCAP; cockpit is TypeScript + React + Three.js
speaking the Foxglove WebSocket protocol; sim time by default with a real-time-locked mode; Python for
orchestration, C++ for Gazebo plugins, TypeScript for the cockpit; Apache-2.0.

## End of every session

Update `docs/plan/STATUS.md` (task state, PR link, hand-off notes, open questions for the maintainer),
make sure CI is green or the failure is documented, and summarise what the maintainer must do next.
