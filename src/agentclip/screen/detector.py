"""What is on screen right now, and where - one answer per frame, for everyone.

A detector detects. It does not know what the answer will be used for, and
nothing about *when* it looks may depend on what anybody is waiting for. That
sounds obvious and was not what shipped: the send button was searched for only
while the send gate held it open, and the copy button only inside the auto-copy
flow's own click - so their rows in the ELEMENTS column (tui.md 1.7) sat at "no
match yet" for the entire life of a session, and a user whose capture had
drifted had no way to see it until an automation silently failed. The state
machine has been demoted to a *reader* of this module: it consumes verdicts and
sightings, it never decides what gets looked for.

**The policy here is calibration and nothing else.** Every kind the live
window's service is calibrated for is searched on every frame:

* ``BUSY`` / ``IDLE`` through a :class:`~agentclip.screen.presence.PresenceTracker`
  each - calibrated meaning the service's finish-signal checklist ticks the
  signal *and* it has a capture of the appearance (a ticked signal with no
  picture can search for nothing);
* the drawn region's own stillness through a
  :class:`~agentclip.screen.stale.StaleTracker`, when the checklist ticks it -
  the one detector with no appearance behind it;
* ``SEND_READY`` and ``COPY`` as plain presence searches, calibrated meaning
  simply that the service has a capture of them. They have no checklist entry
  because there is nothing to decide: a service either shows such a control or
  it does not (tui.md 3.4d).

There is deliberately no session state, no gate, no flow and no loop phase in
that list, and no way to pass one in.

**One frame feeds all of them.** The caller captures once and calls
:meth:`ScreenDetector.observe`; every verdict in the returned
:class:`DetectionSnapshot` therefore describes the same instant of a moving
screen rather than four moments of it, and a failed capture reaches all of them
as the same ERROR. The snapshot is immutable and is published by a single
attribute assignment, which is what makes it safe to observe on a worker thread
and read on another: a reader either sees the previous whole answer or the new
whole answer, never half of one.

**Two ways to ask.** :class:`DetectionSnapshot` is *this* frame - the verdicts,
and per kind whether it was searched and where it was found. :meth:`last_seen`
is the memory: the most recent frame each kind was actually on screen, with the
timestamp of that frame, which is what "tell me what you detected and where"
means to a caller that is not standing at the poll loop. **A remembered
location is not a click target**: it is up to half a poll interval old, and the
auto-copy flow re-searches a fresh capture before it touches the mouse.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from agentclip.screen.busy import BusyProbe
from agentclip.screen.capture import RegionImage
from agentclip.screen.presence import PresenceTracker
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.stale import StaleProbe, StaleTracker
from agentclip.screen.template import RegionMatch, Template, find_in_region, match_rect

# The appearances that are searched for while the automation runs, in the order
# the ELEMENTS column lists them. The other three kinds (the two chat boxes, the
# new-chat button) are found on demand by a click, never on a timer.
RUNTIME_KINDS: tuple[TemplateKind, ...] = (
    TemplateKind.SEND_READY,
    TemplateKind.BUSY,
    TemplateKind.IDLE,
    TemplateKind.COPY,
)

# The kinds this module searches for DIRECTLY, rather than through a tracker:
# presence questions with no de-bounce, because neither of them is a finish
# detector. A tracker would actively harm the send gate - "not seen yet" and
# "has gone" are opposite answers and a debounce cannot tell them apart.
PROBE_KINDS: tuple[TemplateKind, ...] = (TemplateKind.SEND_READY, TemplateKind.COPY)

# The finish detectors, in the canonical order they are built, observed and
# posted in - the same order as config.FINISH_SIGNALS.
FINISH_DETECTORS: tuple[str, ...] = ("busy", "idle", "stale")


@dataclass(frozen=True, slots=True)
class Sighting:
    """One verified match: what was found, where, and when.

    The ``template`` travels with the ``match`` because a match is only half an
    answer - ``RegionMatch`` carries the top-left corner and the variant that
    matched carries the size, and it takes both to cut the pixels back out of
    the frame or to turn them into a rectangle on the real screen.
    """

    kind: TemplateKind
    template: Template
    match: RegionMatch
    at: float
    """``time.monotonic()`` of the frame this was found in."""

    @property
    def diff(self) -> float:
        return self.match.diff

    def rect(self, region: ScreenRegion) -> ScreenRegion:
        """Where it is on the real screen, given the region the frame came from."""
        return match_rect(region, self.template, self.match)


@dataclass(frozen=True, slots=True)
class DetectionSnapshot:
    """Everything one frame said. Immutable, and complete for that frame.

    ``sightings`` holds an entry for every kind that was SEARCHED and nothing
    else, which is what makes three states expressible: a kind mapped to a
    :class:`Sighting` was found, a kind mapped to ``None`` was searched and is
    not on screen, and a kind that is **absent** was not searched at all -
    because the live window's service has no capture of it (or, for busy/idle,
    does not tick it). A frame that failed to capture searched nothing, so the
    map is empty: a dropped frame is not evidence that anything went away.
    """

    at: float
    captured: bool
    busy: BusyProbe | None
    idle: BusyProbe | None
    stale: StaleProbe | None
    sightings: Mapping[TemplateKind, Sighting | None]

    def searched(self, kind: TemplateKind) -> bool:
        return kind in self.sightings

    def found(self, kind: TemplateKind) -> Sighting | None:
        """Where this kind was on this frame, or None (not there, or not searched)."""
        return self.sightings.get(kind)

    def present(self, kind: TemplateKind) -> bool | None:
        """Is it on screen? ``None`` = no answer from this frame.

        The send gate's three-valued question (tui.md 3.4b): True on screen,
        False not on screen, None the frame said nothing - which is a failed
        capture, or a kind this detector does not search at all.
        """
        if kind not in self.sightings:
            return None
        return self.sightings[kind] is not None


def _first_match(
    templates: Sequence[Template], scene: RegionImage, *, max_diff: float
) -> tuple[Template, RegionMatch] | None:
    """The first of a kind's images that is on screen, with where it is.

    A plain OR over the variants (screen.profile): a service that greys its send
    button out mid-upload draws a second picture of the same control, and either
    one being there means the same thing. First rather than best-matching,
    because this runs on a poll timer and the question is presence - and
    candidates arrive bottom-most first (screen.template), where the controls of
    a chat live.
    """
    for template in templates:
        match = find_in_region(template, scene, max_diff=max_diff)
        if match is not None:
            return (template, match)
    return None


class ScreenDetector:
    """Searches one drawn region for every appearance it is calibrated for.

    Owns no thread, no timer and no capture: the caller polls it with a frame,
    exactly as :class:`PresenceTracker` is polled, so the poll cadence, the
    thread model and the lifecycle stay with whoever built it. What it owns is
    the *composition* - which trackers exist and which template stacks are
    searched - and that is fixed for the life of the object: it describes one
    window pointed at one service, and both of those changing is what rebuilding
    it is for.
    """

    def __init__(
        self,
        region: ScreenRegion,
        *,
        busy: PresenceTracker | None = None,
        idle: PresenceTracker | None = None,
        stale: StaleTracker | None = None,
        templates: Mapping[TemplateKind, Sequence[Template]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.region = region
        self.busy = busy
        self.idle = idle
        self.stale = stale
        # Empty stacks are dropped rather than kept: "calibrated" and "has at
        # least one picture" are the same statement, and a kind mapped to an
        # empty tuple would report itself searched every tick while searching
        # for nothing.
        self._templates: dict[TemplateKind, tuple[Template, ...]] = {
            kind: tuple(templates[kind])
            for kind in PROBE_KINDS
            if templates is not None and templates.get(kind)
        }
        self._clock = clock
        self._latest: DetectionSnapshot | None = None
        self._last_seen: dict[TemplateKind, Sighting] = {}

    # -- what this detector is watching ---------------------------------------

    @property
    def active_detectors(self) -> tuple[str, ...]:
        """The FINISH detectors that will report, in canonical order.

        Only the three: the send button and the copy button close no tick and
        fold into no verdict, and the tick-closing rule (``_finish_tick_closed_by``)
        reads exactly this.
        """
        return tuple(
            name
            for name, tracker in (
                ("busy", self.busy),
                ("idle", self.idle),
                ("stale", self.stale),
            )
            if tracker is not None
        )

    @property
    def searched_kinds(self) -> tuple[TemplateKind, ...]:
        """Every appearance a frame will be searched for, in ELEMENTS order."""
        return tuple(kind for kind in RUNTIME_KINDS if self.searches(kind))

    def searches(self, kind: TemplateKind) -> bool:
        if kind is TemplateKind.BUSY:
            return self.busy is not None
        if kind is TemplateKind.IDLE:
            return self.idle is not None
        return kind in self._templates

    @property
    def watching(self) -> bool:
        """Is there anything at all to do with a frame?

        False means a poll loop would be pure cost: no tracker to feed and no
        picture to look for. It is deliberately NOT the same question as "is
        finish detection on" (``active_detectors``) - a service with a captured
        send button and an empty checklist still has something to show the user.
        """
        return bool(self.active_detectors) or bool(self._templates)

    # -- the tick --------------------------------------------------------------

    def observe(self, scene: RegionImage | None) -> DetectionSnapshot:
        """Fold one frame into a verdict per detector and a sighting per kind.

        Never raises. ``None`` is a capture that failed: every tracker hears
        about it as an ERROR with its streak intact, and nothing is searched -
        so the snapshot's sighting map is empty and no row of the ELEMENTS
        column is contradicted by a frame that does not exist.
        """
        at = self._clock()
        busy_probe = self.busy.observe(scene) if self.busy is not None else None
        idle_probe = self.idle.observe(scene) if self.idle is not None else None
        stale_probe = self.stale.observe(scene) if self.stale is not None else None
        sightings: dict[TemplateKind, Sighting | None] = {}
        if scene is not None:
            for kind, tracker in ((TemplateKind.BUSY, self.busy), (TemplateKind.IDLE, self.idle)):
                if tracker is not None:
                    # The tracker has already searched this very frame - taking
                    # its sighting rather than searching again is the difference
                    # between two template scans a tick and four.
                    sightings[kind] = self._sight(kind, tracker.last_sighting, at)
            for kind, templates in self._templates.items():
                sightings[kind] = self._sight(
                    kind, _first_match(templates, scene, max_diff=kind.max_diff), at
                )
        snapshot = DetectionSnapshot(
            at=at,
            captured=scene is not None,
            busy=busy_probe,
            idle=idle_probe,
            stale=stale_probe,
            sightings=sightings,
        )
        for sighting in sightings.values():
            if sighting is not None:
                self._last_seen[sighting.kind] = sighting
        self._latest = snapshot
        return snapshot

    def _sight(
        self, kind: TemplateKind, found: tuple[Template, RegionMatch] | None, at: float
    ) -> Sighting | None:
        if found is None:
            return None
        template, match = found
        return Sighting(kind=kind, template=template, match=match, at=at)

    # -- what it has detected, for readers that are not the poll loop ----------

    @property
    def latest(self) -> DetectionSnapshot | None:
        """The last frame's whole answer, or None before the first one."""
        return self._latest

    def last_seen(self, kind: TemplateKind) -> Sighting | None:
        """The most recent frame this kind was actually on screen, with its time.

        Never cleared by a miss: "the copy button was last seen 8 seconds ago"
        and "the copy button has never been seen" are different diagnoses of a
        harvest that failed, and only the memory can tell them apart. Use
        :meth:`DetectionSnapshot.found` for *now*, and re-search before acting
        on any coordinate - this one is up to a poll interval stale.
        """
        return self._last_seen.get(kind)

    def seen_ago(self, kind: TemplateKind, *, now: float | None = None) -> float | None:
        """Seconds since this kind was last on screen, or None if never."""
        sighting = self._last_seen.get(kind)
        if sighting is None:
            return None
        return (self._clock() if now is None else now) - sighting.at

    def locate(self, kind: TemplateKind) -> ScreenRegion | None:
        """Where it was last seen, in absolute screen coordinates. A HINT."""
        sighting = self._last_seen.get(kind)
        return None if sighting is None else sighting.rect(self.region)

    def reset(self) -> None:
        """Forget every tracker's debounce, as if freshly built.

        The sightings memory survives deliberately: it is a record of what was
        on screen, and AgentClip pasting into a chat does not un-see the copy
        button that was there.
        """
        for tracker in (self.busy, self.idle, self.stale):
            if tracker is not None:
                tracker.reset()


