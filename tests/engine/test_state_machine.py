"""Phase transitions: legal flows work, illegal calls raise/noop predictably."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import Config
from agentclip.engine.engine import (
    AskUser,
    AutoReply,
    ChunkAck,
    Done,
    Engine,
    NewTurn,
    Noise,
    ProtocolError,
    Send,
)
from agentclip.engine.states import (
    TRANSITIONS,
    Decision,
    EngineStateError,
    Phase,
    can_transition,
)

READ_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

WRONG_CHAT_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 chat=silver-otter===
"""

NO_CHAT_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1===
"""

# A reply the chat rendered outside a ~~~~ fence: markdown ate the newlines, so
# the copy glued a whole follow-up message onto the previous EOM line. The chat
# name is RIGHT - only the line breaks are gone (protocol.md 1.4 tolerance #14).
FLATTENED_REPLY = """===CLIP:CALL id=1 tool=task_done===
summary: earlier work finished
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===~~~~ ===CLIP:CALL id=1 \
tool=run_command=== command: echo hello world ===CLIP:END===

===CLIP:EOM calls=1 chat=amber-falcon===
"""

# Sentinels present, no EOM at all: the truncation signature, which must stay
# on the truncated-reply path instead of tripping the chat gate.
TRUNCATED_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
the reply was cut off before the heredoc terminator
"""

EDIT_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
hello from the model
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

ASK_REPLY = """===CLIP:CALL id=1 tool=ask_user===
question: Should I also update the changelog?
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

DONE_REPLY = """===CLIP:CALL id=1 tool=task_done===
summary <<EOT
All done.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

DONE_WITH_SIBLING_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=task_done===
summary <<EOT
Read it; done.
EOT
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

