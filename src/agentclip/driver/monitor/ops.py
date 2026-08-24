"""The monitor's own hand on the machine.

:class:`ScreenOps` is the handful of :mod:`agentclip.driver.screen` calls the
OS-acting sequences make - capture a rectangle, click one, move the pointer,
turn the wheel, tap a scroll key, bring a window back, search a frame, drop a
synthetic Ctrl+V or Enter into whatever has focus - plus the beats and the one
size those sequences pace themselves by. It is the monitor's OS adapter
(docs/design/ui-monitor.md §3): nothing outside ``agentclip.driver.monitor``
imports it, and it never crosses to a shell.

It exists as an OBJECT rather than as bare module calls so a suite can hand the
monitor a substitute whose methods record instead of act. The signatures are
deliberately the ones the underlying functions have, down to ``click``'s
optional settle: a caller that passes no settle must reach ``click_region``
with no settle argument, because that is the call the suites' stubs are shaped
for.
"""

from __future__ import annotations

from agentclip.driver.clip.chunking import STREAM_CHUNK_CHARS
from agentclip.driver.monitor.beats import (
    ACTIVATION_ATTEMPTS,
    ACTIVATION_POLL_S,
    FOCUS_CLICK_GAP_S,
    NEW_CHAT_SETTLE_S,
    PASTE_SETTLE_DELAY,
    SNAP_BACK_SETTLE_S,
    STREAM_CHUNK_SETTLE_S,
    SUBMIT_SETTLE_S,
)
from agentclip.driver.screen.capture import RegionImage, capture_region
from agentclip.driver.screen.focus import (
    click_region,
    focus_window_verified,
    foreground_window,
    move_cursor,
    scroll_region,
    send_enter,
    send_paste,
    send_scroll_key,
)
from agentclip.driver.screen.hover import STEP_DELAY_S
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import (
    CandidateSource,
    RegionMatch,
    Template,
    find_all_in_region,
    find_lowest_with_best_miss,
)


class ScreenOps:
    """Every OS call one automation sequence makes, in one substitutable place."""

    def capture(self, region: ScreenRegion) -> RegionImage:
        """One frame of ``region``. Raises ``CaptureError`` like the real thing."""
        return capture_region(region)

    def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        """Click ``region``'s centre. ``None`` means "however long the primitive
        settles by default" and reaches it as no argument at all."""
        return click_region(region) if settle_s is None else click_region(region, settle_s=settle_s)

    def move_cursor(self, x: int, y: int) -> bool:
        """A real synthetic pointer MOVE - the one a browser's hover chain sees."""
        return move_cursor(x, y)

    def scroll(self, region: ScreenRegion, detents: int) -> bool:
        """Turn the wheel over ``region``."""
        return scroll_region(region, detents)

    def scroll_key(self, key: str, taps: int = 1) -> bool:
        """Send ``taps`` of a scroll key ("page_down" / "end") to whatever has focus."""
        return send_scroll_key(key, taps)

    def focus_window(self, handle: int) -> bool:
        """Bring a window back, verified and retried (``focus_window_verified``)."""
        return focus_window_verified(handle)

    def foreground_window(self) -> int | None:
        """Whose window holds the foreground right now, or None (unsupported
        platform, or mid focus switch). The read the delivery's activation wait
        is built on: not ours any more means the click's activation landed."""
        return foreground_window()

    def send_paste(self) -> bool:
        """A synthetic Ctrl+V into whatever has focus. Un-aimed on purpose - the
        caller has just clicked the chat box and waited out the activation."""
        return send_paste()

    def send_enter(self) -> bool:
        """A synthetic Enter, the opt-in auto-submit tap
        (``ServicePreset.auto_submit``). Un-aimed for the same reason."""
        return send_enter()

    def lowest_match(
        self,
        template: Template,
        scene: RegionImage,
        *,
        tolerance: int,
        max_diff: float,
        matcher: CandidateSource | None,
    ) -> tuple[RegionMatch | None, float | None]:
        """The bottom-most match of one image in one frame, plus the closest miss."""
        return find_lowest_with_best_miss(
            template, scene, tolerance=tolerance, max_diff=max_diff, matcher=matcher
        )

    def all_matches(
        self,
        template: Template,
        scene: RegionImage,
        *,
        tolerance: int,
        max_diff: float,
        limit: int,
        matcher: CandidateSource | None,
    ) -> list[RegionMatch]:
        """Every match of one image in one frame, in reading order, at most
        ``limit`` of them - the other half of the search model
        (``flow.element_rects``)."""
        return find_all_in_region(
            template,
            scene,
            tolerance=tolerance,
            max_diff=max_diff,
            limit=limit,
            matcher=matcher,
        )

    def hover_step_delay(self) -> float:
        """How long the hover scan lets a page paint before it looks again.

        A call rather than a constant so a shell can shrink it per run - which
        is what the Pilot suites do to keep from sleeping their way up a region.
        """
        return STEP_DELAY_S

    def new_chat_settle(self) -> float:
        """How long a fresh browser chat is given to render its input box before
        the automation treats it as the live window. A call for the same reason
        as ``hover_step_delay``."""
        return NEW_CHAT_SETTLE_S

    # -- the delivery's own cadence -------------------------------------------
    # Calls for the same reason as the two above and no other: the suites shrink
    # them, because a file full of paste tests that each waited out a real focus
    # activation would be a file nobody runs. What each one is FOR is documented
    # where its default lives (agentclip.driver.automation.delivery).

    def activation_attempts(self) -> int:
        """How many times the delivery asks who holds the foreground before it
        stops waiting for the browser's activation and pastes anyway."""
        return ACTIVATION_ATTEMPTS

    def activation_poll(self) -> float:
        """The beat between two of those askings."""
        return ACTIVATION_POLL_S

    def focus_click_gap(self) -> float:
        """The beat between the two clicks of the pre-paste focus click - long
        enough that the woken window is ready for the second one, and past the
        OS double-click threshold so the pair is two single clicks."""
        return FOCUS_CLICK_GAP_S

    def paste_settle(self) -> float:
        """How long the focused chat box is given to take a caret before the
        Ctrl+V - the in-page half of the wait, after the activation half."""
        return PASTE_SETTLE_DELAY

    def snap_back_settle(self) -> float:
        """How long a click in the browser is given to register before the
        foreground is handed back to our own window."""
        return SNAP_BACK_SETTLE_S

    def submit_settle(self) -> float:
        """How long the pasted box is given to re-measure before the Enter tap."""
        return SUBMIT_SETTLE_S

    def stream_chunk_settle(self) -> float:
        """How long a streamed delivery waits between two bursts."""
        return STREAM_CHUNK_SETTLE_S

    def stream_chunk_chars(self) -> int:
        """How big one burst of a streamed delivery is."""
        return STREAM_CHUNK_CHARS
