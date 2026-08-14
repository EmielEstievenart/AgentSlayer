"""Finish detection's vocabulary: what a probe MEANS, and what it reads as.

The third pure-vocabulary module of the automation package, alongside
:mod:`agentclip.automation.loop_state` and
:mod:`agentclip.automation.harness_log`, and here for the same reason they are:
:class:`~agentclip.automation.controller.AutomationController` folds the poller's
probes into one "the model stopped" decision, and every constant and phrase that
decision is made of has to live below both UI shells or it drifts between them.

Three groups, and they are three different kinds of thing:

* **The verdicts.** ``busy_verdict`` / ``idle_verdict`` / ``stale_verdict`` turn
  one detector's probe into the same three-valued answer - True finished, False
  generating, None no verdict (the capture failed) - so the fold above them
  never has to remember that a busy appearance means the opposite of an idle
  one. That inversion is the whole reason these are functions and not a
  comparison at the call site.
* **The readout.** ``format_*_probe`` and the ``SEND_READY_*`` lines are the
  words the sidebar (and, later, the GUI) shows for those same probes. They are
  paint TEXT rather than paint DECISIONS, which is why they cross the
  ``AutomationView`` port as strings: the controller knows what happened, the
  shell only knows where to put it.
* **The tunables.** How much change counts as change, how long a gate may wait.
  Policy about a screen, not about a widget.

``SendGate`` is here too, for the same reason ``LoopState`` is in its own module:
it is a state the automation is IN, and both shells will want to show it.
"""

from __future__ import annotations

from enum import Enum

from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.stale import StaleProbe, StaleState


class SendGate(Enum):
    """Where the ready-to-send gate is between AgentClip's paste and the send.

    Two states and an absence. ``None`` is by far the commonest and means "not
    gating": the live service has no ``SEND_READY`` appearance, or the gate has
    already let go. Only the two below hold finish detection back.
    """

    HOLD = "hold"  # pasted, and the send button has not been seen yet
    SEEN = "seen"  # it is on screen; its disappearance is the user's Enter


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

# The send gate's line in the DETECTION readout (tui.md 3.4b). It is not a finish
# detector: it reports whether the ready-to-send button is holding finish
# detection back between AgentClip's paste and the user's Enter, and the four
# terminal lines below name WHICH way it let go, because the user's fix differs
# per way.
SEND_READY_RESTING = "no gate - not captured"
SEND_READY_ARMED = "gate armed - holds the next paste"
SEND_READY_HOLDING = "watching for the send button"
SEND_READY_SEEN = "on screen - press Enter to send"
SEND_READY_RELEASED = "gone - sent, finish detection running"
SEND_READY_OVERRIDDEN = "generating - sent, finish detection running"
SEND_READY_TIMEOUT = "never appeared - finish detection running"
SEND_READY_STUCK = "never went away - finish detection running"


def format_busy_probe(probe: BusyProbe) -> str:
    """Unmistakable readout for the sidebar - this is the whole deliverable."""
    if probe.state is BusyState.ERROR:
        return "✗ capture failed"
    pct = f"{(probe.diff or 0.0) * 100:.1f}%"
    if probe.state is BusyState.MATCH:
        return f"● GENERATING · match (diff {pct})"
    return f"○ response ready · changed (diff {pct})"


def format_idle_probe(probe: BusyProbe) -> str:
    """Same readout for the idle element, with the polarity flipped: it was
    calibrated while the chat was idle, so MATCH is the *finished* verdict."""
    if probe.state is BusyState.ERROR:
        return "✗ capture failed"
    pct = f"{(probe.diff or 0.0) * 100:.1f}%"
    if probe.state is BusyState.MATCH:
        return f"○ response ready · match (diff {pct})"
    return f"● GENERATING · changed (diff {pct})"


def format_stale_probe(probe: StaleProbe) -> str:
    """Same unmistakable readout for the stale detector: the response region
    still moving means the model is still typing; long enough unchanged means
    the answer is done. ``still ×N`` shows the streak building toward STALE."""
    if probe.state is StaleState.ERROR:
        return "✗ capture failed"
    if probe.state is StaleState.STALE:
        return f"○ response ready · stale (still ×{probe.stable_ticks})"
    pct = f"{(probe.diff or 0.0) * 100:.2f}%"
    return f"● GENERATING · changing (diff {pct} · still ×{probe.stable_ticks})"


def busy_verdict(probe: BusyProbe) -> bool | None:
    """The busy element's probe as a finish verdict: True = finished,
    False = generating, None = no verdict (capture error).

    It was calibrated WHILE the model was generating, so a MATCH means the
    generation is still going.
    """
    if probe.state is BusyState.ERROR:
        return None
    return probe.state is BusyState.CHANGED


def idle_verdict(probe: BusyProbe) -> bool | None:
    """The idle element's probe as a finish verdict, same three values.

    It was calibrated while the chat was IDLE, so a MATCH means the response
    has finished - the exact inverse of the busy element.
    """
    if probe.state is BusyState.ERROR:
        return None
    return probe.state is BusyState.MATCH


def stale_verdict(probe: StaleProbe) -> bool | None:
    """The stale tracker's probe as a finish verdict, same three values.

    STALE (unchanged long enough) means finished; CHANGING - including the
    settling polls before the streak completes - means generating.
    """
    if probe.state is StaleState.ERROR:
        return None
    return probe.state is StaleState.STALE
