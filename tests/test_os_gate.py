"""The suite-wide OS-input gate, tested from the outside.

``tests/conftest.py::_no_real_os_input`` is the one fixture whose failure is
silent and expensive: nothing goes red, a Ctrl+V just lands in the user's
editor. So it gets tests of its own - each one calls the real
``agentclip.screen.focus`` entry point (not a patched alias) and asserts the
gate answered instead of the desktop.

These call the OS layer unmocked on purpose, which means they must NOT run when
the gate is disarmed - they would become exactly the thing they guard against.
"""

from __future__ import annotations

import os
import sys

import pytest

from agentclip.screen import focus, picker
from agentclip.screen.region import ScreenRegion

REGION = ScreenRegion(40, 40, 20, 20)

# Captured at import time, i.e. during collection - before any fixture runs.
REAL_PICK_REGION = picker.pick_region

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


@pytest.mark.real_os
def test_the_real_os_marker_lifts_the_gate() -> None:
    """Nothing is injected here - the whole point is that a marked test gets the
    genuine functions back, which is what the opt-in promises."""
    assert picker.pick_region is REAL_PICK_REGION


@pytest.mark.real_os
@windows_only
def test_the_real_os_marker_restores_sendinput() -> None:
    """Also proves the gate RESTORES the original function pointer: this test
    runs after gated ones in the same process."""
    import ctypes

    assert isinstance(ctypes.windll.user32.SendInput, _funcptr_type())
