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
  predates the last :meth:`UIMonitor.configure`.

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

from agentclip.driver.screen.busy import BusyProbe
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
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
class Located:
    """One search's whole answer: where, whether there were several, how close.

    Three fields because a caller that has only the first one cannot tell the
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
    """

    region: ScreenRegion | None
    ambiguous: bool
    best_miss: float | None


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

    Plain data, every field a scalar or a tuple of scalars, so it rides over
    the wire in phase 5 unchanged. The template PNGs never do: ``service`` is
    the profile KEY, and the monitor resolves it on its own machine.

    ``stable_seconds`` is raw seconds - the monitor converts it against its own
    tick rate (§2.10 "cadence moves to the monitor"); no caller computes ticks.
    The four ``send_*`` fields are the send gate's tick budgets from
    ``driver/automation/finish.py``, handed over here so the monitor's tick
    count is the one they are measured in.
    """

    service: str
    region: ScreenRegion | None
    finish_signals: tuple[str, ...]
    stable_seconds: float
    tolerance: int
    matcher: str
    hover_scan: bool
    scroll_action: str
    snap_back: bool
    delivery: str
    auto_submit: bool
    send_arm_min_diff: float
    send_arm_ticks: int
    send_gate_timeout_ticks: int
    send_gate_seen_timeout_ticks: int


TickHook = Callable[[Tick], None]
ClipHook = Callable[[str], None]


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
        """Retarget onto ``spec``. Rebuilds trackers fresh (never mutates the
        old ones), bumps and returns the generation; ticks captured under an
        older generation are ghosts from this instant. A ``spec`` with no
        region, or one whose profile has nothing to watch, leaves the monitor
        configured but idle (``latest`` stops advancing)."""
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

    async def hover_scan(self, kind: TemplateKind) -> ScreenRegion | None:
        """Walk the real cursor up the configured region and stop at the FIRST
        frame ``kind`` appears in - or None if it never does.

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
