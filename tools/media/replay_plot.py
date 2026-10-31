#!/usr/bin/env python3
r"""Plot a replay run's localisation: SLAM's pose against the log's ground truth (the M2 video).

    uv run --locked --project engine --with mcap-ros2-support --with matplotlib \\
        python tools/media/replay_plot.py runs/<run_id> --output replay.png

Reads the run's MCAP recording: `/pose` (slam_toolbox's estimate, the map frame) and the
ground-truth poses the observer publishes on `/fieldproof/metrics` (from the log's
`/ground_truth/pose`, the same frame in the synthetic log), and the `localised` verdict from
`assertions.jsonl`. Draws the two paths and the position error over the timeline, at the
video's size (1280 x 720) so the image drops into the clip sequence as a card.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib  # type: ignore[import-not-found]

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # type: ignore[import-not-found]
from mcap_ros2.reader import read_ros2_messages


def load(
    run_dir: Path, vehicle: str
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """`(t, x, y)` of the SLAM estimates and of the ground truth, in log time."""
    recording = next((run_dir / "recording").glob("*.mcap"))
    estimates: list[tuple[float, float, float]] = []
    truth: list[tuple[float, float, float]] = []
    for message in read_ros2_messages(recording, topics=["/pose", "/fieldproof/metrics"]):
        if message.channel.topic == "/pose":
            stamp = message.ros_msg.header.stamp
            position = message.ros_msg.pose.pose.position
            estimates.append((stamp.sec + stamp.nanosec / 1e9, position.x, position.y))
        else:
            data = json.loads(message.ros_msg.data)
            pose = (data.get("poses") or {}).get(vehicle)
            if pose and data.get("sim") is not None:
                truth.append((float(data["sim"]), float(pose["x"]), float(pose["y"])))
    return estimates, truth


def nearest(truth: list[tuple[float, float, float]], t: float) -> tuple[float, float] | None:
    """The ground-truth position at `t`, interpolated between the samples around it.

    The observer samples the truth about once a second and the vehicle moves 0.5 m/s, so
    the nearest sample alone would be off by up to a quarter of a metre.
    """
    after = next((i for i, p in enumerate(truth) if p[0] >= t), None)
    if after is None or after == 0:
        return None
    before = truth[after - 1]
    following = truth[after]
    span = following[0] - before[0]
    if span <= 0 or span > 2.0:
        return None
    weight = (t - before[0]) / span
    return (
        before[1] + weight * (following[1] - before[1]),
        before[2] + weight * (following[2] - before[2]),
    )


def verdict(run_dir: Path) -> str:
    """The `localised` assertion's verdict line from the run's assertions."""
    for line in (run_dir / "assertions.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record.get("name") == "localised":
            return f"{record['status'].upper()}: {record['reason']}"
    return ""


def main() -> int:
    """Draw the card; 1 when the recording holds no estimates or no ground truth."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--vehicle", default="ugv1")
    parser.add_argument("--output", type=Path, default=Path("replay.png"))
    args = parser.parse_args()
    estimates, truth = load(args.run_dir, args.vehicle)
    if not estimates or not truth:
        print("no estimates or no ground truth in the recording")
        return 1
    t0 = truth[0][0]
    errors = []
    for t, x, y in estimates:
        reference = nearest(truth, t)
        if reference is not None:
            errors.append((t - t0, math.hypot(x - reference[0], y - reference[1])))

    plt.rcParams.update({"font.size": 14, "font.family": "DejaVu Sans"})
    fig, (paths, error) = plt.subplots(
        1, 2, figsize=(12.8, 7.2), dpi=100, gridspec_kw={"width_ratios": [1.1, 1]}
    )
    fig.patch.set_facecolor("#14191f")
    for axis in (paths, error):
        axis.set_facecolor("#1d242c")
        for spine in axis.spines.values():
            spine.set_color("#5a6673")
        axis.tick_params(colors="#c9d1d9")
        axis.xaxis.label.set_color("#c9d1d9")
        axis.yaxis.label.set_color("#c9d1d9")
        axis.title.set_color("#f0f3f6")
        axis.grid(color="#2e3842", linewidth=0.8)
    paths.plot(
        [p[1] for p in truth],
        [p[2] for p in truth],
        color="#8b949e",
        linewidth=3,
        label="ground truth (the log)",
    )
    paths.plot(
        [p[1] for p in estimates],
        [p[2] for p in estimates],
        color="#58d68d",
        linewidth=2,
        marker="o",
        markersize=4,
        label="slam_toolbox /pose",
    )
    paths.set_aspect("equal")
    paths.set_xlabel("x [m]")
    paths.set_ylabel("y [m]")
    paths.set_title("The replayed drive: where SLAM thinks the vehicle is")
    paths.legend(facecolor="#1d242c", edgecolor="#5a6673", labelcolor="#f0f3f6", loc="lower right")
    error.plot(
        [e[0] for e in errors],
        [e[1] for e in errors],
        color="#58d68d",
        linewidth=2,
        marker="o",
        markersize=4,
    )
    error.axhline(0.5, color="#e5c07b", linewidth=1.5, linestyle="--")
    error.text(
        0.02,
        0.5,
        "limit 0.5 m",
        color="#e5c07b",
        transform=error.get_yaxis_transform(),
        va="bottom",
    )
    error.set_ylim(0, max(0.6, max((e[1] for e in errors), default=0) * 1.2))
    error.set_xlabel("log time [s]")
    error.set_ylabel("position error [m]")
    error.set_title(f"{len(errors)} estimates judged against the ground truth")
    fig.suptitle(
        "Field log replay: the SLAM stack localises on a recorded drive",
        color="#f0f3f6",
        fontsize=20,
        y=0.98,
    )
    fig.text(0.5, 0.02, verdict(args.run_dir), color="#58d68d", ha="center", fontsize=14)
    fig.tight_layout(rect=(0, 0.05, 1, 0.94))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, facecolor=fig.get_facecolor())
    print(f"wrote {args.output} ({len(estimates)} estimates, {len(truth)} ground-truth poses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
