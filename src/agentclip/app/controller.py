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

Delegation (the ``delegate`` tool) is a *nested session*, not a second one
running alongside: when the master engine parks in ``AWAITING_SUBAGENT`` the
controller pushes the master's whole session context onto a local, swaps in a
freshly built sub-agent Engine, runs the ordinary ingest -> review -> execute
loop against it until it emits ``task_done``, and then restores the master and
feeds the sub-agent's ``result`` back as the ``delegate`` call's result body.
The master is blocked inside its own flow coroutine for the entire sub-run, so
**at most one session is live at any instant** - which is precisely what lets
the single clipboard watcher, the single approval gate, the single ask_user
future and the single focused transcript be *retargeted* instead of duplicated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from agentclip.app.commands import command_list, help_text, lookup
from agentclip.app.types import EngineRequest, SessionRef, SessionStats
from agentclip.app.view import ChatView, SessionView
from agentclip.config import Config, ServicePreset
from agentclip.engine.engine import (
    AskUser,
    ChunkAck,
    Decision,
    Delegate,
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
from agentclip.protocol.parser import peek_chat_name
from agentclip.protocol.types import Outbound, ParsedReply, ResultStatus

_T = TypeVar("_T")

# What a delegation hands back to the master when it could not run. Every one of
# these is delivered as an `error` ToolResult on the `delegate` call, never as a
# silent drop: the model made a call and must always learn what became of it.
_DELEGATION_HINT = "hint: do this part of the work yourself in this conversation."

_NEW_CHAT_FAILED_BODY = (
    "delegation failed: AgentClip could not open a fresh chat for the sub-agent. "
    "The new-chat button did not verify against its calibration, so nothing was "
    "clicked and nothing was pasted - no sub-agent ran.\n" + _DELEGATION_HINT
)

_ABORTED_BODY = (
    "the user aborted the sub-agent run before it produced a result, so nothing "
    "was handed back.\n"
    "hint: ask the user what they want instead, or do the work yourself here."
)

# Unreachable by construction (a sub-agent's registry has no `delegate`, so the
# call pre-resolves as unknown_tool); kept as the belt to that braces.
_NESTED_DELEGATION_BODY = (
    "a sub-agent cannot delegate further - nesting is not supported.\n"
    "hint: do this part of the task yourself."
)

def _gone_service_body(key: str) -> str:
    return (
        f"delegation is unavailable: the sub-agent chat is set to the service preset "
        f"{key!r}, which no longer exists in this AgentClip's configuration. No "
        "sub-agent was started.\n"
        f"{_DELEGATION_HINT} Do not retry delegate."
    )


_EMPTY_RESULT_BODY = (
    "the sub-agent finished without stating a result. Treat the sub-task as "
    "unverified and check anything you depended on it for."
)

# Annotations left on a sub-agent's transcript tab when its run ends - the tab
# stays mounted and readable afterwards. Two of them, because a run that never
# produced a deliverable (a refused chat, a bootstrap over budget, an abort, a
# crash) reaches the same `finally` as one that did, and the success wording was
# then printed directly under the error explaining why nothing ran.
_FINISHED_NOTE = "sub-agent run ended - the result above was handed back to the delegating agent"
_FAILED_NOTE = (
    "sub-agent run ended WITHOUT a result - the failure above was reported to the "
    "delegating agent instead"
)

_ABORT_NOTE = "the user aborted the sub-agent run"

# Noise reason -> toast. "{chat}" is filled with the session's chat name.
_NOISE_TEXT = {
    "duplicate": "duplicate reply ignored",
    "not-protocol": "clipboard text has no CLIP blocks - ignored",
    "wrong-phase": "reply ignored - not awaiting a reply right now",
    "missing-chat": "ignored a paste without this chat's name ({chat}) - not from this chat?",
    "wrong-chat": "ignored a paste naming a different chat - this session is {chat}",
}


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


def _parse_onoff(arg: str, *, current: bool) -> bool | None:
    """Parse an on/off command argument (`/yolo`, `/armed`): empty toggles
    against ``current``, on/off variants set explicitly, anything else is
    unrecognized (None). ``current`` is consulted for the empty case ONLY, so a
    caller that handles the bare form itself may pass anything."""
    if not arg:
        return not current
    low = arg.strip().lower()
    if low in ("on", "true", "1", "yes", "y", "enable"):
        return True
    if low in ("off", "false", "0", "no", "n", "disable"):
        return False
    return None


def _short_title(task: str) -> str:
    """A tab-sized label for one delegated task (first line, squeezed)."""
    line = " ".join(task.split())
    if not line:
        return "sub-agent"
    return line if len(line) <= 32 else line[:31].rstrip() + "…"


def _compose_sub_task(req: Delegate) -> str:
    """The delegated task as the sub-agent receives it: the `task` param, plus
    the optional `context` param under the heading protocol.md section 2.1
    promises the sub-agent it will find it under."""
    if not req.context:
        return req.task
    return f"{req.task}\n\nContext from the delegating agent:\n{req.context}"


def _unavailable_body(missing: tuple[str, ...]) -> str:
    gaps = ", ".join(missing) if missing else "the sub-agent chat window"
    return (
        "delegation is unavailable: the sub-agent chat window is not calibrated "
        f"in AgentClip (missing: {gaps}). No sub-agent was started.\n"
        f"{_DELEGATION_HINT} Do not retry delegate."
    )


def _budget_body(exc: BudgetExceeded) -> str:
    return (
        f"the sub-task did not fit in one paste: its bootstrap needs "
        f"{exc.needed_chars:,} chars but the service allows {exc.budget_chars:,}. "
        "No sub-agent ran.\n"
        "hint: split it into smaller delegations, or do the work yourself."
    )


class _SubagentAborted(Exception):
    """The user ended a sub-agent run with /abort.

    Raised into whichever await the sub-run is parked on (the reply future, the
    ask_user future) so the run unwinds through ``_run_subagent``'s ``finally``
    like any other failure - one restore path, not two.
    """


@dataclass(slots=True)
class _SessionContext:
    """Everything ``SessionController`` holds *per session*, saved whole.

    Nearly every method on the controller reads ``self._engine`` /
    ``self._stats`` / ``self._snap``; a delegation swaps all of it for the
    sub-agent's and swaps it back afterwards, so those methods work on the
    sub-agent unchanged. Save-and-restore beats threading a session parameter
    through thirty call sites, and it is exactly right because delegation is
    single-flight: there is never a second live session to confuse it with.
    """

    ref: SessionRef
    engine: Engine
    chat_name: str | None
    preset: ServicePreset | None
    snap: StatusSnapshot | None
    stats: SessionStats
    turn_glyphs: dict[int, list[str]]
    last_outbound: str | None
    has_outbound: bool
    yolo: bool


class SessionController:
    """Synchronous-at-heart session driver; UI-agnostic via the ChatView port."""

    def __init__(
        self,
        config: Config,
        engine_factory: Callable[[EngineRequest], Engine],
        project_root: Path,
        *,
        view: ChatView,
    ) -> None:
        self._config = config
        self._engine_factory = engine_factory
        self._project_root = project_root
        self._view = view

        self._engine: Engine | None = None
        self._chat_name: str | None = None  # this session's agreed chat name
        self._preset: ServicePreset | None = None
        self._snap: StatusSnapshot | None = None
        self._engine_lock = asyncio.Lock()
        self._gate_future: asyncio.Future[tuple[Decision, str | None]] | None = None
        self._answer_future: asyncio.Future[str] | None = None
        self._queued_capture: str | None = None
        self._last_outbound: str | None = None
        self._stats = SessionStats()
        self._turn_glyphs: dict[int, list[str]] = {}  # call id -> [glyph, tool]

        # -- delegation ------------------------------------------------------
        # ``_active`` is whose session the fields above currently describe: the
        # master normally, a sub-agent for the length of a delegation.
        # ``_sub`` is non-None EXACTLY while a sub-run is in flight and is the
        # switch every routing decision reads (clipboard, /abort, labels).
        self._active: SessionRef | None = None
        self._sub: SessionRef | None = None
        # The service every sub-agent of THIS session runs on, taken from
        # SessionSpec at bootstrap (see _arm_session). Empty between sessions.
        self._subagent_service = ""
        self._sub_index = 0  # numbers the sub-1, sub-2, ... transcript tabs
        # Where a parked sub-run waits for its chat's next reply. The master
        # never uses it: its replies arrive as flows, not as awaited values.
        self._reply_future: asyncio.Future[str] | None = None
        # Latched by /abort so an abort that lands while the sub-agent is
        # executing (nothing to resolve yet) still ends the run at the next park.
        self._sub_aborting = False

        # state flags mirrored to the view via SessionView
        self._session_active = False
        self._busy = False
        # True only while execute()/answer_user() is actually running tool calls
        # on the worker thread - the window in which cancel_execution() bites.
        self._executing = False
        self._pending_approval = False
        self._awaiting_answer = False
        self._has_outbound = False
        self._yolo = config.approval.yolo  # auto-approve everything; /yolo toggles it

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Kick off the session: prompt for a task, then run the loop."""
        self._spawn_flow(self._session_flow())

    def update_config(self, config: Config) -> None:
        """Swap in a freshly-edited Config (service editor save).

        Only affects things read from ``self._config`` going forward - notably
        the preset label/services table used when the *next* session starts and
        the YOLO default a reset session falls back to. It never touches a
        session already in flight (its Engine was built from its own Config
        snapshot at start time)."""
        self._config = config

    # -- view-facing events ---------------------------------------------------

    def submit_clipboard(self, text: str, *, accept_prose: bool = False) -> None:
        """A captured (or injected) clipboard reply, routed to the session that
        claimed it. Queued if that session's turn is busy.

        Routing is by chat name (``peek_chat_name`` - a cheap scan of the last
        sentinel line, no parse), and it runs *before* the busy check on
        purpose: while a sub-agent runs, the master's flow is busy for the whole
        delegation, so a sub-agent reply reaching the depth-1 queue would be
        swallowed and never seen. An unnamed paste (an ACK, or a model that
        dropped the attribute) falls through to whichever session is live - the
        engine's own chat gate is the backstop.

        ``accept_prose`` marks text the caller KNOWS is the model's reply (the
        auto-copy flow's verified click put it there - ``capture_prose``): if
        the engine judges it not-protocol, it is shown in the transcript as
        prose instead of being dropped with a toast, the same treatment a
        forced ingest (the `i` key) has always given it. Master path only; a
        sub-agent's reply must carry CLIP blocks to mean anything to the run
        that is waiting on it.
        """
        if not self._session_active or self._engine is None:
            return
        name = peek_chat_name(text)
        sub = self._sub
        if sub is not None:
            self._route_to_subagent(text, name, sub)
            return
        active = self._active
        if name is not None and active is not None and name != active.chat_name:
            self._view.notify(
                f"ignored a paste naming the {name} chat - this session is {active.chat_name}",
                severity="warning",
            )
            return
        if self._busy:
            self._queued_capture = text  # depth-1 queue, newest wins
            self._view.notify("reply received mid-turn - queued (newest wins)", severity="warning")
            return
        # ``forced`` is exactly the accept-prose behaviour: a not-protocol
        # verdict shows the text as prose instead of dropping it.
        self._spawn_flow(self._ingest_flow(text, forced=accept_prose))

    def _route_to_subagent(self, text: str, name: str | None, sub: SessionRef) -> None:
        """Hand a paste to the parked sub-run, or explain why it was dropped.

        Nothing is ever queued here: the master composes its next payload fresh
        once the delegation returns, so a master-chat reply arriving mid-sub-run
        is stale by definition."""
        if name is not None and name != sub.chat_name:
            self._view.notify(
                f"that reply is from the {name} chat - the sub-agent run ({sub.chat_name}) "
                "is still waiting; /abort ends it",
                severity="warning",
            )
            return
        future = self._reply_future
        if future is None or future.done():
            self._view.notify(
                "the sub-agent is still working - that paste was ignored", severity="warning"
            )
            return
        future.set_result(text)

    def submit_message(self, text: str) -> None:
        """Composer send: an ask_user answer, a slash command, or a follow-up."""
        text = text.strip()
        if not text:
            return
        # An ask_user answer ALWAYS wins. While the flow is parked on the answer
        # future the composer's text IS the answer, verbatim - so a legitimate
        # answer like "/etc/hosts" or "/no" is delivered, never eaten as a command.
        if self._awaiting_answer:
            future = self._answer_future
            if future is not None and not future.done():
                self._view.reset_composer()
                future.set_result(text)
            else:  # a previous send already resolved it (sub-frame double-tap)
                self._view.notify("answer already sent - please wait", severity="warning")
            return
        if text.startswith("/"):
            self._handle_command(text)
            return
        self._send_follow_up(text)

    def _send_follow_up(self, text: str) -> None:
        if self._session_active and not self._busy and self._can_follow_up():
            self._view.reset_composer()
            self._spawn_flow(self._follow_up_flow(text))
            return
        self._view.notify(
            "can't send right now - wait for the current step to finish", severity="warning"
        )

    # -- chat slash commands --------------------------------------------------
    # Parsed here (not in the view) so any front-end that forwards composer text
    # gets the commands for free - the controller owns session lifecycle and the
    # engine. Parsing is plain string work: no Textual/clip import (layering OK).
    # Only reached when NOT answering a question (submit_message gates that first),
    # so a slash-leading answer is never mistaken for a command.
    #
    # WHICH commands exist is not decided here: agentclip.app.commands holds the
    # one table, and the help note, the "unknown command" hint and the composer's
    # autocomplete popup all render from it. This module only says what each one
    # DOES, and _command_handlers is the join - one entry per registry name.

    def _command_handlers(self) -> dict[str, Callable[[str], None]]:
        """What each registered command runs, keyed by its canonical name.

        Every :data:`~agentclip.app.commands.COMMANDS` entry must appear here and
        nothing else may (a test pins the two sets together), so a command added
        to the registry cannot ship as a dead menu row. The uniform ``(arg)``
        signature is what lets dispatch stay a dict lookup; only the two on/off
        commands - `/yolo` and `/armed` - read it.
        """
        return {
            "yolo": self._cmd_yolo,
            "new": lambda _arg: self._cmd_new(),
            "abort": lambda _arg: self._cmd_abort(),
            "help": lambda _arg: self._cmd_help(),
            "identify": lambda _arg: self._cmd_identify(),
            "log": lambda _arg: self._cmd_log(),
            "armed": self._cmd_armed,
        }

    def _handle_command(self, raw: str) -> None:
        """Dispatch a leading-slash composer line. `//text` is an escape hatch that
        sends a follow-up message beginning with a literal slash. The box is cleared
        first so a typo or command never lingers; unknown commands are reported, not
        sent."""
        self._view.reset_composer()
        if raw.startswith("//"):
            self._send_follow_up(raw[1:])  # literal-slash message escape
            return
        name, _, arg = raw[1:].partition(" ")
        name = name.strip().lower()
        arg = arg.strip()
        command = lookup(name)  # aliases resolve here: /commands and /? are /help
        handler = self._command_handlers().get(command.name) if command is not None else None
        if handler is None:
            shown = f"/{name}" if name else "/"
            self._view.notify(
                f"unknown command: {shown} - try {command_list()}",
                severity="warning",
            )
            return
        handler(arg)

    def _cmd_yolo(self, arg: str) -> None:
        """Toggle (or set on/off) YOLO auto-approve-everything. Only reachable while
        armed/idle (an ask_user answer wins over commands), so the toggle itself runs
        off-loop via _engine_call - set_yolo writes one session audit line."""
        if not self._session_active or self._engine is None:
            self._view.notify("start a session before using /yolo", severity="warning")
            return
        target = _parse_onoff(arg, current=self._yolo)
        if target is None:
            self._view.notify("usage: /yolo [on|off] - bare /yolo toggles", severity="warning")
            return
        self._view.spawn(self._apply_yolo(target))

    async def _apply_yolo(self, target: bool) -> None:
        # Spawned off the flow machinery (not _wrap_flow), so it owns its error
        # handling. set_yolo flips the policy flag THEN writes an audit line; if
        # that write fails we resync the mirror from the engine's real state
        # (re-read by _refresh_status) so a later bare /yolo toggles the right way.
        engine = self._engine
        if engine is None:
            return
        try:
            await self._engine_call(engine.set_yolo, target)  # flips policy + audits, off-loop
        except Exception as exc:  # keep the mirror honest, then surface it
            await self._refresh_status()
            if self._snap is not None:
                self._yolo = self._snap.yolo
            await self._view.add_error(f"could not record the YOLO toggle: {exc}")
            self._view.alert("YOLO toggle failed - see transcript", severity="error")
            return
        self._yolo = target
        await self._refresh_status()  # repaint the status bar (YOLO badge)
        if target:
            await self._view.add_note(
                "YOLO mode ON - every tool call (edits AND commands) auto-approves, "
                "bypassing the allowlist and deny tokens. /yolo off restores the gates."
            )
            self._view.alert("YOLO mode ON - approvals are off", severity="warning")
            self._view.notify("YOLO mode ON - every tool call auto-approves", severity="warning")
        else:
            await self._view.add_note(
                "YOLO mode OFF - edits and non-allowlisted commands gate again."
            )
            self._view.alert("YOLO mode OFF", severity="information")
            self._view.notify("YOLO mode OFF - approvals restored", severity="information")

    def _cmd_new(self) -> None:
        """Open a fresh BROWSER chat now, and start a fresh session behind it.

        /new is the one command that asks the view to touch the browser (tui.md
        section 3.3a): the user wants a new conversation, and the one on screen
        is the old one's. The whole flow is the view's - it clicks the new-chat
        control immediately and calls ``request_new_session`` back into this
        controller, which is the *same* path the sidebar's "New browser chat"
        button takes. So the reset is not started here even though it always
        happens: only the view knows whether the click landed, and only the view
        can tell the user that the fresh chat is now theirs to open.

        Only here. The launch, the budget-exceeded retry and the summary
        screen's *new session* reach ``_reset_session`` too, and none of them
        means "the chat in the browser is stale" (the first has none, the last
        has just been read by the user).
        """
        if not self._new_session_allowed():
            return  # refused before the browser is touched, exactly like the button
        self._view.open_new_chat_now()

    def request_new_session(self) -> bool:
        """Start a fresh session once a fresh browser chat is already open.

        The tool-side half of /new, public because the browser side runs in the
        view: both the sidebar's "New browser chat" on the master tab and /new
        itself land here *after* the click has been attempted (tui.md sections
        1.3 and 3.3a), to reset the conversation to match the chat that replaced
        it - or the one the user is about to open by hand. Returns whether the
        reset started, and toasts the refusal when it did not.
        """
        if not self._new_session_allowed():
            return False
        self._spawn_flow(self._reset_session())
        return True

    def _new_session_allowed(self) -> bool:
        """The two refusals a session reset can give, toasted where they happen."""
        if not self._session_active:
            self._view.notify("no active session to replace", severity="warning")
            return False
        if self._busy:
            self._view.notify(
                "can't start a new session mid-turn - answer or finish the current step first",
                severity="warning",
            )
            return False
        return True

    def _cmd_abort(self) -> None:
        """End the sub-agent run in flight (protocol.md's delegate failure path).

        Deliberately the ONLY thing that kills a whole delegation - ctrl+x is the
        narrower tool (it cancels the tool calls running right now, in whichever
        chat is live, and the turn still finishes and reports). The two are
        reachable at the same time and mean different things.

        There is no single await to interrupt, because a sub-run parks in three
        different places, so this resolves whichever one is actually up and
        latches ``_sub_aborting`` for the rest:

        * waiting for the sub-agent's next reply -> the reply future raises;
        * at an approval gate -> the gate is rejected, which aborts the sub-
          agent's turn; the abort then lands at the next reply park;
        * executing tool calls -> ``request_cancel`` on the SUB-AGENT's engine
          (``self._engine`` is the sub's for the length of the run) so the
          worker thread unblocks; that turn ends normally, and the latched flag
          ends the run when it comes back for a reply.

        A sub-agent's ask_user is NOT abortable this way, by design: while the
        composer is in answer mode its text is the answer, verbatim (the
        standing invariant), so "/abort" typed there is an answer like any other.
        """
        if self._sub is None:
            self._view.notify("no sub-agent run to abort", severity="warning")
            return
        self._sub_aborting = True
        self._view.notify("aborting the sub-agent run...", severity="warning")
        future = self._reply_future
        if future is not None and not future.done():
            future.set_exception(_SubagentAborted(_ABORT_NOTE))
            return
        gate = self._gate_future
        if gate is not None and not gate.done():
            gate.set_result((Decision.REJECT, _ABORT_NOTE))
            return
        engine = self._engine
        if self._executing and engine is not None:
            engine.request_cancel()

    def _cmd_help(self) -> None:
        self._view.spawn(self._view.add_note(help_text()))

    def _cmd_identify(self) -> None:
        """Show the user what the tool believes it can see in the chat window.

        The only command with NO session gate, deliberately: it is a calibration
        aid, and the moments it is most needed - a paste that landed in the wrong
        box, a copy that never fired - are exactly the moments the session is
        wedged or over. Nothing here can affect a run either; it captures one
        frame and draws on top of it.

        Which is also why the controller has no more to say about it than
        "please do": what a chat region is, which window is live and what a copy
        button looks like all live in the screen layer, on the far side of the
        ChatView port.
        """
        self._view.show_identify_overlay()

    def _cmd_log(self) -> None:
        """Show (or put away) the harness decision log - every loop move, with
        its reason.

        `/identify` answers "what can you see?"; this answers "why did you do
        that?", and the two share a rationale for having NO session gate: the
        moment a user wants either one is the moment the automation has stopped
        making sense to them, which is routinely after a run has wedged or
        ended. A log you can only read from a healthy session is a log you
        cannot read when it matters.

        And, like `/identify`, the controller has nothing to add: every decision
        in it was taken on the view's side of the port - the paste attempt, the
        send gate, the finish detectors, the auto-copy flow - so it says "show
        it" and stops. It is a toggle on the far side (a pane, not a modal), so
        typing it twice puts it away; the controller does not track which.
        """
        self._view.toggle_harness_log()

    def _cmd_armed(self, arg: str) -> None:
        """Arm or disarm the whole OS-acting half of the tool (bare = toggle).

        NO session gate, for `/identify`'s reason and more sharply: the moments
        a user reaches for this are the moments something is going wrong on
        their screen - a paste landing in the wrong window, a click flow that
        will not stop - and a switch that could only be reached from a healthy
        session would be missing exactly when it is wanted. Nothing here can
        affect a run either; the flag decides what the VIEW may do to the
        browser, and every read-only half keeps running.

        No engine involvement either, which is what makes it different from
        `/yolo`: YOLO is a policy of one session (it is audited into that
        session's log and dies with it), while this is a property of the
        machine in front of the user and outlives every session on it. So the
        controller neither stores it nor mirrors it - it forwards the intent to
        the one layer that owns the mouse and has nothing else to say.
        """
        if not arg:
            self._view.set_os_armed(None)  # bare /armed toggles, exactly like F5
            return
        # ``current`` is never read for a non-empty argument (see _parse_onoff),
        # and the bare-toggle case has already returned: the flag lives in the
        # view, so there is no current state here to toggle against.
        target = _parse_onoff(arg, current=False)
        if target is None:
            self._view.notify("usage: /armed [on|off] - bare /armed toggles", severity="warning")
            return
        self._view.set_os_armed(target)

    def submit_decision(self, decision: Decision, note: str | None) -> None:
        """Resolve the approval gate (from a key action or panel button)."""
        future = self._gate_future
        if future is not None and not future.done():
            future.set_result((decision, note))

    @property
    def executing(self) -> bool:
        """True while the engine is running this turn's tool calls (the spinner
        is up). The view reads it to enable/disable the cancel affordance."""
        return self._executing

    def cancel_execution(self) -> None:
        """Cancel the tool calls running right now (a too-slow command, usually).

        Deliberately does NOT go through ``_engine_call``: the engine lock is
        held by the very ``execute()`` we are interrupting, and
        ``request_cancel`` is thread-safe by design (it sets an Event) precisely
        so it can be called from here, on the event loop, mid-execute.

        The turn is not aborted: the engine finishes it normally and the results
        - the interrupted call plus the skipped ones - flow through the usual
        Send path, so the model is told what happened without the user doing
        anything else. A no-op when nothing is executing."""
        engine = self._engine
        if engine is None or not self._executing:
            return
        engine.request_cancel()
        self._view.notify(
            "cancelling - the killed call and the skipped ones are sent to the model",
            severity="warning",
        )

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

    async def _run_engine_step(
        self, fn: Callable[..., _T], /, *args: object, **kwargs: object
    ) -> _T:
        """Run execute()/answer_user() with the 'working' spinner showing meanwhile.

        The window this brackets is exactly the window in which cancelling means
        something (``_executing``): the engine is chewing through tool calls on
        the worker thread and the user is watching the spinner."""
        n = len(self._turn_glyphs)
        label = f"Working - running {n} tool call{'' if n == 1 else 's'}..." if n else "Working..."
        self._view.start_working(label)
        self._executing = True
        try:
            return await self._engine_call(fn, *args, **kwargs)
        finally:
            self._executing = False
            self._view.stop_working()

    # -- session start --------------------------------------------------------

    async def _session_flow(self) -> None:
        while True:
            spec = await self._view.prompt_new_session()
            if spec is None:
                self._view.exit_app()
                return
            # The catalog is fixed at bootstrap, so whether the model is offered
            # `delegate` at all is decided HERE, once, from the sub-agent chat's
            # calibration. Calibrating it later notifies the user to /new - we
            # cannot retro-fit a tool into a conversation the model already read.
            delegation = self._view.delegation_available()
            engine = await asyncio.to_thread(
                self._engine_factory,
                EngineRequest(service=spec.service, allow_delegate=delegation),
            )
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
        # Immutable for the session, so it is read straight off the engine (no
        # _engine_call round trip needed) and mirrored for the noise toasts.
        self._chat_name = engine.chat_name
        self._active = SessionRef(
            id="master",
            role="master",
            title=_short_title(spec.task),
            chat_name=engine.chat_name,
        )
        self._preset = self._config.services.get(spec.service, self._config.preset())
        # Frozen here for the same reason the master's preset is: the sub-agent
        # window's picker is locked for the whole session, so a delegation
        # started at turn 30 must build its Engine from the service the user had
        # chosen when the session armed - not from whatever the view says now.
        self._subagent_service = spec.subagent_service or spec.service
        self._stats = SessionStats(service=spec.service)
        await self._view.add_user(spec.task)
        await self._copy_outbound(out)
        await self._view.add_note(
            f"chat name: {engine.chat_name} - the model echoes chat={engine.chat_name} on "
            "every reply; pastes without it are ignored"
        )
        if delegation:
            await self._view.add_note(
                "delegate tool enabled - the model may hand a bounded sub-task to a "
                "sub-agent in the calibrated second chat; /abort ends a run in flight"
            )
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
                self._view.notify(self._noise_text(result.reason))
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

    def _noise_text(self, reason: str) -> str:
        """Toast for one ingest rejection; unknown reasons pass through raw."""
        return _NOISE_TEXT.get(reason, reason).format(chat=self._chat_name or "?")

    async def _run_turn(self, reply: ParsedReply) -> None:
        await self._handle_step(await self._run_turn_body(reply))

    async def _run_turn_body(self, reply: ParsedReply) -> StepResult:
        """Everything one ingested reply does up to (and including) ``execute()``:
        show it, gate each pending action, run the calls - and *return* the step.

        Split from ``_run_turn`` because a sub-agent's turn needs exactly this
        much and then does something different with the result (it loops on the
        sub-agent's own chat instead of handing the step to the master's
        ``_handle_step``). No behaviour of its own: the master path is still
        body-then-handle, in that order.
        """
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
        return await self._run_engine_step(engine.execute)

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
        self._view.alert(
            f"{self._alert_prefix}approval needed: {action.call.tool}", severity="warning"
        )
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
        # Both parking steps resume the SAME turn, so they loop together: a
        # reply may ask a question, then delegate, then ask again. `engine` stays
        # valid across a delegation because _run_subagent restores this session's
        # context before it returns.
        while isinstance(step, (AskUser, Delegate)):
            if isinstance(step, AskUser):
                await self._view.add_note(f"? {step.question}")
                answer = await self._ask(step.question)
                await self._view.add_user(answer)
                step = await self._run_engine_step(engine.answer_user, answer)
                continue
            text, status, code = await self._run_subagent(step)
            await self._view.add_note(
                f"← sub-agent result ({len(text):,} chars, {status}) - handed back to the model"
            )
            step = await self._run_engine_step(
                engine.deliver_delegate_result, text, status=status, code=code
            )
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
        self._view.alert(
            f"{self._alert_prefix}the model asks you a question - type your answer below",
            severity="warning",
        )
        self._answer_future = asyncio.get_running_loop().create_future()
        try:
            return await self._answer_future
        finally:
            self._answer_future = None
            self._awaiting_answer = False
            self._push_state()

    @property
    def _alert_prefix(self) -> str:
        """Bells and toasts pull the user back from the browser, so they must say
        WHO wants them - the conversation they started, or a sub-agent of it."""
        active = self._active
        return "sub-agent: " if active is not None and active.role == "subagent" else ""

    # -- delegation ------------------------------------------------------------

    async def _run_subagent(self, req: Delegate) -> tuple[str, ResultStatus, str | None]:
        """Run one delegated sub-task to completion and return its result body.

        Always returns - never raises - because the caller is mid-turn on the
        master and every outcome, including a crash, has to reach the model as
        the `delegate` call's result. The ``finally`` is what makes that safe:
        whatever happened, the master's context is restored, the live browser
        chat goes back to the master's window and the master's tab is refocused
        before this returns.
        """
        if not self._view.delegation_available():
            body = _unavailable_body(self._view.delegation_missing())
            await self._view.add_error("delegation refused: the sub-agent chat is not calibrated")
            self._view.notify(
                "the model tried to delegate, but the sub-agent chat is not calibrated",
                severity="warning",
            )
            return (body, "error", "delegation_unavailable")

        # The sub-agent window's service is frozen at bootstrap (both pickers
        # lock for the session's life), but the service EDITOR is not: F2
        # mid-session can delete the very preset that key names. Building the
        # engine anyway falls through cli.build's "unknown preset" fallback to
        # [general], so the run would quietly get neither the budget readiness
        # advertised nor the one the sub window is pointed at. Refuse instead -
        # a delegation the user can fix by re-picking a service beats one whose
        # paste budget is a guess.
        wanted = self._subagent_service
        if wanted and wanted not in self._config.services:
            await self._view.add_error(
                f"delegation refused: the sub-agent's service preset {wanted!r} was "
                "deleted while this session was running"
            )
            self._view.notify(
                f"the model tried to delegate, but the sub-agent's service {wanted!r} no "
                "longer exists - /new to pick another",
                severity="warning",
            )
            return (_gone_service_body(wanted), "error", "delegation_unavailable")

        master = self._snapshot_ctx()
        master.stats.subagents += 1
        self._sub_index += 1
        ref = SessionRef(
            id=f"sub-{self._sub_index}",
            role="subagent",
            title=_short_title(req.task),
            chat_name="",
        )
        await self._view.add_note(f"→ delegating to a sub-agent · {ref.title}")
        outcome: tuple[str, ResultStatus, str | None]
        # Whether this run handed a deliverable back, for the transcript note and
        # the tab glyph the `finally` writes. Pessimistic by default: every path
        # out of here except the last line of _sub_run is a failure, including
        # the ones that raise past `outcome` ever being bound.
        handed_back = False
        try:
            outcome = await self._sub_run(req, ref, master)
            handed_back = outcome[1] == "ok"
        except _SubagentAborted:
            await self._view.add_note("✗ sub-agent run aborted by the user")
            outcome = (_ABORTED_BODY, "error", "aborted")
        except (EngineStateError, BudgetExceeded) as exc:
            await self._view.add_error(f"sub-agent run failed: {exc}")
            outcome = (f"the sub-agent run failed: {exc}\n{_DELEGATION_HINT}", "error", "failed")
        except Exception as exc:  # never take the master's turn down with it
            await self._view.add_error(f"sub-agent run failed: {exc!r}")
            outcome = (f"the sub-agent run failed: {exc!r}\n{_DELEGATION_HINT}", "error", "failed")
        finally:
            sub, self._sub = self._sub, None
            self._reply_future = None
            self._sub_aborting = False
            await self._view.end_chat(sub if sub is not None else ref)
            if sub is not None:
                await self._view.finish_session_view(
                    sub.id,
                    _FINISHED_NOTE if handed_back else _FAILED_NOTE,
                    handed_back,
                )
            self._restore_ctx(master)
            self._view.focus_session_view(master.ref.id)
            await self._refresh_status()
        return outcome

    async def _sub_run(
        self, req: Delegate, ref: SessionRef, master: _SessionContext
    ) -> tuple[str, ResultStatus, str | None]:
        """The sub-run proper, with the master's context already saved.

        Order is load-bearing: the transcript tab opens before anything is
        clicked (so a failure is visible where it happened), the bootstrap is
        composed before the browser is touched (a budget failure must cost
        nothing), and ``start_chat`` runs before the FIRST paste - a False there
        aborts with zero ``copy_outbound`` calls, because a sub-agent bootstrap
        pasted into the master's chat would corrupt that conversation
        irrecoverably.
        """
        engine = await asyncio.to_thread(
            self._engine_factory,
            EngineRequest(
                # The SUB-AGENT window's own service, not the master's: the two
                # tabs are pointed at a service each, and the sub-agent's paste
                # budget, stillness window and captured appearances all have to
                # come from the chat it is actually going to run in.
                service=(
                    self._subagent_service
                    or master.stats.service
                    or self._config.general.service
                ),
                role="subagent",
                allow_delegate=False,  # nesting is excluded by construction
                parent_chat_name=master.ref.chat_name,
            ),
        )
        ref = replace(ref, chat_name=engine.chat_name)
        await self._view.open_session_view(ref)
        self._sub = ref
        self._adopt_ctx(ref, engine)
        task_text = _compose_sub_task(req)
        await self._view.add_note(
            f"sub-agent chat: {engine.chat_name} - a fresh chat with its own context; "
            "it sees nothing of the conversation that delegated to it"
        )
        await self._view.add_user(task_text)
        try:
            out = await self._engine_call(engine.start_task, task_text)
        except BudgetExceeded as exc:
            await self._view.add_error(f"the sub-agent bootstrap does not fit one paste: {exc}")
            return (_budget_body(exc), "error", "too_large")
        if not await self._view.start_chat(ref):
            await self._view.add_error(
                "could not open a fresh chat for the sub-agent - nothing was pasted"
            )
            return (_NEW_CHAT_FAILED_BODY, "error", "new_chat_failed")
        await self._copy_outbound(out)
        await self._view.add_note(
            f"→ sub-agent bootstrap copied ({out.total_chars:,} chars) - it goes into the "
            "sub-agent chat"
        )
        await self._refresh_status()
        return (await self._sub_loop(engine), "ok", None)

    async def _sub_loop(self, engine: Engine) -> str:
        """The ordinary session loop, with an awaited reply instead of a flow.

        Same body as the master's (``_run_turn_body`` is literally shared, so
        the gate, the glyph strip and the transcript behave identically); the
        difference is only what brackets it - this loop *waits* for the next
        reply rather than being re-entered by one, and it ends by returning the
        sub-agent's deliverable instead of leaving the session armed.
        """
        while True:
            text = await self._await_reply()
            result = await self._engine_call(engine.ingest, text)
            if isinstance(result, Noise):
                self._view.notify(f"sub-agent: {self._noise_text(result.reason)}")
                continue
            if isinstance(result, ChunkAck):
                self._view.notify(
                    "sub-agent: chunk ACK received, but chunked sends land in M3",
                    severity="warning",
                )
                continue
            if isinstance(result, ProtocolError):
                await self._view.add_error(f"protocol error: {result.detail}")
                self._view.notify(
                    "sub-agent: protocol error - re-copy its reply", severity="warning"
                )
                continue
            assert isinstance(result, NewTurn)
            self._stats.replies += 1
            self._stats.chars_in += len(text)
            step = await self._run_turn_body(result.reply)
            while isinstance(step, (AskUser, Delegate)):
                if isinstance(step, AskUser):
                    await self._view.add_note(f"? {step.question}")
                    answer = await self._ask(step.question)
                    await self._view.add_user(answer)
                    step = await self._run_engine_step(engine.answer_user, answer)
                    continue
                # Unreachable: `delegate` is not in a sub-agent's registry, so
                # the call pre-resolves as unknown_tool long before here.
                step = await self._run_engine_step(
                    engine.deliver_delegate_result,
                    _NESTED_DELEGATION_BODY,
                    status="error",
                    code="unknown_tool",
                )
            if isinstance(step, Done):
                self._stats.summary = step.summary
                if step.outbound is not None:
                    # Sibling results of the task_done turn: they belong in the
                    # sub-agent's chat, which nobody will read again - so they
                    # are recorded, not copied out.
                    await self._view.add_outbound(step.outbound, "final results (not copied)")
                await self._view.add_note("✓ sub-agent task done")
                if step.summary.strip():
                    await self._view.add_prose(step.summary)
                deliverable = step.result.strip() or step.summary.strip() or _EMPTY_RESULT_BODY
                # The deliverable goes in this tab too, not only into the
                # master's payload: it is the whole point of the run, and this
                # tab is the only place the user can ever read it again.
                await self._view.add_note(f"→ result handed back ({len(deliverable):,} chars)")
                if deliverable != step.summary.strip():
                    await self._view.add_prose(deliverable)
                await self._refresh_status()
                return deliverable
            assert isinstance(step, Send)
            await self._copy_outbound(step.outbound)
            await self._view.add_outbound(step.outbound, "results copied")
            self._view.alert(
                f"sub-agent: results copied ({step.outbound.total_chars:,} chars) - "
                "paste into the sub-agent chat"
            )
            await self._refresh_status()

    async def _await_reply(self) -> str:
        """Park until the sub-agent's chat produces a reply (or /abort fires).

        No wall-clock timeout on purpose: the transport is a human alt-tabbing
        between windows, and a sub-task can legitimately take many minutes. The
        way out is ``/abort``, which either resolves this future with
        ``_SubagentAborted`` or - if it fired while nothing was parked here -
        leaves the flag this checks on the way in."""
        if self._sub_aborting:
            raise _SubagentAborted(_ABORT_NOTE)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._reply_future = future
        try:
            return await future
        finally:
            self._reply_future = None

    # -- session context save / restore ---------------------------------------

    def _snapshot_ctx(self) -> _SessionContext:
        engine = self._engine
        active = self._active
        assert engine is not None and active is not None
        return _SessionContext(
            ref=active,
            engine=engine,
            chat_name=self._chat_name,
            preset=self._preset,
            snap=self._snap,
            stats=self._stats,
            turn_glyphs=self._turn_glyphs,
            last_outbound=self._last_outbound,
            has_outbound=self._has_outbound,
            yolo=self._yolo,
        )

    def _adopt_ctx(self, ref: SessionRef, engine: Engine) -> None:
        """Make ``ref``/``engine`` the session every other method operates on.

        The stats, glyph strip and outbound state start empty - they describe a
        conversation, and this is a different one. YOLO deliberately does NOT
        inherit: ``ApprovalPolicy`` is per-engine, so a sub-agent starts from the
        configured default and the user re-arms it per session if they mean it.
        """
        self._active = ref
        self._engine = engine
        self._chat_name = engine.chat_name
        self._snap = None
        self._stats = SessionStats(service=self._stats.service)
        self._turn_glyphs = {}
        self._last_outbound = None
        self._has_outbound = False
        self._yolo = self._config.approval.yolo
        self._push_state()

    def _restore_ctx(self, ctx: _SessionContext) -> None:
        self._active = ctx.ref
        self._engine = ctx.engine
        self._chat_name = ctx.chat_name
        self._preset = ctx.preset
        self._snap = ctx.snap
        self._stats = ctx.stats
        self._turn_glyphs = ctx.turn_glyphs
        self._last_outbound = ctx.last_outbound
        self._has_outbound = ctx.has_outbound
        self._yolo = ctx.yolo
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
        if self._stats.subagents:
            rows.append(("sub-agent runs", str(self._stats.subagents)))
        rows.append(("chars copied out", f"{self._stats.chars_out:,}"))
        rows.append(("chars ingested", f"{self._stats.chars_in:,}"))
        if snap is not None:
            rows.append(("session dir", str(snap.session_dir)))
        return rows

    async def _reset_session(self) -> None:
        self._session_active = False
        self._engine = None
        self._chat_name = None
        # A sub-run cannot be live here (/new is refused mid-turn), but the
        # numbering restarts with the session: the view clears the sub-agent
        # window's transcript along with the master's.
        self._active = None
        self._sub = None
        self._sub_index = 0
        self._subagent_service = ""  # the next session's spec chooses again
        self._reply_future = None
        self._sub_aborting = False
        self._preset = None
        self._snap = None
        self._last_outbound = None
        self._has_outbound = False
        self._queued_capture = None
        self._yolo = self._config.approval.yolo  # back to the configured default
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
        # The session_* fields say WHOSE state the rest of this snapshot is:
        # during a delegation every other field describes the sub-agent, and a
        # status bar or gate that did not say so would be actively misleading.
        active = self._active
        self._view.render_state(
            SessionView(
                session_active=self._session_active,
                busy=self._busy,
                pending_approval=self._pending_approval,
                awaiting_answer=self._awaiting_answer,
                has_outbound=self._has_outbound,
                snapshot=self._snap,
                session_id=active.id if active is not None else "master",
                session_role=active.role if active is not None else "master",
                session_title=active.title if active is not None else "",
            )
        )

    def _can_follow_up(self) -> bool:
        # A follow-up is legal while armed for a reply AND after task_done:
        # task_done completes the session but the user may continue (protocol.md
        # section 8). A DONE follow-up reopens the session into AWAITING_REPLY.
        return self._snap is not None and self._snap.phase in (Phase.AWAITING_REPLY, Phase.DONE)
