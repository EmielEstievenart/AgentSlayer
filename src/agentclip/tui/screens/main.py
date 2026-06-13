"""MainScreen: the session orchestrator.

Threading model (tui.md section 3 / architecture.md section 11):

- ONE clipboard thread runs ``clip.watcher.watch`` via ``run_worker(thread=True)``
  and bridges captures back with the thread-safe ``post_message``.
- The engine is synchronous and not thread-safe. Every engine call is funneled
  through :meth:`_engine_call`, which serializes via an asyncio.Lock and
  offloads the actual call to a thread (``asyncio.to_thread``) so a
  minutes-long ``execute()`` never blocks the event loop. Only one flow worker
  runs at a time (``busy`` flag); a reply arriving mid-turn is queued depth-1,
  newest wins.
- The approval gate is an asyncio.Future awaited by the flow coroutine and
  resolved by the y/n/a key actions; ask_user uses a second Future the same way.
- Clipboard writes go through ``clip.watcher.write_via`` so the self-write hash
  is registered BEFORE the write (self-detection suppression).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Collapsible, Footer, Input
from textual.worker import Worker, get_current_worker

from agentclip.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.clip.watcher import SelfWriteSet, watch, write_via
from agentclip.config import Config, ServicePreset
from agentclip.engine.engine import (
    AskUser,
    ChunkAck,
    Done,
    Engine,
    NewTurn,
    Noise,
    PendingAction,
    ProtocolError,
    Send,
    StatusSnapshot,
    StepResult,
)
from agentclip.engine.states import Decision, EngineStateError
from agentclip.protocol.composer import BudgetExceeded
from agentclip.protocol.parser import looks_like_protocol
from agentclip.protocol.types import Outbound, ParsedReply
from agentclip.tui.messages import ClipboardCaptured
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.new_session import NewSessionScreen
from agentclip.tui.screens.summary import SummaryScreen
from agentclip.tui.screens.text_entry import TextEntryScreen
from agentclip.tui.widgets.action_panel import ActionPanel
from agentclip.tui.widgets.statusbar import StatusBar
from agentclip.tui.widgets.transcript import TranscriptPanel

_T = TypeVar("_T")

_NOISE_TEXT = {
    "duplicate": "duplicate reply ignored",
    "stale-turn": "stale reply ignored (it echoes an older turn)",
    "not-protocol": "clipboard text has no CLIP blocks - ignored",
    "wrong-phase": "reply ignored - not awaiting a reply right now",
}


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


@dataclass
class SessionStats:
    service: str = ""
    replies: int = 0
    calls: Counter[str] = field(default_factory=Counter)
    chars_out: int = 0
    chars_in: int = 0
    summary: str = ""


class MainScreen(Screen[None]):
    BINDINGS = [
        Binding("y", "approve", "approve"),
        Binding("n", "reject", "reject"),
        Binding("a", "auto_edits", "auto-edits"),
        Binding("u", "undo", "undo"),
        Binding("c", "recopy", "re-copy"),
        Binding("i", "force_ingest", "ingest"),
        Binding("w", "toggle_watch", "watcher"),
        Binding("t", "follow_up", "follow-up"),
        Binding("e", "end_session", "summary"),
        Binding("x", "toggle_last", "expand last", show=False),
        Binding("ctrl+s", "submit_answer", "send answer", priority=True, show=False),
        Binding("ctrl+enter", "submit_answer", "send answer", priority=True, show=False),
        Binding("escape", "cancel_entry", "cancel", show=False),
    ]

    pending_approval: reactive[bool] = reactive(False, bindings=True)
    awaiting_answer: reactive[bool] = reactive(False, bindings=True)
    busy: reactive[bool] = reactive(False, bindings=True)
    session_active: reactive[bool] = reactive(False, bindings=True)
    phase_name: reactive[str] = reactive("IDLE", bindings=True)
    watch_paused: reactive[bool] = reactive(False, bindings=True)
    reject_open: reactive[bool] = reactive(False, bindings=True)
    has_outbound: reactive[bool] = reactive(False, bindings=True)

    def __init__(
        self,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[str], Engine],
        project_root: Path,
    ) -> None:
        super().__init__()
        self._config = config
        self._provider = provider
        self._engine_factory = engine_factory
        self._project_root = project_root
        self._self_writes = SelfWriteSet()
        self._engine: Engine | None = None
        self._preset: ServicePreset | None = None
        self._snap: StatusSnapshot | None = None
        self._engine_lock = asyncio.Lock()
        self._gate_future: asyncio.Future[tuple[Decision, str | None]] | None = None
        self._gate_kind: str | None = None
        self._answer_future: asyncio.Future[str] | None = None
        self._watch_worker: Worker[None] | None = None
        self._queued_capture: str | None = None
        self._last_outbound: str | None = None
        self._stats = SessionStats()
        self._turn_glyphs: dict[int, list[str]] = {}  # call id -> [glyph, tool]

    # -- layout ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TranscriptPanel(id="transcript")
        yield ActionPanel(id="action")
        yield StatusBar(id="statusbar")
        yield Footer()

    @property
    def transcript(self) -> TranscriptPanel:
        return self.query_one(TranscriptPanel)

    @property
    def action_panel(self) -> ActionPanel:
        return self.query_one(ActionPanel)

    @property
    def status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    def on_mount(self) -> None:
        self._paint_status()
        self._spawn_flow(self._session_flow())

    # -- dynamic bindings ----------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("approve", "reject"):
            return True if self.pending_approval else None
        if action == "auto_edits":
            return True if (self.pending_approval and self._gate_kind == "edit") else None
        if action in ("undo", "end_session"):
            ok = (
                self.session_active
                and not self.busy
                and self.phase_name in ("AWAITING_REPLY", "DONE")
            )
            return True if ok else None
        if action == "recopy":
            return True if self.has_outbound else None
        if action in ("force_ingest", "follow_up"):
            ok = self.session_active and not self.busy and self.phase_name == "AWAITING_REPLY"
            return True if ok else None
        if action == "toggle_watch":
            if self._provider.name == "manual":
                return False
            return True if self.session_active else None
        if action == "submit_answer":
            return self.awaiting_answer
        if action == "cancel_entry":
            return self.reject_open
        return True

    # -- notifications ----------------------------------------------------------------

    def alert(
        self,
        message: str,
        severity: Literal["information", "warning", "error"] = "information",
    ) -> None:
        """bell + toast, each switchable in config: the user is staring at the browser."""
        if self._config.notify.bell:
            self.app.bell()
        if self._config.notify.toast:
            self.notify(message, severity=severity)

    # -- flow plumbing ------------------------------------------------------------------

    def _spawn_flow(self, coro: Coroutine[Any, Any, None]) -> None:
        self.busy = True
        self.run_worker(self._wrap_flow(coro), group="flow")

    async def _wrap_flow(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except (EngineStateError, BudgetExceeded) as exc:
            if self.is_mounted:
                await self.transcript.add_error(str(exc))
                self.alert(str(exc), severity="error")
        finally:
            self.busy = False
            self.pending_approval = False
            self.awaiting_answer = False
            self.reject_open = False
            if self.is_mounted:
                self.action_panel.hide_panel()
        await self._refresh_status()
        queued, self._queued_capture = self._queued_capture, None
        if queued is not None and self.session_active and self._engine is not None:
            self._spawn_flow(self._ingest_flow(queued))

    async def _engine_call(self, fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        """Serialize every engine call and run it off the event loop."""
        async with self._engine_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # -- session start --------------------------------------------------------------------

    async def _session_flow(self) -> None:
        while True:
            spec = await self.app.push_screen_wait(
                NewSessionScreen(self._config, self._project_root)
            )
            if spec is None:
                self.app.exit()
                return
            engine = await asyncio.to_thread(self._engine_factory, spec.service)
            try:
                out = await self._engine_call(engine.start_task, spec.task)
            except BudgetExceeded as exc:
                self.notify(
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
        await self.transcript.add_user(spec.task)
        await self._copy_outbound(out)
        await self.transcript.add_note(
            f"→ bootstrap copied ({out.total_chars:,} chars) - paste into {self._preset.label}"
        )
        self.session_active = True
        if self._provider.name == "manual":
            self.notify(
                "manual clipboard mode: press i and paste the model's reply into the box; "
                "outbound payloads go out via the terminal's OSC-52 copy",
                severity="warning",
                timeout=10,
            )
        else:
            self._start_watcher()
        await self._refresh_status()
        self.notify(
            f"bootstrap copied ({out.total_chars:,} chars) - paste into {self._preset.label}",
            timeout=8,
        )

    # -- clipboard watcher -------------------------------------------------------------------

    def _start_watcher(self) -> None:
        if self._provider.name == "manual" or self._watch_worker is not None:
            return
        provider = self._provider
        self_writes = self._self_writes
        interval = self._config.clipboard.poll_interval_ms

        def capture(text: str) -> None:
            self.post_message(ClipboardCaptured(text))  # thread-safe bridge to the UI

        def loop() -> None:
            worker = get_current_worker()
            watch(
                provider,
                interval,
                should_stop=lambda: worker.is_cancelled,
                accepts=looks_like_protocol,
                on_capture=capture,
                self_writes=self_writes,
            )

        self._watch_worker = self.run_worker(
            loop, thread=True, group="clipwatch", exit_on_error=False
        )
        self.watch_paused = False

    def on_clipboard_captured(self, message: ClipboardCaptured) -> None:
        message.stop()
        if not self.session_active or self._engine is None:
            return
        if self.busy:
            self._queued_capture = message.text  # depth-1 queue, newest wins
            self.notify("reply received mid-turn - queued (newest wins)", severity="warning")
            return
        self._spawn_flow(self._ingest_flow(message.text))

    # -- ingest -> review -> execute --------------------------------------------------------------

    async def _ingest_flow(self, text: str, *, forced: bool = False) -> None:
        engine = self._engine
        if engine is None:
            return
        result = await self._engine_call(engine.ingest, text)
        if isinstance(result, Noise):
            if forced and result.reason == "not-protocol":
                await self.transcript.add_prose(text[:4000])
                self.notify("no tool calls found - reply shown in transcript; press t to follow up")
            else:
                self.notify(_NOISE_TEXT.get(result.reason, result.reason))
            return
        if isinstance(result, ProtocolError):
            await self.transcript.add_error(
                f"protocol error: {result.detail} - press c to re-copy the last outbound"
            )
            self.alert("protocol error - see transcript", severity="error")
            return
        if isinstance(result, ChunkAck):
            self.notify("chunk ACK received, but chunked sends land in M3", severity="warning")
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
                await self.transcript.add_prose(prose)
        for call in reply.calls:
            self._stats.calls[call.tool] += 1
            await self.transcript.add_call(call)
        if reply.truncated:
            await self.transcript.add_error(
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
                await self.transcript.add_note(
                    f"✗ rejected {action.call.tool} {target}{reason} - remaining calls skipped"
                )
            else:
                self._set_glyph(action.call.id, "✓")
                label = (
                    "approved (auto-accept edits ON)"
                    if decision is Decision.APPROVE_ALL_EDITS
                    else "approved"
                )
                await self.transcript.add_note(f"✓ {label} {action.call.tool} {target}".rstrip())
        self.action_panel.hide_panel()
        await self._refresh_status()  # EXECUTING (status segment driven by busy)
        step = await self._engine_call(engine.execute)
        await self._handle_step(step)

    def _set_glyph(self, call_id: int, glyph: str) -> None:
        if call_id in self._turn_glyphs:
            self._turn_glyphs[call_id][0] = glyph

    def _queue_strip(self) -> str:
        return "  ".join(
            f"{glyph}{cid} {tool}" for cid, (glyph, tool) in sorted(self._turn_glyphs.items())
        )

    async def _gate(self, action: PendingAction, position: str) -> tuple[Decision, str | None]:
        self._gate_kind = action.kind
        self.action_panel.show_approval(action, position, self._queue_strip())
        self.pending_approval = True
        self.alert(f"approval needed: {action.call.tool}", severity="warning")
        self._gate_future = asyncio.get_running_loop().create_future()
        try:
            return await self._gate_future
        finally:
            self._gate_future = None
            self._gate_kind = None
            self.pending_approval = False
            self.reject_open = False

    async def _handle_step(self, step: StepResult) -> None:
        engine = self._engine
        assert engine is not None
        while isinstance(step, AskUser):
            await self.transcript.add_note(f"? {step.question}")
            answer = await self._ask(step.question)
            await self.transcript.add_user(answer)
            step = await self._engine_call(engine.answer_user, answer)
        if isinstance(step, Send):
            await self._copy_outbound(step.outbound)
            await self.transcript.add_outbound(step.outbound, "results copied")
            self.alert(
                f"results copied ({step.outbound.total_chars:,} chars) - paste into the chat"
            )
            return
        assert isinstance(step, Done)
        if step.outbound is not None:
            await self._copy_outbound(step.outbound)
            await self.transcript.add_outbound(step.outbound, "final results copied")
        self._stats.summary = step.summary
        first_line = step.summary.strip().splitlines()[0] if step.summary.strip() else ""
        await self.transcript.add_note(f"✓ task done {('- ' + first_line) if first_line else ''}")
        self.alert("task done", severity="information")
        await self._refresh_status()
        await self._show_summary()

    async def _ask(self, question: str) -> str:
        self.action_panel.show_question(question)
        self.awaiting_answer = True
        self.alert("the model asks you a question", severity="warning")
        self._answer_future = asyncio.get_running_loop().create_future()
        try:
            return await self._answer_future
        finally:
            self._answer_future = None
            self.awaiting_answer = False
            self.action_panel.hide_panel()

    # -- summary / reset --------------------------------------------------------------------------

    async def _show_summary(self) -> None:
        action = await self.app.push_screen_wait(
            SummaryScreen(self._stats_table(), self._stats.summary)
        )
        if action == "undo":
            await self._undo_flow()
        elif action == "new":
            await self._reset_session()

    def _stats_table(self) -> Table:
        table = Table(show_header=False, box=None, padding=(0, 1))
        snap = self._snap
        table.add_row("service", self._stats.service or "-")
        table.add_row("turns", str(snap.turn) if snap else "0")
        table.add_row("replies ingested", str(self._stats.replies))
        calls = ", ".join(f"{tool}×{n}" for tool, n in sorted(self._stats.calls.items()))
        table.add_row("tool calls", calls or "none")
        table.add_row("chars copied out", f"{self._stats.chars_out:,}")
        table.add_row("chars ingested", f"{self._stats.chars_in:,}")
        if snap is not None:
            table.add_row("session dir", str(snap.session_dir))
        return table

    async def _reset_session(self) -> None:
        self.session_active = False
        self._engine = None
        self._preset = None
        self._snap = None
        self._last_outbound = None
        self.has_outbound = False
        self.phase_name = "IDLE"
        self._queued_capture = None
        self._stats = SessionStats()
        await self.transcript.clear_events()
        self._paint_status()
        await self._session_flow()

    # -- undo / follow-up / manual ingest -------------------------------------------------------------

    async def _undo_flow(self) -> None:
        engine = self._engine
        if engine is None:
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(
                "Undo the most recent turn?",
                "Files changed by that turn are restored from the per-turn backup. "
                "run_command side effects are NOT undone. A revert notice for the "
                "model will be composed and copied.",
            )
        )
        if not confirmed:
            return
        try:
            report, notice = await self._engine_call(engine.undo_last_turn, compose_notice=True)
        except EngineStateError as exc:
            self.notify(str(exc), severity="warning")
            return
        parts = []
        if report.restored:
            parts.append(f"{len(report.restored)} restored")
        if report.deleted:
            parts.append(f"{len(report.deleted)} deleted")
        if report.recreated:
            parts.append(f"{len(report.recreated)} recreated")
        await self.transcript.add_note(
            f"↩ undid turn {report.turn} ({', '.join(parts) or 'nothing to restore'})"
        )
        for warning in report.warnings:
            self.notify(warning, severity="warning")
        if notice is not None:
            await self._copy_outbound(notice)
            await self.transcript.add_note(
                f"→ revert notice copied ({notice.total_chars:,} chars) - paste it into the chat"
            )

    async def _follow_up_flow(self) -> None:
        engine = self._engine
        if engine is None:
            return
        text = await self.app.push_screen_wait(
            TextEntryScreen(
                "Follow-up message to the model", "ctrl+s (or ctrl+enter) send · escape cancel"
            )
        )
        if not text:
            return
        out = await self._engine_call(engine.follow_up, text)
        await self.transcript.add_user(text)
        await self._copy_outbound(out)
        self.notify(f"follow-up copied ({out.total_chars:,} chars) - paste into the chat")

    async def _force_ingest_flow(self) -> None:
        text = await asyncio.to_thread(self._provider.read_text)
        if not text:
            text = await self.app.push_screen_wait(
                TextEntryScreen(
                    "Paste the model's reply",
                    "the clipboard had no text - paste the reply here; ctrl+s ingests",
                )
            )
            if not text:
                return
        await self._ingest_flow(text, forced=True)

    # -- outbound copies -----------------------------------------------------------------------------

    async def _copy_outbound(self, outbound: Outbound) -> None:
        if len(outbound.chunks) > 1:  # cannot happen with the M1 composer
            self.notify(
                "multi-part outbound - only part 1 copied (chunk walk lands in M3)",
                severity="warning",
            )
        text = outbound.chunks[0]
        await self._copy_text(text)
        self._last_outbound = text
        self.has_outbound = True
        self._stats.chars_out += outbound.total_chars

    async def _copy_text(self, text: str) -> None:
        try:
            await asyncio.to_thread(write_via, self._provider, self._self_writes, text)
        except ClipboardUnavailable:
            self.app.copy_to_clipboard(text)  # OSC-52, write-only
            self.notify(
                "no clipboard backend - sent via the terminal's OSC-52 escape; if pasting "
                "fails, copy from .agentclip/sessions/<id>/outbound/",
                severity="warning",
            )

    # -- key actions ------------------------------------------------------------------------------------

    def action_approve(self) -> None:
        self._resolve_gate(Decision.APPROVE, None)

    def action_auto_edits(self) -> None:
        if self._gate_kind == "edit":
            self._resolve_gate(Decision.APPROVE_ALL_EDITS, None)

    def action_reject(self) -> None:
        if self._gate_future is None:
            return
        self.reject_open = True
        self.action_panel.open_reject_input()

    def _resolve_gate(self, decision: Decision, note: str | None) -> None:
        future = self._gate_future
        if future is not None and not future.done():
            future.set_result((decision, note))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "reject-reason":
            return
        event.stop()
        self.reject_open = False
        self.action_panel.close_reject_input()
        self._resolve_gate(Decision.REJECT, event.value.strip() or None)

    def action_cancel_entry(self) -> None:
        if self.reject_open:
            self.reject_open = False
            self.action_panel.close_reject_input()

    def action_submit_answer(self) -> None:
        future = self._answer_future
        if future is not None and not future.done():
            future.set_result(self.action_panel.answer_text())

    def action_undo(self) -> None:
        if self.busy or not self.session_active:
            return
        self._spawn_flow(self._undo_flow())

    def action_recopy(self) -> None:
        text = self._last_outbound
        if text is None:
            return
        self.run_worker(self._recopy(text), group="recopy")

    async def _recopy(self, text: str) -> None:
        await self._copy_text(text)
        self.notify(f"re-copied the last outbound ({len(text):,} chars)")

    def action_force_ingest(self) -> None:
        if self.busy or not self.session_active:
            return
        self._spawn_flow(self._force_ingest_flow())

    def action_toggle_watch(self) -> None:
        if self._provider.name == "manual" or not self.session_active:
            return
        if self._watch_worker is not None:
            self._watch_worker.cancel()
            self._watch_worker = None
            self.watch_paused = True
            self.notify("clipboard watcher paused - w resumes, i ingests manually")
        else:
            self._start_watcher()
            self.notify("clipboard watcher resumed")

    def action_follow_up(self) -> None:
        if self.busy or not self.session_active:
            return
        self._spawn_flow(self._follow_up_flow())

    def action_end_session(self) -> None:
        if self.busy or not self.session_active:
            return
        self._spawn_flow(self._show_summary())

    def action_toggle_last(self) -> None:
        try:
            last = self.transcript.query(Collapsible).last()
        except NoMatches:
            return
        last.collapsed = not last.collapsed

    # -- status bar ----------------------------------------------------------------------------------------

    async def _refresh_status(self) -> None:
        engine = self._engine
        if engine is not None:
            self._snap = await self._engine_call(engine.status)
            self.phase_name = self._snap.phase.name
        self._paint_status()

    def watch_pending_approval(self) -> None:
        self._paint_status()

    def watch_awaiting_answer(self) -> None:
        self._paint_status()

    def watch_busy(self) -> None:
        self._paint_status()

    def watch_watch_paused(self) -> None:
        self._paint_status()

    def watch_session_active(self) -> None:
        self._paint_status()

    def watch_phase_name(self) -> None:
        self._paint_status()

    def _watch_segment(self) -> tuple[str, str]:
        if self.phase_name == "DONE":
            return "✓ DONE", "st-done"
        if self.pending_approval:
            return "◍ APPROVAL?", "st-attn"
        if self.awaiting_answer:
            return "◍ ASK USER", "st-attn"
        if self.busy:
            return "◍ EXECUTING", "st-busy"
        if self._provider.name == "manual":
            return "✗ MANUAL", "st-err"
        if self.watch_paused:
            return "○ PAUSED", "st-dim"
        if self.session_active and self.phase_name == "AWAITING_REPLY":
            return "● ARMED", "st-armed"
        return "○ IDLE", "st-dim"

    def _paint_status(self) -> None:
        if not self.is_mounted:
            return
        try:
            bar = self.status_bar
        except NoMatches:
            return
        watch_text, watch_class = self._watch_segment()
        preset = self._preset
        snap = self._snap
        service = f"{preset.key} {_fmt_k(preset.max_paste_chars)}" if preset else "no session"
        out = (
            f"out {_fmt_k(snap.last_outbound_chars)}/{_fmt_k(snap.budget_chars)} (1/1)"
            if snap
            else "out -"
        )
        turn = f"turn {snap.turn}" if snap else "turn -"
        edits = "EDITS:auto" if snap and snap.auto_accept_edits else "EDITS:ask"
        try:
            root = str(Path("~") / self._project_root.relative_to(Path.home()))
        except ValueError:
            root = str(self._project_root)
        bar.update_segments(
            watch=watch_text,
            watch_class=watch_class,
            service=service,
            out=out,
            turn=turn,
            edits=edits,
            root=root,
        )