EMPTY_REPLY = """Looking around first.
===CLIP:EOM calls=0 chat=amber-falcon===
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
    assert out.chunks[0].rstrip().endswith("===CLIP:EOM turn=1 chat=amber-falcon===")
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


def test_own_outbound_is_suppressed(engine: Engine) -> None:
    out = engine.start_task("t")
    result = engine.ingest(out.chunks[0])
    assert isinstance(result, Noise) and result.reason == "own-outbound"


def test_a_fenced_results_payload_pasted_back_is_still_own_outbound(engine: Engine) -> None:
    """Results now go out inside a ~~~~ fence. The self-write key strips fence
    lines before hashing (normalized_hash), so the fence cannot smuggle our own
    payload past the suppression and back in as a reply."""
    engine.start_task("t")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert payload.startswith("~~~~\n")
    assert engine.ingest(payload) == Noise("own-outbound")


def test_chat_name_is_stamped_on_the_bootstrap(engine: Engine) -> None:
    out = engine.start_task("t")
    assert engine.chat_name == "amber-falcon"
    payload = out.chunks[0]
    assert "This chat's name is amber-falcon." in payload
    assert "===CLIP:EOM calls=N chat=amber-falcon===" in payload
    assert payload.rstrip().endswith("===CLIP:EOM turn=1 chat=amber-falcon===")


def test_wrong_chat_name_is_noise(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(WRONG_CHAT_REPLY)
    assert isinstance(result, Noise) and result.reason == "wrong-chat"
    assert engine.status().phase is Phase.AWAITING_REPLY
    # Rejected pastes are not remembered: the reason must be stable on a retry.
    again = engine.ingest(WRONG_CHAT_REPLY)
    assert isinstance(again, Noise) and again.reason == "wrong-chat"


def test_missing_chat_name_is_noise(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(NO_CHAT_REPLY)
    assert isinstance(result, Noise) and result.reason == "missing-chat"
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_chat_name_match_is_case_and_quote_tolerant(engine: Engine) -> None:
    engine.start_task("t")
    reply = READ_REPLY.replace("chat=amber-falcon", "chat=`Amber-Falcon`")
    assert isinstance(engine.ingest(reply), NewTurn)


def test_flattened_reply_is_bounced_back_to_the_model(engine: Engine) -> None:
    """The paste is ours - the chat name survived - but the reply is not whole:
    a `run_command` block is glued onto the EOM line and was never parsed. The
    fragment that did parse must not execute (the `task_done` in it would end
    the session!), and the model - not just the user - must be told, or it sits
    waiting for results that are never coming."""
    engine.start_task("t")
    result = engine.ingest(FLATTENED_REPLY)
    assert isinstance(result, AutoReply)
    assert "line breaks" in result.detail and "~~~~" in result.detail
    assert "chat name" not in result.detail
    payload = result.outbound.chunks[0]
    assert "===CLIP:RESULT id=0 status=error code=reply_flattened===" in payload
    assert "chat=amber-falcon" in payload  # it is an ordinary outbound payload
    # It quotes what actually arrived, so the model can see its own lost break.
    assert "===CLIP:EOM calls=1 chat=amber-falcon===~~~~ ===CLIP:CALL" in payload
    # And it asks for ALL of it back, inside one fence - not just the tail.
    assert "ENTIRE reply" in payload and "~~~~ fence" in payload
    assert "===CLIP:EOM" in payload
    assert engine.status().phase is Phase.AWAITING_REPLY  # nothing ran


def test_flattened_bounces_are_capped_then_handed_to_the_user(engine: Engine) -> None:
    """A host that strips newlines from every copy would ping-pong forever, so
    the engine asks for a resend twice and then tells the human instead."""
    engine.start_task("t")
    assert isinstance(engine.ingest(FLATTENED_REPLY), AutoReply)
    assert isinstance(engine.ingest(FLATTENED_REPLY), AutoReply)
    result = engine.ingest(FLATTENED_REPLY)
    assert isinstance(result, ProtocolError)
    assert "line breaks" in result.detail and "~~~~" in result.detail
    assert "chat name" not in result.detail
    assert "===CLIP:EOM calls=1 chat=amber-falcon===~~~~" in result.detail  # quoted
    assert engine.status().phase is Phase.AWAITING_REPLY  # still nothing ran
    # Past the cap it stays the user's problem: no further payloads are composed.
    assert isinstance(engine.ingest(FLATTENED_REPLY), ProtocolError)


def test_a_whole_reply_restores_the_flattened_bounce_budget(engine: Engine) -> None:
    """The cap is for a transport that is broken NOW. One paste that arrived
    with its line breaks intact proves it is not, so the next flattening is a
    fresh incident with its own two tries."""
    engine.start_task("t")
    assert isinstance(engine.ingest(FLATTENED_REPLY), AutoReply)
    assert isinstance(engine.ingest(FLATTENED_REPLY), AutoReply)
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    assert isinstance(engine.execute(), Send)
    assert isinstance(engine.ingest(FLATTENED_REPLY), AutoReply)


# -- tolerance #15: a reply that arrived outside a fence ----------------------
#
# Only on a service preset that asks for it (`require_fenced_reply`): unfenced
# arrivals are legitimate wherever the per-code-block copy button hands over the
# block's contents without its fence lines (golden fixture 002). On a host whose
# whole-message copy markdown-processes unfenced text instead, the missing fence
# is the only evidence there is that the code has been rewritten - link-stripped
# `[label](target)` shapes, sometimes collapsed newlines - and the reply parses
# perfectly either way, so the refusal has to happen here or not at all.


@pytest.fixture
def fenced_engine(config: Config, make_engine) -> Engine:  # type: ignore[no-untyped-def]
    """An engine whose service preset demands fenced replies."""
    key = config.general.service
    services = dict(config.services)
    services[key] = replace(services[key], require_fenced_reply=True)
    return make_engine(replace(config, services=services))


def _fenced(reply: str, fence: str = "~~~~") -> str:
    return f"{fence}\n{reply}{fence}\n"


# The one that must NOT be fooled: every fence-looking line in it is inside a
# heredoc body, i.e. content of a file being written, not evidence about how the
# reply itself was rendered.
HEREDOC_FENCES_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: README.md
content <<EOT
# Title
```python
print("hi")
```
~~~~
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

# Sentinels, one complete call, no EOM: the truncation signature. The truncated
# path would happily run that complete call - which is exactly the silent
# corruption this gate exists to stop.
TRUNCATED_UNFENCED_REPLY = """===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
hello from the model
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: other.txt
content <<EOT
the reply was cut off before the heredoc terminator
"""


def test_an_unfenced_reply_is_bounced_when_the_service_asks_for_fences(
    fenced_engine: Engine, project: Path
) -> None:
    fenced_engine.start_task("t")
    result = fenced_engine.ingest(EDIT_REPLY)
    assert isinstance(result, AutoReply)
    payload = result.outbound.chunks[0]
    assert "===CLIP:RESULT id=0 status=error code=reply_unfenced===" in payload
    assert "NOTHING in it ran" in payload
    assert "not a parse failure" in payload.replace("NOT a parse failure", "not a parse failure")
    assert "[this](int a)" in payload  # the corruption, made concrete
    assert "ENTIRE reply" in payload and "~~~~ fence" in payload
    assert fenced_engine.status().phase is Phase.AWAITING_REPLY  # no turn was built
    assert not (project / "notes.txt").exists()  # and the write never happened


def test_an_unfenced_reply_runs_normally_when_the_service_does_not_ask(
    engine: Engine,
) -> None:
    """The default. Refusing unfenced text globally would break every host whose
    copy button strips the fence for us."""
    engine.start_task("t")
    assert isinstance(engine.ingest(EDIT_REPLY), NewTurn)


@pytest.mark.parametrize("fence", ["~~~~", "```", "```text"])
def test_a_fenced_reply_passes_the_gate(fenced_engine: Engine, fence: str) -> None:
    """Tilde or backtick, any length: the parser accepts both as fences, so both
    are proof that fence lines survived this host's copy."""
    fenced_engine.start_task("t")
    assert isinstance(fenced_engine.ingest(_fenced(READ_REPLY, fence)), NewTurn)


