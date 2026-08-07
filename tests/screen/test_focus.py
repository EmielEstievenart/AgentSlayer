"""Synthetic mouse and keyboard input. The real send needs Windows and would move
the cursor (or type into whatever has focus), so only the paths that refuse
before touching the OS are exercised here."""

from __future__ import annotations

import inspect
import sys

import pytest

from agentclip.screen.focus import (
    click_region,
    focus_window,
    foreground_window,
    scroll_region,
    send_paste,
)
from agentclip.screen.region import ScreenRegion

REGION = ScreenRegion(120, 240, 32, 24)

off_windows_only = pytest.mark.skipif(
    sys.platform == "win32", reason="only other platforms refuse outright"
)


def test_clicking_settles_only_when_asked() -> None:
    """``settle_s`` is keyword-only and defaults to no pause, so the callers that
    just want a focus click keep clicking the instant the cursor lands."""
    settle = inspect.signature(click_region).parameters["settle_s"]
    assert settle.kind is inspect.Parameter.KEYWORD_ONLY
    assert settle.default == 0.0


def test_scrolling_zero_clicks_is_a_no_op() -> None:
    """Reported as False and, crucially, sends nothing - not even a cursor move."""
    assert scroll_region(REGION, 0) is False


@off_windows_only
def test_scroll_is_unavailable_off_windows() -> None:
    assert scroll_region(REGION, -5) is False
    assert scroll_region(REGION, 3) is False


@off_windows_only
def test_click_is_unavailable_off_windows() -> None:
    assert click_region(REGION) is False


@off_windows_only
def test_a_settling_click_is_unavailable_off_windows() -> None:
    """The hover pause is no excuse to reach for the OS - refused just as fast,
    and without sleeping, since the platform check comes before the cursor move."""
    assert click_region(REGION, settle_s=0.05) is False


@off_windows_only
def test_paste_is_unavailable_off_windows() -> None:
    assert send_paste() is False


def test_focus_refuses_a_null_handle() -> None:
    """0 is Windows' own "no window" value - refused before any OS call."""
    assert focus_window(0) is False


@off_windows_only
def test_window_focus_is_unavailable_off_windows() -> None:
    assert foreground_window() is None
    assert focus_window(0x1234) is False
