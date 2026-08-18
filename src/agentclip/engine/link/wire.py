"""The wire vocabulary of the Shell<->Engine link - protocol version 1.

This module is the ONE place the two halves of the link agree on what a message
looks like (docs/design/remote-executor.md section 2.9). Both ends import it:
the server loop that runs beside the engine on the target, and the ``RemoteLink``
that stands in for an engine inside the Shell. Neither of them owns a schema of
its own, so neither of them can drift from the other - a field added here is
added to both sides at once, and a field added on one side alone does not
compile into a message at all.

Framing
-------
JSON Lines: one JSON object per line, UTF-8, ``"\\n"``-terminated, compact
separators, no raw newline anywhere inside a line (strings carry their newlines
escaped). ``stderr`` is NEVER protocol data - it is the remote process's log, and
a server that prints to stdout outside :func:`encode_line` has corrupted the
stream. Every frame is flushed as it is written; nothing is batched.

Frame vocabulary (v1)
---------------------
``{"type":"hello","version":1}``
    The client's first line. Nothing else may precede it.
``{"type":"hello_ack","version":1,"server_id":"<uuid4>"}``
    The server's reply. ``server_id`` identifies the PROCESS, not a session: it
    exists so a later detach/reattach daemon mode can be added without a
    protocol redesign (design section 2.3), and v1 clients only check that it is
    a non-empty string.
``{"type":"call","id":<int>,"method":"<str>","params":{...}}``
    A request. ``id`` is client-chosen, strictly increasing, and echoed by
    exactly one ``result`` or ``error``. Session-scoped methods (the 13 `Link`
    methods) also carry ``"session":"<sid>"``; ``build_session`` does not,
    because it is what MINTS a session id.
``{"type":"result","id":<int>,"value":<encoded value>}``
    The successful answer. ``value`` is whatever :func:`encode_result` makes of
    that method's return, and is ``null`` for the methods that return None.
``{"type":"error","id":<int>,"kind":"<kind>","detail":"<str>"[,"data":{...}]}``
    The failed answer, one per unanswered call. ``kind`` is one of
    :data:`ERROR_KINDS`: ``budget_exceeded`` (``BudgetExceeded``),
    ``engine_state_error`` (``EngineStateError``), ``bad_request`` (protocol
    misuse - unknown method, unknown session, wrong version) and ``internal``
    (anything else). ``data`` carries the structured fields an exception type
    needs to be rebuilt faithfully on the other side; today only
    ``budget_exceeded`` uses it (``needed_chars``/``budget_chars``, which the
    Shell prints), and readers must tolerate its absence.
``{"type":"progress","session":"<sid>","progress":{...}}``
    One encoded :class:`~agentclip.engine.engine.CallProgress`, fired from the
    engine's worker thread mid-``execute``. No ``id``: it belongs to the session,
    not to one request.
``{"type":"output","session":"<sid>","call_id":<int>,"delta":"<str>"}``
    A chunk of a running command's output, for the RunPanel's live view.
``{"type":"cancel","session":"<sid>"}``
    Out-of-band, carries no ``id`` and is NEVER answered - it is the wire's
    ``Link.request_cancel``, and the call it interrupts is the one whose answer
    the client is still waiting for. A cancel for a session that is doing
    nothing is a no-op, exactly like the local one.

Interleaving guarantee
----------------------
All ``progress``/``output`` frames belonging to a call are written and FLUSHED
strictly before that call's ``result``/``error`` frame. So a client may treat the
answer as the end of the call's event stream: nothing that happened during a
turn arrives after the turn's answer, and no event needs a sequence number to be
ordered against it. (``cancel`` travels the other way and is the one frame that
may appear at any point.)

Value encoding
--------------
* dataclasses become JSON objects with EVERY field present - decode is strict
  and never fills a missing field in for a peer;
* the two unions - ``IngestResult`` and ``StepResult`` - are tagged by the
  member's class name under ``"kind"``;
* enums travel by ``.name`` (``Decision``, ``Phase``), ``Literal`` aliases by
  their value (``PermissionMode``, ``ResultStatus``, ``ArmResult``, and the
  inline ``kind``/``phase`` literals);
* ``StatusSnapshot.session_dir`` is the ONE ``Path`` on the seam and travels as
  a POSIX string.

Decoding is STRICT throughout: an unknown frame type, an unknown union tag, an
unknown enum name, a wrong version, a missing field, a field of the wrong JSON
type or an unknown method/parameter all raise :class:`WireError`. Guessing is
the one thing a protocol boundary must never do - a client that silently
tolerates a frame it does not understand is a client that will act on a message
it got wrong.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from agentclip.engine.approval import PermissionMode
from agentclip.engine.engine import (
    ArmResult,
    AskUser,
    AutoReply,
    CallProgress,
    ChunkAck,
    Delegate,
    Done,
    IngestResult,
    NewTurn,
    Noise,
    PendingAction,
    ProtocolError,
    Send,
    StatusSnapshot,
    StepResult,
)
from agentclip.engine.link.factory import EngineRequest, Role
from agentclip.engine.states import Decision, EngineStateError, Phase
from agentclip.engine.store.backups import UndoReport
from agentclip.protocol.composer import BudgetExceeded
from agentclip.protocol.types import (
    EomInfo,
    Outbound,
    ParsedReply,
    ParseIssue,
    ResultStatus,
    ToolCall,
    ToolResult,
)

WIRE_VERSION = 1


class WireError(Exception):
    """A line, frame or value that is not valid protocol v1.

    Raised by every decoder in this module and by nothing else. Both ends treat
    it as fatal for the frame it was raised on: the server answers the offending
    call with ``kind="bad_request"``, the client tears the link down, and neither
    ever tries to salvage a partially-understood message.
    """


class EngineLinkError(RuntimeError):
    """A remote failure with no local exception type of its own.

    ``BudgetExceeded`` and ``EngineStateError`` cross the seam AS THEMSELVES
    because the Shell catches exactly those by type (see
    ``agentclip.shell.app.link``). Everything else the engine can fail with -
    ``internal``, and the ``bad_request`` a misused link earns - has no
    counterpart worth reconstructing, so it arrives as this: the kind and the
    detail, unmistakably from the other side of the wire.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# -- frame types ---------------------------------------------------------------