def test_a_reply_with_no_calls_is_not_gated(fenced_engine: Engine) -> None:
    """A zero-call reply has nothing executable to corrupt, and it already earns
    the no-calls nag. ACK/NACK are taught as bare single lines and would be
    refused outright by a gate that did not stop at calls."""
    fenced_engine.start_task("t")
    assert isinstance(fenced_engine.ingest(EMPTY_REPLY), NewTurn)
    assert isinstance(fenced_engine.execute(), Send)
    assert isinstance(
        fenced_engine.ingest("===CLIP:ACK 2/3 chat=amber-falcon==="), ChunkAck
    )


def test_fences_inside_heredoc_content_do_not_count_as_a_fence(
    fenced_engine: Engine, project: Path
) -> None:
    """saw_fence is structural on purpose: a reply WRITING a markdown file is
    full of ``` lines and is no more fenced for it."""
    fenced_engine.start_task("t")
    result = fenced_engine.ingest(HEREDOC_FENCES_REPLY)
    assert isinstance(result, AutoReply)
    assert "code=reply_unfenced" in result.outbound.chunks[0]
    assert (project / "README.md").read_text(encoding="utf-8") == "demo project for engine tests\n"


def test_a_truncated_unfenced_reply_with_calls_is_still_bounced(
    fenced_engine: Engine, project: Path
) -> None:
    fenced_engine.start_task("t")
    result = fenced_engine.ingest(TRUNCATED_UNFENCED_REPLY)
    assert isinstance(result, AutoReply)
    assert "code=reply_unfenced" in result.outbound.chunks[0]
    assert fenced_engine.status().phase is Phase.AWAITING_REPLY
    # The complete call in it - which the truncated path WOULD have executed.
    assert not (project / "notes.txt").exists()


