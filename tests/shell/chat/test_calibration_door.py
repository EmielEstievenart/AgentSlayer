"""The chat GUI's door onto the CALIBRATION WINDOW (ui-monitor.md 2.6, 6.4).

Everything made of pixels - the service editor, the ELEMENTS column, the
chat-region picker and ``/identify`` - is a second pywebview window now, with a
page, a bridge and a monitor of its own. What is left in this shell is a door
and a bracket, and that is exactly what is pinned here:

* one window at a time, and a second press says so rather than opening another;
* the chat shell's OWN detectors are suspended for the whole visit and resumed
  when it closes - that window is nothing but fullscreen capture overlays thrown
  over the very browser these detectors watch;
* the window is built over a monitor of its own, never this shell's;
* the two answers that come back (a chat region drawn, a config saved) land the
  way the in-window picker's used to.

No toolkit is imported and no window is created: ``GuiView`` reaches pywebview
through one injected callable, and the harness records what it was asked for
(tests/shell/chat/conftest.py). The window's own surface is pinned one directory
down, in ``tests/shell/monitor_ui/``.
"""

from __future__ import annotations

import asyncio

import pytest

from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.chat.view import CALIBRATION_OPEN, GuiView
from agentclip.shell.webview.bridge import JsApi
from tests.shell.chat.conftest import Harness, settle

pytestmark = pytest.mark.asyncio


def on_a_real_loop(view: GuiView) -> None:
    """Let this view's coroutines actually run.

    The harness closes what it is handed, which is right for every other test
    here (nothing is asserted about what a flow DID). This one is about
    ``suspend``/``resume``, which are coroutines on the monitor, so the counting
    only works if they are awaited.
    """
    view._schedule = asyncio.ensure_future  # type: ignore[assignment]


async def test_f2_opens_the_window_once_and_suspends_the_chat_monitor(
    harness: Harness,
) -> None:
    view = harness.view
    on_a_real_loop(view)

    view.open_calibration()
    await settle()

    assert len(harness.calibrations) == 1
    # The bracket, from this shell's side: the poller stops for the whole visit.
    assert [name for name, _ in harness.monitor.calls] == ["suspend"]


async def test_a_second_press_is_a_toast_not_a_second_window(harness: Harness) -> None:
    """Two windows over one screen would each be throwing fullscreen overlays at
    the same browser, and the suspend above is a bracket rather than a count."""
    view = harness.view
    on_a_real_loop(view)
    view.open_calibration()
    await settle()
    harness.flush().clear()

    view.open_calibration()
    await settle()

    assert len(harness.calibrations) == 1
    assert harness.flush().last("toast")["message"] == CALIBRATION_OPEN
    assert [name for name, _ in harness.monitor.calls] == ["suspend"]


async def test_closing_the_window_resumes_the_chat_monitor_and_reopens_the_door(
    harness: Harness,
) -> None:
    view = harness.view
    on_a_real_loop(view)
    view.open_calibration()
    await settle()

    harness.close_calibration()  # pywebview's `closed`, from the window's thread
    await settle()

    assert view._calibration is None
    assert [name for name, _ in harness.monitor.calls][:2] == ["suspend", "resume"]

    view.open_calibration()
    await settle()
    assert len(harness.calibrations) == 2


async def test_the_window_runs_over_a_monitor_of_its_own(harness: Harness) -> None:
    """Never this shell's and never the controller's (6.4). Two readers of one
    screen is fine; one monitor with two owners of its generation is not."""
    view = harness.view
    on_a_real_loop(view)

    view.open_calibration()
    await settle()

    runner = harness.calibrations[0]
    assert runner.view._monitor is not harness.monitor
    # ...and it borrows this shell's loop rather than starting a second one.
    assert runner.start() is None
    assert runner._owns_loop is False


async def test_the_js_api_and_the_key_reach_the_same_door(harness: Harness) -> None:
    """F2, the titlebar button and both sidebar buttons all send ``calibrate``,
    and the bridge marshals it onto the one call."""
    view = harness.view
    on_a_real_loop(view)

    JsApi(view).calibrate()
    await settle()

    assert len(harness.calibrations) == 1


async def test_a_region_drawn_over_there_lands_here(harness: Harness) -> None:
    """The picker's answer, minus the picker: adopt the box, repaint the column
    that describes that tab, and rebuild the poller when it is the live one."""
    view = harness.view
    on_a_real_loop(view)
    view.open_calibration()
    await settle()
    harness.flush().clear()
    region = ScreenRegion(10, 20, 300, 400)

    view._calibrated(AgentSlot.MASTER, region)
    await settle()

    assert view.automation.calibration(AgentSlot.MASTER).chat_region == region
    assert region.describe() in harness.flush().last("sidebar")["region"]


async def test_identify_opens_the_window_it_is_drawn_from(harness: Harness) -> None:
    """``/identify`` is still a command with an answer - the overlay just lives
    in the other window now, next to the ELEMENTS column."""
    view = harness.view
    on_a_real_loop(view)

    view.show_identify_overlay()
    await settle()

    assert len(harness.calibrations) == 1
    assert not harness.flush().of_type("toast")
