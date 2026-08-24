"""The suite-wide OS-input gate, tested from the outside.

``tests/conftest.py::_no_real_os_input`` is the one fixture whose failure is
silent and expensive: nothing goes red, a Ctrl+V just lands in the user's
editor. So it gets tests of its own - each one calls the real
``agentclip.driver.screen.focus`` entry point (not a patched alias) and asserts the
gate answered instead of the desktop. The Linux branch gets the same treatment
one section down, simulated with a monkeypatched ``sys.platform`` so it is
covered from the Windows machine this suite usually runs on.

These call the OS layer unmocked on purpose, which means they must NOT run when
the gate is disarmed - they would become exactly the thing they guard against.
"""

from __future__ import annotations

import os
import sys

import pytest

from agentclip.driver.screen import focus, overlay, picker, x11
from agentclip.driver.screen.region import ScreenRegion

REGION = ScreenRegion(40, 40, 20, 20)

# Captured at import time, i.e. during collection - before any fixture runs.
REAL_PICK_REGION = picker.pick_region
REAL_IDENTIFY_OVERLAY = picker.draw_identify_overlay
# The X11 backend's two injection seams, same rule. Importing the module costs
# nothing anywhere: it reaches python-xlib only inside its functions.
REAL_FAKE_INPUT = x11._fake_input
REAL_ACTIVATE = x11._activate

gated_only = pytest.mark.skipif(
    os.environ.get("AGENTCLIP_OS_TESTS") == "1",
    reason="AGENTCLIP_OS_TESTS=1 disarms the gate - these calls would hit the real desktop",
)
windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="ctypes.windll, and the gate's choke point, are Windows-only"
)


@gated_only
def test_paste_never_reaches_the_keyboard() -> None:
    """The headline case: a real Ctrl+V typed into the test runner's window."""
    assert focus.send_paste() is False


@gated_only
def test_clicking_never_reaches_the_mouse() -> None:
    assert focus.click_region(REGION) is False


@gated_only
def test_scrolling_never_reaches_the_wheel() -> None:
    assert focus.scroll_region(REGION, -3) is False


@gated_only
def test_enter_never_reaches_the_keyboard() -> None:
    """The auto-submit tap: a real one would SEND whatever sits half-typed in
    the user's focused window."""
    assert focus.send_enter() is False


@gated_only
def test_scroll_keys_never_reach_the_keyboard() -> None:
    assert focus.send_scroll_key("page_down", 8) is False
    assert focus.send_scroll_key("end") is False


@gated_only
def test_cursor_moves_never_reach_the_pointer() -> None:
    assert focus.move_cursor(60, 60) is False


@gated_only
@windows_only
def test_focus_stealing_never_reaches_the_window_manager() -> None:
    """``SetForegroundWindow`` is the only call that can yank a window in front
    of the user; ``focus_window`` reads it off ``windll.user32`` at call time,
    so neutering it there covers every caller."""
    import ctypes

    assert ctypes.windll.user32.SetForegroundWindow(0x1234) is False


@gated_only
def test_the_region_picker_fails_loudly_instead_of_covering_the_screen() -> None:
    """A forgotten mock must be a red test, not a fullscreen overlay waiting on
    a user who is not there."""
    with pytest.raises(AssertionError, match="mock it at the use site"):
        picker.pick_region()


@gated_only
def test_the_identify_overlay_fails_loudly_instead_of_covering_the_screen() -> None:
    """`/identify`'s overlay is the picker's read-only twin - same fullscreen
    child process, thrown over whatever the user is doing - and it has no timer
    at all, so a forgotten mock would hang the suite behind a fullscreen window
    nobody is watching. It must be a red test instead."""
    with pytest.raises(AssertionError, match="mock it at the use site"):
        picker.draw_identify_overlay([])


