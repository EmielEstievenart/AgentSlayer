"""The beats every OS-acting sequence paces itself by, and the sizes it moves in.

Cadence is a property of the machine whose screen is being driven, not of the
policy that decides what to do - so these live in the monitor package
(docs/design/ui-monitor.md §2.10) and are read through :class:`ScreenOps`
(``paste_settle`` and friends). :mod:`agentclip.driver.automation.delivery`
re-exports them under the names its suites already reach for.
"""

from __future__ import annotations

from agentclip.config import DEFAULT_SUBMIT_DELAY_S

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
#
# Raised from 150ms to 1.2s: a composer that turns a big paste into an
# attachment chip (ChatGPT's "Pasted text") does that ASYNCHRONOUSLY, disables
# its send control while it does, and drops an Enter that arrives meanwhile on
# the floor - the prompt then sits in the box unsent, which is exactly what the
# users of a streamed delivery were seeing. Over a second because the chip is
# built from the whole payload and a 20k-char paste takes a busy tab most of
# that. The delivery is already seconds long by then, and an Enter that never
# takes costs far more than one that waits. Tests shrink it.
#
# It is a SETTING now (``ServicePreset.submit_delay_s``, ui-monitor.md §11.8):
# the delivery reads the WATCHED SERVICE's number, and this constant is the
# default that number starts at - defined in ``config.py`` beside the rest of
# the preset defaults and re-spelled here, where the rest of the cadence lives.
# ``ScreenOps.submit_settle`` still answers it, for a caller with no preset in
# its hand.
SUBMIT_SETTLE_S = DEFAULT_SUBMIT_DELAY_S
# Beat between the bursts of a streamed delivery (ServicePreset.delivery), so a
# chat box that reflows and re-measures after every paste is not handed the next
# one mid-repaint. Only between chunks: the last one is followed by the settle
# the user's own Enter provides.
STREAM_CHUNK_SETTLE_S = 0.12
# Beat between opening a fresh browser chat and treating it as the live slot -
# the page still has to render its (centred) input box.
NEW_CHAT_SETTLE_S = 0.4
# Hover pause before clicking a calibrated element, for the same reason the copy
# click settles: web UIs paint their buttons on hover, so the pixel that was
# matched has to be given a moment under the pointer before it is pressed.
# ``UIMonitor.click_element``'s default settle.
ELEMENT_CLICK_SETTLE_S = 0.05

# -- the send gate's budgets ------------------------------------------------
# Counted in TICKS and in frame-to-frame DIFF, which is why they live here and
# not in the brain (docs/design/ui-monitor.md §10.5): a tick is the monitor's
# unit and a diff is its measurement, so the numbers a gate is measured in
# belong to the machine that produces them. They used to ride on
# ``MonitorSpec`` - the brain telling the monitor what its own tick meant - and
# §10.5 took that field off the wire. ``driver/automation/finish.py`` re-exports
# all four under the names its suites already reach for.

# What it takes for the STALE detector alone to arm the auto-copy trigger, i.e.
# to claim it has watched the user's message actually get sent.
#
# The busy/idle detectors arm on one frame, because a reasoning icon appearing
# is evidence nothing else produces. Frame-to-frame change is not: after
# AgentClip pastes the outbound text the user still has to press Enter, and in
# that window a blinking caret or a mouse-over highlight makes the region
# "change" by a handful of pixels. Arming on that, then reading the still
# pre-Enter screen as finished, fires the auto-copy at a chat with no reply in
# it at all - the exact bug these two constants exist to close.
#
# So a CHANGING verdict must be BIG and SUSTAINED: 2% of the sampled pixels
# (caret blink and hover tints are orders of magnitude below; a prompt landing
# in the transcript and the reasoning UI unfolding are far above) on
# SEND_ARM_TICKS consecutive stale probes - ~1.5 s at the 0.5 s cadence, longer
# than any repaint and shorter than any answer.
SEND_ARM_MIN_DIFF = 0.02
SEND_ARM_TICKS = 3
# How long the ready-to-send gate waits for the button to show up at all before
# it gives up and hands finish detection back (tui.md §3.4b). Counted in poller
# TICKS rather than seconds - ten of them are ~5 s at the 0.5 s cadence - because
# the state machine must be deterministic and injectable: a wall clock would make
# the same test pass or fail depending on how busy the machine was.
SEND_GATE_TIMEOUT_TICKS = 10
# The SAME promise for the phase AFTER the button has been seen, on its own much
# longer clock - ~2 minutes at the 0.5 s cadence.
#
# The gate's release is one non-debounced template match going away, and a fresh
# chat is exactly where that match is least reliable: the composer is centred and
# animating rather than docked where the capture was taken, so the button can be
# seen once and then never yield a clean not-found frame. Nothing else could
# release the gate, and "the gate may delay a session; it may never deadlock one"
# then held for the never-seen phase only - the user pressed Enter, the model
# generated, and ">>> PRESS ENTER <<<" flashed for ever.
#
# So the SEEN phase gets a budget too, and the budget is generous rather than
# tight because waiting out a human reading what is about to be sent is the whole
# point of the gate: minutes, not the five seconds a never-appearing button costs.
# Anything shorter would expire on a user who paused to think. It is the LAST
# line of defence in any case - a model that actually starts generating releases
# the gate on the icon evidence the moment it does (``evaluate_finish``), long
# before this runs out.
SEND_GATE_SEEN_TIMEOUT_TICKS = 240

# -- how far one snap to the bottom goes -----------------------------------
# Sizes rather than beats, and here for the same reason the beats are: how far a
# wheel detent or a Page Down tap carries is a property of the machine and the
# page being driven, not of the policy that decided to scroll
# (docs/design/ui-monitor.md §2.10). ``UIMonitor.snap_to_bottom`` is their only
# reader; ``driver/automation/flow.py`` re-exports them under the names its
# suites size their assertions off.

# The wheel flick's size, in detents. Deliberately far more than it takes to
# cross one screenful: a flick that stops short leaves the newest reply's copy
# button above the fold and the harvest hunts a transcript that is not showing
# the answer, which is a silent fall to MANUAL_COPY. Over-shooting costs nothing
# at all - the page is already at its bottom and the extra detents land on a
# wall - so the number is chosen for the worst long response rather than the
# typical one.
SNAP_WHEEL_DETENTS = -100
# How many Page Down taps a "page_down" scroll action sends in one burst
# (ServicePreset.scroll_action). Sized like the wheel flick above and for the
# same reason: a generous over-shoot that stops at the bottom, because the flow
# wants the newest reply on screen, not a measured scroll. Twelve taps is
# roughly a dozen screenfuls, which comfortably covers a long reply the user
# scrolled away from. End needs no such count - one tap is the bottom by
# definition, which is why it is left at one.
PAGE_DOWN_TAPS = 12
