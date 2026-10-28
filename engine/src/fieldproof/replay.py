"""The field log replay source (task P2-T07): a scenario's ``source: {kind: replay}`` section.

A replay run feeds a vehicle's stack the sensor data of a **field log** (an MCAP file or a
rosbag2 directory) instead of the simulator's: the engine starts no simulator and no bridge,
and a **player** container (:mod:`fieldproof.player`, mounted into the bridge image like the
observer) publishes the clock from the log's timestamps and plays the mapped topics into the
vehicle's namespace with ``ros2 bag play``. The vehicle runs as it would on the real machine,
its actuators open-loop: what it commands is recorded and judged, nothing moves for it. The
log's ground-truth pose (a mocap, an RTK fix turned into a pose, a survey) takes the place of
the simulator's, so every assertion over the ground truth keeps working, and the
``localisation_error`` assertion (:mod:`fieldproof.assertions.localisation`) judges the stack's
own estimate against it.

This module is the engine's side: the schema of the section (the models the scenario embeds),
:func:`inspect_log` (what a log holds, read from the MCAP summary or the rosbag2 metadata
without decoding a message) and :func:`plan_replay`, which resolves the section against the
log and the vehicles into what the player and the observer need. ``docs/reference/replay.md``
is the reference.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, PositiveFloat, field_validator, model_validator

from fieldproof.dsl import Duration, PositiveDuration, ScenarioError, Strict
from fieldproof.manifest import Name

__all__ = [
    "CLOCK_TOPIC",
    "GROUND_TRUTH_TYPES",
    "HIDDEN_PREFIX",
    "LOG_IN_CONTAINER",
    "POSE_TYPES",
    "REPLAY_FILE",
    "STATUS_FILE",
    "GroundTruth",
    "LogInfo",
    "ReplayPlan",
    "ReplaySource",
    "ReplayTf",
    "ReplayVehicle",
    "inspect_log",
    "plan_replay",
]

CLOCK_TOPIC = "/clock"
HIDDEN_PREFIX = "/fieldproof/replay"
"""Where the player publishes what the vehicle must not see as an input: the ground truth, and
the log's TF topic before the frame filter."""
LOG_IN_CONTAINER = "/fieldproof/log"
"""Where the player container sees the log (the file, or the bag directory, mounted read-only)."""
REPLAY_FILE = "replay.json"
"""The player's configuration, written by the runner into the run directory."""
STATUS_FILE = "replay.jsonl"
"""The player's status lines in the run directory (`ready`, `playing`, `done`, `error`)."""
DEFAULT_CLOCK_HZ = 100.0
POSE_TYPES = (
    "geometry_msgs/msg/PoseStamped",
    "geometry_msgs/msg/PoseWithCovarianceStamped",
    "nav_msgs/msg/Odometry",
)
"""The message types a pose (a ground truth, an estimate) is read from."""
GROUND_TRUTH_TYPES = POSE_TYPES
FRAME_PAIR = re.compile(r"^\s*([A-Za-z0-9_/]+)\s*->\s*([A-Za-z0-9_/]+)\s*$")
METADATA_FILE = "metadata.yaml"
MCAP_SUFFIX = ".mcap"
SQLITE_SUFFIX = ".db3"


# --------------------------------------------------------------------------------- the schema
class ReplayTf(Strict):
    """The log's TF topic and which of its transforms reach the vehicle.

    A field log carries the vehicle's whole TF tree, its own localisation (`map -> odom`)
    included; played back as it is, that output would compete with the stack under test. The
    `frames` filter lets only the listed transforms through (the wheel odometry, typically),
    so the stack computes the rest itself.
    """

    topic: str = Field(default="/tf", description="The TF topic of the log.")
    frames: list[str] = Field(
        default_factory=list,
        description="Transforms to let through, each `parent -> child` (empty: every one).",
    )

    @field_validator("topic")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("a log topic is absolute (a leading /)")
        return value

    @field_validator("frames")
    @classmethod
    def _pairs(cls, value: list[str]) -> list[str]:
        for item in value:
            if not FRAME_PAIR.match(item):
                raise ValueError(f"{item!r}: a transform is written `parent -> child`")
        return value

    def pairs(self) -> list[tuple[str, str]]:
        """The `(parent, child)` pairs let through."""
        pairs = []
        for item in self.frames:
            match = FRAME_PAIR.match(item)
            if match is not None:
                pairs.append((match.group(1), match.group(2)))
        return pairs


