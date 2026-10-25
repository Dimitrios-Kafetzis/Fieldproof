"""The temporal combinators (task P2-T05): ``always``, ``eventually``, ``never``.

A condition over several named **signals** (topic fields and metrics of the run, as a `when`
trigger reads them), judged every time one of them changes once all of them have a value:
``always`` fails at the first evaluation that does not hold, ``never`` at the first that does,
``eventually`` passes at the first that does (or once it has held for ``for_s``). ``after_s``
and ``within_s`` make the window. ``topic_value`` is the one-signal case; these combine
signals: "while the link is reported lost, the vehicle commands no speed" is
``always`` over ``link_state.data`` and ``cmd_vel.linear.x``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from fieldproof.assertions.core import (
    FAILED,
    PASSED,
    AssertionSpec,
    AssertionType,
    Checker,
    Metric,
    PlanContext,
    PlannedAssertion,
    PlannedSignal,
    Sample,
    WindowedChecker,
    rounded,
)
from fieldproof.dsl import AssertionBase, Duration, Signal
from fieldproof.expression import ExpressionError, check_names, compile_condition
from fieldproof.manifest import Name


# ------------------------------------------------------------------------------- the specs
class TemporalBase(AssertionBase):
    """What the three combinators share: the signals, the condition, the window."""

    signals: dict[str, Signal] = Field(
        min_length=1,
        description="The named signals the condition reads (a topic field or a metric each).",
    )
    condition: str = Field(description="Expression over the signals' names.")
    vehicle: Name | None = Field(default=None, description="The vehicle the signals default to.")
    after_s: Duration = Field(default=0.0, description="Grace period after the timeline starts.")
    within_s: Duration | None = Field(
        default=None,
        description="End of the window: the deadline of `eventually`, the end of `always` "
        "and `never` (absent: the end of the timeline).",
    )

    def vehicle_names(self) -> list[str]:
        """See :meth:`AssertionBase.vehicle_names`."""
        names = [self.vehicle] if self.vehicle else []
        return names + [s.vehicle for s in self.signals.values() if s.vehicle is not None]

    @model_validator(mode="after")
    def _compiles(self) -> TemporalBase:
        names = list(self.signals)
        try:
            check_names(names)
            compiled = compile_condition(self.condition, names)
        except ExpressionError as error:
            raise ValueError(f"condition: {error}") from error
        unused = sorted(set(names) - compiled.used)
        if unused:
            raise ValueError(f"condition: the signal(s) {', '.join(unused)} are not read")
        if self.within_s is not None and self.within_s <= self.after_s:
            raise ValueError("within_s must be later than after_s")
        return self


class Always(TemporalBase):
    """The condition holds at every evaluation in the window."""

    type: Literal["always"]


class Never(TemporalBase):
    """The condition holds at no evaluation in the window."""

    type: Literal["never"]


class Eventually(TemporalBase):
    """The condition holds at some evaluation in the window (for `for_s`, when given)."""

    type: Literal["eventually"]
    for_s: Duration | None = Field(
        default=None,
        description="The condition must keep holding this long once it first does.",
    )


# ----------------------------------------------------------------------------- the checker
class TemporalChecker(WindowedChecker):
    """The combinator over its signals."""

    def __init__(self, planned: PlannedAssertion) -> None:
        super().__init__(planned)
        self.assertion: TemporalBase = planned.assertion  # type: ignore[assignment]
        self.signals: dict[str, PlannedSignal] = dict(planned.resolved.get("signals", {}))
        self.condition = compile_condition(self.assertion.condition, list(self.signals))
        self.values: dict[str, Any] = {}
        self.evaluations = 0
        self.held_since: float | None = None
        self.longest_hold = 0.0
        self.satisfied_t: float | None = None

    @property
    def mode(self) -> str:
        """`always`, `never` or `eventually`."""
        return self.planned.type

    @property
    def for_s(self) -> float | None:
        """How long an `eventually` must hold."""
        return getattr(self.assertion, "for_s", None)

    def metrics(self) -> dict[str, Metric]:
        """`evaluations`, `satisfied_at_s`, `longest_hold_s`."""
        return {
            "evaluations": (self.evaluations, ""),
            "satisfied_at_s": (self.satisfied_t, "s"),
            "longest_hold_s": (self.longest_hold if self.mode == "eventually" else None, "s"),
        }

    def _stats(self) -> dict[str, Any]:
        return {"evaluations": self.evaluations, "last_values": dict(self.values)}

    def _snapshot(self, t: float, sample: Sample) -> dict[str, Any]:
        return {"t_s": rounded(t), "sim_s": rounded(sample.sim), "values": dict(self.values)}

    def on_sample(self, sample: Sample, t: float) -> None:
        """See :meth:`Checker.on_sample`."""
        if self.done:
            return
        changed = False
        for name, signal in self.signals.items():
            error = signal.error_of(sample)
            if error is not None:
                self.resolve(FAILED, f"{name} ({signal.summary()}): {error}", t, sample.sim)
                return
            value = signal.value_of(sample)
            if value is not None:
                self.values[name] = value
                changed = True
        if not changed or len(self.values) < len(self.signals):
            return
        if not self.in_window(t):
            if self.window_closed(t):
                self.on_tick(t)
            return
        try:
            holds = self.condition.evaluate(self.values)
        except ExpressionError as error:
            self.resolve(FAILED, str(error), t, sample.sim, **self._stats())
            return
        self.evaluations += 1
        self.note(t, sample, holds=holds, values={k: rounded(v) for k, v in self.values.items()})
        if self.mode == "always" and not holds:
            self.resolve(
                FAILED,
                f"{self.assertion.condition!r} did not hold at {t:.1f} s with "
                f"{self._values_text()}",
                t,
                sample.sim,
                violation=self._snapshot(t, sample),
                **self._stats(),
            )
        elif self.mode == "never" and holds:
            self.resolve(
                FAILED,
                f"{self.assertion.condition!r} held at {t:.1f} s with {self._values_text()}",
                t,
                sample.sim,
                violation=self._snapshot(t, sample),
                **self._stats(),
            )
        elif self.mode == "eventually":
            self._eventually(t, sample, holds)

    def _values_text(self) -> str:
        return ", ".join(f"{k} = {v!r}" for k, v in self.values.items())

    def _eventually(self, t: float, sample: Sample, holds: bool) -> None:
        if not holds:
            if self.held_since is not None:
                self.longest_hold = max(self.longest_hold, t - self.held_since)
            self.held_since = None
            return
        if self.held_since is None:
            self.held_since = t
        held = t - self.held_since
        self.longest_hold = max(self.longest_hold, held)
        if self.for_s is None or held >= self.for_s:
            self.satisfied_t = t
            reason = f"{self.assertion.condition!r} held at {t:.1f} s with {self._values_text()}"
            if self.for_s is not None:
                reason = f"{self.assertion.condition!r} held for {held:.1f} s by {t:.1f} s"
            self.resolve(
                PASSED,
                reason,
                t,
                sample.sim,
                satisfied=self._snapshot(t, sample),
                held_since_t_s=rounded(self.held_since),
                **self._stats(),
            )

    def _settle(self, t: float) -> None:
        if self.done:
            return
        if self.evaluations == 0:
            missing = sorted(set(self.signals) - set(self.values))
            why = f"never evaluated {self.window_text()}"
            if missing:
                why += f": no value of {', '.join(missing)}"
            self.resolve(FAILED, why, t, **self._stats())
            return
        if self.mode == "eventually":
            if self.held_since is not None and self.for_s is not None:
                held = t - self.held_since
                self.longest_hold = max(self.longest_hold, held)
                if held >= self.for_s:
                    self.satisfied_t = t
                    self.resolve(
                        PASSED,
                        f"{self.assertion.condition!r} held for {held:.1f} s by {t:.1f} s",
                        t,
                        held_since_t_s=rounded(self.held_since),
                        **self._stats(),
                    )
                    return
            what = f"never held {self.window_text()}"
            if self.for_s is not None:
                what = (
                    f"never held for {self.for_s:g} s {self.window_text()} (longest "
                    f"{self.longest_hold:.1f} s)"
                )
            self.resolve(
                FAILED,
                f"{self.assertion.condition!r} {what} ({self.evaluations} evaluation(s))",
                t,
                **self._stats(),
            )
            return
        verb = "held" if self.mode == "always" else "never held"
        self.resolve(
            PASSED,
            f"{self.assertion.condition!r} {verb} in all {self.evaluations} evaluation(s) "
            f"{self.window_text()}",
            t,
            **self._stats(),
        )

    def on_tick(self, t: float) -> None:
        """See :meth:`Checker.on_tick`."""
        if self.done:
            return
        if (
            self.mode == "eventually"
            and self.held_since is not None
            and self.for_s is not None
            and t - self.held_since >= self.for_s
            and self.in_window(t)
        ):
            self._settle(t)
            return
        if self.window_closed(t):
            self._settle(t)

    def on_finish(self, t: float) -> None:
        """See :meth:`Checker.on_finish`."""
        self._settle(t)


# ------------------------------------------------------------------------------- the types
class TemporalType(AssertionType):
    """What the three types share: the signals resolved, the checker."""

    metrics: ClassVar[dict[str, str]] = {
        "evaluations": "how many times the condition was judged",
        "satisfied_at_s": "when it was satisfied (eventually; s)",
        "longest_hold_s": "the longest it held (eventually; s)",
    }

    def plan(self, spec: TemporalBase, context: PlanContext) -> PlannedAssertion:
        """Every signal resolved under the assertion's vehicle."""
        signals = {
            name: context.signal(signal, spec.vehicle, f"signals.{name}")
            for name, signal in spec.signals.items()
        }
        watch = tuple(w for s in signals.values() for w in s.watches())
        return PlannedAssertion(spec, resolved={"signals": signals}, watch=watch)

    def checker(self, planned: PlannedAssertion) -> Checker:
        """See :meth:`AssertionType.checker`."""
        return TemporalChecker(planned)


class AlwaysType(TemporalType):
    """`always`."""

    type: ClassVar[str] = "always"
    Spec: ClassVar[AssertionSpec] = Always
    summary: ClassVar[str] = "A condition over several signals holds at every evaluation."


class NeverType(TemporalType):
    """`never`."""

    type: ClassVar[str] = "never"
    Spec: ClassVar[AssertionSpec] = Never
    summary: ClassVar[str] = "A condition over several signals holds at no evaluation."


class EventuallyType(TemporalType):
    """`eventually`."""

    type: ClassVar[str] = "eventually"
    Spec: ClassVar[AssertionSpec] = Eventually
    summary: ClassVar[str] = (
        "A condition over several signals holds at some evaluation (for a while)."
    )


ASSERTIONS: tuple[type[AssertionType], ...] = (AlwaysType, NeverType, EventuallyType)
