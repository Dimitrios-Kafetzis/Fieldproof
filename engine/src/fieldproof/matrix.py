"""The run matrix (task P2-T06): parameter sweeps, seeds, parallel runs, retries.

A matrix file (``apiVersion: fieldproof.dev/v0alpha1``, ``kind: Matrix``) names a scenario and
how to vary it: ``parameters`` (a list of values per scenario path, the cases are their
Cartesian product), ``seeds`` (one more axis), and ``cases`` (explicit sets of overrides). Every
case is the scenario with its overrides applied (:mod:`fieldproof.overrides`), resolved
up-front so a bad case fails the matrix before anything runs, and run as its own
``fieldproof run`` process with ``--set`` for each override: a crash of one run is one failed
case, Ctrl-C reaches every running case, and each case has the run directory, the record, the
reports and the logs any run has, under the matrix directory.

Scheduling is resource-aware: the cases' demands are estimated from their plans (the vehicles'
budgets plus allowances for the simulator, the bridges and the recorder) against the host's
cores and memory less a reserve for the engine, and ``parallel: auto`` starts a case whenever
it fits next to the running ones (one always runs); an explicit ``parallel: N`` runs up to N at
once. Every running case gets a ROS domain of its own from a pool derived from the matrix id,
so parallel cases never share one (the Gazebo partition is the run's own already); every case
records its run in the matrix's database (``--database``), not in one of its own.

A case whose attempt fails, times out or errors (not one interrupted, not a usage error) is run
again up to ``retries`` times; a case that passes after a failed attempt is *flaky*: green in
the counts, flagged in the record, the JUnit properties, the report and the database
(``fail_on_flaky`` turns the flag into a failure). The matrix directory holds ``matrix.yaml``
(the file as given), ``matrix.json`` (the record, rewritten as cases finish), ``matrix.log``
(one JSON object per line), ``junit.xml`` (one test suite per case, the single document CI
consumes), ``report.md`` and ``report.html``, and every attempt's run directory.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal

import yaml
from pydantic import (
    Field,
    PositiveFloat,
    PositiveInt,
    PrivateAttr,
    ValidationError,
    field_validator,
)

from fieldproof import __version__
from fieldproof.backend import HostInfo
from fieldproof.config import Config
from fieldproof.html import render_matrix_html
from fieldproof.manifest import MEMORY_PATTERN, Name, Strict, format_errors, memory_bytes
from fieldproof.overrides import OverrideError, format_value, load_scenario_with, parse_path
from fieldproof.report import (
    REPORT_JSON,
    ReportError,
    build_report,
    junit_document,
    junit_suite,
    load_record,
)
from fieldproof.rundb import DatabaseError, MatrixContext, RunDatabase
from fieldproof.runner import (
    DOMAIN_RANGE,
    EXIT_CODES,
    Plan,
    RunLog,
    Status,
    auto_domain_id,
    make_run_id,
    plan,
    utc_now,
)
from fieldproof.scenario import SEED_MAX, Metadata, Scenario, ScenarioError

API_VERSION = "fieldproof.dev/v0alpha1"
KIND = "Matrix"
MATRIX_COPY = "matrix.yaml"
MATRIX_RECORD = "matrix.json"
MATRIX_LOG = "matrix.log"
MATRIX_JUNIT = "junit.xml"
MATRIX_MD = "report.md"
MATRIX_HTML = "report.html"
CONSOLE_LOG = "console.log"
SEED_PATH = "seed"
GIB = 1024**3
MIB = 1024**2
RETRIABLE = (Status.FAILED, Status.TIMEOUT, Status.ERROR)
USAGE_EXIT = 2
DEFAULT_COMMAND = (sys.executable, "-m", "fieldproof")
COMMAND_ENV = "FIELDPROOF_COMMAND"
"""The command the matrix runs cases with, split like a shell line (default: this engine)."""


class MatrixError(ValueError):
    """The matrix file cannot be read, validated or expanded."""


# ------------------------------------------------------------------------------------ demand
@dataclass(frozen=True)
class Demand:
    """What a run asks of the host: cores and memory."""

    cpus: float
    memory_bytes: int

    def __add__(self, other: Demand) -> Demand:
        return Demand(self.cpus + other.cpus, self.memory_bytes + other.memory_bytes)

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        return {"cpus": round(self.cpus, 2), "memory_bytes": self.memory_bytes}


NOTHING = Demand(0.0, 0)
SIM_DEMAND = Demand(2.0, 2 * GIB)
"""The simulator (physics and the sensors' rendering)."""
BRIDGE_DEMAND = Demand(0.5, 512 * MIB)
"""One bridge container."""
RECORDER_DEMAND = Demand(0.5, 512 * MIB)
"""The recorder and its observer."""
LINKS_DEMAND = Demand(0.5, 256 * MIB)
"""The link holders and the ground station of a scenario with radio links."""
VEHICLE_DEFAULT = Demand(2.0, 2 * GIB)
"""A vehicle without a budget."""
ENGINE_RESERVE = Demand(1.0, 1 * GIB)
"""What the host keeps for the engine and the daemon."""


def estimate_demand(resolved: Plan, override: CaseResources | None = None) -> Demand:
    """The demand of a run of `resolved`: the budgets plus the stack's allowances."""
    demand = SIM_DEMAND + RECORDER_DEMAND
    for vehicle in resolved.vehicles:
        resources = vehicle.resources
        cpus = float(resources.cpus) if resources.cpus else VEHICLE_DEFAULT.cpus
        memory = (
            memory_bytes(resources.memory) if resources.memory else VEHICLE_DEFAULT.memory_bytes
        )
        demand = demand + Demand(cpus, memory) + BRIDGE_DEMAND
    if resolved.links is not None:
        demand = demand + LINKS_DEMAND
    if override is not None:
        demand = Demand(
            float(override.cpus) if override.cpus else demand.cpus,
            memory_bytes(override.memory) if override.memory else demand.memory_bytes,
        )
    return demand


@dataclass(frozen=True)
class Capacity:
    """What the host can give to runs: its cores and memory less the engine's reserve."""

    cpus: float
    memory_bytes: float
    host: HostInfo
    reserve: Demand = ENGINE_RESERVE

    @classmethod
    def of(cls, host: HostInfo, reserve: Demand = ENGINE_RESERVE) -> Capacity:
        """The capacity of `host` (an unknown figure counts as unlimited)."""
        cpus = max(0.0, host.cpus - reserve.cpus) if host.cpus else math.inf
        memory = (
            max(0.0, host.memory_bytes - reserve.memory_bytes) if host.memory_bytes else math.inf
        )
        return cls(cpus, memory, host, reserve)

    def to_dict(self) -> dict[str, Any]:
        """As data (an unlimited figure as null)."""
        return {
            "cpus": None if math.isinf(self.cpus) else round(self.cpus, 2),
            "memory_bytes": None if math.isinf(self.memory_bytes) else int(self.memory_bytes),
            "host": {"cpus": self.host.cpus, "memory_bytes": self.host.memory_bytes},
            "reserve": self.reserve.to_dict(),
        }


def host_info() -> HostInfo:
    """The host's cores and memory from the operating system (no daemon needed)."""
    memory = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        memory = 0
    return HostInfo(os.cpu_count() or 0, memory)


class Scheduler:
    """Which case may start next to the running ones."""

    def __init__(self, capacity: Capacity, parallel: int | Literal["auto"]) -> None:
        self.capacity = capacity
        self.parallel = parallel

    def can_start(self, demand: Demand, running: Sequence[Demand]) -> bool:
        """Whether a case of `demand` fits next to `running` (one always runs)."""
        if not running:
            return True
        if self.parallel != "auto":
            return len(running) < self.parallel
        used = sum(running, NOTHING)
        return (
            used.cpus + demand.cpus <= self.capacity.cpus
            and used.memory_bytes + demand.memory_bytes <= self.capacity.memory_bytes
        )

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        return {"parallel": self.parallel, "capacity": self.capacity.to_dict()}


# ----------------------------------------------------------------------------------- the file
class CaseResources(Strict):
    """What a case asks of the host, instead of the estimate from its plan."""

    cpus: PositiveFloat | None = Field(default=None, description="Cores per run.")
    memory: str | None = Field(
        default=None, pattern=MEMORY_PATTERN, description="Memory per run (`4g`)."
    )


class ExplicitCase(Strict):
    """A case written out: a name and its overrides."""

    name: Name = Field(description="Case name: lower case, digits and underscores.")
    set: dict[str, Any] = Field(
        default_factory=dict, description="Overrides by scenario path (overrides.md)."
    )

    @field_validator("set")
    @classmethod
    def _paths(cls, value: dict[str, Any]) -> dict[str, Any]:
        for path in value:
            try:
                parse_path(path)
            except OverrideError as error:
                raise ValueError(str(error)) from error
        return value


class MatrixOutputs(Strict):
    """Where the matrix writes."""

    directory: str | None = Field(
        default=None,
        description="Where the matrix directory goes, relative to the workspace (default: the "
        "workspace's runs directory).",
    )


class Matrix(Strict):
    """A matrix file, `apiVersion: fieldproof.dev/v0alpha1`, `kind: Matrix`."""

    api_version: Literal["fieldproof.dev/v0alpha1"] = Field(alias="apiVersion")
    kind: Literal["Matrix"]
    metadata: Metadata
    scenario: str = Field(description="The scenario file, relative to the matrix file.")
    seeds: list[int] = Field(
        default_factory=list,
        description="Seeds to run every case with (one more axis of the matrix).",
    )
    parameters: dict[str, list[Any]] = Field(
        default_factory=dict,
        description="Values per scenario path; the cases are their Cartesian product.",
    )
    cases: list[ExplicitCase] = Field(
        default_factory=list,
        description="Explicit cases, added to the product (or alone, without parameters).",
    )
    retries: int = Field(default=1, ge=0, description="How many times a failed case is run again.")
    parallel: PositiveInt | Literal["auto"] = Field(
        default="auto",
        description="Cases at once: a number, or `auto` for as many as the host's cores and "
        "memory fit (by the cases' estimated demand).",
    )
    resources: CaseResources | None = Field(
        default=None, description="The demand of a run, instead of the estimate from its plan."
    )
    fail_on_flaky: bool = Field(
        default=False, description="A case that passed only on a retry fails the matrix."
    )
    outputs: MatrixOutputs = MatrixOutputs()

    _path: Path | None = PrivateAttr(default=None)
    _digest: str = PrivateAttr(default="")
    _text: str = PrivateAttr(default="")

    @field_validator("seeds")
    @classmethod
    def _seeds(cls, value: list[int]) -> list[int]:
        for seed in value:
            if not 0 <= seed <= SEED_MAX:
                raise ValueError(f"seed {seed} is not between 0 and {SEED_MAX}")
        if len(set(value)) != len(value):
            raise ValueError("seeds must be unique")
        return value

    @field_validator("parameters")
    @classmethod
    def _parameters(cls, value: dict[str, list[Any]]) -> dict[str, list[Any]]:
        for path, values in value.items():
            try:
                parse_path(path)
            except OverrideError as error:
                raise ValueError(str(error)) from error
            if not values:
                raise ValueError(f"{path}: at least one value")
        return value

    @property
    def path(self) -> Path | None:
        """Where the matrix was loaded from."""
        return self._path

    @property
    def directory(self) -> Path:
        """The matrix file's directory, which `scenario` resolves against."""
        if self._path is None:
            raise MatrixError("the matrix was not loaded from a file")
        return self._path.parent

    @property
    def name(self) -> str:
        """`metadata.name`."""
        return self.metadata.name

    @property
    def digest(self) -> str:
        """Short SHA-256 of the file's text (the first part of a matrix id)."""
        return self._digest

    @property
    def text(self) -> str:
        """The file's text."""
        return self._text

    def scenario_path(self) -> Path:
        """The scenario file."""
        return (self.directory / self.scenario).resolve()

    def expand(self) -> list[Case]:
        """The cases: the parameters' product (or the explicit cases), each with every seed."""
        return expand_cases(self)


def validate_matrix_data(data: Any, path: Path | None = None, text: str = "") -> Matrix:
    """Validate a matrix document."""
    prefix = f"{path}: " if path else ""
    if not isinstance(data, dict):
        raise MatrixError(f"{prefix}a matrix is a YAML mapping")
    try:
        matrix = Matrix.model_validate(data)
    except ValidationError as error:
        raise MatrixError(prefix + format_errors(error)) from error
    if SEED_PATH in matrix.parameters and matrix.seeds:
        raise MatrixError(f"{prefix}parameters.seed and seeds are two ways to say the same")
    matrix._path = path
    matrix._digest = hashlib.sha256(text.encode()).hexdigest()[:8] if text else ""
    matrix._text = text
    return matrix


def load_matrix(path: Path | str) -> Matrix:
    """Read and validate the matrix at `path`."""
    file = Path(path).resolve()
    try:
        text = file.read_text()
    except OSError as error:
        raise MatrixError(f"{file}: {error.strerror}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise MatrixError(f"{file}: not valid YAML: {error}") from error
    return validate_matrix_data(data, file, text)


# ---------------------------------------------------------------------------------- the cases
@dataclass(frozen=True)
class Case:
    """One point of the matrix."""

    id: str
    name: str
    overrides: dict[str, Any]
    seed: int | None
    index: int

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        return {
            "id": self.id,
            "name": self.name,
            "overrides": dict(self.overrides),
            "seed": self.seed,
            "index": self.index,
        }


def case_name(overrides: Mapping[str, Any]) -> str:
    """`path=value, path=value` (values as `--set` takes them), `default` for none."""
    return ", ".join(f"{k}={format_value(v)}" for k, v in overrides.items()) or "default"


def expand_cases(spec: Matrix) -> list[Case]:
    """The cases of a matrix file, in the order they run."""
    bases: list[tuple[str, dict[str, Any]]] = []
    if spec.parameters:
        axes = [[(path, value) for value in values] for path, values in spec.parameters.items()]
        for combination in itertools.product(*axes):
            overrides = dict(combination)
            bases.append((case_name(overrides), overrides))
    for explicit in spec.cases:
        bases.append((explicit.name, dict(explicit.set)))
    if not bases:
        bases.append(("default", {}))
    seeds: list[int | None] = list(spec.seeds) or [None]
    expanded: list[tuple[str, dict[str, Any], int | None]] = []
    for name, overrides in bases:
        for seed in seeds:
            with_seed = dict(overrides)
            if seed is not None:
                with_seed[SEED_PATH] = seed
            label = (
                name
                if seed is None
                else (f"{name}, seed={seed}" if name != "default" else f"seed={seed}")
            )
            expanded.append((label, with_seed, seed))
    width = max(2, len(str(len(expanded))))
    return [
        Case(f"c{index + 1:0{width}d}", name, overrides, seed, index)
        for index, (name, overrides, seed) in enumerate(expanded)
    ]


@dataclass(frozen=True)
class ResolvedCase:
    """A case with its scenario resolved and its demand estimated."""

    case: Case
    scenario: Scenario
    plan: Plan
    demand: Demand

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        return {
            **self.case.to_dict(),
            "scenario_digest": self.scenario.digest,
            "demand": self.demand.to_dict(),
        }


def resolve_cases(
    spec: Matrix, config: Config, cases: Iterable[Case], *, render: str | None = None
) -> list[ResolvedCase]:
    """Resolve every case against the workspace (a `MatrixError` names the first bad one)."""
    scenario_path = spec.scenario_path()
    resolved: list[ResolvedCase] = []
    for case in cases:
        try:
            scenario = load_scenario_with(scenario_path, case.overrides)
            resolved_plan = plan(
                scenario, config, render=render, now=datetime.now(UTC), token=f"m{case.id}"
            )
        except (ScenarioError, ValueError) as error:
            raise MatrixError(f"case {case.id} ({case.name}): {error}") from error
        resolved.append(
            ResolvedCase(
                case, scenario, resolved_plan, estimate_demand(resolved_plan, spec.resources)
            )
        )
    return resolved


def make_matrix_id(spec: Matrix, now: datetime, token: str) -> str:
    """`<matrix-hash>-<timestamp>-<token>`, the shape of a run id."""
    return f"{spec.digest or 'nohash'}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{token}"


# ------------------------------------------------------------------------------ the attempts
@dataclass
class Attempt:
    """One `fieldproof run` of a case."""

    number: int
    run_id: str
    run_dir: Path
    domain_id: int
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    status: str = "pending"
    verdict: str | None = None
    reason: str | None = None
    duration_s: float | None = None
    kpis: dict[str, Any] = field(default_factory=dict)
    assertion_counts: dict[str, int] = field(default_factory=dict)
    report: dict[str, Any] | None = field(default=None, repr=False)
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    console: IO[bytes] | None = field(default=None, repr=False)
    started_clock: float = 0.0
    interrupted_at: float | None = None
    killed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """As data (without the report and the process)."""
        return {
            "number": self.number,
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "domain_id": self.domain_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "duration_s": self.duration_s,
            "assertion_counts": dict(self.assertion_counts),
        }


@dataclass
class CaseRun:
    """A case through its attempts."""

    resolved: ResolvedCase
    attempts: list[Attempt] = field(default_factory=list)
    status: str = "pending"
    flaky: bool = False

    @property
    def case(self) -> Case:
        """The case."""
        return self.resolved.case

    @property
    def demand(self) -> Demand:
        """The case's demand."""
        return self.resolved.demand

    @property
    def final(self) -> Attempt | None:
        """The last attempt, whose outcome is the case's."""
        return self.attempts[-1] if self.attempts else None

    @property
    def running(self) -> Attempt | None:
        """The attempt in progress, if any."""
        last = self.final
        return last if last is not None and last.process is not None else None

    def to_dict(self) -> dict[str, Any]:
        """As data."""
        final = self.final
        return {
            **self.resolved.to_dict(),
            "status": self.status,
            "verdict": final.verdict if final is not None else None,
            "flaky": self.flaky,
            "run_id": final.run_id if final is not None else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "kpis": dict(final.kpis) if final is not None else {},
        }


Launcher = Callable[[Sequence[str], IO[bytes]], "subprocess.Popen[bytes]"]


def launch(argv: Sequence[str], console: IO[bytes]) -> subprocess.Popen[bytes]:
    """Start a run in its own session, its output to `console`."""
    return subprocess.Popen(  # noqa: S603  (the engine's own command line)
        list(argv),
        stdout=console,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def command_from_env(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The command cases run with: :data:`COMMAND_ENV` split like a shell line, or this engine."""
    import shlex  # noqa: PLC0415  (one caller)

    text = (os.environ if environment is None else environment).get(COMMAND_ENV, "").strip()
    return tuple(shlex.split(text)) if text else DEFAULT_COMMAND


# ------------------------------------------------------------------------------- the matrix
class MatrixRun:
    """One execution of a matrix: the cases through the scheduler, retried, recorded."""

    def __init__(
        self,
        spec: Matrix,
        cases: Sequence[ResolvedCase],
        *,
        matrix_id: str,
        matrix_dir: Path,
        capacity: Capacity,
        log: RunLog,
        parallel: int | Literal["auto"] | None = None,
        retries: int | None = None,
        fail_on_flaky: bool | None = None,
        command: Sequence[str] = DEFAULT_COMMAND,
        run_args: Sequence[str] = (),
        database: Path | None = None,
        launcher: Launcher = launch,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_s: float = 0.5,
        grace_s: float = 600.0,
    ) -> None:
        self.spec = spec
        self.cases = [CaseRun(c) for c in cases]
        self.matrix_id = matrix_id
        self.matrix_dir = matrix_dir
        self.log = log
        self.parallel: int | Literal["auto"] = spec.parallel if parallel is None else parallel
        self.retries = spec.retries if retries is None else retries
        self.fail_on_flaky = spec.fail_on_flaky if fail_on_flaky is None else fail_on_flaky
        self.scheduler = Scheduler(capacity, self.parallel)
        self.command = tuple(command)
        self.run_args = tuple(run_args)
        self.database = database
        self.launcher = launcher
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.poll_s = poll_s
        self.grace_s = grace_s
        self.token = matrix_id.rsplit("-", 1)[-1]
        self.interrupted = False
        self.max_concurrent = 0
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.duration_s: float | None = None
        self._started_clock = 0.0
        self.status: str | None = None
        self.reason: str | None = None

    # ------------------------------------------------------------------------------ control
    def interrupt(self) -> None:
        """Stop launching cases and interrupt the running ones (Ctrl-C, SIGTERM)."""
        if not self.interrupted:
            self.interrupted = True
            self.log.event("matrix.interrupt", "interrupting the running cases", level="warning")

    def execute(self) -> dict[str, Any]:
        """Run every case; return the record."""
        self.matrix_dir.mkdir(parents=True, exist_ok=True)
        if self.spec.text:
            (self.matrix_dir / MATRIX_COPY).write_text(self.spec.text)
        self.started_at = utc_now()
        self._started_clock = self.clock()
        self.log.event(
            "matrix.start",
            f"{self.spec.name} ({self.matrix_id}): {len(self.cases)} case(s), parallel "
            f"{self.parallel}, {self.retries} retr{'y' if self.retries == 1 else 'ies'}, in "
            f"{self.matrix_dir}",
            cases=len(self.cases),
            parallel=self.parallel,
            retries=self.retries,
            capacity=self.scheduler.capacity.to_dict(),
        )
        self._write()
        queue: deque[CaseRun] = deque(self.cases)
        running: list[CaseRun] = []
        try:
            while queue or running:
                progressed = self._reap(running, queue)
                while queue and not self.interrupted:
                    candidate = queue[0]
                    if not self.scheduler.can_start(candidate.demand, [r.demand for r in running]):
                        break
                    queue.popleft()
                    self._launch(candidate, running)
                    progressed = True
                if self.interrupted and queue:
                    for case_run in queue:
                        case_run.status = "skipped"
                        self.log.event(
                            "case.skipped",
                            f"{case_run.case.id} ({case_run.case.name}): the matrix was "
                            "interrupted",
                            case=case_run.case.id,
                            level="warning",
                        )
                    queue.clear()
                    progressed = True
                if self.interrupted and running:
                    self._signal(running)
                if progressed:
                    self._write()
                elif running:
                    self.sleep(self.poll_s)
        finally:
            self._finish()
        return self.record()

    # ---------------------------------------------------------------------------- launching
    def _domain(self, running: Sequence[CaseRun]) -> int:
        used = {r.final.domain_id for r in running if r.final is not None}
        low, high = DOMAIN_RANGE
        span = high - low + 1
        base = auto_domain_id(self.matrix_id)
        for offset in range(span):
            candidate = low + (base - low + offset) % span
            if candidate not in used:
                return candidate
        return base

    def _launch(self, case_run: CaseRun, running: list[CaseRun]) -> None:
        case = case_run.case
        number = len(case_run.attempts) + 1
        run_id = make_run_id(
            case_run.resolved.scenario, self.now(), f"{self.token}{case.id}a{number}"
        )
        run_dir = self.matrix_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        domain = self._domain(running)
        overrides = dict(case.overrides)
        if case_run.resolved.scenario.outputs.directory is not None:
            overrides["outputs.directory"] = None
        argv = [
            *self.command,
            "run",
            str(self.spec.scenario_path()),
            "--runs-dir",
            str(self.matrix_dir),
            "--run-id",
            run_id,
            "--domain-id",
            str(domain),
            "--quiet",
            *(["--database", str(self.database)] if self.database is not None else []),
            *self.run_args,
        ]
        for path, value in overrides.items():
            argv += ["--set", f"{path}={format_value(value)}"]
        console = (run_dir / CONSOLE_LOG).open("ab")
        attempt = Attempt(number, run_id, run_dir, domain, started_at=utc_now())
        attempt.started_clock = self.clock()
        attempt.console = console
        try:
            attempt.process = self.launcher(argv, console)
        except OSError as error:
            console.close()
            attempt.console = None
            attempt.status = str(Status.ERROR)
            attempt.verdict = "ERROR"
            attempt.reason = f"cannot start {argv[0]}: {error}"
            attempt.finished_at = utc_now()
            attempt.exit_code = None
            case_run.attempts.append(attempt)
            self._settle(case_run, attempt, None)
            return
        attempt.status = "running"
        case_run.attempts.append(attempt)
        case_run.status = "running"
        running.append(case_run)
        self.max_concurrent = max(self.max_concurrent, len(running))
        self.log.event(
            "case.start",
            f"{case.id} ({case.name}) attempt {number}: {run_id}, ROS_DOMAIN_ID {domain}, "
            f"{case_run.demand.cpus:g} CPU(s) and {case_run.demand.memory_bytes / GIB:.1f} GiB "
            f"estimated; {len(running)} running",
            case=case.id,
            attempt=number,
            run_id=run_id,
            domain_id=domain,
            running=len(running),
            argv=argv,
        )

    # ------------------------------------------------------------------------------ reaping
    def _reap(self, running: list[CaseRun], queue: deque[CaseRun]) -> bool:
        progressed = False
        for case_run in list(running):
            attempt = case_run.running
            if attempt is None or attempt.process is None:
                running.remove(case_run)
                progressed = True
                continue
            code = attempt.process.poll()
            if code is None:
                continue
            running.remove(case_run)
            progressed = True
            self._collect(attempt, code)
            self._settle(case_run, attempt, queue)
        return progressed

    def _collect(self, attempt: Attempt, code: int) -> None:
        """Read what the attempt's run left behind."""
        attempt.process = None
        if attempt.console is not None:
            attempt.console.close()
            attempt.console = None
        attempt.exit_code = code
        attempt.finished_at = utc_now()
        attempt.duration_s = round(self.clock() - attempt.started_clock, 3)
        record: dict[str, Any] | None = None
        report: dict[str, Any] | None = None
        try:
            record = load_record(attempt.run_dir)
        except ReportError:
            record = None
        if record is not None:
            report_file = attempt.run_dir / REPORT_JSON
            try:
                data = json.loads(report_file.read_text()) if report_file.is_file() else None
                report = data if isinstance(data, dict) and "run_id" in data else None
            except (OSError, json.JSONDecodeError):
                report = None
            if report is None:
                report = build_report(record, attempt.run_dir)
        if report is not None and record is not None and record.get("status"):
            attempt.report = report
            attempt.status = str(record["status"])
            attempt.verdict = str(report.get("verdict") or attempt.status.upper())
            attempt.reason = record.get("reason")
            attempt.kpis = dict(report.get("kpis") or {})
            attempt.assertion_counts = dict(report.get("assertion_counts") or {})
            if report.get("duration_s") is not None:
                attempt.duration_s = float(report["duration_s"])
            return
        attempt.status = str(Status.INTERRUPTED if self.interrupted else Status.ERROR)
        attempt.verdict = attempt.status.upper()
        tail = self._console_tail(attempt.run_dir / CONSOLE_LOG)
        attempt.reason = (
            f"fieldproof run exited {code} without a verdict"
            + (" (usage error)" if code == USAGE_EXIT else "")
            + (f": {tail}" if tail else "")
        )

    @staticmethod
    def _console_tail(path: Path, lines: int = 3) -> str:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return ""
        kept = [line.strip() for line in text.splitlines() if line.strip()]
        return " | ".join(kept[-lines:])

    def _settle(self, case_run: CaseRun, attempt: Attempt, queue: deque[CaseRun] | None) -> None:
        """Decide the case after an attempt: retry it, or close it."""
        case = case_run.case
        self._record_attempt(case_run, attempt)
        retriable = (
            attempt.status in RETRIABLE
            and attempt.exit_code != USAGE_EXIT
            and not self.interrupted
            and attempt.number <= self.retries
        )
        if retriable and queue is not None:
            self.log.event(
                "case.retry",
                f"{case.id} ({case.name}) attempt {attempt.number} {attempt.verdict}: "
                f"{attempt.reason or attempt.status}; retrying "
                f"({attempt.number} of {self.retries} "
                f"retr{'y' if self.retries == 1 else 'ies'} used)",
                case=case.id,
                attempt=attempt.number,
                run_id=attempt.run_id,
                status=attempt.status,
                level="warning",
            )
            case_run.status = "retrying"
            queue.appendleft(case_run)
            return
        case_run.status = attempt.status
        case_run.flaky = attempt.status == Status.PASSED and attempt.number > 1
        level = "info" if attempt.status == Status.PASSED else "error"
        self.log.event(
            "case.end",
            f"{case.id} ({case.name}) {attempt.verdict}"
            + (" (flaky)" if case_run.flaky else "")
            + f" after {attempt.number} attempt(s): "
            + (attempt.reason or "the timeline completed and every assertion passed"),
            case=case.id,
            attempt=attempt.number,
            run_id=attempt.run_id,
            status=attempt.status,
            flaky=case_run.flaky,
            duration_s=attempt.duration_s,
            level="warning" if case_run.flaky else level,
        )

    def _signal(self, running: Sequence[CaseRun]) -> None:
        for case_run in running:
            attempt = case_run.running
            if attempt is None or attempt.process is None:
                continue
            if attempt.interrupted_at is None:
                attempt.interrupted_at = self.clock()
                with contextlib.suppress(OSError):
                    attempt.process.send_signal(signal.SIGINT)
            elif not attempt.killed and self.clock() - attempt.interrupted_at > self.grace_s:
                attempt.killed = True
                self.log.event(
                    "case.kill",
                    f"{case_run.case.id}: no exit {self.grace_s:g} s after the interrupt, killed",
                    case=case_run.case.id,
                    level="error",
                )
                with contextlib.suppress(OSError):
                    attempt.process.kill()

    # ------------------------------------------------------------------------------ records
    def _record_attempt(self, case_run: CaseRun, attempt: Attempt) -> None:
        if self.database is None or attempt.report is None:
            return
        context = MatrixContext(
            self.matrix_id, case_run.case.id, case_run.case.name, attempt.number, case_run.flaky
        )
        try:
            with RunDatabase(self.database) as database:
                database.record_run(attempt.report, context)
        except DatabaseError as error:
            self.log.event("database.error", str(error), level="warning")

    def counts(self) -> dict[str, int]:
        """How the cases went."""
        statuses = [c.status for c in self.cases]
        return {
            "cases": len(self.cases),
            "passed": statuses.count(str(Status.PASSED)),
            "failed": sum(
                1 for s in statuses if s in (Status.FAILED, Status.TIMEOUT, Status.ERROR)
            ),
            "flaky": sum(1 for c in self.cases if c.flaky),
            "interrupted": statuses.count(str(Status.INTERRUPTED)),
            "skipped": statuses.count("skipped"),
            "attempts": sum(len(c.attempts) for c in self.cases),
        }

    def _finish(self) -> None:
        counts = self.counts()
        if self.interrupted or counts["interrupted"] or counts["skipped"]:
            self.status = str(Status.INTERRUPTED)
        elif counts["failed"] or (self.fail_on_flaky and counts["flaky"]):
            self.status = str(Status.FAILED)
        else:
            self.status = str(Status.PASSED)
        self.reason = (
            f"{counts['passed']} of {counts['cases']} case(s) passed"
            + (f", {counts['failed']} failed" if counts["failed"] else "")
            + (
                f", {counts['flaky']} flaky"
                + (
                    " (fail_on_flaky)"
                    if self.fail_on_flaky and self.status == Status.FAILED
                    else ""
                )
                if counts["flaky"]
                else ""
            )
            + (f", {counts['interrupted']} interrupted" if counts["interrupted"] else "")
            + (f", {counts['skipped']} skipped" if counts["skipped"] else "")
            + f"; {counts['attempts']} attempt(s)"
        )
        self.finished_at = utc_now()
        self.duration_s = round(self.clock() - self._started_clock, 3)
        record = self.record()
        try:
            (self.matrix_dir / MATRIX_JUNIT).write_text(render_matrix_junit(record, self.reports()))
            (self.matrix_dir / MATRIX_MD).write_text(render_matrix_markdown(record))
            (self.matrix_dir / MATRIX_HTML).write_text(render_matrix_html(record))
        except OSError as error:
            self.log.event("report.error", str(error), level="warning")
        self._write()
        if self.database is not None:
            try:
                with RunDatabase(self.database) as database:
                    database.record_matrix(record)
                    for case_run in self.cases:
                        for attempt in case_run.attempts:
                            if attempt.report is not None:
                                database.record_run(
                                    attempt.report,
                                    MatrixContext(
                                        self.matrix_id,
                                        case_run.case.id,
                                        case_run.case.name,
                                        attempt.number,
                                        case_run.flaky,
                                    ),
                                )
            except DatabaseError as error:
                self.log.event("database.error", str(error), level="warning")
            else:
                self.log.event("database.recorded", f"{self.matrix_id} in {self.database}")
        level = "info" if self.status == Status.PASSED else "error"
        self.log.event(
            "matrix.end",
            f"{self.verdict()}: {self.reason}",
            status=self.status,
            matrix_dir=str(self.matrix_dir),
            level=level,
        )
        self.log.close()

    def verdict(self) -> str:
        """`PASS`, `FAIL` or `INTERRUPTED`."""
        return {"passed": "PASS", "failed": "FAIL"}.get(self.status or "", "INTERRUPTED")

    def exit_code(self) -> int:
        """The exit code of the matrix: 0 passed, 1 failed, 130 interrupted."""
        return EXIT_CODES[Status(self.status or Status.ERROR)]

    def reports(self) -> dict[str, dict[str, Any]]:
        """The attempts' reports by run id."""
        return {a.run_id: a.report for c in self.cases for a in c.attempts if a.report is not None}

    def record(self) -> dict[str, Any]:
        """The record (`matrix.json`)."""
        if self.duration_s is not None:
            duration: float | None = self.duration_s
        elif self.started_at is not None:
            duration = round(self.clock() - self._started_clock, 3)
        else:
            duration = None
        return {
            "matrix_id": self.matrix_id,
            "matrix_dir": str(self.matrix_dir),
            "engine_version": __version__,
            "matrix": {
                "name": self.spec.name,
                "description": self.spec.metadata.description,
                "tags": list(self.spec.metadata.tags),
                "path": str(self.spec.path) if self.spec.path else None,
                "digest": self.spec.digest,
            },
            "scenario": {
                "name": self.cases[0].resolved.scenario.name if self.cases else None,
                "path": str(self.spec.scenario_path()) if self.spec.path else self.spec.scenario,
            },
            "status": self.status,
            "verdict": self.verdict() if self.status else None,
            "exit_code": self.exit_code() if self.status else None,
            "reason": self.reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": duration,
            "schedule": {
                **self.scheduler.to_dict(),
                "max_concurrent": self.max_concurrent,
                "retries": self.retries,
                "fail_on_flaky": self.fail_on_flaky,
                "command": list(self.command),
                "run_args": list(self.run_args),
            },
            "counts": self.counts(),
            "cases": [c.to_dict() for c in self.cases],
            "outputs": {
                "record": MATRIX_RECORD,
                "log": MATRIX_LOG,
                "junit": MATRIX_JUNIT,
                "markdown": MATRIX_MD,
                "html": MATRIX_HTML,
                "database": str(self.database) if self.database else None,
            },
        }

    def _write(self) -> None:
        self.matrix_dir.mkdir(parents=True, exist_ok=True)
        (self.matrix_dir / MATRIX_RECORD).write_text(json.dumps(self.record(), indent=2) + "\n")


# ----------------------------------------------------------------------------------- outputs
def render_matrix_junit(record: dict[str, Any], reports: Mapping[str, dict[str, Any]]) -> str:
    """One JUnit document for the matrix: a test suite per case, from its final attempt."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415  (one caller)

    matrix = record.get("matrix") or {}
    scenario_name = (record.get("scenario") or {}).get("name") or matrix.get("name") or "matrix"
    suites = []
    for case in record.get("cases") or []:
        attempts = case.get("attempts") or []
        final = attempts[-1] if attempts else None
        suite_name = f"{scenario_name}[{case.get('name')}]"
        properties = {
            "matrix_id": record.get("matrix_id"),
            "case": case.get("id"),
            "case_name": case.get("name"),
            "parameters": json.dumps(case.get("overrides") or {}, sort_keys=True),
            "case_seed": case.get("seed"),
            "attempt": final.get("number") if final else None,
            "attempts": len(attempts),
            "flaky": str(bool(case.get("flaky"))).lower(),
            "retried_run_ids": ", ".join(str(a.get("run_id")) for a in attempts[:-1]) or None,
        }
        report = reports.get(str(final.get("run_id"))) if final else None
        if report is not None:
            suites.append(junit_suite(report, suite_name=suite_name, properties=properties))
            continue
        suite = ET.Element("testsuite", name=suite_name)
        props = ET.SubElement(suite, "properties")
        for key, value in properties.items():
            if value is not None:
                ET.SubElement(props, "property", name=key, value=str(value))
        case_element = ET.SubElement(suite, "testcase", classname=suite_name, name="run")
        status = str(case.get("status") or "skipped")
        reason = str((final or {}).get("reason") or status)
        if status == "skipped":
            ET.SubElement(case_element, "skipped", message=reason)
            skipped, errors = 1, 0
        else:
            ET.SubElement(case_element, "error", message=reason, type=status)
            skipped, errors = 0, 1
        suite.set("tests", "1")
        suite.set("failures", "0")
        suite.set("errors", str(errors))
        suite.set("skipped", str(skipped))
        suite.set("time", f"{float((final or {}).get('duration_s') or 0.0):.3f}")
        suites.append(suite)
    return junit_document(
        suites, name=f"fieldproof matrix {matrix.get('name') or record['matrix_id']}"
    )


def _md_cell(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|")


def render_matrix_markdown(record: dict[str, Any]) -> str:
    """The matrix's record as Markdown (the job summary, `report.md`)."""
    matrix = record.get("matrix") or {}
    counts = record.get("counts") or {}
    schedule = record.get("schedule") or {}
    name = matrix.get("name") or record["matrix_id"]
    lines = [
        f"# Fieldproof matrix report: {name}",
        "",
        f"**{record.get('verdict') or '-'}** (status `{record.get('status')}`, exit code "
        f"{record.get('exit_code')})" + (f": {record['reason']}" if record.get("reason") else ""),
        "",
        f"- Matrix `{record['matrix_id']}`, engine {record.get('engine_version') or '-'}",
        f"- Sweep `{matrix.get('path') or '-'}` (sha `{matrix.get('digest') or '-'}`), scenario "
        f"`{(record.get('scenario') or {}).get('path') or '-'}`",
        f"- Started {record.get('started_at') or '-'}, finished "
        f"{record.get('finished_at') or '-'}; "
        f"{record.get('duration_s') if record.get('duration_s') is not None else '-'} s wall clock",
        f"- {counts.get('cases', 0)} case(s): {counts.get('passed', 0)} passed, "
        f"{counts.get('failed', 0)} failed, {counts.get('flaky', 0)} flaky, "
        f"{counts.get('skipped', 0)} skipped; {counts.get('attempts', 0)} attempt(s), "
        f"{schedule.get('retries', 0)} retr{'y' if schedule.get('retries', 0) == 1 else 'ies'} "
        f"per case, parallel {schedule.get('parallel')} (at most "
        f"{schedule.get('max_concurrent', 0)} at once)",
        "",
        "## Cases",
        "",
        "| Case | Name | Seed | Verdict | Attempts | Duration | Run | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    kpi_names: set[str] = set()
    for case in record.get("cases") or []:
        attempts = case.get("attempts") or []
        final = attempts[-1] if attempts else {}
        verdict = final.get("verdict") or str(case.get("status", "-")).upper()
        kpi_names.update((case.get("kpis") or {}).keys())
        duration = final.get("duration_s")
        lines.append(
            f"| `{case.get('id')}` | {_md_cell(case.get('name'))} | "
            f"{case.get('seed') if case.get('seed') is not None else '-'} | "
            f"{verdict}{' (flaky)' if case.get('flaky') else ''} | {len(attempts)} | "
            f"{f'{duration:.1f} s' if isinstance(duration, int | float) else '-'} | "
            f"`{final.get('run_id') or '-'}` | {_md_cell(final.get('reason') or '-')} |"
        )
    lines += ["", "## KPIs", ""]
    if kpi_names:
        names = sorted(kpi_names)
        lines.append("| Case | " + " | ".join(f"`{n}`" for n in names) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in names) + " |")
        for case in record.get("cases") or []:
            kpis = case.get("kpis") or {}
            cells = []
            for kpi_name in names:
                kpi = kpis.get(kpi_name) or {}
                value = kpi.get("value")
                unit = kpi.get("unit") or ""
                if isinstance(value, float):
                    cells.append(f"{value:.3f} {unit}".strip())
                elif value is None:
                    cells.append("-")
                else:
                    cells.append(f"{value} {unit}".strip())
            lines.append(f"| `{case.get('id')}` | " + " | ".join(cells) + " |")
    else:
        lines.append("No KPI was measured.")
    return "\n".join(lines) + "\n"
