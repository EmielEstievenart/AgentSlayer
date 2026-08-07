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
    move_cursor,
    scroll_region,
    send_paste,
    virtual_screen_bounds,
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


@off_windows_only
def test_cursor_moves_are_unavailable_off_windows() -> None:
    """The hover scan gives up on the first refused move rather than sleeping
    its way through a scan that can never see anything."""
    assert move_cursor(100, 200) is False


@off_windows_only
def test_virtual_screen_bounds_are_windows_only() -> None:
    assert virtual_screen_bounds() is None


@pytest.mark.skipif(sys.platform != "win32", reason="needs the Windows desktop metrics")
def test_virtual_screen_bounds_describe_a_real_desktop() -> None:
    bounds = virtual_screen_bounds()
    assert bounds is not None
    _left, _top, width, height = bounds
    assert width > 0 and height > 0


def test_move_cursor_refuses_a_degenerate_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1-pixel-wide desktop would divide by zero while normalizing - refused
    instead, on every platform."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("agentclip.screen.focus.virtual_screen_bounds", lambda: (0, 0, 1, 1))
    assert move_cursor(0, 0) is False


def test_move_cursor_refuses_without_desktop_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("agentclip.screen.focus.virtual_screen_bounds", lambda: None)
    assert move_cursor(10, 10) is False


def test_focus_refuses_a_null_handle() -> None:
    """0 is Windows' own "no window" value - refused before any OS call."""
    assert focus_window(0) is False


@off_windows_only
def test_window_focus_is_unavailable_off_windows() -> None:
    assert foreground_window() is None
    assert focus_window(0x1234) is False
