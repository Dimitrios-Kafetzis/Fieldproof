"""A synthetic field log of the reference UGV (task P2-T07): the stand-in for a real one.

The maintainer supplied no field log for the replay source (STATUS.md, open question 2), so
``fieldproof log synth`` writes one: an MCAP file in the shape a ``ros2 bag record`` of the
reference UGV's drivers would produce, deterministic for a seed. The vehicle starts at rest
in a 20 m by 12 m hall with a few crates, drives a rectangle (8 m, a left turn, 4 m, a turn,
8 m, a turn, 4 m, a turn) at 0.5 m/s and 0.5 rad/s, and rests again until the log ends:

- ``/scan`` (``sensor_msgs/msg/LaserScan``, 10 Hz, 720 beams over 360 degrees, 0.1 to 25 m,
  1 cm of Gaussian range noise, ``inf`` past the range) ray-cast from the lidar on the
  vehicle's mast against the hall's walls and crates;
- ``/odom`` (``nav_msgs/msg/Odometry``, 20 Hz) and ``/tf`` (``odom -> base_link``, 20 Hz):
  the wheel odometry, integrated with a 2 % scale error, a yaw-rate bias and noise, so the
  odometry frame drifts the way wheels do;
- ``/tf_static`` (once): ``base_link`` to ``base_footprint``, ``lidar_link``, ``imu_link`` and
  ``gps_link`` as the reference UGV's description places them;
- ``/ground_truth/pose`` (``geometry_msgs/msg/PoseStamped``, 10 Hz, frame ``world``): the
  true pose, as a motion-capture system or an RTK survey would give it, in the world frame the
  scenario's placements refer to (``start`` puts the hall's origin anywhere in that frame).

Timestamps start at an epoch of 2023-11-14, as a real recording's would. The IMU, the camera
and the GNSS receiver are left out: the example stack does not read them, and a log is only as
complete as the drivers that were recorded.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_SECONDS", "DEFAULT_SEED", "Hall", "SynthSummary", "write_synthetic_log"]

DEFAULT_SECONDS = 120.0
DEFAULT_SEED = 7
EPOCH_S = 1_700_000_000
"""The log's first timestamp: 2023-11-14T22:13:20Z, a plausible field date."""
SCAN_HZ = 10.0
ODOM_HZ = 20.0
TRUTH_HZ = 10.0
STEP_S = 0.01
"""The integration step of the motion."""
BEAMS = 720
RANGE_MIN_M = 0.10
RANGE_MAX_M = 25.0
RANGE_NOISE_M = 0.01
SPEED_MPS = 0.5
TURN_RADPS = 0.5
REST_S = 10.0
"""How long the vehicle rests before the drive."""
ODOM_SCALE = 1.02
"""The wheel odometry's scale error (the wheels a little smaller than the driver thinks)."""
ODOM_YAW_BIAS_RADPS = 0.003
ODOM_YAW_NOISE_RADPS = 0.002
ODOM_SPEED_NOISE_MPS = 0.01
LIDAR_XYZ = (0.20, 0.0, 0.185)
"""The lidar on the mast: 0.20 m ahead of base_link, chassis top plus 0.025 m."""
STATIC_FRAMES: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("base_footprint", (0.0, 0.0, -0.11)),
    ("lidar_link", LIDAR_XYZ),
    ("imu_link", (0.0, 0.0, 0.10)),
    ("gps_link", (-0.25, 0.0, 0.17)),
)
TOPIC_SCAN = "/scan"
TOPIC_ODOM = "/odom"
TOPIC_TF = "/tf"
TOPIC_TF_STATIC = "/tf_static"
TOPIC_TRUTH = "/ground_truth/pose"