FRAME_TYPES: frozenset[str] = frozenset(
    {"hello", "hello_ack", "call", "result", "error", "progress", "output", "cancel"}
)

ErrorKind = Literal["budget_exceeded", "engine_state_error", "bad_request", "internal"]

ERROR_KINDS: tuple[ErrorKind, ...] = (
    "budget_exceeded",
    "engine_state_error",
    "bad_request",
    "internal",
)

# The 13 state-changing `Link` methods, verbatim by name, plus the one call that
# has no session yet because it is what creates one. Named here so the server can
# reject an unknown method without a dispatch table of its own.
SESSION_METHODS: tuple[str, ...] = (
    "start_task",
    "follow_up",
    "ingest",
    "pending",
    "decide",
    "execute",
    "answer_user",
    "deliver_delegate_result",
    "undo_last_turn",
    "status",
    "set_yolo",
    "set_permission_mode",
    "arm_extra_instructions",
)

BUILD_SESSION = "build_session"

METHODS: tuple[str, ...] = (BUILD_SESSION, *SESSION_METHODS)


def is_session_method(method: str) -> bool:
    """Does a ``call`` frame for this method have to carry a ``session``?"""
    return method in SESSION_METHODS


# -- lines ---------------------------------------------------------------------


def encode_line(frame: dict[str, Any]) -> str:
    """One frame as the exact bytes-worth-of-text that goes on the wire.

    Compact separators and ``ensure_ascii=False``: the stream is UTF-8, so text
    rides as itself rather than as ``\\uXXXX`` escapes. The only raw newline in
    the returned string is the terminator - JSON escapes the ones inside strings,
    which is what makes "one frame per line" true of a 200k-char command output
    as much as of a handshake.
    """
    try:
        line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:  # a value no codec here produced
        raise WireError(f"frame is not JSON-encodable: {exc}") from exc
    return line + "\n"


def decode_line(line: str) -> dict[str, Any]:
    """One line back into a frame, or :class:`WireError`.

    Only the envelope is checked here - it must be a JSON object with a string
    ``type`` - because that is all a reader needs to route it. The per-type
    readers below do the rest.
    """
    try:
        value = json.loads(line)
    except ValueError as exc:
        raise WireError(f"not a JSON line: {exc}") from exc
    if not isinstance(value, dict):
        raise WireError(f"frame must be a JSON object, got {type(value).__name__}")
    kind = value.get("type")
    if not isinstance(kind, str):
        raise WireError("frame has no string 'type'")
    return value


# -- strict readers ------------------------------------------------------------


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WireError(f"{what}: expected an object, got {type(value).__name__}")
    return value


def _field(data: dict[str, Any], key: str, what: str) -> Any:
    if key not in data:
        raise WireError(f"{what}: missing {key!r}")
    return data[key]


def _as_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise WireError(f"{what}: expected a string, got {type(value).__name__}")
    return value