def test_flattened_and_unfenced_share_one_bounce_budget(fenced_engine: Engine) -> None:
    """One fault class, one budget: both symptoms mean "the transport mangled
    this reply" and both are answered by the same fenced resend. Separate
    budgets would let a host that alternates symptoms ping-pong twice as long
    before the human hears about it."""
    fenced_engine.start_task("t")
    assert isinstance(fenced_engine.ingest(FLATTENED_REPLY), AutoReply)
    assert isinstance(fenced_engine.ingest(EDIT_REPLY), AutoReply)
    third = fenced_engine.ingest(READ_REPLY)  # unfenced again: over the shared cap
    assert isinstance(third, ProtocolError)
    assert "stopped asking" in third.detail
    assert "require-fenced" in third.detail
    assert fenced_engine.status().phase is Phase.AWAITING_REPLY


def test_two_unfenced_bounces_then_the_user_is_told(fenced_engine: Engine) -> None:
    fenced_engine.start_task("t")
    assert isinstance(fenced_engine.ingest(READ_REPLY), AutoReply)
    assert isinstance(fenced_engine.ingest(READ_REPLY), AutoReply)
    result = fenced_engine.ingest(READ_REPLY)
    assert isinstance(result, ProtocolError)
    assert "raw/code view" in result.detail
    # Past the cap it stays the user's problem: nothing further is composed.
    assert isinstance(fenced_engine.ingest(READ_REPLY), ProtocolError)


def test_an_accepted_fenced_reply_restores_the_shared_budget(
    fenced_engine: Engine,
) -> None:
    """The cap is for a transport that is broken NOW. One paste that arrived
    whole - line breaks AND fence - proves it is not."""
    fenced_engine.start_task("t")
    assert isinstance(fenced_engine.ingest(FLATTENED_REPLY), AutoReply)
    assert isinstance(fenced_engine.ingest(READ_REPLY), AutoReply)  # unfenced
    assert isinstance(fenced_engine.ingest(_fenced(READ_REPLY)), NewTurn)
    assert isinstance(fenced_engine.execute(), Send)
    assert isinstance(fenced_engine.ingest(READ_REPLY), AutoReply)  # a fresh two tries
    assert isinstance(fenced_engine.ingest(READ_REPLY), AutoReply)


