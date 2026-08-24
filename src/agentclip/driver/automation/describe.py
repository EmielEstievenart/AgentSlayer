"""One sentence for two machines: the state label both shells show.

AgentClip runs two state machines on purpose (`docs/design/ui-monitor.md`
section 2.5). :class:`~agentclip.engine.states.Phase` says where the TASK is -
waiting on a reply, reviewing one, parked on a question, done.
:class:`~agentclip.driver.automation.loop_state.LoopState` says where the ROUND
TRIP through the chat window is - the paste, the send, the generation, the
copy. They are not merged, and this module is the only place that has to hold
both at once: :func:`describe` picks the ONE line a user glancing at the app
should read.

The rule is precedence, not prose. A loop state that the user has to act on -
or that is visibly moving on screen - outranks the phase, because "generating"
is news and "waiting for the reply" (which is the same fact, one machine over)
is not. The ``ATTENTION_STATES`` are the sharpest case: a payload nobody pasted,
a reply nobody copied and a monitor nobody can reach are the moments where the
app is stuck on something no phase can speak for, and no phase wording may bury
them. When the loop is idle or merely interpreting, the browser has nothing to
add and the phase speaks.

Wording is inherited rather than invented: where the GUI's watch segment
already had a sentence for a situation (``gui/view.py`` ``_base_watch_segment``:
"ready - paste the reply", "working...", "done - reply to continue", "idle") it
is kept verbatim, minus the leading glyph - the glyph and its colour are the
shell's styling decision, and the TUI draws them differently. The rail's terse
vocabulary ("auto insert", "wait send") stays on the rail; this label is a
sentence, not a row.
"""

from __future__ import annotations

from agentclip.driver.automation.loop_state import LoopState
from agentclip.engine.states import Phase

# What the TASK is doing, when the browser has nothing more urgent to say.
# Total over ``Phase`` - a new phase must be given words here, and the totality
# test says so out loud rather than letting a KeyError surface in the GUI.
PHASE_LABEL: dict[Phase, str] = {
    Phase.IDLE: "idle",
    # The loop is not carrying this one: the payload is out and nobody is
    # pasting for the user. Today's watch-segment wording, glyph stripped.
    Phase.AWAITING_REPLY: "ready - paste the reply",
    Phase.REVIEW: "working...",
    Phase.SENDING_CHUNKS: "sending chunks",
    Phase.AWAITING_USER: "answer needed",
    Phase.AWAITING_SUBAGENT: "sub-agent running",
    Phase.DONE: "done - reply to continue",
}

# What the ROUND TRIP is doing. ``None`` is the explicit precedence marker: it
# means "this loop state has nothing the phase does not say better", and the
# lookup falls through to ``PHASE_LABEL``. Every other row outranks the phase.
#
# IDLE defers because an idle loop is exactly the moment the phase is the whole
# story (no session, waiting on a paste, done). INTERPRETING defers because the
# turn IS the engine: while the reply is being parsed and acted on, the phase
# already distinguishes the outcomes the user cares about - working, question
# parked, sub-agent out, done - and "interpreting" would flatten all four.
#
# Total over ``LoopState`` - a new member is answered HERE, and the totality
# test fails until it is.
LOOP_LABEL: dict[LoopState, str | None] = {
    LoopState.IDLE: None,
    LoopState.AUTO_INSERT: "pasting into the chat box",
    LoopState.MANUAL_INSERT: "paste it yourself - Ctrl+V into the chat box",
    LoopState.WAIT_SEND: "press Enter to send",
    LoopState.WAIT_GENERATE: "generating...",
    LoopState.AUTO_COPY: "copying the reply",
    LoopState.MANUAL_COPY: "copy the reply yourself",
    LoopState.INTERPRETING: None,
    # Phase 5's state, and the one row that is not about the browser at all: the
    # link to the machine the browser is ON is gone (ui-monitor.md §2.9). It
    # outranks every phase for the ``ATTENTION_STATES`` reason - a phase saying
    # "generating..." while nothing can see the screen is the app lying about
    # what it knows - and it says "reconnecting" because the brain is already
    # redialling on its own; the user is being told, not asked.
    LoopState.DISCONNECTED: "monitor link lost - reconnecting",
}


def describe(phase: Phase, loop_state: LoopState) -> str:
    """The single state label for ``(phase, loop_state)``.

    Two lookups and no judgement: the loop's word if it has one, the phase's
    otherwise. Plain text - no glyph, no styling, no trailing punctuation
    beyond what the sentence carries - because both shells decorate it their
    own way.
    """
    return LOOP_LABEL[loop_state] or PHASE_LABEL[phase]