# The message definitions as `ros2 bag record` writes them into an MCAP (the dependencies
# concatenated, the way Foxglove and the mcap-ros2-support encoder read them).
TIME_DEF = "int32 sec\nuint32 nanosec\n"
HEADER_DEF = "builtin_interfaces/Time stamp\nstring frame_id\n"
_SEP = "=" * 80
_HEADER_DEPS = (
    f"{_SEP}\nMSG: std_msgs/Header\n{HEADER_DEF}{_SEP}\nMSG: builtin_interfaces/Time\n{TIME_DEF}"
)
_GEOMETRY_DEPS = (
    f"{_SEP}\nMSG: geometry_msgs/Point\nfloat64 x\nfloat64 y\nfloat64 z\n"
    f"{_SEP}\nMSG: geometry_msgs/Quaternion\nfloat64 x 0\nfloat64 y 0\nfloat64 z 0\nfloat64 w 1\n"
)
LASER_SCAN_DEF = (
    "std_msgs/Header header\n"
    "float32 angle_min\nfloat32 angle_max\nfloat32 angle_increment\nfloat32 time_increment\n"
    "float32 scan_time\nfloat32 range_min\nfloat32 range_max\n"
    "float32[] ranges\nfloat32[] intensities\n" + _HEADER_DEPS
)
ODOMETRY_DEF = (
    "std_msgs/Header header\nstring child_frame_id\n"
    "geometry_msgs/PoseWithCovariance pose\ngeometry_msgs/TwistWithCovariance twist\n"
    + _HEADER_DEPS
    + f"{_SEP}\nMSG: geometry_msgs/PoseWithCovariance\ngeometry_msgs/Pose pose\n"
    "float64[36] covariance\n"
    f"{_SEP}\nMSG: geometry_msgs/Pose\ngeometry_msgs/Point position\n"
    "geometry_msgs/Quaternion orientation\n"
    + _GEOMETRY_DEPS
    + f"{_SEP}\nMSG: geometry_msgs/TwistWithCovariance\ngeometry_msgs/Twist twist\n"
    "float64[36] covariance\n"
    f"{_SEP}\nMSG: geometry_msgs/Twist\ngeometry_msgs/Vector3 linear\n"
    "geometry_msgs/Vector3 angular\n"
    f"{_SEP}\nMSG: geometry_msgs/Vector3\nfloat64 x\nfloat64 y\nfloat64 z\n"
)
TF_MESSAGE_DEF = (
    "geometry_msgs/TransformStamped[] transforms\n"
    f"{_SEP}\nMSG: geometry_msgs/TransformStamped\nstd_msgs/Header header\n"
    "string child_frame_id\ngeometry_msgs/Transform transform\n"
    + _HEADER_DEPS
    + f"{_SEP}\nMSG: geometry_msgs/Transform\ngeometry_msgs/Vector3 translation\n"
    "geometry_msgs/Quaternion rotation\n"
    f"{_SEP}\nMSG: geometry_msgs/Vector3\nfloat64 x\nfloat64 y\nfloat64 z\n"
    f"{_SEP}\nMSG: geometry_msgs/Quaternion\nfloat64 x 0\nfloat64 y 0\nfloat64 z 0\nfloat64 w 1\n"
)
POSE_STAMPED_DEF = (
    "std_msgs/Header header\ngeometry_msgs/Pose pose\n"
    + _HEADER_DEPS
    + f"{_SEP}\nMSG: geometry_msgs/Pose\ngeometry_msgs/Point position\n"
    "geometry_msgs/Quaternion orientation\n" + _GEOMETRY_DEPS
)
SCHEMAS = {
    TOPIC_SCAN: ("sensor_msgs/msg/LaserScan", LASER_SCAN_DEF),
    TOPIC_ODOM: ("nav_msgs/msg/Odometry", ODOMETRY_DEF),
    TOPIC_TF: ("tf2_msgs/msg/TFMessage", TF_MESSAGE_DEF),
    TOPIC_TF_STATIC: ("tf2_msgs/msg/TFMessage", TF_MESSAGE_DEF),
    TOPIC_TRUTH: ("geometry_msgs/msg/PoseStamped", POSE_STAMPED_DEF),
}
Segment = tuple[float, float, float, float]


# ---------------------------------------------------------------------------------- the hall
@dataclass(frozen=True)
class Hall:
    """The hall the log was "recorded" in: walls and crates as segments, in the hall's frame.

    The vehicle starts at the origin facing +x; the hall spans -10 to 10 m in x and -5 to 7 m
    in y, with four crates off the driven rectangle.
    """

    x_min: float = -10.0
    x_max: float = 10.0
    y_min: float = -5.0
    y_max: float = 7.0
    crates: tuple[tuple[float, float, float, float], ...] = (
        (3.5, -2.5, 4.5, -1.5),
        (3.4, 5.6, 4.6, 6.4),
        (-6.5, 1.5, -5.5, 3.0),
        (-8.0, -4.0, -7.0, -3.0),
    )
    """Axis-aligned crates: `x_min, y_min, x_max, y_max`."""

    def segments(self) -> list[Segment]:
        """Every wall and crate edge as `x1, y1, x2, y2`."""
        boxes = [(self.x_min, self.y_min, self.x_max, self.y_max), *self.crates]
        segments: list[Segment] = []
        for x1, y1, x2, y2 in boxes:
            segments += [(x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)]
        return segments