def test_the_unfenced_bounce_is_audited_with_the_raw_paste(
    fenced_engine: Engine,
) -> None:
    fenced_engine.start_task("t")
    assert isinstance(fenced_engine.ingest(READ_REPLY), AutoReply)
    events = [
        json.loads(line)
        for line in (fenced_engine.status().session_dir / "transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    unfenced = [e for e in events if e["t"] == "unfenced"]
    assert len(unfenced) == 1
    assert unfenced[0]["attempt"] == 1
    assert unfenced[0]["bounced"] is True
    assert unfenced[0]["raw"] == READ_REPLY  # what the transport actually delivered
    assert not any(e["t"] == "inbound" for e in events)  # it never became a turn


def test_truncated_reply_without_eom_skips_the_chat_gate(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(TRUNCATED_REPLY)
    assert isinstance(result, NewTurn)
    assert result.reply.truncated
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "code=reply_truncated" in payload
    assert "chat=amber-falcon" in payload  # our own EOM still carries the name


def test_ack_and_nack(engine: Engine) -> None:
    engine.start_task("t")
    ack = engine.ingest("===CLIP:ACK 2/3 chat=amber-falcon===")
    assert isinstance(ack, ChunkAck) and (ack.part, ack.total) == (2, 3)
    assert engine.status().phase is Phase.AWAITING_REPLY
    nack = engine.ingest("===CLIP:NACK reason=truncated chat=amber-falcon===")
    assert isinstance(nack, ProtocolError) and "truncated" in nack.detail


def test_ack_nack_without_the_chat_name_is_noise(engine: Engine) -> None:
    engine.start_task("t")
    assert engine.ingest("===CLIP:ACK 2/3===") == Noise("missing-chat")
    assert engine.ingest("===CLIP:ACK 2/3 chat=silver-otter===") == Noise("wrong-chat")
    assert engine.ingest("===CLIP:NACK reason=truncated===") == Noise("missing-chat")


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


def test_re_pasted_reply_is_reinterpreted(engine: Engine) -> None:
    """A reply AgentClip already ran, pasted again, RE-RUNS by design.

    Eligibility has exactly two preconditions: the clipboard CHANGED (the clip
    watcher enforces that byte-wise, and our own writes advance its baseline)
    and the chat name MATCHES. So identical text arriving here twice is always
    two deliberate copies - either the user re-copying an older message to have
    it re-interpreted, or a model that meant its answer twice."""
    engine.start_task("t")
    assert isinstance(engine.ingest(READ_REPLY), NewTurn)
    engine.execute()
    again = engine.ingest(READ_REPLY)
    assert isinstance(again, NewTurn)
    assert engine.status().phase is Phase.REVIEW
    assert isinstance(engine.execute(), Send)  # and it really runs again


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
    # A stray ACK cannot revive a completed session; only a whole, chat-stamped
    # reply reopens it (the DONE-reopen tests below).
    assert engine.ingest("===CLIP:ACK 1/2 chat=amber-falcon===") == Noise("wrong-phase")
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
        "===CLIP:EOM calls=1 chat=amber-falcon===\n"
    )
    assert isinstance(engine.ingest(next_reply), NewTurn)
    assert isinstance(engine.execute(), Send)
    assert engine.status().turn == 3  # follow-up=2, its results=3


def _drive_to_done(engine: Engine) -> None:
    """start_task -> task_done -> execute: leaves the engine in Phase.DONE."""
    engine.start_task("t")
    assert isinstance(engine.ingest(DONE_REPLY), NewTurn)
    assert isinstance(engine.execute(), Done)
    assert engine.status().phase is Phase.DONE


def test_valid_reply_in_done_reopens_the_session(engine: Engine) -> None:
    """A paste is eligible when the clipboard changed and the chat name matches
    - completion is not a third precondition. A whole, chat-stamped reply
    arriving after task_done reopens the session and runs."""
    _drive_to_done(engine)
    result = engine.ingest(READ_REPLY)
    assert isinstance(result, NewTurn)
    assert engine.status().phase is Phase.REVIEW
    assert isinstance(engine.execute(), Send)
    events = [
        json.loads(line)
        for line in (engine.status().session_dir / "transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert any(e["t"] == "reopened" for e in events)  # the reopen is audited


def test_ack_in_done_does_not_reopen(engine: Engine) -> None:
    """An ACK belongs to a PART handshake; once the session is complete there is
    nothing for it to acknowledge, so it must not revive the session."""
    _drive_to_done(engine)
    assert engine.ingest("===CLIP:ACK 2/3 chat=amber-falcon===") == Noise("wrong-phase")
    assert engine.status().phase is Phase.DONE


def test_truncated_paste_in_done_does_not_reopen(engine: Engine) -> None:
    """No EOM means no chat name, and the chat gate deliberately skips it. Such
    a paste has never PROVEN it is ours, so it cannot reopen a finished session
    - that would breach the chat-name precondition."""
    _drive_to_done(engine)
    assert engine.ingest(TRUNCATED_REPLY) == Noise("wrong-phase")
    assert engine.status().phase is Phase.DONE


def test_wrong_chat_paste_in_done_is_wrong_chat(engine: Engine) -> None:
    _drive_to_done(engine)
    assert engine.ingest(WRONG_CHAT_REPLY) == Noise("wrong-chat")
    assert engine.status().phase is Phase.DONE


def test_own_outbound_in_done_is_still_own_outbound(engine: Engine) -> None:
    out = engine.start_task("t")
    assert isinstance(engine.ingest(DONE_REPLY), NewTurn)
    assert isinstance(engine.execute(), Done)
    assert engine.status().phase is Phase.DONE
    assert engine.ingest(out.chunks[0]) == Noise("own-outbound")
    assert engine.status().phase is Phase.DONE


def _done_reply(turn: int) -> str:
    return (
        "===CLIP:CALL id=1 tool=task_done===\n"
        "summary <<EOT\n"
        f"done at turn {turn}\n"  # distinct text per turn so the dedup guard never fires
        "EOT\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 chat=amber-falcon===\n"
    )


def test_repeated_done_reopen_cycle(engine: Engine) -> None:
    """The DONE <-> AWAITING_REPLY loop is stable across iterations: complete,
    continue, complete again, continue again, with a monotonically rising turn."""
    engine.start_task("t")
    expected_turn = 1  # the bootstrap is turn 1
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
        "===CLIP:EOM calls=1 chat=amber-falcon===\n"
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


SAY_ONLY_REPLY = """===CLIP:SAY===
Hi! What would you like me to work on?
===CLIP:END===
===CLIP:EOM calls=0 chat=amber-falcon===
"""


def test_a_say_only_reply_parks_the_session_waiting_for_the_user(engine: Engine) -> None:
    """The bootstrap's own escape hatch ("a greeting or question needing nothing
    touched gets one SAY block and EOM") must not be answered with the no-calls
    nag: the model is talking to the user, so the session parks as after
    task_done - nothing goes out, a follow-up reopens it."""
    engine.start_task("t")
    assert isinstance(engine.ingest(SAY_ONLY_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Done)
    assert step.waiting
    assert step.outbound is None and step.summary == ""
    assert engine.status().phase is Phase.DONE

    out = engine.follow_up("please read the README")
    assert out.kind == "user_answer"
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_a_sub_agent_saying_things_is_still_nagged(make_engine) -> None:  # type: ignore[no-untyped-def]
    """A sub-agent has no user to wait on: a SAY-only reply from it is a stall,
    and the nag is the only thing that gets it moving again."""
    engine = make_engine(role="subagent")
    engine.start_task("t")
    assert isinstance(engine.ingest(SAY_ONLY_REPLY), NewTurn)
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


def test_subagent_phase_is_reachable_from_the_two_mid_turn_phases() -> None:
    # A delegate call can be the first parked call of a turn (from REVIEW) or
    # can follow an ask_user that was just answered (from AWAITING_USER).
    assert can_transition(Phase.REVIEW, Phase.AWAITING_SUBAGENT)
    assert can_transition(Phase.AWAITING_USER, Phase.AWAITING_SUBAGENT)


def test_subagent_phase_resumes_into_send_ask_or_done() -> None:
    assert TRANSITIONS[Phase.AWAITING_SUBAGENT] == frozenset(
        {Phase.AWAITING_REPLY, Phase.AWAITING_USER, Phase.DONE}
    )
    # Two delegate calls in one reply park twice: the self-transition is legal.
    assert can_transition(Phase.AWAITING_SUBAGENT, Phase.AWAITING_SUBAGENT)


def test_subagent_phase_is_unreachable_outside_a_turn() -> None:
    for phase in (Phase.IDLE, Phase.AWAITING_REPLY, Phase.DONE):
        assert not can_transition(phase, Phase.AWAITING_SUBAGENT), phase


def test_deliver_delegate_result_outside_the_subagent_phase_raises(engine: Engine) -> None:
    with pytest.raises(EngineStateError, match="AWAITING_SUBAGENT"):
        engine.deliver_delegate_result("anything")


def test_undo_with_nothing_to_undo_raises(engine: Engine) -> None:
    engine.start_task("t")
    with pytest.raises(EngineStateError, match="nothing to undo"):
        engine.undo_last_turn()
