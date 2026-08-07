"""Engine-side delegation: the parked `delegate` call and its resume path."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentclip.engine.engine import AskUser, Delegate, Done, Engine, NewTurn, Send
from agentclip.engine.states import Decision, EngineStateError, Phase
from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import (
    ToolContext,
    ToolRegistry,
    ToolSpec,
    default_registry,
    tool_handler,
)

# Same local alias as tests/engine/test_cancel.py: the conftest fixture is
# injected by name, only the annotation is spelled out here.
EngineFactory = Callable[..., Engine]

DELEGATE_MID_TURN_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=delegate===
task <<EOT
Survey src/ and report how a region capture reaches the matcher.
EOT
context: the caller only needs function names
===CLIP:END===
===CLIP:CALL id=3 tool=write_file===
path: notes.txt
content <<EOT
written after the sub-agent came back
EOT
===CLIP:END===
===CLIP:EOM calls=3 chat=amber-falcon===
"""

DELEGATE_WITHOUT_TASK_REPLY = """===CLIP:CALL id=1 tool=delegate===
context: no task at all
===CLIP:END===
===CLIP:CALL id=2 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

ASK_THEN_DELEGATE_REPLY = """===CLIP:CALL id=1 tool=ask_user===
question: which module should the sub-agent read?
===CLIP:END===
===CLIP:CALL id=2 tool=delegate===
task: read what the user named and summarise it
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

DELEGATE_THEN_ASK_REPLY = """===CLIP:CALL id=1 tool=delegate===
task: survey the screen package
===CLIP:END===
===CLIP:CALL id=2 tool=ask_user===
question: shall I act on what the sub-agent found?
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

TWO_DELEGATES_REPLY = """===CLIP:CALL id=1 tool=delegate===
task: first bounded chunk
===CLIP:END===
===CLIP:CALL id=2 tool=delegate===
task: second bounded chunk
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

SUBAGENT_DONE_REPLY = """===CLIP:CALL id=1 tool=task_done===
summary: surveyed the package
result <<EOT
capture.grab_region() -> RegionImage -> matcher.find_template().
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

DONE_WITHOUT_RESULT_REPLY = """===CLIP:CALL id=1 tool=task_done===
summary: nothing to hand back
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""

SLOW_THEN_DELEGATE_REPLY = """===CLIP:CALL id=1 tool=slow_tool===
===CLIP:END===
===CLIP:CALL id=2 tool=delegate===
task: this must never start
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

NESTED_DELEGATE_REPLY = """===CLIP:CALL id=1 tool=delegate===
task: delegate again, which must not be possible
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


@pytest.fixture
def master(make_engine: EngineFactory) -> Engine:
    """A master engine whose sub-agent chat is calibrated (delegate offered)."""
    return make_engine(tools=default_registry(allow_delegate=True))


def test_delegate_parks_mid_turn_and_resumes_into_the_rest(
    master: Engine, project: Path
) -> None:
    master.start_task("t")
    assert isinstance(master.ingest(DELEGATE_MID_TURN_REPLY), NewTurn)
    # delegate is intercepted by name: it is never a pending approval.
    assert [p.call.id for p in master.pending()] == [3]
    master.decide(3, Decision.APPROVE)

    step = master.execute()
    assert isinstance(step, Delegate)
    assert step.call_id == 2
    assert step.task.startswith("Survey src/")
    assert step.context == "the caller only needs function names"
    assert master.status().phase is Phase.AWAITING_SUBAGENT
    # The write has NOT run yet: the turn is parked on the delegation.
    assert not (project / "notes.txt").exists()

    resumed = master.deliver_delegate_result("the sub-agent's verbatim deliverable")
    assert isinstance(resumed, Send)
    payload = resumed.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=ok===" in payload  # the read
    assert "===CLIP:RESULT id=2 status=ok===" in payload  # the delegation
    assert "the sub-agent's verbatim deliverable" in payload
    assert "===CLIP:RESULT id=3 status=ok===" in payload  # the write, after resume
    assert master.status().phase is Phase.AWAITING_REPLY


def test_delegate_failure_comes_back_as_an_error_result(master: Engine) -> None:
    master.start_task("t")
    assert isinstance(master.ingest(TWO_DELEGATES_REPLY), NewTurn)
    assert isinstance(master.execute(), Delegate)
    step = master.deliver_delegate_result(
        "delegation is unavailable: the sub-agent chat window is not calibrated.",
        status="error",
        code="bad_param",
    )
    # The second delegate still parks; failure of the first does not abort.
    assert isinstance(step, Delegate) and step.call_id == 2
    final = master.deliver_delegate_result("second deliverable")
    assert isinstance(final, Send)
    payload = final.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=error code=bad_param===" in payload
    assert "not calibrated" in payload
    assert "===CLIP:RESULT id=2 status=ok===" in payload
    assert "second deliverable" in payload