def ray_distance(  # noqa: PLR0917
    ox: float, oy: float, dx: float, dy: float, segments: list[Segment], limit: float
) -> float:
    """The distance along the ray `(ox, oy) + t (dx, dy)` to the nearest segment, or `inf`."""
    best = limit
    for x1, y1, x2, y2 in segments:
        ex, ey = x2 - x1, y2 - y1
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-12:
            continue
        wx, wy = x1 - ox, y1 - oy
        t = (wx * ey - wy * ex) / denominator
        if t <= 0.0 or t >= best:
            continue
        u = (wx * dy - wy * dx) / denominator
        if 0.0 <= u <= 1.0:
            best = t
    return best if best < limit else math.inf


# ------------------------------------------------------------------------------- the motion
def commands(seconds: float) -> Iterator[tuple[float, float, float]]:
    """`(until_s, v, w)` legs of the drive: rest, the rectangle, rest to the end."""
    legs = [(REST_S, 0.0, 0.0)]
    t = REST_S
    for length in (8.0, 4.0, 8.0, 4.0):
        t += length / SPEED_MPS
        legs.append((t, SPEED_MPS, 0.0))
        t += (math.pi / 2) / TURN_RADPS
        legs.append((t, 0.0, TURN_RADPS))
    legs.append((max(seconds, t), 0.0, 0.0))
    yield from legs


@dataclass
class _State:
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0

    def advance(self, v: float, w: float, dt: float) -> None:
        self.x += v * math.cos(self.yaw) * dt
        self.y += v * math.sin(self.yaw) * dt
        self.yaw = math.remainder(self.yaw + w * dt, math.tau)


def _quaternion(yaw: float) -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}


