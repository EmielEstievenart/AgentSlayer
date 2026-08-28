"""The Engine: AgentClip's synchronous, sans-IO session state machine.

The engine consumes strings (ingested clipboard text, user decisions, user
answers) and returns values (outbound payloads, pending actions, results). It
performs filesystem/subprocess side effects only through the tool layer,
never touches the clipboard, and never imports Textual (enforced by
tests/test_layering.py). The TUI calls it from exactly one worker thread.

Semantics implemented here (protocol.md sections 4-6 + plan synthesis):
- ingest: own-outbound suppression (the hashes of our own composed payloads,
  last 20), the chat-name gate, the DONE-reopen rule, tool name validation
  (unknown tool -> pre-resolved unknown_tool result), fatal per-call parse
  issues -> pre-resolved error results;
- plan: one approval verdict per call (engine/approval.py); a call a permission
  rule DENIES is pre-resolved as a denied result and the turn carries on -
  only an interactive rejection aborts the rest of it;
- execute: strict id order, denied results with user_note, the same-path skip
  rule after a failed/denied mutation, rejection-aborts-turn, the per-turn
  backup bracket (begin_turn at first mutation, finish_turn at turn end),
  ask_user pause/resume, delegate pause/resume (the host runs a sub-agent and
  feeds its deliverable back), task_done collection, id=0 reply_truncated /
  reply_flattened / reply_unfenced results, and cooperative cancellation
  (request_cancel, from any thread).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from agentclip.config import Config, LimitsConfig, ServicePreset, resolve_limits
from agentclip.engine.approval import (
    DENY_VERDICTS,
    EDITS_RULE,
    ApprovalPolicy,
    PermissionMode,
)
from agentclip.engine.numbered import (
    ServedRead,
    content_hash,
    describe_ranges,
    surviving_numbered_lines,
)
from agentclip.engine.results import fit_results
from agentclip.engine.states import Decision, EngineStateError, Phase, can_transition
from agentclip.engine.store.backups import BackupStore, UndoReport
from agentclip.engine.store.session import SessionStore
from agentclip.executor.hosts.base import Host
from agentclip.executor.hosts.local import LocalHost
from agentclip.executor.tools.chunks import CachedChunks, chunk_chars_for
from agentclip.executor.tools.fs_tools import numbered_requested, split_lines
from agentclip.executor.tools.registry import ToolContext, ToolRegistry, ToolSpec, error_result
from agentclip.executor.tools.sandbox import SandboxViolation, Workspace
from agentclip.protocol.composer import BudgetExceeded, Composer
from agentclip.protocol.names import normalize_chat_name
from agentclip.protocol.parser import normalize, normalized_hash, parse_reply
from agentclip.protocol.preset import LivePreset
from agentclip.protocol.types import (
    Outbound,
    ParsedReply,
    ParseIssue,
    ResultStatus,
    ToolCall,
    ToolResult,
)

_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})

# Per-call ParseIssue kinds that make a call non-executable (the parser keeps
# benign tolerances at reply level; anything attached to the call is fatal).
_FATAL_ISSUES = frozenset(
    {"bad_header", "unterminated_heredoc", "missing_end", "client_mangled_heredoc"}
)

# Reply-level warning kinds surfaced to the LLM as NOTE lines in the results.
_NOTE_WARNINGS = frozenset({"renumbered", "duplicate_id", "missing_end", "unknown_param"})

# Chunks of one outbound are joined with this separator in outbound/turn-NNNN.txt.
_CHUNK_SEPARATOR = "\n␞\n"

# Result bodies below this never get a fetch_chunk id (Engine._mint_chunks). The
# marker alone is ~100 characters, so caching a body barely longer than one buys
# nothing worth the id - and a payload so crowded that its LARGEST body is under
# a kilobyte has no room to serve a part back either.
_CHUNK_MINT_FLOOR = 1_000

# How many times in a row the engine answers a TRANSPORT-BROKEN reply BY ITSELF
# (an id=0 reply_flattened or reply_unfenced payload asking for a fenced resend)
# before it gives up and hands the problem to the user instead.
#
# The cap is the whole reason this is safe to automate. Flattening is a
# TRANSPORT fault, not a model mistake, so there are two populations of hosts:
# ones where the model simply forgot the fence (a resend fixes it, and two
# tries is generous) and ones whose copy path strips newlines from EVERY copy,
# where the model's corrected reply comes back flattened exactly like the last
# one. Without a cap the second population ping-pongs forever - AgentClip
# composes, the user pastes, the host flattens, AgentClip composes - burning
# the model's context and the user's turns while never once being able to
# succeed. After two bounces the evidence points at the host, and the only
# actor who can do anything about the host is the human.
#
# ONE budget, SHARED by both bounce kinds (protocol.md 1.4 #15). Flattening and
# fence-stripping are not two problems: they are two symptoms of one fault -
# the transport mangled a reply - and they have one remedy, the fenced resend
# both payloads ask for. A host that alternates symptoms (this paste lost its
# newlines, the next arrived unfenced but intact) is still one broken host, and
# two separate budgets would let the pair ping-pong 2N turns deep before the
# human hears about it. Any paste that arrives whole clears it.
_MAX_TRANSPORT_BOUNCES = 2

# How much of the glued-together line is quoted back at the model. Enough to
# recognise its own text and see where the break should have been; short enough
# that a 20k-char single line does not become the payload.
_FLATTENED_QUOTE_CHARS = 160

# What the model is told when the user changes the permission mode mid-session.
# It rides the NEXT results payload's note channel rather than a payload of its
# own: the bootstrap has ~200 chars of slack (protocol.md section 2, "Budget
# headroom"), so a mode is never explained there, and the denial bodies say
# everything a model needs even if this note never gets a payload to ride.
_MODE_NOTES: dict[str, str] = {
    "plan": "permission mode is now plan: exploration only; edit/command calls will be denied.",
    "build": "permission mode is now build: normal approvals resumed.",
}

# The same channel, for the unattended toggle. It shares the single pending-note
# slot with the mode notes: the latest thing the user did is the thing the model
# needs to know, and two notes racing for one payload would only ever read as
# one instruction contradicting another.
_UNATTENDED_NOTES: dict[bool, str] = {
    True: (
        "the user has stepped away (unattended): only calls covered by allow rules will"
        " run; everything that would have asked them is auto-denied."
    ),
    False: "the user is back (unattended off): normal approvals resumed.",
}

# The prefix the re-injected `extra_instructions` ride under. Named as a
# reminder, because it is one: the same words already went out with the
# bootstrap, and a model that reads this as a NEW rule has been told the
# opposite of what the user meant by pressing `r`.
_INSTRUCTIONS_NOTE_PREFIX = "user instructions reminder:"

# What arm_extra_instructions() answers with. Four states rather than a bool
# because the two refusals are the interesting ones: the UI has to say which
# door is shut ("no session" vs "this service has nothing to re-inject"), and
# neither is an error worth raising.
ArmResult = Literal["armed", "disarmed", "no-session", "no-instructions"]


# -- value types returned to the TUI ------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingAction:
    call: ToolCall
    kind: Literal["edit", "command", "auto"]
    preview: str  # unified diff / command line / "" for auto
    auto_reason: str | None  # e.g. 'allowed by rule bash["git status *"]'
    # The resource pattern an "always allow" answer would remember (e.g.
    # "git commit *") - what the gate's third button offers to write down.
    always_pattern: str | None = None


@dataclass(frozen=True, slots=True)
class CallProgress:
    """One step of a turn's execution, reported AS IT HAPPENS.

    Everything else the engine says about a turn is said when the turn is over
    (the StepResult and its results payload), which is exactly wrong for the
    minutes a build spends running: the user is watching, and "N tool calls" is
    all the screen has to say about which one. So each call announces itself
    twice - ``phase="running"`` just before its handler is entered, and
    ``phase="done"`` with the result's status once it has resolved - and calls
    that never run at all (denied by a rule, skipped after a rejection or a
    cancel, pre-resolved parse errors) announce only the second, so a queued row
    on screen always resolves visibly rather than sitting there pending forever.

    ``status`` is the ToolResult status ("ok" | "error" | "denied" | "skipped")
    and is empty while phase is "running".
    """

    call_id: int
    tool: str
    phase: Literal["running", "done"]
    status: str = ""


# See Engine.set_progress_hook: called ON THE WORKER THREAD, mid-plan.
ProgressHook = Callable[[CallProgress], None]


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    phase: Phase
    turn: int
    service_key: str
    budget_chars: int
    auto_accept_edits: bool
    yolo: bool  # auto-approve everything (edits + commands) - bypasses every gate
    mode: PermissionMode  # build (the ruleset as written) | plan (no changes)
    unattended: bool  # nobody is at the keyboard: every gate auto-denies
    session_dir: Path
    last_outbound_chars: int
    # Does this session's preset carry extra_instructions at all, and has the
    # user armed a re-injection of them? The pair the `r` key needs: the first
    # decides whether the key exists, the second whether it is lit.
    has_extra_instructions: bool = False
    instructions_armed: bool = False


@dataclass(frozen=True, slots=True)
class NewTurn:
    reply: ParsedReply


@dataclass(frozen=True, slots=True)
class ChunkAck:
    part: int | None
    total: int | None


@dataclass(frozen=True, slots=True)
class Noise:
    # "wrong-phase" | "not-protocol" | "own-outbound" | "missing-chat" | "wrong-chat"
    reason: str


@dataclass(frozen=True, slots=True)
class ProtocolError:
    detail: str


@dataclass(frozen=True, slots=True)
class AutoReply:
    """A payload the engine composed BY ITSELF, with no turn behind it.

    Every other outbound answers something the model asked for: results answer
    calls, a NOTE answers an undo, a TASK answers the user. This one answers a
    paste that never became a turn - a reply the transport broke badly enough
    that nothing in it could run: the flattened reply of protocol.md section 1.4
    tolerance #14, or the unfenced one of #15. The alternative, telling only the
    human, leaves
    the model sitting in silence waiting for results that are never coming: the
    session looks stalled from the one side that could actually fix it.

    ``outbound`` is an ordinary RESULTS payload carrying one id=0 error result,
    so the host copies it out exactly like a turn's results and the model reads
    it with the envelope it already knows. ``detail`` is the human's version of
    the same fact, for the transcript. The phase does not move: the engine was
    AWAITING_REPLY and it still is, now waiting for the corrected reply.
    """

    outbound: Outbound
    detail: str


IngestResult = NewTurn | ChunkAck | Noise | ProtocolError | AutoReply


@dataclass(frozen=True, slots=True)
class Send:
    outbound: Outbound


@dataclass(frozen=True, slots=True)
class AskUser:
    question: str
    call_id: int


@dataclass(frozen=True, slots=True)
class Delegate:
    """A parked `delegate` call: run this task on a fresh sub-agent, then hand
    its deliverable back with Engine.deliver_delegate_result()."""

    task: str
    context: str | None
    call_id: int


@dataclass(frozen=True, slots=True)
class Done:
    summary: str
    outbound: Outbound | None  # results of sibling calls this turn, if any
    # task_done's `result` param: a sub-agent's deliverable, handed to the
    # agent that delegated to it. Empty for an ordinary master session.
    result: str = ""


StepResult = Send | AskUser | Delegate | Done


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
    # Plan index of the in-flight parked call (the ask_user while AWAITING_USER
    # or the delegate while AWAITING_SUBAGENT); only one call is ever parked.
    index: int = 0
    backup_started: bool = False
    done_summary: str | None = None
    done_result: str = ""
    results: list[ToolResult] = field(default_factory=list)
    failed_paths: set[str] = field(default_factory=set)
    # (call id, path param) of every `read_file numbered: yes` that succeeded
    # this turn. Collected here rather than re-derived at compose time because
    # the plan - and with it the calls' params - is torn down before the payload
    # is rendered.
    numbered_reads: list[tuple[int, str]] = field(default_factory=list)


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
        chat_name: str,
        role: Literal["master", "subagent"] = "master",
        host: Host | None = None,
        build_warnings: Sequence[str] = (),
        presets: LivePreset | None = None,
    ) -> None:
        # THE ONE PLACE `[limits]` stops being a wish and becomes numbers. Two of
        # its keys default to config.AUTO_LIMIT because the right value for them
        # is a fraction of the paste budget, and no loader could have known that
        # budget: it is the SERVICE's, and the service is the monitor's answer,
        # which can still change while this session runs (§11.9). So the
        # marriage is re-done per read (:meth:`_limits`) rather than once here,
        # and `self._config.limits` stays what it always was - resolved numbers,
        # never a sentinel, so no consumer needs an `or default` of its own.
        # WHICH service this session composes against, and it is a question
        # rather than a value: the monitor owns the preset and can change it
        # mid-session (docs/design/ui-monitor.md §11.9). No provider - a CLI or
        # headless launch, a remote engine, an idle window - means the local
        # `[services.*]` table, read exactly as it always was.
        self._presets = presets if presets is not None else LivePreset(config.preset())
        # The `[limits]` block as the user WROTE it, sentinels and all: the
        # resolution below answers them from the paste budget, so it has to be
        # re-done whenever that budget moves (see :meth:`_limits`) and a
        # re-resolution of already-resolved limits would freeze the first budget
        # in forever.
        self._raw_limits = config.limits
        config = replace(config, limits=self._limits())
        self._config = config
        self._role = role
        # One-line facts about how this session was ASSEMBLED that the user
        # should see once at session start (today: "the paste budget could not
        # hold the MCP tools", docs/design/mcp.md section 5). The factory has no
        # view, so they ride the engine to whoever starts the session - the
        # controller surfaces them as transcript notes, the same channel
        # config.warnings use at app start.
        self.build_warnings: tuple[str, ...] = tuple(build_warnings)
        # The machine the project lives on. Every tool's filesystem and command
        # access goes through it; local unless a session was pointed elsewhere.
        self._host = host if host is not None else LocalHost()
        self._registry = registry
        self._workspace = workspace
        self._session = session
        self._backups = backups
        self._composer = composer
        self._policy = ApprovalPolicy(config.approval, config.permission_rules)
        # Set by request_cancel() from the UI thread while the plan runs on the
        # engine's worker thread; cleared at the start of every plan run.
        self._cancel = threading.Event()
        # The full text of the bodies the last payload had to truncate, keyed by
        # the id its marker names, and the counter those ids come from. Engine-
        # side only: nothing about it touches `Outbound`/`ToolResult`, so a
        # remote session inherits it with no wire change - the engine that
        # composed the payload is the engine that holds the cache, wherever it
        # runs. Handed to the ToolContext by IDENTITY and only ever mutated in
        # place afterwards (see _update_chunk_cache): `fetch_chunk` reads this
        # very dict a turn or more later, and a rebind here would leave the
        # handler holding the cache as it was when the session started.
        #
        # The id counter is engine-global and monotonic, deliberately unlike
        # `ToolResult.call_id`, which the parser renumbers from 1 every turn and
        # therefore cannot name anything across one.
        self._chunk_cache: dict[str, CachedChunks] = {}
        self._next_chunk_id = 1
        self._ctx = ToolContext(
            workspace=workspace,
            limits=config.limits,
            caps=self._presets.caps(),
            host=self._host,
            backup_hook=self._backup_hook,
            cancel_event=self._cancel,
            chunk_cache=self._chunk_cache,
        )
        # The session's agreed chat name, normalized once for comparison. The
        # composer stamps the same name on every outbound; ingest requires the
        # model to echo it (see _chat_gate).
        self._chat_name = normalize_chat_name(chat_name) or chat_name
        self._phase = Phase.IDLE
        self._turn = 0  # number of the last outbound payload (ordering only)
        # Hashes of OUR OWN composed payloads only (a session can have several
        # outbounds live in the model's scrollback). Accepted replies are never
        # remembered - see ingest().
        self._outbound_hashes: deque[str] = deque(maxlen=20)
        # Per file, the numbered lines that survived into the LAST results
        # payload - the whole basis on which a `replace_lines` is allowed
        # (engine/numbered.py). Keyed by _norm_path of the file's path inside
        # the workspace, and replaced wholesale every time a results payload
        # goes out, because "the results you were just given" has to mean
        # literally that: a range read three turns ago has had three turns to
        # stop pointing where the model thinks it does.
        self._numbered_reads_served: dict[str, ServedRead] = {}
        self._reply: ParsedReply | None = None
        self._plan: list[_Planned] = []
        self._exec: _ExecState | None = None
        self._last_outbound_chars = 0
        # Consecutive transport-broken ingests answered automatically (flattened
        # AND unfenced together - see _MAX_TRANSPORT_BOUNCES for why one
        # counter); reset by any paste that parsed whole. Lives here, next to
        # the phase and the turn counter, because it is the same kind of fact:
        # where this session has got to.
        self._transport_bounces = 0
        # A one-line "the mode changed" note waiting for a payload to ride out
        # on. At most one: a user who cycles three times before the next turn
        # meant the mode they landed on, not the trip.
        self._mode_note: str | None = None
        # Has the user asked for the preset's extra_instructions to ride the
        # next outbound? Same shape as _mode_note and for the same reason: a
        # fact about this session, spent on the next payload that actually goes
        # out. Not reset anywhere else, because an engine is per-session - a
        # `/new` builds a fresh one, which starts here at False.
        self._instructions_armed = False
        # Watchers of the plan as it executes; see set_progress_hook.
        self._progress_hook: ProgressHook | None = None

    # -- the live service ------------------------------------------------------

    def _preset(self) -> ServicePreset:
        """The service this session is composing FOR, asked fresh every time.

        Never cached in a field: the monitor owns the ``[services.*]`` table and
        pushes a new one over ``Watched`` whenever the user edits it, so a
        budget raised mid-session has to govern the very next payload
        (docs/design/ui-monitor.md §11.9). With no monitor behind the provider
        this is the local config's preset and the answer never changes.
        """
        return self._presets.preset()

    def _limits(self) -> LimitsConfig:
        """``[limits]``, with every AUTO sentinel answered by the CURRENT budget.

        Re-derived rather than stored for :meth:`_preset`'s reason: half of this
        block is a fraction of the paste budget, so a budget the monitor moved
        has to move `max_result_chars` with it or the next turn would fit its
        results to a number the service no longer allows.
        """
        return resolve_limits(self._raw_limits, self._preset().max_paste_chars)

    def _sync_ctx(self) -> None:
        """Point the tool context at the live service before a plan runs.

        The context is built once per session and handed to every handler, and
        two of its fields - the resolved limits and the budget caps - are
        budget-shaped. This is where they catch up with a monitor's edit; the
        object itself is never rebound, because ``chunk_cache`` is shared with
        the handlers by IDENTITY (see above).
        """
        self._ctx.limits = self._limits()
        self._ctx.caps = self._presets.caps()

    # -- watching a turn execute ---------------------------------------------

    def set_progress_hook(self, hook: ProgressHook | None) -> None:
        """Be told which call is running, as it runs (see :class:`CallProgress`).

        Wired once, like backup_hook, by whoever owns the engine - and called
        FROM THE WORKER THREAD that is inside ``execute()``, in the middle of the
        plan. A hook that blocks blocks the turn, so a UI hook must do nothing
        but hand the fact to its own thread (the TUI posts a message). One that
        raises is dropped, hook and all: the progress report is a courtesy, and
        a turn must not fail because nobody was left to watch it.
        """
        self._progress_hook = hook

    def set_output_hook(self, hook: Callable[[int, str], None] | None) -> None:
        """Be handed a running command's output as it appears (call_id, delta).

        The same thread contract as set_progress_hook, and the same wiring
        moment; it lands on the ToolContext, where ``run_command`` picks it up
        (see :attr:`agentclip.executor.tools.registry.ToolContext.on_output`).
        """
        self._ctx.on_output = hook

    def _progress(
        self,
        call_id: int,
        tool: str,
        phase: Literal["running", "done"],
        status: str = "",
    ) -> None:
        hook = self._progress_hook
        if hook is None:
            return
        try:
            hook(CallProgress(call_id=call_id, tool=tool, phase=phase, status=status))
        except Exception:  # noqa: BLE001 - see set_progress_hook: a watcher is not the turn
            self._progress_hook = None

    # -- task lifecycle ------------------------------------------------------

    @property
    def chat_name(self) -> str:
        """This session's chat name; immutable for the engine's lifetime."""
        return self._chat_name

    @property
    def role(self) -> Literal["master", "subagent"]:
        """"master" for the session the user started, "subagent" for one built
        to serve a `delegate` call. Immutable for the engine's lifetime."""
        return self._role

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
        reopens the session, transitioning back to AWAITING_REPLY for the reply.

        Spends an armed extra-instructions reminder like a results payload does:
        "the next thing we send" has to mean the next thing, or a session steered
        by typed messages would never deliver one."""
        if self._phase not in (Phase.AWAITING_REPLY, Phase.DONE):
            raise EngineStateError(
                f"follow_up() requires phase AWAITING_REPLY or DONE, but engine is {self._phase.name}"
            )
        outbound = self._composer.task(self._turn + 1, text, self._take_instructions_note())
        self._turn += 1
        self._session.append_event("task", text=text, turn=self._turn)
        self._register_outbound(outbound)
        if self._phase is Phase.DONE:
            self._set_phase(Phase.AWAITING_REPLY)  # reopen the completed session
        return outbound

    # -- inbound -------------------------------------------------------------

    def ingest(self, text: str) -> IngestResult:
        """Parse one clipboard text. Meaningful in AWAITING_REPLY and in DONE
        (a valid reply reopens a completed session); any other phase returns
        Noise("wrong-phase") and the TUI decides what to show (e.g. the
        unexpected-reply modal).

        A paste is eligible for interpretation on exactly two preconditions:
        the CLIPBOARD CHANGED, and the CHAT NAME MATCHES. The first is the clip
        watcher's job - it forwards byte-level changes only, and our own writes
        advance its baseline - so by the time text reaches here, it is always a
        NEW copy. Therefore re-pasting a reply AgentClip already ran RE-RUNS it,
        by design: a user deliberately re-copying an older message is asking for
        it to be re-interpreted, and a model that sends the identical response
        twice means it twice. Accepted replies are consequently never
        remembered; the hash set holds only our own outbound payloads, which are
        suppressed because a results payload contains `===CLIP:` and would
        otherwise read as a reply.

        Gate order is load-bearing: wrong-phase, not-protocol, own-outbound, the
        chat name, the DONE-reopen rule, then the two transport checks
        (flattened, then unfenced). The transport checks come last so a corrupt
        paste from ANOTHER chat is still reported as the foreign paste it is -
        and because the answer to either is a payload back to the model (see
        _flattened_ingest / _unfenced_ingest), which must only ever be composed
        for a chat we established is ours.

        Flattened is asked FIRST because it is the more specific diagnosis: a
        flattened paste has hard evidence in hand (a sentinel line with another
        marker glued onto it), and its message already demands the fenced
        resend the unfenced bounce would ask for. A reply that is both is one
        broken paste, told once, in the words that describe what actually
        arrived."""
        if self._phase not in (Phase.AWAITING_REPLY, Phase.DONE):
            return Noise("wrong-phase")
        reply = parse_reply(text)
        if reply.kind == "noise":
            return Noise("not-protocol")
        if reply.normalized_hash in self._outbound_hashes:
            return Noise("own-outbound")  # the user copied our payload back: silent
        chat_noise = self._chat_gate(reply)
        if chat_noise is not None:
            # Not remembered: a foreign paste must report the same reason every
            # time it is pasted.
            return chat_noise
        if self._phase is Phase.DONE:
            # The session is complete; only a whole reply that PROVED it belongs
            # to this chat may reopen it. A present EOM that survived the chat
            # gate is that proof. An ACK/NACK is meaningless once the session is
            # over, and a truncated reply carries no chat name at all (the gate
            # deliberately skips it), so neither may reopen - that would run
            # text we never established was ours.
            if reply.kind != "reply" or not reply.eom.present:
                return Noise("wrong-phase")
            self._set_phase(Phase.AWAITING_REPLY)  # reopen the completed session
            self._session.append_event("reopened", turn=self._turn)
        flattened = next((w for w in reply.warnings if w.kind == "flattened_reply"), None)
        if flattened is not None:
            return self._flattened_ingest(text, flattened)
        if self._preset().require_fenced_reply and reply.calls and not reply.saw_fence:
            # Scope, deliberate on both sides (protocol.md 1.4 #15):
            #
            # - only replies carrying AT LEAST ONE CALL are gated. A zero-call
            #   reply has nothing executable in it to corrupt; refusing it would
            #   replace the no-calls nag - which already says the useful thing -
            #   with a lecture about fences. ACK and NACK are taught as bare
            #   single lines with no fence anywhere near them, and gating those
            #   would break the chunk handshake outright.
            # - a TRUNCATED reply that carries calls IS gated, even though the
            #   truncation path would otherwise handle it. That path executes
            #   the complete calls and asks for the tail - and executing code
            #   that came through the prose renderer is precisely the silent
            #   corruption this gate exists to stop.
            return self._unfenced_ingest(text)
        # A paste that parsed whole clears the bounce budget: whatever went
        # wrong before, this transport is delivering line breaks and fences now,
        # so the NEXT mangling is a fresh incident with its own two tries. It
        # sits after BOTH checks because passing one and failing the other is
        # not "arrived whole".
        self._transport_bounces = 0
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
            eom_chat=reply.eom.chat,
        )
        if reply.kind == "ack":
            return ChunkAck(reply.ack_part, reply.ack_total)
        if reply.kind == "nack":
            reason = reply.nack_reason or "unspecified"
            return ProtocolError(
                f"model NACKed the last paste (reason={reason}); re-copy the outbound payload"
            )
        self._reply = reply
        self._plan = self._build_plan(reply)
        self._set_phase(Phase.REVIEW)
        return NewTurn(reply)

    def _flattened_ingest(self, text: str, issue: ParseIssue) -> AutoReply | ProtocolError:
        """Answer a paste whose line breaks died in transport (section 1.4 #14).

        The paste is ours - it passed the chat gate - but it is not a whole
        reply: blocks are glued onto sentinel lines and were never parsed.
        Executing the fragment that DID parse would silently drop the rest, the
        one failure mode the design forbids, and re-splitting the glued line
        would mean executing a command AgentClip reassembled. So nothing here
        runs, and nothing is recovered; the only question is who gets told.

        The model gets told first, up to _MAX_TRANSPORT_BOUNCES times: an id=0
        `reply_flattened` result riding the ordinary results envelope, exactly
        like the `reply_truncated` result of section 5.2 and for the same
        reason - the actor that can fix this in one move is the one that wrote
        the reply, and it is currently waiting on results that will never come.
        The offending line is quoted back at it because "your reply was
        flattened" is abstract until you see your own text with the breaks
        missing, and the fix (one ~~~~ fence around EVERYTHING) is stated in
        full, since the bootstrap has no budget left to teach this case.

        Past the cap the user gets told instead, with the same quoted line: at
        that point the host, not the model, is the thing that needs changing.
        The paste is not remembered either way - a mangled paste must report
        the same thing every time it is pasted.
        """
        quoted = _offending_line(text, issue.line)
        self._transport_bounces += 1
        attempt = self._transport_bounces
        # One event with the raw paste in it, not the usual "inbound" + verdict
        # pair: this text never became a turn, and the whole point of auditing it
        # is being able to read afterwards exactly what the transport delivered.
        self._session.append_event(
            "flattened",
            line=issue.line,
            attempt=attempt,
            quoted=quoted,
            bounced=attempt <= _MAX_TRANSPORT_BOUNCES,
            raw=text,
        )
        if attempt > _MAX_TRANSPORT_BOUNCES:
            return ProtocolError(
                f"{issue.detail}. Nothing ran, and this is broken paste"
                f" #{attempt} in a row - AgentClip has already asked the chat to"
                f" resend {_MAX_TRANSPORT_BOUNCES} times, so it has stopped asking."
                f" What arrived: {quoted}. This host is probably stripping line"
                " breaks from every copy: ask the chat to resend the whole reply"
                " inside one ~~~~ fence, or copy the reply from its raw/code view"
                " instead of the rendered message"
            )
        outbound = self._compose_results([_flattened_result(quoted)], [], keep_chunks=True)
        return AutoReply(
            outbound,
            f"{issue.detail}. Nothing ran - asked the chat to resend the whole reply"
            f" inside one ~~~~ fence ({attempt} of {_MAX_TRANSPORT_BOUNCES})",
        )

    def _unfenced_ingest(self, text: str) -> AutoReply | ProtocolError:
        """Answer a reply that carries calls but arrived unfenced (§1.4 #15).

        The paste is ours and it PARSED - that is exactly what makes it
        dangerous. On a host whose whole-message copy markdown-processes text
        outside a fence, the reply comes back structurally perfect with its code
        quietly rewritten: `[label](target)` shapes link-stripped (a C++
        `[this](int a)` capture list simply gone), sometimes newlines collapsed
        as well. Nothing in the parse can tell a rewritten line from an intended
        one, so there is nothing to detect downstream - the corrupted text
        writes itself to disk, or `edit_file` loops on match_not_found and the
        model rewrites a file to "fix" a mismatch that was never there. The
        service preset says this host does that; the only safe reading of a
        missing fence is that this text has been through the renderer.

        Everything else mirrors _flattened_ingest, and mirrors it deliberately:
        same shared budget, same model-first escalation, same "the paste is not
        remembered" rule. What it does NOT do is quote a line back - there is no
        offending line to point at, and the damage is invisible in the text.
        """
        self._transport_bounces += 1
        attempt = self._transport_bounces
        self._session.append_event(
            "unfenced",
            attempt=attempt,
            bounced=attempt <= _MAX_TRANSPORT_BOUNCES,
            raw=text,
        )
        if attempt > _MAX_TRANSPORT_BOUNCES:
            return ProtocolError(
                "the reply arrived outside a code fence, so nothing in it ran."
                f" This is broken paste #{attempt} in a row - AgentClip has already"
                f" asked the chat to resend {_MAX_TRANSPORT_BOUNCES} times, so it has"
                " stopped asking. This service is marked require-fenced, and either the"
                " model keeps replying without a fence or this host strips the fence on"
                " copy. Ask the chat by hand to resend the whole reply inside one ~~~~"
                " fence, copy it from the raw/code view instead of the rendered message,"
                " or turn the require-fenced setting off for this service (F2)"
            )
        outbound = self._compose_results([_unfenced_result()], [], keep_chunks=True)
        return AutoReply(
            outbound,
            "the reply arrived outside a code fence, where this host rewrites code"
            " before the copy. Nothing ran - asked the chat to resend the whole reply"
            f" inside one ~~~~ fence ({attempt} of {_MAX_TRANSPORT_BOUNCES})",
        )

    def _chat_gate(self, reply: ParsedReply) -> Noise | None:
        """Require this session's chat name on every line the model was told to
        stamp it on: the EOM of a full reply and the ACK/NACK chunk lines.

        A reply whose EOM never arrived carries no chat name to check - that is
        the truncation signature, and it must stay on the truncated/NACK
        recovery path (section 5.2) rather than being silently dropped here."""
        if reply.kind in ("ack", "nack"):
            seen = reply.ack_chat
        elif reply.eom.present:
            seen = reply.eom.chat
        else:
            return None
        if seen is None:
            return Noise("missing-chat")
        if seen != self._chat_name:
            return Noise("wrong-chat")
        return None

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
        if decision not in (Decision.APPROVE_ALL_EDITS, Decision.APPROVE_ALWAYS):
            return
        # Remember a rule, then let it cascade - every other still-pending call
        # the new rule now allows is approved without asking again (a turn full
        # of edits takes one answer). APPROVE_ALL_EDITS is the same mechanism
        # with the pattern fixed: "every edit", rather than "calls like this one".
        assert planned.spec is not None
        if decision is Decision.APPROVE_ALL_EDITS:
            self._policy.auto_accept_edits = True  # so the status bar can say so
            self._policy.remember(EDITS_RULE)
        else:
            self._policy.remember(self._policy.always_rule(planned.spec, planned.call))
        for other in self._plan:
            if (
                other.needs_decision
                and other.decision is None
                and not other.aborted
                and other.spec is not None
                and self._policy.verdict(other.spec, other.call) == "auto"
            ):
                other.decision = Decision.APPROVE
                self._log_decision(other.call.id, "approved", "rule", None)

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

    def deliver_delegate_result(
        self,
        text: str,
        *,
        status: ResultStatus = "ok",
        code: str | None = None,
    ) -> StepResult:
        """Resume after Delegate: the sub-agent's deliverable becomes the
        delegate call's result body and the remaining calls execute.

        Every failure path of a delegation comes back through here too - an
        uncalibrated sub-agent chat, a new-chat click that did not verify, an
        aborted run - as status="error" with a code, so the model always learns
        what happened to the call it made instead of silently losing it."""
        self._require_phase(Phase.AWAITING_SUBAGENT, "deliver_delegate_result")
        assert self._exec is not None
        waiting = self._plan[self._exec.index]
        self._record(
            ToolResult(
                call_id=waiting.call.id,
                status=status,
                body=text,
                tool=waiting.call.tool,
                code=code,
            )
        )
        return self._run_plan(self._exec.index + 1)

    def request_cancel(self) -> None:
        """Ask the running batch to stop. THREAD-SAFE - and the only method that
        is: it just sets a threading.Event, so the host calls it from the UI
        thread while execute()/answer_user() is in flight on the worker thread.

        What the model ends up seeing:

        - the call being executed right now is interrupted if its handler
          cooperates (run_command polls the same event, kills its process tree
          and returns code=cancelled with the partial output); a handler that
          does not cooperate simply finishes and reports normally;
        - every call after it is skipped with a code=cancelled error result, so
          the reply says explicitly that they did not run;
        - the turn then finishes NORMALLY - the results are composed and
          returned as the usual Send step, which the host copies out. Cancelling
          is not an abort of the conversation, it is a short turn.

        A cancel requested while nothing is executing is a no-op: every plan run
        clears the flag before its first call.
        """
        self._cancel.set()

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
            service_key=self._preset().key or self._config.general.service,
            budget_chars=self._preset().max_paste_chars,
            auto_accept_edits=self._policy.auto_accept_edits,
            yolo=self._policy.yolo,
            mode=self._policy.mode,
            unattended=self._policy.unattended,
            session_dir=self._session.session_dir,
            last_outbound_chars=self._last_outbound_chars,
            has_extra_instructions=bool(self._preset().extra_instructions.strip()),
            instructions_armed=self._instructions_armed,
        )

    def set_yolo(self, enabled: bool) -> bool:
        """Toggle YOLO mode: auto-approve every tool call that would otherwise
        ask - everything a rule does not explicitly DENY, which stays refused.
        Session-scoped and legal in any phase: it only flips the policy flag, so
        it never races the state machine. It does not revisit decisions already
        made this turn; it governs every plan built afterwards. Returns the new
        state."""
        self._policy.yolo = enabled
        self._session.append_event("yolo", enabled=enabled)
        return enabled

    def set_unattended(self, enabled: bool) -> bool:
        """Toggle "nobody is at the keyboard": every call that would have opened
        a gate is auto-denied instead, because a question nobody answers must not
        become a silent yes. Allow rules still run and deny rules still deny.

        set_yolo's shape, and set_permission_mode's note: legal in any phase, not
        retroactive (a gate already pending stays pending), and announced to the
        model on the next results payload unless the session has not started -
        IDLE means this is simply the state the session BEGINS in, which is
        nothing to announce. Returns the new state."""
        self._policy.unattended = enabled
        self._session.append_event("unattended", enabled=enabled)
        if self._phase is not Phase.IDLE:
            self._mode_note = _UNATTENDED_NOTES[enabled]
        return enabled

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Set the session's permission mode: "build" (the default builder - the
        ruleset exactly as the user wrote it) or "plan" (exploration only - the
        built-in overlay denies every edit, command, MCP call and delegation).

        set_yolo's shape exactly, and for its reasons: legal in any phase because
        it only writes a policy field, and NOT retroactive - a gate already
        pending stays pending, and the new mode governs every verdict computed
        after it. The model is told at the next results payload (see
        _take_mode_note); it is not told at all if the session ends first, which
        is fine because each denial body explains itself. Returns the new mode.

        IDLE is the one phase that arms no note. Before start_task there is no
        conversation to interrupt: whatever mode is set then is simply the mode
        this session STARTED in (the user dialled it in at the start prompt, or
        [approval] mode did), it is in force from the first verdict of the first
        turn, and a "the mode is now X" note in the very first results payload
        would be announcing a change that never happened."""
        self._policy.mode = mode
        self._session.append_event("permission_mode", mode=mode)
        if self._phase is not Phase.IDLE:
            self._mode_note = _MODE_NOTES[mode]
        return mode

    def arm_extra_instructions(self) -> ArmResult:
        """Toggle "the next payload also carries the preset's extra_instructions".

        The re-inject half of the feature (tui.md 3.4h): the instructions go out
        once with the bootstrap, and a long session on a host that mangles code
        drifts back to mangling it. This arms a ONE-SHOT reminder, spent by the
        next outbound of any kind - results or a typed follow-up, whichever the
        session reaches first (see _take_instructions_note).

        A toggle rather than a latch, because the only way to see the flag is
        the status bar and the only way out of a press the user did not mean has
        to be the same key. Refuses in two cases, each named in the return so the
        UI can explain rather than sit there: IDLE (there is no next payload -
        and once there is, it is the bootstrap, which embeds the instructions
        anyway) and a preset with nothing to re-inject.
        """
        if self._phase is Phase.IDLE:
            return "no-session"
        if not self._preset().extra_instructions.strip():
            return "no-instructions"
        self._instructions_armed = not self._instructions_armed
        self._session.append_event("extra_instructions", armed=self._instructions_armed)
        return "armed" if self._instructions_armed else "disarmed"

    # -- planning ----------------------------------------------------------------

    def _build_plan(self, reply: ParsedReply) -> list[_Planned]:
        # Every turn starts by re-asking what the service allows now (§11.9).
        self._sync_ctx()
        plan: list[_Planned] = []
        # Ranged edits accepted so far, per file, in reply order: what the
        # bottom-to-top and non-overlap rules are checked against. Rebuilt every
        # plan, because both rules are about ONE reply.
        ranged: dict[str, list[tuple[int, int]]] = {}
        # Handed to the tools through the context (see ToolContext.
        # numbered_slices) and filled as the plan is built, so the preview that
        # this same loop renders a few lines below already sees its own call's
        # entry - gate diff and write can never be of different edits.
        slices: dict[int, str] = {}
        self._ctx.numbered_slices = slices
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
            # Intercepted by name during execution; never pending, never gated.
            # `delegate` is NOT one of them: it answers to the `task` permission
            # like any other tool.
            if call.tool in ("ask_user", "task_done"):
                plan.append(
                    _Planned(call, spec, PendingAction(call, "auto", "", "handled by AgentClip"))
                )
                continue
            if call.tool == "replace_lines":
                # BEFORE the policy, deliberately: a call that is going to be
                # refused must not first stop the turn at an approval gate and
                # make the user read a diff of an edit that will never run.
                refusal = self._ranged_edit_guard(call, ranged, slices)
                if refusal is not None:
                    plan.append(
                        _Planned(
                            call,
                            spec,
                            PendingAction(call, "auto", "", "pre-resolved unverified range"),
                            pre_result=refusal,
                        )
                    )
                    self._log_decision(call.id, "denied", "ranged-edit", None)
                    continue
            verdict = self._policy.verdict(spec, call)
            if verdict in DENY_VERDICTS:
                # A rule said no, or the permission mode did. Pre-resolved, not
                # gated: there is nothing to ask. The rest of the turn still runs
                # - only an interactive rejection aborts it.
                reason, source, pre_result = self._denial(verdict, spec, call)
                plan.append(
                    _Planned(
                        call,
                        spec,
                        PendingAction(call, "auto", "", reason),
                        pre_result=pre_result,
                    )
                )
                self._log_decision(call.id, "denied", source, None)
                continue
            if verdict == "auto":
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
            if not preview:
                # A read-only tool can gate too - the rules decide, not the
                # approval kind - and it has no diff and no command line to show.
                _, resource = self._policy.target(spec, call)
                preview = f"{call.tool} {resource}".rstrip()
            plan.append(
                _Planned(
                    call,
                    spec,
                    PendingAction(
                        call,
                        kind,
                        preview,
                        None,
                        always_pattern=self._policy.always_rule(spec, call).pattern,
                    ),
                    needs_decision=True,
                )
            )
        return plan

    # -- the ranged-edit guard ---------------------------------------------------
    #
    # `replace_lines` is the one tool that cannot check its own work: "lines
    # 88-90" is true of every file with ninety lines, so a stale line number
    # writes real code into the wrong place and reports success. Everything that
    # makes it safe is here and in engine/numbered.py, in three layers:
    #
    #   record   at compose time, the numbered lines that SURVIVED into the
    #            payload (post-truncation, verified against the file) plus the
    #            file's hash - _record_numbered_reads;
    #   plan     before the approval gate, four refusals: never read, read but
    #            not this range, file since changed, and the ordering rules that
    #            keep several edits to one file from renumbering each other -
    #            _ranged_edit_guard;
    #   apply    at the instant of the write, the served text is compared with
    #            what is on disk, which is the only thing that catches a
    #            run_command earlier in the SAME reply moving the file -
    #            fs_tools._apply_replace_lines via ctx.numbered_slices.
    #
    # The refusals are pre-resolved errors, not exceptions: the rest of the turn
    # still runs, and the model is told exactly which door shut and what to do -
    # every hint here ends in "read it again with numbered: yes", because that
    # is always the recovery and a guess is always the wrong one.

    def _numbered_target(self, path_param: str) -> tuple[str, str] | None:
        """(record key, current LF-normalised text) for a path, or None if unreadable.

        The key is the workspace-relative path, so `./src/a.py` and `src/a.py`
        are one file - the model that reads it one way and edits it the other
        must not be refused for a spelling.
        """
        try:
            abs_path = self._workspace.resolve_read(path_param)
            raw = self._host.read_bytes(abs_path)
            rel = abs_path.relative_to(self._workspace.root).as_posix()
        except (SandboxViolation, OSError, ValueError):
            return None
        return _norm_path(rel), raw.decode("utf-8", errors="replace").replace("\r\n", "\n")

    def _ranged_edit_guard(
        self,
        call: ToolCall,
        ranged: dict[str, list[tuple[int, int]]],
        slices: dict[int, str],
    ) -> ToolResult | None:
        """Refuse a replace_lines the model cannot possibly have verified, else
        record its expected text and let it through."""
        path_param = call.params.get("path", "")
        try:
            start = int(call.params["start"].strip())
            end = int(call.params["end"].strip())
        except (KeyError, ValueError, AttributeError):
            return None  # missing/non-numeric params are the handler's error to give
        if start < 1 or end < start:
            return None  # ...and so is an inside-out range, which it words better
        disp = path_param or "(no path)"
        target = self._numbered_target(path_param)
        served = self._numbered_reads_served.get(target[0]) if target else None
        if served is None:
            return error_result(
                call,
                "unverified_range",
                f"replace_lines refused: nothing of {disp} was read with a line-number"
                " gutter in the results you were just given.",
                "call read_file with numbered: yes for the range you want, then send"
                " the edit in your NEXT reply.",
            )
        if any(n not in served.lines for n in range(start, end + 1)):
            return error_result(
                call,
                "unverified_range",
                f"replace_lines refused: lines {start}-{end} are not inside what you"
                f" were shown of {disp} (numbered lines: {describe_ranges(served.lines)}).",
                "read_file that range with numbered: yes, then send the edit in your"
                " NEXT reply.",
            )
        assert target is not None  # served is not None => the file was readable
        if content_hash(target[1]) != served.content_hash:
            return error_result(
                call,
                "stale_read",
                f"replace_lines refused: {disp} has changed since you read it, so those"
                " line numbers no longer point where you think.",
                "read_file it again with numbered: yes, then resend the edit.",
            )
        key = target[0]
        for prev_start, prev_end in ranged.get(key, ()):
            if start >= prev_start:
                return error_result(
                    call,
                    "bad_edit_order",
                    f"replace_lines refused: this reply already edits {disp} at lines"
                    f" {prev_start}-{prev_end}, and {start}-{end} is not BELOW it."
                    " Edits to one file must go bottom to top.",
                    "reorder the calls highest start first, so an applied edit can"
                    " never renumber a range that has not run yet.",
                )
            if end >= prev_start:
                return error_result(
                    call,
                    "bad_edit_order",
                    f"replace_lines refused: lines {start}-{end} overlap the"
                    f" {prev_start}-{prev_end} edit of {disp} earlier in this reply.",
                    "merge the two into one call, or pick ranges that do not touch.",
                )
        ranged.setdefault(key, []).append((start, end))
        slices[call.id] = "\n".join(served.lines[n] for n in range(start, end + 1))
        return None

    def _record_numbered_reads(self, payload: str, reads: Sequence[tuple[int, str]]) -> None:
        """Replace the served-reads record with what THIS payload really carries.

        Wholesale, never merged, for the reason given where the field is
        declared. Two honesty rules decide what goes in:

        - the lines come from the RENDERED payload, so anything truncation ate
          is simply not there (engine/numbered.py explains why the read's own
          header cannot be trusted for this);
        - each surviving line is compared against the file as it stands NOW, at
          the end of the turn, and dropped if it differs. A later call in the
          same turn may have rewritten a file an earlier call read, and a record
          that disagrees with the disk is worse than no record at all - it is a
          promise the apply-time check would have to break.
        """
        served: dict[str, ServedRead] = {}
        by_call = surviving_numbered_lines(payload, [cid for cid, _ in reads])
        for call_id, path_param in reads:
            shown = by_call.get(call_id)
            if not shown:
                continue
            target = self._numbered_target(path_param)
            if target is None:
                continue
            key, text = target
            current, _ = split_lines(text)
            kept = {n: t for n, t in shown.items() if 0 < n <= len(current) and current[n - 1] == t}
            if not kept:
                continue
            record = served.get(key)
            if record is None:
                served[key] = ServedRead(content_hash(text), dict(kept))
            else:
                record.lines.update(kept)
        self._numbered_reads_served = served

    def _denial(
        self, verdict: str, spec: ToolSpec, call: ToolCall
    ) -> tuple[str, str, ToolResult]:
        """The (transcript reason, audit source, model-facing result) for one
        refusal. Three causes, three answers: the model can only choose a
        different route if it is told which door was shut."""
        if verdict == "deny_plan":
            return ("denied by plan mode", "plan", self._denied_by_plan_result(call))
        if verdict == "deny_unattended":
            return (
                "auto-denied (unattended)",
                "unattended",
                self._denied_unattended_result(spec, call),
            )
        return ("denied by rule", "rule", self._denied_by_rule_result(spec, call))

    def _denied_by_plan_result(self, call: ToolCall) -> ToolResult:
        """Plan mode's refusal. It names the mode rather than a rule, because
        nothing is wrong with the call - the user simply is not ready to run it -
        and it points at the reads that DO work, so the turn stays productive."""
        return ToolResult(
            call_id=call.id,
            status="denied",
            body=(
                "plan mode is active: the user is only exploring and no changes may be"
                " made.\nhint: explore with read_file/list_dir/glob/grep and present your"
                " plan via task_done or ask_user; the user can switch modes to enable"
                " execution."
            ),
            tool=call.tool,
        )

    def _denied_unattended_result(self, spec: ToolSpec, call: ToolCall) -> ToolResult:
        """The unattended toggle's refusal: the gate this call would have opened
        had nobody to answer it. The relevant rules ride along (the rule-deny
        path's payload), because "which rules would have let this through" is
        exactly what the model needs to keep working."""
        return ToolResult(
            call_id=call.id,
            status="denied",
            body=(
                "auto-denied: the user is away (unattended is on) and this call is not"
                " covered by an allow rule.\nHere are some of the relevant rules "
                + self._policy.denied_rules_json(spec, call)
                + "\nhint: do not retry unchanged; continue with calls that allow rules"
                " cover, or finish with task_done and list what was blocked."
            ),
            tool=call.tool,
        )

    def _denied_by_rule_result(self, spec: ToolSpec, call: ToolCall) -> ToolResult:
        """OpenCode's DeniedError payload, verbatim: the model is told a rule
        forbade this call and shown the rules that could apply, so it can pick a
        different route instead of retrying the same one."""
        return ToolResult(
            call_id=call.id,
            status="denied",
            body=(
                "The user has specified a rule which prevents you from using this"
                " specific tool call. Here are some of the relevant rules "
                + self._policy.denied_rules_json(spec, call)
            ),
            tool=call.tool,
        )

    def _auto_reason(self, spec: ToolSpec, call: ToolCall) -> tuple[str, str]:
        # Which rule let it through is the audit trail's whole point here:
        # "allowed" without the pattern is unreviewable. Anything reaching this
        # line without an allow rule was answered by YOLO.
        rule = self._policy.rule_for(spec, call)
        if rule.action == "allow":
            return f'allowed by rule {rule.permission}["{rule.pattern}"]', "rule"
        return "YOLO mode (auto-approve all)", "yolo"

    def _parse_issue_result(self, call: ToolCall) -> ToolResult:
        fatal = [i for i in call.issues if i.kind in _FATAL_ISSUES]
        if any(i.kind == "client_mangled_heredoc" for i in fatal):
            return _client_mangled_result(call, fatal)
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
            " and/or key << TAG heredocs, then ===CLIP:END==="
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
        # A cancel governs the run it was pressed during and nothing else: a
        # stray one from an idle moment (or a leftover from the previous turn)
        # must never poison this run.
        self._cancel.clear()
        for i in range(start, len(self._plan)):
            p = self._plan[i]
            call = p.call
            if p.pre_result is not None:
                self._record(p.pre_result)
                continue
            if self._cancel.is_set():
                # Checked between calls, so even handlers that cannot be
                # interrupted stop the batch at the next boundary. ask_user,
                # delegate and task_done are skipped too: a cancelled batch must
                # not park on a question, start a sub-agent, or declare the task
                # complete.
                self._record(_cancelled_skip_result(call))
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
            if call.tool == "delegate":
                task = call.params.get("task", "").strip()
                if not task:
                    self._record(
                        error_result(
                            call,
                            "missing_param",
                            "missing required parameter: task",
                            "resend delegate with a task parameter.",
                        )
                    )
                    continue
                exec_.index = i
                self._set_phase(Phase.AWAITING_SUBAGENT)
                return Delegate(
                    task=task,
                    context=call.params.get("context") or None,
                    call_id=call.id,
                )
            if call.tool == "task_done":
                exec_.done_summary = call.params.get("summary", "")
                exec_.done_result = call.params.get("result", "")
                # It produces no ToolResult (there is nobody left to tell), so
                # its progress "done" is emitted by hand or its row never
                # resolves on screen.
                self._progress(call.id, call.tool, "done", "ok")
                continue
            assert p.spec is not None
            self._progress(call.id, call.tool, "running")
            result = p.spec.handler(self._ctx, call)
            if result.status == "error" and call.tool in _MUTATING_TOOLS:
                exec_.failed_paths.add(_norm_path(call.params.get("path", "")))
            if result.status == "ok" and numbered_requested(call):
                # Noted, not yet trusted: what the model can actually SEE of it
                # is only known once the payload has been rendered and fitted
                # (_record_numbered_reads).
                exec_.numbered_reads.append((call.id, call.params.get("path", "")))
            self._record(result)
        return self._finish_turn()

    def _finish_turn(self) -> StepResult:
        exec_ = self._exec
        reply = self._reply
        assert exec_ is not None and reply is not None
        if exec_.backup_started:
            self._backups.finish_turn()
        results = list(exec_.results)
        if reply.truncated and not _client_mangled(reply):
            # A mangled reply always looks truncated (the client ate the EOM
            # line), but "your reply was cut off - resend the rest" is exactly
            # the advice that loops here. The per-call result says the truth.
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
            self._session.append_event(
                "task_done", summary=exec_.done_summary, result_chars=len(exec_.done_result)
            )
            # No results means no payload, so the pending notes are NOT taken:
            # they keep waiting for one that actually goes out (a follow-up after
            # task_done reopens the session - protocol.md section 8).
            outbound = (
                self._compose_results(
                    results,
                    notes + self._take_mode_note() + self._take_instructions_note(),
                    exec_.numbered_reads,
                )
                if results
                else None
            )
            self._set_phase(Phase.DONE)
            return Done(exec_.done_summary, outbound, exec_.done_result)
        outbound = self._compose_results(
            results,
            notes + self._take_mode_note() + self._take_instructions_note(),
            exec_.numbered_reads,
        )
        self._set_phase(Phase.AWAITING_REPLY)
        return Send(outbound)

    def _take_mode_note(self) -> list[str]:
        """The pending permission note (a mode change or an unattended toggle),
        once - consumed as it is handed to a payload, so the model is told about
        a change exactly one time. One slot for both: the latest change is the
        one that describes the session the model is about to act in."""
        if self._mode_note is None:
            return []
        note, self._mode_note = self._mode_note, None
        return [f"note: {note}"]

    def _take_instructions_note(self) -> list[str]:
        """The armed extra-instructions reminder, once - same contract as
        _take_mode_note, and taken alongside it wherever a payload is composed.
        Both pending at the same time is legal and rides as two note lines."""
        if not self._instructions_armed:
            return []
        self._instructions_armed = False
        text = self._preset().extra_instructions.strip()
        return [f"note: {_INSTRUCTIONS_NOTE_PREFIX} {text}"] if text else []

    def _compose_results(
        self,
        results: list[ToolResult],
        notes: list[str],
        numbered_reads: Sequence[tuple[int, str]] = (),
        *,
        keep_chunks: bool = False,
    ) -> Outbound:
        """Render one results payload, and settle the chunk cache around it.

        ``keep_chunks`` is for the payloads that are not a turn: a transport
        bounce (`reply_flattened`/`reply_unfenced`) carries one synthetic result
        and executed nothing, so treating it as "a payload with no truncations
        and no fetch" would throw away a cache the model is one resend away from
        using.
        """
        minted = self._mint_chunks(results)
        markers = {entry.call_id: entry.marker for entry in minted}
        capped = fit_results(results, self._limits().max_result_chars, markers)
        next_turn = self._turn + 1
        try:
            outbound = self._composer.results(next_turn, capped, notes, markers)
        except BudgetExceeded as exc:
            # The composer's line-boundary fitting could not get under budget
            # (e.g. a single enormous line). Cut harder, mid-line if needed.
            self._session.append_event("error", detail=f"results over budget, refitting: {exc}")
            budget = self._preset().max_paste_chars
            per_result = max(120, (budget - 600) // max(len(capped), 1) - 150)
            outbound = self._composer.results(
                next_turn, fit_results(capped, per_result, markers), notes, markers
            )
        self._turn = next_turn
        payload = _CHUNK_SEPARATOR.join(outbound.chunks)
        # After the fitting, never before: the record has to describe the text
        # that is really going out, and the refit above can cut a body a second
        # time (engine/numbered.py). The chunk cache is settled here for exactly
        # the same reason.
        self._record_numbered_reads(payload, numbered_reads)
        self._update_chunk_cache(payload, minted, results, keep=keep_chunks)
        self._register_outbound(outbound)
        return outbound

    def _mint_chunks(self, results: Sequence[ToolResult]) -> list[CachedChunks]:
        """Pre-slice every body that a truncation pass might go after.

        Minted BEFORE composing because the marker has to name the part count,
        and the part count is a fact about the original body - which is exactly
        the thing that stops existing the moment either pass runs. Ids are spent
        eagerly and dropped freely: which of these bodies was really cut is not
        knowable until the payload has been rendered and measured, so the honest
        order is mint, render, then keep what survived. Unused ids are simply
        never reused, which is why the ids in a session are not consecutive.
        """
        chunk_chars = chunk_chars_for(
            self._preset().max_paste_chars, self._limits().max_result_chars
        )
        minted: list[CachedChunks] = []
        for result in results:
            if len(result.body) <= _CHUNK_MINT_FLOOR:
                continue
            chunk_id = f"c{self._next_chunk_id}"
            self._next_chunk_id += 1
            minted.append(
                CachedChunks.of(
                    chunk_id,
                    result.body,
                    call_id=result.call_id,
                    turn=self._turn + 1,
                    tool=result.tool,
                    chunk_chars=chunk_chars,
                )
            )
        return minted

    def _update_chunk_cache(
        self,
        payload: str,
        minted: Sequence[CachedChunks],
        results: Sequence[ToolResult],
        *,
        keep: bool,
    ) -> None:
        """THE EVICTION RULE for the fetch_chunk cache, in the one place it applies.

        A cache with no expiry is a memory leak that also hands the model stale
        ids for output three tasks old; a cache cleared every turn cannot be
        fetched from at all, because a fetch by definition happens in a LATER
        turn than the truncation. So, in order:

        1. REPLACED when this payload truncated something. The markers minted
           above are checked against the RENDERED text - derived truth, like
           `_record_numbered_reads` - so an id is only cached when the model can
           actually see it, and the newest truncations are the ones worth
           holding. Wholesale, never merged: "the output you were just handed"
           has to mean literally that.
        2. SURVIVES a payload answering a turn that CALLED fetch_chunk. A fetch
           must not evict what it is fetching, and a model working through parts
           1..K would otherwise lose the cache to its own first fetch.
        3. CLEARED otherwise - a turn came and went, nothing was cut and nothing
           was fetched, so whatever is held is output the model has moved on
           from. Fetching an evicted id then gets the handler's "this expired,
           re-run the original tool" rather than the wrong text.

        Mutated in place, never rebound: the ToolContext holds this same dict.
        """
        survived = {entry.chunk_id: entry for entry in minted if entry.marker in payload}
        if survived:
            self._chunk_cache.clear()
            self._chunk_cache.update(survived)
            return
        if keep or any(r.tool == "fetch_chunk" for r in results):
            return
        self._chunk_cache.clear()

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
        # Every resolution of every call passes through here - the executed
        # ones, the denied ones, the skipped ones - which is exactly the set a
        # watcher needs to see resolve.
        self._progress(result.call_id, result.tool, "done", result.status)
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
        re-ingest of our own text is dropped as Noise("own-outbound")."""
        self._session.write_outbound(outbound.turn, _CHUNK_SEPARATOR.join(outbound.chunks))
        self._session.append_event(
            "outbound",
            kind=outbound.kind,
            turn=outbound.turn,
            total_chars=outbound.total_chars,
            chunks=len(outbound.chunks),
        )
        for chunk in outbound.chunks:
            digest = normalized_hash(chunk)
            if digest not in self._outbound_hashes:
                self._outbound_hashes.append(digest)
        self._last_outbound_chars = outbound.total_chars

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


def _cancelled_skip_result(call: ToolCall) -> ToolResult:
    """The result a call gets when the user cancelled before it started."""
    return error_result(
        call,
        "cancelled",
        "skipped: the user cancelled this batch before this call ran, so it had no effect.",
        "nothing changed for this call - ask what the user wants instead, or resend"
        " it only if they still want it.",
    )


def _client_mangled(reply: ParsedReply) -> bool:
    """True when the chat client corrupted this reply in transport."""
    return any(i.kind == "client_mangled_heredoc" for c in reply.calls for i in c.issues)


def _client_mangled_result(call: ToolCall, fatal: list[ParseIssue]) -> ToolResult:
    """The one parse failure that happened before the clipboard: the chat client
    flattened the reply into a single HTML element (protocol.md section 1.4).

    Addressed to the USER, not the model. "Resend this call" - the answer to
    every other parse error - is an infinite loop here: the model sends valid
    text, the client mangles it identically, forever.
    """
    message = (
        f"call id={call.id} arrived corrupted and was NOT executed.\n"
        "The chat client mangled this reply in transport: it read the heredoc tag"
        " as an HTML start tag and absorbed the rest of the reply -"
        " ===CLIP:END===, the EOM line, the closing fence - into it as sorted"
        " attributes. The words come back in ASCII order, so nothing in the"
        " parameter can be recovered.\n"
        + "\n".join(i.detail for i in fatal)
        + "\nThe model's output was valid; resending this reply UNCHANGED will not"
        " help, because the client will mangle it the same way again.\n"
        "offending block (first lines):\n"
        + "\n".join(call.raw.split("\n")[:10])
    )
    hint = (
        "do not just resend: change the shape. Send this call again with every"
        " parameter as a single-line `key: value` - no heredoc, so there is no tag"
        " for the client to mistake for HTML. If a value truly needs newlines,"
        " keep the heredoc but put a space between << and the tag."
    )
    return error_result(call, "client_mangled_reply", message, hint)


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


def _offending_line(text: str, line_no: int) -> str:
    """The glued-together line a `flattened_reply` issue points at, quoted.

    Read from the INGESTED TEXT rather than carried on the ParseIssue: the
    parser reports anomalies, it does not ship evidence, and keeping it that way
    means parse_reply's output stays a description of structure instead of
    growing a copy of the input. Line numbers are 1-based over the NORMALIZED
    text, which is what the parser counted, so normalization is re-applied here
    rather than assumed away (a CRLF reply would otherwise be off by nothing and
    a BOM by one, which is exactly the kind of bug that only shows up on someone
    else's machine).

    Returns "" for a line number that does not exist, so a quote is always
    optional to the caller and never a crash on the error path.
    """
    lines = normalize(text).split("\n")
    if not 1 <= line_no <= len(lines):
        return ""
    line = lines[line_no - 1].strip()
    if len(line) > _FLATTENED_QUOTE_CHARS:
        line = line[:_FLATTENED_QUOTE_CHARS].rstrip() + "..."
    return line


def _flattened_result(quoted: str) -> ToolResult:
    """The id=0 reply_flattened error result (protocol.md sections 1.4 #14, 4).

    Addressed to the MODEL, unlike `client_mangled_reply`: there the client
    destroys valid output the same way every time and resending is an infinite
    loop, here the usual cause is the fence the model left off, and putting one
    back costs it one reply. The two things it must be told are that NOTHING
    ran (so it resends everything, not the tail - the truncation reflex is
    wrong here) and that the fence goes around the WHOLE reply including the
    EOM line, which is the part models get wrong when they do refence: an EOM
    left outside the fence is flattened onto the fence line and the next paste
    fails identically.

    The quoted line is the model's own text, so it can see which break went
    missing. It is safe inside the payload because result bodies are heredoc-
    framed with a collision-free tag (section 4), so the `===CLIP:` fragments
    riding in it cannot be read as part of our envelope.
    """
    lines = [
        "Your last reply arrived with its line breaks GONE - several CLIP blocks"
        " were glued onto one line, so they were never parsed.",
        "NOTHING in that reply ran. No file was read, no file was changed, no"
        " command was executed.",
    ]
    if quoted:
        lines.append("This is the line that arrived:")
        lines.append(quoted)
    lines.append(
        "The cause is markdown: CLIP blocks written as ordinary message text are"
        " rendered as prose, where single newlines are not line breaks, and the"
        " copy button then hands over one long line."
    )
    hint = (
        "resend the ENTIRE reply - every ===CLIP:CALL block AND the final"
        " ===CLIP:EOM line - inside ONE ~~~~ fence, with nothing of the protocol"
        " outside it. Do not resend only part of it and do not use several"
        " fences: nothing ran, so all of it is still owed."
    )
    return ToolResult(
        call_id=0, status="error", body="\n".join(lines) + f"\nhint: {hint}", code="reply_flattened"
    )


def _unfenced_result() -> ToolResult:
    """The id=0 reply_unfenced error result (protocol.md §1.4 #15, §4).

    Addressed to the MODEL, like `reply_flattened` and for the same reason: the
    fix is one fenced resend. Two things have to be said that the model would
    otherwise get wrong. First that NOTHING ran - the reply parsed, so its
    natural reading of any answer is "some of it worked". Second that this is
    not a parse complaint: told only "your reply was refused", a model rewrites
    its perfectly good CALL blocks, hunting a grammar mistake that does not
    exist, and sends the rewrite unfenced again. Naming the real mechanism -
    prose processing between the chat and the relay, with the bracket-paren
    example that makes it concrete - is what turns the next reply into a resend
    rather than a rewrite.
    """
    lines = [
        "Your last reply arrived OUTSIDE a code fence, so NOTHING in it ran. No"
        " file was read, no file was changed, no command was executed.",
        "This is a transport-safety gate, NOT a parse failure: the reply itself"
        " parsed fine.",
        "On this chat, text outside a fence is processed as prose before it"
        " reaches the relay, and that processing corrupts code invisibly:"
        " bracket-paren shapes like [label](target) are stripped to just the"
        " target (a C++ capture like [this](int a) loses its capture list), and"
        " line breaks can collapse. The result parses perfectly and is wrong,"
        " which is why it cannot be run.",
    ]
    hint = (
        "resend the ENTIRE reply - every ===CLIP:CALL block AND the final"
        " ===CLIP:EOM line - inside ONE ~~~~ fence, with nothing of the protocol"
        " outside it."
    )
    return ToolResult(
        call_id=0,
        status="error",
        body="\n".join(lines) + f"\nhint: {hint}",
        tool="",
        code="reply_unfenced",
    )


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
    "AutoReply",
    "ChunkAck",
    "Decision",
    "Delegate",
    "Done",
    "Engine",
    "EngineStateError",
    "IngestResult",
    "NewTurn",
    "Noise",
    "PendingAction",
    "PermissionMode",  # re-exported: the TUI reads it off StatusSnapshot.mode
    "Phase",
    "ProtocolError",
    "Send",
    "StatusSnapshot",
    "StepResult",
]
