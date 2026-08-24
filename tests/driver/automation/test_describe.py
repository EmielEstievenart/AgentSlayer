"""The state label's table, asserted as a table.

Three things are worth pinning about :func:`describe`, and they are different
kinds of claim:

1. **Totality.** Every ``(Phase, LoopState)`` pair answers with words. A new
   member of either enum (phase 5's ``LoopState.DISCONNECTED``) must fail here,
   loudly, rather than reach a user as a ``KeyError`` inside a repaint.
2. **Distinctness.** Two situations that mean different things to the person
   watching must not read the same. Collapsing is fine where the situations
   ARE the same fact (a phase whose loop state is idle vs interpreting); it is
   a bug where one asks for a Ctrl+V and the other asks for a Ctrl+C.
3. **The exact words** for the handful of pairs the user spends the session
   looking at.
"""

from __future__ import annotations

import pytest

from agentclip.driver.automation.describe import LOOP_LABEL, PHASE_LABEL, describe
from agentclip.driver.automation.loop_state import ATTENTION_STATES, LoopState
from agentclip.engine.states import Phase

ALL_PAIRS = [(phase, state) for phase in Phase for state in LoopState]


def test_tables_are_total() -> None:
    """Both tables cover their enum - the guard a new member trips first."""
    assert set(PHASE_LABEL) == set(Phase)
    assert set(LOOP_LABEL) == set(LoopState)


@pytest.mark.parametrize(("phase", "state"), ALL_PAIRS)
def test_every_pair_has_words(phase: Phase, state: LoopState) -> None:
    label = describe(phase, state)
    assert isinstance(label, str)
    assert label.strip()
    # The label is a sentence for a human, never an enum name leaking through.
    assert label == label.strip()
    assert "_" not in label


def test_attention_states_outrank_every_phase() -> None:
    """The two "the next move is yours" states are never buried by the phase.

    This is the precedence rule stated as a property rather than a row: for
    both attention states, all seven phases produce that state's own words.
    """
    for state in ATTENTION_STATES:
        labels = {describe(phase, state) for phase in Phase}
        assert labels == {LOOP_LABEL[state]}


# Pairs that mean genuinely different things to the user. Each entry is
# (pair_a, pair_b, what_would_be_lost).
DISTINCT_PAIRS = [
    (
        (Phase.IDLE, LoopState.IDLE),
        (Phase.DONE, LoopState.IDLE),
        "no session vs a finished task the user may still continue",
    ),
    (
        (Phase.IDLE, LoopState.IDLE),
        (Phase.AWAITING_REPLY, LoopState.IDLE),
        "nothing outstanding vs a payload sitting there waiting to be pasted",
    ),
    (
        (Phase.AWAITING_REPLY, LoopState.MANUAL_INSERT),
        (Phase.AWAITING_REPLY, LoopState.MANUAL_COPY),
        "Ctrl+V the payload in vs Ctrl+C the reply out - opposite ends of the trip",
    ),
    (
        (Phase.AWAITING_REPLY, LoopState.WAIT_SEND),
        (Phase.AWAITING_REPLY, LoopState.WAIT_GENERATE),
        "press Enter vs the model already running",
    ),
    (
        (Phase.AWAITING_REPLY, LoopState.MANUAL_COPY),
        (Phase.AWAITING_REPLY, LoopState.AUTO_COPY),
        "the user copies vs AgentClip clicks the copy button",
    ),
    (
        (Phase.AWAITING_REPLY, LoopState.MANUAL_INSERT),
        (Phase.AWAITING_REPLY, LoopState.AUTO_INSERT),
        "the user pastes vs AgentClip pastes",
    ),
    (
        (Phase.REVIEW, LoopState.INTERPRETING),
        (Phase.AWAITING_USER, LoopState.INTERPRETING),
        "the turn is running vs the turn is parked on a question for the user",
    ),
    (
        (Phase.REVIEW, LoopState.INTERPRETING),
        (Phase.AWAITING_SUBAGENT, LoopState.INTERPRETING),
        "this session working vs waiting on a delegated one",
    ),
    (
        (Phase.AWAITING_USER, LoopState.INTERPRETING),
        (Phase.DONE, LoopState.INTERPRETING),
        "answer needed vs the task finished",
    ),
]


@pytest.mark.parametrize(("pair_a", "pair_b", "lost"), DISTINCT_PAIRS)
def test_distinct_situations_read_differently(
    pair_a: tuple[Phase, LoopState],
    pair_b: tuple[Phase, LoopState],
    lost: str,
) -> None:
    assert describe(*pair_a) != describe(*pair_b), lost


def test_collapsing_is_allowed_where_the_situation_is_the_same() -> None:
    """The deferring loop states are deliberately indistinguishable.

    IDLE and INTERPRETING both hand the label to the phase, so a phase reads
    the same under either. That is the design, not a leak: what the user needs
    is the phase, and the rail already shows which of the two the loop is in.
    """
    for phase in Phase:
        assert describe(phase, LoopState.IDLE) == describe(phase, LoopState.INTERPRETING)


@pytest.mark.parametrize(
    ("phase", "state", "expected"),
    [
        (Phase.IDLE, LoopState.IDLE, "idle"),
        (Phase.AWAITING_REPLY, LoopState.IDLE, "ready - paste the reply"),
        (Phase.AWAITING_REPLY, LoopState.WAIT_GENERATE, "generating..."),
        (Phase.REVIEW, LoopState.WAIT_GENERATE, "generating..."),
        (
            Phase.AWAITING_REPLY,
            LoopState.MANUAL_INSERT,
            "paste it yourself - Ctrl+V into the chat box",
        ),
        (Phase.AWAITING_REPLY, LoopState.MANUAL_COPY, "copy the reply yourself"),
        (Phase.DONE, LoopState.IDLE, "done - reply to continue"),
        (Phase.DONE, LoopState.INTERPRETING, "done - reply to continue"),
        (Phase.AWAITING_USER, LoopState.INTERPRETING, "answer needed"),
        (Phase.AWAITING_SUBAGENT, LoopState.INTERPRETING, "sub-agent running"),
        (Phase.REVIEW, LoopState.INTERPRETING, "working..."),
        (Phase.AWAITING_REPLY, LoopState.AUTO_INSERT, "pasting into the chat box"),
        (Phase.AWAITING_REPLY, LoopState.WAIT_SEND, "press Enter to send"),
        (Phase.AWAITING_REPLY, LoopState.AUTO_COPY, "copying the reply"),
    ],
)
def test_exact_wording(phase: Phase, state: LoopState, expected: str) -> None:
    assert describe(phase, state) == expected