@gated_only
def test_the_identify_drawing_itself_is_blocked_in_process() -> None:
    """The `--show-identify` child calls this one directly, in whatever process
    it is running - which, for a test that drives the CLI entry point, is the
    test runner. There is no child process in between to keep a Tk window off
    the user's screen, so the draw is stubbed too."""
    with pytest.raises(AssertionError, match="mock it at the use site"):
        overlay.run_identify_overlay([])


@gated_only
def test_the_gate_swaps_the_identify_overlay_out_by_default() -> None:
    assert picker.draw_identify_overlay is not REAL_IDENTIFY_OVERLAY


def _funcptr_type() -> type:
    """The type ctypes gives a ``windll`` entry, borrowed from a call the gate
    never touches (``GetSystemMetrics`` only reads the desktop)."""
    import ctypes

    return type(ctypes.windll.user32.GetSystemMetrics)


@gated_only
def test_the_gate_swaps_the_picker_out_by_default() -> None:
    assert picker.pick_region is not REAL_PICK_REGION


@gated_only
@windows_only
def test_the_gate_swaps_sendinput_out_by_default() -> None:
    """The choke point itself: whatever name a caller imported, the input burst
    goes through this attribute, and by default it is not ctypes' any more."""
    import ctypes

    assert not isinstance(ctypes.windll.user32.SendInput, _funcptr_type())


# == the Linux branch =========================================================


@gated_only
def test_the_linux_gate_neuters_the_x11_input_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Linux half of the gate, simulated on whatever platform runs this.

    ``sys.platform`` is monkeypatched rather than the fixture re-run - the gate
    is autouse and has already made its choice by the time a test body starts -
    so this asserts the two halves that make the Linux branch work: the focus
    layer really does hand off to ``screen.x11``, and the two seams conftest
    neuters there really are the ones every injecting call passes through. The
    real Xlib is swapped for a recorded one, so the call reaches ``_fake_input``
    instead of dying at the import.
    """
    from agentclip.driver.screen import x11
    from tests.driver.screen.fake_xlib import FakeDisplay, install_fake_xlib, keycodes_for

    display = install_fake_xlib(
        monkeypatch, FakeDisplay(active_window=0x99, keymap=keycodes_for("Control_L", "v", "Return", "Page_Down", "End"))
    )
    monkeypatch.setattr(sys, "platform", "linux")
    # Exactly what tests/conftest.py's Linux branch installs.
    monkeypatch.setattr(x11, "_fake_input", lambda *args, **kwargs: False)
    monkeypatch.setattr(x11, "_activate", lambda *args, **kwargs: False)

    assert focus.send_paste() is False
    assert focus.send_enter() is False
    assert focus.send_scroll_key("page_down", 4) is False
    assert focus.click_region(REGION) is False
    assert focus.scroll_region(REGION, -3) is False
    assert focus.move_cursor(60, 60) is False
    assert focus.focus_window(0x99) is False
    assert display.events == []  # nothing was injected...
    assert display.messages == []  # ...and no window was yanked forward


@gated_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux branch of the gate")
def test_the_gate_swaps_the_x11_seams_out_by_default() -> None:
    """On a real Linux box, the branch above is live: the seams a poll tick
    would inject through are not the module's own functions any more."""
    from agentclip.driver.screen import x11

    assert x11._fake_input is not REAL_FAKE_INPUT
    assert x11._activate is not REAL_ACTIVATE


@pytest.mark.real_os
def test_the_real_os_marker_lifts_the_gate() -> None:
    """Nothing is injected here - the whole point is that a marked test gets the
    genuine functions back, which is what the opt-in promises."""
    assert picker.pick_region is REAL_PICK_REGION
    assert picker.draw_identify_overlay is REAL_IDENTIFY_OVERLAY


@pytest.mark.real_os
@windows_only
def test_the_real_os_marker_restores_sendinput() -> None:
    """Also proves the gate RESTORES the original function pointer: this test
    runs after gated ones in the same process."""
    import ctypes

    assert isinstance(ctypes.windll.user32.SendInput, _funcptr_type())
