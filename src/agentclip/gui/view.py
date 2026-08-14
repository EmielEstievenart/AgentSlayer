"""GuiView: the pywebview adapter that implements the three UI ports.

The GUI's ``MainScreen``. It is the same arrangement, structurally and for the
same reasons (``docs/design/gui.md`` §0/§1): the session orchestration lives in
:class:`~agentclip.app.SessionController` and the browser automation in
:class:`~agentclip.automation.controller.AutomationController`, and this object
is the *view* - it owns both controllers, implements ``ChatView`` for the first,
``AutomationView`` + ``AutomationHost`` for the second, and turns every call
into an event on the JS bridge. State flows one way (the controller pushes a
``SessionView`` through ``render_state`` and the page repaints from it); user
input flows back through the ``js_api`` methods the runner marshals onto this
object's loop.

**Threading.** Everything below runs on one of three kinds of thread and knows
which: the GUI's asyncio loop (every ``async def``, every ``js_api``-originated
call after the runner has marshalled it), the engine's worker thread (the
``call_*`` family), and the automation's watcher/poller threads (every
``AutomationView`` paint). The last two are exactly the port methods with a
thread contract, and all of them do one thing: ``bridge.send``, which is
non-blocking and ordered by construction (``gui/bridge.py``). Nothing here
touches the page directly.

**What this slice reduces, honestly.** Slice 2 is the first live conversation,
not parity, so a handful of ``ChatView`` methods are implemented smaller than
the TUI's rather than left as a silent ``pass`` that would strand a controller
flow. Each one says so at its own definition and they are listed together in
``docs/design/gui.md`` §2: what is left of that list is the ``/identify``
overlay and the elements crops (kinds, not pictures). Everything a *turn*
passes through - the transcript, the gate, the delivery, the watcher, the
prompts - is the real thing, and so are the sidebar, the status bar's ten
segments and the harness log pane (parity increment 2) and the window tabs,
the per-window transcripts and the session summary (parity increment 3).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from agentclip.app import SessionController, SessionSpec, SessionView
from agentclip.app.types import EngineRequest, SessionRef
from agentclip.app.view import RunCall, Severity
from agentclip.automation.controller import AutomationController, DetectorPoller
from agentclip.automation.harness_log import (
    KIND_ARMED,
    KIND_CLIPBOARD,
    KIND_SESSION,
    HarnessEntry,
)
from agentclip.automation.loop_state import LOOP_TRANSITIONS, LoopState
from agentclip.automation.ops import ElementClick
from agentclip.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.config import Config, ServicePreset, default_profile_dir, save_active_services
from agentclip.engine.engine import Decision, Engine, PendingAction
from agentclip.gui.bridge import Bridge
from agentclip.protocol.parser import looks_like_protocol
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.screen.capture import CaptureError, RegionImage, capture_region
from agentclip.screen.detector import ScreenDetector, build_detector
from agentclip.screen.focus import foreground_window
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.profile_store import load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, can_delegate, missing
from agentclip.screen.template import (
    RegionMatch,
    Template,
    find_all_in_region,
    match_rect,
    same_element,
)

# The finish-detector poll cadence, the TUI's own (tui/screens/main.py). Spelled
# here rather than imported: the two shells may not import each other, and this
# is a number the detector composition needs, not a shared decision.
_BUSY_POLL_S = 0.5

# How many matches of one appearance are worth collecting - the question a
# search answers is "one, or more than one?" (see ``find_all``).
_MAX_MATCHES = 8

# Where a model's stated reason is clipped at the gate. The tools layer's own
# number (``tools/shell.py``), spelled here with ``_reason_line``.
_REASON_PREVIEW_CHARS = 200

# The browser windows the automation drives, as the AutomationController keys
# them. Opaque strings down there by design; the GUI names the same two the TUI
# does, because a tab IS a browser window in both shells (tui.md §1.6): it
# exists before any session, keeps its own service, and survives ``/new``.
MASTER_WINDOW = "m1"
SUBAGENT_WINDOW = "m1-s1"

_WINDOW_SLOTS: dict[str, AgentSlot] = {
    MASTER_WINDOW: AgentSlot.MASTER,
    SUBAGENT_WINDOW: AgentSlot.SUBAGENT,
}

# What the tab bar cycles, in the order it is on screen: every master, then the
# selected master's sub-agent windows. A tuple rather than ``_WINDOW_SLOTS``'s
# key order, so the picture cannot silently re-order itself the day the mapping
# grows an entry (``WindowTabs.order``).
_WINDOW_ORDER: tuple[str, ...] = (MASTER_WINDOW, SUBAGENT_WINDOW)

MASTER_VIEW = "master"

# The two browser windows, as the tabs and the DETECTION heading name them (the
# TUI's ``_WINDOW_NAMES``). The heading is about the LIVE window, which parts
# company with the SELECTED one - what the user is reading - for the whole of a
# delegation; the tabs are about the selected one. Two pointers, one vocabulary.
_WINDOW_NAMES = {MASTER_WINDOW: "MASTER", SUBAGENT_WINDOW: "SUB-AGENT"}

# A window tab's state, derived from its run history rather than stored, and the
# glyph the label carries for it (``MainScreen._window_label``). The three-state
# distinction is the parity requirement; the BMP glyphs are the TUI's terminal
# constraint and are kept here only because the page also colours from ``state``
# and a label that reads the same in both shells is a cheaper assertion surface
# than one that does not (ui-briefs/tabs-delegation-summary.md §7).
WINDOW_STATE_GLYPH: dict[str, str] = {
    "none": "",
    "running": "▶ ",
    "ok": "✓ ",
    "failed": "✗ ",
}

# == the sidebar's words ======================================================
# Everything from here to ``_service_options`` is ``tui/widgets/sidebar.py``'s,
# spelled again for the reason ``_reason_line`` and ``_distinct_rects`` are: the
# two shells may not import each other (tests/test_layering.py), and widening
# that boundary so a frontend could reach another frontend's display strings
# would be a worse trade than a block of literals with a comment saying where
# the original lives. Three of them are deliberately NOT verbatim - the TUI's
# say "F2", and this shell has no service editor behind that key until increment
# 7, so they name the door instead of a key that would do nothing.

# The STATE rail, in LOOP order rather than declaration order. A tuple rather
# than ``list(LoopState)`` for the sidebar's reason: the rail is a picture of
# the round trip, and the day the enum grows a value in the middle the picture
# must not silently re-order itself.
_LOOP_ORDER: tuple[LoopState, ...] = (
    LoopState.IDLE,
    LoopState.AUTO_INSERT,
    LoopState.MANUAL_INSERT,
    LoopState.WAIT_SEND,
    LoopState.WAIT_GENERATE,
    LoopState.AUTO_COPY,
    LoopState.MANUAL_COPY,
    LoopState.INTERPRETING,
)
_LOOP_LABEL: dict[LoopState, str] = {
    LoopState.IDLE: "idle",
    LoopState.AUTO_INSERT: "auto insert",
    LoopState.MANUAL_INSERT: "manual insert",
    LoopState.WAIT_SEND: "wait send",
    LoopState.WAIT_GENERATE: "wait generate",
    LoopState.AUTO_COPY: "auto copy",
    LoopState.MANUAL_COPY: "manual copy",
    LoopState.INTERPRETING: "interpreting",
}

# The standing banner, the paste flash's quiet sibling: same place in the column,
# opposite temperament. It never blinks - it is a fact about the whole app that
# has to survive being looked at for an hour.
DISARMED_BANNER_TEXT = "⛔ DISARMED\nwatching only - F5 arms"

# The DETECTION block. Four of the seven appearances have something to say while
# the automation runs, and each gets one named line.
DETECTOR_LABEL: dict[TemplateKind, str] = {
    TemplateKind.SEND_READY: "send",
    TemplateKind.BUSY: "busy",
    TemplateKind.IDLE: "idle",
    TemplateKind.COPY: "copy",
}
PROBE_RESTING = "no verdict yet"
COPY_RESTING = "no click yet"
# The one combination that will never produce a verdict at all: the checklist
# ticks this signal and the service has no appearance to match it against, so
# the detector is silently skipped and "no verdict yet" would be a lie of
# omission for the rest of the run.
PROBE_UNCAPTURED = "ticked but not captured"
STALE_UNSET = "no chat region - staleness check disabled"
STALE_CALIBRATED = "watching the chat region"
STALE_UNTICKED = "stillness not watched for this service"
STALE_OFF = "finish detection off - nothing can decide a reply finished"

REGION_UNSET = "not set - alt-tab to the chat yourself"
# The CHAT WINDOW block's readiness line, per SELECTED window - ``tui/widgets/
# sidebar.py:slot_note``, whose two inputs are the window's drawn box and what
# the service THAT TAB is pointed at looks like. The master's window is never
# "ready or not": it is where the user's own conversation already is.
SLOT_NOTE_MASTER = "the main agent's chat window"
SLOT_NOTE_READY = "delegation ON"
SLOT_NOTE_MISSING = "delegation off · need: "

# The MCP block: the state literal -> the words the column shows.
_MCP_STATE_LABEL: dict[str, str] = {
    "pending": "pending",
    "connecting": "connecting",
    "connected": "connected",
    "disabled": "disabled",
    "failed": "failed",
    "needs_auth": "needs auth",
    "missing_sdk": "no mcp sdk",
}

# Leading state glyphs the watch segment prefixes its text with; a sub-agent run
# replaces them with its own, so they are stripped before rebadging.
_STATE_GLYPHS = "●○■✓✗"


def _strip_glyph(text: str) -> str:
    return text.lstrip(_STATE_GLYPHS).lstrip()


def _fmt_k(chars: int) -> str:
    return f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)


def _budget(chars: int) -> str:
    return f"{chars // 1000}k" if chars >= 1000 else str(chars)


def _short_root(project_root: Path) -> str:
    try:
        return str(Path("~") / project_root.relative_to(Path.home()))
    except ValueError:
        # A root with no drive letter on Windows is a REMOTE, POSIX one: str()
        # would spell /home/dev/app with backslashes, which is not its name.
        return str(project_root) if project_root.drive else project_root.as_posix()


def _service_options(config: Config) -> list[list[str]]:
    """``key · 12k`` per row, with the key as the value - the picker's options."""
    presets = sorted(config.services.values(), key=lambda p: p.key)
    return [[preset.key, f"{preset.key} · {_budget(preset.max_paste_chars)}"] for preset in presets]


