"""The built-in radio link events (task P2-T02, ported to the plugin API in P2-T03).

`link.set`, `link.drop` and `link.restore` need a scenario with a `network` section
(docs/reference/network.md); the profile goes on the vehicle's link holder through the run's
:class:`EventContext`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import Field

from fieldproof.dsl import Duration, ScenarioError, TimelineBase
from fieldproof.events import OK, TIMEOUT, ActionRecord, Event, EventContext, PlanContext
from fieldproof.manifest import Name
from fieldproof.network import DROP

LINK_ACTIONS = ("link.set", "link.drop", "link.restore")


class LinkSet(TimelineBase):
    """Put another profile on a vehicle's radio link (`network.profiles` or a preset)."""

    vehicle: Name = Field(description="The vehicle whose link changes.")
    action: Literal["link.set"]
    profile: str = Field(description="The profile from now on.")


class LinkDrop(TimelineBase):
    """Drop a vehicle's radio link: nothing gets through either way.

    With `duration_s` the entry restores the link's configured profile after that long and ends
    then; without it the link stays down until a `link.restore` or `link.set`.
    """

    vehicle: Name = Field(description="The vehicle whose link drops.")
    action: Literal["link.drop"]
    duration_s: Duration | None = Field(
        default=None, description="Restore the link after this long (absent: stay down)."
    )


class LinkRestore(TimelineBase):
    """Bring back the profile the scenario configures for a vehicle's link."""

    vehicle: Name = Field(description="The vehicle whose link is restored.")
    action: Literal["link.restore"]


class LinkEvent(Event):
    """What the three link events share: they need radio links."""

    def plan(self, entry: TimelineBase, context: PlanContext) -> dict[str, Any]:
        """Refuse the entry without a `network` section."""
        context.vehicle(entry.vehicle)
        if not context.links:
            raise ScenarioError(
                f"{context.where}: {self.action} needs a network section (radio links)"
            )
        return {}


class LinkSetEvent(LinkEvent):
    """`link.set`."""

    action: ClassVar[str] = "link.set"
    Entry: ClassVar[type[TimelineBase]] = LinkSet
    summary: ClassVar[str] = "Put another profile on the vehicle's radio link."

    def apply(
        self,
        entry: LinkSet,
        resolved: Mapping[str, Any],  # noqa: ARG002
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Apply the profile."""
        context.apply_link(entry.vehicle, entry.profile, record.name)
        record.status, record.detail = OK, f"{entry.profile} applied"


class LinkRestoreEvent(LinkEvent):
    """`link.restore`."""

    action: ClassVar[str] = "link.restore"
    Entry: ClassVar[type[TimelineBase]] = LinkRestore
    summary: ClassVar[str] = "Bring back the profile the scenario configures for the link."

    def apply(
        self,
        entry: LinkRestore,
        resolved: Mapping[str, Any],  # noqa: ARG002
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Apply the configured profile."""
        configured = context.link_profile(entry.vehicle)
        context.apply_link(entry.vehicle, configured, record.name)
        record.status, record.detail = OK, f"{configured} restored"


class LinkDropEvent(LinkEvent):
    """`link.drop`."""

    action: ClassVar[str] = "link.drop"
    Entry: ClassVar[type[TimelineBase]] = LinkDrop
    summary: ClassVar[str] = (
        "Drop the vehicle's radio link, for a duration (then the configured profile) or for good."
    )

    def apply(
        self,
        entry: LinkDrop,
        resolved: Mapping[str, Any],
        context: EventContext,
        record: ActionRecord,
    ) -> None:
        """Drop the link; with a duration, hold the worker and restore the configured profile.

        A drop with a duration holds its worker until the link is restored, so the entry ends
        when the outage does.
        """
        context.apply_link(entry.vehicle, DROP, record.name)
        record.status, record.detail = OK, "dropped"
        if entry.duration_s is None:
            return
        limit_s = float(resolved.get("limit_s", entry.duration_s))
        if not context.hold(min(entry.duration_s, limit_s)):
            return
        if entry.duration_s > limit_s:
            record.status = TIMEOUT
            record.detail = f"the budget ended the drop after {limit_s:.0f} s"
            return
        configured = context.link_profile(entry.vehicle)
        context.apply_link(entry.vehicle, configured, f"{record.name} (restore)")
        record.detail = f"dropped for {entry.duration_s:g} s, {configured} restored"


EVENTS = (LinkSetEvent, LinkDropEvent, LinkRestoreEvent)