def _stamp(t_s: float) -> dict[str, int]:
    nanoseconds = EPOCH_S * 1_000_000_000 + round(t_s * 1e9)
    return {"sec": nanoseconds // 1_000_000_000, "nanosec": nanoseconds % 1_000_000_000}


def _nanoseconds(t_s: float) -> int:
    return EPOCH_S * 1_000_000_000 + round(t_s * 1e9)


@dataclass(frozen=True)
class SynthSummary:
    """What was written."""

    path: Path
    seconds: float
    seed: int
    messages: dict[str, int]
    start: tuple[float, float, float]
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        return {
            "path": str(self.path),
            "seconds": self.seconds,
            "seed": self.seed,
            "start": {"x": self.start[0], "y": self.start[1], "yaw": self.start[2]},
            "messages": dict(self.messages),
            "bytes": self.bytes,
        }


def write_synthetic_log(  # noqa: PLR0915
    path: Path | str,
    *,
    seconds: float = DEFAULT_SECONDS,
    seed: int = DEFAULT_SEED,
    start: tuple[float, float, float] = (0.0, 0.0, 0.0),
    hall: Hall | None = None,
) -> SynthSummary:
    """Write the synthetic log to `path` (an `.mcap` file) and say what went in.

    `start` places the hall's origin (where the vehicle starts, facing the hall's +x) in the
    world frame the ground truth is expressed in: `x`, `y`, `yaw`.
    """
    from mcap_ros2.writer import Writer  # noqa: PLC0415  (a slow import; the CLI stays quick)

    if seconds <= 0:
        raise ValueError("seconds must be positive")
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)  # noqa: S311  (noise, not security)
    scene = hall or Hall()
    segments = scene.segments()
    truth, odom = _State(), _State()
    legs = list(commands(seconds))
    sx, sy, syaw = start
    cos_s, sin_s = math.cos(syaw), math.sin(syaw)
    counts: dict[str, int] = dict.fromkeys(SCHEMAS, 0)
    next_due = {TOPIC_SCAN: 0.0, TOPIC_ODOM: 0.0, TOPIC_TRUTH: 0.0}
    periods = {TOPIC_SCAN: 1.0 / SCAN_HZ, TOPIC_ODOM: 1.0 / ODOM_HZ, TOPIC_TRUTH: 1.0 / TRUTH_HZ}
    angle_increment = math.tau / BEAMS
    angles = [-math.pi + i * angle_increment for i in range(BEAMS)]
    cosines = [math.cos(a) for a in angles]
    sines = [math.sin(a) for a in angles]
    sequence = 0

    def world_of(state: _State) -> tuple[float, float, float]:
        return (
            sx + cos_s * state.x - sin_s * state.y,
            sy + sin_s * state.x + cos_s * state.y,
            math.remainder(syaw + state.yaw, math.tau),
        )

    with file.open("wb") as stream, Writer(stream) as writer:
        schemas = {
            topic: writer.register_msgdef(name, text) for topic, (name, text) in SCHEMAS.items()
        }

        def write(topic: str, t_s: float, message: dict[str, Any]) -> None:
            nonlocal sequence
            sequence += 1
            stamp = _nanoseconds(t_s)
            writer.write_message(topic, schemas[topic], message, stamp, stamp, sequence)
            counts[topic] += 1

        write(
            TOPIC_TF_STATIC,
            0.0,
            {
                "transforms": [
                    {
                        "header": {"stamp": _stamp(0.0), "frame_id": "base_link"},
                        "child_frame_id": child,
                        "transform": {
                            "translation": {"x": x, "y": y, "z": z},
                            "rotation": _quaternion(0.0),
                        },
                    }
                    for child, (x, y, z) in STATIC_FRAMES
                ]
            },
        )
        steps = round(seconds / STEP_S)
        leg = 0
        for step in range(steps + 1):
            t = step * STEP_S
            while leg < len(legs) - 1 and t >= legs[leg][0]:
                leg += 1
            _until, v, w = legs[leg]
            if step > 0:
                truth.advance(v, w, STEP_S)
                odom.advance(
                    v * ODOM_SCALE,
                    w + ODOM_YAW_BIAS_RADPS * (1.0 if v or w else 0.0),
                    STEP_S,
                )
            if t >= next_due[TOPIC_TRUTH] - 1e-9:
                next_due[TOPIC_TRUTH] += periods[TOPIC_TRUTH]
                wx, wy, wyaw = world_of(truth)
                write(
                    TOPIC_TRUTH,
                    t,
                    {
                        "header": {"stamp": _stamp(t), "frame_id": "world"},
                        "pose": {
                            "position": {"x": wx, "y": wy, "z": 0.0},
                            "orientation": _quaternion(wyaw),
                        },
                    },
                )
            if t >= next_due[TOPIC_ODOM] - 1e-9:
                next_due[TOPIC_ODOM] += periods[TOPIC_ODOM]
                v_meas = v * ODOM_SCALE + (rng.gauss(0.0, ODOM_SPEED_NOISE_MPS) if v else 0.0)
                w_meas = w + (rng.gauss(0.0, ODOM_YAW_NOISE_RADPS) if v or w else 0.0)
                pose = {
                    "position": {"x": odom.x, "y": odom.y, "z": 0.0},
                    "orientation": _quaternion(odom.yaw),
                }
                header = {"stamp": _stamp(t), "frame_id": "odom"}
                write(
                    TOPIC_ODOM,
                    t,
                    {
                        "header": header,
                        "child_frame_id": "base_link",
                        "pose": {"pose": pose, "covariance": _covariance(0.01, 0.001)},
                        "twist": {
                            "twist": {
                                "linear": {"x": v_meas, "y": 0.0, "z": 0.0},
                                "angular": {"x": 0.0, "y": 0.0, "z": w_meas},
                            },
                            "covariance": _covariance(0.001, 0.0001),
                        },
                    },
                )
                write(
                    TOPIC_TF,
                    t,
                    {
                        "transforms": [
                            {
                                "header": header,
                                "child_frame_id": "base_link",
                                "transform": {
                                    "translation": pose["position"],
                                    "rotation": pose["orientation"],
                                },
                            }
                        ]
                    },
                )
            if t >= next_due[TOPIC_SCAN] - 1e-9:
                next_due[TOPIC_SCAN] += periods[TOPIC_SCAN]
                cos_y, sin_y = math.cos(truth.yaw), math.sin(truth.yaw)
                lx = truth.x + cos_y * LIDAR_XYZ[0]
                ly = truth.y + sin_y * LIDAR_XYZ[0]
                ranges = []
                for i in range(BEAMS):
                    dx = cos_y * cosines[i] - sin_y * sines[i]
                    dy = sin_y * cosines[i] + cos_y * sines[i]
                    distance = ray_distance(lx, ly, dx, dy, segments, RANGE_MAX_M)
                    if math.isinf(distance):
                        ranges.append(math.inf)
                        continue
                    measured = distance + rng.gauss(0.0, RANGE_NOISE_M)
                    ranges.append(measured if measured >= RANGE_MIN_M else RANGE_MIN_M)
                write(
                    TOPIC_SCAN,
                    t,
                    {
                        "header": {"stamp": _stamp(t), "frame_id": "lidar_link"},
                        "angle_min": -math.pi,
                        "angle_max": math.pi - angle_increment,
                        "angle_increment": angle_increment,
                        "time_increment": 0.0,
                        "scan_time": 1.0 / SCAN_HZ,
                        "range_min": RANGE_MIN_M,
                        "range_max": RANGE_MAX_M,
                        "ranges": ranges,
                        "intensities": [],
                    },
                )
    return SynthSummary(file, seconds, seed, counts, start, file.stat().st_size)


def _covariance(position: float, orientation: float) -> list[float]:
    """A diagonal 6x6 covariance (x, y, z, roll, pitch, yaw)."""
    values = [0.0] * 36
    for index in range(3):
        values[index * 7] = position
        values[(index + 3) * 7] = orientation
    return values