def _as_opt_str(value: Any, what: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise WireError(f"{what}: expected a string or null, got {type(value).__name__}")


def _as_int(value: Any, what: str) -> int:
    # bool is an int in Python and never one on this wire: a phase that decoded
    # `true` into 1 would be a bug nobody sees until a turn counter reads True.
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireError(f"{what}: expected an integer, got {type(value).__name__}")
    return value


def _as_opt_int(value: Any, what: str) -> int | None:
    if value is None:
        return None
    return _as_int(value, what)


def _as_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise WireError(f"{what}: expected a boolean, got {type(value).__name__}")
    return value


def _as_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise WireError(f"{what}: expected a list, got {type(value).__name__}")
    return value


def _as_strs(value: Any, what: str) -> tuple[str, ...]:
    return tuple(_as_str(item, f"{what}[{i}]") for i, item in enumerate(_as_list(value, what)))


def _as_str_map(value: Any, what: str) -> dict[str, str]:
    return {key: _as_str(item, f"{what}[{key!r}]") for key, item in _mapping(value, what).items()}


def _as_literal(value: Any, allowed: Sequence[str], what: str) -> Any:
    text = _as_str(value, what)
    if text not in allowed:
        raise WireError(f"{what}: {text!r} is not one of {tuple(allowed)!r}")
    return text


def _str_at(data: dict[str, Any], key: str, what: str) -> str:
    return _as_str(_field(data, key, what), f"{what}.{key}")


def _opt_str_at(data: dict[str, Any], key: str, what: str) -> str | None:
    return _as_opt_str(_field(data, key, what), f"{what}.{key}")


def _int_at(data: dict[str, Any], key: str, what: str) -> int:
    return _as_int(_field(data, key, what), f"{what}.{key}")


def _opt_int_at(data: dict[str, Any], key: str, what: str) -> int | None:
    return _as_opt_int(_field(data, key, what), f"{what}.{key}")


def _bool_at(data: dict[str, Any], key: str, what: str) -> bool:
    return _as_bool(_field(data, key, what), f"{what}.{key}")


def _strs_at(data: dict[str, Any], key: str, what: str) -> tuple[str, ...]:
    return _as_strs(_field(data, key, what), f"{what}.{key}")


def _literal_at(data: dict[str, Any], key: str, allowed: Sequence[str], what: str) -> Any:
    return _as_literal(_field(data, key, what), allowed, f"{what}.{key}")


# -- enums and literals --------------------------------------------------------

# The named Literal aliases are read off the types themselves, so a value added
# to one of them is on the wire the same day it is in the engine.
_RESULT_STATUSES: tuple[str, ...] = get_args(ResultStatus)
_PERMISSION_MODES: tuple[str, ...] = get_args(PermissionMode)
_ARM_RESULTS: tuple[str, ...] = get_args(ArmResult)
_ROLES: tuple[str, ...] = get_args(Role)

# The inline ones, spelled out because they are annotations on a field rather
# than a name anything can import (protocol/types.py, engine/engine.py).
_OUTBOUND_KINDS = ("bootstrap", "results", "user_answer", "note", "calibration")
_REPLY_KINDS = ("reply", "ack", "nack", "noise")
_PENDING_KINDS = ("edit", "command", "auto")
_PROGRESS_PHASES = ("running", "done")


def encode_decision(value: Decision) -> str:
    return value.name


def decode_decision(value: Any, what: str = "decision") -> Decision:
    name = _as_str(value, what)
    try:
        return Decision[name]
    except KeyError:
        raise WireError(f"{what}: {name!r} is not a Decision") from None


def encode_phase(value: Phase) -> str:
    return value.name


def decode_phase(value: Any, what: str = "phase") -> Phase:
    name = _as_str(value, what)
    try:
        return Phase[name]
    except KeyError:
        raise WireError(f"{what}: {name!r} is not a Phase") from None


def decode_permission_mode(value: Any, what: str = "mode") -> PermissionMode:
    mode: PermissionMode = _as_literal(value, _PERMISSION_MODES, what)
    return mode


def decode_result_status(value: Any, what: str = "status") -> ResultStatus:
    status: ResultStatus = _as_literal(value, _RESULT_STATUSES, what)
    return status


def decode_arm_result(value: Any, what: str = "arm_result") -> ArmResult:
    armed: ArmResult = _as_literal(value, _ARM_RESULTS, what)
    return armed


def decode_role(value: Any, what: str = "role") -> Role:
    role: Role = _as_literal(value, _ROLES, what)
    return role


# -- protocol values -----------------------------------------------------------


def encode_parse_issue(value: ParseIssue) -> dict[str, Any]:
    return {"kind": value.kind, "line": value.line, "detail": value.detail}


def decode_parse_issue(value: Any, what: str = "issue") -> ParseIssue:
    data = _mapping(value, what)
    return ParseIssue(
        kind=_str_at(data, "kind", what),
        line=_int_at(data, "line", what),
        detail=_str_at(data, "detail", what),
    )


def encode_tool_call(value: ToolCall) -> dict[str, Any]:
    return {
        "id": value.id,
        "tool": value.tool,
        "params": dict(value.params),
        "raw": value.raw,
        "original_id": value.original_id,
        "issues": [encode_parse_issue(issue) for issue in value.issues],
    }


def decode_tool_call(value: Any, what: str = "call") -> ToolCall:
    data = _mapping(value, what)
    issues = _as_list(_field(data, "issues", what), f"{what}.issues")
    return ToolCall(
        id=_int_at(data, "id", what),
        tool=_str_at(data, "tool", what),
        params=_as_str_map(_field(data, "params", what), f"{what}.params"),
        raw=_str_at(data, "raw", what),
        original_id=_opt_str_at(data, "original_id", what),
        issues=tuple(
            decode_parse_issue(issue, f"{what}.issues[{i}]") for i, issue in enumerate(issues)
        ),
    )


def encode_eom_info(value: EomInfo) -> dict[str, Any]:
    return {
        "present": value.present,
        "calls": value.calls,
        "turn": value.turn,
        "chat": value.chat,
    }


def decode_eom_info(value: Any, what: str = "eom") -> EomInfo:
    data = _mapping(value, what)
    return EomInfo(
        present=_bool_at(data, "present", what),
        calls=_opt_int_at(data, "calls", what),
        turn=_opt_int_at(data, "turn", what),
        chat=_opt_str_at(data, "chat", what),
    )


def encode_parsed_reply(value: ParsedReply) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "calls": [encode_tool_call(call) for call in value.calls],
        "prose": list(value.prose),
        "warnings": [encode_parse_issue(issue) for issue in value.warnings],
        "eom": encode_eom_info(value.eom),
        "truncated": value.truncated,
        "saw_fence": value.saw_fence,
        "normalized_hash": value.normalized_hash,
        "ack_part": value.ack_part,
        "ack_total": value.ack_total,
        "ack_chat": value.ack_chat,
        "nack_reason": value.nack_reason,
    }


