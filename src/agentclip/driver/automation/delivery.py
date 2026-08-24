"""The outbound delivery's beats and the words its banner says.

The sibling of :mod:`agentclip.driver.automation.flow`, for the other half of the loop:
where that one holds the auto-copy HARVEST's numbers, this holds the ones the
paste path pauses for, plus the four things the "your move" banner can say.
Everything here was a module-level constant of the Textual ``MainScreen`` (the
beats) or of its sidebar widget (the words) until ``deliver`` came down into
:class:`~agentclip.driver.automation.controller.AutomationController` (docs/design/gui.md
§1, slice 7).

The WORDS are here rather than in a shell for the reason
:mod:`agentclip.driver.automation.view` gives: the view port takes finished text and is
told where to put it, so which of these four the user is looking at is decided
once, below both shells, and two front-ends cannot phrase the same moment
differently. The shell's sidebar re-exports them under its old names, because
that is where the widget and the suites already reach for them.

The BEATS are defaults: they are read through ``ScreenOps`` (``paste_settle``
and friends), so a shell whose suites shrink them keeps doing exactly that.
"""

from __future__ import annotations

# The beats moved to the monitor package with the rest of the OS cadence
# (docs/design/ui-monitor.md §2.10); re-exported so the suites that shrink
# them keep their names.
from agentclip.driver.monitor.beats import (  # noqa: E402, F401
    ACTIVATION_ATTEMPTS,
    ACTIVATION_POLL_S,
    FOCUS_CLICK_GAP_S,
    PASTE_SETTLE_DELAY,
    SNAP_BACK_SETTLE_S,
    STREAM_CHUNK_SETTLE_S,
    SUBMIT_SETTLE_S,
)

# -- the four things the banner can say ----------------------------------------
# Obnoxious by design: the user is staring at the browser, not at us.
PASTE_FLASH_TEXT = ">>> PRESS CTRL+V <<<\nin the chat, then send"
ENTER_FLASH_TEXT = ">>> PRESS ENTER <<<\nreply pasted - just send it"
# auto_submit tapped Enter itself; the flash stays up until the send gate sees
# the send land, so the second line covers the tap not taking.
AUTO_SEND_FLASH_TEXT = ">>> AUTO-SENT <<<\nEnter was tapped for you"
# The third thing the same banner can say, and the only one that is not asking
# for a keystroke: a streamed delivery is pasting the payload in a chunk at a
# time, and the count is the whole point - a big message takes seconds, and
# without it the user is watching a chat box fill from nowhere.
STREAM_FLASH_TEXT = ">>> STREAMING <<<\nchunk {index}/{total} - don't type"


def stream_flash_text(index: int, total: int) -> str:
    return STREAM_FLASH_TEXT.format(index=index, total=total)