class GroundTruth(Strict):
    """Where the vehicle's true pose is in the log.

    A `geometry_msgs/msg/PoseStamped`, a `geometry_msgs/msg/PoseWithCovarianceStamped` or a
    `nav_msgs/msg/Odometry` topic (a mocap, an RTK-INS, a survey), in the world frame of the
    scenario: the placements of the assertions and the timeline refer to it.
    """

    topic: str = Field(description="The pose topic of the log.")
    type: str | None = Field(
        default=None,
        description="Its message type (absent: the log's; one of the pose types either way).",
    )

    @field_validator("topic")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("a log topic is absolute (a leading /)")
        return value

    @field_validator("type")
    @classmethod
    def _pose_type(cls, value: str | None) -> str | None:
        if value is not None and value not in POSE_TYPES:
            raise ValueError(f"{value!r} is not a pose type ({', '.join(POSE_TYPES)})")
        return value


class ReplayVehicle(Strict):
    """What the log feeds one vehicle of the scenario."""

    topics: dict[str, str] = Field(
        default_factory=dict,
        description="Log topic to vehicle topic (relative to the vehicle's namespace, or "
        "absolute with a leading /): what the swapped drivers would publish.",
    )
    tf: ReplayTf | None = Field(
        default=None, description="The log's TF topic, filtered to the transforms the stack needs."
    )
    ground_truth: GroundTruth | None = Field(
        default=None,
        description="The vehicle's true pose in the log (the assertions' ground truth).",
    )

    @model_validator(mode="after")
    def _consistent(self) -> ReplayVehicle:
        for log_topic, target in self.topics.items():
            if not log_topic.startswith("/"):
                raise ValueError(f"topics: {log_topic!r}: a log topic is absolute (a leading /)")
            if log_topic == CLOCK_TOPIC:
                raise ValueError(
                    "topics: /clock is never played; the player publishes the clock from the "
                    "log's timestamps"
                )
            if not target or target.endswith("/"):
                raise ValueError(f"topics: {log_topic!r}: {target!r} is not a topic")
        targets = list(self.topics.values())
        if len(set(targets)) != len(targets):
            twice = sorted({t for t in targets if targets.count(t) > 1})
            raise ValueError(f"topics: several log topics feed {', '.join(twice)}")
        if self.tf is not None and self.tf.topic in self.topics:
            raise ValueError(f"tf.topic: {self.tf.topic!r} is also under topics")
        if self.ground_truth is not None and self.ground_truth.topic in self.topics:
            raise ValueError(
                f"ground_truth.topic: {self.ground_truth.topic!r} is also under topics (the "
                "ground truth is not an input of the vehicle)"
            )
        if not self.topics and self.tf is None:
            raise ValueError("the vehicle gets nothing from the log (topics or tf)")
        return self

    def log_topics(self) -> list[str]:
        """Every topic of the log the vehicle uses, in order."""
        topics = list(self.topics)
        if self.tf is not None:
            topics.append(self.tf.topic)
        if self.ground_truth is not None:
            topics.append(self.ground_truth.topic)
        return topics


class ReplaySource(Strict):
    """A field log as the source of the run (task P2-T07, docs/reference/replay.md).

    No simulator runs: the player publishes `/clock` from the log's timestamps, `lead_in_s`
    ahead of the log's first message so the stacks come up on a moving clock, and plays the
    mapped topics at `rate` times real time from `start_s` into the log for `duration_s` (or
    to its end). The timeline starts once every vehicle is ready, somewhere into the log;
    the run ends when the log has been played (and the timeline's entries are done).
    """

    kind: Literal["replay"]
    log: str = Field(
        description="The field log: an MCAP file or a rosbag2 directory, relative to the scenario."
    )
    rate: PositiveFloat = Field(
        default=1.0, description="Playback pace: log seconds per wall-clock second."
    )
    start_s: Duration = Field(default=0.0, description="Skip this far into the log.")
    duration_s: PositiveDuration | None = Field(
        default=None, description="Play this much of the log (absent: to its end)."
    )
    lead_in_s: Duration = Field(
        default=20.0,
        description="Log seconds the clock runs ahead of the log's first message, for the "
        "stacks to come up on a moving clock before the data starts.",
    )
    vehicles: dict[Name, ReplayVehicle] = Field(
        min_length=1, description="What the log feeds each vehicle, by vehicle name."
    )

    @model_validator(mode="after")
    def _one_vehicle_per_log_topic(self) -> ReplaySource:
        users: dict[str, str] = {}
        for name, vehicle in self.vehicles.items():
            for topic in vehicle.log_topics():
                if topic in users and users[topic] != name:
                    raise ValueError(
                        f"vehicles: log topic {topic!r} feeds both {users[topic]} and {name}"
                    )
                users[topic] = name
        return self


