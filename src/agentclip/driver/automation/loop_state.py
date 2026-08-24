"""The browser-automation loop: what AgentClip is doing to the chat window.

Deliberately NOT ``engine.states.Phase``. The engine's machine says where the
TASK is (awaiting reply, review, done); this one says where the round trip
through the browser is - the paste, the send, the generation, the copy - which
is the loop the user actually watches the tool run and the one the sidebar's
STATE rail draws. The evidence behind it is scattered by design (the send
gate, the finish detectors' verdicts, the auto-copy flow's outcome); this enum
is the one story they add up to, and MainScreen is its only writer.

``LOOP_TRANSITIONS`` is the legal-next table the rail's styling reads, in the
same shape as ``engine.states.TRANSITIONS``. It describes the loop's forward
motion only: session teardown (``/new``) sends every state home to IDLE, and
that is a reset rather than a transition - drawing "idle" as reachable from
everywhere would make the rail's legal-next brightness meaningless.
"""

from __future__ import annotations

from enum import Enum, auto


class LoopState(Enum):
    """One full turn of the copy-paste loop, in the order it runs."""

    IDLE = auto()  # nothing outstanding; the user's next chat message starts the loop
    AUTO_INSERT = auto()  # outbound copied; clicking the chat box and pasting it in
    MANUAL_INSERT = auto()  # the click/paste did not land; the user Ctrl+Vs it themselves
    WAIT_SEND = auto()  # the payload is in the chat box; waiting for the user's Enter
    WAIT_GENERATE = auto()  # sent, and the model is visibly generating
    AUTO_COPY = auto()  # generation stopped; hunting for the reply's copy button
    MANUAL_COPY = auto()  # no copy button to click; the user copies the reply themselves
    INTERPRETING = auto()  # the reply is on the clipboard; parsing and acting on it
    # Last, and outside the round trip on purpose: it is not a step of the loop
    # but the loop's absence. The link to the UI monitor is gone (docs/design/
    # ui-monitor.md §2.9), so nothing can be looked at, clicked or pasted until
    # a redial lands. Declared at the end so the rail's picture of the round
    # trip keeps its order and gains a row rather than growing one in the middle.
    DISCONNECTED = auto()


# The states the loop cannot leave by itself: every one of them means "the next
# move is not this loop's" - the payload nobody pasted, the reply nobody copied,
# and the machine nobody can reach. Named here rather than at the one place that
# reads them because it is a fact about the enum, and anything that has to nag
# the user (today: the audible alert) is asking exactly this question.
#
# DISCONNECTED belongs here for the same reason the two manual states do, with
# one difference worth being honest about: what the user is being nagged towards
# is the *monitor*, not the browser - the brain redials on its own, and if it
# cannot, somebody has to go and look at the machine the monitor runs on.
ATTENTION_STATES: frozenset[LoopState] = frozenset(
    {LoopState.MANUAL_INSERT, LoopState.MANUAL_COPY, LoopState.DISCONNECTED}
)

# ``LOOP_TRANSITIONS`` is DERIVED as of phase 2 (docs/design/ui-monitor.md §2.4):
# the authority is ``recipes/transitions.py``'s ``(state, outcome) -> state``
# table, and this is the legal-next picture the sidebar's STATE rail draws its
# brightness from - the same name, the same type, one fewer thing to keep in
# step. The two moves that are not a recipe's outcome (losing the monitor link,
# which happens TO the loop from any state, and the shell's own "the user copied
# it" / "the turn is over") are folded in over there, beside the table that
# earns them.
#
# Imported at the BOTTOM of this module on purpose: ``transitions`` needs
# ``LoopState``, which is defined above, so by the time this line runs the name
# it wants is already bound and the cycle costs nothing.
from agentclip.driver.automation.recipes.transitions import legal_next  # noqa: E402

LOOP_TRANSITIONS: dict[LoopState, frozenset[LoopState]] = legal_next()
