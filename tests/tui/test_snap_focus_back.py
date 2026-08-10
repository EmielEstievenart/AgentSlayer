"""The verified snap-back to AgentClip's own window (``focus_window_verified``).

Every caller asks for the foreground immediately after clicking inside a
browser, which is itself an activation request the browser is still processing.
A single ``SetForegroundWindow`` therefore wins the race sometimes and loses it
sometimes - and losing it is silent, which is why "after /new the browser keeps
focus" read as a missing call rather than a lost race. The helper answers that
by asking again and checking afterwards, and this is where that contract is
pinned: how many asks, that the check happens AFTER a settle, and that the whole
budget stays short enough to sit in front of a user.

No OS is touched: ``focus_window`` and ``foreground_window`` are monkeypatched on
the module the helper reads them from, and ``sys.platform`` is forced so the
Windows-only guard is exercised everywhere the suite runs.
"""

from __future__ import annotations

import sys
import time

import pytest

import agentclip.screen.focus as focus_mod
from agentclip.screen.focus import REFOCUS_ATTEMPTS, focus_window_verified

OURS = 4242  # AgentClip's terminal
THEIRS = 999  # the browser, still holding on


@pytest.fixture
def _on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend this is Windows without letting anything reach it."""
    monkeypatch.setattr(sys, "platform", "win32")


def _asks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every ``SetForegroundWindow`` request, and grant them all: the
    interesting failure is a window that says yes and loses the foreground
    again, not one that refuses."""
    asks: list[int] = []
    monkeypatch.setattr(focus_mod, "focus_window", lambda handle: asks.append(handle) or True)
    return asks


def test_one_ask_is_enough_when_the_window_really_comes_forward(
    monkeypatch: pytest.MonkeyPatch, _on_windows: None
) -> None:
    asks = _asks(monkeypatch)
    monkeypatch.setattr(focus_mod, "foreground_window", lambda: OURS)

    assert focus_window_verified(OURS, settle_s=0.0) is True
    assert asks == [OURS]  # nothing is retried once the foreground agrees


def test_the_snap_back_asks_again_while_the_browser_still_holds_focus(
    monkeypatch: pytest.MonkeyPatch, _on_windows: None
) -> None:
    """The browser's own activation lands after ours, so the first grants are
    taken back - which is exactly what one un-verified call cannot see."""
    asks = _asks(monkeypatch)
    readings = [THEIRS, THEIRS, OURS]
    monkeypatch.setattr(focus_mod, "foreground_window", lambda: readings.pop(0))

    assert focus_window_verified(OURS, settle_s=0.0) is True
    assert asks == [OURS, OURS, OURS]
    assert readings == []  # the check runs once per ask, never speculatively


def test_a_window_that_never_comes_forward_gives_up_and_says_so(
    monkeypatch: pytest.MonkeyPatch, _on_windows: None
) -> None:
    """False rather than a hang: the caller carries on with the browser in
    front, which is worse than the snap-back but is not a failure of the copy
    or the new chat it followed."""
    asks = _asks(monkeypatch)
    monkeypatch.setattr(focus_mod, "foreground_window", lambda: THEIRS)

    assert focus_window_verified(OURS, settle_s=0.0) is False
    assert asks == [OURS] * REFOCUS_ATTEMPTS


def test_the_check_waits_a_growing_beat_and_the_whole_budget_stays_sub_second(
    monkeypatch: pytest.MonkeyPatch, _on_windows: None
) -> None:
    """The settle before each check is the point - a foreground read taken in
    the same instant as the request is the answer ``focus_window`` already
    gives. It grows so a slow activation still gets caught, and it is bounded
    because a user is waiting behind this."""
    beats: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: beats.append(seconds))
    _asks(monkeypatch)
    monkeypatch.setattr(focus_mod, "foreground_window", lambda: THEIRS)

    assert focus_window_verified(OURS) is False
    assert len(beats) == REFOCUS_ATTEMPTS
    assert beats == sorted(beats) and beats[0] < beats[-1]
    assert sum(beats) < 1.0


def test_a_null_handle_is_refused_before_any_ask(
    monkeypatch: pytest.MonkeyPatch, _on_windows: None
) -> None:
    """0 is Windows' own "no window", and nothing was ever recorded - the same
    refusal ``focus_window`` gives, without burning the retry budget on it."""
    asks = _asks(monkeypatch)
    assert focus_window_verified(0) is False
    assert asks == []


def test_the_snap_back_is_unavailable_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    asks = _asks(monkeypatch)
    assert focus_window_verified(OURS) is False
    assert asks == []
