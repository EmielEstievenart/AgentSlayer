"""Pilot tests for the transcript's fit-or-park auto-scroll.

The contract (transcript.py ``_autoscroll``): an event that fits the visible
area pins the panel to the bottom; an event taller than the visible area parks
the view with the event's top at the top so reading starts at the first line;
while parked, follow-up noise (notes, calls) must not move the view, but a new
conversational beat (user/assistant message) re-applies the fit rule, and
returning to the bottom resumes pinning.

And the rule the per-event one cannot express (``begin_reply``/``reveal_reply``):
one ingested reply is several events, so the same fit-or-park question is asked
once more about the reply as a whole - a reply too tall for the view is revealed
from its FIRST event, a reply that fits leaves the panel at the bottom, still
following.

The panel is exercised in a bare single-widget app: scroll geometry is the
whole point here, so the harness gives the panel the full screen instead of
the real MainScreen chrome.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.pilot import Pilot

from agentclip.protocol.types import ToolCall
from agentclip.shell.tui.widgets.transcript import TranscriptPanel

TALL_TEXT = "\n\n".join(f"paragraph {i}" for i in range(60))  # ~120 rows rendered
SHORT_TEXT = "just one line"


def _call(index: int) -> ToolCall:
    path = f"src/mod{index}.py"
    return ToolCall(
        id=index,
        tool="read_file",
        params={"path": path},
        raw=f"CALL read_file\npath: {path}\nEND",
    )


async def _ingest_reply(panel: TranscriptPanel, prose: str, calls: int) -> None:
    """One reply as the controller writes it: bracketed prose plus tool calls."""
    panel.begin_reply()
    await panel.add_prose(prose)
    for index in range(calls):
        await panel.add_call(_call(index))
    panel.reveal_reply()


class _PanelApp(App[None]):
    # The two rules the real AgentClipApp.CSS applies that scroll geometry
    # depends on: the panel fills the screen, events take their natural height.
    CSS = "TranscriptPanel { height: 1fr; } TranscriptPanel > * { height: auto; }"

    def compose(self) -> ComposeResult:
        yield TranscriptPanel(id="transcript")


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _panel(app: _PanelApp) -> TranscriptPanel:
    return app.query_one(TranscriptPanel)


def _at_bottom(panel: TranscriptPanel) -> bool:
    return panel.max_scroll_y > 0 and panel.scroll_y >= panel.max_scroll_y - 0.5


def _parked_at(panel: TranscriptPanel, index: int) -> bool:
    """The view's top row is the top of the index-th event widget."""
    widget = panel.children[index]
    return abs(panel.scroll_y - widget.virtual_region.y) <= 1


async def test_streaming_small_events_stays_pinned_to_the_bottom() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        for i in range(40):
            await panel.add_note(f"note {i}")
        await _wait_for(pilot, lambda: _at_bottom(panel), "panel pinned to the bottom")


async def test_tall_response_parks_with_its_top_at_the_top() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        for i in range(30):  # enough to overflow the 24-row screen
            await panel.add_note(f"note {i}")
        await _wait_for(pilot, lambda: _at_bottom(panel), "pinned before the tall response")

        await panel.add_prose(TALL_TEXT)
        await _wait_for(pilot, lambda: _parked_at(panel, -1), "parked at the response top")
        assert not _at_bottom(panel)


async def test_follow_up_noise_holds_the_parked_position() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        await panel.add_prose(TALL_TEXT)
        await _wait_for(pilot, lambda: _parked_at(panel, 0), "parked at the response top")
        parked_y = panel.scroll_y

        await panel.add_note("tool call landed below")
        await panel.add_note("outbound copied")
        await pilot.pause(0.3)
        assert panel.scroll_y == parked_y  # the view did not move


async def test_new_fitting_response_repins_to_the_bottom() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        await panel.add_prose(TALL_TEXT)
        await _wait_for(pilot, lambda: _parked_at(panel, 0), "parked at the response top")

        await panel.add_prose(SHORT_TEXT)  # a new beat that fits -> bottom
        await _wait_for(pilot, lambda: _at_bottom(panel), "re-pinned by the fitting response")


async def test_scrolling_back_to_the_bottom_resumes_pinning() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        await panel.add_prose(TALL_TEXT)
        await _wait_for(pilot, lambda: _parked_at(panel, 0), "parked at the response top")

        panel.scroll_end(animate=False)  # the user finished reading
        await _wait_for(pilot, lambda: _at_bottom(panel), "user at the bottom")

        await panel.add_note("next event")
        await _wait_for(
            pilot,
            lambda: panel._reading is False and _at_bottom(panel),
            "pinning resumed at the bottom",
        )


async def test_an_ingested_reply_of_small_events_is_revealed_from_its_first_line() -> None:
    """The bug the brackets exist for: prose plus a dozen tool calls, none of
    them taller than the view, used to leave the panel pinned at the LAST call
    with the model's opening sentence scrolled off the top."""
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        for i in range(30):  # a conversation already scrolled past the top
            await panel.add_note(f"note {i}")
        await _wait_for(pilot, lambda: _at_bottom(panel), "pinned before the reply")

        await _ingest_reply(panel, SHORT_TEXT, calls=15)

        await _wait_for(pilot, lambda: _parked_at(panel, 30), "revealed at the reply's first event")
        assert not _at_bottom(panel)


async def test_a_revealed_reply_holds_its_position_under_follow_up_noise() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        for i in range(30):
            await panel.add_note(f"note {i}")
        await _ingest_reply(panel, SHORT_TEXT, calls=15)
        await _wait_for(pilot, lambda: _parked_at(panel, 30), "revealed at the reply's first event")
        revealed_y = panel.scroll_y

        await panel.add_note("→ results copied (900 chars)")
        await pilot.pause(0.3)
        assert panel.scroll_y == revealed_y


async def test_a_reply_that_fits_leaves_the_panel_following_the_bottom() -> None:
    """The other half of the reveal rule, and the reason it is a rule and not
    an unconditional park: a short reply is on screen entire from the bottom
    either way, so parking it would win nothing and would stop the transcript
    following - every event of the next turn would land below the fold."""
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        for i in range(30):  # a conversation already scrolled past the top
            await panel.add_note(f"note {i}")
        await _wait_for(pilot, lambda: _at_bottom(panel), "pinned before the reply")

        await _ingest_reply(panel, SHORT_TEXT, calls=1)

        await _wait_for(pilot, lambda: _at_bottom(panel), "still following after the short reply")
        assert panel._reading is False
        # And it keeps following: the next event is not held off screen.
        await panel.add_note("→ results copied (900 chars)")
        await _wait_for(pilot, lambda: _at_bottom(panel), "the follow-up event stayed visible")


async def test_a_tall_reply_is_revealed_where_its_own_park_left_it() -> None:
    """The single-event park and the reveal must not fight: both put the
    reply's first line at the first row, so the view lands there once."""
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        for i in range(30):
            await panel.add_note(f"note {i}")

        await _ingest_reply(panel, TALL_TEXT, calls=3)

        await _wait_for(pilot, lambda: _parked_at(panel, 30), "revealed at the reply's first event")


async def test_clear_resets_the_reading_park() -> None:
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = _panel(app)
        await panel.add_prose(TALL_TEXT)
        await _wait_for(pilot, lambda: _parked_at(panel, 0), "parked at the response top")

        await panel.clear_events()
        assert panel._reading is False

        for i in range(30):
            await panel.add_note(f"after reset {i}")
        await _wait_for(pilot, lambda: _at_bottom(panel), "pinned again after the reset")
