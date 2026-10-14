# 05 — Working model: one task, one session

## Roles

- **Maintainer** (Dimitris): owns decisions, GitHub, hardware, approvals for sudo and destructive
  actions, milestone sign-off, and supplies real-world inputs (robot repositories, field logs, flight
  controllers).
- **Working session**: executes exactly one backlog task to its definition of done, following the
  [session guide](06-session-guide.md), the standards and the ADRs. It reports what it could not finish
  instead of narrowing scope silently. All commits are authored by the maintainer; no co-author trailers
  or tool attribution appear anywhere in the repository.

## Session protocol

1. **Open.** Maintainer pastes the task's *session opener* line from the backlog. The session reads
   `STATUS.md`, the task entry, the referenced ADRs and the hand-off notes of the previous session.
2. **Plan.** The session states in a few lines what it will build and any assumption it is making on
   open inputs. If an input is blocking, it says so immediately and does all non-blocked work first.
3. **Branch.** `task/<id>-<slug>` from current `main`.
4. **Build.** Small commits in Conventional Commits style. Tests alongside code. Pre-commit clean.
5. **Verify.** Run the task's verification exactly as written in the backlog. Paste the real output
   in the PR body.
6. **Document.** Update docs, reference pages, ADRs if needed.
7. **PR.** Open the PR with the template filled, CI green. Do not merge; the maintainer merges
   (or explicitly delegates merging for the session).
8. **Hand-off.** Update `STATUS.md`: task state, PR link, what was verified, what is left, open
   questions, and the recommended next task. End with a short message to the maintainer listing what
   they need to do (merge, approve, provide an input, run the demo).

## Task states (STATUS.md)

`todo` → `in-progress` → `in-review` (PR open) → `done` (merged, CI green) · `blocked (reason)`

## Multi-session tasks (×2)

Session 1 ends with a PR that is mergeable on its own (partial but coherent, behind a flag or as a
library without wiring) and hand-off notes naming the exact remaining deliverables. Session 2 opens
with `…, session 2 of 2`.

## Parallel sessions

Tasks with no dependency edge between them may run in parallel sessions (for example P3-T02 and
P2-T05). Each session uses its own branch. Conflicts are resolved by the later PR rebasing.

## Unplanned work

Bugs or small improvements discovered mid-task that are outside the task's definition of done are
filed as GitHub issues, not fixed in the task branch, unless they block the task. Issues are labelled
`phase:N` and triaged by the maintainer into the backlog.

## Changing the plan

A change to scope, architecture or stack is a new ADR (branch `adr/<nnnn>-<slug>`), reviewed in its
own PR, then reflected in the backlog and roadmap in the same PR.

## What the maintainer provides, by phase

| When | Needed | Why |
| --- | --- | --- |
| Before P0-T01 | GitHub owner (user or org), visibility, final name | Create the remote and set protections |
| P0-T02 | Sudo approvals during the session | Docker, binfmt, nvm install |
| P0-T05 | Confirm GHCR for images, enable GitHub Pages | Publishing |
| P1-T01 (optional) | A real robot URDF | Use the real description instead of the reference |
| P1-T07, P2-T08, P3-T07, P4-T05, P5-T05, P6-T06, P7-T06 | Run the demo locally and sign off | Milestone gates |
| P2-T07, P6-T03, P6-T06 (optional) | Field MCAP or ROS bags | Replay, calibration, divergence |
| P2-T08 (optional) | A real vehicle repository | Worked onboarding example |
| P4-T01, P4-T02 | Preferred PX4 and ArduPilot releases | Pinning |
| P6-T01 | Target board type or image | ARM64 emulation |
| P6-T04 | Pixhawk-class flight controller, USB cable, optional companion board | HIL |
| P7-T06 | Release approval | Tag v1.0.0 |

Items marked optional have a synthetic fallback; the real input raises fidelity but does not block.

## Cadence suggestion

Sessions are self-contained, so cadence is the maintainer's choice. At one session per working day
the roadmap is roughly three months of calendar time; at two per week, roughly seven months. Milestone
demos are natural review points to re-prioritise P3, P4 and P5.
