"""Synthetic mouse input. The real send needs Windows and would move the cursor,
so only the paths that refuse before touching the OS are exercised here."""

from __future__ import annotations

import sys

import pytest

from agentclip.screen.focus import click_region, focus_window, foreground_window, scroll_region
from agentclip.screen.region import ScreenRegion

REGION = ScreenRegion(120, 240, 32, 24)

off_windows_only = pytest.mark.skipif(
    sys.platform == "win32", reason="only other platforms refuse outright"
)


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


def test_focus_refuses_a_null_handle() -> None:
    """0 is Windows' own "no window" value - refused before any OS call."""
    assert focus_window(0) is False


@off_windows_only
def test_window_focus_is_unavailable_off_windows() -> None:
    assert foreground_window() is None
    assert focus_window(0x1234) is False
