"""What the brain needs of the machine, and what it does without one.

Two things live here, both of them wiring rather than decision.

:class:`MonitorLike` is the :class:`~agentclip.driver.monitor.protocol.UIMonitor`
contract as the automation names it at its own call site: the whole wire-able
half, plus the two local-only members a ``RemoteUIMonitor`` will never answer -
the frame hook (pixels, so they never cross, docs/design/ui-monitor.md §2.2) and
the self-write register (which lives with the clipboard it is about, §2.11).
Declared on this side of the seam rather than in ``driver/monitor`` because it is
the CALLER's requirement and not the wire's, and spelled out once so mypy checks
one Protocol instead of two.

The rest is the answers a controller nobody wired anything into gives: no
clipboard filter, no capture sink, no crop renderer, no captured appearances.
Each is the honest reading of "no shell", and each makes the recipes refuse
rather than guess.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.clip.watcher import SelfWriteSet
from agentclip.driver.monitor.protocol import Located, MonitorSpec, Tick
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import Sighting
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion

# The tick's recognitions, cut down to pictures. Sizing a crop depends on which
# renderer the shell can drive, so the CUT is the shell's - but it happens on the
# monitor's thread, the one that captured the frame.
CropFn = Callable[
    [RegionImage, Mapping[TemplateKind, Sighting | None]], Mapping[TemplateKind, object]
]
# One captured frame and what was recognised in it: the monitor's local-only
# frame hook, delivered beside the tick rather than inside it.
FrameHook = Callable[[RegionImage, Mapping[TemplateKind, Sighting | None]], None]


class MonitorLike(Protocol):
    """The monitor as the automation asks for it (see the module docstring)."""

    # -- lifecycle / configuration --------------------------------------------
    @property
    def spec(self) -> MonitorSpec | None: ...
    @property
    def generation(self) -> int: ...
    async def configure(self, spec: MonitorSpec) -> int: ...
    async def suspend(self) -> None: ...
    async def resume(self) -> None: ...

    # -- observation ----------------------------------------------------------
    @property
    def latest(self) -> Tick | None: ...
    async def observe(self) -> Tick: ...
    def subscribe(self, hook: Callable[[Tick], None]) -> Callable[[], None]: ...
    def on_clip(self, hook: Callable[[str], None]) -> Callable[[], None]: ...

    # -- actions --------------------------------------------------------------
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
    def watch_clipboard(self, on: bool) -> bool: ...
    @property
    def clipboard_kind(self) -> str | None: ...

    # -- the pixel verdicts (§2.3): a kind and nothing else -------------------
    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]: ...
    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located: ...
    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick: ...
    async def hover_scan(self, kind: TemplateKind) -> Located: ...
    async def snap_to_bottom(self, action: str) -> None: ...

    # -- the local-only tier --------------------------------------------------
    @property
    def self_writes(self) -> SelfWriteSet: ...
    def on_frame(self, hook: FrameHook) -> Callable[[], None]: ...
    def reset_trackers(self) -> None: ...


def accept_all(_text: str) -> bool:
    """Watcher filter for a controller nobody handed one to."""
    return True


def drop_capture(_text: str) -> None:
    """Capture sink for a controller nobody handed one to."""


def uncut(
    _scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
) -> Mapping[TemplateKind, object]:
    """Crop function for a controller nobody handed one to: the sightings
    themselves, so a view with no renderer still sees what was recognised."""
    return dict(sightings)


def nothing_captured(_kind: TemplateKind) -> bool:
    """Appearance lookup for a controller nobody handed one to: a service with no
    captures at all - no send gate, and a finish that lands on MANUAL_COPY."""
    return False
