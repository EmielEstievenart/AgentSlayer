"""Phase transitions: legal flows work, illegal calls raise/noop predictably."""

from __future__ import annotations

import pytest

from agentclip.engine.engine import (
    AskUser,
    ChunkAck,
    Done,
    Engine,
    NewTurn,
    Noise,
    ProtocolError,
    Send,
)
from agentclip.engine.states import Decision, EngineStateError, Phase

READ_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
"""

STALE_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 turn=0===
"""

EDIT_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
hello from the model
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
"""

ASK_REPLY = """===CLIP:CALL id=1 tool=ask_user===
question: Should I also update the changelog?
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
"""

DONE_REPLY = """===CLIP:CALL id=1 tool=task_done===
summary <<EOT
All done.
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
"""

DONE_WITH_SIBLING_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=task_done===
summary <<EOT
Read it; done.
EOT
===CLIP:END===
===CLIP:EOM calls=2 turn=1===
"""

EMPTY_REPLY = """Looking around first.
===CLIP:EOM calls=0 turn=1===
"""


def test_initial_phase_is_idle(engine: Engine) -> None:
    snap = engine.status()
    assert snap.phase is Phase.IDLE
    assert snap.turn == 0
    assert snap.last_outbound_chars == 0


def test_illegal_calls_in_idle(engine: Engine) -> None:
    result = engine.ingest(READ_REPLY)
    assert isinstance(result, Noise) and result.reason == "wrong-phase"
    with pytest.raises(EngineStateError):
        engine.execute()
    with pytest.raises(EngineStateError):
        engine.decide(1, Decision.APPROVE)
    with pytest.raises(EngineStateError):
        engine.answer_user("hi")
    with pytest.raises(EngineStateError):
        engine.follow_up("more")
    with pytest.raises(EngineStateError):
        engine.undo_last_turn()


def test_start_task_bootstrap(engine: Engine) -> None:
    out = engine.start_task("Fix the bug.")
    assert out.kind == "bootstrap"
    assert out.turn == 1
    assert len(out.chunks) == 1
    assert "Fix the bug." in out.chunks[0]
    assert out.chunks[0].rstrip().endswith("===CLIP:EOM turn=1===")
    snap = engine.status()
    assert snap.phase is Phase.AWAITING_REPLY
    assert snap.turn == 1
    assert snap.last_outbound_chars == out.total_chars
    with pytest.raises(EngineStateError):
        engine.start_task("again")


