"""What one click can come to.

:class:`ElementClick` is the verdict of the find-then-click primitive. The OS
adapter that used to live beside it (``ScreenOps``) is the monitor's now -
:mod:`agentclip.driver.monitor.ops` (docs/design/ui-monitor.md §3).
"""

from __future__ import annotations

from enum import Enum

from agentclip.driver.monitor.beats import NEW_CHAT_SETTLE_S  # noqa: F401


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