def decode_parsed_reply(value: Any, what: str = "reply") -> ParsedReply:
    data = _mapping(value, what)
    calls = _as_list(_field(data, "calls", what), f"{what}.calls")
    warnings = _as_list(_field(data, "warnings", what), f"{what}.warnings")
    return ParsedReply(
        kind=_literal_at(data, "kind", _REPLY_KINDS, what),
        calls=tuple(decode_tool_call(c, f"{what}.calls[{i}]") for i, c in enumerate(calls)),
        prose=_strs_at(data, "prose", what),
        warnings=tuple(
            decode_parse_issue(w, f"{what}.warnings[{i}]") for i, w in enumerate(warnings)
        ),
        eom=decode_eom_info(_field(data, "eom", what), f"{what}.eom"),
        truncated=_bool_at(data, "truncated", what),
        saw_fence=_bool_at(data, "saw_fence", what),
        normalized_hash=_str_at(data, "normalized_hash", what),
        ack_part=_opt_int_at(data, "ack_part", what),
        ack_total=_opt_int_at(data, "ack_total", what),
        ack_chat=_opt_str_at(data, "ack_chat", what),
        nack_reason=_opt_str_at(data, "nack_reason", what),
    )


def encode_tool_result(value: ToolResult) -> dict[str, Any]:
    return {
        "call_id": value.call_id,
        "status": value.status,
        "body": value.body,
        "tool": value.tool,
        "code": value.code,
        "user_note": value.user_note,
    }


def decode_tool_result(value: Any, what: str = "result") -> ToolResult:
    data = _mapping(value, what)
    return ToolResult(
        call_id=_int_at(data, "call_id", what),
        status=decode_result_status(_field(data, "status", what), f"{what}.status"),
        body=_str_at(data, "body", what),
        tool=_str_at(data, "tool", what),
        code=_opt_str_at(data, "code", what),
        user_note=_opt_str_at(data, "user_note", what),
    )


def encode_outbound(value: Outbound) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "chunks": list(value.chunks),
        "total_chars": value.total_chars,
        "turn": value.turn,
    }


def decode_outbound(value: Any, what: str = "outbound") -> Outbound:
    data = _mapping(value, what)
    return Outbound(
        kind=_literal_at(data, "kind", _OUTBOUND_KINDS, what),
        chunks=_strs_at(data, "chunks", what),
        total_chars=_int_at(data, "total_chars", what),
        turn=_int_at(data, "turn", what),
    )


def encode_opt_outbound(value: Outbound | None) -> dict[str, Any] | None:
    return None if value is None else encode_outbound(value)


def decode_opt_outbound(value: Any, what: str = "outbound") -> Outbound | None:
    return None if value is None else decode_outbound(value, what)


# -- engine values -------------------------------------------------------------


def encode_pending_action(value: PendingAction) -> dict[str, Any]:
    return {
        "call": encode_tool_call(value.call),
        "kind": value.kind,
        "preview": value.preview,
        "auto_reason": value.auto_reason,
        "always_pattern": value.always_pattern,
    }


def decode_pending_action(value: Any, what: str = "pending") -> PendingAction:
    data = _mapping(value, what)
    return PendingAction(
        call=decode_tool_call(_field(data, "call", what), f"{what}.call"),
        kind=_literal_at(data, "kind", _PENDING_KINDS, what),
        preview=_str_at(data, "preview", what),
        auto_reason=_opt_str_at(data, "auto_reason", what),
        always_pattern=_opt_str_at(data, "always_pattern", what),
    )


def encode_call_progress(value: CallProgress) -> dict[str, Any]:
    return {
        "call_id": value.call_id,
        "tool": value.tool,
        "phase": value.phase,
        "status": value.status,
    }


def decode_call_progress(value: Any, what: str = "progress") -> CallProgress:
    data = _mapping(value, what)
    return CallProgress(
        call_id=_int_at(data, "call_id", what),
        tool=_str_at(data, "tool", what),
        phase=_literal_at(data, "phase", _PROGRESS_PHASES, what),
        # Deliberately a plain str, not ResultStatus: the field is "" while the
        # call is still running, which is not one of the four statuses.
        status=_str_at(data, "status", what),
    )


def encode_status(value: StatusSnapshot) -> dict[str, Any]:
    return {
        "phase": encode_phase(value.phase),
        "turn": value.turn,
        "service_key": value.service_key,
        "budget_chars": value.budget_chars,
        "auto_accept_edits": value.auto_accept_edits,
        "yolo": value.yolo,
        "mode": value.mode,
        # The ONE Path on the seam, and it travels as text on purpose: in a
        # remote session it names a directory on ANOTHER MACHINE, so the Shell
        # must treat it as display data - something to show in the sidebar or
        # copy into a transcript note, never something to open, stat or join.
        "session_dir": value.session_dir.as_posix(),
        "last_outbound_chars": value.last_outbound_chars,
        "has_extra_instructions": value.has_extra_instructions,
        "instructions_armed": value.instructions_armed,
    }