# ------------------------------------------------------------------------------- inspection
@dataclass(frozen=True)
class LogInfo:
    """What a log holds, from its summary (nothing decoded)."""

    path: Path
    storage: str
    """`mcap` or `sqlite3`."""
    directory: bool
    """A rosbag2 directory (with `metadata.yaml`) rather than a single MCAP file."""
    start_ns: int
    end_ns: int
    topics: dict[str, str]
    """Topic name to message type."""
    counts: dict[str, int] = field(default_factory=dict)
    """Topic name to message count (where the summary has it)."""

    @property
    def duration_s(self) -> float:
        """The log's span in seconds."""
        return max(self.end_ns - self.start_ns, 0) / 1e9

    @property
    def messages(self) -> int:
        """Messages in the log."""
        return sum(self.counts.values())

    def to_dict(self) -> dict[str, Any]:
        """As data (`fieldproof log info --json`, the plan)."""
        return {
            "path": str(self.path),
            "storage": self.storage,
            "directory": self.directory,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_s": round(self.duration_s, 3),
            "messages": self.messages,
            "topics": {
                name: {"type": kind, "messages": self.counts.get(name)}
                for name, kind in sorted(self.topics.items())
            },
        }


def inspect_log(path: Path | str) -> LogInfo:
    """What the log at `path` holds; a :class:`ScenarioError` if it cannot be read."""
    file = Path(path)
    if file.is_dir():
        return _inspect_bag(file)
    if file.is_file() and file.suffix == MCAP_SUFFIX:
        return _inspect_mcap(file)
    if file.is_file() and file.suffix == SQLITE_SUFFIX:
        raise ScenarioError(
            f"{file}: give the bag directory holding this file (its metadata.yaml names the "
            "topics), not the database"
        )
    if not file.exists():
        raise ScenarioError(f"{file}: no such file or directory")
    raise ScenarioError(f"{file}: not an MCAP file (.mcap) or a rosbag2 directory")


def _inspect_mcap(file: Path) -> LogInfo:
    from mcap.exceptions import McapError  # noqa: PLC0415  (a slow import; the CLI stays quick)
    from mcap.reader import make_reader  # noqa: PLC0415

    try:
        with file.open("rb") as stream:
            summary = make_reader(stream).get_summary()
    except (OSError, McapError, ValueError) as error:
        raise ScenarioError(f"{file}: cannot read the MCAP summary: {error}") from error
    if summary is None or summary.statistics is None:
        raise ScenarioError(
            f"{file}: the MCAP has no summary section (the writer never finished it; "
            "`mcap recover` rewrites one)"
        )
    schemas = {s.id: s.name for s in summary.schemas.values()}
    topics: dict[str, str] = {}
    counts: dict[str, int] = {}
    for channel in summary.channels.values():
        topics[channel.topic] = schemas.get(channel.schema_id, "")
        counts[channel.topic] = counts.get(channel.topic, 0) + int(
            summary.statistics.channel_message_counts.get(channel.id, 0)
        )
    return LogInfo(
        path=file,
        storage="mcap",
        directory=False,
        start_ns=int(summary.statistics.message_start_time),
        end_ns=int(summary.statistics.message_end_time),
        topics=topics,
        counts=counts,
    )


