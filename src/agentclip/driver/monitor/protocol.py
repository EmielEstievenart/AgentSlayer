"""The ``UIMonitor`` contract: what a brain may ask of the machine whose screen
shows the chat, and the one thing that comes back - a :class:`Tick`.

docs/design/ui-monitor.md §2–§3 is binding here. The rules in one place:

* **Queries are local reads; actions are round trips** (§2.1). The monitor
  polls on its own and pushes a tick after every observation. ``latest`` is the
  newest one, free; :meth:`UIMonitor.observe` waits for the *next* one, which
  is what a recipe wants right after it scrolled.
* **The tick carries booleans, locations and counts - never pixels** (§2.2).
  Every field below is a scalar, an enum, a frozen dataclass of scalars, or a
  mapping of those, so the wire (phase 5) never needs a binary codec. Crops for
  the ELEMENTS panel are a *calibration* surface and stay on the local-only
  tier of :class:`~agentclip.driver.monitor.local.LocalUIMonitor`.
* **Policy stays local** (§2.3). Nothing here knows what a ``LoopState`` is, and
  nothing in this package imports ``driver/automation``.
* **Ghost ticks are dropped** (§4.2). Every tick carries the ``generation`` it
  was captured under; :meth:`UIMonitor.observe` never hands out one that
  predates the last retarget.
* **The service is the monitor's** (§10.5). The brain never sends a service
  key, a preset or a spec: it names a WINDOW (:meth:`UIMonitor.watch`) and
  reads back the monitor's whole effective service as a :class:`Watched`.
  :meth:`UIMonitor.configure` stays as the in-process door the Monitor UI and
  the suites drive, and does not cross the wire.

Phase 1 (§6.1) carries exactly what the controller's five ``consume_*`` methods
read today: the three debounced probes, the per-kind sighting map (as screen
rectangles, not template matches), and which detectors this configuration
runs. The controller-computed streaks (stale-arm, copy-changed) move onto the
tick in phase 2 when ``evaluate_finish`` splits into a recipe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from agentclip.config import (
    DEFAULT_DELIVERY,
    DEFAULT_SCROLL_ACTION,
    DEFAULT_SUBMIT_DELAY_S,
    ServicePreset,
)
from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.stale import StaleProbe

# Kinds the monitor searches every frame it captures, in the order the ELEMENTS
# panel lists them. Mirrors ``driver.screen.detector.RUNTIME_KINDS``; spelled
# out here so a tick's consumer never has to import the detector.
TICK_KINDS: tuple[TemplateKind, ...] = (
    TemplateKind.BUSY,
    TemplateKind.IDLE,
    TemplateKind.CHATBOX_INITIAL,
    TemplateKind.CHATBOX_ONGOING,
    TemplateKind.COPY,
    TemplateKind.NEW_CHAT,
    TemplateKind.SEND_READY,
)


class ElementClick(Enum):
    """Outcome of the find-then-click primitive (:meth:`UIMonitor.click_element`).

    Six states, not a bool, because the five failures are five different things
    to tell the user: the app is disarmed and may not click anything at all
    (DISARMED - the user's own switch, and the only one that is not about this
    element), nothing to look for or nowhere to look (NOT_CALIBRATED - go
    capture it), it is simply not on screen (MISMATCH - nothing was clicked, and
    clicking blind is never the answer), it is on screen in more than one place
    (AMBIGUOUS - which is not a search failure but a *drawing* failure, and the
    fix is to redraw the window), or we clicked and the OS refused (NOT_CLICKED
    - Windows-only input).

    **Only four of them are the monitor's** (docs/design/ui-monitor.md §2.3).
    MISMATCH, AMBIGUOUS, CLICKED and NOT_CLICKED are pixel verdicts: they are
    what looking at the screen and pressing a pixel came to, and
    :meth:`UIMonitor.click_element` returns nothing else, ever. DISARMED and
    NOT_CALIBRATED are refusals the BRAIN makes *before* it calls - the armed
    switch is policy and stays local, and "there is nothing captured to look
    for" is answered against calibration the brain is holding anyway. A monitor
    that returned either would be answering a question about a machine it does
    not own.

    (The enum keeps all six because one verdict type spans the whole decision:
    a caller folds its own two refusals and the monitor's four answers into one
    value to report, and two enums to say one thing would be two enums to keep
    in step.)
    """

    CLICKED = "clicked"
    MISMATCH = "mismatch"  # not on screen right now; refused to click
    AMBIGUOUS = "ambiguous"  # several of them in the region; refused to guess
    NOT_CLICKED = "not_clicked"  # found fine, but the click did not land
    NOT_CALIBRATED = "not_calibrated"  # no chat region drawn, or nothing captured
    DISARMED = "disarmed"  # the OS-acting switch is off; nothing was looked at


@dataclass(frozen=True, slots=True)
class Watched:
    """The monitor's whole effective service for the window it is watching (§10.5).

    The answer to ``watch(slot)``, and the ONLY door the brain has onto which
    service is being driven: the Chat UI never sends a service key, a preset or
    a spec, so everything it needs in order to compose a turn for this chat has
    to come back out of here (docs/design/ui-monitor.md §10.5). The monitor
    resolves all of it on its own machine, out of its own config, its own region
    store and its own captured appearances.

    The identity half:

    * ``service`` - the profile KEY the monitor settled on; None before it has
      been pointed at anything.
    * ``label`` - that preset's human name, for a read-only picker to show.
    * ``region`` - the box actually watched: the spec's, or the one this
      monitor's machine remembered for the service. None when neither exists.
    * ``profiled`` - whether THIS machine has a captured appearance for that
      key. False is the split-mode trap made visible: a brain driving
      ``claude`` against a monitor calibrated for ``zai`` gets every element
      verdict as NOT_CALIBRATED and nothing else says why.
    * ``captured`` - WHICH appearances, kind by kind (§11.3). ``profiled`` says
      the monitor has *something* for the key; this says whether it has a copy
      button, a new-chat button, a chat box. It is here because the brain owns
      no templates any more: "may I ask for a new chat?" used to be a read of
      the brain's own profile store, which on any machine but the one the
      pictures were taken on answered no. Empty for an unprofiled service, and
      empty is a refusal every recipe makes before it calls.
    * ``generation`` - the run this answer describes, i.e. the stamp its ticks
      carry. It is what makes ``watched()`` re-readable without a round trip
      per tick: a brain compares the generation on an arriving tick with the one
      it last read, and asks again only when the two differ - which is how a
      service picked or a region redrawn in the Monitor UI reaches the Chat UI
      with no new frame from the brain at all.

    The PRESET half is the rest, and it is here for the same reason the region
    is: the ``[services.*]`` table lives on the monitor's disk and is edited in
    the Monitor UI, so a brain that read its own host's copy would be composing
    turns against a service somebody else is running. These are the fields a
    brain ACTS on - what a paste may weigh, whether it may press Enter, whether
    a reply has to arrive fenced, what extra sentence this host needs, whether
    this chat gets the ranged-edit tools (``edit_by_lines``, which decides a
    CATALOG the bootstrap is built from and is therefore as much the monitor's
    answer as the paste budget beside it) - and nothing about how pixels are
    searched (a tolerance or a matcher is the monitor's business and never
    leaves it).
    """

    service: str | None
    region: ScreenRegion | None
    profiled: bool
    label: str = ""
    generation: int = 0
    captured: tuple[TemplateKind, ...] = ()
    # -- the preset the brain acts on (§10.5) ------------------------------
    delivery: str = DEFAULT_DELIVERY
    auto_submit: bool = False
    submit_delay_s: float = DEFAULT_SUBMIT_DELAY_S
    scroll_action: str = DEFAULT_SCROLL_ACTION
    snap_back: bool = True
    hover_scan: bool = False
    max_paste_chars: int = 0
    total_context_chars: int = 0
    wrap_blocks_in_fence: bool = True
    attachment_note: bool = True
    require_fenced_reply: bool = False
    extra_instructions: str = ""
    edit_by_lines: bool = False


#: What a monitor that has not been pointed at anything answers, and what an
#: idle handle answers for ever. One object rather than three
#: ``Watched(None, None, False)`` literals, so "nothing is being watched" reads
#: the same at every site that has to say it.
EMPTY_WATCHED = Watched(service=None, region=None, profiled=False)


@dataclass(frozen=True, slots=True)
class Located:
    """One search's whole answer: where, where to click, whether there were
    several, how close.

    Four fields because a caller that has only the first one cannot tell the
    two failures apart, and they call for opposite things:

    * ``region`` - where the BOTTOM-MOST match of the kind is, in absolute
      screen coordinates, or None for "not on screen". The lowest one because
      every response stamps its own copy of an icon and the newest is at the
      bottom.
    * ``ambiguous`` - the kind was found in more than one place inside the
      configured region. Not a search failure but a DRAWING failure: an
      appearance belongs to the service, so a second window of it under one
      drawn region carries an identical button and picking one is a coin toss
      between two conversations. ``region`` still names the lowest of them, so a
      caller that only wants to report the trouble does not have to search
      again; every caller that CLICKS is expected to refuse.
    * ``best_miss`` - the smallest diff among the candidates that were judged
      and rejected, None when nothing got as far as being judged. The one number
      that turns "not found" into an actionable report: a near miss says the
      capture has drifted and wants recapturing, nothing judged at all says the
      thing simply was not on the frame. Only ever set on a miss - a hit needs
      no diagnosis.
    * ``target`` - the ONE pixel a click on this match should land on: the
      service's own click point (a per-image percentage the user set in the
      Monitor UI) applied to ``region``, as the 1x1 rectangle every click in
      the app takes. None if and only if ``region`` is None. It is computed
      here, on the machine that holds the pictures, because a click point is
      part of an appearance and §11.3 leaves the brain none: a caller that
      clicks a rectangle's middle instead aims at whatever the service drew
      there.
    """

    region: ScreenRegion | None
    ambiguous: bool
    best_miss: float | None
    target: ScreenRegion | None = None


@dataclass(frozen=True, slots=True)
class Tick:
    """One observation of the chat window, as the brain is allowed to see it.

    ``sightings`` holds an entry for every kind that was SEARCHED and nothing
    else - the same three-state map ``DetectionSnapshot.sightings`` keeps, with
    the located :class:`ScreenRegion` (absolute screen coordinates) in place of
    the pixel match: mapped to a region = found there, mapped to ``None`` =
    searched and not on screen, absent = not searched (no capture of it). A
    frame that failed to capture searched nothing, so the map is empty.

    ``busy`` / ``idle`` / ``stale`` are the FINISH probes, ``None`` for a
    signal the service's checklist does not tick. ``active_detectors`` names
    the signals this configuration runs (``"busy"``, ``"idle"``, ``"stale"``)
    so a consumer can tell "no verdict yet" from "never going to get one".

    ``seq`` counts every tick the monitor has produced since it was built and
    never repeats; ``generation`` is bumped by every :meth:`UIMonitor.configure`
    and is what makes a tick a ghost; ``at`` is the monitor's monotonic clock
    at capture time.

    The last two are the STREAKS (§2.2's "counts"), and they are counts of
    consecutive ticks rather than of anything on this one - which is exactly why
    the monitor computes them: a brain that had to keep its own running totals
    would lose them on every reconnect and would have to be told which ticks it
    had already counted (§2.9). Both are the arithmetic
    ``AutomationController.evaluate_finish`` did as it read each probe, moved
    down whole:

    * ``stale_arm_streak`` - consecutive ticks whose stale probe reported a BIG
      change (``MonitorSpec.send_arm_min_diff``). What the stale detector alone
      is allowed to arm an auto-copy on, so it has to be sustained: a blinking
      caret changes the region too.
    * ``changed_streak`` - consecutive ticks on which every ACTIVE detector said
      "finished". The agreement a second detector exists for.

    Both are counts and neither is a decision: what a run of two is worth, and
    whether anything may fire on it, is the brain's (§2.3). Both restart at zero
    on a ``configure`` and on a tracker reset, because both are statements about
    a screen that has just been declared a different screen.
    """

    seq: int
    generation: int
    at: float
    captured: bool
    busy: BusyProbe | None
    idle: BusyProbe | None
    stale: StaleProbe | None
    sightings: Mapping[TemplateKind, ScreenRegion | None]
    active_detectors: tuple[str, ...]
    # Defaulted so that every tick written by hand - a suite's scenario, a
    # shell's placeholder - stays one line shorter than the thing it is about.
    # The poll loop always fills both in.
    stale_arm_streak: int = 0
    changed_streak: int = 0

    def searched(self, kind: TemplateKind) -> bool:
        return kind in self.sightings

    def present(self, kind: TemplateKind) -> bool | None:
        """Is it on screen? ``None`` = this tick has no answer (failed capture,
        or a kind this configuration does not search)."""
        if kind not in self.sightings:
            return None
        return self.sightings[kind] is not None

    def visible(self, kind: TemplateKind) -> bool:
        return self.sightings.get(kind) is not None

    def locate(self, kind: TemplateKind) -> ScreenRegion | None:
        """Where it was on this tick, on the real screen; ``None`` otherwise."""
        return self.sightings.get(kind)


@dataclass(frozen=True, slots=True)
class MonitorSpec:
    """Everything the monitor needs to watch one chat window - §2.10's payload.

    Plain data, every field a scalar or a tuple of scalars. The template PNGs
    never appear: ``service`` is the profile KEY, and the monitor resolves it on
    its own machine.

    **It no longer crosses the wire.** Until wave 3 the brain composed one of
    these and sent it; §10.5 turned that round - the monitor owns its own
    service configuration and a remote brain calls :meth:`UIMonitor.watch`
    instead. So this is the MONITOR-SIDE payload now: what its own config, its
    own region store and (when it has a window) its own Monitor UI hand to
    :meth:`UIMonitor.configure`. :func:`spec_from_preset` is how one is built
    from a ``[services.*]`` row.

    Three groups. The first is what to WATCH and how to search it -
    ``service``, ``region``, the finish checklist, the debounce and the
    matcher's two knobs - and none of it ever leaves the monitor.
    ``stable_seconds`` is raw seconds; the monitor converts it against its own
    tick rate (§2.10 "cadence moves to the monitor"), so no caller computes
    ticks.

    The second is ``label`` plus the preset fields a BRAIN acts on. They are
    carried here rather than beside here so that one callable
    (``LocalUIMonitor``'s ``spec_for``) answers the whole of "what is this
    window", and :class:`Watched` can be built from one object: a second
    ``preset_for`` seam would be two answers free to name two different
    services.
    """

    service: str
    region: ScreenRegion | None
    finish_signals: tuple[str, ...]
    stable_seconds: float
    tolerance: int
    matcher: str
    # -- what the brain gets to see (§10.5) --------------------------------
    # Defaulted to ``ServicePreset``'s own defaults, so a spec written by hand
    # in a suite stays one line and still says what the real one would.
    label: str = ""
    hover_scan: bool = False
    scroll_action: str = DEFAULT_SCROLL_ACTION
    snap_back: bool = True
    delivery: str = DEFAULT_DELIVERY
    auto_submit: bool = False
    submit_delay_s: float = DEFAULT_SUBMIT_DELAY_S
    max_paste_chars: int = 0
    total_context_chars: int = 0
    wrap_blocks_in_fence: bool = True
    attachment_note: bool = True
    require_fenced_reply: bool = False
    extra_instructions: str = ""
    edit_by_lines: bool = False


def spec_from_preset(
    preset: ServicePreset,
    region: ScreenRegion | None = None,
    *,
    service: str | None = None,
) -> MonitorSpec:
    """One ``[services.*]`` row, as the monitor's target for a window.

    The single place a config preset becomes a spec, so the headless monitor
    (``driver/monitor/__main__.build_monitor``) and the Monitor UI's own
    ``_spec()`` cannot come to two different answers about the same row. The
    region is separate because it is not in the preset at all: it is a fact
    about this desktop, kept in the monitor's region store, and ``None`` here
    is the honest "let the store fill it" that ``configure`` already handles.

    ``service`` overrides the key the spec is filed under, for the one config a
    window can be in that a preset cannot describe: ``general.service`` naming a
    row that does not exist. The caller falls back to a default PRESET but keeps
    the KEY it was asked for, because the key is what the profile store and the
    region store are indexed by and answering about another one would report a
    calibration that belongs to a different service.
    """
    return MonitorSpec(
        service=preset.key if service is None else service,
        region=region,
        finish_signals=tuple(preset.finish_signals),
        stable_seconds=preset.stable_seconds,
        tolerance=preset.tolerance,
        matcher=preset.matcher,
        label=preset.label,
        hover_scan=preset.hover_scan,
        scroll_action=preset.scroll_action,
        snap_back=preset.snap_back,
        delivery=preset.delivery,
        auto_submit=preset.auto_submit,
        submit_delay_s=preset.submit_delay_s,
        max_paste_chars=preset.max_paste_chars,
        total_context_chars=preset.total_context_chars,
        wrap_blocks_in_fence=preset.wrap_blocks_in_fence,
        attachment_note=preset.attachment_note,
        require_fenced_reply=preset.require_fenced_reply,
        extra_instructions=preset.extra_instructions,
        edit_by_lines=preset.edit_by_lines,
    )


def watched_from(
    spec: MonitorSpec,
    *,
    profiled: bool,
    generation: int,
    captured: tuple[TemplateKind, ...] = (),
) -> Watched:
    """The answer a monitor gives for the spec it just settled on.

    Written once because two monitors build it - the real one and the double -
    and a double whose ``watched()`` dropped a preset field would be a suite
    passing against nothing.

    ``captured`` comes from beside the spec rather than out of it: it is a fact
    about the pictures on this machine (``ServiceProfile.captured``), and the
    spec is a fact about the row in the config. ``profiled`` is the same fact
    narrowed to a yes/no, so the two always move together - an unprofiled
    service captures nothing.
    """
    return Watched(
        service=spec.service,
        region=spec.region,
        profiled=profiled,
        label=spec.label,
        generation=generation,
        captured=captured,
        delivery=spec.delivery,
        auto_submit=spec.auto_submit,
        submit_delay_s=spec.submit_delay_s,
        scroll_action=spec.scroll_action,
        snap_back=spec.snap_back,
        hover_scan=spec.hover_scan,
        max_paste_chars=spec.max_paste_chars,
        total_context_chars=spec.total_context_chars,
        wrap_blocks_in_fence=spec.wrap_blocks_in_fence,
        attachment_note=spec.attachment_note,
        require_fenced_reply=spec.require_fenced_reply,
        extra_instructions=spec.extra_instructions,
        edit_by_lines=spec.edit_by_lines,
    )


#: How a monitor answers "what am I watching for this window": its own config,
#: its own region store, and - where there is one - its own Monitor UI. The
#: whole of §10.5's inversion in one type. The slot enum is ``driver/screen``'s,
#: because a slot IS a drawn box and that is squarely below this layer.
SpecFor = Callable[[AgentSlot], MonitorSpec]


TickHook = Callable[[Tick], None]
ClipHook = Callable[[str], None]
#: What the Monitor UI's page wears, as the Chat UI's ``[gui] theme`` names it.
#: Registered on the LOCAL tier only (``LocalUIMonitor.on_theme``), for
#: ``on_frame``'s reason: a theme travels brain -> monitor and nothing ever
#: arrives on the brain's side, so a hook on the wire-facing Protocol would be a
#: registration that can never fire.
ThemeHook = Callable[[str], None]


class UIMonitor(Protocol):
    """docs/design/ui-monitor.md §3. Two implementations: ``LocalUIMonitor``
    (in-process, phase 1) and ``RemoteUIMonitor`` (TCP client, phase 5).

    Hooks registered with :meth:`subscribe` / :meth:`on_clip` are called on the
    monitor's own thread (local) or reader task (remote) and must not block;
    they are the seam the GUI's live detection panel and the clipboard ingest
    hang off. Every action returns whether the OS accepted it, never raises for
    "it did not land".
    """

    # -- lifecycle / configuration ----------------------------------------
    async def configure(self, spec: MonitorSpec) -> int:
        """Retarget onto ``spec`` - the IN-PROCESS door, and only that (§10.5).

        What the Monitor UI and the suites drive, on the machine where the spec
        was composed. Deliberately NOT on the wire any more: a remote brain has
        no business naming a service, a region or a tolerance on somebody
        else's desktop, and calls :meth:`watch` instead.

        Rebuilds trackers fresh (never mutates the
        old ones), bumps and returns the generation; ticks captured under an
        older generation are ghosts from this instant. A ``spec`` with no
        region, or one whose profile has nothing to watch, leaves the monitor
        configured but idle (``latest`` stops advancing)."""
        ...

    async def watch(self, slot: AgentSlot) -> Watched:
        """Point the monitor at the window it keeps for ``slot``, and say what
        it settled on - the ONLY retarget a remote brain has (§10.5).

        The inversion wave 3 made: the brain no longer composes a spec and sends
        it, because the service, its preset and its chat region are all facts
        about the monitor's machine and the monitor is where they are edited. So
        the brain names a WINDOW - master or sub-agent - and the monitor runs
        its own configuration for it, bumps its generation, and hands the whole
        effective service back as a :class:`Watched`.

        Equivalent to ``configure(its own spec for slot)`` followed by
        ``watched()``, and that is exactly what ``LocalUIMonitor`` does - but
        ONE round trip, because the brain needs both halves before it can act
        and a redial that got only the generation would be driving a service it
        had not been told about.

        A monitor with no configuration for ``slot`` answers its current
        :class:`Watched` unchanged and bumps nothing: "there is nothing over
        here to watch" is an answer, not a failure, and the brain reads it off
        ``service`` / ``region`` exactly as it reads an uncalibrated one.
        """
        ...

    async def watched(self) -> Watched:
        """What the last retarget settled on: the service key, the box
        actually watched (the spec's, or the store's - §9.1), and whether this
        machine has a profile for that key.

        The brain has to ask, because both the store and the profile are on
        the monitor's disk: a Chat UI on another machine holds no rectangle of
        its own, every recipe that says "no chat window is drawn" is reading
        the brain's calibration, and a service key that names nothing over
        there is otherwise a silent NOT_CALIBRATED on every click.
        """
        ...

    async def suspend(self) -> None:
        """Stop polling without bumping the generation - a capture overlay is
        about to own the screen and nothing has moved."""
        ...

    async def resume(self) -> None:
        """Poll again under the same configuration; a no-op while polling."""
        ...

    async def close(self) -> None:
        """Stop every thread/task for good. Idempotent."""
        ...

    async def set_theme(self, theme: str) -> None:
        """Wear the attached brain's palette (§11.7).

        The one verb on this Protocol that is about NOTHING on the screen being
        watched: it is the Chat UI telling the machine it is driving which
        appearance the user picked, so the Monitor UI's window stops being the
        one dark rectangle in a light desktop. A monitor with no window of its
        own (the ``--headless`` door) stores it and paints nothing, which is why
        this returns nothing and cannot fail: a theme that did not land is not a
        thing a brain may act on.

        Sent twice by design - in the ``hello`` at connect time, so a window is
        already right the moment somebody attaches, and as this verb whenever
        the user changes it while the link is up.
        """
        ...

    # -- observation (local reads; no round trip) --------------------------
    @property
    def generation(self) -> int: ...

    @property
    def latest(self) -> Tick | None:
        """The newest non-ghost tick, or ``None`` before the first one."""
        ...

    async def observe(self) -> Tick:
        """The next tick captured AFTER this call - never the cached one."""
        ...

    def subscribe(self, hook: TickHook) -> Callable[[], None]:
        """Every non-ghost tick, as it lands; returns the unsubscribe."""
        ...

    def on_clip(self, hook: ClipHook) -> Callable[[], None]:
        """Every clipboard change the watcher accepts (never the monitor's own
        writes); returns the unsubscribe."""
        ...

    # -- actions (round trips) ---------------------------------------------
    async def focus_window(self, handle: int) -> bool: ...
    async def foreground_window(self) -> int | None: ...
    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool: ...
    async def move_cursor(self, x: int, y: int) -> bool: ...
    async def scroll(self, region: ScreenRegion, detents: int) -> bool: ...
    async def scroll_key(self, key: str, taps: int = 1) -> bool: ...
    async def send_paste(self) -> bool: ...
    async def send_enter(self) -> bool: ...
    async def read_clipboard(self) -> str | None: ...
    async def write_clipboard(self, text: str) -> None: ...

    # -- the pixel verdicts (§2.3) -----------------------------------------
    # Everything below looks at the screen and answers about it. Each takes a
    # fresh capture of the CONFIGURED region and searches it with the CONFIGURED
    # profile - a caller names a kind and never a template, a rectangle or a
    # tolerance, because none of those could cross the wire (§2.10: the PNGs
    # stay on the monitor's machine).
    #
    # None of them raises, ever. A monitor with no spec, no region or no profile
    # for its service answers the empty answer - ``()``, a ``Located`` with no
    # region, ``MISMATCH``, ``None`` - because "there is nothing to look at" and
    # "it is not on screen" are the same fact to a caller that may not click
    # either way, and a capture that failed is the same fact again.

    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]:
        """Every place ``kind`` is on screen right now, in absolute coordinates.

        All of them rather than the first, with near-duplicate hits on one
        physical element folded away, so a tuple longer than one really does
        mean two elements - which is the question that matters: an appearance
        belongs to the SERVICE, so a second window of it inside the drawn region
        carries the same button.
        """
        ...

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        """The bottom-most ``kind`` on one fresh frame, and what else that frame
        says about it (:class:`Located`).

        The verb behind "where do I click, and may I?": one capture answers where
        the newest one is, whether there were several, and - when there were
        none - how close the search came.

        ``exclude_kinds`` names appearances the answer may NOT be. A match that
        lands on one of them is refused (no region), because the caller asked for
        a thing that is not that thing: two kinds of one service can share pixels
        (a button and the toolbar it sits in), and a hit that is really the other
        one is a click on the wrong control. Searched in the same frame, so the
        exclusion describes the same instant as the match it vetoes; empty by
        default, and then nothing extra is searched at all.
        """
        ...

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        """Find ``kind`` inside the configured region right now, and click it.

        The primitive every programmatic click on a service appearance goes
        through. It replaces "click where those pixels used to be" with "click
        where they *are*", which is both safer and the reason the browser may
        move: a page that re-laid itself out, scrolled or opened a dialog simply
        reads as not-on-screen and gets no click at all. Refusing is always the
        safe answer - the user can click it themselves.

        Where inside the matched rectangle the click lands is the service's own
        click point, because the middle of a control is only the right pixel
        until a service draws one whose middle is a label.

        Only the four pixel verdicts (:class:`ElementClick`): MISMATCH,
        AMBIGUOUS, CLICKED, NOT_CLICKED. ``settle_s`` is the hover pause before
        the press; None means the monitor's own default.
        """
        ...

    async def hover_scan(self, kind: TemplateKind) -> Located:
        """Walk the real cursor up the configured region and stop at the FIRST
        frame ``kind`` appears in - or a :class:`Located` with no region if it
        never does.

        A ``Located`` rather than a rectangle so that the click which follows a
        hover is aimed by the same point as the click which follows a static
        search (§11.3): ``target`` is set, and the other two fields are the
        answers a walk cannot give - it stops at the first frame it sees the
        thing in, so it never counts a second one (``ambiguous`` is always
        False) and it judges no candidate it could report a diff for
        (``best_miss`` is always None).

        Some chats only render a response's copy button while the pointer is
        over that response, so the cheap static capture finds nothing there no
        matter how good the template is. Bottom-up, because the newest response
        is at the bottom, so the usual answer is one or two stops in.

        SLOW by nature - a cursor move, a settle pause and a capture per stop -
        and it moves the user's real mouse, which is why the brain asks for it
        only when a service opted in (``MonitorSpec.hover_scan``) and only after
        a static search came up empty.

        Any failure ends the scan and reads as "not found": a scan that cannot
        see is not a scan that found nothing, and both mean "do not click".
        """
        ...

    async def snap_to_bottom(self, action: str) -> None:
        """Scroll the configured region to its bottom, the way ``action`` says.

        One of the three scroll actions a service can be set to (``config``'s
        ``SCROLL_*``): a burst of Page Down taps, one End, or a long wheel
        flick. Its own verb rather than three calls at the call site because a
        caller does it repeatedly and the three branches must not drift apart
        between rounds - a retry that quietly used the wheel on a page whose
        preset says End would be a retry of something else.

        Deliberately *only* the scroll. The focus click before it and the settle
        after it are the caller's: one is choreography that happens once, the
        other is paid per round.
        """
        ...

    # -- the clipboard watcher ---------------------------------------------
    # Sync, alone among the verbs below, because every caller is: the armed
    # switch, a session starting and a session ending are all UI-thread acts
    # that return a state the shell paints from (``set_os_armed``), and making
    # them coroutines would ripple an ``await`` into every one of those call
    # sites for a call that only raises or lowers a flag. Idempotent and
    # thread-safe for the same reason.

    def watch_clipboard(self, on: bool) -> bool:
        """Start or stop the clipboard watcher; returns whether one is polling
        now. ``False`` for ``on=False``, and also for an ``on=True`` there is
        nothing to honour - no backend at all, or a write-only ("manual") one."""
        ...

    @property
    def clipboard_kind(self) -> str | None:
        """Which backend is behind ``read_clipboard`` / ``write_clipboard`` /
        ``watch_clipboard``: the provider's name, ``"manual"`` for the
        write-only sentinel, ``None`` when there is no backend at all. A shell
        tells the last two apart - manual mode is explained to the user, and no
        backend at all was never promised anything."""
        ...