def decode_status(value: Any, what: str = "status") -> StatusSnapshot:
    data = _mapping(value, what)
    return StatusSnapshot(
        phase=decode_phase(_field(data, "phase", what), f"{what}.phase"),
        turn=_int_at(data, "turn", what),
        service_key=_str_at(data, "service_key", what),
        budget_chars=_int_at(data, "budget_chars", what),
        auto_accept_edits=_bool_at(data, "auto_accept_edits", what),
        yolo=_bool_at(data, "yolo", what),
        mode=decode_permission_mode(_field(data, "mode", what), f"{what}.mode"),
        session_dir=Path(_str_at(data, "session_dir", what)),
        last_outbound_chars=_int_at(data, "last_outbound_chars", what),
        has_extra_instructions=_bool_at(data, "has_extra_instructions", what),
        instructions_armed=_bool_at(data, "instructions_armed", what),
    )


def encode_undo_report(value: UndoReport) -> dict[str, Any]:
    return {
        "turn": value.turn,
        "restored": list(value.restored),
        "deleted": list(value.deleted),
        "recreated": list(value.recreated),
        "warnings": list(value.warnings),
    }


def decode_undo_report(value: Any, what: str = "undo") -> UndoReport:
    data = _mapping(value, what)
    return UndoReport(
        turn=_int_at(data, "turn", what),
        restored=_strs_at(data, "restored", what),
        deleted=_strs_at(data, "deleted", what),
        recreated=_strs_at(data, "recreated", what),
        warnings=_strs_at(data, "warnings", what),
    )


def encode_engine_request(value: EngineRequest) -> dict[str, Any]:
    return {
        "service": value.service,
        "role": value.role,
        "allow_delegate": value.allow_delegate,
        "chat_name": value.chat_name,
        "parent_chat_name": value.parent_chat_name,
        "task_chars": value.task_chars,
    }