def test_delegate_without_a_task_errors_without_parking(master: Engine) -> None:
    master.start_task("t")
    assert isinstance(master.ingest(DELEGATE_WITHOUT_TASK_REPLY), NewTurn)
    step = master.execute()
    assert isinstance(step, Send)  # never parked
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=error code=missing_param===" in payload
    assert "missing required parameter: task" in payload
    # The sibling call still ran: one bad delegate does not abort the turn.
    assert "===CLIP:RESULT id=2 status=ok===" in payload
    assert master.status().phase is Phase.AWAITING_REPLY


def test_deliver_delegate_result_in_the_wrong_phase_raises(master: Engine) -> None:
    master.start_task("t")
    with pytest.raises(EngineStateError, match="deliver_delegate_result"):
        master.deliver_delegate_result("nobody asked")
    assert isinstance(master.ingest(DELEGATE_MID_TURN_REPLY), NewTurn)
    with pytest.raises(EngineStateError):
        master.deliver_delegate_result("still nobody asked")


def test_ask_user_then_delegate_in_one_reply(master: Engine) -> None:
    master.start_task("t")
    assert isinstance(master.ingest(ASK_THEN_DELEGATE_REPLY), NewTurn)
    ask = master.execute()
    assert isinstance(ask, AskUser)
    assert master.status().phase is Phase.AWAITING_USER
    step = master.answer_user("the screen package")
    assert isinstance(step, Delegate)
    assert master.status().phase is Phase.AWAITING_SUBAGENT
    final = master.deliver_delegate_result("what the sub-agent found")
    assert isinstance(final, Send)
    payload = final.outbound.chunks[0]
    assert "the screen package" in payload
    assert "what the sub-agent found" in payload


def test_delegate_then_ask_user_in_one_reply(master: Engine) -> None:
    master.start_task("t")
    assert isinstance(master.ingest(DELEGATE_THEN_ASK_REPLY), NewTurn)
    step = master.execute()
    assert isinstance(step, Delegate)
    ask = master.deliver_delegate_result("the survey")
    assert isinstance(ask, AskUser)
    assert master.status().phase is Phase.AWAITING_USER
    final = master.answer_user("yes, go ahead")
    assert isinstance(final, Send)
    payload = final.outbound.chunks[0]
    assert "the survey" in payload
    assert "yes, go ahead" in payload


def test_two_delegations_in_one_reply_park_one_at_a_time(master: Engine) -> None:
    master.start_task("t")
    assert isinstance(master.ingest(TWO_DELEGATES_REPLY), NewTurn)
    first = master.execute()
    assert isinstance(first, Delegate) and first.task == "first bounded chunk"
    second = master.deliver_delegate_result("A")
    assert isinstance(second, Delegate) and second.task == "second bounded chunk"
    assert master.status().phase is Phase.AWAITING_SUBAGENT
    final = master.deliver_delegate_result("B")
    assert isinstance(final, Send)
    assert "A" in final.outbound.chunks[0] and "B" in final.outbound.chunks[0]


def test_a_cancelled_batch_never_parks_on_a_delegation(
    make_engine: EngineFactory,
) -> None:
    """A cancel must not be able to strand the turn on a sub-agent run that the
    user just asked to stop."""
    started = threading.Event()

    @tool_handler
    def slow(ctx: ToolContext, call: ToolCall) -> str:
        started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ctx.cancelled():
            time.sleep(0.02)
        return "gave up"

    base = default_registry(allow_delegate=True)
    specs = [s for s in (base.get(n) for n in base.names()) if s is not None]
    slow_spec = ToolSpec("slow_tool", "auto", slow, None, "slow_tool()\n  test double")
    engine = make_engine(tools=ToolRegistry([*specs, slow_spec]))
    engine.start_task("t")
    assert isinstance(engine.ingest(SLOW_THEN_DELEGATE_REPLY), NewTurn)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(engine.execute)
        assert started.wait(10), "the slow tool never started"
        engine.request_cancel()
        step = running.result(timeout=20)

    assert isinstance(step, Send)  # the turn finished; it never parked
    assert engine.status().phase is Phase.AWAITING_REPLY
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULT id=2 status=error code=cancelled===" in payload
    assert "skipped: the user cancelled this batch" in payload


def test_task_done_result_rides_on_the_done_step(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(SUBAGENT_DONE_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Done)
    assert step.summary == "surveyed the package"
    assert step.result.strip().startswith("capture.grab_region()")


def test_task_done_without_result_leaves_it_empty(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(DONE_WITHOUT_RESULT_REPLY), NewTurn)
    step = engine.execute()
    assert isinstance(step, Done)
    assert step.result == ""  # the host falls back to the summary


def test_a_subagent_cannot_delegate_further(make_engine: EngineFactory) -> None:
    sub = make_engine(tools=default_registry(role="subagent"), role="subagent")
    assert sub.role == "subagent"
    sub.start_task("your bounded task")
    assert isinstance(sub.ingest(NESTED_DELEGATE_REPLY), NewTurn)
    step = sub.execute()
    assert isinstance(step, Send)  # no parking: there is no such tool
    payload = step.outbound.chunks[0]
    assert "code=unknown_tool" in payload
    assert "unknown tool: 'delegate'" in payload
    assert "valid tools:" in payload


def test_role_defaults_to_master(engine: Engine) -> None:
    assert engine.role == "master"
