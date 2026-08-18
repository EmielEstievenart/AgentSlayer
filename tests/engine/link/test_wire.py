"""The wire codec: every value that crosses the link, both ways.

Two properties are worth more than any single case here. ROUND-TRIP: whatever
the engine hands the seam comes back out of the codec equal to itself, field for
field, so a remote session behaves like a local one. STRICTNESS: everything that
is not valid protocol v1 raises `WireError` instead of decoding into a
plausible-looking value - a link that guesses is a link that acts on a message
it got wrong.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import get_args

import pytest

from agentclip.engine.engine import (
    AskUser,
    AutoReply,
    CallProgress,
    ChunkAck,
    Delegate,
    Done,
    NewTurn,
    Noise,
    PendingAction,
    ProtocolError,
    Send,
    StatusSnapshot,
)
from agentclip.engine.link import wire
from agentclip.engine.link.factory import EngineRequest
from agentclip.engine.states import Decision, EngineStateError, Phase
from agentclip.engine.store.backups import UndoReport
from agentclip.executor.permissions import PermissionMode
from agentclip.protocol.composer import BudgetExceeded
from agentclip.protocol.types import EomInfo, Outbound, ParsedReply, ParseIssue, ToolCall
from agentclip.shell.app.link import Link

# -- sample values -------------------------------------------------------------


def _issue() -> ParseIssue:
    return ParseIssue(kind="renumbered", line=12, detail="ids 3,9 -> 1,2")


def _call() -> ToolCall:
    return ToolCall(
        id=2,
        tool="edit_file",
        params={"path": "src/a.py", "old": "def f():\n    pass", "new": "def f():\n    return 1"},
        raw="===CLIP:CALL id=9 tool=edit_file===\n...\n===CLIP:END===",
        original_id="9",
        issues=(_issue(), ParseIssue(kind="unknown_param", line=14, detail="depth")),
    )


def _outbound() -> Outbound:
    return Outbound(
        kind="results",
        chunks=("===CLIP:PART 1/3===\nfirst", "second\nline", "third"),
        total_chars=42,
        turn=7,
    )


def _reply() -> ParsedReply:
    return ParsedReply(
        kind="reply",
        calls=(_call(),),
        prose=("I will edit the file.", "Then run the tests."),
        warnings=(_issue(),),
        eom=EomInfo(present=True, calls=1, turn=7, chat="brisk-otter"),
        truncated=False,
        saw_fence=True,
        normalized_hash="9f2a" * 8,
        ack_part=None,
        ack_total=None,
        ack_chat=None,
        nack_reason=None,
    )


def _status() -> StatusSnapshot:
    return StatusSnapshot(
        phase=Phase.REVIEW,
        turn=7,
        service_key="claude",
        budget_chars=12_000,
        auto_accept_edits=True,
        yolo=False,
        mode="ask",
        session_dir=Path("/home/emiel/proj/.agentclip/sessions/2026-08-18T10-00-00"),
        last_outbound_chars=3_140,
        has_extra_instructions=True,
        instructions_armed=False,
    )


# -- the two tagged unions -----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        NewTurn(reply=_reply()),
        ChunkAck(part=2, total=5),
        ChunkAck(part=None, total=None),
        Noise(reason="wrong-chat"),
        ProtocolError(detail="EOM claims 3 calls, found 2"),
        AutoReply(outbound=_outbound(), detail="the reply arrived flattened"),
    ],
)
def test_ingest_result_round_trips(value: object) -> None:
    assert wire.decode_ingest_result(wire.encode_ingest_result(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        Send(outbound=_outbound()),
        AskUser(question="which branch?", call_id=3),
        Delegate(task="port the parser", context=None, call_id=4),
        Delegate(task="port the parser", context="see docs/design", call_id=4),
        Done(summary="all green", outbound=None, result=""),
        Done(summary="all green", outbound=_outbound(), result="the deliverable"),
    ],
)
def test_step_result_round_trips(value: object) -> None:
    assert wire.decode_step_result(wire.encode_step_result(value)) == value


def test_union_tags_are_the_member_names() -> None:
    assert wire.encode_ingest_result(Noise(reason="own-outbound"))["kind"] == "Noise"
    assert wire.encode_step_result(AskUser(question="?", call_id=1))["kind"] == "AskUser"


# -- values --------------------------------------------------------------------


def test_pending_action_round_trips_with_a_full_tool_call() -> None:
    action = PendingAction(
        call=_call(),
        kind="edit",
        preview="--- a/src/a.py\n+++ b/src/a.py\n@@\n-    pass\n+    return 1",
        auto_reason=None,
        always_pattern="edit_file src/**",
    )
    back = wire.decode_pending_action(wire.encode_pending_action(action))
    assert back == action
    assert back.call.params == action.call.params
    assert back.call.issues == action.call.issues


def test_outbound_round_trips_every_chunk() -> None:
    out = _outbound()
    back = wire.decode_outbound(wire.encode_outbound(out))
    assert back == out
    assert len(back.chunks) == 3


def test_parsed_reply_round_trips() -> None:
    reply = _reply()
    assert wire.decode_parsed_reply(wire.encode_parsed_reply(reply)) == reply


def test_status_round_trips_and_session_dir_survives_as_a_path() -> None:
    status = _status()
    back = wire.decode_status(wire.encode_status(status))
    assert back == status
    assert isinstance(back.session_dir, Path)
    assert wire.encode_status(status)["session_dir"] == status.session_dir.as_posix()


@pytest.mark.parametrize("phase", list(Phase))
def test_every_phase_survives(phase: Phase) -> None:
    status = StatusSnapshot(
        phase=phase,
        turn=1,
        service_key="s",
        budget_chars=1,
        auto_accept_edits=False,
        yolo=False,
        mode="plan",
        session_dir=Path("/tmp/x"),
        last_outbound_chars=0,
    )
    assert wire.decode_status(wire.encode_status(status)).phase is phase


@pytest.mark.parametrize("mode", get_args(PermissionMode))
def test_every_permission_mode_survives(mode: PermissionMode) -> None:
    status = StatusSnapshot(
        phase=Phase.IDLE,
        turn=0,
        service_key="s",
        budget_chars=1,
        auto_accept_edits=False,
        yolo=True,
        mode=mode,
        session_dir=Path("C:/Users/x/proj"),
        last_outbound_chars=0,
    )
    assert wire.decode_status(wire.encode_status(status)).mode == mode


@pytest.mark.parametrize("name", list(Decision.__members__))
def test_every_decision_survives_by_name(name: str) -> None:
    decision = Decision[name]
    assert wire.encode_decision(decision) == name
    assert wire.decode_decision(name) is decision


def test_undo_report_round_trips() -> None:
    report = UndoReport(
        turn=4,
        restored=("src/a.py", "src/b.py"),
        deleted=("scratch.txt",),
        recreated=(),
        warnings=("sha mismatch for src/b.py",),
    )
    assert wire.decode_undo_report(wire.encode_undo_report(report)) == report


def test_engine_request_round_trips() -> None:
    request = EngineRequest(
        service="claude",
        role="subagent",
        allow_delegate=False,
        chat_name="brisk-otter",
        parent_chat_name="calm-heron",
        task_chars=812,
    )
    assert wire.decode_engine_request(wire.encode_engine_request(request)) == request


def test_call_progress_round_trips_both_phases() -> None:
    running = CallProgress(call_id=3, tool="run_command", phase="running")
    done = CallProgress(call_id=3, tool="run_command", phase="done", status="ok")
    assert wire.decode_call_progress(wire.encode_call_progress(running)) == running
    assert wire.decode_call_progress(wire.encode_call_progress(done)) == done


# -- per-method plumbing -------------------------------------------------------


def test_session_methods_are_exactly_the_links_async_surface() -> None:
    """The table and the Protocol must name the same 13 calls.

    This is the drift guard the whole table exists for: a method added to `Link`
    without a wire entry is a call a remote session simply cannot make, and the
    only place that shows up otherwise is a remote run.
    """
    async_methods = {
        name
        for name, member in vars(Link).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }
    assert async_methods == set(wire.SESSION_METHODS)
    assert set(wire.METHODS) == set(wire._PARAMS) == set(wire._RESULTS)


_PARAM_CASES: dict[str, dict[str, object]] = {
    "build_session": {"service": "claude", "role": "subagent", "task_chars": 40},
    "start_task": {"task": "port the parser"},
    "follow_up": {"text": "and add tests"},
    "ingest": {"text": "===CLIP:EOM turn=1==="},
    "pending": {},
    "decide": {"call_id": 2, "decision": Decision.APPROVE_ALWAYS, "note": "fine"},
    "execute": {},
    "answer_user": {"text": "the main branch"},
    "deliver_delegate_result": {"text": "done", "status": "error", "code": "too_large"},
    "undo_last_turn": {"compose_notice": False},
    "status": {},
    "set_yolo": {"enabled": True},
    "set_permission_mode": {"mode": "unattended"},
    "arm_extra_instructions": {},
}


@pytest.mark.parametrize("method", wire.METHODS)
def test_params_round_trip_for_every_method(method: str) -> None:
    kwargs = _PARAM_CASES[method]
    # A frame carries what a frame carries: json is the real medium, not dicts.
    params = json.loads(json.dumps(wire.encode_params(method, **kwargs)))
    decoded = wire.decode_params(method, params)
    # Everything the caller passed comes back as itself, and nothing else is
    # invented: the rest of the keys are the method's own defaults.
    assert {key: decoded[key] for key in kwargs} == dict(kwargs)
    assert set(decoded) == set(params)


def test_params_carry_defaults_the_far_side_never_has_to_know() -> None:
    params = wire.encode_params("deliver_delegate_result", text="ok")
    assert params == {"text": "ok", "status": "ok", "code": None}
    assert wire.decode_params("deliver_delegate_result", params) == {
        "text": "ok",
        "status": "ok",
        "code": None,
    }


def test_build_session_params_are_an_engine_request() -> None:
    request = EngineRequest(service="claude", role="subagent", task_chars=9)
    params = wire.encode_engine_request(request)
    assert EngineRequest(**wire.decode_params("build_session", params)) == request
    assert wire.encode_params("build_session", service="claude", role="subagent", task_chars=9) == (
        params
    )


_RESULT_CASES: dict[str, object] = {
    "build_session": wire.SessionInfo(
        session="s-1", chat_name="brisk-otter", role="master", build_warnings=("mcp dropped",)
    ),
    "start_task": _outbound(),
    "follow_up": _outbound(),
    "ingest": NewTurn(reply=_reply()),
    "pending": (
        PendingAction(call=_call(), kind="command", preview="pytest -q", auto_reason=None),
    ),
    "decide": None,
    "execute": Send(outbound=_outbound()),
    "answer_user": Done(summary="done", outbound=None, result=""),
    "deliver_delegate_result": AskUser(question="which?", call_id=1),
    "undo_last_turn": (
        UndoReport(turn=2, restored=("a.py",), deleted=(), recreated=(), warnings=()),
        _outbound(),
    ),
    "status": _status(),
    "set_yolo": True,
    "set_permission_mode": "plan",
    "arm_extra_instructions": "no-instructions",
}


@pytest.mark.parametrize("method", wire.METHODS)
def test_results_round_trip_for_every_method(method: str) -> None:
    value = _RESULT_CASES[method]
    payload = json.loads(json.dumps(wire.encode_result(method, value)))
    assert wire.decode_result(method, payload) == value


def test_undo_result_carries_a_missing_notice() -> None:
    report = UndoReport(turn=1, restored=(), deleted=(), recreated=(), warnings=())
    payload = wire.encode_result("undo_last_turn", (report, None))
    assert wire.decode_result("undo_last_turn", payload) == (report, None)


# -- frames --------------------------------------------------------------------


def test_handshake_frames() -> None:
    assert wire.read_hello(wire.hello_frame()) == wire.WIRE_VERSION
    ack = wire.hello_ack_frame("6f1a-uuid4")
    assert wire.read_hello_ack(ack) == "6f1a-uuid4"
    assert ack["version"] == wire.WIRE_VERSION


def test_call_frame_carries_a_session_for_session_methods_only() -> None:
    frame = wire.call_frame(3, "ingest", wire.encode_params("ingest", text="x"), session="s-1")
    read = wire.read_call(frame)
    assert (read.id, read.method, read.session) == (3, "ingest", "s-1")
    assert wire.decode_params(read.method, read.params) == {"text": "x"}

    build = wire.call_frame(1, "build_session", wire.encode_params("build_session", service="c"))
    assert wire.read_call(build).session is None
    with pytest.raises(wire.WireError):
        wire.call_frame(2, "ingest", {}, session=None)
    with pytest.raises(wire.WireError):
        wire.call_frame(2, "build_session", {}, session="s-1")


def test_result_and_event_frames() -> None:
    assert wire.read_result(wire.result_frame(4, None)) == (4, None)
    progress = CallProgress(call_id=1, tool="read_file", phase="done", status="ok")
    read = wire.read_progress(wire.progress_frame("s-1", progress))
    assert (read.session, read.progress) == ("s-1", progress)
    out = wire.read_output(wire.output_frame("s-1", 2, "hello\n"))
    assert (out.session, out.call_id, out.delta) == ("s-1", 2, "hello\n")
    assert wire.read_cancel(wire.cancel_frame("s-1")) == "s-1"
    assert "id" not in wire.cancel_frame("s-1")


# -- errors --------------------------------------------------------------------


def test_budget_exceeded_crosses_as_itself_with_its_numbers() -> None:
    frame = wire.error_frame_for(5, BudgetExceeded(13_400, 12_000))
    assert frame["kind"] == "budget_exceeded"
    exc = wire.error_exception(wire.read_error(frame))
    assert isinstance(exc, BudgetExceeded)
    assert (exc.needed_chars, exc.budget_chars) == (13_400, 12_000)
    assert str(exc) == str(BudgetExceeded(13_400, 12_000))


def test_engine_state_error_crosses_as_itself() -> None:
    frame = wire.error_frame_for(6, EngineStateError("execute() is not legal in IDLE"))
    assert frame["kind"] == "engine_state_error"
    exc = wire.error_exception(wire.read_error(frame))
    assert isinstance(exc, EngineStateError)
    assert str(exc) == "execute() is not legal in IDLE"


def test_anything_else_is_internal() -> None:
    frame = wire.error_frame_for(7, ValueError("boom"))
    assert frame["kind"] == "internal"
    exc = wire.error_exception(wire.read_error(frame))
    assert isinstance(exc, wire.EngineLinkError)
    assert "boom" in str(exc)


def test_bad_request_is_its_own_kind() -> None:
    frame = wire.error_frame(8, "bad_request", "unknown session 's-9'")
    exc = wire.error_exception(wire.read_error(frame))
    assert isinstance(exc, wire.EngineLinkError)
    assert exc.kind == "bad_request"


def test_a_budget_error_without_data_still_raises_the_right_type() -> None:
    exc = wire.error_exception(wire.read_error(wire.error_frame(9, "budget_exceeded", "nope")))
    assert isinstance(exc, BudgetExceeded)


def test_unknown_error_kind_is_refused_both_ways() -> None:
    with pytest.raises(wire.WireError):
        wire.error_frame(1, "exploded", "x")  # type: ignore[arg-type]
    with pytest.raises(wire.WireError):
        wire.read_error({"type": "error", "id": 1, "kind": "exploded", "detail": "x"})


# -- lines ---------------------------------------------------------------------


def test_a_line_is_one_line() -> None:
    frame = wire.output_frame("s-1", 3, "first\r\nsecond\nthird\n")
    line = wire.encode_line(frame)
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert wire.decode_line(line) == frame


def test_unicode_rides_as_itself() -> None:
    text = "héllo — 漢字 🎉 ✓\nnext"
    line = wire.encode_line(wire.output_frame("s-1", 1, text))
    assert "\\u" not in line
    assert wire.read_output(wire.decode_line(line)).delta == text


def test_a_very_large_delta_survives() -> None:
    delta = ("x" * 100_000) + "\n漢" + ("y" * 100_000)
    line = wire.encode_line(wire.output_frame("s-1", 1, delta))
    assert line.count("\n") == 1
    assert wire.read_output(wire.decode_line(line)).delta == delta


def test_a_whole_turn_of_frames_survives_the_line_codec() -> None:
    frames = [
        wire.hello_frame(),
        wire.call_frame(1, "execute", {}, session="s-1"),
        wire.progress_frame("s-1", CallProgress(call_id=1, tool="run_command", phase="running")),
        wire.output_frame("s-1", 1, "compiling…\n"),
        wire.result_frame(1, wire.encode_result("execute", Send(outbound=_outbound()))),
    ]
    stream = "".join(wire.encode_line(f) for f in frames)
    assert [wire.decode_line(line) for line in stream.splitlines()] == frames


# -- strictness ----------------------------------------------------------------


@pytest.mark.parametrize("line", ["", "not json", "[1,2]", '"a string"', "null", '{"id":1}'])
def test_a_line_that_is_not_a_typed_object_is_refused(line: str) -> None:
    with pytest.raises(wire.WireError):
        wire.decode_line(line)


def test_unknown_frame_type_is_refused() -> None:
    with pytest.raises(wire.WireError):
        wire.frame_type({"type": "gossip"})
    with pytest.raises(wire.WireError):
        wire.read_result({"type": "progress", "session": "s"})


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "hello", "version": 2},
        {"type": "hello", "version": "1"},
        {"type": "hello"},
    ],
)
def test_a_wrong_version_never_handshakes(frame: dict[str, object]) -> None:
    with pytest.raises(wire.WireError):
        wire.read_hello(frame)


def test_a_wrong_version_ack_is_refused() -> None:
    with pytest.raises(wire.WireError):
        wire.read_hello_ack({"type": "hello_ack", "version": 99, "server_id": "x"})
    with pytest.raises(wire.WireError):
        wire.read_hello_ack({"type": "hello_ack", "version": 1, "server_id": ""})


def test_unknown_union_tags_are_refused() -> None:
    with pytest.raises(wire.WireError):
        wire.decode_ingest_result({"kind": "Whatever"})
    with pytest.raises(wire.WireError):
        wire.decode_step_result({"kind": "Whatever"})


def test_unknown_enum_names_are_refused() -> None:
    with pytest.raises(wire.WireError):
        wire.decode_decision("MAYBE")
    with pytest.raises(wire.WireError):
        wire.decode_phase("SHRUGGING")
    with pytest.raises(wire.WireError):
        wire.decode_permission_mode("yolo")
    with pytest.raises(wire.WireError):
        wire.decode_result_status("fine")
    with pytest.raises(wire.WireError):
        wire.decode_arm_result("maybe")


def test_a_status_with_an_unknown_phase_is_refused() -> None:
    payload = wire.encode_status(_status())
    payload["phase"] = "RUMINATING"
    with pytest.raises(wire.WireError):
        wire.decode_status(payload)


def test_missing_and_mistyped_fields_are_refused() -> None:
    payload = wire.encode_outbound(_outbound())
    del payload["turn"]
    with pytest.raises(wire.WireError):
        wire.decode_outbound(payload)
    bad = wire.encode_outbound(_outbound())
    bad["chunks"] = "one big string"
    with pytest.raises(wire.WireError):
        wire.decode_outbound(bad)
    call = wire.encode_tool_call(_call())
    call["params"] = {"path": 3}
    with pytest.raises(wire.WireError):
        wire.decode_tool_call(call)


def test_a_bool_is_never_an_int_on_this_wire() -> None:
    payload = wire.encode_status(_status())
    payload["turn"] = True
    with pytest.raises(wire.WireError):
        wire.decode_status(payload)


def test_unknown_methods_and_parameters_are_refused() -> None:
    with pytest.raises(wire.WireError):
        wire.encode_params("teleport", x=1)
    with pytest.raises(wire.WireError):
        wire.decode_params("teleport", {})
    with pytest.raises(wire.WireError):
        wire.encode_params("ingest", text="x", extra=1)
    with pytest.raises(wire.WireError):
        wire.decode_params("ingest", {"text": "x", "extra": 1})
    with pytest.raises(wire.WireError):
        wire.encode_params("ingest")  # no default for a required parameter
    with pytest.raises(wire.WireError):
        wire.decode_params("ingest", {})
    with pytest.raises(wire.WireError):
        wire.encode_result("teleport", None)
    with pytest.raises(wire.WireError):
        wire.decode_result("teleport", None)


def test_a_session_method_without_a_session_is_refused() -> None:
    with pytest.raises(wire.WireError):
        wire.read_call({"type": "call", "id": 1, "method": "execute", "params": {}})
    with pytest.raises(wire.WireError):
        wire.read_call(
            {"type": "call", "id": 1, "method": "teleport", "params": {}, "session": "s"}
        )


def test_decide_refuses_a_decision_that_is_not_one() -> None:
    with pytest.raises(wire.WireError):
        wire.decode_params("decide", {"call_id": 1, "decision": "APPROVE_MAYBE", "note": None})
