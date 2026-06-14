"""The Engine: AgentClip's synchronous, sans-IO session state machine.

The engine consumes strings (ingested clipboard text, user decisions, user
answers) and returns values (outbound payloads, pending actions, results). It
performs filesystem/subprocess side effects only through the tool layer,
never touches the clipboard, and never imports Textual (enforced by
tests/test_layering.py). The TUI calls it from exactly one worker thread.

Semantics implemented here (protocol.md sections 4-6 + plan synthesis):
- ingest: dedup over the last 20 normalized hashes, stale-turn guard, tool
  name validation (unknown tool -> pre-resolved unknown_tool result), fatal
  per-call parse issues -> pre-resolved error results;
- execute: strict id order, denied results with user_note, the same-path skip
  rule after a failed/denied mutation, rejection-aborts-turn, the per-turn
  backup bracket (begin_turn at first mutation, finish_turn at turn end),
  ask_user pause/resume, task_done collection, id=0 reply_truncated results.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agentclip.config import Config
from agentclip.engine.approval import ApprovalPolicy
from agentclip.engine.results import fit_results
from agentclip.engine.states import Decision, EngineStateError, Phase, can_transition
from agentclip.protocol.composer import BudgetExceeded, Composer
from agentclip.protocol.parser import normalized_hash, parse_reply
from agentclip.protocol.types import Outbound, ParsedReply, ToolCall, ToolResult
from agentclip.store.backups import BackupStore, UndoReport
from agentclip.store.session import SessionStore
from agentclip.tools.registry import ToolContext, ToolRegistry, ToolSpec, error_result
from agentclip.tools.sandbox import Workspace

_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})

# Per-call ParseIssue kinds that make a call non-executable (the parser keeps
# benign tolerances at reply level; anything attached to the call is fatal).
_FATAL_ISSUES = frozenset({"bad_header", "unterminated_heredoc", "missing_end"})

# Reply-level warning kinds surfaced to the LLM as NOTE lines in the results.
_NOTE_WARNINGS = frozenset({"renumbered", "duplicate_id", "missing_end", "unknown_param"})

# Chunks of one outbound are joined with this separator in outbound/turn-NNNN.txt.
_CHUNK_SEPARATOR = "\n␞\n"


# -- value types returned to the TUI ------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingAction:
    call: ToolCall
    kind: Literal["edit", "command", "auto"]
    preview: str  # unified diff / command line / "" for auto
    auto_reason: str | None  # e.g. 'matched "pytest*"' or "read-only tool"


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    phase: Phase
    turn: int
    service_key: str
    budget_chars: int
    auto_accept_edits: bool
    yolo: bool  # auto-approve everything (edits + commands) - bypasses every gate
    session_dir: Path
    last_outbound_chars: int


@dataclass(frozen=True, slots=True)
class NewTurn:
    reply: ParsedReply


@dataclass(frozen=True, slots=True)
class ChunkAck:
    part: int | None
    total: int | None


@dataclass(frozen=True, slots=True)
class Noise:
    reason: str  # "wrong-phase" | "not-protocol" | "duplicate" | "stale-turn"


@dataclass(frozen=True, slots=True)
class ProtocolError:
    detail: str


IngestResult = NewTurn | ChunkAck | Noise | ProtocolError


@dataclass(frozen=True, slots=True)
class Send:
    outbound: Outbound


@dataclass(frozen=True, slots=True)
class AskUser:
    question: str
    call_id: int


@dataclass(frozen=True, slots=True)
class Done:
    summary: str
    outbound: Outbound | None  # results of sibling calls this turn, if any


StepResult = Send | AskUser | Done


# -- internal per-turn bookkeeping ---------------------------------------------


@dataclass(slots=True)
class _Planned:
    call: ToolCall
    spec: ToolSpec | None
    action: PendingAction
    pre_result: ToolResult | None = None  # parse/unknown-tool error, emitted as-is
    needs_decision: bool = False
    decision: Decision | None = None
    note: str | None = None  # user's rejection reason
    aborted: bool = False  # a later-than-rejection call: skipped


@dataclass(slots=True)
class _ExecState:
    index: int = 0  # plan index of the in-flight ask_user (while AWAITING_USER)
    backup_started: bool = False
    done_summary: str | None = None
    results: list[ToolResult] = field(default_factory=list)
    failed_paths: set[str] = field(default_factory=set)


def _norm_path(path: str) -> str:
    """Comparison key for the same-path skip rule (forward slashes, casefolded)."""
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts).casefold()


class Engine:
    """Synchronous, single-threaded; sans-IO with respect to clipboard and UI."""

    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        workspace: Workspace,
        session: SessionStore,
        backups: BackupStore,
        composer: Composer,
    ) -> None:
        self._config = config
        self._registry = registry
        self._workspace = workspace
        self._session = session
        self._backups = backups
        self._composer = composer
        self._policy = ApprovalPolicy(config.approval)
        self._ctx = ToolContext(
            workspace=workspace,
            limits=config.limits,
            caps=config.caps(),
            backup_hook=self._backup_hook,
        )
        self._phase = Phase.IDLE
        self._turn = 0  # number of the last outbound payload (the model echoes it)
        self._seen_hashes: deque[str] = deque(maxlen=20)
        self._reply: ParsedReply | None = None
        self._plan: list[_Planned] = []
        self._exec: _ExecState | None = None
        self._last_outbound_chars = 0

    # -- task lifecycle ------------------------------------------------------

    def start_task(self, task: str) -> Outbound:
        """IDLE -> AWAITING_REPLY: compose and persist the bootstrap payload."""
        self._require_phase(Phase.IDLE, "start_task")
        outbound = self._composer.bootstrap(task)
        self._turn = outbound.turn
        self._session.append_event("task", text=task, turn=self._turn)
        self._register_outbound(outbound)
        self._set_phase(Phase.AWAITING_REPLY)
        return outbound

    def follow_up(self, text: str) -> Outbound:
        """An extra TASK payload from the user. Legal while AWAITING_REPLY (the
        user steers mid-session) and after DONE (task_done completes the session
        but the user may continue - protocol.md section 8). From DONE this
        reopens the session, transitioning back to AWAITING_REPLY for the reply."""
        if self._phase not in (Phase.AWAITING_REPLY, Phase.DONE):
            raise EngineStateError(
                f"follow_up() requires phase AWAITING_REPLY or DONE, but engine is {self._phase.name}"
            )
        outbound = self._composer.task(self._turn + 1, text)
        self._turn += 1
        self._session.append_event("task", text=text, turn=self._turn)
        self._register_outbound(outbound)
        if self._phase is Phase.DONE:
            self._set_phase(Phase.AWAITING_REPLY)  # reopen the completed session
        return outbound

    # -- inbound -------------------------------------------------------------

    def ingest(self, text: str) -> IngestResult:
        """Parse one clipboard text. Only meaningful in AWAITING_REPLY: any
        other phase returns Noise("wrong-phase") and the TUI decides what to
        show (e.g. the unexpected-reply modal)."""
        if self._phase is not Phase.AWAITING_REPLY:
            return Noise("wrong-phase")
        reply = parse_reply(text)
        if reply.kind == "noise":
            return Noise("not-protocol")
        if reply.normalized_hash in self._seen_hashes:
            return Noise("duplicate")  # duplicate copy or our own outbound: silent
        self._remember_hash(reply.normalized_hash)
        self._session.append_event("inbound", raw=text)
        self._session.append_event(
            "parsed",
            kind=reply.kind,
            calls=[
                {"id": c.id, "tool": c.tool, "issues": [i.kind for i in c.issues]}
                for c in reply.calls
            ],
            warnings=[w.kind for w in reply.warnings],
            truncated=reply.truncated,
            eom_turn=reply.eom.turn,
        )
        if reply.kind == "ack":
            return ChunkAck(reply.ack_part, reply.ack_total)
        if reply.kind == "nack":
            reason = reply.nack_reason or "unspecified"
            return ProtocolError(
                f"model NACKed the last paste (reason={reason}); re-copy the outbound payload"
            )
        if reply.eom.turn is not None and reply.eom.turn < self._turn:
            return Noise("stale-turn")
        self._reply = reply
        self._plan = self._build_plan(reply)
        self._set_phase(Phase.REVIEW)
        return NewTurn(reply)

    # -- review --------------------------------------------------------------

    def pending(self) -> tuple[PendingAction, ...]:
        """Calls still needing a user decision, in id order. Auto calls and
        pre-resolved errors never appear; neither do ask_user/task_done."""
        return tuple(
            p.action
            for p in self._plan
            if p.needs_decision and p.decision is None and not p.aborted
        )

    def decide(self, call_id: int, decision: Decision, note: str | None = None) -> None:
        self._require_phase(Phase.REVIEW, "decide")
        planned = self._find_pending(call_id)
        if decision is Decision.REJECT:
            planned.decision = Decision.REJECT
            planned.note = note
            self._log_decision(call_id, "denied", "user", note)
            self._abort_after(planned)
            return
        planned.decision = Decision.APPROVE
        self._log_decision(call_id, "approved", "user", note)
        if decision is Decision.APPROVE_ALL_EDITS:
            self._policy.auto_accept_edits = True
            for other in self._plan:
                if (
                    other.needs_decision
                    and other.decision is None
                    and not other.aborted
                    and other.action.kind == "edit"  # never commands
                ):
                    other.decision = Decision.APPROVE
                    self._log_decision(other.call.id, "approved", "auto_edits", None)

    def all_decided(self) -> bool:
        return not any(
            p.needs_decision and p.decision is None and not p.aborted for p in self._plan
        )

    # -- execution -------------------------------------------------------------

    def execute(self) -> StepResult:
        """REVIEW -> AWAITING_REPLY | AWAITING_USER | DONE."""
        self._require_phase(Phase.REVIEW, "execute")
        if not self.all_decided():
            raise EngineStateError("execute() called with undecided pending actions")
        self._exec = _ExecState()
        return self._run_plan(0)

    def answer_user(self, text: str) -> StepResult:
        """Resume after AskUser: the answer becomes the ask_user call's ok
        result (verbatim body) and the remaining calls execute."""
        self._require_phase(Phase.AWAITING_USER, "answer_user")
        assert self._exec is not None
        waiting = self._plan[self._exec.index]
        self._record(
            ToolResult(call_id=waiting.call.id, status="ok", body=text, tool=waiting.call.tool)
        )
        return self._run_plan(self._exec.index + 1)

    # -- undo ------------------------------------------------------------------

    def undo_last_turn(self, *, compose_notice: bool = True) -> tuple[UndoReport, Outbound | None]:
        """Revert the newest undoable turn from the backup store. With
        compose_notice, also returns a NOTE payload telling the LLM its mental
        file state must roll back."""
        if self._phase not in (Phase.AWAITING_REPLY, Phase.DONE):
            raise EngineStateError(f"undo_last_turn() is not available in {self._phase.name}")
        turn = self._backups.latest_undoable_turn()
        if turn is None:
            raise EngineStateError("nothing to undo: no undoable turn on disk")
        report = self._backups.undo_turn(turn)
        self._session.append_event(
            "undo",
            turn=turn,
            restored=list(report.restored),
            deleted=list(report.deleted),
            recreated=list(report.recreated),
            warnings=list(report.warnings),
        )
        outbound: Outbound | None = None
        if compose_notice:
            outbound = self._composer.note(self._turn + 1, _undo_notice(report))
            self._turn += 1
            self._register_outbound(outbound)
            if self._phase is Phase.DONE:
                # The revert notice is a payload the model must answer, so an undo
                # from a completed session must reopen it (symmetric with follow_up;
                # otherwise the model's reply ingests as Noise("wrong-phase")).
                self._set_phase(Phase.AWAITING_REPLY)
        return report, outbound

    # -- status ----------------------------------------------------------------

    def status(self) -> StatusSnapshot:
        return StatusSnapshot(
            phase=self._phase,
            turn=self._turn,
            service_key=self._config.general.service,
            budget_chars=self._config.preset().max_paste_chars,
            auto_accept_edits=self._policy.auto_accept_edits,
            yolo=self._policy.yolo,
            session_dir=self._session.session_dir,
            last_outbound_chars=self._last_outbound_chars,
        )

    def set_yolo(self, enabled: bool) -> bool:
        """Toggle YOLO mode: auto-approve EVERY tool call (edits and commands),
        bypassing the allowlist and the deny tokens. Session-scoped and legal in
        any phase - it only flips the policy flag, so it never races the state
        machine. It does not revisit decisions already made this turn; it governs
        every plan built afterwards. Returns the new state."""
        self._policy.yolo = enabled
        self._session.append_event("yolo", enabled=enabled)
        return enabled

    # -- planning ----------------------------------------------------------------

    def _build_plan(self, reply: ParsedReply) -> list[_Planned]:
        plan: list[_Planned] = []
        for call in reply.calls:
            fatal = [i for i in call.issues if i.kind in _FATAL_ISSUES]
            if fatal:
                plan.append(
                    _Planned(
                        call,
                        None,
                        PendingAction(call, "auto", "", "pre-resolved parse error"),
                        pre_result=self._parse_issue_result(call),
                    )
                )
                continue
            spec = self._registry.get(call.tool)
            if spec is None:
                plan.append(
                    _Planned(
                        call,
                        None,
                        PendingAction(call, "auto", "", "pre-resolved unknown tool"),
                        pre_result=self._unknown_tool_result(call),
                    )
                )
                continue
            if call.tool in ("ask_user", "task_done"):
                # Intercepted by name during execution; never pending, never gated.
                plan.append(
                    _Planned(call, spec, PendingAction(call, "auto", "", "handled by AgentClip"))
                )
                continue
            if self._policy.verdict(spec, call) == "auto":
                reason, source = self._auto_reason(spec, call)
                plan.append(_Planned(call, spec, PendingAction(call, "auto", "", reason)))
                self._log_decision(call.id, "auto", source, None)
                continue
            kind: Literal["edit", "command"] = "edit" if spec.approval_kind == "edit" else "command"
            preview = (
                spec.preview(self._ctx, call)
                if spec.preview is not None
                else call.params.get("command", "")
            )
            plan.append(
                _Planned(
                    call,
                    spec,
                    PendingAction(call, kind, preview, None),
                    needs_decision=True,
                )
            )
        return plan

    def _auto_reason(self, spec: ToolSpec, call: ToolCall) -> tuple[str, str]:
        if spec.approval_kind == "auto":
            return "read-only tool", "auto"
        # Edits and commands only reach here when something auto-approved them.
        if self._policy.yolo:
            return "YOLO mode (auto-approve all)", "yolo"
        if spec.approval_kind == "command":
            matched = self._policy.command_auto_allowed(call.params.get("command", ""))
            return f'matched "{matched}"', "allowlist"
        return "auto-accept edits enabled", "auto_edits"

    def _parse_issue_result(self, call: ToolCall) -> ToolResult:
        fatal = [i for i in call.issues if i.kind in _FATAL_ISSUES]
        if any(i.kind == "unterminated_heredoc" for i in fatal):
            code = "unterminated_heredoc"
            hint = "resend this call; terminate every heredoc with its tag alone on a line."
        else:
            code = "parse_error"
            hint = "resend this call using the exact CALL block grammar."
        raw_lines = call.raw.split("\n")[:10]
        message = (
            f"call id={call.id} could not be parsed and was NOT executed:\n"
            + "\n".join(i.detail for i in fatal)
            + "\noffending block (first lines):\n"
            + "\n".join(raw_lines)
            + "\ngrammar reminder: ===CLIP:CALL id=N tool=name=== then key: value lines"
            " and/or key <<TAG heredocs, then ===CLIP:END==="
        )
        return error_result(call, code, message, hint)

    def _unknown_tool_result(self, call: ToolCall) -> ToolResult:
        names = ", ".join(self._registry.names())
        return error_result(
            call,
            "unknown_tool",
            f"unknown tool: {call.tool!r}\nvalid tools: {names}",
            "use one of the valid tools listed above.",
        )

    def _find_pending(self, call_id: int) -> _Planned:
        for p in self._plan:
            if p.call.id == call_id:
                if not p.needs_decision or p.aborted:
                    raise ValueError(f"call id={call_id} does not need a decision")
                if p.decision is not None:
                    raise ValueError(f"call id={call_id} is already decided")
                return p
        raise ValueError(f"no call with id={call_id} in this turn")

    def _abort_after(self, rejected: _Planned) -> None:
        """Rejection aborts the rest of the turn: every later call that would
        have executed is marked skipped (pre-resolved errors still emit as-is,
        they never run anyway and their diagnostics help the model)."""
        seen = False
        for p in self._plan:
            if p is rejected:
                seen = True
                continue
            if seen and p.pre_result is None:
                p.aborted = True

    # -- the execution loop -------------------------------------------------------

    def _run_plan(self, start: int) -> StepResult:
        exec_ = self._exec
        assert exec_ is not None
        for i in range(start, len(self._plan)):
            p = self._plan[i]
            call = p.call
            if p.pre_result is not None:
                self._record(p.pre_result)
                continue
            if p.aborted:
                self._record(
                    ToolResult(
                        call_id=call.id,
                        status="skipped",
                        body="did not run.\nhint: turn aborted after a rejection"
                        " - resend this call if still wanted.",
                        tool=call.tool,
                    )
                )
                continue
            if p.decision is Decision.REJECT:
                self._record(
                    ToolResult(
                        call_id=call.id,
                        status="denied",
                        body="denied by the user at the approval gate.\nhint: do not"
                        " retry unchanged - reconsider or use ask_user.",
                        tool=call.tool,
                        user_note=p.note,
                    )
                )
                if call.tool in _MUTATING_TOOLS:
                    exec_.failed_paths.add(_norm_path(call.params.get("path", "")))
                continue
            if call.tool in _MUTATING_TOOLS:
                key = _norm_path(call.params.get("path", ""))
                if key and key in exec_.failed_paths:
                    self._record(
                        ToolResult(
                            call_id=call.id,
                            status="skipped",
                            body="did not run.\nhint: prior edit of this file failed;"
                            " resend after fixing.",
                            tool=call.tool,
                        )
                    )
                    continue
            if call.tool == "ask_user":
                question = call.params.get("question", "").strip()
                if not question:
                    self._record(
                        error_result(
                            call,
                            "missing_param",
                            "missing required parameter: question",
                            "resend ask_user with a question parameter.",
                        )
                    )
                    continue
                exec_.index = i
                self._set_phase(Phase.AWAITING_USER)
                return AskUser(question=question, call_id=call.id)
            if call.tool == "task_done":
                exec_.done_summary = call.params.get("summary", "")
                continue
            assert p.spec is not None
            result = p.spec.handler(self._ctx, call)
            if result.status == "error" and call.tool in _MUTATING_TOOLS:
                exec_.failed_paths.add(_norm_path(call.params.get("path", "")))
            self._record(result)
        return self._finish_turn()

    def _finish_turn(self) -> StepResult:
        exec_ = self._exec
        reply = self._reply
        assert exec_ is not None and reply is not None
        if exec_.backup_started:
            self._backups.finish_turn()
        results = list(exec_.results)
        if reply.truncated:
            results.insert(0, _truncated_result(reply))
        notes = [f"note: {w.detail}" for w in reply.warnings if w.kind in _NOTE_WARNINGS]
        if not reply.calls and not reply.truncated and exec_.done_summary is None:
            notes.append(
                "note: your reply contained no tool calls; every reply must contain"
                " at least one call until task_done."
            )
        self._plan = []
        self._reply = None
        self._exec = None
        if exec_.done_summary is not None:
            self._session.append_event("task_done", summary=exec_.done_summary)
            outbound = self._compose_results(results, notes) if results else None
            self._set_phase(Phase.DONE)
            return Done(exec_.done_summary, outbound)
        outbound = self._compose_results(results, notes)
        self._set_phase(Phase.AWAITING_REPLY)
        return Send(outbound)

    def _compose_results(self, results: list[ToolResult], notes: list[str]) -> Outbound:
        capped = fit_results(results, self._config.limits.max_result_chars)
        next_turn = self._turn + 1
        try:
            outbound = self._composer.results(next_turn, capped, notes)
        except BudgetExceeded as exc:
            # The composer's line-boundary fitting could not get under budget
            # (e.g. a single enormous line). Cut harder, mid-line if needed.
            self._session.append_event("error", detail=f"results over budget, refitting: {exc}")
            budget = self._config.preset().max_paste_chars
            per_result = max(120, (budget - 600) // max(len(capped), 1) - 150)
            outbound = self._composer.results(next_turn, fit_results(capped, per_result), notes)
        self._turn = next_turn
        self._register_outbound(outbound)
        return outbound

    # -- shared internals -----------------------------------------------------------

    def _backup_hook(self, rel: str, abs_path: Path, action: str) -> None:
        """Wired into ToolContext; mutating handlers call it before first touch."""
        exec_ = self._exec
        assert exec_ is not None, "backup hook fired outside execute()"
        if not exec_.backup_started:
            self._backups.begin_turn(self._turn)
            exec_.backup_started = True
        if action == "delete":
            self._backups.snapshot_before_delete(rel, abs_path)
        else:
            self._backups.snapshot_before_write(rel, abs_path)

    def _record(self, result: ToolResult) -> None:
        assert self._exec is not None
        self._exec.results.append(result)
        self._session.append_event(
            "result",
            call_id=result.call_id,
            tool=result.tool,
            status=result.status,
            code=result.code,
            chars=len(result.body),
        )

    def _log_decision(self, call_id: int, verdict: str, source: str, note: str | None) -> None:
        self._session.append_event(
            "decision", call_id=call_id, verdict=verdict, source=source, note=note
        )

    def _register_outbound(self, outbound: Outbound) -> None:
        """Persist + audit one composed payload and pre-register its hash so a
        re-ingest of our own text is dropped as a duplicate."""
        self._session.write_outbound(outbound.turn, _CHUNK_SEPARATOR.join(outbound.chunks))
        self._session.append_event(
            "outbound",
            kind=outbound.kind,
            turn=outbound.turn,
            total_chars=outbound.total_chars,
            chunks=len(outbound.chunks),
        )
        for chunk in outbound.chunks:
            self._remember_hash(normalized_hash(chunk))
        self._last_outbound_chars = outbound.total_chars

    def _remember_hash(self, digest: str) -> None:
        if digest not in self._seen_hashes:
            self._seen_hashes.append(digest)

    def _require_phase(self, phase: Phase, method: str) -> None:
        if self._phase is not phase:
            raise EngineStateError(
                f"{method}() requires phase {phase.name}, but engine is {self._phase.name}"
            )

    def _set_phase(self, new: Phase) -> None:
        if not can_transition(self._phase, new):
            raise EngineStateError(f"illegal transition {self._phase.name} -> {new.name}")
        self._phase = new


# -- module-level helpers ---------------------------------------------------------


def _truncated_result(reply: ParsedReply) -> ToolResult:
    """The id=0 reply_truncated error result (protocol.md section 5.2)."""
    complete = [c for c in reply.calls if not any(i.kind in _FATAL_ISSUES for i in c.issues)]
    partial = [c for c in reply.calls if any(i.kind in _FATAL_ISSUES for i in c.issues)]
    lines = ["Your reply was cut off."]
    if complete:
        ids = ", ".join(f"id={c.id}" for c in complete)
        lines.append(
            f"Received {len(complete)} complete call(s) ({ids}); they were processed"
            " and their results are below."
        )
    else:
        lines.append("No complete calls were received.")
    for c in partial:
        what = "; ".join(i.detail for i in c.issues if i.kind in _FATAL_ISSUES)
        tool = c.tool or "unknown"
        lines.append(f"Partial call id={c.id} (tool={tool}): {what}. It was NOT executed.")
    if reply.eom.present and reply.eom.calls is not None and reply.eom.calls != len(reply.calls):
        lines.append(
            f"Your EOM declared calls={reply.eom.calls} but {len(reply.calls)}"
            " CALL block(s) arrived."
        )
    elif not reply.eom.present:
        lines.append("The final ===CLIP:EOM=== line was missing.")
    resend_from = partial[0].id if partial else len(reply.calls) + 1
    hint = f"resend call id={resend_from} and any later calls."
    if complete:
        hint += " Do not resend the calls processed above."
    hint += (
        " If a content block is too large for one reply, send the first half with"
        " write_file mode: create and the rest with mode: append across replies."
    )
    body = "\n".join(lines) + f"\nhint: {hint}"
    return ToolResult(call_id=0, status="error", body=body, tool="", code="reply_truncated")


def _undo_notice(report: UndoReport) -> str:
    lines = [
        f"The user reverted turn {report.turn} with AgentClip's undo."
        " The files below are back to their state from BEFORE that turn:"
    ]
    if report.restored:
        lines.append("- restored to pre-turn content: " + ", ".join(report.restored))
    if report.deleted:
        lines.append("- deleted (that turn had created them): " + ", ".join(report.deleted))
    if report.recreated:
        lines.append("- restored (that turn had deleted them): " + ", ".join(report.recreated))
    if report.warnings:
        lines.append("- warnings: " + "; ".join(report.warnings))
    lines.append(
        "Update your mental model of these files accordingly; re-read them before"
        " editing. run_command side effects (if any) were not undone."
    )
    return "\n".join(lines)


__all__ = [
    "AskUser",
    "ChunkAck",
    "Decision",
    "Done",
    "Engine",
    "EngineStateError",
    "IngestResult",
    "NewTurn",
    "Noise",
    "PendingAction",
    "Phase",
    "ProtocolError",
    "Send",
    "StatusSnapshot",
    "StepResult",
]
