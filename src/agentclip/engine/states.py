"""Engine phases, user decisions, and the legal-transition table.

The Engine (engine.py) is the only writer of its phase; it validates every
phase change against TRANSITIONS and raises EngineStateError when a public
method is called in a phase where it is not legal.
"""

from __future__ import annotations

from enum import Enum, auto


class Phase(Enum):
    IDLE = auto()  # constructed, no task yet
    AWAITING_REPLY = auto()  # outbound copied; waiting for the LLM reply
    REVIEW = auto()  # reply parsed; approval decisions outstanding or execute() due
    SENDING_CHUNKS = auto()  # M3: reserved for the PART/ACK chunked send
    AWAITING_USER = auto()  # ask_user hit mid-turn; waiting for answer_user()
    AWAITING_SUBAGENT = auto()  # delegate hit mid-turn; waiting for deliver_delegate_result()
    DONE = auto()  # task_done received; session complete


class Decision(Enum):
    APPROVE = auto()
    REJECT = auto()  # optional reason rides as Engine.decide(..., note=...)
    APPROVE_ALL_EDITS = auto()  # sticky for the session; never applies to commands
    # "Always allow calls like this one": remembers a permission rule for the
    # session (ruleset mode - engine/approval.py). Falls back to
    # APPROVE_ALL_EDITS's behaviour when no ruleset governs the session.
    APPROVE_ALWAYS = auto()


class EngineStateError(RuntimeError):
    """An Engine method was called in a phase where it is not legal."""


# phase -> phases legally reachable from it (self-transitions are always legal).
TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.IDLE: frozenset({Phase.AWAITING_REPLY}),
    Phase.AWAITING_REPLY: frozenset({Phase.REVIEW}),
    Phase.REVIEW: frozenset(
        {Phase.AWAITING_REPLY, Phase.AWAITING_USER, Phase.AWAITING_SUBAGENT, Phase.DONE}
    ),
    Phase.SENDING_CHUNKS: frozenset(),  # M3
    # ask_user and delegate can both appear in one reply, in either order, so
    # each parked phase must be able to hand over to the other.
    Phase.AWAITING_USER: frozenset(
        {Phase.AWAITING_REPLY, Phase.AWAITING_SUBAGENT, Phase.DONE}
    ),
    Phase.AWAITING_SUBAGENT: frozenset(
        {Phase.AWAITING_REPLY, Phase.AWAITING_USER, Phase.DONE}
    ),
    # DONE is not a dead end: task_done completes the session, but the user may
    # continue (protocol.md section 8), and a follow_up reopens it for a reply.
    Phase.DONE: frozenset({Phase.AWAITING_REPLY}),
}


def can_transition(current: Phase, new: Phase) -> bool:
    return new is current or new in TRANSITIONS[current]
