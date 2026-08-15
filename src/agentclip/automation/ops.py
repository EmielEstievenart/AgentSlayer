"""The automation's own hand on the machine, and what one click can come to.

:class:`ScreenOps` is the handful of :mod:`agentclip.screen` calls the OS-acting
sequences in :class:`~agentclip.automation.controller.AutomationController` make
- capture a rectangle, click one, move the pointer, turn the wheel, tap a
scroll key, bring a window back, search a frame, drop a synthetic Ctrl+V or
Enter into whatever has focus - plus the beats and the one size those sequences
pace themselves by. It is emphatically **not** part of the ``AutomationView``
port: OS primitives do not cross to a shell (docs/design/gui.md §1), and the
default implementation below simply calls ``agentclip.screen`` directly, which
is what "the controller imports the screen layer" means in practice.

It exists as an OBJECT rather than as bare module calls for one reason, and it
is the same one slice 4 gave ``_poll_capture``: the Textual suites monkeypatch
these primitives at ``agentclip.tui.screens.main``'s scope, because that is
where they were imported when this code lived on the screen. A shell may
therefore hand in a subclass whose methods resolve ITS module's names per call,
and the sequence it drives is then stubbed exactly as it always was. Nothing
else is meant to substitute this - a second shell gets the default.

The signatures are deliberately the ones the underlying functions have, down to
``click``'s optional settle: a caller that passes no settle must reach
``click_region`` with no settle argument, because that is the call the suites'
stubs are shaped for.
"""

from __future__ import annotations

from enum import Enum

from agentclip.automation.delivery import (
    PASTE_SETTLE_DELAY,
    STREAM_CHUNK_SETTLE_S,
    SUBMIT_SETTLE_S,
)
from agentclip.clip.chunking import STREAM_CHUNK_CHARS
from agentclip.screen.capture import RegionImage, capture_region
from agentclip.screen.focus import (
    click_region,
    focus_window_verified,
    move_cursor,
    scroll_region,
    send_enter,
    send_paste,
    send_scroll_key,
)
from agentclip.screen.hover import STEP_DELAY_S
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import (
    CandidateSource,
    RegionMatch,
    Template,
    find_all_in_region,
    find_lowest_with_best_miss,
)

# Beat between opening a fresh browser chat and treating it as the live slot -
# the page still has to render its (centred) input box.
NEW_CHAT_SETTLE_S = 0.4


class ElementClick(Enum):
    """Outcome of the find-then-click primitive
    (``AutomationController.click_profile_element``).

    Six states, not a bool, because the five failures are five different
    things to tell the user: the app is disarmed and may not click anything at
    all (DISARMED - the user's own switch, and the only one that is not about
    this element), nothing to look for or nowhere to look (NOT_CALIBRATED - go
    capture it), it is simply not on screen (MISMATCH - nothing was clicked, and
    clicking blind is never the answer), it is on screen in more than one place
    (AMBIGUOUS - which is not a search failure but a *drawing* failure, and the
    fix is to redraw the window), or we clicked and the OS refused (NOT_CLICKED
    - Windows-only input).
    """

    CLICKED = "clicked"
    MISMATCH = "mismatch"  # not on screen right now; refused to click
    AMBIGUOUS = "ambiguous"  # several of them in the region; refused to guess
    NOT_CLICKED = "not_clicked"  # found fine, but the click did not land
    NOT_CALIBRATED = "not_calibrated"  # no chat region drawn, or nothing captured
    DISARMED = "disarmed"  # the OS-acting switch is off; nothing was looked at


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
    # where its default lives (agentclip.automation.delivery).

    def paste_settle(self) -> float:
        """How long the focus click is given to take effect before the Ctrl+V."""
        return PASTE_SETTLE_DELAY

    def submit_settle(self) -> float:
        """How long the pasted box is given to re-measure before the Enter tap."""
        return SUBMIT_SETTLE_S

    def stream_chunk_settle(self) -> float:
        """How long a streamed delivery waits between two bursts."""
        return STREAM_CHUNK_SETTLE_S

    def stream_chunk_chars(self) -> int:
        """How big one burst of a streamed delivery is."""
        return STREAM_CHUNK_CHARS