def _inspect_bag(directory: Path) -> LogInfo:
    metadata = directory / METADATA_FILE
    try:
        data = yaml.safe_load(metadata.read_text())
    except OSError as error:
        raise ScenarioError(f"{directory}: not a rosbag2 directory ({error.strerror})") from error
    except yaml.YAMLError as error:
        raise ScenarioError(f"{metadata}: not valid YAML: {error}") from error
    info = (data or {}).get("rosbag2_bagfile_information") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        raise ScenarioError(f"{metadata}: no rosbag2_bagfile_information section")
    try:
        start = int(info["starting_time"]["nanoseconds_since_epoch"])
        duration = int(info["duration"]["nanoseconds"])
        storage = str(info.get("storage_identifier") or "sqlite3")
        topics: dict[str, str] = {}
        counts: dict[str, int] = {}
        for entry in info.get("topics_with_message_count") or []:
            topic = entry["topic_metadata"]
            topics[str(topic["name"])] = str(topic.get("type") or "")
            counts[str(topic["name"])] = int(entry.get("message_count") or 0)
    except (KeyError, TypeError, ValueError) as error:
        raise ScenarioError(f"{metadata}: cannot read the bag's metadata: {error}") from error
    return LogInfo(directory, storage, True, start, start + duration, topics, counts)


# ------------------------------------------------------------------------------- the plan
@dataclass(frozen=True)
class ReplayPlan:
    """The replay source resolved: what the player plays and the observer watches."""

    source: ReplaySource
    log: Path
    """The log on the host (a file or a directory)."""
    info: LogInfo
    t0_ns: int
    """The log time the playback starts at: the log's first message plus `start_s`."""
    play_topics: tuple[str, ...]
    """The log topics played, in order."""
    remaps: dict[str, str]
    """Log topic to the topic it is published on (only the ones that differ)."""
    tf_filters: tuple[dict[str, Any], ...]
    """Per filtered TF topic: `from` (the hidden topic), `to` (the vehicle's), `frames`."""
    qos: dict[str, dict[str, Any]]
    """QoS overrides by log topic (the vehicle's `tf_static` is transient local)."""
    ground_truth: tuple[dict[str, str], ...]
    """Per vehicle with one: `model`, `topic` (hidden, absolute), `type`."""

    @property
    def log_in_container(self) -> str:
        """Where the player sees the log."""
        return f"{LOG_IN_CONTAINER}/{self.log.name}"

    @property
    def played_duration_s(self) -> float:
        """Log seconds the playback covers."""
        available = max(self.info.end_ns - self.t0_ns, 0) / 1e9
        if self.source.duration_s is not None:
            return min(self.source.duration_s, available)
        return available

    def player_config(self, run_dir: str) -> dict[str, Any]:
        """`replay.json`: what the player reads (`run_dir` as the container sees it)."""
        source = self.source
        return {
            "log": self.log_in_container,
            "storage": self.info.storage,
            "rate": source.rate,
            "start_s": source.start_s,
            "duration_s": source.duration_s,
            "lead_in_s": source.lead_in_s,
            "t0_ns": self.t0_ns,
            "end_ns": self.t0_ns + round(self.played_duration_s * 1e9),
            "clock_hz": DEFAULT_CLOCK_HZ,
            "topics": list(self.play_topics),
            "remaps": dict(self.remaps),
            "tf_filters": [dict(f) for f in self.tf_filters],
            "qos": {topic: dict(profile) for topic, profile in self.qos.items()},
            "status_file": f"{run_dir}/{STATUS_FILE}",
            "qos_file": f"{run_dir}/replay-qos.yaml",
        }

    def to_dict(self) -> dict[str, Any]:
        """As data (the plan's JSON, the record)."""
        return {
            "kind": "replay",
            "log": str(self.log),
            "storage": self.info.storage,
            "rate": self.source.rate,
            "start_s": self.source.start_s,
            "duration_s": round(self.played_duration_s, 3),
            "lead_in_s": self.source.lead_in_s,
            "t0_ns": self.t0_ns,
            "log_duration_s": round(self.info.duration_s, 3),
            "topics": list(self.play_topics),
            "remaps": dict(self.remaps),
            "tf_filters": [dict(f) for f in self.tf_filters],
            "ground_truth": [dict(g) for g in self.ground_truth],
        }


def hidden_topic(vehicle: str, what: str) -> str:
    """The hidden topic the player publishes a vehicle's `what` on."""
    return f"{HIDDEN_PREFIX}/{vehicle}/{what}"