def test_non_protocol_text_is_noise(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest("Sure! Here's a summary of what I would do...")
    assert isinstance(result, Noise) and result.reason == "not-protocol"
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_own_outbound_is_suppressed_as_duplicate(engine: Engine) -> None:
    out = engine.start_task("t")
    result = engine.ingest(out.chunks[0])
    assert isinstance(result, Noise) and result.reason == "duplicate"


def test_stale_turn_guard(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(STALE_REPLY)
    assert isinstance(result, Noise) and result.reason == "stale-turn"
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_ack_and_nack(engine: Engine) -> None:
    engine.start_task("t")
    ack = engine.ingest("===CLIP:ACK 2/3===")
    assert isinstance(ack, ChunkAck) and (ack.part, ack.total) == (2, 3)
    assert engine.status().phase is Phase.AWAITING_REPLY
    nack = engine.ingest("===CLIP:NACK reason=truncated===")
    assert isinstance(nack, ProtocolError) and "truncated" in nack.detail


def test_review_and_execute_flow(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(READ_REPLY)
    assert isinstance(result, NewTurn)
    assert engine.status().phase is Phase.REVIEW
    assert engine.pending() == ()  # read_file is auto
    assert engine.all_decided()
    mid = engine.ingest(READ_REPLY)
    assert isinstance(mid, Noise) and mid.reason == "wrong-phase"
    with pytest.raises(EngineStateError):
        engine.follow_up("not now")
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULTS turn=2===" in payload
    assert "status=ok" in payload
    assert "demo project" in payload  # README content came back
    snap = engine.status()
    assert snap.phase is Phase.AWAITING_REPLY
    assert snap.turn == 2


def test_duplicate_reply_after_roundtrip(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    engine.execute()
    again = engine.ingest(READ_REPLY)
    assert isinstance(again, Noise) and again.reason == "duplicate"


def test_execute_requires_all_decisions(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(EDIT_REPLY), NewTurn)
    assert len(engine.pending()) == 1
    assert not engine.all_decided()
    with pytest.raises(EngineStateError):
        engine.execute()
    with pytest.raises(ValueError, match="no call with id=99"):
        engine.decide(99, Decision.APPROVE)
    engine.decide(1, Decision.APPROVE)
    with pytest.raises(ValueError, match="already decided"):
        engine.decide(1, Decision.APPROVE)
    step = engine.execute()
    assert isinstance(step, Send)


def test_decide_on_auto_call_raises(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    with pytest.raises(ValueError, match="does not need a decision"):
        engine.decide(1, Decision.APPROVE)


def test_ask_user_pause_and_resume(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(ASK_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, AskUser)
    assert step.call_id == 1
    assert "changelog" in step.question
    assert engine.status().phase is Phase.AWAITING_USER
    noise = engine.ingest(READ_REPLY)
    assert isinstance(noise, Noise) and noise.reason == "wrong-phase"
    resumed = engine.answer_user("yes, please do")
    assert isinstance(resumed, Send)
    assert "yes, please do" in resumed.outbound.chunks[0]
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_task_done_alone_no_outbound(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(DONE_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Done)
    assert step.summary.strip() == "All done."
    assert step.outbound is None
    assert engine.status().phase is Phase.DONE
    assert isinstance(engine.ingest(READ_REPLY), Noise)  # ingest stays inert until reopened
    with pytest.raises(EngineStateError):
        engine.start_task("next")  # the bootstrap is one-shot; continue via follow_up
    # task_done completes the session, but the user may continue: a follow-up reopens it.
    reopened = engine.follow_up("one more thing")
    assert reopened.kind == "user_answer"
    assert "one more thing" in reopened.chunks[0]
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_follow_up_reopens_completed_session(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(DONE_REPLY), NewTurn)
    assert isinstance(engine.execute(), Done)
    assert engine.status().phase is Phase.DONE

    # A follow-up after task_done reopens the session (DONE -> AWAITING_REPLY).
    out = engine.follow_up("actually, also add a test")
    assert out.kind == "user_answer"
    assert out.turn == 2  # bootstrap=1; task_done had no sibling results; follow-up=2
    assert "===CLIP:TASK===" in out.chunks[0]
    assert "also add a test" in out.chunks[0]
    assert engine.status().phase is Phase.AWAITING_REPLY

    # ...and the reopened session ingests and runs another turn normally.
    next_reply = (
        "===CLIP:CALL id=1 tool=read_file===\n"
        "path: README.md\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 turn=2===\n"
    )
    assert isinstance(engine.ingest(next_reply), NewTurn)
    assert isinstance(engine.execute(), Send)
    assert engine.status().turn == 3  # follow-up=2, its results=3


def _done_reply(turn: int) -> str:
    return (
        "===CLIP:CALL id=1 tool=task_done===\n"
        "summary <<EOT\n"
        f"done at turn {turn}\n"  # distinct text per turn so the dedup guard never fires
        "EOT\n"
        "===CLIP:END===\n"
        f"===CLIP:EOM calls=1 turn={turn}===\n"
    )


def test_repeated_done_reopen_cycle(engine: Engine) -> None:
    """The DONE <-> AWAITING_REPLY loop is stable across iterations: complete,
    continue, complete again, continue again, with a monotonically rising turn."""
    engine.start_task("t")
    expected_turn = 1  # the bootstrap is turn 1; each done reply must echo the live turn
    for follow_up_text in ("keep going", "and again"):
        assert isinstance(engine.ingest(_done_reply(expected_turn)), NewTurn)
        assert isinstance(engine.execute(), Done)
        assert engine.status().phase is Phase.DONE
        out = engine.follow_up(follow_up_text)
        expected_turn += 1
        assert out.turn == expected_turn
        assert engine.status().phase is Phase.AWAITING_REPLY
        assert engine.status().turn == expected_turn


def test_task_done_with_sibling_results(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(DONE_WITH_SIBLING_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Done)
    assert step.outbound is not None
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=ok===" in payload
    assert "task_done" not in payload  # task_done itself produces no RESULT block
    assert engine.status().phase is Phase.DONE
    assert engine.status().turn == 2  # bootstrap=1; the sibling RESULTS advanced it to 2

    # Reopening from a DONE that already sent sibling results: the follow-up is
    # turn 3 (not 2), and the reopened session round-trips a turn=3 reply.
    out = engine.follow_up("one more change")
    assert out.turn == 3
    assert engine.status().phase is Phase.AWAITING_REPLY
    next_reply = (
        "===CLIP:CALL id=1 tool=read_file===\n"
        "path: README.md\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 turn=3===\n"
    )
    assert isinstance(engine.ingest(next_reply), NewTurn)
    assert isinstance(engine.execute(), Send)
    assert engine.status().turn == 4


def test_call_less_reply_gets_nudge(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(EMPTY_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert "no tool calls" in step.outbound.chunks[0]


def test_follow_up_task_payload(engine: Engine) -> None:
    engine.start_task("t")
    out = engine.follow_up("also update the docs")
    assert out.kind == "user_answer"
    assert out.turn == 2
    assert "===CLIP:TASK===" in out.chunks[0]
    assert "also update the docs" in out.chunks[0]
    assert engine.status().turn == 2
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_undo_with_nothing_to_undo_raises(engine: Engine) -> None:
    engine.start_task("t")
    with pytest.raises(EngineStateError, match="nothing to undo"):
        engine.undo_last_turn()