def build_detector(
    region: ScreenRegion,
    profile: ServiceProfile,
    *,
    signals: Sequence[str],
    required_ticks: int,
    clock: Callable[[], float] = time.monotonic,
) -> ScreenDetector:
    """Compose a detector from a drawn region and one service's calibration.

    The whole "what do I search for" policy, in one place and with nothing else
    in scope: ``signals`` is the service's finish-signal checklist
    (``ServicePreset.finish_signals``) and ``profile`` is what it has pictures
    of. Busy and idle need both halves - a ticked signal with no capture
    searches for nothing, a capture the checklist does not tick is not wanted -
    while stale needs no picture at all and the send/copy buttons need no tick.

    Every image the service has of a kind is passed on, not one: the searches OR
    them, so a second capture of the same control drawn differently is one more
    way to see it and never a replacement.
    """
    busy_templates = profile.variants(TemplateKind.BUSY) if "busy" in signals else ()
    idle_templates = profile.variants(TemplateKind.IDLE) if "idle" in signals else ()
    return ScreenDetector(
        region,
        busy=(
            PresenceTracker(
                busy_templates,
                found_is_busy=True,
                required_ticks=required_ticks,
                max_diff=TemplateKind.BUSY.max_diff,
            )
            if busy_templates
            else None
        ),
        idle=(
            PresenceTracker(
                idle_templates,
                found_is_busy=False,
                required_ticks=required_ticks,
                max_diff=TemplateKind.IDLE.max_diff,
            )
            if idle_templates
            else None
        ),
        # No ``capture=``: the caller hands every tick's single frame to
        # ``observe``, so this tracker never captures for itself.
        stale=StaleTracker(region, required_ticks=required_ticks) if "stale" in signals else None,
        templates={kind: profile.variants(kind) for kind in PROBE_KINDS},
        clock=clock,
    )