def plan_replay(  # noqa: PLR0912, PLR0915
    source: ReplaySource,
    scenario_dir: Path,
    vehicles: Mapping[str, Callable[[str], str]],
    *,
    tf_static_topics: Mapping[str, str] | None = None,
    inspect: Callable[[Path], LogInfo] = inspect_log,
) -> ReplayPlan:
    """Resolve the section against the log and the vehicles.

    `vehicles` maps each vehicle's name to its `topic()` resolver (a relative topic under its
    namespace); `tf_static_topics` names, per vehicle, its static TF topic (absolute), which is
    played transient local when the log feeds it. A :class:`ScenarioError` names what does not
    resolve: a missing log, a mapped topic the log has not, a ground truth of another type.
    """
    log = (scenario_dir / source.log).resolve()
    if not log.exists():
        raise ScenarioError(
            f"source.log: {log} does not exist (a field log, or the synthetic one: "
            "`make replay-log`)"
        )
    info = inspect(log)
    for name in source.vehicles:
        if name not in vehicles:
            raise ScenarioError(f"source.vehicles: {name!r} is not a vehicle of the scenario")
    for name in vehicles:
        if name not in source.vehicles:
            raise ScenarioError(
                f"source.vehicles: {name!r} gets nothing from the log (map its topics, or "
                "leave the vehicle out of the scenario)"
            )
    known = ", ".join(sorted(info.topics)) or "none"
    play_topics: list[str] = []
    remaps: dict[str, str] = {}
    tf_filters: list[dict[str, Any]] = []
    qos: dict[str, dict[str, Any]] = {}
    ground_truth: list[dict[str, str]] = []
    static = dict(tf_static_topics or {})
    for name, mapping in source.vehicles.items():
        where = f"source.vehicles.{name}"
        resolve = vehicles[name]
        for log_topic, target in mapping.topics.items():
            if log_topic not in info.topics:
                raise ScenarioError(
                    f"{where}.topics: the log has no topic {log_topic!r} (its topics: {known})"
                )
            published = resolve(target)
            play_topics.append(log_topic)
            if published != log_topic:
                remaps[log_topic] = published
            if published == static.get(name):
                qos[log_topic] = {
                    "history": "keep_last",
                    "depth": 100,
                    "reliability": "reliable",
                    "durability": "transient_local",
                }
        if mapping.tf is not None:
            tf = mapping.tf
            if tf.topic not in info.topics:
                raise ScenarioError(
                    f"{where}.tf.topic: the log has no topic {tf.topic!r} (its topics: {known})"
                )
            vehicle_tf = resolve("tf")
            play_topics.append(tf.topic)
            if tf.frames:
                hidden = hidden_topic(name, "tf")
                remaps[tf.topic] = hidden
                tf_filters.append({"from": hidden, "to": vehicle_tf, "frames": tf.pairs()})
            elif vehicle_tf != tf.topic:
                remaps[tf.topic] = vehicle_tf
        if mapping.ground_truth is not None:
            truth = mapping.ground_truth
            if truth.topic not in info.topics:
                raise ScenarioError(
                    f"{where}.ground_truth.topic: the log has no topic {truth.topic!r} "
                    f"(its topics: {known})"
                )
            logged = info.topics[truth.topic]
            kind = truth.type or logged
            if logged and truth.type is not None and logged != truth.type:
                raise ScenarioError(
                    f"{where}.ground_truth.type: the log's {truth.topic} is a {logged}, "
                    f"not a {truth.type}"
                )
            if kind not in POSE_TYPES:
                raise ScenarioError(
                    f"{where}.ground_truth.topic: {truth.topic} is a {
                        kind or 'message of an unknown type'
                    }, not a pose ({', '.join(POSE_TYPES)})"
                )
            hidden = hidden_topic(name, "ground_truth")
            play_topics.append(truth.topic)
            remaps[truth.topic] = hidden
            ground_truth.append({"model": name, "topic": hidden, "type": kind})
    outputs = list(remaps.values()) + [t for t in play_topics if t not in remaps]
    if len(set(outputs)) != len(outputs):
        twice = sorted({t for t in outputs if outputs.count(t) > 1})
        raise ScenarioError(f"source.vehicles: several log topics are published as {twice}")
    if source.start_s >= info.duration_s and info.duration_s > 0:
        raise ScenarioError(
            f"source.start_s: {source.start_s:g} s is past the end of the log "
            f"({info.duration_s:.1f} s)"
        )
    t0_ns = info.start_ns + round(source.start_s * 1e9)
    return ReplayPlan(
        source=source,
        log=log,
        info=info,
        t0_ns=t0_ns,
        play_topics=tuple(play_topics),
        remaps=remaps,
        tf_filters=tuple(tf_filters),
        qos=qos,
        ground_truth=tuple(ground_truth),
    )