def _mcp_line(status: Any) -> str:
    """One server's row: name + human state (+ tools when connected, + the detail
    on the two states that are questions until it is read)."""
    state = str(getattr(status, "state", ""))
    parts = [str(getattr(status, "name", "")), _MCP_STATE_LABEL.get(state, state)]
    if state == "connected":
        count = int(getattr(status, "tool_count", 0) or 0)
        parts.append(f"{count} tool{'' if count == 1 else 's'}")
    detail = str(getattr(status, "detail", "") or "")
    if state in ("failed", "needs_auth") and detail:
        parts.append(detail)
    return " · ".join(parts)


@dataclass(slots=True)
class LogEvent:
    """One transcript event, kept for ``render_log``'s export.

    The page holds the rendered DOM; this holds the text. Same split as the
    TUI's (``TranscriptPanel.event_log`` beside its widgets) and for the same
    reason: an export must survive whatever the display prunes, and must carry
    the verbatim payloads the rendered form collapses. One list per WINDOW, as
    the panels are.
    """

    time: str
    headline: str
    body: str = ""
    fenced: bool = False


def _run_divider(title: str) -> str:
    """The line separating one delegated run from the next in the sub-agent
    window's transcript. That window's transcript is never cleared between runs,
    so this is the only thing saying where one sub-task ended and the next
    began (``MainScreen._run_divider``)."""
    return f"── task: {title} ──"


@dataclass(slots=True)
class _SubRun:
    """Where one delegated run lives inside the sub-agent window's transcript.

    The window's transcript is persistent, so a run is a *slice* of it rather
    than a panel of its own: ``start`` is the index of its first event (just
    past the divider) and ``end`` is set when it finishes. The list is never
    pruned, so both stay valid for the life of the session - which is what lets
    ``render_log`` put one ``## sub-agent:`` heading over each run instead of
    one over all of them (``MainScreen._SubRun``).
    """

    ref: SessionRef
    start: int
    end: int | None = None
    # How it ended, recorded when ``end`` is, and always the CALLER's answer:
    # a run that was refused a fresh chat, blew its paste budget or crashed
    # reaches ``finish_session_view`` exactly like one that finished, so the
    # view must never infer this from the note text.
    ok: bool = True


class McpStatusSource(Protocol):
    """What this view needs of the process-wide ``McpManager``.

    Structural, and stated with ``Any`` rather than ``McpServerStatus``, because
    ``agentclip.gui`` may not import ``agentclip.mcp`` (tests/test_layering.py):
    the GUI only ever reads a status row's ``name``/``state``/``detail`` and
    hands them to a toast. Displaying MCP properly is a later increment.
    """

    def statuses(self) -> Sequence[Any]: ...
    def set_status_hook(self, cb: Callable[[Any], None] | None) -> None: ...


def _fence(body: str) -> str:
    """A backtick fence longer than any backtick run inside ``body``."""
    longest = run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _call_target(call: ToolCall) -> str:
    """The one thing worth reading beside a tool name in the transcript."""
    return (
        call.params.get("path")
        or call.params.get("command")
        or call.params.get("pattern")
        or call.params.get("question")
        or ""
    )


def _reason_line(call: ToolCall) -> str:
    """The model's own justification, as the gate shows it - or ``""``.

    ``agentclip.tools.shell.reason_line``, spelled again here for the same
    reason ``_distinct_rects`` below is: this shell may not import that layer
    (tests/test_layering.py gives ``agentclip.gui`` the engine's VALUE types and
    no tool code), and the alternative - widening the allowance so a view can
    reach a display helper - would open the whole tools package to a frontend.
    Six lines and a name that says where the original lives is the cheaper
    duplication. It moves down beside the other shared display text if a third
    caller ever appears.
    """
    flat = " ".join(call.params.get("reason", "").split())
    if not flat:
        return ""
    if len(flat) > _REASON_PREVIEW_CHARS:
        flat = flat[: _REASON_PREVIEW_CHARS - 1].rstrip() + "…"
    return f"reason: {flat}"


def _gate_target(call: ToolCall) -> str:
    """The gate title's target: the param the VERDICT was computed from.

    Per tool, never "whatever params exist" - an ``mcp`` call carrying a decoy
    ``command:`` must not repaint the gate as a harmless shell line
    (docs/design/ui-briefs/main-chat.md §6, and ``ActionPanel.show_approval``).
    """
    if call.tool == "run_command":
        return call.params.get("command", "")
    if call.tool == "mcp":
        return call.params.get("tool", "")
    return call.params.get("path", "")


def _preview_fields(action: PendingAction) -> dict[str, str]:
    """What the gate's body IS, so the page can render it instead of sniffing it.

    ``ActionPanel.preview_renderable``'s branches, kept on this side of the
    bridge: the decision "this preview is a unified diff / a brand-new file /
    a shell line and its reason" is the same decision in both shells, and the
    page's job is the colouring. Five kinds, and every one of them names the
    same three fields so the renderer has no optional-field arithmetic to do:

    - ``command`` - ``preview_head`` is ``$ <command>``, ``reason`` the model's
      justification, ``note`` why it is being asked at all, ``timeout`` if set.
    - ``mcp`` - every other command-kind tool. ``preview_head`` is the
      ENGINE's one-line preview and ``preview_body`` the full args: params are
      model-authored, and a decoy ``command: git status`` riding an mcp call
      must not repaint the gate as a harmless shell line (main-chat.md §6).
    - ``new_file`` - ``preview_head`` is the NEW FILE banner, ``preview_body``
      the whole file.
    - ``diff`` - ``preview_body`` is the unified diff, for the page to colour.
    - ``text`` - anything else, verbatim.
    """
    call = action.call
    fields = {
        "preview_kind": "text",
        "preview_head": "",
        "preview_body": action.preview,
        "reason": "",
        "note": "",
        "timeout": "",
    }
    if action.kind == "command":
        fields["reason"] = _reason_line(call)
        if call.tool != "run_command":
            fields["preview_kind"] = "mcp"
            fields["preview_head"] = action.preview
            fields["preview_body"] = call.params.get("args", "")
            fields["note"] = "no rule allows this - approve to run it once"
            return fields
        fields["preview_kind"] = "command"
        fields["preview_head"] = f"$ {call.params.get('command') or action.preview}"
        fields["preview_body"] = ""
        fields["note"] = (
            "no rule allows this - approve to run it once"
            if action.always_pattern is not None
            else "not on the allowlist - approve to run once in the project root"
        )
        fields["timeout"] = call.params.get("timeout", "")
        return fields
    first, _, rest = action.preview.partition("\n")
    if first.startswith("NEW FILE"):
        fields["preview_kind"] = "new_file"
        fields["preview_head"] = first
        fields["preview_body"] = rest
        return fields
    if action.preview.lstrip().startswith(("---", "+++", "@@")):
        fields["preview_kind"] = "diff"
    return fields


def _distinct_rects(
    region: ScreenRegion, found: list[tuple[Template, RegionMatch]]
) -> list[ScreenRegion]:
    """Scene-local matches as absolute rectangles, one per physical element.

    The TUI's ``_distinct_rects``, spelled again here rather than imported: the
    two shells may not import each other, and this is the fold that stops two
    IMAGES of one control reading as two windows of the same service. It moves
    down into ``agentclip.automation`` the moment the GUI grows calibration and
    there are two real callers of it (parity backlog, gui.md §2).
    """
    kept: list[ScreenRegion] = []
    for template, match in found:
        rect = match_rect(region, template, match)
        if not any(same_element(rect, other) for other in kept):
            kept.append(rect)
    return kept