def decode_engine_request(value: Any, what: str = "request") -> EngineRequest:
    data = _mapping(value, what)
    return EngineRequest(
        service=_str_at(data, "service", what),
        role=decode_role(_field(data, "role", what), f"{what}.role"),
        allow_delegate=_bool_at(data, "allow_delegate", what),
        chat_name=_opt_str_at(data, "chat_name", what),
        parent_chat_name=_opt_str_at(data, "parent_chat_name", what),
        task_chars=_int_at(data, "task_chars", what),
    )


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What ``build_session`` answers with: an id plus the immutable facts.

    The three facts are exactly the sync attributes of the `Link` Protocol
    (``chat_name``, ``role``, ``build_warnings``) - they are snapshotted at
    construction and can never change for the life of a session, which is why a
    remote link may carry them home in this one answer and then read them with
    no await, the way the local one reads them off the engine.
    """

    session: str
    chat_name: str
    role: Role
    build_warnings: tuple[str, ...] = ()


def encode_session_info(value: SessionInfo) -> dict[str, Any]:
    return {
        "session": value.session,
        "chat_name": value.chat_name,
        "role": value.role,
        "build_warnings": list(value.build_warnings),
    }


def decode_session_info(value: Any, what: str = "session_info") -> SessionInfo:
    data = _mapping(value, what)
    return SessionInfo(
        session=_str_at(data, "session", what),
        chat_name=_str_at(data, "chat_name", what),
        role=decode_role(_field(data, "role", what), f"{what}.role"),
        build_warnings=_strs_at(data, "build_warnings", what),
    )


# -- the two tagged unions -----------------------------------------------------


def encode_ingest_result(value: IngestResult) -> dict[str, Any]:
    if isinstance(value, NewTurn):
        return {"kind": "NewTurn", "reply": encode_parsed_reply(value.reply)}
    if isinstance(value, ChunkAck):
        return {"kind": "ChunkAck", "part": value.part, "total": value.total}
    if isinstance(value, Noise):
        return {"kind": "Noise", "reason": value.reason}
    if isinstance(value, ProtocolError):
        return {"kind": "ProtocolError", "detail": value.detail}
    if isinstance(value, AutoReply):
        return {
            "kind": "AutoReply",
            "outbound": encode_outbound(value.outbound),
            "detail": value.detail,
        }
    raise WireError(f"not an IngestResult: {type(value).__name__}")


def _decode_new_turn(data: dict[str, Any], what: str) -> IngestResult:
    return NewTurn(reply=decode_parsed_reply(_field(data, "reply", what), f"{what}.reply"))


def _decode_chunk_ack(data: dict[str, Any], what: str) -> IngestResult:
    return ChunkAck(part=_opt_int_at(data, "part", what), total=_opt_int_at(data, "total", what))


def _decode_noise(data: dict[str, Any], what: str) -> IngestResult:
    return Noise(reason=_str_at(data, "reason", what))


def _decode_protocol_error(data: dict[str, Any], what: str) -> IngestResult:
    return ProtocolError(detail=_str_at(data, "detail", what))


def _decode_auto_reply(data: dict[str, Any], what: str) -> IngestResult:
    return AutoReply(
        outbound=decode_outbound(_field(data, "outbound", what), f"{what}.outbound"),
        detail=_str_at(data, "detail", what),
    )


_INGEST_DECODERS: dict[str, Callable[[dict[str, Any], str], IngestResult]] = {
    "NewTurn": _decode_new_turn,
    "ChunkAck": _decode_chunk_ack,
    "Noise": _decode_noise,
    "ProtocolError": _decode_protocol_error,
    "AutoReply": _decode_auto_reply,
}


def decode_ingest_result(value: Any, what: str = "ingest_result") -> IngestResult:
    data = _mapping(value, what)
    tag = _str_at(data, "kind", what)
    decoder = _INGEST_DECODERS.get(tag)
    if decoder is None:
        raise WireError(f"{what}: {tag!r} is not an IngestResult kind")
    return decoder(data, what)


def encode_step_result(value: StepResult) -> dict[str, Any]:
    if isinstance(value, Send):
        return {"kind": "Send", "outbound": encode_outbound(value.outbound)}
    if isinstance(value, AskUser):
        return {"kind": "AskUser", "question": value.question, "call_id": value.call_id}
    if isinstance(value, Delegate):
        return {
            "kind": "Delegate",
            "task": value.task,
            "context": value.context,
            "call_id": value.call_id,
        }
    if isinstance(value, Done):
        return {
            "kind": "Done",
            "summary": value.summary,
            "outbound": encode_opt_outbound(value.outbound),
            "result": value.result,
        }
    raise WireError(f"not a StepResult: {type(value).__name__}")


def _decode_send(data: dict[str, Any], what: str) -> StepResult:
    return Send(outbound=decode_outbound(_field(data, "outbound", what), f"{what}.outbound"))


def _decode_ask_user(data: dict[str, Any], what: str) -> StepResult:
    return AskUser(question=_str_at(data, "question", what), call_id=_int_at(data, "call_id", what))


def _decode_delegate(data: dict[str, Any], what: str) -> StepResult:
    return Delegate(
        task=_str_at(data, "task", what),
        context=_opt_str_at(data, "context", what),
        call_id=_int_at(data, "call_id", what),
    )


def _decode_done(data: dict[str, Any], what: str) -> StepResult:
    return Done(
        summary=_str_at(data, "summary", what),
        outbound=decode_opt_outbound(_field(data, "outbound", what), f"{what}.outbound"),
        result=_str_at(data, "result", what),
    )


_STEP_DECODERS: dict[str, Callable[[dict[str, Any], str], StepResult]] = {
    "Send": _decode_send,
    "AskUser": _decode_ask_user,
    "Delegate": _decode_delegate,
    "Done": _decode_done,
}


def decode_step_result(value: Any, what: str = "step_result") -> StepResult:
    data = _mapping(value, what)
    tag = _str_at(data, "kind", what)
    decoder = _STEP_DECODERS.get(tag)
    if decoder is None:
        raise WireError(f"{what}: {tag!r} is not a StepResult kind")
    return decoder(data, what)


# -- per-method plumbing -------------------------------------------------------
#
# One table, both directions. The server and the RemoteLink each hold ONE line of
# code per method (dispatch and await), because everything that could drift
# between them - a parameter name, a default, the shape of a return - is stated
# here once and read by both.

_NO_DEFAULT = object()


@dataclass(frozen=True, slots=True)
class _Param:
    name: str
    encode: Callable[[Any], Any]
    decode: Callable[[Any, str], Any]
    # Present only for parameters the Python signature gives a default: a caller
    # may omit them, and they are still written to the wire in full, so the far
    # side never has to know what the near side's default was.
    default: Any = _NO_DEFAULT


@dataclass(frozen=True, slots=True)
class _Value:
    encode: Callable[[Any], Any]
    decode: Callable[[Any, str], Any]


def _identity(value: Any) -> Any:
    return value


def _encode_none(value: Any) -> Any:
    if value is not None:
        raise WireError(f"expected no value, got {type(value).__name__}")
    return None


def _decode_none(value: Any, what: str = "value") -> None:
    if value is not None:
        raise WireError(f"{what}: expected null, got {type(value).__name__}")
    return None


def _encode_pending_tuple(value: Any) -> Any:
    return [encode_pending_action(action) for action in value]


def _decode_pending_tuple(value: Any, what: str = "pending") -> tuple[PendingAction, ...]:
    return tuple(
        decode_pending_action(item, f"{what}[{i}]")
        for i, item in enumerate(_as_list(value, what))
    )


def _encode_undo_pair(value: Any) -> Any:
    report, notice = value
    return {"report": encode_undo_report(report), "notice": encode_opt_outbound(notice)}


def _decode_undo_pair(value: Any, what: str = "undo") -> tuple[UndoReport, Outbound | None]:
    data = _mapping(value, what)
    return (
        decode_undo_report(_field(data, "report", what), f"{what}.report"),
        decode_opt_outbound(_field(data, "notice", what), f"{what}.notice"),
    )


_STR = (_identity, _as_str)
_OPT_STR = (_identity, _as_opt_str)
_INT = (_identity, _as_int)
_BOOL = (_identity, _as_bool)

_PARAMS: dict[str, tuple[_Param, ...]] = {
    # No session yet: these ARE the EngineRequest's fields, in its own order, so
    # `EngineRequest(**decode_params("build_session", params))` is the whole of
    # the server's decoding and `encode_engine_request` produces the same object.
    BUILD_SESSION: (
        _Param("service", *_STR),
        _Param("role", _identity, decode_role, "master"),
        _Param("allow_delegate", *_BOOL, False),
        _Param("chat_name", *_OPT_STR, None),
        _Param("parent_chat_name", *_OPT_STR, None),
        _Param("task_chars", *_INT, 0),
    ),
    "start_task": (_Param("task", *_STR),),
    "follow_up": (_Param("text", *_STR),),
    "ingest": (_Param("text", *_STR),),
    "pending": (),
    "decide": (
        _Param("call_id", *_INT),
        _Param("decision", encode_decision, decode_decision),
        _Param("note", *_OPT_STR, None),
    ),
    "execute": (),
    "answer_user": (_Param("text", *_STR),),
    "deliver_delegate_result": (
        _Param("text", *_STR),
        _Param("status", _identity, decode_result_status, "ok"),
        _Param("code", *_OPT_STR, None),
    ),
    "undo_last_turn": (_Param("compose_notice", *_BOOL, True),),
    "status": (),
    "set_yolo": (_Param("enabled", *_BOOL),),
    "set_permission_mode": (_Param("mode", _identity, decode_permission_mode),),
    "arm_extra_instructions": (),
}

_RESULTS: dict[str, _Value] = {
    BUILD_SESSION: _Value(encode_session_info, decode_session_info),
    "start_task": _Value(encode_outbound, decode_outbound),
    "follow_up": _Value(encode_outbound, decode_outbound),
    "ingest": _Value(encode_ingest_result, decode_ingest_result),
    "pending": _Value(_encode_pending_tuple, _decode_pending_tuple),
    "decide": _Value(_encode_none, _decode_none),
    "execute": _Value(encode_step_result, decode_step_result),
    "answer_user": _Value(encode_step_result, decode_step_result),
    "deliver_delegate_result": _Value(encode_step_result, decode_step_result),
    "undo_last_turn": _Value(_encode_undo_pair, _decode_undo_pair),
    "status": _Value(encode_status, decode_status),
    "set_yolo": _Value(_identity, _as_bool),
    "set_permission_mode": _Value(_identity, decode_permission_mode),
    "arm_extra_instructions": _Value(_identity, decode_arm_result),
}


def _params_for(method: str) -> tuple[_Param, ...]:
    try:
        return _PARAMS[method]
    except KeyError:
        raise WireError(f"unknown method {method!r}") from None


def encode_params(method: str, **kwargs: Any) -> dict[str, Any]:
    """The ``params`` object for one call, by keyword, exactly as the Python
    method takes them. Every parameter is written, defaults included."""
    fields = _params_for(method)
    unknown = sorted(set(kwargs) - {field.name for field in fields})
    if unknown:
        raise WireError(f"{method}: unknown parameter(s) {unknown}")
    params: dict[str, Any] = {}
    for field in fields:
        if field.name in kwargs:
            value = kwargs[field.name]
        elif field.default is not _NO_DEFAULT:
            value = field.default
        else:
            raise WireError(f"{method}: missing parameter {field.name!r}")
        params[field.name] = field.encode(value)
    return params


def decode_params(method: str, params: Any) -> dict[str, Any]:
    """One call's ``params`` back into the keyword arguments the method takes."""
    fields = _params_for(method)
    what = f"{method}.params"
    data = _mapping(params, what)
    unknown = sorted(set(data) - {field.name for field in fields})
    if unknown:
        raise WireError(f"{what}: unknown parameter(s) {unknown}")
    kwargs: dict[str, Any] = {}
    for field in fields:
        if field.name in data:
            kwargs[field.name] = field.decode(data[field.name], f"{what}.{field.name}")
        elif field.default is not _NO_DEFAULT:
            kwargs[field.name] = field.default
        else:
            raise WireError(f"{what}: missing parameter {field.name!r}")
    return kwargs


