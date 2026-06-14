"""SessionController: the UI-agnostic session orchestrator.

This is the engine's host - the state machine that drives a whole AgentClip
session, lifted out of the Textual ``MainScreen`` so the UI can be swapped. It
owns the Engine, the async flow state machine, the approval gate / ask_user
futures, session stats, the per-turn glyph strip, and the depth-1 mid-turn reply
queue. It talks to the UI ONLY through the :class:`~agentclip.app.view.ChatView`
port and therefore imports no Textual and no ``clip`` (clipboard I/O is a view
concern - see ``ChatView.copy_outbound`` / ``read_clipboard``).

Threading model (unchanged from the old MainScreen, now expressed through the port):

- Every Engine call is funneled through :meth:`_engine_call`, which serializes
  via an ``asyncio.Lock`` and offloads to a thread (``asyncio.to_thread``) so a
  minutes-long ``execute()`` never blocks the event loop.
- Flow coroutines run as background workers via ``view.spawn``; only one runs at a
  time (the ``busy`` flag). A reply arriving mid-turn is queued depth-1, newest wins.
- The approval gate is an ``asyncio.Future`` resolved by ``submit_decision``;
  ask_user uses a second future resolved by ``submit_message``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from agentclip.app.types import SessionStats
from agentclip.app.view import ChatView, SessionView
from agentclip.config import Config, ServicePreset
from agentclip.engine.engine import (
    AskUser,
    ChunkAck,
    Decision,
    Done,
    Engine,
    EngineStateError,
    NewTurn,
    Noise,
    PendingAction,
    Phase,
    ProtocolError,
    Send,
    StatusSnapshot,
    StepResult,
)
from agentclip.protocol.composer import BudgetExceeded
from agentclip.protocol.types import Outbound, ParsedReply

_T = TypeVar("_T")

_NOISE_TEXT = {
    "duplicate": "duplicate reply ignored",
    "stale-turn": "stale reply ignored (it echoes an older turn)",
    "not-protocol": "clipboard text has no CLIP blocks - ignored",
    "wrong-phase": "reply ignored - not awaiting a reply right now",
}


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


class SessionController:
    """Synchronous-at-heart session driver; UI-agnostic via the ChatView port."""

    def __init__(
        self,
        config: Config,
        engine_factory: Callable[[str], Engine],
        project_root: Path,
        *,
        view: ChatView,
    ) -> None:
        self._config = config
        self._engine_factory = engine_factory
        self._project_root = project_root
        self._view = view

        self._engine: Engine | None = None
        self._preset: ServicePreset | None = None
        self._snap: StatusSnapshot | None = None
        self._engine_lock = asyncio.Lock()
        self._gate_future: asyncio.Future[tuple[Decision, str | None]] | None = None
        self._answer_future: asyncio.Future[str] | None = None
        self._queued_capture: str | None = None
        self._last_outbound: str | None = None
        self._stats = SessionStats()
        self._turn_glyphs: dict[int, list[str]] = {}  # call id -> [glyph, tool]

        # state flags mirrored to the view via SessionView
        self._session_active = False
        self._busy = False
        self._pending_approval = False
        self._awaiting_answer = False
        self._has_outbound = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Kick off the session: prompt for a task, then run the loop."""
        self._spawn_flow(self._session_flow())

    # -- view-facing events ---------------------------------------------------

    def submit_clipboard(self, text: str) -> None:
        """A captured (or injected) clipboard reply. Queued if a turn is busy."""
        if not self._session_active or self._engine is None:
            return
        if self._busy:
            self._queued_capture = text  # depth-1 queue, newest wins
            self._view.notify("reply received mid-turn - queued (newest wins)", severity="warning")
            return
        self._spawn_flow(self._ingest_flow(text))

    def submit_message(self, text: str) -> None:
        """Composer send: an ask_user answer, or a follow-up message."""
        text = text.strip()
        if not text:
            return
        if self._awaiting_answer:
            future = self._answer_future
            if future is not None and not future.done():
                self._view.reset_composer()
                future.set_result(text)
            else:  # a previous send already resolved it (sub-frame double-tap)
                self._view.notify("answer already sent - please wait", severity="warning")
            return
        if self._session_active and not self._busy and self._can_follow_up():
            self._view.reset_composer()
            self._spawn_flow(self._follow_up_flow(text))
            return
        self._view.notify(
            "can't send right now - wait for the current step to finish", severity="warning"
        )

    def submit_decision(self, decision: Decision, note: str | None) -> None:
        """Resolve the approval gate (from a key action or panel button)."""
        future = self._gate_future
        if future is not None and not future.done():
            future.set_result((decision, note))

    def undo(self) -> None:
        if self._busy or not self._session_active:
            return
        self._spawn_flow(self._undo_flow())

    def force_ingest(self) -> None:
        if self._busy or not self._session_active:
            return
        self._spawn_flow(self._force_ingest_flow())

    def end_session(self) -> None:
        if self._busy or not self._session_active:
            return
        self._spawn_flow(self._show_summary())

    def recopy(self) -> None:
        text = self._last_outbound
        if text is None:
            return
        self._view.spawn(self._recopy(text))

    def export_log(self) -> None:
        # Read-only snapshot of in-memory state - runs OUTSIDE the flow worker so
        # it never sets busy or touches the engine, and is safe mid-turn.
        if not self._session_active:
            return
        self._view.spawn(self._export_log())

    # -- flow plumbing --------------------------------------------------------

    def _spawn_flow(self, coro: Coroutine[Any, Any, None]) -> None:
        self._busy = True
        self._push_state()
        self._view.spawn(self._wrap_flow(coro))

    async def _wrap_flow(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except (EngineStateError, BudgetExceeded) as exc:
            await self._view.add_error(str(exc))
            self._view.alert(str(exc), severity="error")
        finally:
            self._busy = False
            self._pending_approval = False
            self._awaiting_answer = False
            self._view.stop_working()
            self._view.hide_gate()
        await self._refresh_status()
        queued, self._queued_capture = self._queued_capture, None
        if queued is not None and self._session_active and self._engine is not None:
            self._spawn_flow(self._ingest_flow(queued))

    async def _engine_call(self, fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        """Serialize every engine call and run it off the event loop."""
        async with self._engine_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def _run_engine_step(self, fn: Callable[..., _T], /, *args: object) -> _T:
        """Run execute()/answer_user() with the 'working' spinner showing meanwhile."""
        n = len(self._turn_glyphs)
        label = f"Working - running {n} tool call{'' if n == 1 else 's'}..." if n else "Working..."
        self._view.start_working(label)
        try:
            return await self._engine_call(fn, *args)
        finally:
            self._view.stop_working()

    # -- session start --------------------------------------------------------

    async def _session_flow(self) -> None:
        while True:
            spec = await self._view.prompt_new_session()
            if spec is None:
                self._view.exit_app()
                return
            engine = await asyncio.to_thread(self._engine_factory, spec.service)
            try:
                out = await self._engine_call(engine.start_task, spec.task)
            except BudgetExceeded as exc:
                self._view.notify(
                    f"the bootstrap needs {exc.needed_chars:,} chars but {spec.service!r} "
                    f"allows {exc.budget_chars:,} - pick a larger-budget preset "
                    "(chunked sends land in M3)",
                    severity="error",
                    timeout=10,
                )
                continue
            break
        self._engine = engine
        self._preset = self._config.services.get(spec.service, self._config.preset())
        self._stats = SessionStats(service=spec.service)
        await self._view.add_user(spec.task)
        await self._copy_outbound(out)
        await self._view.add_note(
            f"→ bootstrap copied ({out.total_chars:,} chars) - paste into {self._preset.label}"
        )
        self._session_active = True
        await self._refresh_status()
        self._view.start_input()  # starts the watcher (or shows the manual-mode note)
        self._view.notify(
            f"bootstrap copied ({out.total_chars:,} chars) - paste into {self._preset.label}",
            timeout=8,
        )

    # -- ingest -> review -> execute -----------------------------------------

    async def _ingest_flow(self, text: str, *, forced: bool = False) -> None:
        engine = self._engine
        if engine is None:
            return
        result = await self._engine_call(engine.ingest, text)
        if isinstance(result, Noise):
            if forced and result.reason == "not-protocol":
                await self._view.add_prose(text[:4000])
                self._view.notify(
                    "no tool calls found - reply shown in transcript; press t to follow up"
                )
            else:
                self._view.notify(_NOISE_TEXT.get(result.reason, result.reason))
            return
        if isinstance(result, ProtocolError):
            await self._view.add_error(
                f"protocol error: {result.detail} - press c to re-copy the last outbound"
            )
            self._view.alert("protocol error - see transcript", severity="error")
            return
        if isinstance(result, ChunkAck):
            self._view.notify("chunk ACK received, but chunked sends land in M3", severity="warning")
            return
        assert isinstance(result, NewTurn)
        self._stats.replies += 1
        self._stats.chars_in += len(text)
        await self._run_turn(result.reply)

    async def _run_turn(self, reply: ParsedReply) -> None:
        engine = self._engine
        assert engine is not None
        for prose in reply.prose:
            if prose.strip():
                await self._view.add_prose(prose)
        for call in reply.calls:
            self._stats.calls[call.tool] += 1
            await self._view.add_call(call)
        if reply.truncated:
            await self._view.add_error(
                "reply arrived truncated - the model will be told to resend the missing tail"
            )
        await self._refresh_status()  # REVIEW

        self._turn_glyphs = {c.id: ["•", c.tool] for c in reply.calls}
        done = 0
        while True:
            pending = await self._engine_call(engine.pending)
            if not pending:
                break
            action = pending[0]
            self._set_glyph(action.call.id, "▶")
            decision, note = await self._gate(action, f"{done + 1}/{done + len(pending)}")
            await self._engine_call(engine.decide, action.call.id, decision, note)
            done += 1
            target = action.call.params.get("path") or action.call.params.get("command", "")
            if decision is Decision.REJECT:
                self._set_glyph(action.call.id, "✗")
                for glyph in self._turn_glyphs.values():
                    if glyph[0] in ("•", "▶"):
                        glyph[0] = "−"
                reason = f': "{note}"' if note else ""
                await self._view.add_note(
                    f"✗ rejected {action.call.tool} {target}{reason} - remaining calls skipped"
                )
            else:
                self._set_glyph(action.call.id, "✓")
                label = (
                    "approved (auto-accept edits ON)"
                    if decision is Decision.APPROVE_ALL_EDITS
                    else "approved"
                )
                await self._view.add_note(f"✓ {label} {action.call.tool} {target}".rstrip())
        self._view.hide_gate()
        await self._refresh_status()  # EXECUTING (status segment driven by busy)
        step = await self._run_engine_step(engine.execute)
        await self._handle_step(step)

    def _set_glyph(self, call_id: int, glyph: str) -> None:
        if call_id in self._turn_glyphs:
            self._turn_glyphs[call_id][0] = glyph

    def _queue_strip(self) -> str:
        return "  ".join(
            f"{glyph}{cid} {tool}" for cid, (glyph, tool) in sorted(self._turn_glyphs.items())
        )

    async def _gate(self, action: PendingAction, position: str) -> tuple[Decision, str | None]:
        self._pending_approval = True
        self._push_state()  # composer disabled while the gate is up
        self._view.show_gate(action, position, self._queue_strip())
        self._view.alert(f"approval needed: {action.call.tool}", severity="warning")
        self._gate_future = asyncio.get_running_loop().create_future()
        try:
            return await self._gate_future
        finally:
            self._gate_future = None
            self._pending_approval = False
            self._push_state()
            # NB: do NOT hide the gate here - in a multi-call turn the panel must
            # stay up between sequential gates (the next show_gate updates it). It
            # is hidden once at the end of _run_turn (and by _wrap_flow on teardown).

    async def _handle_step(self, step: StepResult) -> None:
        engine = self._engine
        assert engine is not None
        while isinstance(step, AskUser):
            await self._view.add_note(f"? {step.question}")
            answer = await self._ask(step.question)
            await self._view.add_user(answer)
            step = await self._run_engine_step(engine.answer_user, answer)
        if isinstance(step, Send):
            await self._copy_outbound(step.outbound)
            await self._view.add_outbound(step.outbound, "results copied")
            self._view.alert(
                f"results copied ({step.outbound.total_chars:,} chars) - paste into the chat"
            )
            await self._refresh_status()
            return
        assert isinstance(step, Done)
        if step.outbound is not None:
            await self._copy_outbound(step.outbound)
            await self._view.add_outbound(step.outbound, "final results copied")
        self._stats.summary = step.summary
        await self._view.add_note("✓ task done")
        if step.summary.strip():
            await self._view.add_prose(step.summary)
        await self._view.add_note(
            "session complete - type a follow-up to keep going, or press e for the summary"
        )
        self._view.alert("task done", severity="information")
        await self._refresh_status()
        # NB: do NOT push the summary modal here. task_done completes the session
        # but the user may continue (protocol.md section 8): the composer stays
        # enabled in DONE so a follow-up reopens the session, and the summary +
        # stats are one keypress away (the e / end_session action).

    async def _ask(self, question: str) -> str:
        self._awaiting_answer = True  # the view switches the composer into answer mode
        self._push_state()
        self._view.alert("the model asks you a question - type your answer below", severity="warning")
        self._answer_future = asyncio.get_running_loop().create_future()
        try:
            return await self._answer_future
        finally:
            self._answer_future = None
            self._awaiting_answer = False
            self._push_state()

    # -- summary / reset ------------------------------------------------------

    async def _show_summary(self) -> None:
        while True:
            action = await self._view.show_summary(self._stats_rows(), self._stats.summary)
            if action == "export":  # export, then return to the summary
                await self._export_log()
                continue
            break
        if action == "undo":
            await self._undo_flow()
        elif action == "new":
            await self._reset_session()

    def _stats_rows(self) -> list[tuple[str, str]]:
        snap = self._snap
        rows: list[tuple[str, str]] = [
            ("service", self._stats.service or "-"),
            ("turns", str(snap.turn) if snap else "0"),
            ("replies ingested", str(self._stats.replies)),
        ]
        calls = ", ".join(f"{tool}×{n}" for tool, n in sorted(self._stats.calls.items()))
        rows.append(("tool calls", calls or "none"))
        rows.append(("chars copied out", f"{self._stats.chars_out:,}"))
        rows.append(("chars ingested", f"{self._stats.chars_in:,}"))
        if snap is not None:
            rows.append(("session dir", str(snap.session_dir)))
        return rows

    async def _reset_session(self) -> None:
        self._session_active = False
        self._engine = None
        self._preset = None
        self._snap = None
        self._last_outbound = None
        self._has_outbound = False
        self._queued_capture = None
        self._stats = SessionStats()
        await self._view.clear_transcript()
        self._push_state()  # phase -> IDLE (snap is None)
        await self._session_flow()

    # -- undo / follow-up / manual ingest ------------------------------------

    async def _undo_flow(self) -> None:
        engine = self._engine
        if engine is None:
            return
        confirmed = await self._view.confirm(
            "Undo the most recent turn?",
            "Files changed by that turn are restored from the per-turn backup. "
            "run_command side effects are NOT undone. A revert notice for the "
            "model will be composed and copied.",
        )
        if not confirmed:
            return
        try:
            report, notice = await self._engine_call(engine.undo_last_turn, compose_notice=True)
        except EngineStateError as exc:
            self._view.notify(str(exc), severity="warning")
            return
        parts = []
        if report.restored:
            parts.append(f"{len(report.restored)} restored")
        if report.deleted:
            parts.append(f"{len(report.deleted)} deleted")
        if report.recreated:
            parts.append(f"{len(report.recreated)} recreated")
        await self._view.add_note(
            f"↩ undid turn {report.turn} ({', '.join(parts) or 'nothing to restore'})"
        )
        for warning in report.warnings:
            self._view.notify(warning, severity="warning")
        if notice is not None:
            await self._copy_outbound(notice)
            await self._view.add_note(
                f"→ revert notice copied ({notice.total_chars:,} chars) - paste it into the chat"
            )

    async def _follow_up_flow(self, text: str) -> None:
        engine = self._engine
        if engine is None:
            return
        out = await self._engine_call(engine.follow_up, text)
        await self._view.add_user(text)
        await self._copy_outbound(out)
        await self._refresh_status()
        self._view.notify(f"follow-up copied ({out.total_chars:,} chars) - paste into the chat")

    async def _force_ingest_flow(self) -> None:
        text = await self._view.read_clipboard()
        if not text:
            text = await self._view.prompt_text(
                "Paste the model's reply",
                "the clipboard had no text - paste the reply here; ctrl+s ingests",
            )
            if not text:
                return
        await self._ingest_flow(text, forced=True)

    # -- outbound copies ------------------------------------------------------

    async def _copy_outbound(self, outbound: Outbound) -> None:
        if len(outbound.chunks) > 1:  # cannot happen with the M1 composer
            self._view.notify(
                "multi-part outbound - only part 1 copied (chunk walk lands in M3)",
                severity="warning",
            )
        text = outbound.chunks[0]
        await self._view.copy_outbound(text)
        self._last_outbound = text
        self._has_outbound = True
        self._stats.chars_out += outbound.total_chars

    async def _recopy(self, text: str) -> None:
        await self._view.copy_outbound(text)
        self._view.notify(f"re-copied the last outbound ({len(text):,} chars)")

    # -- export log -----------------------------------------------------------

    async def _export_log(self) -> None:
        if not self._view.has_transcript_events():
            self._view.notify("nothing to export yet")
            return
        text = self._view.render_log(self._log_meta())
        snap = self._snap
        target_dir = snap.session_dir if snap is not None else self._project_root / ".agentclip"
        path = target_dir / f"chat-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        try:
            await asyncio.to_thread(self._write_log, path, text)
        except OSError as exc:
            self._view.notify(f"could not write the chat log: {exc}", severity="error")
            return
        await self._view.add_note(f"⤓ chat log exported → {path}")
        self._view.notify(f"chat log exported ({len(text):,} chars) → {path}", timeout=8)

    @staticmethod
    def _write_log(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _log_meta(self) -> list[str]:
        meta = [f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
        preset = self._preset
        if preset is not None:
            meta.append(f"Service: {preset.label} ({_fmt_k(preset.max_paste_chars)} budget)")
        try:
            root = str(Path("~") / self._project_root.relative_to(Path.home()))
        except ValueError:
            root = str(self._project_root)
        meta.append(f"Project: {root}")
        snap = self._snap
        if snap is not None:
            meta.append(f"Session dir: {snap.session_dir}")
            meta.append(f"Turn: {snap.turn}")
        stats = self._stats
        meta.append(f"Replies ingested: {stats.replies}")
        calls = ", ".join(f"{tool}×{n}" for tool, n in sorted(stats.calls.items()))
        meta.append(f"Tool calls: {calls or 'none'}")
        return meta

    # -- status push ----------------------------------------------------------

    async def _refresh_status(self) -> None:
        engine = self._engine
        if engine is not None:
            self._snap = await self._engine_call(engine.status)
        self._push_state()

    def _push_state(self) -> None:
        self._view.render_state(
            SessionView(
                session_active=self._session_active,
                busy=self._busy,
                pending_approval=self._pending_approval,
                awaiting_answer=self._awaiting_answer,
                has_outbound=self._has_outbound,
                snapshot=self._snap,
            )
        )

    def _can_follow_up(self) -> bool:
        # A follow-up is legal while armed for a reply AND after task_done:
        # task_done completes the session but the user may continue (protocol.md
        # section 8). A DONE follow-up reopens the session into AWAITING_REPLY.
        return self._snap is not None and self._snap.phase in (Phase.AWAITING_REPLY, Phase.DONE)
