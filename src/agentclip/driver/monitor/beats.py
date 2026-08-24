"""The beats every OS-acting sequence paces itself by.

Cadence is a property of the machine whose screen is being driven, not of the
policy that decides what to do - so these live in the monitor package
(docs/design/ui-monitor.md §2.10) and are read through :class:`ScreenOps`
(``paste_settle`` and friends). :mod:`agentclip.driver.automation.delivery`
re-exports them under the names its suites already reach for.
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
# The click that focuses the chat box is a PAIR of clicks, because one was not
# reliably enough: the first click is what brings the browser window forward,
# and a page that is still activating can swallow it as the "wake up" click
# without ever routing it to the input field - which leaves the window focused,
# the caret nowhere, and the paste going into the void. The second click
# arrives at a window that is already awake, so it lands where it was aimed.
#
# Half a second, up from 120ms, for two reasons that point the same way:
#
# * 120ms was sometimes not enough time for the woken window to be READY to
#   route the second click into the input field - a busy page is still
#   restyling and reflowing its composer while the pair goes out, so the second
#   click was landing on a layout that had already moved. Deliveries were still
#   intermittently failing, which is what this beat exists to stop.
# * it also clears the OS double-click threshold (500ms by default on Windows),
#   so the two register as two SINGLE clicks rather than as a double click. In
#   a text box a double click selects a word - harmless while the box is empty
#   (which is why the pair was safe here in the first place), but there is no
#   reason to keep relying on the box being empty when a longer beat buys both
#   the readiness and the guarantee.
#
# No config knob: timing knobs multiply, and this constant is the tuning point.
FOCUS_CLICK_GAP_S = 0.5
# Beat between the browser holding the foreground and the synthetic Ctrl+V.
#
# Still needed after the poll above, and this is the whole reason it did not
# replace it: window activation is not caret focus. The OS has handed the
# browser the foreground; the PAGE has still to route the click through to the
# chat box and put a caret in it, and that is renderer work no window handle
# reports on. Raised from 200ms to 300ms because inserts were still going
# missing at 200, and to 600ms when the second focus click above went in: the
# two together are what the failing case needed, and a page that reflows its
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
# Beat between opening a fresh browser chat and treating it as the live slot -
# the page still has to render its (centred) input box.
NEW_CHAT_SETTLE_S = 0.4