def encode_result(method: str, value: Any) -> Any:
    """One method's return value as the ``value`` of a ``result`` frame."""
    try:
        codec = _RESULTS[method]
    except KeyError:
        raise WireError(f"unknown method {method!r}") from None
    return codec.encode(value)


def decode_result(method: str, payload: Any) -> Any:
    """A ``result`` frame's ``value`` back into what the method returns."""
    try:
        codec = _RESULTS[method]
    except KeyError:
        raise WireError(f"unknown method {method!r}") from None
    return codec.decode(payload, f"{method}.result")


# -- frames --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallFrame:
    id: int
    method: str
    params: dict[str, Any]
    session: str | None = None


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    id: int
    kind: ErrorKind
    detail: str
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ProgressFrame:
    session: str
    progress: CallProgress


@dataclass(frozen=True, slots=True)
class OutputFrame:
    session: str
    call_id: int
    delta: str


def frame_type(frame: dict[str, Any]) -> str:
    """The frame's ``type``, checked against the v1 vocabulary."""
    kind = _str_at(frame, "type", "frame")
    if kind not in FRAME_TYPES:
        raise WireError(f"unknown frame type {kind!r}")
    return kind


def _typed(frame: dict[str, Any], expected: str) -> dict[str, Any]:
    kind = frame_type(frame)
    if kind != expected:
        raise WireError(f"expected a {expected!r} frame, got {kind!r}")
    return frame


def _version(frame: dict[str, Any], what: str) -> int:
    version = _int_at(frame, "version", what)
    if version != WIRE_VERSION:
        raise WireError(f"{what}: wire version {version} is not {WIRE_VERSION}")
    return version


def hello_frame() -> dict[str, Any]:
    return {"type": "hello", "version": WIRE_VERSION}


def read_hello(frame: dict[str, Any]) -> int:
    return _version(_typed(frame, "hello"), "hello")


