"""SessionController: the UI-agnostic session orchestrator.

This is the engine's host - the state machine that drives a whole AgentClip
session, lifted out of the Textual ``MainScreen`` so the UI can be swapped. It
owns the link to the Engine, the async flow state machine, the approval gate /
ask_user futures, session stats, the per-turn glyph strip, the per-turn
run-panel rows, and the depth-1 mid-turn reply queue. It talks to the UI ONLY through the
:class:`~agentclip.shell.app.view.ChatView` port and therefore imports no
Textual and no ``clip`` (clipboard I/O is a view concern - see
``ChatView.copy_outbound`` / ``read_clipboard``).

Threading model (unchanged from the old MainScreen, now expressed through the port):

- Every Engine call goes through the :class:`~agentclip.shell.app.link.Link`
  seam (docs/design/remote-executor.md section 2.2), which serializes them - one
  in flight - and keeps them off the event loop, so a minutes-long ``execute()``
  never blocks it. ``LocalLink`` does that with an ``asyncio.Lock`` and
  ``asyncio.to_thread``; the controller holds a ``Link`` and never an ``Engine``,
  which is what lets a later increment put a wire under the same calls.
- Flow coroutines run as background workers via ``view.spawn``; only one runs at a
  time (the ``busy`` flag). A reply arriving mid-turn is queued depth-1, newest wins.
- The approval gate is an ``asyncio.Future`` resolved by ``submit_decision``;
  ask_user uses a second future resolved by ``submit_message``.
- Traffic goes the OTHER way across that thread boundary too, and it is the one
  exception to everything above: while ``execute()`` runs, the engine calls
  ``_on_call_progress`` / ``_on_call_output`` from the worker thread so the run
  panel can show what is happening (tui.md §8a). Those two touch nothing but the
  row map and the view's three thread-safe port methods.

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
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from agentclip.config import (
    Config,
    ServicePreset,
    default_permissions_config_path,
    project_permissions_path,
)
from agentclip.engine.approval import PERMISSION_MODES, normalize_mode
from agentclip.engine.engine import (
    AskUser,
    AutoReply,
    CallProgress,
    ChunkAck,
    Decision,
    Delegate,
    Done,
    EngineStateError,
    NewTurn,
    Noise,
    PendingAction,
    PermissionMode,
    Phase,
    ProtocolError,
    Send,
    StatusSnapshot,
    StepResult,
)
from agentclip.engine.link.factory import EngineRequest
from agentclip.protocol.composer import BudgetExceeded
from agentclip.protocol.parser import peek_chat_name
from agentclip.protocol.types import Outbound, ParsedReply, ResultStatus, ToolCall
from agentclip.shell.app.commands import command_list, help_text, lookup
from agentclip.shell.app.link import Link, McpStatusLine
from agentclip.shell.app.types import SessionRef, SessionStats
from agentclip.shell.app.view import ChatView, RunCall, SessionView, Severity

_T = TypeVar("_T")

# How long a first `c` stays armed for the second one that turns a re-copy into
# a re-DELIVERY (tui.md 3.4a). Short on purpose, and measured with
# ``time.monotonic`` so a clock adjustment mid-session cannot widen it: the
# second press must be part of the *same gesture* as the first. A generous
# window would mean a `c` pressed now and another one pressed absent-mindedly
# some seconds later moves the mouse into the browser and pastes - and the
# whole point of the two stages is that nothing touches the machine until the
# user has said so twice, deliberately, in a row.
_RECOPY_DOUBLE_TAP_S = 1.5

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

# What a CANCELLED ask_user is answered with. Cancelling is an ordinary answer,
# never a poisoned park: the engine's only way out of AWAITING_USER is
# ``answer_user`` (engine.py, phase-guarded), so an exception raised into the
# answer future would leave a live engine parked on a question nobody will ever
# answer again. This string travels the normal path instead - the model reads it
# as the tool's result, the phase advances, and ``_ask``'s finally puts the
# composer back the way any other answer does.
_CANCELLED_ANSWER = "[cancelled by user]"

# Noise reason -> toast. "{chat}" is filled with the session's chat name.
_NOISE_TEXT = {
    "own-outbound": "ignored AgentClip's own outbound text - copy the model's reply instead",
    "not-protocol": "clipboard text has no CLIP blocks - ignored",
    "wrong-phase": "reply ignored - not awaiting a reply right now",
    "missing-chat": "ignored a paste without this chat's name ({chat}) - not from this chat?",
    "wrong-chat": "ignored a paste naming a different chat - this session is {chat}",
}


# What the USER is told when the permission mode changes. The MODEL gets its own
# one-liner on the next results payload (engine._MODE_NOTES) - these two say the
# same thing to two different readers, so neither borrows the other's wording.
_MODE_NOTE_TEXT: dict[str, str] = {
    "ask": (
        "permission mode: ASK - the normal gates are back. Edits and commands that no "
        "rule covers ask you before they run."
    ),
    "plan": (
        "permission mode: PLAN - exploration only. Every edit and command call is "
        "auto-denied (YOLO does not override this); reads, greps and listings run as "
        "usual, so the model can research and hand you a plan."
    ),
    "unattended": (
        "permission mode: UNATTENDED - nothing will ask you. Calls an allow rule covers "
        "run; everything that would have opened a gate is auto-denied instead, and deny "
        "rules still deny."
    ),
}

# Said on top of the UNATTENDED note when YOLO is on: the two settings pull in
# opposite directions and the user has to know which one wins.
_MODE_YOLO_CAUTION = (
    " CAUTION: YOLO is ON, and it still auto-APPROVES every call that would have asked "
    "- so those run instead of being denied. /yolo off to make unattended mean what it "
    "says."
)

_REINSTRUCT_NOTE = (
    "extra instructions armed - your service's instructions ride the next payload "
    "(results or a typed message) as a note, once. Press r again to disarm."
)

_MODE_ALERT_TEXT: dict[str, str] = {
    "ask": "mode: ASK - approvals restored",
    "plan": "mode: PLAN - edits and commands are denied",
    "unattended": "mode: UNATTENDED - nothing will ask you",
}

# What `/config` writes into a permissions.json that does not exist yet: the two
# blocks the loader reads and nothing else, so an editor opens a valid document
# whose shape is already the answer to "where do I put a rule?".
_CONFIG_TEMPLATE = '{\n  "permission": {},\n  "mcp": {}\n}\n'

# Said every time /config names a file, because the read is a LAUNCH read
# (cli.py builds one Config per process): an edit made now changes nothing until
# the next start, and a user who believed otherwise would think their rule was
# ignored.
_CONFIG_RESTART_NOTE = "read once at launch - restart AgentClip after editing it"


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


# What a finished call's status looks like in one cell. Same alphabet the glyph
# strip already uses at the gate, so a row does not change language halfway
# through the turn.
_RESULT_GLYPHS = {"ok": "✓", "error": "✗", "denied": "✗", "skipped": "−"}

# The params worth reading next to a tool name, most specific first: a row says
# "run_command  pytest -q", never "run_command" alone.
_DETAIL_PARAMS = ("command", "path", "pattern", "task", "question", "name", "summary")
_DETAIL_CHARS = 64


def _call_detail(call: ToolCall) -> str:
    """The one line the run panel shows about a call, flattened and clipped.

    Clipped HERE rather than in the view because it is a decision about what
    matters (the head of a command line, not its tail), and the port carries
    data the view only renders.
    """
    raw = next((call.params[p] for p in _DETAIL_PARAMS if call.params.get(p)), "")
    line = " ".join(raw.split())
    if len(line) > _DETAIL_CHARS:
        line = line[: _DETAIL_CHARS - 1].rstrip() + "…"
    return line


def _next_mode(current: PermissionMode) -> PermissionMode:
    """The mode one step around the cycle (ask -> plan -> unattended -> ask)."""
    return PERMISSION_MODES[(PERMISSION_MODES.index(current) + 1) % len(PERMISSION_MODES)]


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


def _mcp_server_line(status: McpStatusLine) -> str:
    """One transcript row per server: name, state, tools when connected, and
    the detail whenever there is one (a failure's whole value is its detail,
    and a connected server's can carry the shadowed-ids warning)."""
    parts = [status.name, status.state]
    if status.state == "connected":
        parts.append(f"{status.tool_count} tool{'' if status.tool_count == 1 else 's'}")
    if status.detail:
        parts.append(status.detail)
    return "  " + " · ".join(parts)


class _SubagentAborted(Exception):
    """The user ended a sub-agent run with /abort.

    Raised into whichever await the sub-run is parked on (the reply future, the
    ask_user future) so the run unwinds through ``_run_subagent``'s ``finally``
    like any other failure - one restore path, not two.
    """


class _TurnAborted(Exception):
    """The user asked for a new chat while a turn was in flight.

    ``_SubagentAborted``'s bigger sibling: that one ends a delegation and lets
    the master's turn carry on, this one ends the TURN - and the session behind
    it - because a fresh browser chat has just made every result the turn was
    holding undeliverable. Raised into whichever park is live
    (``_abort_turn``) or, for a flow caught between two parks, at the next
    ``_check_turn_abort`` checkpoint, so the flow unwinds through the same
    ``finally`` blocks any other failure would use. ``_wrap_flow`` swallows it:
    the new-chat path owns what the user is told.
    """


@dataclass(slots=True)
class _SessionContext:
    """Everything ``SessionController`` holds *per session*, saved whole.

    Nearly every method on the controller reads ``self._link`` /
    ``self._stats`` / ``self._snap``; a delegation swaps all of it for the
    sub-agent's and swaps it back afterwards, so those methods work on the
    sub-agent unchanged. Save-and-restore beats threading a session parameter
    through thirty call sites, and it is exactly right because delegation is
    single-flight: there is never a second live session to confuse it with.
    """

    ref: SessionRef
    link: Link
    chat_name: str | None
    preset: ServicePreset | None
    snap: StatusSnapshot | None
    stats: SessionStats
    turn_glyphs: dict[int, list[str]]
    turn_rows: dict[int, RunCall]
    last_outbound: str | None
    has_outbound: bool
    yolo: bool
    # NOT a value to restore into the mirror - the permission mode is app-wide
    # and outlives every swap (see _restore_ctx). This records what the SAVED
    # ENGINE's policy was left at, which is the only thing a delegation can make
    # stale: a mode cycled while the sub-agent held the engine slot reached the
    # sub-agent's policy and never the master's. Comparing the two on the way
    # back is how the master gets re-armed, and only when it actually moved.
    engine_mode: PermissionMode


class SessionController:
    """Synchronous-at-heart session driver; UI-agnostic via the ChatView port."""

    def __init__(
        self,
        config: Config,
        engine_factory: Callable[[EngineRequest], Link],
        project_root: Path,
        *,
        view: ChatView,
        mcp_statuses: Callable[[], Sequence[McpStatusLine]] | None = None,
    ) -> None:
        self._config = config
        self._engine_factory = engine_factory
        self._project_root = project_root
        self._view = view
        # Where /mcp reads its listing, or None when the app runs without an
        # MCP manager. A supplier of duck-typed rows rather than the manager
        # itself, so this layer stays clear of agentclip.executor.mcp (see
        # link.McpStatusLine); the shells pass cli.LinkFactory.statuses, bound.
        self._mcp_statuses = mcp_statuses

        self._link: Link | None = None
        self._chat_name: str | None = None  # this session's agreed chat name
        self._preset: ServicePreset | None = None
        self._snap: StatusSnapshot | None = None
        self._gate_future: asyncio.Future[tuple[Decision, str | None]] | None = None
        self._answer_future: asyncio.Future[str] | None = None
        self._queued_capture: str | None = None
        self._last_outbound: str | None = None
        # When the last `c` landed, or None when no re-copy is armed for the
        # escalation (see ``recopy``). Deliberately NOT part of
        # ``_SessionContext``: it is half a keystroke, not session state, and a
        # delegation that starts between two presses has plainly ended the
        # gesture the second press would have completed.
        self._recopy_armed_at: float | None = None
        self._stats = SessionStats()
        self._turn_glyphs: dict[int, list[str]] = {}  # call id -> [glyph, tool]
        # The same turn's calls as the run panel lists them, in id order: what
        # each one is ABOUT (the command line, the path), which of them can
        # stream output, and - through _turn_glyphs - how far each has got.
        self._turn_rows: dict[int, RunCall] = {}

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

        # -- the mid-turn new-chat abort --------------------------------------
        # Latched by ``request_new_session`` while it tears a turn down, and the
        # /abort latch's counterpart: it ends the whole TURN, not just a
        # delegation. Every flow-spawning door reads it too, so nothing sneaks a
        # new flow into the gap between the poisoned park and the reset.
        self._turn_aborting = False
        # Set exactly when no flow is running - what ``_abort_turn`` waits on to
        # know the turn has finished unwinding and the reset may start.
        self._flow_idle = asyncio.Event()
        self._flow_idle.set()

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
        # The session's permission mode, mirrored from the engine the way _yolo
        # is: read by the status bar and by the bare `/mode`, which toggles
        # against it. The engine's policy is the truth; this follows it.
        self._mode: PermissionMode = normalize_mode(config.approval.mode) or "ask"

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Kick off the session: prompt for a task, then run the loop."""
        self._spawn_flow(self._session_flow())

    def rebind(
        self,
        config: Config,
        engine_factory: Callable[[EngineRequest], Link],
        project_root: Path,
    ) -> bool:
        """Point the NEXT session at a different machine. Returns whether it took.

        :meth:`update_config`'s bigger sibling, and the whole of "one session,
        one host" from this layer's side (docs/design/remote-ssh.md decision 4):
        the GUI's connect dialog dials a box, builds an engine factory over that
        host, and hands the three things a session is assembled from over
        together, because on a remote session they change together - the root is
        a path on the target, the config was read off the target, and the factory
        carries the Host itself. Splitting them would let a session be built from
        two machines' answers.

        Refused while a session is live. A conversation's Engine, workspace jail
        and learned paths all belong to the machine it started on, so the caller
        ends it first (``request_new_session``) - which is exactly what
        "host-hopping = new session" means, expressed as a precondition rather
        than as a silent swap under a running turn. A controller parked on
        ``prompt_new_session`` needs nothing else: the flow reads these three
        attributes when it builds, which has not happened yet.
        """
        if self._session_active:
            return False
        self._config = config
        self._engine_factory = engine_factory
        self._project_root = project_root
        return True

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
        if not self._session_active or self._link is None:
            return
        if self._turn_aborting:
            # A /new is tearing this session down right now, so this reply
            # belongs to a conversation that is a second from not existing.
            # Dropped silently: the fresh bootstrap supersedes it, and a toast
            # about a paste nobody can act on would only compete with the reset.
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
        # ``_turn_aborting`` rides along with ``_busy`` on every door that
        # spawns a flow: between the poisoned park and the reset the flow slot
        # is free for an instant, and anything that took it would leave
        # ``_abort_then_reset`` with a busy controller and no reset to do.
        if (
            self._session_active
            and not self._busy
            and not self._turn_aborting
            and self._can_follow_up()
        ):
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
    # WHICH commands exist is not decided here: agentclip.shell.app.commands holds the
    # one table, and the help note, the "unknown command" hint and the composer's
    # autocomplete popup all render from it. This module only says what each one
    # DOES, and _command_handlers is the join - one entry per registry name.

    def _command_handlers(self) -> dict[str, Callable[[str], None]]:
        """What each registered command runs, keyed by its canonical name.

        Every :data:`~agentclip.shell.app.commands.COMMANDS` entry must appear here and
        nothing else may (a test pins the two sets together), so a command added
        to the registry cannot ship as a dead menu row. The uniform ``(arg)``
        signature is what lets dispatch stay a dict lookup; only the two on/off
        commands - `/yolo` and `/armed` - read it.
        """
        return {
            "yolo": self._cmd_yolo,
            "mode": self._cmd_mode,
            "new": lambda _arg: self._cmd_new(),
            "abort": lambda _arg: self._cmd_abort(),
            "help": lambda _arg: self._cmd_help(),
            "identify": lambda _arg: self._cmd_identify(),
            "log": lambda _arg: self._cmd_log(),
            "mcp": lambda _arg: self._cmd_mcp(),
            "armed": self._cmd_armed,
            "theme": self._cmd_theme,
            "config": self._cmd_config,
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

    @property
    def yolo(self) -> bool:
        """Whether approvals are off, as the controller last saw it. ``permission_mode``'s
        shape, and read for its reason: before a session exists this is what the
        next one will start in, which is what the status bar's edits segment
        paints when there is no snapshot to read it off."""
        return self._yolo

    def _cmd_yolo(self, arg: str) -> None:
        """Toggle (or set on/off) YOLO auto-approve-everything. Only reachable while
        armed/idle (an ask_user answer wins over commands), so the toggle itself runs
        off-loop through the link - set_yolo writes one session audit line.

        Ungated for ``set_permission_mode``'s reason: "approve everything I am
        about to ask for" is a decision made about the task one is ABOUT to
        describe, and a switch that could only be thrown after the bootstrap had
        gone out would be missing at the one moment it is easiest to mean. With
        no engine there is nothing to audit into and no conversation to announce
        the change to, so only the mirror moves - ``_session_flow`` hands it to
        the engine it builds, before the first payload. YOLO still DIES with the
        session (``_reset_session`` puts the configured default back) and still
        does not inherit into a sub-agent; only "before the first one" changes.
        """
        target = _parse_onoff(arg, current=self._yolo)
        if target is None:
            self._view.notify("usage: /yolo [on|off] - bare /yolo toggles", severity="warning")
            return
        if self._link is None:
            self._yolo = target
            self._push_state()  # repaint: the badge falls back to the mirror
            state = "ON" if target else "OFF"
            self._view.notify(
                f"YOLO will be {state} when the next session starts",
                severity="warning" if target else "information",
            )
            return
        self._view.spawn(self._apply_yolo(target))

    async def _apply_yolo(self, target: bool) -> None:
        # Spawned off the flow machinery (not _wrap_flow), so it owns its error
        # handling. set_yolo flips the policy flag THEN writes an audit line; if
        # that write fails we resync the mirror from the engine's real state
        # (re-read by _refresh_status) so a later bare /yolo toggles the right way.
        link = self._link
        if link is None:
            return
        try:
            await link.set_yolo(target)  # flips policy + audits, off-loop
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

    # -- the permission mode ---------------------------------------------------
    # Three public methods rather than one, because two different front-ends ask
    # for two different things: a chat command names the mode it wants, a key
    # binding only knows "the next one". Both land on ``_apply_mode``, which is
    # ``_apply_yolo``'s shape - engine call off-loop, then status, then the note.

    @property
    def permission_mode(self) -> PermissionMode:
        """The permission mode, as the controller last saw it. Meaningful with or
        without a live session: between sessions it is the mode the next one will
        start in, which is what the status bar paints when there is no snapshot."""
        return self._mode

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Switch to ``mode`` - with or without a live session.

        Deliberately NOT gated on a session. "Only explore, do not change
        anything" is a decision a user makes about the task they are ABOUT to
        describe, and a dial that could only be turned after the bootstrap had
        already gone out would be missing at the one moment it is easiest to
        mean. Pre-session only the engine half is skipped (there is no engine
        yet); the mirror, the transcript note and the repaint all happen, and
        ``_session_flow`` hands the mode to the engine it builds, before the
        first payload."""
        self._view.spawn(self._apply_mode(mode))

    def cycle_permission_mode(self) -> None:
        """The next mode round the cycle: ask -> plan -> unattended -> ask."""
        self.set_permission_mode(_next_mode(self._mode))

    def _cmd_mode(self, arg: str) -> None:
        """`/mode [plan|ask|unattended]`. Bare `/mode` REPORTS rather than cycles:
        a command that names three states must not require the user to remember
        which one they are in to reach the one they want."""
        if not arg:
            self._view.notify(
                f"permission mode: {self._mode} - /mode [{'|'.join(PERMISSION_MODES)}] to change",
                severity="information",
            )
            return
        wanted = normalize_mode(arg)
        if wanted is None:
            self._view.notify(
                f"usage: /mode [{'|'.join(PERMISSION_MODES)}] - bare /mode reports the current one",
                severity="warning",
            )
            return
        self.set_permission_mode(wanted)

    async def _apply_mode(self, target: PermissionMode) -> None:
        # _apply_yolo's error handling, for its reason: set_permission_mode flips
        # the policy field THEN writes the audit line, so a failed write leaves
        # the engine ahead of the mirror - resync from the engine's own state
        # rather than guessing.
        #
        # No engine means no session yet, and that is not an error: everything
        # below still runs, and the mirror it leaves behind is what _session_flow
        # arms the next engine with, before that engine's first payload.
        #
        # During a DELEGATION ``self._link`` is the sub-agent's, so a shift+tab
        # pressed while one runs reaches the conversation actually running - the
        # one the user is watching - and takes effect on its next verdict. The
        # master picks the change up when the slot comes back
        # (``_rearm_master_mode``).
        link = self._link
        if link is not None:
            try:
                await link.set_permission_mode(target)
            except Exception as exc:
                await self._refresh_status()
                if self._snap is not None:
                    self._mode = self._snap.mode
                await self._view.add_error(f"could not record the mode change: {exc}")
                self._view.alert("mode change failed - see transcript", severity="error")
                return
        self._mode = target
        await self._refresh_status()  # repaint the status bar (mode segment)
        note = _MODE_NOTE_TEXT[target]
        if target == "unattended" and self._yolo:
            note += _MODE_YOLO_CAUTION
        await self._view.add_note(note)
        severity: Severity = "information" if target == "ask" else "warning"
        self._view.alert(_MODE_ALERT_TEXT[target], severity=severity)
        self._view.notify(_MODE_ALERT_TEXT[target], severity=severity)

    # -- the extra-instructions re-inject (`r`) --------------------------------

    def reinstruct(self) -> None:
        """`r`: arm (or disarm) the service's extra instructions for the next send.

        ``cycle_permission_mode``'s shape - the engine owns the flag, this only
        asks and reports - and ungated for its reason: the moment a user reaches
        for this is the moment they have just watched the model mangle something,
        which is as likely to be mid-turn as not. The engine refuses what it
        cannot do (no session, nothing to re-inject) and names which, so the two
        refusals read as different sentences rather than one silent no-op.
        """
        if self._link is None:
            self._view.notify(
                "no session - the instructions ride the bootstrap when one starts",
                severity="warning",
            )
            return
        self._view.spawn(self._apply_reinstruct())

    async def _apply_reinstruct(self) -> None:
        link = self._link
        if link is None:
            return
        try:
            result = await link.arm_extra_instructions()
        except Exception as exc:
            await self._view.add_error(f"could not arm the instructions: {exc}")
            self._view.alert("re-instruct failed - see transcript", severity="error")
            return
        if result == "no-session":
            self._view.notify(
                "no session - the instructions ride the bootstrap when one starts",
                severity="warning",
            )
            return
        if result == "no-instructions":
            self._view.notify(
                "this service has no extra instructions - add them in the service editor (F2)",
                severity="warning",
            )
            return
        await self._refresh_status()  # repaint the status bar (instructions segment)
        if result == "armed":
            await self._view.add_note(_REINSTRUCT_NOTE)
            self._view.notify("instructions armed - they ride the next send", severity="warning")
        else:
            self._view.notify("instructions disarmed", severity="information")

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

        Never refused. Not mid-turn - a turn in flight is ABORTED for it (see
        ``request_new_session``), because a user who cannot start a new chat has
        no way out of a conversation that has gone wrong - and not with no
        session either: AgentClip having nothing to replace says nothing about
        the chat on screen, which is exactly the state a user reaches for `/new`
        from before starting work in a page still full of the last run. So the
        command always states the intent, and the view reports which halves
        happened; with no session there is simply no tool half to renew, and the
        view's own toast says so (it calls ``request_new_session`` back only
        when there is a session to reset).

        The one place typing `/new` still does something else is the ask_user
        answer park, where the composer's text IS the answer, verbatim -
        ``submit_message`` never gets as far as command parsing there, and the
        sidebar's "New browser chat" button is the escape hatch instead.
        """
        self._view.open_new_chat_now()

    def request_new_session(self) -> bool:
        """Start a fresh session once a fresh browser chat is already open.

        The tool-side half of /new, public because the browser side runs in the
        view: both the sidebar's "New browser chat" on the master tab and /new
        itself land here *after* the click has been attempted (tui.md sections
        1.3 and 3.3a), to reset the conversation to match the chat that replaced
        it - or the one the user is about to open by hand. Returns whether a
        fresh session is coming, and toasts the one refusal left when it is not.

        This is also the ONE door that knows how to end a turn in flight. A new
        browser chat has already destroyed the conversation the turn belongs to,
        so there is nothing left for it to deliver and no reason to make the
        user finish it first: ``_abort_turn`` poisons whichever park it is
        sitting on, ``_abort_then_reset`` waits for the flow to unwind and only
        then resets. True is returned immediately in that case - the reset is
        started, just not finished, which is what the caller's toast means by it.
        """
        if not self._session_active:
            self._view.notify("no active session to replace", severity="warning")
            return False
        if self._busy:
            if not self._turn_aborting:  # a second press is not a second abort
                self._turn_aborting = True
                self._view.notify(
                    "aborting the current step - starting a fresh session", severity="warning"
                )
                self._view.spawn(self._abort_then_reset())
            return True
        self._spawn_flow(self._reset_session())
        return True

    async def _abort_then_reset(self) -> None:
        """End the turn in flight, then reset - the mid-turn half of /new.

        Spawned bare rather than through ``_spawn_flow``: it is what waits for
        the busy flow to end, so it cannot be one. The latch is dropped in a
        ``finally`` and the reset spawned with no await in between, so nothing
        else can claim the flow slot in the gap (every other flow door refuses
        while ``_turn_aborting`` is up).

        The re-check is not paranoia: the turn's own unwinding can end the
        session on its way out (a summary, a crash into an inactive state), and
        a reset spawned onto that would prompt for a task nobody asked for.
        """
        try:
            await self._abort_turn()
        finally:
            self._turn_aborting = False
        if self._session_active and not self._busy:
            self._spawn_flow(self._reset_session())

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
          (``self._link`` is the sub's for the length of the run) so the
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
        link = self._link
        if self._executing and link is not None:
            link.request_cancel()

    def _cmd_help(self) -> None:
        self._view.spawn(self._view.add_note(help_text()))

    def _cmd_mcp(self) -> None:
        """`/mcp`: list every configured MCP server into the transcript.

        No session gate, `/log`'s reasoning: the listing answers "why does the
        model not have my server's tools?", which is asked before a session as
        readily as during one, and the manager is process-wide - nothing here
        reads session state. Text into the transcript rather than a toast
        because a failed server's detail is a sentence worth keeping; the
        sidebar's block shows the same facts clipped to a 30-cell column and
        this is where the whole line can be read.
        """
        source = self._mcp_statuses
        statuses = source() if source is not None else ()
        if not statuses:
            self._view.notify(
                "MCP is not configured - add servers to the mcp block of "
                "permissions.json (/config says where it lives)",
                severity="information",
            )
            return
        listing = "\n".join(_mcp_server_line(status) for status in statuses)
        self._view.spawn(self._view.add_note(f"MCP servers:\n{listing}"))

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

    def _cmd_theme(self, arg: str) -> None:
        """`/theme [name]`. Bare REPORTS the choices, `/theme <name>` wears one.

        No session gate (`/armed`'s reasoning, minus the urgency): appearance is
        a property of the machine the user is sitting at, not of a conversation,
        and there is nothing here a run could notice.

        It is the ONLY command surface for the theme in either shell now that
        neither has a command palette, so bare `/theme` has to be a listing
        rather than a cycle: the names are not guessable, and the two shells
        have different ones. Which names those are is the view's answer, not
        this module's - the controller never holds a theme name it did not read
        back from ``theme_choices`` (tui.md section 3.3a), which is also why an
        unrecognised one changes nothing: `/armed`'s rule that a typo is never
        read as an instruction.
        """
        choices = self._view.theme_choices()
        current = self._view.current_theme()
        if not arg:
            listing = " · ".join(
                f"{name} (current)" if name == current else name for name in choices
            )
            self._view.spawn(self._view.add_note(f"themes:  {listing}  -  /theme <name> to switch"))
            return
        wanted = arg.strip().lower()
        if wanted not in choices:
            self._view.notify(
                f"unknown theme: {arg} - try {', '.join(choices)}",
                severity="warning",
            )
            return
        self._view.apply_theme(wanted)  # applies AND persists; idempotent
        self._view.notify(f"theme: {wanted}")

    def _cmd_config(self, arg: str) -> None:
        """`/config [global|local]`: where the permission + MCP ruleset lives.

        No session gate, `/theme`'s reasoning: a ruleset is a property of a
        machine and a project, not of a conversation - and the moment a user
        wants it is usually the moment a gate just asked them something they
        would rather have answered once, in a file, before starting.

        Bare `/config` REPORTS both layers (`/mode`'s rule: a command naming two
        things must not make the user guess which one they are in), and an
        argument that is neither ACTS on nothing at all - `/armed`'s rule that a
        typo is never read as an instruction, which matters more here because
        the acting branch WRITES a file.

        The two writes are the same write: make sure the file exists (with an
        empty template, so an editor opens something valid rather than a blank
        page) and put its path on the clipboard, which is as close to "open it"
        as a layer that owns no file manager can get.
        """
        wanted = arg.strip().lower()
        if not wanted:
            self._view.spawn(self._view.add_note(self._config_report()))
            return
        if wanted not in ("global", "local"):
            self._view.notify(
                "usage: /config [global|local] - bare /config says where both files are",
                severity="warning",
            )
            return
        if wanted == "local" and self._config.remote.is_remote():
            # The project is on the target, and this shell can only write files
            # on THIS PC: creating a same-named file locally would put a ruleset
            # nobody will read next to a project that is not here. Said, not
            # guessed at (docs/design/remote-ssh.md, "the target owns its policy").
            self._view.notify(
                "/config local is not supported in a remote session yet - the project's "
                f"ruleset lives on {self._config.remote.target}; edit it over there",
                severity="warning",
            )
            return
        path = (
            default_permissions_config_path()
            if wanted == "global"
            else project_permissions_path(self._project_root)
        )
        self._view.spawn(self._apply_config(wanted, path))

    def _config_report(self) -> str:
        """Both layers and whether each one exists yet - bare `/config`'s answer."""
        lines = [f"permission + MCP ruleset (JSON; {_CONFIG_RESTART_NOTE}):"]
        for label, path in (
            ("global", default_permissions_config_path()),
            ("project", project_permissions_path(self._project_root)),
        ):
            if label == "project" and self._config.remote.is_remote():
                lines.append(f"  {label}:  {path.as_posix()}  (on {self._config.remote.target})")
                continue
            state = "exists" if path.is_file() else "not created yet"
            lines.append(f"  {label}:  {path}  ({state})")
        lines.append("  /config global or /config local creates it and copies the path")
        if self._config.remote.is_remote():
            # Neither local file governs a remote session - the target owns its
            # policy - and a listing that did not say so would read as if this
            # PC's ruleset were still in force (docs/design/remote-ssh.md).
            lines.append(
                f"  this session's rules come from {self._config.remote.target}, "
                "not from the files above"
            )
        return "\n".join(lines)

    async def _apply_config(self, label: str, path: Path) -> None:
        """Create the ruleset file if it is missing, then park its path.

        The clipboard half goes through ``park_outbound`` and only that: it is
        the one write registered with the self-write set before it lands, so the
        watcher does not read the path back in as a pasted reply (driver/clip).
        """
        try:
            created = await asyncio.to_thread(self._ensure_config_file, path)
        except OSError as exc:
            self._view.notify(f"could not create {path}: {exc}", severity="error")
            return
        await self._view.park_outbound(str(path))
        verb = "created" if created else "found"
        await self._view.add_note(f"⎘ {verb} {path} - path copied. {_CONFIG_RESTART_NOTE}")
        # Both layers are called permissions.json, so the toast names the LAYER:
        # "found permissions.json" would not say which of the two it meant.
        self._view.notify(f"{verb} the {label} ruleset - path copied to the clipboard", timeout=8)

    @staticmethod
    def _ensure_config_file(path: Path) -> bool:
        """Write the empty template unless the file is already there; did it write?

        Never touches an existing file - the user's rules are the whole point of
        it - and the template is the two blocks the loader reads, so an editor
        opens a valid document with the shape already visible."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return False
        path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        return True

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

        Deliberately NOT one of the link's serialized calls: the lock is held
        by the very ``execute()`` we are interrupting, which is why the seam
        keeps ``request_cancel`` sync and out-of-band (the engine behind it just
        sets an Event) so it can be called from here, on the event loop.

        The turn is not aborted: the engine finishes it normally and the results
        - the interrupted call plus the skipped ones - flow through the usual
        Send path, so the model is told what happened without the user doing
        anything else. A no-op when nothing is executing."""
        link = self._link
        if link is None or not self._executing:
            return
        link.request_cancel()
        self._view.notify(
            "cancelling - the killed call and the skipped ones are sent to the model",
            severity="warning",
        )

    def cancel_pending_question(self) -> None:
        """Answer a pending ``ask_user`` with "cancelled" and let the turn run on.

        The way OUT of a question the user does not want to answer, and it is a
        RESOLUTION, not an abort: the future is completed with
        ``_CANCELLED_ANSWER``, which flows through ``_handle_step``'s ordinary
        AskUser branch - echoed into the transcript like any other answer, handed
        to ``engine.answer_user``, and unparking the engine from AWAITING_USER,
        which nothing else can do. Poisoning the park instead (what ``/new``
        does) is only safe when a full session reset follows it; here there is
        none, and the engine would be stranded.

        A no-op when no question is on the floor, and when a send already
        resolved it a heartbeat earlier."""
        if not self._awaiting_answer:
            return
        future = self._answer_future
        if future is None or future.done():
            return
        # Same two lines an ordinary send makes (``submit_message``), in the same
        # order: the box is emptied before the flow can repaint it.
        self._view.reset_composer()
        future.set_result(_CANCELLED_ANSWER)
        self._view.notify(
            "question cancelled - the model is told you did not answer", severity="warning"
        )

    # The three key actions that spawn a flow. ``_turn_aborting`` guards each
    # alongside ``_busy`` for the reason spelled out in ``_send_follow_up``:
    # the flow slot falls free for an instant while a mid-turn /new unwinds,
    # and it is spoken for.

    def undo(self) -> None:
        if self._busy or self._turn_aborting or not self._session_active:
            return
        self._spawn_flow(self._undo_flow())

    def force_ingest(self) -> None:
        if self._busy or self._turn_aborting or not self._session_active:
            return
        self._spawn_flow(self._force_ingest_flow())

    def end_session(self) -> None:
        if self._busy or self._turn_aborting or not self._session_active:
            return
        self._spawn_flow(self._show_summary())

    def recopy(self) -> None:
        """`c`: the last outbound back onto the clipboard - and, pressed twice,
        back into the chat (tui.md 3.4a).

        Two stages, because the two things a user means by `c` are not the same
        size. *Give me that payload again* is a clipboard write and costs
        nothing; *send it again* moves the mouse into the browser, clicks a chat
        box and pastes into whatever has focus, which is the one class of act
        this whole app asks before doing. So the first press parks and says what
        the second one would do, and only a second press inside
        ``_RECOPY_DOUBLE_TAP_S`` escalates - the double tap IS the confirmation
        dialog, spent as one gesture instead of a modal.

        The arm is consumed on every press, so a `c` after the window has
        expired is simply a fresh first press rather than a delivery the user
        stopped expecting. Stage one is deliberately ungated - a clipboard write
        is safe mid-turn, like `/log` and the log export - while stage two takes
        the same ``_busy`` / ``_turn_aborting`` refusal the flow-spawning
        actions do: a payload pasted on top of a turn that is about to compose
        its own would put two messages in the box. The states only the VIEW can
        see (disarmed, an auto-copy flow already driving the mouse) are refused
        on the far side of ``redeliver_outbound``, where they are visible.
        """
        text = self._last_outbound
        if text is None:
            return
        armed_at, self._recopy_armed_at = self._recopy_armed_at, None
        now = time.monotonic()
        if armed_at is not None and now - armed_at <= _RECOPY_DOUBLE_TAP_S:
            if self._busy or self._turn_aborting:
                self._view.notify(
                    "a turn is running - the payload is on your clipboard to paste yourself",
                    severity="warning",
                )
                return
            self._view.redeliver_outbound(text)
            return
        self._recopy_armed_at = now
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
        self._flow_idle.clear()
        self._push_state()
        self._view.spawn(self._wrap_flow(coro))

    async def _wrap_flow(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except _TurnAborted:
            # The turn was torn down so a fresh chat could take its place. No
            # transcript line and no toast here: ``request_new_session`` said
            # what is happening before the poison went in, and the reset it is
            # waiting to start says the rest.
            pass
        except (EngineStateError, BudgetExceeded) as exc:
            await self._view.add_error(str(exc))
            self._view.alert(str(exc), severity="error")
        finally:
            self._busy = False
            self._pending_approval = False
            self._awaiting_answer = False
            self._view.stop_working()
            self._view.hide_gate()
            # Last, and after the flags: an aborting /new resumes the instant
            # this is set and reads them to decide it may reset now.
            self._flow_idle.set()
        await self._refresh_status()
        queued, self._queued_capture = self._queued_capture, None
        if queued is not None and self._session_active and self._link is not None:
            self._spawn_flow(self._ingest_flow(queued))

    async def _abort_turn(self) -> None:
        """Tear the turn in flight down, and wait until it has finished falling.

        The user must always be able to start a new chat, and a turn cannot be
        interrupted from outside: it is parked on one of three futures, or
        inside ``execute()`` on a worker thread, or in the handful of lines
        between two of those. So this poisons *every* park that could be live -
        harmless when it is not - and then waits on ``_flow_idle`` for the flow
        coroutine to actually unwind, because the reset that follows tears down
        the very engine the turn is still holding.

        Poisoning is safe precisely because each park cleans up in a ``finally``
        (``_gate`` clears ``_pending_approval``, ``_ask`` clears
        ``_awaiting_answer``, ``_run_subagent`` restores the master's context),
        so an exception raised into one unwinds through the same path a failure
        would. A sub-run is ended with ``_SubagentAborted`` rather than
        ``_TurnAborted`` for the same reason /abort uses it: that is the
        exception ``_run_subagent`` knows how to finish a run with, and the
        master's own ``_TurnAborted`` arrives a moment later at its next
        checkpoint.

        The flow caught BETWEEN parks is ``_check_turn_abort``'s half: the
        latch is already set when this returns to the event loop, so the next
        checkpoint the flow reaches raises for it.

        Not poisoned, and deliberately: the flows parked on a *modal*
        (``confirm`` for undo, the summary screen, the paste prompt). Those hold
        the screen, so neither `/new` nor the sidebar button can be reached
        while one is up - there is no way to arrive here from them.
        """
        self._queued_capture = None  # nothing captured for the old chat survives
        if self._sub is not None:
            self._sub_aborting = True
        reply = self._reply_future
        if reply is not None and not reply.done():
            reply.set_exception(_SubagentAborted(_ABORT_NOTE))
        answer = self._answer_future
        if answer is not None and not answer.done():
            answer.set_exception(_TurnAborted())
        gate = self._gate_future
        if gate is not None and not gate.done():
            gate.set_exception(_TurnAborted())
        link = self._link
        if self._executing and link is not None:
            # Out-of-band by design (it sets an Event), which is what lets it be
            # called from the event loop while the worker thread is inside
            # execute(). That turn ends normally; the checkpoint after it raises.
            link.request_cancel()
        await self._flow_idle.wait()

    def _check_turn_abort(self) -> None:
        """Raise if a new-chat abort landed while this flow was between parks.

        Called at every point a flow could be passing through when
        ``_abort_turn`` fires with nothing to poison - including the top of
        ``_handle_step``, which is what stops an aborted turn from copying its
        ``Send`` outbound into the chat that no longer exists.
        """
        if self._turn_aborting:
            raise _TurnAborted()

    async def _run_engine_step(
        self, fn: Callable[..., Awaitable[_T]], /, *args: object, **kwargs: object
    ) -> _T:
        """Run execute()/answer_user() with the 'working' spinner showing meanwhile.

        Bookkeeping only: the serializing and the off-loop hop live in the Link
        (see :mod:`agentclip.shell.app.link`), so all this adds is the spinner and
        the ``_executing`` window - which is exactly the window in which
        cancelling means something: the engine is chewing through tool calls on
        the worker thread and the user is watching the spinner."""
        # Race-free: there is no await between this check and ``_executing``
        # going true, so an abort either raises here or finds the flag set and
        # cancels the engine.
        self._check_turn_abort()
        n = len(self._turn_glyphs)
        label = f"Working - running {n} tool call{'' if n == 1 else 's'}..." if n else "Working..."
        self._view.start_working(label, self._run_rows())
        self._executing = True
        try:
            return await fn(*args, **kwargs)
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
            link = await asyncio.to_thread(
                self._engine_factory,
                EngineRequest(
                    service=spec.service,
                    allow_delegate=delegation,
                    # The factory sizes the MCP catalog against the REAL task
                    # (it is part of the bootstrap the sizing protects).
                    task_chars=len(spec.task),
                ),
            )
            self._watch_link(link)
            # The permission mode the user dialled in BEFORE this session existed
            # (shift+tab or /mode at the start prompt, or the one carried over
            # from the session before it) is the mode this one starts in. Applied
            # here, ahead of start_task, so the very first verdict of the very
            # first turn already obeys it - and while the engine is still IDLE,
            # which is how it knows this is a starting mode and not a change to
            # announce to a conversation that has not begun.
            await link.set_permission_mode(self._mode)
            # Same story for a `/yolo` thrown at the start prompt, but only when
            # it DIVERGES from what the fresh engine already believes: the engine
            # builds its policy from the same config default the mirror started
            # at, so an untouched mirror has nothing to say and set_yolo would
            # only write an audit line about a change nobody made.
            if self._yolo != self._config.approval.yolo:
                await link.set_yolo(self._yolo)
            try:
                out = await link.start_task(spec.task)
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
        self._link = link
        # Immutable for the session, so the link answers for them with no round
        # trip of its own (see Link) and they are mirrored for the noise toasts.
        self._chat_name = link.chat_name
        self._active = SessionRef(
            id="master",
            role="master",
            title=_short_title(spec.task),
            chat_name=link.chat_name,
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
        # How this session was ASSEMBLED, said once where the user will read it
        # (e.g. "paste budget too small for MCP tools" - docs/design/mcp.md
        # section 5). The factory that decided it has no view, so the facts ride
        # the engine to here, the first moment a transcript exists to hold them.
        for warning in link.build_warnings:
            await self._view.add_note(f"! {warning}")
            self._view.notify(warning, severity="warning", timeout=8)
        await self._view.add_note(
            f"chat name: {link.chat_name} - the model echoes chat={link.chat_name} on "
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
        link = self._link
        if link is None:
            return
        result = await link.ingest(text)
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
        if isinstance(result, AutoReply):
            # The engine answered the model itself (a broken paste nothing could
            # be run from). Same copy-and-tell shape as a turn's results - the
            # payload is one, and the user's next move is the same paste - with
            # the transcript saying why it exists.
            await self._view.add_error(f"protocol error: {result.detail}")
            await self._copy_outbound(result.outbound)
            await self._view.add_outbound(result.outbound, "resend request copied")
            # Deliberately says "refused as sent" rather than naming a cause:
            # two different transport faults arrive here (a flattened reply and
            # an unfenced one), the toast is one line, and the transcript error
            # above it already carries the engine's own diagnosis. A toast that
            # says "flattened" for a reply that was merely unfenced sends the
            # user hunting for missing line breaks that are all present.
            self._view.alert(
                f"{self._alert_prefix}reply refused as sent - a resend request was "
                "copied; paste it into the chat",
                severity="warning",
            )
            await self._refresh_status()
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
        link = self._link
        assert link is not None
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
        self._turn_rows = {
            c.id: RunCall(
                call_id=c.id,
                tool=c.tool,
                detail=_call_detail(c),
                streams=c.tool == "run_command",
            )
            for c in reply.calls
        }
        done = 0
        while True:
            self._check_turn_abort()
            pending = await link.pending()
            if not pending:
                break
            action = pending[0]
            self._set_glyph(action.call.id, "▶")
            decision, note = await self._gate(action, f"{done + 1}/{done + len(pending)}")
            await link.decide(action.call.id, decision, note)
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
                if decision is Decision.APPROVE_ALL_EDITS:
                    label = "approved (auto-accept edits ON)"
                elif decision is Decision.APPROVE_ALWAYS:
                    pattern = action.always_pattern or "*"
                    label = f"approved (always allowing {pattern} this session)"
                else:
                    label = "approved"
                await self._view.add_note(f"✓ {label} {action.call.tool} {target}".rstrip())
        self._view.hide_gate()
        await self._refresh_status()  # EXECUTING (status segment driven by busy)
        return await self._run_engine_step(link.execute)

    def _set_glyph(self, call_id: int, glyph: str) -> None:
        if call_id in self._turn_glyphs:
            self._turn_glyphs[call_id][0] = glyph

    def _queue_strip(self) -> str:
        return "  ".join(
            f"{glyph}{cid} {tool}" for cid, (glyph, tool) in sorted(self._turn_glyphs.items())
        )

    def _run_rows(self) -> list[RunCall]:
        """This turn's calls for the run panel, each carrying its glyph so far.

        Deliberately NOT read off ``_turn_glyphs``, which looks like the same
        information and is not: there a ✓ means "the user approved this", here
        it means "this ran and it worked", and every call in the plan is
        approved before the first one runs. Same alphabet, two questions.

        The glyph matters when a PARKED turn resumes (an ask_user answered
        mid-plan re-enters execute), where the earlier calls really are done
        and a panel that redrew them as pending would undo what the user just
        watched happen.
        """
        return [row for _, row in sorted(self._turn_rows.items())]

    # -- watching the engine execute (WORKER-THREAD callbacks) ----------------
    #
    # Both are called by the engine from the thread the link offloaded the call
    # to, so they may not await, may not touch the flow state machine, and
    # may not assume the event loop is anywhere near them. All they do is stamp
    # the row map - a dict write per call, which the GIL makes atomic and which
    # nothing on the loop side reads mid-execute - and hand the fact straight to
    # the view, whose implementation is required to be thread-safe for exactly
    # these three methods (see ChatView).

    def _on_call_progress(self, progress: CallProgress) -> None:
        glyph = "▶" if progress.phase == "running" else _RESULT_GLYPHS.get(progress.status, "✓")
        row = self._turn_rows.get(progress.call_id)
        if row is not None:
            self._turn_rows[progress.call_id] = replace(row, glyph=glyph)
        if progress.phase == "running":
            self._view.call_started(
                progress.call_id, progress.tool, row.detail if row is not None else ""
            )
            return
        self._view.call_finished(progress.call_id, glyph)

    def _on_call_output(self, call_id: int, chunk: str) -> None:
        self._view.call_output(call_id, chunk)

    def _watch_link(self, link: Link) -> None:
        """Point a freshly built session's two live-progress hooks at this view.

        Sync registration, and the hooks keep the engine's own contract behind
        the seam: they fire on the worker thread, mid-execute (see Link)."""
        link.set_progress_hook(self._on_call_progress)
        link.set_output_hook(self._on_call_output)

    async def _gate(self, action: PendingAction, position: str) -> tuple[Decision, str | None]:
        self._check_turn_abort()  # never put a panel up for a turn already gone
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
        link = self._link
        assert link is not None
        # Nothing this method does is safe for a turn the user has abandoned -
        # least of all the Send branch, which would copy the old conversation's
        # next payload onto the clipboard for a chat that no longer exists.
        self._check_turn_abort()
        # Both parking steps resume the SAME turn, so they loop together: a
        # reply may ask a question, then delegate, then ask again. `link` stays
        # valid across a delegation because _run_subagent restores this session's
        # context before it returns.
        while isinstance(step, (AskUser, Delegate)):
            self._check_turn_abort()
            if isinstance(step, AskUser):
                await self._view.add_note(f"? {step.question}")
                answer = await self._ask(step.question)
                await self._view.add_user(answer)
                step = await self._run_engine_step(link.answer_user, answer)
                continue
            text, status, code = await self._run_subagent(step)
            await self._view.add_note(
                f"← sub-agent result ({len(text):,} chars, {status}) - handed back to the model"
            )
            step = await self._run_engine_step(
                link.deliver_delegate_result, text, status=status, code=code
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
        self._check_turn_abort()  # never park the composer for a turn already gone
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

        Returns for every outcome including a crash - never raises - because the
        caller is mid-turn on the master and whatever happened has to reach the
        model as the `delegate` call's result. The ``finally`` is what makes that
        safe: the master's context is restored, the live browser chat goes back
        to the master's window and the master's tab is refocused before this
        returns.

        The single exception is ``_TurnAborted`` (a mid-turn `/new`), which is
        re-raised: there is no master turn left to deliver a result to, and the
        session itself is being replaced. The ``finally`` still runs.
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
        except _TurnAborted:
            # The one exception that must NOT become a delegate result: there is
            # no master turn left to hand it to. It can only arrive from a gate
            # or an engine step INSIDE the sub-run (a sub park raises
            # _SubagentAborted instead), which is exactly what a mid-turn /new
            # does. The ``finally`` below still restores the master's context on
            # the way past - one teardown path, however the run ended.
            raise
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
            await self._rearm_master_mode(master)
            self._view.focus_session_view(master.ref.id)
            await self._refresh_status()
        return outcome

    async def _rearm_master_mode(self, master: _SessionContext) -> None:
        """Hand the master's engine any mode change it slept through.

        Only one engine is reachable at a time (``_apply_mode`` writes to
        whatever ``self._link`` currently is), so a shift+tab pressed during a
        delegation lands on the SUB-AGENT's policy - which is right, that is the
        conversation running - and leaves the master's saying what it said when
        the run began. The mirror is the authority, so the master is the stale
        one and gets told on the way back.

        Silent when nothing moved: re-sending the same mode would arm a "the mode
        is now ask" note on a conversation whose mode never changed. When it DID
        move the note is exactly right - the master really was re-dialled while
        it was parked, and its next results payload should say so.

        Never raises: this runs in ``_run_subagent``'s ``finally``, where an
        exception would replace the delegation's own outcome (or a _TurnAborted
        on its way out) with a bookkeeping failure.
        """
        link = self._link
        if link is None or master.engine_mode == self._mode:
            return
        try:
            await link.set_permission_mode(self._mode)
        except Exception as exc:
            await self._view.add_error(
                f"could not re-arm the permission mode after the sub-agent run: {exc}"
            )

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
        # Composed BEFORE the engine is built: the sub-task (the model's task
        # plus the delegating context) is exactly what start_task will paste,
        # and the factory needs its real length to size the MCP catalog -
        # model-written delegations routinely dwarf a typed master task.
        task_text = _compose_sub_task(req)
        link = await asyncio.to_thread(
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
                task_chars=len(task_text),
            ),
        )
        self._watch_link(link)  # a sub-agent's calls are watched like the master's
        ref = replace(ref, chat_name=link.chat_name)
        await self._view.open_session_view(ref)
        self._sub = ref
        self._adopt_ctx(ref, link)
        # The dial governs every engine in the app run, sub-agents included:
        # armed here, while this engine is still IDLE and before its bootstrap,
        # so the sub-agent's very first verdict already obeys plan/unattended and
        # nothing is announced to a conversation that has not started.
        await link.set_permission_mode(self._mode)
        await self._view.add_note(
            f"sub-agent chat: {link.chat_name} - a fresh chat with its own context; "
            "it sees nothing of the conversation that delegated to it"
        )
        await self._view.add_user(task_text)
        try:
            out = await link.start_task(task_text)
        except BudgetExceeded as exc:
            await self._view.add_error(f"the sub-agent bootstrap does not fit one paste: {exc}")
            return (_budget_body(exc), "error", "too_large")
        if not await self._view.start_chat(ref):
            await self._view.add_error(
                "could not open a fresh chat for the sub-agent - nothing was pasted"
            )
            return (_NEW_CHAT_FAILED_BODY, "error", "new_chat_failed")
        await self._copy_outbound(out)
        # How this sub-session was ASSEMBLED, the master flow's surfacing verbatim
        # (see _session_flow): the sub-agent's engine is built by the same factory
        # against its own service, so "paste budget too small for MCP tools" can
        # be true of the sub-run alone - and the note lands in the sub-agent's
        # tab, the only transcript anyone will read this run in.
        for warning in link.build_warnings:
            await self._view.add_note(f"! {warning}")
            self._view.notify(warning, severity="warning", timeout=8)
        await self._view.add_note(
            f"→ sub-agent bootstrap copied ({out.total_chars:,} chars) - it goes into the "
            "sub-agent chat"
        )
        await self._refresh_status()
        return (await self._sub_loop(link), "ok", None)

    async def _sub_loop(self, link: Link) -> str:
        """The ordinary session loop, with an awaited reply instead of a flow.

        Same body as the master's (``_run_turn_body`` is literally shared, so
        the gate, the glyph strip and the transcript behave identically); the
        difference is only what brackets it - this loop *waits* for the next
        reply rather than being re-entered by one, and it ends by returning the
        sub-agent's deliverable instead of leaving the session armed.
        """
        while True:
            text = await self._await_reply()
            result = await link.ingest(text)
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
            if isinstance(result, AutoReply):
                # A sub-agent's chat breaks replies exactly like the master's -
                # flattened, or unfenced on a require-fenced service - and the
                # engine bounces it exactly the same way (the counter is
                # per-engine, so a sub-agent gets its own budget). The only thing
                # this loop does differently is what it does differently for
                # every payload: copy, tell, and go back to waiting for a reply
                # instead of returning to the flow.
                await self._view.add_error(f"protocol error: {result.detail}")
                await self._copy_outbound(result.outbound)
                await self._view.add_outbound(result.outbound, "resend request copied")
                self._view.alert(
                    "sub-agent: reply refused as sent - a resend request was copied; "
                    "paste it into the sub-agent chat"
                )
                await self._refresh_status()
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
                    step = await self._run_engine_step(link.answer_user, answer)
                    continue
                # Unreachable: `delegate` is not in a sub-agent's registry, so
                # the call pre-resolves as unknown_tool long before here.
                step = await self._run_engine_step(
                    link.deliver_delegate_result,
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
        link = self._link
        active = self._active
        assert link is not None and active is not None
        return _SessionContext(
            ref=active,
            link=link,
            chat_name=self._chat_name,
            preset=self._preset,
            snap=self._snap,
            stats=self._stats,
            turn_glyphs=self._turn_glyphs,
            turn_rows=self._turn_rows,
            last_outbound=self._last_outbound,
            has_outbound=self._has_outbound,
            yolo=self._yolo,
            engine_mode=self._mode,
        )

    def _adopt_ctx(self, ref: SessionRef, link: Link) -> None:
        """Make ``ref``/``link`` the session every other method operates on.

        The stats, glyph strip and outbound state start empty - they describe a
        conversation, and this is a different one. YOLO deliberately does NOT
        inherit: ``ApprovalPolicy`` is per-engine, so a sub-agent starts from the
        configured default and the user re-arms it per session if they mean it.

        The permission mode does the OPPOSITE, and the divergence is the point.
        YOLO is an answer to a question ("approve everything in THIS
        conversation"); the mode is a statement about the user ("I am only
        exploring", "I am not at my desk"), and both stay true of a sub-agent -
        a delegation that could still edit files while plan mode is on, or open
        a gate for an absent user while unattended is, would break the promise
        the mode's own denial bodies make. So the mirror is left alone here and
        ``_sub_run`` arms the sub-agent's engine with it before its first
        payload, exactly as ``_session_flow`` does for the master.
        """
        self._active = ref
        self._link = link
        self._chat_name = link.chat_name
        self._snap = None
        self._stats = SessionStats(service=self._stats.service)
        self._turn_glyphs = {}
        self._turn_rows = {}
        self._last_outbound = None
        self._has_outbound = False
        self._yolo = self._config.approval.yolo
        self._push_state()

    def _restore_ctx(self, ctx: _SessionContext) -> None:
        self._active = ctx.ref
        self._link = ctx.link
        self._chat_name = ctx.chat_name
        self._preset = ctx.preset
        self._snap = ctx.snap
        self._stats = ctx.stats
        self._turn_glyphs = ctx.turn_glyphs
        self._turn_rows = ctx.turn_rows
        self._last_outbound = ctx.last_outbound
        self._has_outbound = ctx.has_outbound
        self._yolo = ctx.yolo
        # ``self._mode`` is deliberately NOT restored: the dial is the user's and
        # app-wide, so a cycle made while the sub-agent held the engine slot is
        # still what the user wants now. The saved ``engine_mode`` is only what
        # the master's POLICY was left at - ``_rearm_master_mode`` reconciles the
        # engine to the mirror, never the mirror to the snapshot.
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
        self._link = None
        self._chat_name = None
        # A sub-run has already been torn down before this runs - a mid-turn
        # /new aborts it first (``_abort_turn``), every other road here is idle -
        # but the numbering restarts with the session anyway: the view clears
        # the sub-agent window's transcript along with the master's. The two
        # abort latches are cleared for the same belt-and-braces reason; both
        # are already false on every path that reaches here.
        self._active = None
        self._sub = None
        self._sub_index = 0
        self._subagent_service = ""  # the next session's spec chooses again
        self._reply_future = None
        self._sub_aborting = False
        self._turn_aborting = False
        self._preset = None
        self._snap = None
        self._last_outbound = None
        self._has_outbound = False
        self._queued_capture = None
        self._yolo = self._config.approval.yolo  # back to the configured default
        # ...but NOT the permission mode, which survives every reset for the life
        # of the app run. It is a dial the user holds, not a property of one
        # conversation: like the service picker, "I am only exploring today" or
        # "I have stepped away" is still true of the person after /new, and a
        # mode that silently reverted to `ask` on a reset would hand the next
        # session's first edit to a user who thought they had turned changes off.
        # The status segment keeps showing it the whole time, so it can never be
        # a surprise; a session that really should start fresh is `[approval]
        # mode` plus a restart, or one more shift+tab.
        self._stats = SessionStats()
        self._turn_glyphs = {}
        self._turn_rows = {}
        await self._view.clear_transcript()
        self._push_state()  # phase -> IDLE (snap is None)
        await self._session_flow()

    # -- undo / follow-up / manual ingest ------------------------------------

    async def _undo_flow(self) -> None:
        link = self._link
        if link is None:
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
            report, notice = await link.undo_last_turn(compose_notice=True)
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
        link = self._link
        if link is None:
            return
        out = await link.follow_up(text)
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
        # A fresh payload ends any half-finished `c` gesture: the second press
        # would otherwise deliver THIS one, which is not the message the first
        # press was about and has just been delivered anyway.
        self._recopy_armed_at = None
        self._stats.chars_out += outbound.total_chars

    async def _recopy(self, text: str) -> None:
        # ``park_outbound``, not ``copy_outbound``: the clipboard write and
        # nothing else. The toast is what makes the second stage discoverable -
        # a double tap nobody is told about is a feature nobody has.
        await self._view.park_outbound(text)
        self._view.notify(
            f"re-copied the last outbound ({len(text):,} chars) - press c again to deliver it"
        )

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
        link = self._link
        if link is not None:
            self._snap = await link.status()
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
