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
    async def start_clip_watch(self) -> None:
        """Run the clipboard watcher (idempotent)."""
        ...

    async def stop_clip_watch(self) -> None: ...