def hello_ack_frame(server_id: str) -> dict[str, Any]:
    return {"type": "hello_ack", "version": WIRE_VERSION, "server_id": server_id}


def read_hello_ack(frame: dict[str, Any]) -> str:
    data = _typed(frame, "hello_ack")
    _version(data, "hello_ack")
    server_id = _str_at(data, "server_id", "hello_ack")
    if not server_id:
        raise WireError("hello_ack: empty server_id")
    return server_id


def call_frame(
    req_id: int, method: str, params: dict[str, Any], *, session: str | None = None
) -> dict[str, Any]:
    if method not in _PARAMS:
        raise WireError(f"unknown method {method!r}")
    if is_session_method(method) and not session:
        raise WireError(f"{method} needs a session")
    if not is_session_method(method) and session is not None:
        raise WireError(f"{method} takes no session")
    frame: dict[str, Any] = {"type": "call", "id": req_id, "method": method, "params": params}
    if session is not None:
        frame["session"] = session
    return frame


def read_call(frame: dict[str, Any]) -> CallFrame:
    data = _typed(frame, "call")
    method = _str_at(data, "method", "call")
    if method not in _PARAMS:
        raise WireError(f"unknown method {method!r}")
    session = _opt_str_at(data, "session", "call") if "session" in data else None
    if is_session_method(method) and not session:
        raise WireError(f"call {method}: missing session")
    return CallFrame(
        id=_int_at(data, "id", "call"),
        method=method,
        params=_mapping(_field(data, "params", "call"), "call.params"),
        session=session,
    )


def result_frame(req_id: int, value: Any) -> dict[str, Any]:
    return {"type": "result", "id": req_id, "value": value}


def read_result(frame: dict[str, Any]) -> tuple[int, Any]:
    data = _typed(frame, "result")
    return _int_at(data, "id", "result"), _field(data, "value", "result")


def error_frame(
    req_id: int, kind: ErrorKind, detail: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    if kind not in ERROR_KINDS:
        raise WireError(f"unknown error kind {kind!r}")
    frame: dict[str, Any] = {"type": "error", "id": req_id, "kind": kind, "detail": detail}
    if data:
        frame["data"] = data
    return frame


def error_frame_for(req_id: int, exc: BaseException) -> dict[str, Any]:
    """The ``error`` frame for an exception the engine raised.

    The two the Shell catches BY TYPE are the two with a kind of their own;
    everything else is ``internal``, because a Shell that cannot act on the
    difference is better told plainly that the far side broke.
    """
    if isinstance(exc, BudgetExceeded):
        return error_frame(
            req_id,
            "budget_exceeded",
            str(exc),
            {"needed_chars": exc.needed_chars, "budget_chars": exc.budget_chars},
        )
    if isinstance(exc, EngineStateError):
        return error_frame(req_id, "engine_state_error", str(exc))
    return error_frame(req_id, "internal", f"{type(exc).__name__}: {exc}")


def read_error(frame: dict[str, Any]) -> ErrorFrame:
    data = _typed(frame, "error")
    extra = data.get("data")
    return ErrorFrame(
        id=_int_at(data, "id", "error"),
        kind=_literal_at(data, "kind", ERROR_KINDS, "error"),
        detail=_str_at(data, "detail", "error"),
        data=None if extra is None else _mapping(extra, "error.data"),
    )


def error_exception(error: ErrorFrame) -> Exception:
    """The exception a client raises for an ``error`` frame.

    ``BudgetExceeded`` is rebuilt from its two numbers rather than from its
    message, because the Shell formats them itself (``exc.needed_chars``) - a
    message-only reconstruction would print a plausible sentence with the wrong
    figures in it. A peer that sent no ``data`` gets zeros and its own detail.
    """
    if error.kind == "budget_exceeded":
        extra = error.data or {}
        needed = extra.get("needed_chars")
        budget = extra.get("budget_chars")
        if isinstance(needed, int) and isinstance(budget, int):
            return BudgetExceeded(needed, budget)
        return BudgetExceeded(0, 0)
    if error.kind == "engine_state_error":
        return EngineStateError(error.detail)
    return EngineLinkError(error.kind, error.detail)


def progress_frame(session: str, progress: CallProgress) -> dict[str, Any]:
    return {"type": "progress", "session": session, "progress": encode_call_progress(progress)}


def read_progress(frame: dict[str, Any]) -> ProgressFrame:
    data = _typed(frame, "progress")
    return ProgressFrame(
        session=_str_at(data, "session", "progress"),
        progress=decode_call_progress(_field(data, "progress", "progress"), "progress.progress"),
    )


def output_frame(session: str, call_id: int, delta: str) -> dict[str, Any]:
    return {"type": "output", "session": session, "call_id": call_id, "delta": delta}


def read_output(frame: dict[str, Any]) -> OutputFrame:
    data = _typed(frame, "output")
    return OutputFrame(
        session=_str_at(data, "session", "output"),
        call_id=_int_at(data, "call_id", "output"),
        delta=_str_at(data, "delta", "output"),
    )


def cancel_frame(session: str) -> dict[str, Any]:
    """The out-of-band cancel. No ``id``, and never answered: the call it
    interrupts is the one whose answer is already outstanding."""
    return {"type": "cancel", "session": session}


def read_cancel(frame: dict[str, Any]) -> str:
    return _str_at(_typed(frame, "cancel"), "session", "cancel")