class GuiView:
    """``ChatView`` + ``AutomationView`` + ``AutomationHost``, over the bridge."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[EngineRequest], Engine],
        project_root: Path,
        profile_root: Path | None = None,
        mcp_manager: McpStatusSource | None = None,
        schedule: Callable[[Coroutine[Any, Any, Any]], None] | None = None,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._config = config
        self._provider = provider
        self._project_root = project_root
        self._profile_root = profile_root if profile_root is not None else default_profile_dir()
        self._profiles: dict[str, ServiceProfile] = {}
        self._mcp_manager = mcp_manager
        self._mcp_announced: set[tuple[str, str]] = set()
        # How a coroutine reaches the GUI's loop. Injected because the loop is
        # the RUNNER's (gui/runner.py) and this object must be constructible -
        # and drivable - without one.
        self._schedule = schedule if schedule is not None else _no_schedule
        self._on_exit = on_exit if on_exit is not None else _no_exit

        # -- transcripts, one per WINDOW -------------------------------------
        # Three pointers, and the whole of this surface's correctness is that
        # they are three (ui-briefs/tabs-delegation-summary.md §2):
        #  * SELECTED - what the user is looking at and what the sidebar
        #    configures. Moved by a tab click and by F6, and never by anything
        #    else... except a controller focus, which re-establishes the view.
        #  * FOCUSED - which window's transcript the controller's ``add_*``
        #    calls are written into. Moved ONLY by ``focus_session_view``.
        #  * LIVE - which window the automation drives. It lives below, in the
        #    AutomationController, and is moved only by start/end_browser_chat.
        # They coincide almost always and visibly diverge for exactly the
        # duration of a delegation, which is the state this shell most has to
        # get right: selecting a tab must never redirect a paste.
        self._events: dict[str, list[LogEvent]] = {window: [] for window in _WINDOW_ORDER}
        self._selected_window = MASTER_WINDOW
        self._focused_window = MASTER_WINDOW
        self._sessions: dict[str, SessionRef] = {}
        # Where each delegated run begins and ends inside the sub-agent
        # window's transcript, and how it ended. Cleared with the session; the
        # WINDOWS are not (see ``clear_transcript``).
        self._sub_runs: list[_SubRun] = []

        # -- session chrome mirrored off the last render_state ---------------
        self._last_view: SessionView | None = None
        self._awaiting_new_session = False
        self._new_session_future: asyncio.Future[SessionSpec | None] | None = None
        self._session_role = "master"
        self._session_title = ""
        self._gate_kind: str | None = None
        self._gate_always: str | None = None
        # The status bar's "paused" reading, and nothing else: it MIRRORS what
        # the automation ended up doing with the watcher rather than deciding
        # it (``MainScreen.watch_paused`` / ``_mirror_watcher``).
        self._watch_paused = False

        # -- blocking prompts (confirm / prompt_text / show_summary) ---------
        self._prompts: dict[str, asyncio.Future[Any]] = {}
        self._prompt_ids = itertools.count(1)

        # -- the automation core, exactly as MainScreen builds it ------------
        self._automation = AutomationController(
            view=self,
            host=self,
            services=self._initial_services(config),
            clipboard=provider,
            poll_interval_ms=config.clipboard.poll_interval_ms,
            accepts=looks_like_protocol,
            on_clipboard_captured=self._clipboard_captured,
            has_appearance=self._live_has,
            on_fire=self._fire_auto_copy,
        )
        # The detector the current poller run watches through, and the run
        # itself. Mirrors ``MainScreen._detector`` / ``_detector_worker``.
        self._detector: ScreenDetector | None = None
        self._detector_worker: DetectorPoller | None = None
        self._logged_session_active = False

        self._controller = SessionController(
            config,
            engine_factory,
            project_root,
            view=self,
            mcp_statuses=mcp_manager.statuses if mcp_manager is not None else None,
        )

    # == lifecycle =============================================================

    @property
    def controller(self) -> SessionController:
        """The session controller this view drives (the runner starts it)."""
        return self._controller

    @property
    def automation(self) -> AutomationController:
        """The automation core this view drives (the runner stops it)."""
        return self._automation

    def start(self) -> None:
        """Mount: paint the resting chrome, then let the session flow begin.

        The GUI's ``MainScreen.on_mount``. Order matters the same way: the
        window is recorded while the user is provably here (they just launched
        us), the detector composition runs once so the readout is truthful
        before any calibration exists, and the controller starts last because
        its first act is to park on ``prompt_new_session``.
        """
        self._push_status()
        self._push_state_event()
        self._push_rail()
        self._push_tabs()
        self._push_sidebar()
        self._remember_own_window()
        # Nothing is drawn yet, so this starts no worker - but it is the only
        # writer of the DETECTION block, and the block has to name the window it
        # is about from the first frame rather than after the first calibration.
        self._start_detector_worker()
        if self._mcp_manager is not None:
            # Hook first, paint second, so no transition can fall in the gap.
            self._mcp_manager.set_status_hook(self._mcp_status_hook)
            self._push_mcp()
        for warning in self._config.warnings:
            self.notify(warning, severity="warning", timeout=8)
        self._controller.start()

    def shutdown(self) -> None:
        """The window is closing: stop everything that touches the machine.

        The GUI's ``MainScreen.on_unmount`` - the watcher and the poller are
        plain threads the AutomationController owns, so they are stopped by
        name. Cancelling the session worker is the RUNNER's half (it owns the
        loop the flows run on), exactly as Textual's unmount cancels workers.
        """
        if self._mcp_manager is not None:
            self._mcp_manager.set_status_hook(None)
        self._automation.stop_input()
        self._stop_detector_worker()

    # == what the page asks for (js_api, already on the loop) ==================

    def page_ready(self) -> None:
        """The page installed its receiver: repaint everything it missed.

        Everything the page cannot rebuild for itself, which is every surface
        composed on this side: the status bar's segments, the STATE rail, the
        sidebar's blocks and the MCP rows. The harness log is the one exception -
        the page keeps its own bounded tail of the ``harness`` events and a
        reload has nothing to replay into, exactly as a reopened TUI pane refills
        from the deque rather than from the widget.
        """
        self._push_status()
        self._push_state_event()
        self._push_rail()
        self._push_tabs()
        self._push_sidebar()
        self._push_mcp()
        self._bridge.send("armed", armed=self._automation.os_armed)

    def submit_text(self, text: str) -> None:
        """One door for every composer send - ``MainScreen._submit_text``.

        While the start prompt is up the text IS the task, except for a slash
        line, which is dispatched as a command so ``/identify`` and friends stay
        reachable in exactly the state they are most needed (a task that really
        begins with a slash is written ``//...``). Otherwise the text goes to
        the controller, where an open ``ask_user`` gate wins over slash parsing
        unconditionally.
        """
        self._remember_own_window()  # typing here = our window has OS focus
        future = self._new_session_future
        if future is not None and not future.done():
            task = text.strip()
            if not task:
                self.notify("describe the task first", severity="warning")
                return
            if task.startswith("//"):
                task = task[1:]
            elif task.startswith("/"):
                self._controller.submit_message(task)
                return
            self.reset_composer()
            future.set_result(
                SessionSpec(
                    task=task,
                    service=self._service_for(AgentSlot.MASTER),
                    subagent_service=self._service_for(AgentSlot.SUBAGENT),
                )
            )
            return
        self._controller.submit_message(text)

    def submit_decision(self, choice: str, note: str = "") -> None:
        """The gate's three answers, mapped onto the controller's one call.

        ``approve_always`` resolves the way the TUI's ``a`` does: the ruleset's
        remembered pattern when the gate carries one, the legacy edits-only
        auto-accept at an edit gate, and nothing at all otherwise - a button the
        page should not have offered is refused rather than reinterpreted.
        """
        if choice == "approve":
            self._controller.submit_decision(Decision.APPROVE, None)
        elif choice == "reject":
            self._controller.submit_decision(Decision.REJECT, note.strip() or None)
        elif choice == "approve_always":
            if self._gate_always is not None:
                self._controller.submit_decision(Decision.APPROVE_ALWAYS, None)
            elif self._gate_kind == "edit":
                self._controller.submit_decision(Decision.APPROVE_ALL_EDITS, None)

    def cancel_execution(self) -> None:
        """ctrl+x's equivalent. The controller no-ops when nothing is running."""
        self._controller.cancel_execution()

    def answer_prompt(self, prompt_id: str, value: Any) -> None:
        """Resolve one blocking prompt. Unknown ids are ignored - a modal the
        user answered twice must not raise into the page's promise."""
        future = self._prompts.pop(prompt_id, None)
        if future is not None and not future.done():
            future.set_result(value)

    # == ChatView: transcript ==================================================

    async def add_user(self, text: str) -> None:
        self._record("you", text)
        self._send_transcript(kind="user", text=text, label="you")

    async def add_prose(self, text: str) -> None:
        self._record("assistant", text)
        self._send_transcript(kind="prose", text=text, label="assistant")

    async def add_call(self, call: ToolCall) -> None:
        target = _call_target(call)
        raw = call.raw.strip("\n")
        self._record(f"tool call {call.id} - {call.tool} {target}".rstrip(), raw, fenced=True)
        self._send_transcript(
            kind="call",
            call_id=call.id,
            tool=call.tool,
            target=target,
            summary=f"▶ call {call.id} {call.tool} {target}".rstrip(),
            raw=raw,
        )

    async def add_note(self, text: str) -> None:
        self._record(text)
        self._send_transcript(kind="note", text=text)

    async def add_error(self, text: str) -> None:
        self._record(f"ERROR: {text}")
        self._send_transcript(kind="error", text=text)

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        payload = outbound.chunks[0]
        note = f"→ {label} ({outbound.total_chars:,} chars)"
        self._record(f"{note} [outbound turn {outbound.turn}]", payload, fenced=True)
        self._send_transcript(
            kind="outbound",
            note=note,
            turn=outbound.turn,
            chars=outbound.total_chars,
            parts=len(outbound.chunks),
            payload=payload,
        )

    async def clear_transcript(self) -> None:
        """The ``/new`` teardown, and the automation half of it in full.

        The WINDOWS survive - both tabs stay, with their services and their
        calibrations, because a window outlives the sessions run in it and the
        browser is still open, still drawn, still pointed at its service. What
        goes is session bookkeeping: both transcripts are emptied, the run
        slices are forgotten (so the sub-agent tab drops its ``✓``/``✗``), the
        pointers go home to MASTER and the poller is rebuilt against it. That
        split - runs reset, calibration does not - is the whole of
        ``MainScreen._remove_session_views`` plus ``clear_transcript``.
        """
        for events in self._events.values():
            events.clear()
        self._sessions.clear()
        self._sub_runs.clear()
        self._focused_window = MASTER_WINDOW
        self._select_window(MASTER_WINDOW)  # ...which moves ``calibrating`` too
        self._automation.select_live_slot(AgentSlot.MASTER)
        self._automation.reset_finish_trigger()
        self._automation.close_reply_gate()
        self._automation.forget_pending_insert()
        self._automation.set_loop_state(LoopState.IDLE, "session reset")
        self._automation.log_harness(
            KIND_SESSION,
            "session reset: the transcript is cleared, the calibrations and this log are not",
        )
        self._start_detector_worker()
        self.hide_paste_flash()
        self._bridge.send("transcript_clear")

    def has_transcript_events(self) -> bool:
        return any(self._events.values())

    def render_log(self, meta_lines: list[str]) -> str:
        """The master's transcript, then every sub-agent RUN under its heading.

        One exported document for the whole delegation tree, exactly as
        ``MainScreen.render_log`` writes it: the master reads end to end (the
        sub-runs appear in it as the delegate call and its result) and each
        delegated run follows under its own title and chat name, so nothing a
        sub-agent did is only visible in a tab. Per RUN rather than per window,
        even though the runs share one transcript - five sub-tasks under a
        single heading is a wall, and ``_sub_runs`` remembers the slices.
        """
        header = ["# AgentClip chat log", ""]
        header += [f"- {m}" for m in meta_lines]
        header += ["", "---", ""]
        master = "\n".join(header) + "\n" + self._render_events(self._events[MASTER_WINDOW])
        parts = [master.rstrip() + "\n"]
        sub = self._events[SUBAGENT_WINDOW]
        for run in self._sub_runs:
            chat = f" ({run.ref.chat_name})" if run.ref.chat_name else ""
            body = self._render_events(sub[run.start : run.end])
            parts.append(f"## sub-agent: {run.ref.title}{chat}\n\n{body}".rstrip() + "\n")
        return "\n".join(parts)

    @staticmethod
    def _render_events(events: Sequence[LogEvent]) -> str:
        body: list[str] = []
        for event in events:
            body.append(f"## [{event.time}] {event.headline}")
            body.append("")
            text = event.body.rstrip("\n")
            if text:
                if event.fenced:
                    fence = _fence(text)
                    body += [fence, text, fence, ""]
                else:
                    body += [text, ""]
        return "\n".join(body)

    def _record(self, headline: str, body: str = "", *, fenced: bool = False) -> None:
        """Append to the FOCUSED window's log - never the selected one.

        Which is the point of the split: the user may be reading the master's
        transcript while a sub-agent run's output keeps landing correctly in
        the sub-agent's.
        """
        self._events[self._focused_window].append(
            LogEvent(datetime.now().strftime("%H:%M:%S"), headline, body, fenced)
        )

    def _send_transcript(self, **fields: Any) -> None:
        """One transcript event, addressed to the window it belongs to.

        Every ``add_*`` goes through here, so the routing question is answered
        once: the page appends into the panel named by ``window``, whichever
        panel it happens to be showing.
        """
        self._bridge.send("transcript", window=self._focused_window, **fields)

    # == ChatView: session views (window transcripts) ==========================
    # The port speaks session ids; this shell speaks windows, exactly as the TUI
    # does. ``_window_of_session`` is the whole adapter - a sub-agent session's
    # output belongs in the sub-agent window's transcript, whichever run it is.

    def _window_of_session(self, session_id: str) -> str | None:
        """Which browser window a session's output belongs in.

        Unknown ids answer None rather than guessing: losing a transcript line
        beats writing it into the wrong conversation's panel.
        """
        ref = self._sessions.get(session_id)
        if ref is not None:
            return SUBAGENT_WINDOW if ref.role == "subagent" else MASTER_WINDOW
        return MASTER_WINDOW if session_id == MASTER_VIEW else None

    async def open_session_view(self, session: SessionRef) -> None:
        """Start ``session``'s transcript in its window and focus that window.

        No panel is minted: the sub-agent window's transcript is permanent, so
        a run opens by appending a divider to whatever is already in it and
        recording where it began (``_SubRun``). That is what makes the window's
        history one scroll instead of a graveyard of tabs.

        Focus moves here, not on the user's next click: the controller writes
        the sub-agent's whole run through the ordinary ``add_*`` calls right
        after this returns.
        """
        self._sessions[session.id] = session
        # Focus FIRST, so the divider itself lands in the right transcript: the
        # TUI writes it straight into the window's panel, and this shell writes
        # every transcript line through the focused pointer.
        self.focus_session_view(session.id)
        window = self._focused_window
        await self.add_note(_run_divider(session.title))
        if window == SUBAGENT_WINDOW:
            # Just PAST the divider: the run's own events are what an export
            # puts under its heading.
            self._sub_runs.append(_SubRun(session, len(self._events[window])))
            self._push_tabs()

    def focus_session_view(self, session_id: str) -> None:
        """Route every later ``add_*`` into that session's window (and show it).

        Unknown ids are ignored rather than fatal: losing a transcript line is
        never worth taking a running session down with an exception.
        """
        window = self._window_of_session(session_id)
        if window is None:
            return
        self._focused_window = window
        ref = self._sessions.get(session_id)
        self._bridge.send(
            "focus_session",
            session_id=session_id,
            window=window,
            role=ref.role if ref is not None else "master",
        )
        # Showing what is being written is the controller's one reach into the
        # SELECTED pointer, and the only one: a delegation starting pulls the
        # user's eyes to the sub-agent's transcript, and its ending hands them
        # back. Nothing about the live/automation target moves with it.
        self._select_window(window)

    async def finish_session_view(self, session_id: str, note: str, ok: bool) -> None:
        """A sub-agent run ended: annotate its transcript and re-badge its tab.

        Nothing is removed - the transcripts are output-only and the composer
        always targets the controller's active session, so leaving the run
        readable costs nothing and is the whole point of keeping it. The tab
        drops its ``▶`` for a ``✓`` or a ``✗``: the label belongs to the WINDOW,
        so it reports what happened in it, and the run's own title lives in the
        divider above its transcript.

        ``ok`` is the outcome and it is a PARAMETER rather than an inference,
        because the caller is the only one who knows: a run that was refused a
        fresh chat, blew its budget or crashed reaches here exactly like a run
        that finished, through the same ``finally``.
        """
        window = self._window_of_session(session_id)
        if window is None:
            return
        # The note belongs to the finishing run's transcript, whichever window
        # is focused by the time this lands.
        self._events[window].append(
            LogEvent(datetime.now().strftime("%H:%M:%S"), note)
        )
        self._bridge.send("transcript", window=window, kind="note", text=note, ok=ok)
        if window == SUBAGENT_WINDOW:
            for run in reversed(self._sub_runs):
                if run.ref.id == session_id:
                    run.end = len(self._events[window])
                    run.ok = ok
                    break
            self._push_tabs()

    # == window tabs (view-local: never a controller call) =====================
    # A tab is a browser WINDOW, not a session view (tui.md §1.6): it exists
    # before any session, keeps its own service and its own calibration, and
    # survives ``/new``. Clicking one and F6 are deliberately not routed through
    # the controller - they are pure navigation over state this view already
    # holds, and the controller never asks which tab the user is looking at.

    def select_window(self, window: str) -> None:
        """The page's tab click: show that window, and point the sidebar at it.

        Idempotent by design rather than an early return: "click the tab I am
        already on" has to keep meaning "show me this window", which is how a
        user re-establishes their view after the controller moved focus
        elsewhere mid-delegation. Repainting loses nothing here - every readout
        the tab bar and the sidebar draw is re-derived from state this object
        holds, never written into the page out of band
        (ui-briefs/tabs-delegation-summary.md §6).
        """
        self._select_window(window)

    def next_window(self) -> None:
        """F6: select the next window in the bar's own order.

        Kept even though a DOM tab strip is clickable and focusable, unlike the
        TUI's (§7 of the brief calls the hotkey reasonable-but-not-load-bearing
        for a GUI): the composer holds focus for most of a session and the tabs
        are one keystroke away rather than a mouse trip.
        """
        order = list(_WINDOW_ORDER)
        try:
            index = order.index(self._selected_window)
        except ValueError:  # pragma: no cover - the pointer is never off-list
            index = -1
        self._select_window(order[(index + 1) % len(order)])

    def _select_window(self, window: str) -> None:
        """Make ``window`` the tab the user sees and the sidebar configures.

        It pointedly does NOT touch the automation's LIVE slot. Looking at a
        window is not driving it: a click here while a sub-agent is mid-run
        must not send the next paste into the master's chat. Nor does it touch
        the DETECTION block, which reports on the live window and is the
        detectors' to write.
        """
        if window not in _WINDOW_SLOTS:
            return
        self._selected_window = window
        self._automation.select_calibrating_slot(_WINDOW_SLOTS[window])
        self._push_tabs()
        self._push_sidebar()

    def _push_tabs(self) -> None:
        """The two-row bar: master windows, then the selected master's subs.

        One event for both rows because the bar owns ONE selection across them
        - the two-row shape is a tree, not two independent pickers - and
        because the selected/focused pair is only readable as a pair: they are
        the same window except during a delegation, which is exactly when the
        page has something extra to say.
        """
        self._bridge.send(
            "tabs",
            selected=self._selected_window,
            focused=self._focused_window,
            masters=[self._tab(MASTER_WINDOW)],
            subs=[self._tab(SUBAGENT_WINDOW)],
        )

    def _tab(self, window: str) -> dict[str, str]:
        """One tab: what it is, how it is doing, what it runs on.

        The service key rides on the tab because it is per window and the
        sidebar only ever shows the SELECTED one's - without it, "which chat is
        the sub-agent going to open?" is a question you answer by clicking
        around (``MainScreen._window_label``).
        """
        name = _WINDOW_NAMES[window]
        service = self._automation.service_of(window)
        state = self._window_state(window)
        glyph = WINDOW_STATE_GLYPH[state]
        return {
            "window": window,
            "name": name,
            "service": service,
            "state": state,
            "label": f"{glyph}{name} · {service}" if service else f"{glyph}{name}",
        }

    def _window_state(self, window: str) -> str:
        """A window's run state, DERIVED from its run history, never stored.

        Only the LAST run's outcome is reported: the tab is a status light for
        the window, not a log, and the earlier runs stay readable by scrolling
        its transcript. The master never gets one - it is the user's own
        conversation, and there is no "run" of it to have succeeded.
        """
        if window != SUBAGENT_WINDOW or not self._sub_runs:
            return "none"
        if any(run.end is None for run in self._sub_runs):
            return "running"
        return "ok" if self._sub_runs[-1].ok else "failed"

    # == ChatView: state + chrome ==============================================

    def render_state(self, view: SessionView) -> None:
        self._last_view = view
        self._session_role = view.session_role
        self._session_title = view.session_title
        if view.session_active != self._logged_session_active:
            self._logged_session_active = view.session_active
            self._automation.log_harness(
                KIND_SESSION, "session started" if view.session_active else "session ended"
            )
        # The two ways the loop settles home, read exactly as MainScreen reads
        # them: no session at all, or the turn finished interpreting and the
        # floor is back with the user (an open gate is still interpreting).
        if not view.session_active or (
            self._automation.loop_state is LoopState.INTERPRETING
            and (view.awaiting_answer or not (view.busy or view.pending_approval))
        ):
            self._automation.set_loop_state(
                LoopState.IDLE,
                "no session is running"
                if not view.session_active
                else "the turn finished and the floor is back with you",
            )
        self._push_state_event()
        # Every StatusSnapshot-derived segment is this push's (mode, service,
        # out, turn, instr, edits) and so is the watch segment's whole
        # precedence, so the bar is recomposed here rather than only when the
        # automation moves - ``MainScreen.render_state`` -> ``_paint_status``.
        self._push_status()

    def show_gate(self, action: PendingAction, position: str, queue: str) -> None:
        self._gate_kind = action.kind
        self._gate_always = action.always_pattern
        target = _gate_target(action.call)
        self._bridge.send(
            "gate",
            open=True,
            title=f"{self._gate_prefix()}APPROVE · call {position} · {action.call.tool} "
            f"{target}".rstrip(),
            position=position,
            queue=queue,
            tool=action.call.tool,
            target=target,
            kind=action.kind,
            preview=action.preview,
            auto_reason=action.auto_reason or "",
            always_pattern=action.always_pattern,
            # The third answer is offered on exactly the TUI's terms: a ruleset
            # pattern to remember, or an edit-kind gate in legacy mode. Never
            # for a run_command gate without a pattern - commands stay
            # allowlist-or-prompt (tui.md §2.4).
            always_label=self._always_label(action),
            hint=self._gate_hint(action),
            # WHAT the preview is, decided here so the page can render it and
            # not have to guess (``_preview_fields``).
            **_preview_fields(action),
        )

    def hide_gate(self) -> None:
        self._gate_kind = None
        self._gate_always = None
        self._bridge.send("gate", open=False)

    def _gate_prefix(self) -> str:
        if self._session_role != "subagent":
            return ""
        return f"SUB-AGENT ‹{self._session_title}› · " if self._session_title else "SUB-AGENT · "

    @staticmethod
    def _always_label(action: PendingAction) -> str:
        if action.always_pattern is not None:
            what = "calls like this one" if action.always_pattern == "*" else action.always_pattern
            return f"Always: {what}"
        return "Approve + auto-edits" if action.kind == "edit" else ""

    @staticmethod
    def _gate_hint(action: PendingAction) -> str:
        """The keys line, in the ActionPanel's own words.

        Composed here rather than in JS because the third answer means two
        different things (remember a pattern until restart / auto-accept edits
        for this session) and a user about to press a key that stops a question
        being asked should read which one they are buying.
        """
        hint = "press y to approve · n to reject"
        if action.always_pattern is not None:
            what = "calls like this one" if action.always_pattern == "*" else action.always_pattern
            return f"{hint} · a to always allow {what} (until AgentClip restarts)"
        if action.kind == "edit":
            return f"{hint} · a to approve + auto-accept edits this session"
        return hint

    def start_working(self, label: str, calls: Sequence[RunCall] = ()) -> None:
        self._bridge.send(
            "run",
            running=True,
            label=label,
            calls=[
                {
                    "call_id": row.call_id,
                    "tool": row.tool,
                    "detail": row.detail,
                    "streams": row.streams,
                    "glyph": row.glyph,
                }
                for row in calls
            ],
        )

    def stop_working(self) -> None:
        self._bridge.send("run", running=False)

    def reset_composer(self) -> None:
        self._bridge.send("composer_reset")

    # == ChatView: the turn executing (WORKER-THREAD callers) ==================
    # The GUI's half of the port's one thread contract. All three do exactly
    # what MainScreen's do - hand the fact to a thread-safe queue and return -
    # except that the queue is the JS bridge rather than Textual's message pump.

    def call_started(self, call_id: int, tool: str, detail: str) -> None:
        # ``streams`` is decided here, not on the page: it is what makes a row
        # expandable, and ``RunPanel.call_started`` answers it the same way for
        # a row the panel never planned (only run_command produces a live tail).
        self._bridge.send(
            "run_call",
            phase="started",
            call_id=call_id,
            tool=tool,
            detail=detail,
            streams=tool == "run_command",
        )

    def call_finished(self, call_id: int, glyph: str) -> None:
        self._bridge.send("run_call", phase="finished", call_id=call_id, glyph=glyph)

    def call_output(self, call_id: int, chunk: str) -> None:
        self._bridge.send("run_output", call_id=call_id, chunk=chunk)

    # == ChatView + AutomationView: notifications ==============================

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """A toast, from whichever thread asked for it.

        ``markup`` is accepted and ignored: it is Textual's console-markup
        switch, and the page escapes everything it renders anyway.
        """
        self._bridge.send(
            "toast",
            message=message,
            title=title,
            severity=severity,
            timeout=timeout,
        )

    def alert(self, message: str, severity: Severity = "information") -> None:
        """The attention-grabbing toast. No bell: ``config.notify.bell`` is a
        terminal escape the TUI writes and this shell has no equivalent for, so
        only the toast half is switchable here."""
        if self._config.notify.toast:
            self.notify(message, severity=severity)

    # == ChatView: clipboard / transport =======================================

    async def copy_outbound(self, text: str) -> None:
        """Deliver one outbound payload - the controller's ``copy_outbound``.

        With nothing calibrated (which is every GUI session in this slice) the
        delivery does the honest thing on its own: the payload is written to the
        real clipboard, ``chatbox_region`` answers None, no click and no
        synthetic Ctrl+V happen, and the loop lands on ``MANUAL_INSERT`` with
        the "paste it yourself" banner up. That is the existing manual path,
        reached without a second implementation of it.
        """
        await self._automation.copy_outbound(text)

    async def park_outbound(self, text: str) -> None:
        await self._automation.park_outbound(text)

    def redeliver_outbound(self, text: str) -> None:
        """The `c` double tap's second half. Scheduled rather than awaited: the
        controller is on the event loop and must not park for the seconds a
        delivery takes. The two refusals are the controller's."""
        if not self._automation.may_redeliver():
            return
        self._schedule(self._automation.copy_outbound(text))

    async def read_clipboard(self) -> str | None:
        # Deliberately not gated by the armed switch - this is the one-shot read
        # behind force-ingest, which is the user asking for their own clipboard.
        return await asyncio.to_thread(self._provider.read_text)

    def open_new_chat_now(self) -> None:
        """``/new``: click the browser's new-chat button, then reset the session.

        Best-effort by contract, exactly as in the TUI: AgentClip owns one half
        of a fresh chat and the user owns the other, so every outcome resets the
        session and a failed click spends its toast saying which half is left.
        """
        self._schedule(self._new_browser_chat(AgentSlot.MASTER))

    async def _new_browser_chat(self, slot: AgentSlot) -> None:
        outcome = await self._automation.click_profile_element(slot, TemplateKind.NEW_CHAT)
        restarted = self._controller.request_new_session() if self._session_running() else False
        if outcome is not ElementClick.CLICKED:
            tail = (
                " AgentClip is on a fresh session anyway - open a new browser chat yourself"
                if restarted
                else " Nothing on the tool side to renew - open a new browser chat yourself"
            )
            self.notify(
                f"the new-chat click did not land ({outcome.name.lower()}).{tail}",
                severity="warning",
            )
            return
        self.notify("new browser chat opened")

    def show_identify_overlay(self) -> None:
        """REDUCED SCOPE (gui.md §2): ``/identify`` draws a fullscreen overlay
        over the live chat window through a child process. The mechanism is
        shell-agnostic and carries over unchanged, but it needs a calibrated
        window to identify inside and this shell cannot draw one yet - so it
        says so rather than putting an empty overlay on the user's screen."""
        self.notify(
            "/identify is not wired into the GUI yet - it needs a drawn chat window,"
            " which lands with the calibration surface",
            severity="warning",
        )

    def toggle_harness_log(self) -> None:
        """``/log`` and F8: the same show/hide, two ways to ask for one thing.

        The pane itself is the page's - it keeps the bounded tail of the
        ``harness`` events, so a reveal shows *now* without anything having to
        be replayed across the bridge - and this is the one call that flips it
        from the Python side.
        """
        self._bridge.send("toggle", what="log")

    def set_os_armed(self, target: bool | None) -> None:
        """Arm or disarm everything that ACTS on the machine. The flag and the
        watcher are the controller's; what is left here is the chrome."""
        was_armed = self._automation.os_armed
        armed = self._automation.set_os_armed(target)
        if armed and not was_armed:
            self._mirror_watcher()
        elif was_armed and not armed:
            self._watch_paused = True  # truthful: nothing is polling the clipboard
        self._automation.log_harness(
            KIND_ARMED,
            "ARMED - the tool may click, paste and watch the clipboard again"
            if armed
            else "DISARMED - watching only: no clicks, no paste, no clipboard watch",
        )
        self._push_status()
        if armed and not was_armed:
            self.notify("ARMED - automation restored")
        elif not armed:
            self.notify(
                "DISARMED - watching only: no clicks, no paste, no clipboard watch. "
                "Payloads still land on the clipboard.",
                severity="warning",
                timeout=8,
            )

    def start_input(self) -> None:
        self._automation.start_input()
        self._mirror_watcher()
        self._push_status()

    def stop_input(self) -> None:
        self._automation.stop_input()
        self._push_status()

    def _mirror_watcher(self) -> None:
        """Bring ``_watch_paused`` in line after something tried to START one.

        Only the True->False direction lives here, because that is the only one
        a start can cause; the pauses (the `w` key, the disarm) say so at their
        own site - ``MainScreen._mirror_watcher``, for its reasons.
        """
        if self._automation.watching:
            self._watch_paused = False

    # == what the page's keys ask for (js_api, already on the loop) ============
    # Every one of these is a MainScreen ``action_*`` with the Textual removed:
    # the same controller call, the same refusals, the same order. What they do
    # NOT carry over is ``check_action``'s three-way footer dimming - the page
    # has no footer to dim, so a key that cannot fire says why in a toast
    # instead of being quietly absent (docs/design/gui.md §3).

    def cycle_permission_mode(self) -> None:
        """shift+tab: ask -> plan -> unattended -> ask.

        Ungated, exactly as on the TUI screen: the moment a user reaches for
        this is the moment the app is busy doing the thing they want it to stop
        doing, and it must work pre-session and mid-turn alike.
        """
        self._controller.cycle_permission_mode()

    def recopy(self) -> None:
        """`c`: the last outbound back on the clipboard - and, pressed twice
        inside the double-tap window, delivered again.

        The whole two-stage decision (including the 1.5s window and the arm a
        fresh outbound drops) is ``SessionController.recopy``'s, so both shells
        press the same key: this forwards and nothing more.
        """
        self._controller.recopy()

    def reinstruct(self) -> None:
        """`r`: arm/disarm this service's extra instructions for the next
        payload. The engine owns the flag and both refusals."""
        self._controller.reinstruct()

    def force_ingest(self) -> None:
        """`i`: the user says the reply is on the clipboard right now.

        The one place a key press moves the STATE rail without going through
        the detector machinery. If the parse then fails, the settled status push
        walks it back to idle - ``MainScreen.action_force_ingest``.
        """
        self._automation.set_loop_state(
            LoopState.INTERPRETING, "you pressed i: ingesting the clipboard by hand"
        )
        self._controller.force_ingest()

    def end_session(self) -> None:
        """`e`: close the session out and read what happened.

        Gated exactly as ``MainScreen.check_action`` gates it - a live session,
        no turn in flight, and the floor back with the user (AWAITING_REPLY or
        DONE) - because the summary is a report on a settled session and mid-turn
        numbers would be a lie the moment they were read. The TUI hides the key;
        this shell has no footer to hide it from, so it says why instead
        (docs/design/gui.md §3).

        It is NOT the end of the session: ``task_done`` leaves the user in the
        chat able to follow up, and the summary is one keypress away with three
        exits (undo one turn, start fresh, or just go back).
        """
        view = self._last_view
        if view is None or not view.session_active:
            self.notify("no session yet - describe a task first", severity="warning")
            return
        phase = view.snapshot.phase.name if view.snapshot else "IDLE"
        if view.busy or phase not in ("AWAITING_REPLY", "DONE"):
            self.notify(
                "a turn is running - the summary opens when the floor is back with you",
                severity="warning",
            )
            return
        self._controller.end_session()

    def toggle_watch(self) -> None:
        """`w`: pause or resume the clipboard watcher.

        Refused while disarmed - a resumed watcher would be a hole in the
        promise the switch makes - and in manual-clipboard mode and with no
        session, where the TUI hides the key outright rather than dimming it.
        """
        if self._provider.name == "manual":
            self.notify(
                "manual clipboard mode: nothing polls the clipboard - press i to ingest a reply",
                severity="warning",
            )
            return
        if not self._session_running():
            self.notify("no session - the watcher starts with one", severity="warning")
            return
        if not self._automation.os_armed:
            self.notify(
                "disarmed - the clipboard watcher stays off until F5 arms the tool",
                severity="warning",
            )
            return
        if self._automation.watching:
            self._automation.stop_input()
            self._watch_paused = True
            self._push_status()
            self.notify("clipboard watcher paused - w resumes, i ingests manually")
            return
        self._automation.start_watching()
        self._mirror_watcher()
        self._push_status()
        self.notify("clipboard watcher resumed")

    def retry_insert(self) -> None:
        """The paste flash's one button: run the click-settle-paste again.

        Scheduled rather than awaited, exactly as the sidebar button's worker
        is: the sequence clicks and settles for several seconds and the page is
        holding no promise on it. The three refusals are the controller's.
        """
        self._schedule(self._automation.retry_insert())

    def set_service(self, key: str) -> None:
        """The SERVICE picker: point this window at a different service.

        The same path ``MainScreen._on_service_changed`` takes - the key lands
        in the SELECTED window's slot of the automation's map, that window's tab
        is relabelled (the service key is part of what a tab says), the pick is
        written back to the global config so the next launch comes up on it, and
        the detector worker is rebuilt because a different service is a
        different set of captured appearances.
        """
        if key not in self._config.services:
            return
        if not self._awaiting_new_session:
            # The budget is baked into the Engine at bootstrap, so the service
            # is fixed for the life of a session. The page locks the control
            # too; this is the door the lock cannot cover.
            self.notify("the service is fixed while a session runs", severity="warning")
            self._push_sidebar()
            return
        self._automation.set_service(self._selected_window, key)
        self._persist_services()
        self._start_detector_worker()
        self._push_tabs()
        self._push_sidebar()
        self._push_status()

    def _persist_services(self) -> None:
        """Write both windows' services to the global config.toml.

        Remembering the pick is a convenience, never the point of the press, so
        every way the write can fail degrades to a warning and a session that
        carries on with the switch the user actually asked for.
        """
        try:
            save_active_services(
                self._automation.service_of(MASTER_WINDOW),
                self._automation.service_of(SUBAGENT_WINDOW),
            )
        except OSError as exc:
            self.notify(
                f"could not remember the service for next launch: {exc}", severity="warning"
            )

    # == ChatView: sub-agent transport =========================================

    def delegation_available(self) -> bool:
        return can_delegate(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self.profile_for(AgentSlot.SUBAGENT),
        )

    def delegation_missing(self) -> tuple[str, ...]:
        return missing(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self.profile_for(AgentSlot.SUBAGENT),
        )

    async def start_chat(self, session: SessionRef) -> bool:
        slot = AgentSlot.SUBAGENT if session.role == "subagent" else AgentSlot.MASTER
        return await self._automation.start_browser_chat(slot)

    async def end_chat(self, session: SessionRef) -> None:
        self._automation.end_browser_chat()

    # == ChatView: scheduling + lifecycle ======================================

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Put a controller flow on the GUI's asyncio loop (gui/runner.py)."""
        self._schedule(coro)

    def exit_app(self) -> None:
        """Close the window, which ends ``webview.start()`` and the process."""
        self._on_exit()

    # == ChatView: blocking prompts ============================================

    async def prompt_new_session(self) -> SessionSpec | None:
        """Wait INLINE for the first message - no modal, exactly as the TUI.

        The composer switches into "describe the task" mode and this parks on a
        future the next send resolves. The controller may call it again (a
        budget-exceeded retry, ``/new``, the summary's "new session"), and each
        call re-arms the same surface.
        """
        future: asyncio.Future[SessionSpec | None] = asyncio.get_running_loop().create_future()
        self._new_session_future = future
        self._awaiting_new_session = True
        self._push_state_event()
        # No render_state is coming to trigger them (the controller has nothing
        # to push while parked here), so the bar and the picker's lock are
        # repainted by hand - ``MainScreen.prompt_new_session``.
        self._push_status()
        self._push_sidebar()
        try:
            return await future
        finally:
            self._new_session_future = None
            self._awaiting_new_session = False
            self._push_state_event()
            self._push_status()
            self._push_sidebar()

    async def confirm(self, title: str, body: str = "") -> bool:
        return bool(await self._modal("confirm", title=title, body=body))

    async def prompt_text(self, title: str, hint: str) -> str | None:
        answer = await self._modal("text", title=title, hint=hint)
        return answer if isinstance(answer, str) else None

    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str:
        """The session summary: the stats table, the model's own ``task_done``
        text, and four ways out.

        The four answers are the controller's vocabulary
        (``SessionController._show_summary``): ``undo`` closes this and undoes
        THE SINGLE MOST RECENT turn behind a confirm, ``new`` runs the same
        reset ``/new`` does, ``export`` writes the chat log and RE-OPENS this
        screen (a loop, not an exit), and ``close`` is "back", not "end" - the
        transcript and the session are untouched. Anything else - a modal an
        abort poisoned, a page that answered with a shape we do not know - is
        an empty string, which the controller reads as "none of the above" and
        simply leaves the session alone.
        """
        answer = await self._modal(
            "summary",
            title="SESSION SUMMARY",
            rows=[[label, value] for label, value in rows],
            summary=summary,
            # The empty summary has to say WHY it is empty: a blank panel under
            # a stats table reads as a rendering failure (``SummaryScreen``).
            placeholder="*(the model sent no summary)*",
            hint="u undo last turn · t new session · l export chat log · esc close",
        )
        return answer if answer in ("undo", "new", "export", "close") else ""

    async def _modal(self, modal: str, **fields: Any) -> Any:
        """Open one modal and park on the answer the page sends back.

        Keyed by id rather than by "the modal that is up", because the flows
        that open these are the ones an abort poisons: a stale answer must
        resolve nothing rather than resolve the next question.
        """
        prompt_id = f"p{next(self._prompt_ids)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._prompts[prompt_id] = future
        self._bridge.send("modal", modal=modal, prompt_id=prompt_id, **fields)
        try:
            return await future
        finally:
            self._prompts.pop(prompt_id, None)
            self._bridge.send("modal_close", prompt_id=prompt_id)

    # == AutomationView ========================================================
    # Every method below may be called from the detector poller or the clipboard
    # watcher (the port's thread contract), and every one of them does the same
    # two things: build the event and queue it.

    def paint_loop_state(self, state: LoopState) -> None:
        # Drawn from the CONTROLLER rather than from the payload: this may be
        # raised on the poller thread, and the flag is re-readable while a state
        # that crossed as data would be whatever was true when it was sent.
        self._push_rail()

    def paint_harness_entry(self, entry: HarnessEntry) -> None:
        # ``line`` is the entry as the pane prints it - the fixed-width kind
        # column is ``HarnessEntry.line``'s decision, taken once below both
        # shells, so the page renders a row instead of re-deriving a layout.
        self._bridge.send(
            "harness", kind=entry.kind, time=entry.time, text=entry.text, line=entry.line
        )

    def paint_detection(self, kind: TemplateKind, text: str) -> None:
        # Only the four kinds a verdict is DECIDED from have a line; the two
        # chat boxes and the new-chat button are searched every tick like the
        # rest and decide nothing, so they are dropped here rather than given a
        # row the user would read as a verdict (``Sidebar.update_template``).
        label = DETECTOR_LABEL.get(kind)
        if label is None:
            return
        self._bridge.send("detection", kind=kind.name, label=label, text=text)

    def paint_stale(self, text: str) -> None:
        self._bridge.send("detection", kind="STALE", label="", text=text)

    def paint_elements(self, crops: Mapping[TemplateKind, object]) -> None:
        """REDUCED SCOPE (gui.md §2): the kinds this tick recognised, not their
        pictures. No ``crop_elements`` is wired in, so what arrives here is the
        controller's uncut mapping of sightings - which is the honest thing to
        route while there is no elements panel to draw into. PNG data URIs per
        crop are the panel increment's job."""
        self._bridge.send(
            "elements", kinds=[kind.name for kind, seen in crops.items() if seen is not None]
        )

    def show_paste_flash(self, text: str, *, retry: bool = False) -> None:
        self._bridge.send("flash", show=True, text=text, retry=retry)

    def hide_paste_flash(self) -> None:
        self._bridge.send("flash", show=False)

    def paint_armed(self, armed: bool) -> None:
        self._bridge.send("armed", armed=self._automation.os_armed)

    # == AutomationHost ========================================================

    def live_preset(self) -> ServicePreset:
        return self._preset_for(self._automation.live_slot)

    def profile_for(self, slot: AgentSlot) -> ServiceProfile:
        return self._profile(self._service_for(slot))

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Every place ``kind`` is on screen right now, in absolute coordinates.

        All of them rather than the first, and near-duplicate hits on one
        physical element folded away: an appearance belongs to the SERVICE, so a
        second window of the same service inside the drawn region carries the
        same button, and clicking whichever match came back first would click a
        different conversation's (``MainScreen._find_all``, whose implementation
        this is).

        Empty - never raised - for every way this comes up empty, which in this
        slice is always: the GUI has no calibration surface, so no chat region
        is ever drawn.
        """
        if scene is not None and slot is not None:
            raise ValueError("find_all takes a slot or a captured scene, never both")
        target = slot if slot is not None else self._automation.live_slot
        region = self._automation.calibration(target).chat_region
        if region is None:
            return []
        templates = self.profile_for(target).variants(kind)
        if not templates:
            return []
        if scene is None:
            try:
                scene = await asyncio.to_thread(capture_region, region)
            except CaptureError:
                return []
        tolerance, matcher = self._automation.live_search()
        found: list[tuple[Template, RegionMatch]] = []
        for template in templates:
            matches = await asyncio.to_thread(
                find_all_in_region,
                template,
                scene,
                tolerance=tolerance,
                max_diff=kind.max_diff,
                limit=_MAX_MATCHES,
                matcher=matcher,
            )
            found.extend((template, match) for match in matches)
        found.sort(key=lambda pair: (pair[1].y, pair[1].x))
        return _distinct_rects(region, found)

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        return await self._automation.verified_copy_click(target)

    async def ingest_harvest(self) -> None:
        """A verified copy click landed: show a non-protocol reply as prose if
        this service opted in (``ServicePreset.capture_prose``).

        Protocol-shaped harvests are left alone entirely - the watcher ingests
        those on its own, and reading them here too would ingest them twice.
        """
        if not self.live_preset().capture_prose:
            return
        try:
            text = await asyncio.to_thread(self._provider.read_text)
        except ClipboardUnavailable:
            return
        if not text or looks_like_protocol(text):
            return
        self.hide_paste_flash()
        self._automation.log_harness(
            KIND_CLIPBOARD,
            f"the harvested reply has no CLIP blocks ({len(text)} chars); "
            "capture_prose is on, so it goes to the transcript as prose",
        )
        self._automation.set_loop_state(
            LoopState.INTERPRETING, "the reply has no CLIP blocks - showing it as prose"
        )
        self._controller.submit_clipboard(text, accept_prose=True)

    def copy_seen_note(self) -> str:
        """What the always-running detector remembers about the copy button -
        the other half of a failed harvest's report."""
        detector = self._detector
        if detector is None or not detector.searches(TemplateKind.COPY):
            return ""
        ago = detector.seen_ago(TemplateKind.COPY)
        if ago is None:
            return "; the poller has never seen it in this window either"
        return f"; the poller last saw one {ago:.0f}s ago"

    def rebuild_detectors(self) -> None:
        self._start_detector_worker()

    def park_off_clipboard(self, text: str) -> None:
        """The clipboard provider refused the payload, and this shell has no
        second write channel.

        The TUI's answer is the terminal's OSC-52 escape; a WebView2 window has
        nothing like it, and writing the payload back into the page's own
        clipboard would be the same refused write one layer up. So the GUI's
        honest equivalent is to SHOW the payload where a human can select it -
        a plain block in the window - and say that the copy is theirs to make
        (docs/design/gui.md §2).
        """
        self._bridge.send("payload", text=text)
        self.notify(
            "no clipboard backend - the payload is shown in the window: select it and copy "
            "it by hand (or read it from .agentclip/sessions/<id>/outbound/)",
            severity="error",
            timeout=12,
        )

    # == the clipboard watcher's way in ========================================

    def _clipboard_captured(self, text: str) -> None:
        """The watcher thread's capture, marshalled onto the GUI's loop.

        Called from the AutomationController's watcher thread, so it does the
        one thing that is safe from there and the whole decision happens on the
        loop - the GUI's equivalent of MainScreen's ``post_message``.
        """
        self._schedule(self._ingest_capture(text))

    async def _ingest_capture(self, text: str) -> None:
        self.hide_paste_flash()
        self._automation.log_harness(
            KIND_CLIPBOARD, f"a protocol-shaped clipboard capture came in ({len(text)} chars)"
        )
        self._automation.set_loop_state(
            LoopState.INTERPRETING, "the reply arrived on the clipboard and is being parsed"
        )
        self._controller.submit_clipboard(text)

    # == the detector poller ===================================================

    def _start_detector_worker(self) -> None:
        """Compose and start one poll run against the LIVE window.

        ``MainScreen._start_detector_worker`` minus the sidebar it paints: what
        stays is every question about meaning - which detectors this composition
        runs, what a rebuild invalidates, and the run that replaces the last one.
        With no region drawn (every GUI session in this slice) it stops at the
        retarget, which is what keeps the outgoing run's in-flight probes from
        being read as the new one's.
        """
        self._stop_detector_worker()
        self._automation.retarget_detectors()
        self._automation.forget_verdicts()
        self._detector = None
        region = self._automation.live.chat_region
        if region is None:
            self._paint_detection(STALE_UNSET)
            return
        preset = self.live_preset()
        detector = build_detector(
            region,
            self.profile_for(self._automation.live_slot),
            signals=preset.finish_signals,
            required_ticks=max(1, round(preset.stable_seconds / _BUSY_POLL_S)),
            tolerance=preset.tolerance,
            matcher=preset.matcher,
        )
        self._detector = detector
        self._automation.busy_tracker = detector.busy
        self._automation.idle_tracker = detector.idle
        self._automation.stale_tracker = detector.stale
        self._automation.active_detectors = detector.active_detectors
        if not detector.active_detectors:
            # The service's checklist is empty, or asks only for appearances it
            # has none of. Say so where the stale verdict would go: the
            # consequence (auto-copy will never fire) is otherwise invisible
            # until the user waits for a copy that never comes.
            self._paint_detection(STALE_OFF)
        else:
            # Whether the stale line is a live verdict or an explanation of its
            # own silence: it is the one detector with no appearance behind it.
            self._paint_detection(
                STALE_CALIBRATED
                if "stale" in detector.active_detectors
                else STALE_UNTICKED
            )
        if not detector.watching:
            return
        self._detector_worker = self._automation.start_detectors(
            self._automation.detector_loop(
                detector, region, capture=capture_region, poll_seconds=_BUSY_POLL_S
            )
        )

    def _stop_detector_worker(self) -> None:
        if self._detector_worker is not None:
            self._detector_worker.cancel()
            self._detector_worker = None
        self._automation.stop_detectors()

    def _paint_detection(self, stale_line: str) -> None:
        """Repaint the DETECTION block for the LIVE window - the only writer.

        ``MainScreen._paint_detection``, minus its paint-epoch stamp. That
        filter exists because Textual routes a cross-thread ``post_message``
        through ``call_soon_threadsafe`` and can therefore deliver an outgoing
        run's last paint AFTER the rebuild that replaced it; this shell's bridge
        is one FIFO with one drainer, so ordering is structural and a reset
        queued here cannot be overtaken (``gui/bridge.py``). The generation
        filter still does its own half: a ghost probe is dropped inside
        ``consume_*`` on the poller thread and never becomes a paint at all.

        The send-gate line is deliberately re-derived rather than reset: a
        rebuild does not un-paste the outbound the gate is holding for.
        """
        signals = self.live_preset().finish_signals
        profile = self.profile_for(self._automation.live_slot)
        self.paint_detection(TemplateKind.SEND_READY, self._automation.send_gate_line())
        for name, kind in (("busy", TemplateKind.BUSY), ("idle", TemplateKind.IDLE)):
            ticked_but_blind = name in signals and not profile.has(kind)
            self.paint_detection(kind, PROBE_UNCAPTURED if ticked_but_blind else PROBE_RESTING)
        self.paint_detection(TemplateKind.COPY, COPY_RESTING)
        self.paint_stale(stale_line)
        self._push_sidebar()  # the heading names the window these lines are about

    def _live_has(self, kind: TemplateKind) -> bool:
        """Has the LIVE window's service a capture of ``kind``? Called on the
        POLLER thread, so it reads immutable state and nothing else."""
        return self.profile_for(self._automation.live_slot).has(kind)

    def _fire_auto_copy(self) -> None:
        """The finish decision says the model stopped: harvest the reply.

        Called on the poller thread, so it only SCHEDULES - the bracket that
        suspends evaluation for the flow's duration is the controller's.
        """
        self._schedule(self._automation.run_auto_copy_flow(self._automation.auto_copy_flow))

    # == MCP: the sidebar block, the status segment, and a toast per transition =

    def _mcp_status_hook(self, status: Any) -> None:
        """Called from the manager's loop thread. Non-blocking, never raises -
        the manager drops a listener that does, once, for good."""
        # The repaint reads ``statuses()`` rather than patching one row in: the
        # hook is a tick, and a connect can change a NEIGHBOUR's line too
        # (shadowed tool ids) - ``MainScreen._on_mcp_status_changed``.
        self._push_mcp()
        self._push_status()
        name = getattr(status, "name", "")
        state = getattr(status, "state", "")
        if state not in ("failed", "needs_auth", "connected"):
            return
        key = (name, state)
        if key in self._mcp_announced:
            return
        self._mcp_announced.add(key)
        if state == "connected":
            count = getattr(status, "tool_count", 0)
            self.notify(f"MCP server {name!r} connected · {count} tool{'' if count == 1 else 's'}")
            return
        what = "needs auth" if state == "needs_auth" else "failed"
        detail = getattr(status, "detail", "")
        self.notify(
            f"✗ MCP server {name!r} {what}{f' - {detail}' if detail else ''}",
            severity="warning",
            timeout=8,
        )

    # == chrome pushes =========================================================

    def _push_state_event(self) -> None:
        """The one state snapshot the page repaints from.

        Composed here rather than in JS because the composer's mode is the
        brief's precedence table (main-chat.md §3) and there must be exactly one
        implementation of it - the page renders what it is told.
        """
        view = self._last_view
        snap = view.snapshot if view is not None else None
        mode, placeholder, enabled = self._composer_mode(view)
        self._bridge.send(
            "state",
            session_active=bool(view and view.session_active),
            busy=bool(view and view.busy),
            pending_approval=bool(view and view.pending_approval),
            awaiting_answer=bool(view and view.awaiting_answer),
            awaiting_new_session=self._awaiting_new_session,
            has_outbound=bool(view and view.has_outbound),
            role=view.session_role if view is not None else "master",
            title=view.session_title if view is not None else "",
            phase=snap.phase.name if snap else "IDLE",
            turn=snap.turn if snap else 0,
            service=snap.service_key if snap else "",
            budget=snap.budget_chars if snap else 0,
            out_chars=snap.last_outbound_chars if snap else 0,
            permission_mode=snap.mode if snap else self._controller.permission_mode,
            yolo=bool(snap and snap.yolo),
            composer_mode=mode,
            composer_placeholder=placeholder,
            composer_enabled=enabled,
        )

    def _composer_mode(self, view: SessionView | None) -> tuple[str, str, bool]:
        """The brief's precedence table, first match wins (main-chat.md §3).

        The one deliberate divergence is the newline key: the TUI says Ctrl+J
        because Enter is its send key inside a ``TextArea``; the GUI says
        Shift+Enter, which is what every web composer means. A shell idiom, not
        drift - recorded in gui.md §2.
        """
        if self._awaiting_new_session:
            return "task", "Describe the task · Enter starts the session · Shift+Enter newline", True
        if view is None or not view.session_active:
            return "idle", "no session", False
        if view.awaiting_answer:
            return "answer", "Answer the model · Enter sends · Shift+Enter newline", True
        if view.session_role == "subagent" and not view.pending_approval:
            return "abort", "Sub-agent running · /abort ends it and tells the model", True
        if view.pending_approval:
            return "idle", "approve or reject the action above first", False
        phase = view.snapshot.phase.name if view.snapshot else "IDLE"
        if not view.busy and phase in ("AWAITING_REPLY", "DONE"):
            if phase == "DONE":
                return "done", "Task done · type a follow-up to continue", True
            return "message", "Message the model · Enter sends · Shift+Enter newline", True
        return "idle", "working - the chat box is paused", False

    def _push_status(self) -> None:
        """The status bar: ten segments, composed here, in order.

        Composed on this side for parity increment 1's reason - the DECISIONS
        cross with the data - and because every one of them is a rule rather
        than a style: the watch segment's nine-branch precedence, YOLO winning
        the edits slot over auto, ARMED keeping its own slot so a disarmed YOLO
        session is visible, the sub-agent rebadge. A segment that must hide is
        simply ABSENT from the list, which is how the TUI hides them too: by not
        being drawn, leaving no padding behind (``StatusBar.update_segments``).
        """
        self._bridge.send(
            "status",
            segments=self._status_segments(),
            armed=self._automation.os_armed,
            watching=self._automation.watching,
            provider=self._provider.name,
            project=_short_root(self._project_root),
        )

    def _status_segments(self) -> list[dict[str, str]]:
        view = self._last_view
        snap = view.snapshot if view is not None else None
        watch_text, watch_class = self._watch_segment()
        # The snapshot is the authority once there is a session (and during a
        # delegation it is the SUB-AGENT's, like every other field here); before
        # one, the controller's mirror of the configured default is what
        # shift+tab would be changing.
        mode = snap.mode if snap else self._controller.permission_mode
        mode_class = {"plan": "st-plan", "unattended": "st-unattended"}.get(mode, "st-dim")
        if snap and snap.yolo:
            edits, edits_class = "⚡ YOLO", "st-yolo"
        elif snap and snap.auto_accept_edits:
            edits, edits_class = "EDITS:auto", ""
        else:
            edits, edits_class = "EDITS:ask", ""
        segments: list[dict[str, str]] = [
            {"id": "mode", "text": f"MODE:{mode}", "cls": mode_class},
            {"id": "watch", "text": watch_text, "cls": watch_class},
        ]
        if not self._automation.os_armed:
            segments.append({"id": "armed", "text": "⛔ DISARMED", "cls": "st-disarmed"})
        segments += [
            {
                "id": "service",
                "text": f"{snap.service_key} {_fmt_k(snap.budget_chars)}" if snap else "no session",
                "cls": "",
            },
            {
                "id": "out",
                "text": f"out {_fmt_k(snap.last_outbound_chars)}/{_fmt_k(snap.budget_chars)} (1/1)"
                if snap
                else "out -",
                "cls": "",
            },
            {"id": "turn", "text": f"turn {snap.turn}" if snap else "turn -", "cls": ""},
        ]
        if snap and snap.instructions_armed:
            # Lit only between the `r` press and the payload that spends it.
            segments.append({"id": "instr", "text": "✎ INSTR", "cls": "st-instr"})
        segments.append({"id": "edits", "text": edits, "cls": edits_class})
        connected, enabled = self._mcp_counts()
        if enabled is not None:
            segments.append({"id": "mcp", "text": f"mcp {connected}/{enabled}", "cls": ""})
        segments.append({"id": "root", "text": _short_root(self._project_root), "cls": ""})
        return segments

    def _watch_segment(self) -> tuple[str, str]:
        """The "what does the app want from me" segment.

        While a delegated run is live the whole segment is rebadged and prefixed
        ``◆ SUB-AGENT``, because everything it reports - the phase, the
        approval, the question - is that sub-agent's, not the conversation the
        user is watching.
        """
        text, style = self._base_watch_segment()
        if self._session_role == "subagent":
            return f"◆ SUB-AGENT · {_strip_glyph(text)}", "st-sub"
        return text, style

    def _base_watch_segment(self) -> tuple[str, str]:
        view = self._last_view
        snap = view.snapshot if view is not None else None
        phase = snap.phase.name if snap else "IDLE"
        if phase == "DONE":
            return "✓ done - reply to continue", "st-done"
        if view is not None and view.pending_approval:
            return "■ APPROVE NEEDED", "st-attn"
        if view is not None and view.awaiting_answer:
            return "■ ANSWER NEEDED", "st-attn"
        if self._awaiting_new_session:
            # The session worker is technically busy here too (parked on the
            # inline prompt) but there is no turn in flight - nothing for the
            # user to wait on - so the bar must not say "working".
            return "○ idle", "st-dim"
        if view is not None and view.busy:
            return "● working...", "st-busy"
        if self._provider.name == "manual":
            return "✗ manual paste", "st-err"
        if self._watch_paused:
            return "○ paused", "st-dim"
        if view is not None and view.session_active and phase == "AWAITING_REPLY":
            return "● ready - paste the reply", "st-armed"
        return "○ idle", "st-dim"

    def _push_rail(self) -> None:
        """The STATE rail: eight rows, one per LoopState, in loop order.

        The brightness table is computed here because ``LOOP_TRANSITIONS`` is
        the automation's own vocabulary and the rule reading it is one line -
        the active row is marked, everything it can legally move to next reads
        at normal brightness, the rest is dim. Display only, as it is in the
        TUI: nothing consults the table to DECIDE anything, and a road that
        skips a state simply never lights that row.
        """
        active = self._automation.loop_state
        legal = LOOP_TRANSITIONS.get(active, frozenset())
        self._bridge.send(
            "rail",
            loop=active.name,
            rows=[
                {
                    "state": state.name,
                    "label": _LOOP_LABEL[state],
                    "mark": "active"
                    if state is active
                    else ("legal" if state in legal else "dim"),
                }
                for state in _LOOP_ORDER
            ],
        )

    def _push_sidebar(self) -> None:
        """The sidebar's blocks that are not a rail, a banner or a verdict.

        Everything per-window in here describes the SELECTED window - the
        service picker, the appearance summary, the drawn region and the
        readiness note - because that is what a tab selection IS: it shows a
        window's transcript and points every per-window control at that window
        (``MainScreen._select_window``). The one block that does not follow the
        selection is DETECTION, whose heading names the LIVE window and whose
        lines only the detector machinery writes.

        One event rather than five because they are repainted by the same few
        moments (a tab click, a service pick, a detector rebuild, a session
        boundary) and a page that reassembles a column out of five partial
        writes has five ways to be half-painted.
        """
        window = self._selected_window
        slot = _WINDOW_SLOTS[window]
        service = self._service_for(slot)
        preset = self._config.services.get(service)
        cal = self._automation.calibration(slot)
        region = cal.chat_region
        live_window = _WINDOW_NAMES[
            SUBAGENT_WINDOW if self._automation.live_slot is AgentSlot.SUBAGENT else MASTER_WINDOW
        ]
        self._bridge.send(
            "sidebar",
            project=_short_root(self._project_root),
            services=_service_options(self._config),
            service=service,
            service_label=(
                f"{preset.label} · {preset.max_paste_chars:,} chars per paste "
                f"· {preset.total_context_chars:,} chars context"
            )
            if preset
            else "",
            # The TUI's line names F2 as the door to the captures; this shell
            # has no service editor yet (increment 7), so it reports the count
            # and stops rather than naming a key that would do nothing.
            profile_note=f"appearance: {self.profile_for(slot).describe()}",
            # The picker is locked while a session owns the services: BOTH
            # windows' budgets are baked in at bootstrap (the sub-agent's with
            # the session spec), so neither preset may move mid-session.
            locked=not self._awaiting_new_session,
            window=window,
            region=f"{region.describe()} · chatbot window" if region is not None else REGION_UNSET,
            slot_note=self._slot_note(slot),
            detection_title=f"DETECTION · {live_window}",
        )

    def _slot_note(self, slot: AgentSlot) -> str:
        """The CHAT WINDOW block's readiness line for the selected window.

        ``tui/widgets/sidebar.py:slot_note``, spelled again for the reason the
        other display strings above are. Two inputs, because readiness has two
        halves: the box this window was drawn as, and what the service THAT TAB
        is pointed at looks like - which is the sub-agent tab's own service
        whenever the sub-agent tab is selected, never the master's.
        """
        if slot is AgentSlot.MASTER:
            return SLOT_NOTE_MASTER
        cal = self._automation.calibration(slot)
        profile = self.profile_for(slot)
        if can_delegate(cal, profile):
            return SLOT_NOTE_READY
        return SLOT_NOTE_MISSING + ", ".join(missing(cal, profile))

    def _mcp_counts(self) -> tuple[int, int | None]:
        """``connected`` over ``enabled`` - or ``None`` when there is no manager.

        Disabled entries are a config statement rather than a runtime hope, so
        they are out of both numbers' way. ``None`` hides the segment and the
        whole sidebar block: an install with no MCP servers gets exactly the bar
        it always had.
        """
        if self._mcp_manager is None:
            return 0, None
        statuses = list(self._mcp_manager.statuses())
        if not statuses:
            return 0, None
        connected = sum(1 for s in statuses if getattr(s, "state", "") == "connected")
        enabled = sum(1 for s in statuses if getattr(s, "state", "") != "disabled")
        return connected, enabled

    def _push_mcp(self) -> None:
        """The sidebar's MCP block: one row per configured server, in config
        order. Absent - heading included - when there is no manager."""
        if self._mcp_manager is None:
            return
        statuses = list(self._mcp_manager.statuses())
        self._bridge.send(
            "mcp",
            rows=[
                {"name": str(getattr(s, "name", "")), "state": str(getattr(s, "state", "")),
                 "line": _mcp_line(s)}
                for s in statuses
            ],
        )

    # == services and profiles =================================================

    @staticmethod
    def _initial_services(config: Config) -> dict[str, str]:
        master = config.general.service
        if master not in config.services:
            master = next(iter(sorted(config.services)))
        sub = config.general.subagent_service
        return {
            MASTER_WINDOW: master,
            SUBAGENT_WINDOW: sub if sub in config.services else master,
        }

    def _service_for(self, slot: AgentSlot) -> str:
        window = next(win for win, known in _WINDOW_SLOTS.items() if known is slot)
        key = self._automation.service_of(window)
        return key if key in self._config.services else self._config.general.service

    def _preset_for(self, slot: AgentSlot) -> ServicePreset:
        return self._config.services.get(self._service_for(slot)) or self._config.preset()

    def _profile(self, key: str) -> ServiceProfile:
        """A service's captured appearances, read off disk once per app run."""
        profile = self._profiles.get(key)
        if profile is None:
            profile = load_profile(self._profile_root, key)
            self._profiles[key] = profile
        return profile

    # == small helpers =========================================================

    def _session_running(self) -> bool:
        view = self._last_view
        return bool(view and view.session_active)

    def _remember_own_window(self) -> None:
        """Record the foreground window at a moment the user is provably
        interacting with AgentClip. The HANDLE is OS state both shells snap
        focus back to, so it is kept below."""
        self._automation.set_own_window(foreground_window())


def _no_schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """The scheduler a view nobody wired a loop into gets: close the coroutine
    rather than leak it, and do nothing. Tests inject a recorder; the runner
    injects the real loop."""
    coro.close()


def _no_exit() -> None:
    """The exit a view with no window behind it gets."""
