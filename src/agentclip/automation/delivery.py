"""The outbound delivery's beats and the words its banner says.

The sibling of :mod:`agentclip.automation.flow`, for the other half of the loop:
where that one holds the auto-copy HARVEST's numbers, this holds the ones the
paste path pauses for, plus the four things the "your move" banner can say.
Everything here was a module-level constant of the Textual ``MainScreen`` (the
beats) or of its sidebar widget (the words) until ``deliver`` came down into
:class:`~agentclip.automation.controller.AutomationController` (docs/design/gui.md
§1, slice 7).

The WORDS are here rather than in a shell for the reason
:mod:`agentclip.automation.view` gives: the view port takes finished text and is
told where to put it, so which of these four the user is looking at is decided
once, below both shells, and two front-ends cannot phrase the same moment
differently. The shell's sidebar re-exports them under its old names, because
that is where the widget and the suites already reach for them.

The BEATS are defaults: they are read through ``ScreenOps`` (``paste_settle``
and friends), so a shell whose suites shrink them keeps doing exactly that.
"""

from __future__ import annotations

# Beat between the focus click landing on the chat's input box and the synthetic
# Ctrl+V that follows it (``AutomationController.deliver``).
#
# The click is what gives the browser window the OS focus, and focus is granted
# ASYNCHRONOUSLY: the window is still activating itself while our next SendInput
# burst goes out, and a paste that arrives before the caret is really in the
# input field is delivered to whatever held focus a moment ago - which is to say
# nowhere the user can see, and the reply silently fails to insert. This is the
# same race ``focus_window_verified`` documents from the other direction; here
# there is nothing to verify against (the chat box is a browser widget, not a
# window handle), so the answer is a beat that comfortably outlasts the
# activation on a busy machine. 200ms is under human notice next to a click the
# user is not watching anyway. Tests shrink it.
PASTE_SETTLE_DELAY = 0.2
# Beat between the paste and the opt-in auto-submit Enter, for the box to render
# and re-measure what was just dropped into it before it is sent.
SUBMIT_SETTLE_S = 0.15
# Beat between the bursts of a streamed delivery (ServicePreset.delivery), so a
# chat box that reflows and re-measures after every paste is not handed the next
# one mid-repaint. Only between chunks: the last one is followed by the settle
# the user's own Enter provides.
STREAM_CHUNK_SETTLE_S = 0.12

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
