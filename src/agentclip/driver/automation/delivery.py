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

# How long the delivery waits for the browser to actually TAKE the foreground
# after the focus click, and the beat between two askings
# (``AutomationController._await_browser_activation``).
#
# The click is what gives the browser window the OS focus, and focus is granted
# ASYNCHRONOUSLY: the window is still activating itself while our next SendInput
# burst goes out, and a paste that arrives before the caret is really in the
# input field is delivered to whatever held focus a moment ago - which is to say
# nowhere the user can see, and the reply silently fails to insert. This is the
# same race ``focus_window_verified`` fights from the other direction, and there
# IS something to verify against after all: not the chat box (a browser widget,
# not a window handle) but the window it lives in - once the foreground is no
# longer OUR window, the activation the click asked for has been granted. So the
# blind beat became a POLL with a blind beat behind it, because a fixed sleep
# long enough for a loaded machine is a sleep the user waits out on every
# delivery, and one short enough not to be noticed is the intermittent bug.
#
# 10 x 0.1s = a 1s ceiling, comfortably past a browser's activation and short
# enough that a machine which will never hand the foreground over (no handle
# recorded, a click the compositor swallowed) does not hang the delivery - the
# budget running out is not a failure, it just means we stop waiting and paste.
ACTIVATION_ATTEMPTS = 10
ACTIVATION_POLL_S = 0.1
# Beat between the two clicks of the pre-paste focus click.
#
# The click that focuses the chat box is a DOUBLE click, because one was not
# reliably enough: the first click is what brings the browser window forward,
# and a page that is still activating can swallow it as the "wake up" click
# without ever routing it to the input field - which leaves the window focused,
# the caret nowhere, and the paste going into the void. The second click
# arrives at a window that is already awake, so it lands where it was aimed.
# Safe precisely here and nowhere else: the box is EMPTY at this point in the
# sequence, so a double click has no word to select.
FOCUS_CLICK_GAP_S = 0.12
# Beat between the browser holding the foreground and the synthetic Ctrl+V.
#
# Still needed after the poll above, and this is the whole reason it did not
# replace it: window activation is not caret focus. The OS has handed the
# browser the foreground; the PAGE has still to route the click through to the
# chat box and put a caret in it, and that is renderer work no window handle
# reports on. Raised from 200ms to 300ms because inserts were still going
# missing at 200, and to 600ms when the double click above went in: the two
# together are what the failing case needed, and a page that reflows its
# composer after taking focus can spend most of a second doing it. Tests shrink
# it.
PASTE_SETTLE_DELAY = 0.6
# Beat between an OS click inside the browser and snapping the foreground back
# to our own window (``AutomationController.snap_back_after_click``) - long
# enough that the browser has registered the click before the focus moves off
# it, short enough to read as one motion rather than two.
SNAP_BACK_SETTLE_S = 0.15
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
