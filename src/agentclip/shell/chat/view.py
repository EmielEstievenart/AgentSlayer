"""GuiView: the pywebview adapter that implements the three UI ports.

The GUI's ``MainScreen``. It is the same arrangement, structurally and for the
same reasons (``docs/design/gui.md`` §0/§1): the session orchestration lives in
:class:`~agentclip.shell.app.SessionController` and the browser automation in
:class:`~agentclip.driver.automation.controller.AutomationController`, and this object
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
non-blocking and ordered by construction (``webview/bridge.py``). Nothing here
touches the page directly.

**Nothing here is reduced scope any more.** Slice 2 shipped a handful of
``ChatView`` methods implemented smaller than the TUI's rather than as a silent
``pass`` that would strand a controller flow, each saying so at its own
definition; ``docs/design/gui.md`` §2 lists them and that list is now empty.
Everything a *turn* passes through - the transcript, the gate, the delivery, the
watcher, the prompts - is the real thing, and so are the sidebar, the status
bar's ten segments and the harness log pane (increment 2), the window tabs, the
per-window transcripts and the session summary (increment 3). What is left of
the parity backlog is whole SURFACES this shell does not have yet - the SSH
connect dialog. (Help, settings, the slash popup and the whole key chain landed
in increment 6.)

**This window hosts no monitor** (ui-monitor.md §10.2). Every surface made of
PIXELS - the ELEMENTS column, the chat-region picker, ``/identify`` and the
service editor - belongs to the Monitor UI, which is a PROCESS of its own
(``agentclip-monitor``): either one this shell launched on this PC
(``shell/app/monitor_launch.py``) or one already running on the machine the
browser is on. Both are reached the same way - one dial, one token, one
``watched()`` stream - so there is no second path to a screen for the two to
disagree on (§10.0). Since §11.2 not even a DOOR is left here: the sidebar's two
buttons, the titlebar's **monitor UI** button, ``F2`` and ``/identify`` are all
deleted rather than re-pointed. What remains is a brain that reads its service
back off :class:`Watched` rather than out of this host's ``[services.*]`` tables
(§10.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from agentclip.config import (
    MONITOR_LOOPBACK,
    VALID_GUI_THEMES,
    Config,
    GuiConfig,
    MonitorTarget,
    RemoteTarget,
    ServicePreset,
    default_global_config_path,
    drop_monitor_target,
    save_gui_theme,
    save_monitor_target,
    save_remote_target,
)
from agentclip.driver.automation.controller import AutomationController, MonitorLike
from agentclip.driver.automation.describe import describe
from agentclip.driver.automation.harness_log import (
    KIND_ARMED,
    KIND_CLIPBOARD,
    KIND_SESSION,
    HarnessEntry,
)
from agentclip.driver.automation.loop_state import (
    ATTENTION_STATES,
    LOOP_TRANSITIONS,
    LoopState,
)
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.clip.base import ClipboardProvider, ClipboardUnavailable
from agentclip.driver.monitor.protocol import (
    EMPTY_WATCHED,
    TICK_KINDS,
    Tick,
    UIMonitor,
    Watched,
)
from agentclip.driver.monitor.remote import RemoteUIMonitor
from agentclip.driver.monitor.switchable import IdleMonitor, SwitchableMonitor
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.focus import foreground_window
from agentclip.driver.screen.profile import TemplateKind, describe_captured
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot, can_delegate, missing
from agentclip.engine.engine import Decision, PendingAction
from agentclip.engine.link.factory import EngineRequest
from agentclip.engine.link.wire import EngineLinkError
from agentclip.engine.states import Phase
from agentclip.executor.hosts.connect import (
    PASSWORD_ATTEMPTS,
    STEP_ENGINE,
    ConnectedRemote,
    ConnectError,
    ConnectPrompts,
    StepEvent,
    connect_remote,
    ssh_config_aliases,
)
from agentclip.protocol.parser import looks_like_protocol
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.shell.app import SessionController, SessionSpec, SessionView
from agentclip.shell.app.commands import COMMANDS
from agentclip.shell.app.link import Link, NoSkills, SkillReport
from agentclip.shell.app.monitor_launch import (
    LOCAL_MONITOR_EXITED,
    LOCAL_MONITOR_NAME,
    LaunchLocal,
    LocalMonitorLauncher,
)
from agentclip.shell.app.sizes import fmt_budget, fmt_tokens, fmt_tokens_compact
from agentclip.shell.app.types import SessionRef
from agentclip.shell.app.view import RunCall, Severity
from agentclip.shell.chat.docs import load_doc_pages
from agentclip.shell.chat.remote import (
    MODE_LOCAL,
    MONITOR_CONNECT_FIRST,
    ConnectDialog,
    MonitorDialog,
    RemoteConnect,
    RemoteRuntime,
    alias_rows,
    monitor_rows,
    policy_lines,
    saved_rows,
)
from agentclip.shell.webview.bridge import Bridge

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
# spelled again for the reason ``_reason_line`` is: the
# two shells may not import each other (tests/test_layering.py), and widening
# that boundary so a frontend could reach another frontend's display strings
# would be a worse trade than a block of literals with a comment saying where
# the original lives. The four that named F2 name the Monitor UI instead as of
# ui-monitor.md §11.2: this window has no service editor behind that key any
# more - it has no key - so the words point at the window that does.

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
    # Last, and not a step of the round trip: the link to the monitor is gone
    # and nothing above this row can happen until it is back (ui-monitor.md
    # §2.9). Below the loop rather than inside it, so the picture the other
    # eight rows draw is unchanged.
    LoopState.DISCONNECTED,
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
    LoopState.DISCONNECTED: "disconnected",
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
# ``PROBE_UNCAPTURED`` ("ticked but not captured") lived here. It needed the
# service's finish CHECKLIST and the machine's captures, and both are the
# monitor's since §10.5 with no field on ``Watched`` between them - so the two
# probe rows rest plainly now, and the half of that warning worth keeping moved
# onto the STALE row, where an empty ``active_detectors`` says "nothing here
# will ever produce a verdict" (``_adopt_watched``).
STALE_UNSET = "no chat region - staleness check disabled"
STALE_CALIBRATED = "watching the chat region"
STALE_UNTICKED = "stillness not watched for this service - in the Monitor UI"
STALE_OFF = "finish detection off - configure it in the Monitor UI"
# What the SERVICE block's appearance count is followed by: where the captures
# are made, which since §11.2 is not a key this window has
# (``sidebar.PROFILE_HINT``).
PROFILE_HINT = " · captures + detection in the Monitor UI"
# What the read-only SERVICE line says after the monitor's own words for it
# (§10.5): the key is not a choice this window offers any more, it is a fact
# read back off the machine that owns the screen.
SERVICE_FROM_MONITOR = " · from the monitor"
# ...and what it says before there is an answer at all - no link yet, or a
# monitor with nothing configured for this window.
SERVICE_UNWATCHED = "no service - the monitor has not answered for this window"

# == MONITOR SEES (F2) ========================================================
# The sidebar block ui-monitor.md §11.4 gives the key §11.2 freed. Everything in
# it is the monitor's answer read back - which appearances that machine holds,
# which of them are on screen this second, and the behavioural settings it sent
# for the brain to drive from - because since §11.3 those are the only answers
# there are: this window holds no templates, no click points and no service
# table, so "why did it refuse?" cannot be answered by looking at anything here.
# The three states of a row. The middot the middle one would start with is the
# separator the row already has (``label · state``), so it is not repeated.
SEES_ON = "✓ on screen"
SEES_CAPTURED = "captured, not on screen"
SEES_MISSING = "✗ not captured"
# ...and what stands where the rows would be when there is no monitor at all.
# Not an empty block: with nothing attached, every row would read "not captured"
# and blame the far machine for a link that was never made.
SEES_NO_MONITOR = "no monitor attached"

# == settings (F4) ============================================================
# The TUI's SettingsScreen is a theme picker and nothing else - one "Appearance"
# tab over four Textual themes (``shell/tui/screens/settings.py``), and it does NOT
# touch ``[notify] bell/toast``, which is file-only in both shells. So this
# shell's F4 mirrors exactly that: an appearance picker, no more.
#
# The palettes are CSS, not Textual themes, so they are named in this shell's
# own vocabulary and persisted in this shell's own config table (``[gui] theme``,
# ``config.VALID_GUI_THEMES``). Two of the four names are the TUI's too, and that
# is the point: ``/theme claude-dark`` is one command with one meaning in both
# shells, rendered here as a palette block rather than a Textual theme. The other
# two are this shell's alone. Live preview is the TUI's
# model too: picking one applies it immediately - but here it is also SAVED
# immediately, because a page-side class flip has no "revert on escape" to be
# the other half of a staged edit and a setting that survives the window is what
# the user asked for by picking it (docs/design/gui.md §3).
THEME_CHOICES: tuple[tuple[str, str], ...] = (
    ("dark", "Dark"),
    ("light", "Light"),
    ("claude-warm", "Claude Warm"),
    ("claude-dark", "Claude Dark"),
)
# ``AgentClipApp._open_settings``'s toast, word for word.
THEME_SAVED = "theme saved"

# == quitting mid-turn (ctrl+q / the window's close button) ===================
# ``AgentClipApp.action_quit``'s ConfirmScreen, verbatim: the same two sentences
# in the same order, because what they promise (the turn is lost, the backups
# are not) is the whole reason the dialog exists.
QUIT_TITLE = "Quit mid-turn?"
QUIT_BODY = (
    "The current turn is incomplete and its results were never sent to the "
    "model. Per-turn backups are kept on disk."
)

# == the SSH connect dialog ===================================================
# The one surface with no TUI equivalent (docs/design/gui.md §0: the TUI *cannot*
# prompt before Textual owns the terminal, so its flow stays CLI flags plus
# getpass). The sequence behind it is shared - ``hosts/connect.py``, the same one
# ``cli.remote_launch`` drives - and everything below is what this shell says
# about it.

# The PROJECT block's persistent marker (gui.md §4 ruling 6): the banner at
# connect time is a moment, this is the standing fact. It names the machine
# because a path alone is ambiguous in a screenshot (remote-ssh.md,
# "Consequences to handle").
LINK_LIVE = "link live"
LINK_LOST = "link lost - the next operation re-dials"
LINK_RECONNECTS = "{count} reconnect(s) so far"
RECONNECT_OK = "reconnected to {target}"
RECONNECT_FAILED = "could not re-dial {target}: it will be tried again on the next operation"
RECONNECT_LOCAL = "this session runs on this PC - there is nothing to re-dial"

# The three questions a dial can ask, as this shell's modals. The host-key
# wording is OpenSSH's own, because that is the design intent (``cli.py``'s
# ``confirm_host_key``: "OpenSSH's own question, asked in OpenSSH's own words").
HOST_KEY_TITLE = "The authenticity of host '{host}' can't be established."
HOST_KEY_BODY = "{keytype} key fingerprint is {fingerprint}.\n\nContinue connecting?"
PASSWORD_TITLE = "Password for {target}"
# _PASSWORD_ATTEMPTS is hardcoded at three inside SshHost._authenticate and this
# dialog does NOT add a fourth: a GUI-level retry loop would call connect() again
# from scratch and double-count reconnects (ssh-connect.md §2).
PASSWORD_HINT = "attempt {n} of {total} · Cancel gives up on password auth"

CONNECT_BUSY = "a connect is already running"
CONNECT_MID_TURN = "a turn is running - connecting would end this session"
CONNECT_UNAVAILABLE = "this build has no way to go remote"
CONNECT_DONE = "connected to {target} - this session's tools now run over there"

# ``CALIBRATION_ELSEWHERE`` lived here: the one sentence F2, the titlebar's
# **monitor UI** button, the sidebar's two doors and ``/identify`` all answered
# with. ui-monitor.md §11.2 deleted the doors instead of re-pointing them - a
# window that hosts none of those surfaces should not carry five affordances
# that exist to say so - so the sentence has nobody left to say it.

# == the monitor link (--monitor host:port, ui-monitor.md §6.5) ================
# Split mode's four sentences. Each one is a moment the user cannot see for
# themselves: the screen the app is watching is on another machine, so a link
# that came up, dropped, refused a dial or came back as a DIFFERENT process is
# invisible unless it is said out loud.
MONITOR_DIALLING = "dialling"
# The service key this window drives names nothing on the monitor's machine.
# Two sentences for one fact: the DETECTION line is the standing one, the toast
# says what to do about it - and it names both machines, because the fix is on
# one of them and the user is looking at the other.
MONITOR_UNPROFILED = "the monitor has no captured appearance for '{service}'"
MONITOR_UNPROFILED_TOAST = (
    "the monitor at {peer} has no captured appearance for service '{service}' -"
    " in its Monitor UI select '{service}' and capture it, or select here the"
    " service it is calibrated for (its badge says which)"
)
MONITOR_UP = "monitor link up: {peer}"
MONITOR_LOST = "monitor link lost - redialling {peer}"
MONITOR_RETRY = "cannot reach the monitor at {peer}: {reason}"
# A different ``server_id`` means the process on the far side is not the one we
# were talking to (remote.py's ``server_id``): it was restarted, so every
# generation, every streak and every tracker we ever heard about belonged to a
# monitor that no longer exists. The reconnect is a retarget, and the user is
# told because a monitor that restarted is a monitor somebody restarted.
MONITOR_RESTARTED = "the monitor at {peer} restarted - re-deriving everything from its screen"
# The redial's backoff, doubling from the first up to the cap (§2.9's "the
# brain redials"). Module constants rather than a schedule inside the loop so a
# suite can flatten them without a clock, exactly as the wait they describe is
# the only slow thing in the flow.
MONITOR_BACKOFF_START = 1.0
MONITOR_BACKOFF_CAP = 10.0

# The Monitor tab's own three (§9.2). A dial is one round trip, so unlike the
# SSH tab there is no checklist to be busy on - there is a button that is
# already pressed, and a second press says so.
MONITOR_DIAL_BUSY = "a monitor dial is already running"
MONITOR_ALREADY_NONE = "no monitor is attached - there is nothing to disconnect"
MONITOR_DIAL_FAILED = "the monitor did not answer"

# == no monitor, and the local child (ui-monitor.md §10.1/§10.2) ===============
# Where the loop parks when this window has no screen at all: ``--monitor none``
# at launch, and every deliberate Disconnect afterwards. It is a STATE to sit in
# rather than an error - the window works, the transcript works, and the two
# ways out are both one click away on the Monitor tab.
MONITOR_NONE = "no monitor attached - attach or launch one from the Monitor tab"
# The local child between "spawned" and "listening". Said as a DISCONNECTED
# reason rather than a toast, which is also what makes the dial failures behind
# it quiet: the loop is already parked, so ``_park_disconnected``'s once-per-
# outage toast has already been spent on this sentence.
MONITOR_LOCAL_STARTING = "starting a monitor on this PC - the link comes up when it is listening"
# The launch itself refused. Distinct from a dial that failed, because nothing
# was ever started: no exe beside this one, no permission to spawn, no port.
MONITOR_LOCAL_FAILED = "could not start a local monitor: {reason}"
# What a launched child is called wherever a peer is shown. The launcher's own
# name for its target, so the badge, the toasts and the Monitor tab all agree.
MONITOR_LOCAL_PEER = LOCAL_MONITOR_NAME

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
    "invalid": "invalid config",
    "failed": "failed",
    "needs_auth": "needs auth",
    "missing_sdk": "no mcp sdk",
}

# Leading state glyphs the watch segment prefixes its text with; a sub-agent run
# replaces them with its own, so they are stripped before rebadging.
_STATE_GLYPHS = "●○■✓✗"


def _strip_glyph(text: str) -> str:
    return text.lstrip(_STATE_GLYPHS).lstrip()


def _short_root(project_root: Path) -> str:
    try:
        return str(Path("~") / project_root.relative_to(Path.home()))
    except ValueError:
        # A root with no drive letter on Windows is a REMOTE, POSIX one: str()
        # would spell /home/dev/app with backslashes, which is not its name.
        return str(project_root) if project_root.drive else project_root.as_posix()


def _on_off(flag: bool) -> str:
    return "on" if flag else "off"


def sees_rows(watched: Watched, tick: Tick | None) -> list[dict[str, str]]:
    """One row per kind in ``TICK_KINDS ∪ Watched.captured`` (§11.4).

    Two sources and one each: :attr:`Watched.captured` is the authority on what
    the monitor HAS a picture of, and the latest tick is the authority on what
    is on screen right now. They are asked in that order because they answer
    different questions - a kind the monitor never captured is never searched,
    so a tick has nothing to say about it, and "✗ not captured" is the sentence
    that names the fix (capture it in the Monitor UI).

    The tick is the LIVE window's, which is the only observation there is: the
    monitor watches one window at a time. For the whole of a delegation the rows
    therefore describe the selected window's captures against the live window's
    screen, exactly as the DETECTION block above them does.
    """
    kinds = list(TICK_KINDS) + [kind for kind in watched.captured if kind not in TICK_KINDS]
    rows: list[dict[str, str]] = []
    for kind in kinds:
        if tick is not None and tick.present(kind):
            state, words = "on", SEES_ON
        elif kind in watched.captured:
            state, words = "captured", SEES_CAPTURED
        else:
            state, words = "missing", SEES_MISSING
        rows.append({"kind": kind.name, "state": state, "text": f"{kind.label} · {words}"})
    return rows


def sees_settings(watched: Watched) -> str:
    """The six settings the brain DRIVES from, in the monitor's words (§11.4).

    Not every field of :class:`Watched` - the six whose value explains a
    behaviour the user can see happen or not happen: a prompt that was pasted
    but not sent, a paste that arrived in pieces, a copy click that walked the
    window first, a scroll position that came back, an Enter that went out
    before the composer had finished swallowing the paste (§11.8). Each of
    those is a support question whose answer is one of these words, and until
    this block they were on a machine the user was not looking at.
    """
    return (
        f"auto-submit {_on_off(watched.auto_submit)}"
        f" · delivery {watched.delivery}"
        f" · paste ≤ {watched.max_paste_chars:,} chars"
        f" · hover scan {_on_off(watched.hover_scan)}"
        f" · snap back {_on_off(watched.snap_back)}"
        f" · submit delay {watched.submit_delay_s:.1f}s"
    )


def preset_from_watched(watched: Watched, *, alerts: ServicePreset) -> ServicePreset:
    """The monitor's effective service, as the recipes' ``ServicePreset`` (§10.5).

    The whole of the brain's service knowledge in one adapter. Everything the
    automation ACTS on - what a paste may weigh, whether Enter may be pressed,
    how the auto-copy reaches the newest reply, whether a reply has to arrive
    fenced - is a fact about the chat the MONITOR is driving, so it comes off
    :class:`Watched` and never out of this host's ``[services.*]`` tables. That
    inversion is the point of the wave: a Chat UI reading its own copy of a
    preset would be composing turns for a service somebody else is running
    (§10.0's two bugs, in one sentence).

    Two kinds of field are deliberately NOT taken from ``watched``:

    * ``stable_seconds`` / ``tolerance`` / ``matcher`` / ``finish_signals`` -
      how pixels are searched, which never leaves the monitor and is left at the
      preset defaults here because nothing above this line reads them any more.
    * ``alert_sound`` / ``alert_repeat_seconds`` - the uh-oh alarm, which is a
      sound played on the machine the USER is sitting at. That is this one, so
      they come from ``alerts``: the host's own config (§10.5's "stay host-side").
    """
    return ServicePreset(
        key=watched.service or "",
        label=watched.label,
        max_paste_chars=watched.max_paste_chars,
        total_context_chars=watched.total_context_chars,
        wrap_blocks_in_fence=watched.wrap_blocks_in_fence,
        attachment_note=watched.attachment_note,
        hover_scan=watched.hover_scan,
        delivery=watched.delivery,
        scroll_action=watched.scroll_action,
        auto_submit=watched.auto_submit,
        submit_delay_s=watched.submit_delay_s,
        require_fenced_reply=watched.require_fenced_reply,
        extra_instructions=watched.extra_instructions,
        edit_by_lines=watched.edit_by_lines,
        snap_back=watched.snap_back,
        alert_sound=alerts.alert_sound,
        alert_repeat_seconds=alerts.alert_repeat_seconds,
    )


def _mcp_line(status: Any) -> str:
    """One server's row: name + human state (+ tools when connected, + the detail
    on the three states that are questions until it is read)."""
    state = str(getattr(status, "state", ""))
    parts = [str(getattr(status, "name", "")), _MCP_STATE_LABEL.get(state, state)]
    if state == "connected":
        count = int(getattr(status, "tool_count", 0) or 0)
        parts.append(f"{count} tool{'' if count == 1 else 's'}")
    detail = str(getattr(status, "detail", "") or "")
    # `invalid` sits with the failures, not with `disabled`: the label says the
    # entry was refused and only the detail says why, which is what the row is
    # for. The column ellipses it; `/mcp` prints the whole sentence.
    if state in ("failed", "needs_auth", "invalid") and detail:
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
    ``agentclip.shell.chat`` may not import ``agentclip.executor.mcp`` (tests/test_layering.py):
    the GUI only ever reads a status row's ``name``/``state``/``detail`` and
    hands them to a toast. Displaying MCP properly is a later increment.
    """

    def statuses(self) -> Sequence[Any]: ...
    def set_status_hook(self, cb: Callable[[Any], None] | None) -> None: ...


class ShellMonitor(MonitorLike, Protocol):
    """The monitor as a SHELL needs it: the controller's requirement, plus the
    three verbs only the object that owns the window ever calls.

    ``AutomationController.MonitorLike`` is the automation's own share of the
    contract and deliberately says nothing about lifetime - the controller never
    starts the monitor and never ends it. This shell does: it holds the handle,
    swaps a link into it and closes it when the window goes away.

    :meth:`watch` and :meth:`watched` are the other half, and they are the whole
    of §10.5: the brain names a WINDOW and reads the monitor's answer back, and
    there is nothing on this protocol for sending a service, a preset or a spec.
    Nothing LOCAL-ONLY is on it either - the detector used to be, and with it
    went ``copy_seen_note``'s report: a Chat UI that hosts no monitor has no
    detector object to ask (ui-monitor.md §10.2, §10.4).
    """

    async def close(self) -> None: ...

    async def watch(self, slot: AgentSlot) -> Watched: ...

    async def watched(self) -> Watched: ...


class DialledMonitor(UIMonitor, Protocol):
    """A monitor reached over the wire: the contract, plus what only a LINK has.

    ``RemoteUIMonitor`` structurally, but stated as a Protocol for the reason
    every other seam here is: the dial is injected, so a suite scripts one
    without a socket. The three extra members are exactly what split mode's
    reconnect story needs and nothing else - who we are attached to (for the
    toasts), whether the far PROCESS is still the one we handshook with, and the
    hook that fires when the link goes away for a reason nobody asked for.
    """

    @property
    def peer(self) -> str: ...

    @property
    def server_id(self) -> str: ...

    def on_disconnect(self, hook: Callable[[], None]) -> Callable[[], None]: ...


# How a host:port becomes a monitor. Injected for the reason every OS-touching
# seam in this shell is: the real one opens a TCP connection, and a suite must
# be able to run the whole connect/disconnect/redial story without one - and,
# since §10.2, the whole LAUNCH story too (``launcher``, its sibling).
MonitorDial = Callable[[str, int, str, str], Awaitable[DialledMonitor]]


async def _dial_remote_monitor(host: str, port: int, token: str, theme: str) -> DialledMonitor:
    """The real dial: one TCP connection and the monitor handshake (§6.5).

    ``token`` is §9.1's shared secret and "" is a real value - the right one for
    a monitor started with ``--no-token``. It becomes ``None`` on the wire,
    because the hello's field is optional and an empty string is a token that is
    simply wrong.

    ``theme`` is §11.7's palette - this shell's ``[gui] theme``, so that the
    Monitor UI on the other machine comes up wearing what this user picked
    rather than correcting itself a round trip later. "" is a real value the
    same way, and means the same thing: say nothing, let that window keep its
    own default.
    """
    return await RemoteUIMonitor.connect(host, port, token=token or None, theme=theme or None)


#: What ``--monitor`` can hand this view: a saved/typed address, the "start one
#: here" sentinel (§10.1), or nothing at all. Spelled once because the ctor, the
#: runner and ``run_gui`` all name it and a three-way union is exactly the kind
#: of thing that drifts into four.
MonitorLaunch = MonitorTarget | LaunchLocal | tuple[str, int] | None


def _as_monitor_target(given: MonitorLaunch) -> MonitorTarget | LaunchLocal | None:
    """Normalise what a launch handed over into the values this object holds.

    ``cli.main`` sends a :class:`MonitorTarget` since §9.2 (it has a token and,
    for ``--monitor @name``, an SSH hop to make) or a :class:`LaunchLocal` since
    §10.1; the bare ``(host, port)`` pair is §6.5's shape and is still accepted,
    because it is what "the address and nothing else" honestly looks like and
    half the suite says it that way.
    """
    if given is None or isinstance(given, (MonitorTarget, LaunchLocal)):
        return given
    host, port = given
    return MonitorTarget(name=f"{host}:{port}", host=host, port=port)


class _NoLauncher:
    """The launcher a view nobody wired one into gets: it starts nothing.

    Not a raise and not a real :class:`SubprocessLauncher`, for two opposite
    reasons. A default that could really spawn would mean any test that
    constructed a view with ``LaunchLocal`` would put an ``agentclip-monitor``
    on the developer's desktop; a default that raised on construction would make
    the seam mandatory for the dozen suites that never go near a monitor. So it
    is the honest "this build cannot launch one", reported through the ordinary
    failed-launch path (:data:`MONITOR_LOCAL_FAILED`).
    """

    def start(self, project_root: Path, *, global_config_path: Path | None = None) -> Any:
        raise RuntimeError("this view was built with no local monitor launcher")

    def stop(self) -> None:
        """Nothing was ever started."""

    def alive(self) -> bool:
        return False

    def exit_code(self) -> int | None:
        return None


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

    ``agentclip.executor.tools.shell.reason_line``, spelled again here rather than
    imported: this shell may not import that layer (tests/test_layering.py gives
    ``agentclip.shell.chat`` the engine's VALUE types and no tool code), and the
    alternative - widening the allowance so a view can reach a display helper -
    would open the whole tools package to a frontend. Six lines and a name that
    says where the original lives is the cheaper duplication. It moves down
    beside the other shared display text if a third caller ever appears - which
    is the route ``_distinct_rects`` took into ``agentclip.driver.automation``.
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
        fields["note"] = "no rule allows this - approve to run it once"
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


class GuiView:
    """``ChatView`` + ``AutomationView`` + ``AutomationHost``, over the bridge."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[EngineRequest], Link],
        project_root: Path,
        global_config_path: Path | None = None,
        mcp_manager: McpStatusSource | None = None,
        skills: Callable[[], SkillReport] | None = None,
        host: Any = None,
        remote: RemoteConnect | None = None,
        schedule: Callable[[Coroutine[Any, Any, Any]], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        on_config_change: Callable[[Config], None] | None = None,
        monitor_target: MonitorLaunch = None,
        launcher: LocalMonitorLauncher | None = None,
        dial: MonitorDial | None = None,
    ) -> None:
        self._bridge = bridge
        self._config = config
        self._provider = provider
        self._project_root = project_root
        # Where the service editor persists what it saved. Defaults to the real
        # global config.toml; tests override it so no run ever writes into the
        # user's actual config - the same shape (and the same reason) as
        # ``AgentClipApp._global_config_path``.
        self._global_config_path = (
            global_config_path if global_config_path is not None else default_global_config_path()
        )
        # How a saved Config reaches the ENGINE FACTORY. ``cli.py`` builds that
        # factory before this object exists, over a closure that has to keep
        # reading whatever config is current - the TUI's is ``lambda:
        # app.app_config`` and the editor reassigns that attribute. This shell's
        # equivalent is a hand-back: the factory reads a cell cli.py owns and
        # this is what writes it (see ``_adopt_config``).
        self._on_config_change = on_config_change if on_config_change is not None else _no_config
        self._mcp_manager = mcp_manager
        # Where `/skills` reads before a session exists. A cell rather than a
        # constructor-time binding for ``_mcp_manager``'s reason: a connect
        # swaps the machine the library belongs to, and ``_skill_rows`` re-reads
        # this so no source outlives the box it was about.
        self._skills = skills
        self._mcp_announced: set[tuple[str, str]] = set()
        # How a coroutine reaches the GUI's loop. Injected because the loop is
        # the RUNNER's (chat/runner.py) and this object must be constructible -
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
        # The question an open ``ask_user`` is asking, kept so the state push can
        # carry it to the banner above the composer. Scraped off the transcript
        # note the controller writes rather than taken from a new port method:
        # the TUI needs no such surface (its note plus the composer's answer mode
        # ARE the banner), and a ChatView method only one shell implements would
        # be a port that lies about what a chat view is (gui.md §2).
        self._pending_question: str | None = None
        # The status bar's "paused" reading, and nothing else: it MIRRORS what
        # the automation ended up doing with the watcher rather than deciding
        # it (``MainScreen.watch_paused`` / ``_mirror_watcher``).
        self._watch_paused = False

        # -- blocking prompts (confirm / prompt_text / show_summary) ---------
        self._prompts: dict[str, asyncio.Future[Any]] = {}
        self._prompt_ids = itertools.count(1)

        # -- the machine, and the automation core over it --------------------
        # One :class:`~agentclip.driver.monitor.protocol.UIMonitor` owns
        # everything on the far side of the screen: the poll loop and its
        # cadence, the detector and its trackers, the generation stamp, the
        # mouse, the keyboard and the clipboard watcher
        # (docs/design/ui-monitor.md §6.1). What is left up here is which WINDOW
        # to point it at (``watch``) and what its answers mean to the chrome.
        #
        # **It is always over the wire** (§10.2). There is no in-process tier
        # any more and no ``monitor=`` seam: the handle is a
        # :class:`SwitchableMonitor` that starts INERT and gets a link swapped
        # into it after first paint - whether that link goes to a monitor this
        # app launched on this PC (:class:`LaunchLocal`) or to one already
        # running on the machine the browser is on. Both are the same dial, the
        # same token and the same ``watched()`` stream, which is the whole point
        # of the wave: two ways to reach a screen were two things to disagree
        # (§10.0). A suite injects a fake ``dial`` and a fake ``launcher``.
        launch = _as_monitor_target(monitor_target)
        # Two fields out of one launch value, because they are two different
        # facts with two different lifetimes: WHERE to dial (which a local
        # launch does not know until the child has a port) and whether the
        # first thing ``start`` does is spawn one.
        self._launch_local = isinstance(launch, LaunchLocal)
        self._monitor_target: MonitorTarget | None = (
            None if isinstance(launch, LaunchLocal) else launch
        )
        self._launcher: LocalMonitorLauncher = launcher if launcher is not None else _NoLauncher()
        # Is the link we hold (or are redialling) the child WE started? It
        # changes three things and nothing else: the badge says "local", a
        # deliberate Disconnect stops the process, and a redial that keeps
        # failing can look at ``alive()`` and say the child is gone.
        self._local_launched = False
        # ...and the moment between "we are about to spawn one" and "it has a
        # port": the badge must say DOWN rather than NO MONITOR, because a
        # monitor IS on its way.
        self._launching = False
        # Said once per dead child, so a ten-minute backoff does not stack one
        # "the local monitor exited" toast per attempt.
        self._exited_said = False
        self._dial: MonitorDial = dial if dial is not None else _dial_remote_monitor
        # The tunnel under a Via-SSH link, so a redial can reopen one and a
        # disconnect can close it. None for a direct dial and for a local child.
        self._tunnel: Any = None
        # The far PROCESS's id from the last handshake, so a redial can tell a
        # resumed monitor from a restarted one (``DialledMonitor.server_id``).
        self._monitor_server_id: str | None = None
        # Is a redial loop already running? One at a time: a disconnect that
        # arrived while one was mid-backoff would otherwise start a second.
        self._redialling = False
        # Set when the window goes away, and the only thing that stops the
        # redial loop - a backoff that outlived its window would keep dialling
        # a machine nobody is watching any more.
        self._monitor_closing = False
        self._switch = SwitchableMonitor()
        self._monitor: ShellMonitor = self._switch
        # What the monitor last said it was watching, per window (§10.5). THE
        # source of the service key, the preset the recipes act on and the box
        # the brain believes is drawn: this object holds no service table of its
        # own any more and reads none out of ``config``. One entry per slot
        # rather than a single ``Watched``, because the sidebar describes the
        # SELECTED window while the recipes drive the LIVE one, and those part
        # company for the whole of a delegation.
        self._watched: dict[AgentSlot, Watched] = dict.fromkeys(AgentSlot, EMPTY_WATCHED)
        # The generation the readouts above were built from. A tick carrying a
        # different one means the monitor retargeted itself - a service picked
        # or a region redrawn in ITS window - so the brain re-reads (§10.5).
        self._watched_generation = -1
        # The last MONITOR SEES payload the page was sent (§11.4). Ticks arrive
        # about once a second and almost all of them say exactly what the one
        # before said, so the block is pushed on CHANGE and not on arrival:
        # without this the sidebar would repaint every second for the whole of a
        # session. ``None`` = nothing sent yet, which no payload compares equal
        # to, so the first tick always paints.
        self._sees_sent: dict[str, Any] | None = None
        self._automation = AutomationController(
            view=self,
            monitor=self._monitor,
            host=self,
            accepts=looks_like_protocol,
            on_clipboard_captured=self._clipboard_captured,
            has_appearance=self._live_has,
        )
        # Every tick, on the monitor's own thread (local inner) or reader task
        # (remote). Two jobs, both readout: keep the DETECTION block's
        # active-detector line truthful without a detector object to ask
        # (§10.2), and notice a generation this view has not seen.
        self._switch.subscribe(self._on_monitor_tick)
        # Whether the sub-agent window was ready last time anything readiness
        # depends on changed, so the "you must /new for it" toast fires once
        # rather than on every repaint (``MainScreen._delegation_ready``).
        self._delegation_ready = False
        self._logged_session_active = False
        # Is the quit-mid-turn confirm already up? ``AgentClipApp.action_quit``'s
        # ``isinstance(self.screen, ConfirmScreen)`` guard: hammering the window's
        # X while the dialog is open must not stack a second one behind it.
        self._quit_confirming = False

        # -- going remote (increment 7) ---------------------------------------
        # The machine this session's tools run on, and how a human changes it.
        # ``_host`` is only ever READ here (the link indicator, the close on
        # swap); everything that acts on it is below the seam. ``_remote`` is
        # None in a build with no way to go remote - and in every test that does
        # not ask for one - which is what makes the affordance absent rather
        # than broken.
        self._host = host
        self._remote = remote
        self._remote_target = str(getattr(host, "target", "")) if config.remote.is_remote() else ""
        # The dialog's model while it is up, and None the rest of the time - the
        # service editor's arrangement (``chat/remote.py`` holds every decision).
        self._dialog: ConnectDialog | None = None
        # The Monitor tab's model while the dialog is up (§9.2). A sibling of
        # ``_dialog`` rather than a field inside it: the two tabs answer two
        # unrelated questions (where the FILES are, where the SCREEN is) and
        # only the page's header knows they share a frame.
        self._monitor_dialog: MonitorDialog | None = None
        self._monitor_dialling = False
        # Why the last dial (or the last drop) parked the loop. Kept so the
        # dialog can show on its FORM what the toast said once and scrolled
        # away - the field the user has to fix is right there.
        self._monitor_failure = ""
        self._unprofiled_said: tuple[str, str] | None = None
        # The RemoteTarget that was actually dialled, for the save offer. Held
        # rather than re-parsed: what the user typed and what the config layer
        # resolved it to are not the same string.
        self._dialled: RemoteTarget | None = None
        self._connecting = False
        # How many password prompts this attempt has raised, so the dialog can
        # say "attempt 2 of 3" rather than looking like an unbounded loop. The
        # LIMIT is not ours - it is ``SshHost._PASSWORD_ATTEMPTS``, and this only
        # counts what that loop already decided to ask.
        self._password_asked = 0
        # Has the session controller been started yet? A ``--gui --ssh`` launch
        # defers it: the first session belongs to the box, and starting one
        # against this PC first would bootstrap a conversation the connect is
        # about to throw away.
        self._controller_started = False

        self._controller = SessionController(
            config,
            engine_factory,
            project_root,
            view=self,
            # Not ``mcp_manager.statuses``: an in-app connect replaces the
            # manager, so the source has to be one that re-reads it (_mcp_rows).
            mcp_statuses=self._mcp_rows,
            # Same trick, same reason: `/skills` before a session must name the
            # folders of whichever machine the NEXT session is built on.
            skills=self._skill_rows,
            # ...and the service a session runs on is the MONITOR's answer
            # (§11.9), so the controller asks rather than reading the local
            # `[services.*]` table its Config carries.
            preset_source=self.engine_preset,
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

    @property
    def monitor(self) -> ShellMonitor:
        """The machine the automation acts on - this shell's window onto the
        screen, the mouse and the clipboard, always over the wire (§10.2).
        Closed by the runner."""
        return self._monitor

    @property
    def launcher(self) -> LocalMonitorLauncher:
        """The local monitor's launcher, so the runner can stop the child it
        started after the wire link is closed (§10.1)."""
        return self._launcher

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
        self._push_link()
        self._push_commands()
        self._push_settings()
        self._push_docs()
        self._remember_own_window()
        # Nothing is drawn yet, so the monitor is configured onto a window with
        # no region and polls nothing - but this is the only writer of the
        # DETECTION block, and the block has to name the window it is about from
        # the first frame rather than after the first calibration.
        self._start_detector_worker()
        # ...and then the automation's own task, which is what turns a tick into
        # a decision (ui-monitor.md §6.2). Scheduled rather than called, for the
        # reason the retarget above is: the task has to be created ON the loop it
        # will run on, and it goes on AFTER the configure so the first recipe
        # observes a monitor that has already been pointed at a window.
        self._schedule(self._start_automation_loop())
        if self._mcp_manager is not None:
            # Hook first, paint second, so no transition can fall in the gap.
            self._mcp_manager.set_status_hook(self._mcp_status_hook)
            self._push_mcp()
        for warning in self._config.warnings:
            self.notify(warning, severity="warning", timeout=8)
        # ``--monitor``: same rule as the connect below, and for the same reason
        # (gui.md §2, "everything slow happens after first paint"). Neither the
        # spawn nor the dial is allowed to hold the first frame - the chrome is
        # already painted, the loop already says what it is doing, and the link
        # arrives into a window the user can see.
        #
        # Three values, three answers (§10.2): start one here and dial it, dial
        # the one named, or park with no screen at all. The last is a STATE, not
        # a failure - the Monitor tab is where both other answers come from.
        if self._launch_local:
            self._schedule(self._launch_local_monitor())
        elif self._monitor_target is not None:
            self._schedule(self._dial_monitor())
        else:
            self._park_disconnected(MONITOR_NONE)
        # ``--gui --ssh``: the window is already up (gui.md §2, "everything slow
        # happens after first paint"), so the connect that used to block the
        # launch runs HERE, in the dialog, with its fields filled from the flags.
        # The controller waits: a session started against this PC first would be
        # bootstrapped for the wrong machine and thrown away one step later.
        pending = self._remote.pending if self._remote is not None else None
        if pending is not None:
            self.open_connect(target=pending[0], root=pending[1] or "")
            self.connect_start()
            return
        self._start_controller()

    async def _start_automation_loop(self) -> None:
        """Start the one task that walks the recipes (``recipes/loop.py``).

        This shell is its only starter: nothing below picks the loop up on its
        own, so a shell that owns a loop owns saying when it begins. Idempotent,
        and it lives on this side of ``_schedule`` because ``start_loop`` needs a
        running loop to create the task on.
        """
        self._automation.start_loop()

    def _start_controller(self) -> None:
        """Start the session flow, once. Idempotent: a cancelled connect from a
        ``--gui --ssh`` launch reaches this the second time round."""
        if self._controller_started:
            return
        self._controller_started = True
        self._controller.start()

    def shutdown(self) -> None:
        """The window is closing: stop everything that touches the machine.

        The GUI's ``MainScreen.on_unmount``, and the half of it that needs no
        event loop - the clipboard watcher is stopped through a synchronous
        monitor verb and the alarm is a thread of the automation's own. The
        poller and the recipe loop are the other half: ``UIMonitor.close`` is a
        coroutine by contract and a task may only be cancelled from the loop it
        runs on, so both are :meth:`close`, which the runner awaits on the loop
        before it cancels everything left on it. Cancelling the session worker
        is the RUNNER's half too (it owns the loop the flows run on), exactly as
        Textual's unmount cancels workers.
        """
        if self._mcp_manager is not None:
            self._mcp_manager.set_status_hook(None)
        # Before the two below, because it is the flag a redial loop parked on
        # its backoff reads when it wakes: a window that is going away must not
        # get its link back one second later.
        self._monitor_closing = True
        self._automation.stop_input()
        self._automation.stop_alert()

    async def close(self) -> None:
        """Stop the recipe loop and the monitor's threads for good. Idempotent,
        like the verb below it - both the window's ``closing`` event and
        ``run_gui``'s ``finally`` reach the runner that calls this.

        The loop first: it is what still asks the monitor for observations, and
        cancelling it before the machine goes away means no recipe is left
        awaiting a tick nothing will ever push.
        """
        self._monitor_closing = True
        self._automation.stop_loop()
        await self._monitor.close()
        # The SSH tunnel a Via-SSH link rode on, if there is one. After the
        # monitor, because the pump under it is what the monitor's socket was
        # talking to: closing the tunnel first would turn an orderly close into
        # a dropped link (``executor/hosts/ssh.py:Tunnel``).
        self._close_tunnel()

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
        # The registry, the appearance and the user guide: all three are page
        # state a reload wipes, and all three are read off Python (the commands
        # from ``agentclip.shell.app.commands``, the theme from the config, the
        # guide from ``docs/*.md``).
        self._push_commands()
        self._push_settings()
        self._push_docs()
        # Same reason as the editor above: the connect dialog is a MODEL that
        # outlives a reload, and a page that came back under an open one (or
        # mid-checklist) must get it back rather than a window with a connect
        # running behind nothing.
        self._push_connect()
        self._bridge.send("armed", armed=self._automation.os_armed)

    def submit_text(self, text: str) -> None:
        """One door for every composer send - ``MainScreen._submit_text``.

        While the start prompt is up the text IS the task, except for a slash
        line, which is dispatched as a command so ``/log`` and friends stay
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
        remembered pattern when the gate carries one, the edits-only auto-accept
        at an edit gate that somehow carries none, and nothing at all otherwise -
        a button the page should not have offered is refused rather than
        reinterpreted.
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

    def dismiss_question(self) -> None:
        """Esc's last stage: put the question the banner is showing aside.

        The decision is the controller's whole (nothing is sent - the model
        keeps waiting and the next ordinary message answers it), so this is the
        marshal and nothing more. A no-op on the far side when no question is
        open or one was already dismissed, which is what lets the page press it
        without knowing."""
        self._controller.dismiss_pending_question()

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

    async def add_say(self, text: str) -> None:
        self._record("assistant", text)
        self._send_transcript(kind="say", text=text, label="assistant")

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
        # "? " is the controller's mark for the model's question, written just
        # before it parks on the answer (``_handle_step``). The note still goes
        # to the transcript exactly as it does in the TUI; this stash is what the
        # state push that follows a heartbeat later turns into the banner, so a
        # question cannot scroll away unread.
        if text.startswith("? "):
            self._pending_question = text[2:]
        self._send_transcript(kind="note", text=text)

    async def add_error(self, text: str) -> None:
        self._record(f"ERROR: {text}")
        self._send_transcript(kind="error", text=text)

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        payload = outbound.chunks[0]
        # The size is rendered HERE, not in app.js: the divisor is configuration
        # (``[general] chars_per_token``) and the page must not learn it - it
        # renders what it is handed, the way every other composed label in this
        # bridge works.
        size = fmt_tokens(outbound.total_chars, self._config.general.chars_per_token)
        note = f"→ {label} ({size})"
        self._record(f"{note} [outbound turn {outbound.turn}]", payload, fenced=True)
        self._send_transcript(
            kind="outbound",
            note=note,
            turn=outbound.turn,
            size=size,
            parts=len(outbound.chunks),
            payload=payload,
        )

    # The reply brackets, and they carry no content at all: the page remembers
    # which node the reply started at and scrolls there when it ends, so nothing
    # is recorded (the export is a list of EVENTS, and this is not one).
    async def begin_reply(self) -> None:
        self._send_transcript(kind="reply_start")

    async def reveal_reply(self) -> None:
        self._send_transcript(kind="reply_reveal")

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
        # The calibrations survive ``/new``, so the readiness that was already
        # true is still true and its one-shot toast must not re-fire.
        self._delegation_ready = self.delegation_available()
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
        self._events[window].append(LogEvent(datetime.now().strftime("%H:%M:%S"), note))
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
        service = self._service_for(_WINDOW_SLOTS[window])
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
            if view.session_active:
                # The one moment the MCP rows can have moved with no hook to say
                # so: in remote mode the settle rides ``build_session`` and there
                # is no push over the wire (docs/design/remote-executor.md
                # section 2.9), so a session start is when the target's runtime
                # first has anything to report. Local mode repaints an unchanged
                # block for the price of one ``statuses()`` read.
                self._push_mcp()
        # The two ways the loop settles home, read exactly as MainScreen reads
        # them: no session at all, or the turn finished interpreting and the
        # floor is back with the user (an open gate is still interpreting).
        if not view.session_active or (
            self._automation.loop_state is LoopState.INTERPRETING
            and (
                view.awaiting_answer
                or view.question_dismissed
                or not (view.busy or view.pending_approval)
            )
        ):
            self._automation.set_loop_state(
                LoopState.IDLE,
                "no session is running"
                if not view.session_active
                else "the turn finished and the floor is back with you",
            )
        self._push_state_event()
        # The link indicator lives in the sidebar and its truth is a flag on the
        # host that only an OPERATION flips (the reconnect model is lazy by
        # design - remote-ssh.md decision 5). A turn boundary is the moment most
        # likely to have just done one, so the block is repainted from here
        # rather than polled: nothing dials to keep a light green.
        if self._remote_target:
            self._push_sidebar()
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
            # The third answer is offered on exactly the TUI's terms: the
            # pattern this gate would remember (tui.md §2.4).
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

    def alert_attention(self) -> None:
        """The audible "your move", the same way the TUI gets it: the alarm is
        the automation controller's, so this shell inherits it for free."""
        self._automation.sound_attention_once()

    # == ChatView: clipboard / transport =======================================

    async def copy_outbound(self, text: str) -> None:
        """Deliver one outbound payload - the controller's ``copy_outbound``.

        With nothing calibrated (which is every GUI session in this slice) the
        delivery does the honest thing on its own: the payload is written to the
        real clipboard, ``verified_chatbox_target`` answers None, no click and no
        synthetic Ctrl+V happen, and the loop lands on ``MANUAL_INSERT`` with
        the "paste it yourself" banner up. That is the existing manual path,
        reached without a second implementation of it - and it is the same
        answer a fully calibrated session gets when the chat box is not on
        screen, because a paste may only ever go into a box that was found.
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
            # NOT_CALIBRATED gets its own sentence, and it names the MONITOR
            # (§11.3). "did not land (not_calibrated)" was the report this whole
            # wave opened on: the button was on screen, the link was green, and
            # the advice was unactionable because the missing half is a picture
            # on the OTHER machine.
            watched = self._watched[slot]
            head = (
                f"the monitor has no {TemplateKind.NEW_CHAT.label} captured for "
                f"{watched.label or self._service_for(slot) or 'this chat'} - "
                "capture one in the Monitor UI."
                if outcome is ElementClick.NOT_CALIBRATED
                else f"the new-chat click did not land ({outcome.name.lower()})."
            )
            self.notify(f"{head}{tail}", severity="warning")
            # No snap back on this branch, deliberately, and for the reason the
            # TUI gives: the browser is where the user has to finish the job, so
            # it keeps whatever focus it already has.
            return
        self.notify("new browser chat opened")
        # The click landed and the fresh chat is empty - nothing left to do over
        # there, so bring the user back here (the same call the TUI makes, beat
        # included).
        await self._automation.snap_back_after_click()

    # == the fullscreen child processes ========================================
    # There are none left in this window. The region picker, the capture overlay
    # and Identify are all translucent always-on-top tkinter windows in a
    # CHILD PROCESS (``screen/picker.py``), and every one of them is raised from
    # the calibration window now (``monitor_ui/view.py``), which owns the
    # one-at-a-time mutex over them. What is left on this side is the pair
    # below: the bracket this shell's OWN detectors need while that window is
    # up, because it is their browser those overlays land on.

    def suspend_detectors(self) -> None:
        """Stop polling (and disarm the trigger) while the calibration window is up.

        ``MainScreen.suspend_detectors``: a fullscreen child process thrown over
        the browser the detectors watch is a sustained large delta, which is
        precisely what arms the auto-copy on staleness alone. Left running, the
        overlay closing would then read the settled screen as a finished
        response and fire the copy flow at a chat nobody sent anything to.

        A SUSPEND rather than a retarget, and that is the monitor's own
        distinction: nothing has moved, so the ticks the interrupted loop is
        still finishing are honest readings of the same window and the
        generation is deliberately left alone (ui-monitor.md §6.1).
        """
        self._schedule(self._monitor.suspend())
        self._automation.reset_finish_trigger()

    def resume_detectors(self) -> None:
        """Poll again after ``suspend_detectors``, under the same configuration.

        Free to call beside a path that already retargeted (a chat region drawn
        in the calibration window rebuilds the poller on its way through): the
        detector never went anywhere - only its thread did - so a resume of a
        monitor that is already polling is a no-op down there rather than a
        second rebuild up here.
        """
        self._schedule(self._monitor.resume())

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
    # the same controller call, the same refusals, the same order. They are also
    # the whole gate: ``check_action``'s three-way dimming is drawn on the page
    # now (the key hint strip, from the ``state``/``status``/``run`` pushes), but
    # it is a cheatsheet - the answer to a key actually pressed is here, and a
    # key that cannot fire says why in a toast (docs/design/gui.md §3).

    def cycle_permission_mode(self) -> None:
        """shift+tab: build -> plan -> build.

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
        numbers would be a lie the moment they were read. The TUI dims the key
        in its footer and this shell dims it in the key hint strip, but the
        refusal is still said out loud here (docs/design/gui.md §3).

        It is NOT the end of the session: ``task_done`` leaves the user in the
        chat able to follow up, and the summary is one keypress away with three
        exits (undo one turn, start fresh, or just go back).
        """
        if not self._settled(
            "no session yet - describe a task first",
            "a turn is running - the summary opens when the floor is back with you",
        ):
            return
        self._controller.end_session()

    def undo(self) -> None:
        """`u`: put the most recent turn's file changes back.

        Gated exactly as ``MainScreen.check_action`` gates it (a live session, no
        turn in flight, the floor back with the user) and refused out loud where
        the TUI dims the key - increment 2's divergence, kept.

        What the confirm SAYS is the controller's, not this shell's: ``_undo_flow``
        composes both lines and awaits ``ChatView.confirm``, so the dialog that
        opens here is the same dialog the TUI opens, word for word. There is no
        preview API to list the individual files - the engine only knows what it
        restored *after* it has restored it (``engine/store/backups.py:UndoReport``) -
        so "what this restores" is the sentence the controller writes.
        """
        if not self._settled(
            "no session yet - nothing to undo",
            "a turn is running - undo waits until the floor is back with you",
        ):
            return
        self._controller.undo()

    def export_log(self) -> None:
        """`l`: write the whole chat log (every window, every sub-run) to a file.

        Gated on a session only: the export is a read-only snapshot that runs
        outside the flow worker, so it is safe mid-turn and the TUI gates it the
        same way (``check_action``: ``export_log`` -> ``session_active``).
        """
        if self._last_view is None or not self._last_view.session_active:
            self.notify("no session yet - nothing to export", severity="warning")
            return
        self._controller.export_log()

    def _settled(self, no_session: str, mid_turn: str) -> bool:
        """The gate `u`, `e` and `t` share: a session, no turn, AWAITING_REPLY/DONE.

        One implementation because ``check_action`` has one (``if action in
        ("undo", "end_session")``, and ``follow_up``'s clause is the same three
        conditions): two copies of "is the floor back with the user" is exactly
        how two keys start disagreeing about it. Only the two refusals differ,
        because a refusal that does not name what was refused is noise.
        """
        view = self._last_view
        if view is None or not view.session_active:
            self.notify(no_session, severity="warning")
            return False
        phase = view.snapshot.phase.name if view.snapshot else "IDLE"
        if view.busy or phase not in ("AWAITING_REPLY", "DONE"):
            self.notify(mid_turn, severity="warning")
            return False
        return True

    # == settings (F4) and the help sheet (F1) =================================

    def _push_commands(self) -> None:
        """The slash-command registry, for the popup AND the help modal.

        ``agentclip.shell.app.commands.COMMANDS`` is the one table every consumer
        reads - the controller's dispatch, `/help`, the unknown-command hint and
        the TUI's popup and help screen - and this is how it reaches a page that
        cannot import Python. It crosses ONCE per load rather than per keystroke:
        the filtering is the page's (a round trip per character would be a
        keystroke's worth of latency for string work), the DATA is never the
        page's, which is what keeps a command added here from being invisible
        there.
        """
        self._bridge.send(
            "commands",
            rows=[
                {"name": command.name, "label": command.label, "summary": command.summary}
                for command in COMMANDS
            ],
        )

    def _push_settings(self) -> None:
        self._bridge.send(
            "settings",
            theme=self._config.gui.theme,
            themes=[{"value": value, "label": label} for value, label in THEME_CHOICES],
        )

    def _push_docs(self) -> None:
        """The user guide, as markdown, for the titlebar's "docs" button.

        ``commands``' twin, and pushed beside it for the same reason: it is
        something the page needs to HAVE rather than to be told about, and a
        reload has nothing of its own to rebuild it from. What crosses is the
        text of ``docs/*.md`` verbatim - the files are the source of truth and
        nothing between here and the page edits them (``chat/docs.py``); the
        rendering is the page's own markdown renderer, the one the transcripts
        already use.
        """
        self._bridge.send(
            "docs",
            pages=[
                {"name": page.name, "title": page.title, "text": page.text, "found": page.found}
                for page in load_doc_pages()
            ],
        )

    def set_theme(self, theme: str) -> None:
        """F4's one setting: which CSS palette the page wears.

        Saved as it is picked (see :data:`THEME_CHOICES`). An ``OSError`` toasts
        and still applies in memory - the same trade ``_persist_services`` and
        the service editor's save already make, for the same reason: remembering
        a preference is a convenience, never the point of the press.
        """
        if self._persist_theme(theme):
            self.notify(THEME_SAVED, timeout=4)

    # -- ChatView: /theme ------------------------------------------------------
    # The same setting through the other door. F4 is a picker (it says "theme
    # saved" because the click itself said nothing); `/theme` is a command whose
    # answer the controller already toasts, so this half is silent and the two
    # share the mechanics rather than the message.

    def theme_choices(self) -> tuple[str, ...]:
        return tuple(value for value, _label in THEME_CHOICES)

    def current_theme(self) -> str:
        return self._config.gui.theme

    def apply_theme(self, name: str) -> None:
        self._persist_theme(name)

    def _persist_theme(self, theme: str) -> bool:
        """Wear ``theme``, remember it, repaint the page - and say whether the
        write landed.

        The repaint is the ``settings`` push: the page only ever paints what that
        event says (assets/app.css), so re-pushing it is how a theme changed from
        Python - a slash command, not a radio button - reaches the body class.
        """
        if theme not in VALID_GUI_THEMES:
            return False
        self._config = replace(self._config, gui=GuiConfig(theme=theme))
        try:
            save_gui_theme(theme, self._global_config_path)
        except OSError as exc:
            self.notify(f"could not save the theme: {exc}", severity="warning")
            saved = False
        else:
            saved = True
        self._push_settings()
        self._tell_monitor_theme(theme)
        return saved

    def _tell_monitor_theme(self, theme: str) -> None:
        """Let a connected monitor follow the palette (§11.7).

        Guarded on the LINK rather than on the target: with nobody on the line
        the switch is still holding whichever monitor died, and a ``set_theme``
        into a dead socket is an exception raised for a preference. The check is
        ``_push_link``'s own - a link is up exactly when the loop is not parked
        in DISCONNECTED.

        Nothing is awaited and nothing is reported. A theme that did not cross
        is a window on another machine that is still the colour it was, which is
        not a thing to interrupt somebody's F4 with - and a monitor a release
        behind has never heard of the verb (wire.py), which is the same
        non-event.
        """
        if self._automation.loop_state is LoopState.DISCONNECTED:
            return
        self._schedule(self._push_monitor_theme(theme))

    async def _push_monitor_theme(self, theme: str) -> None:
        with contextlib.suppress(Exception):
            await self._switch.set_theme(theme)

    # == quitting ==============================================================

    @property
    def mid_turn(self) -> bool:
        """Would closing the window now lose a turn? ``action_quit``'s formula.

        The ``not awaiting_new_session`` carve-out is the load-bearing half: the
        inline "describe the task" prompt leaves the session worker parked on a
        future, which reads as busy, but there is no turn to lose - so quitting
        from the empty start screen must not raise the warning.

        Read from the WINDOW's own thread (``GuiRunner.window_closing``), so it
        does nothing but read three flags a loop-thread write rebinds atomically.
        """
        view = self._last_view
        if view is None or not view.session_active or self._awaiting_new_session:
            return False
        return view.busy or view.pending_approval or view.awaiting_answer

    def request_quit(self) -> None:
        """ctrl+q: the same question closing the window asks, from the page.

        One decision, two doors - the window's ``closing`` handler cannot reach
        this method (it runs on a thread that may not touch the loop) so it
        re-reads :attr:`mid_turn` itself, but the *rule* is here and both doors
        end in the same confirm and the same close.
        """
        if self.mid_turn:
            self.confirm_quit()
            return
        self._on_exit()

    def confirm_quit(self) -> None:
        """Ask before losing the turn, then close - scheduled onto the loop.

        Never called from the window's closing handler directly: that handler
        runs on the window's own thread, which is the thread the bridge drainer
        parks against inside ``evaluate_js``, so it may only set a flag and
        return (``chat/runner.py``). This runs one hop later, on the loop, and the
        dialog it opens is the ordinary ``confirm`` modal every other prompt
        uses - which means the answer comes back through the ordinary bridge
        path and the window is destroyed from a thread that is not the one
        waiting on it.
        """
        if self._quit_confirming:
            return
        self._quit_confirming = True
        self._schedule(self._confirm_quit_flow())

    async def _confirm_quit_flow(self) -> None:
        try:
            confirmed = await self.confirm(QUIT_TITLE, QUIT_BODY)
        finally:
            self._quit_confirming = False
        if confirmed:
            self._on_exit()

    # == going remote (the SSH connect dialog) =================================
    # The GUI-only surface, against ``docs/design/ui-briefs/ssh-connect.md``.
    # The sequence is NOT here and not this shell's: ``connect_remote`` is the
    # same function the terminal launch drives, and what this section adds is
    # the three things a terminal cannot give it - prompts that work while a UI
    # owns the screen, progress that is visible, and a failure you can retry
    # without re-running the binary.

    def open_connect(self, target: str = "", root: str = "") -> None:
        """Open the dialog: the saved targets, the ssh_config aliases, the form.

        Both doors land here - the sidebar's "Connect to remote..." button and
        the ``--gui --ssh`` launch, the latter with its flags pre-filled. Also
        the brief's §4 "reconnect to a different target": there is no separate
        switch action, because there is no mid-session switching to offer
        (remote-ssh.md decision 4) - connecting ends the session and starts one
        on the new box, which is what this dialog does.
        """
        if self._remote is None:
            self.notify(CONNECT_UNAVAILABLE, severity="warning")
            return
        if self._connecting:
            self.notify(CONNECT_BUSY, severity="warning")
            return
        saved = saved_rows(self._config)
        self._monitor_dialog = None  # one tab at a time; the header switches
        self._push_monitor()
        self._dialog = ConnectDialog(
            saved=saved,
            aliases=alias_rows(self._ssh_aliases(), saved),
            target=target,
            root=root,
        )
        self._push_connect()

    def _ssh_aliases(self) -> list[str]:
        """``~/.ssh/config``'s literal Host entries. Never fatal: a machine with
        no ssh_config, or an unreadable one, offers no aliases and says nothing
        about it - the manual field still accepts everything ``--ssh`` does."""
        remote = self._remote
        path = remote.ssh_config_path if remote is not None else None
        try:
            return ssh_config_aliases(path)
        except Exception:  # noqa: BLE001 - a picker section is never worth a crash
            return []

    def connect_select(self, key: str) -> None:
        """A picker row: prefill the form. It does NOT connect (brief §3.2)."""
        if self._dialog is None:
            return
        self._dialog.select(key)
        self._push_connect()

    def connect_fields(self, target: str, root: str) -> None:
        """A keystroke in the form. No repaint: the page owns its own inputs
        while they have the caret, exactly as the service editor's ``reload``
        contract has it."""
        if self._dialog is None:
            return
        self._dialog.set_fields(target, root)

    def connect_start(self) -> None:
        """ "Connect", and "Retry" - the same press, because a retry IS a fresh
        attempt at the same values.

        The brief's §3.8 distinguishes retrying a failed ROOT check (which could
        in principle reuse the live connection) from retrying a failed dial.
        This shell does not: ``_abort`` closes the host on every failure, so
        there is never a live connection to reuse and one path is honest where
        two would be one path plus a claim.
        """
        dialog = self._dialog
        if dialog is None or self._remote is None:
            return
        if self._connecting:
            self.notify(CONNECT_BUSY, severity="warning")
            return
        if self.mid_turn:
            self.notify(CONNECT_MID_TURN, severity="warning")
            return
        if not dialog.begin():
            self._push_connect()
            return
        self._password_asked = 0  # a retry is a fresh attempt, and says so
        self._connecting = True
        self._push_connect()
        self._schedule(self._connect_flow(dialog.target, dialog.root))

    def connect_edit(self) -> None:
        """ "Edit": back to the form with the attempted values still in it -
        the fix for the single most common real failure, a typo'd root."""
        if self._dialog is None:
            return
        self._dialog.edit()
        self._push_connect()

    def connect_cancel(self) -> None:
        """ "Cancel"/"Close": drop the dialog. Never kills a connect in flight -
        the prompts are what a user cancels, and each of them returning "give
        up" is what ends an attempt (``PasswordPrompt``'s own contract)."""
        if self._connecting:
            self.notify(CONNECT_BUSY, severity="warning")
            return
        self._dialog = None
        self._push_connect()
        # A ``--gui --ssh`` launch that was cancelled has no session at all yet:
        # the user is on this PC now, and the composer has to become usable.
        self._start_controller()

    def connect_save(self, name: str) -> None:
        """ "Save this target": one ``[remote.<name>]`` table, global file only.

        gui.md §4 ruling 1. Nothing secret goes with it - see
        ``config.save_remote_target``, which writes four keys and no password.
        """
        dialog = self._dialog
        dialled = self._dialled
        if dialog is None or dialled is None:
            return
        clean = "".join(ch for ch in name.strip() if ch.isalnum() or ch in "-_.").strip("-")
        if not clean:
            return
        try:
            save_remote_target(replace(dialled, name=clean), self._global_config_path)
        except OSError as exc:
            self.notify(f"could not save the target: {exc}", severity="warning")
            return
        dialog.saved_as(clean)
        self._push_connect()

    # == the Monitor tab (docs/design/ui-monitor.md 9.2) =======================
    # The other half of the same dialog, and the one that does NOT end a
    # session: a mid-session dial is a LINK event. The loop parks in
    # DISCONNECTED, the SwitchableMonitor's inner is replaced and the recipe
    # re-derives from the screen (2.9) - which is exactly what a redial has
    # always done, so dialling a different monitor is indistinguishable from the
    # old one having been restarted somewhere else. The transcript, the session
    # and the engine are untouched: they are the Executor's half.

    def monitor_open(self) -> None:
        """Open the dialog on the Monitor tab (the header's second button)."""
        if self._monitor_dialling:
            self.notify(MONITOR_DIAL_BUSY, severity="warning")
            return
        self._dialog = None  # one tab at a time; the header switches between them
        self._monitor_dialog = MonitorDialog(
            saved=monitor_rows(self._config),
            ssh_targets=saved_rows(self._config),
            attached=self._monitor_peer(),
        )
        self._push_connect()
        self._push_monitor()

    def monitor_select(self, key: str) -> None:
        """A saved row: fill the form (and its token). It does NOT dial.

        The token comes from the CONFIG here rather than from the row, because a
        row is a thing the page draws and a token is not something to put in a
        list somebody screenshots (``remote.monitor_of_row``).
        """
        dialog = self._monitor_dialog
        if dialog is None:
            return
        dialog.select(key)
        saved = self._config.monitor.targets.get(key.removeprefix("monitor:"))
        if saved is not None:
            dialog.token = saved.token
        self._push_monitor()

    def monitor_fields(self, mode: str, host: str, port: str, token: str, via: str) -> None:
        """A keystroke in the form. No repaint, for ``connect_fields``' reason."""
        if self._monitor_dialog is None:
            return
        self._monitor_dialog.set_fields(mode, host, port, token, via)

    def monitor_start(self) -> None:
        """ "Attach", "Retry" and **Launch a local monitor** - one press.

        A retry IS a fresh dial, and so is a launch: the local mode differs only
        in where the address comes from (a child we spawn, rather than a form
        the user filled), which is the whole of §10.1's claim that "local" is
        not a different kind of monitor.
        """
        dialog = self._monitor_dialog
        if dialog is None:
            return
        if self._monitor_dialling:
            self.notify(MONITOR_DIAL_BUSY, severity="warning")
            return
        if not dialog.begin():
            self._push_monitor()
            return
        self._monitor_dialling = True
        self._push_monitor()
        if dialog.mode == MODE_LOCAL:
            self._schedule(self._local_flow())
            return
        self._schedule(self._monitor_flow(dialog.target()))

    def monitor_edit(self) -> None:
        """ "Edit": back to the form with what was attempted still in it."""
        if self._monitor_dialog is None:
            return
        self._monitor_dialog.edit()
        self._push_monitor()

    def monitor_cancel(self) -> None:
        """ "Close": drop the tab. Never cancels a dial in flight - the round
        trip is one call and the link it makes has nowhere else to go."""
        if self._monitor_dialling:
            self.notify(MONITOR_DIAL_BUSY, severity="warning")
            return
        self._monitor_dialog = None
        self._push_monitor()

    def monitor_save(self, name: str) -> None:
        """ "Save this monitor": one ``[monitor.<name>]`` table, global file only.

        Unlike its SSH sibling this one DOES write a secret - the token, if the
        form has one. Stated rather than hidden (``config.save_monitor_target``):
        the alternative to the token in the file the user chose is the token in
        a second file they did not.
        """
        dialog = self._monitor_dialog
        if dialog is None:
            return
        clean = "".join(ch for ch in name.strip() if ch.isalnum() or ch in "-_.").strip("-")
        if not clean:
            return
        try:
            save_monitor_target(dialog.target(clean), self._global_config_path)
        except OSError as exc:
            self.notify(f"could not save the monitor: {exc}", severity="warning")
            return
        dialog.saved_as(clean)
        dialog.saved = monitor_rows(self._reload_monitor_targets(clean, dialog.target(clean)))
        self._push_monitor()

    def monitor_forget(self, name: str) -> None:
        """Drop a saved ``[monitor.<name>]`` table. The picker's other half."""
        try:
            drop_monitor_target(name, self._global_config_path)
        except OSError as exc:
            self.notify(f"could not forget the monitor: {exc}", severity="warning")
            return
        self._config = replace(
            self._config,
            monitor=replace(
                self._config.monitor,
                targets={
                    key: value for key, value in self._config.monitor.targets.items() if key != name
                },
            ),
        )
        if self._monitor_dialog is not None:
            self._monitor_dialog.saved = monitor_rows(self._config)
            self._push_monitor()

    def monitor_disconnect(self) -> None:
        """Let go of the monitor. There is no screen after this, on purpose.

        §10.2 removed the fallback: there is no in-process monitor left for a
        disconnect to swap back to, and quietly launching a child instead would
        make the button mean "attach a different one". So the link is dropped,
        the child we started (if it is ours) is stopped, and the loop parks in
        DISCONNECTED until the user attaches or launches one. Nothing redials -
        a deliberate detach that a backoff undid one second later would be a
        button that does nothing.
        """
        if self._monitor_target is None:
            self.notify(MONITOR_ALREADY_NONE, severity="warning")
            return
        self._schedule(self._detach_monitor())

    async def _detach_monitor(self) -> None:
        """``monitor_disconnect``'s work, on the loop: drop everything.

        The order is the teardown's, not the dial's: the handle goes inert
        FIRST (so nothing that arrives late acts on a link we have let go of),
        then the child is stopped, then the tunnel under it. ``_monitor_target``
        is cleared before any of it, because that field is what the redial loop
        and the badge read.
        """
        self._monitor_target = None
        self._monitor_server_id = None
        self._monitor_dialling = False
        self._monitor_failure = ""
        self._exited_said = False
        previous = self._switch.swap(IdleMonitor())
        # Ours to stop, and only ours: a monitor somebody else started on that
        # machine is a standing process this window has no business ending.
        if self._local_launched:
            self._local_launched = False
            self._launcher.stop()
        self._close_tunnel()
        self._watched = dict.fromkeys(AgentSlot, EMPTY_WATCHED)
        self._watched_generation = -1
        self._automation.active_detectors = ()
        self._automation.set_loop_state(LoopState.DISCONNECTED, MONITOR_NONE)
        self._push_link()
        self.notify(MONITOR_NONE)
        if self._monitor_dialog is not None:
            self._monitor_dialog.detached()
            self._push_monitor()
        self._push_sidebar()
        # The link, not the handle: ``swap`` hands the old inner back precisely
        # so the side that knows whether it is dead gets to close it.
        self._schedule(previous.close())

    async def _monitor_flow(self, target: MonitorTarget) -> None:
        """One dial from the dialog, and what it does to the rest of the shell.

        The dial itself is ``_attach_monitor``, unchanged and shared with the
        launch flag and the redial loop - so a link made from a dialog and a
        link made from ``--monitor`` are the same link, configured by the same
        sequence, redialled by the same backoff. What this adds is the two
        things only a dialog owes: the target becomes the one the redial loop
        uses, and a failure lands on the form rather than in a toast that scrolls
        away.
        """
        dialog = self._monitor_dialog
        previous, previous_tunnel = self._monitor_target, self._tunnel
        self._monitor_target = target
        try:
            ok = await self._attach_monitor()
        finally:
            self._monitor_dialling = False
        if not ok:
            # Back to whatever was being watched before the attempt: a failed
            # dial must not leave a redial loop chasing a machine the user only
            # typed at. The reason is already on the loop's DISCONNECTED note,
            # and it goes on the form too.
            self._monitor_target, self._tunnel = previous, previous_tunnel
            if dialog is not None:
                dialog.failed(self._monitor_failure or MONITOR_DIAL_FAILED)
                self._push_monitor()
            return
        if dialog is not None:
            dialog.succeeded(peer=target.describe(), can_save=not dialog.is_saved())
            self._push_monitor()
        self._push_sidebar()

    async def _local_flow(self) -> None:
        """**Launch a local monitor**, and what a landed one does to the dialog.

        :meth:`_monitor_flow`'s sibling over :meth:`_launch_local_monitor`, and
        it differs in exactly one place: there is nothing to put back on failure.
        A form dial that fails restores the target the user was on; a launch
        that fails leaves this window with no monitor, which is the state it was
        already in - the button is only ever pressed from there.
        """
        dialog = self._monitor_dialog
        try:
            ok = await self._launch_local_monitor()
        finally:
            self._monitor_dialling = False
        if dialog is None:
            return
        if ok:
            dialog.succeeded(peer=MONITOR_LOCAL_PEER, can_save=False)
        else:
            dialog.failed(self._monitor_failure or MONITOR_DIAL_FAILED)
        self._push_monitor()
        self._push_sidebar()

    def _monitor_peer(self) -> str:
        """Which monitor is attached, "" when there is none."""
        if self._local_launched or self._launching:
            return MONITOR_LOCAL_PEER
        target = self._monitor_target
        return target.describe() if target is not None else ""

    def _reload_monitor_targets(self, name: str, target: MonitorTarget) -> Config:
        """Fold a just-saved target into the live config, without a re-read.

        Re-reading the file would be re-reading the machine's whole
        configuration to learn one thing this process just wrote - and in a
        remote session the config that is live belongs to the TARGET, which has
        no [monitor] tables at all (they are global-only by design).
        """
        self._config = replace(
            self._config,
            monitor=replace(
                self._config.monitor,
                targets={**self._config.monitor.targets, name: replace(target, name=name)},
            ),
        )
        return self._config

    def _push_link(self) -> None:
        """The titlebar's MONITOR badge: which monitor, and is the line up.

        Three states and nothing finer (§10.2):

        * ``none`` - no monitor at all. ``--monitor none``, or a deliberate
          Disconnect. Red, because a Chat UI with no screen can drive nothing,
          and the fix is one click away on the Monitor tab.
        * ``up`` - a link is live. The peer is ``local`` for the child this app
          started and the target's own address for anything else.
        * ``down`` - there is a target and no live link: launching, dialling,
          redialling, refused. Red, with the reason.

        ``local`` as a STATE is gone with the in-process monitor: watching this
        PC's screen is now a link like any other, and the badge says so.
        """
        target = self._monitor_target
        if target is None and not self._launching:
            self._bridge.send("monitor_link", state="none", peer="", reason="")
            return
        live = self._automation.loop_state is not LoopState.DISCONNECTED
        self._bridge.send(
            "monitor_link",
            state="up" if live else "down",
            peer=self._monitor_peer(),
            reason="" if live else (self._monitor_failure or MONITOR_DIALLING),
        )

    def _push_monitor(self) -> None:
        """The whole Monitor tab in one event; ``open: false`` is closed."""
        if self._monitor_dialog is None:
            self._bridge.send("monitor", open=False)
            return
        self._bridge.send("monitor", **self._monitor_dialog.event())

    def reconnect_now(self) -> None:
        """The link indicator's button (gui.md §4 ruling 5).

        Deliberately the SAME ``_ensure`` the next operation would have called
        (``SshHost.reconnect``), not a second dial path: the model stays lazy
        and reactive, and this only spends the cost early. On a live link it is
        a no-op that says so.
        """
        reconnect = getattr(self._host, "reconnect", None)
        if reconnect is None:
            self.notify(RECONNECT_LOCAL, severity="warning")
            return
        self._schedule(self._reconnect_flow(reconnect))

    async def _reconnect_flow(self, reconnect: Callable[[], bool]) -> None:
        ok = await asyncio.to_thread(reconnect)
        self.notify(
            (RECONNECT_OK if ok else RECONNECT_FAILED).format(target=self._remote_target),
            severity="information" if ok else "warning",
        )
        self._push_sidebar()

    # -- the flow --------------------------------------------------------------

    async def _connect_flow(self, target: str, root: str) -> None:
        """Run the sequence off the loop, with this window answering its questions.

        ``connect_remote`` is synchronous and blocking (it dials, it waits on a
        server), so it goes to a worker thread - which puts its three prompt
        callbacks on that thread too. Each one therefore hops back: the modal is
        opened and awaited ON the loop and the worker parks on the answer, which
        is the only arrangement in which a blocking auth flow and a single-
        threaded UI can both be told the truth.

        Then a seventh beat the sequence does not run: starting the engine on
        the target and shaking hands with it. It is the same shape - blocking,
        on a worker thread, reported as a checklist row - and it is what makes
        the connect real, because since the flip the session's engine, stores,
        policy, skills and MCP servers are all over there
        (docs/design/remote-executor.md §2.12).
        """
        remote = self._remote
        dialog = self._dialog
        if remote is None or dialog is None:  # pragma: no cover - guarded by callers
            return
        loop = asyncio.get_running_loop()
        prompts = ConnectPrompts(
            password=lambda text: self._ask(loop, self._password_modal(text)),
            host_key=lambda host, keytype, fingerprint: bool(
                self._ask(loop, self._host_key_modal(host, keytype, fingerprint))
            ),
            keyboard_interactive=lambda title, instructions, fields: self._ask(
                loop, self._keyboard_modal(title, instructions, fields)
            ),
        )

        def report(event: StepEvent) -> None:
            """The worker thread's half of the checklist: hand the beat over."""
            loop.call_soon_threadsafe(self._connect_step, event)

        try:
            connected = await asyncio.to_thread(
                connect_remote,
                target,
                root or None,
                local_root=remote.local_root,
                service_override=remote.service_override,
                prompts=prompts,
                on_step=report,
                global_config_path=remote.global_config_path,
            )
            # The seventh row, and the one that decides whether there is a
            # session at all: ``build`` launches ``agentclip-engine`` on the box
            # and shakes hands with it (docs/design/remote-executor.md §2.12).
            # It goes to a worker thread for the same reason the sequence does -
            # it opens a channel and waits on a process across a network - and
            # it is inside this ``try`` so the dialog stays busy until the engine
            # is really answering.
            self._connect_step(StepEvent(STEP_ENGINE, "running"))
            runtime = await asyncio.to_thread(remote.build, connected)
            self._connect_step(StepEvent(STEP_ENGINE, "ok"))
        except ConnectError as err:
            dialog.failed(err)
            return
        except EngineLinkError as exc:
            # A launch that produced no handshake, or a target running another
            # wire version: both arrive already classified into the sentence a
            # human can act on (§2.9, §2.12), so the row shows it verbatim.
            self._connect_step(StepEvent(STEP_ENGINE, "failed", exc.detail or str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - a dial can fail in ways paramiko owns
            dialog.failed(ConnectError(dialog.failed_step or "connect", str(exc)))
            return
        finally:
            self._connecting = False
            self._push_connect()
        await self._adopt_remote(runtime, connected)
        dialog.succeeded(
            connected=self._remote_target,
            policy=policy_lines(self._config, self._remote_target),
            can_save=not dialog.is_saved(),
        )
        self._push_connect()
        self.notify(CONNECT_DONE.format(target=self._remote_target), timeout=8)

    def _connect_step(self, event: StepEvent) -> None:
        """One beat of the checklist, marshalled back onto the loop.

        The reporter runs on the worker thread; the dialog's state is loop-owned
        like every other model in this shell, so nothing mutates it from there.
        """
        if self._dialog is None:
            return
        self._dialog.step(event)
        self._push_connect()

    def _ask(self, loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any]) -> Any:
        """Open a modal from the WORKER thread and block there for the answer.

        The inverse of every other hop in this shell. ``None`` on any failure,
        which is what each of the three callbacks reads as "give up" - so a
        window closing under an open password prompt ends the attempt rather
        than wedging a thread inside paramiko.
        """
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop).result()
        except Exception:  # noqa: BLE001 - a cancelled loop is a cancelled prompt
            coro.close()
            return None

    async def _password_modal(self, prompt: str) -> str | None:
        """One password attempt. Three of these happen at most, and the count is
        ``SshHost._PASSWORD_ATTEMPTS``' - this dialog adds no fourth."""
        self._password_asked += 1
        answer = await self._modal(
            "connect_password",
            title=PASSWORD_TITLE.format(target=prompt.removeprefix("password for ").rstrip(": ")),
            hint=PASSWORD_HINT.format(n=self._password_asked, total=PASSWORD_ATTEMPTS),
        )
        return answer if isinstance(answer, str) and answer else None

    async def _host_key_modal(self, host: str, keytype: str, fingerprint: str) -> bool:
        """OpenSSH's own question. Never auto-accepted, never remembered by this
        dialog: accepting writes the key through the same path ``ssh.py`` always
        did, declining raises out of ``connect()`` (brief §3.6)."""
        return bool(
            await self._modal(
                "connect_hostkey",
                title=HOST_KEY_TITLE.format(host=host),
                body=HOST_KEY_BODY.format(keytype=keytype, fingerprint=fingerprint),
            )
        )

    async def _keyboard_modal(
        self, title: str, instructions: str, fields: Sequence[tuple[str, bool]]
    ) -> list[str] | None:
        """The keyboard-interactive/2FA challenge: one field per prompt, masked
        where the server said ``echo=False``.

        **Not reachable yet**, and deliberately so: ``SshHost._authenticate``
        still lets paramiko's own ``auth_interactive_dumb`` handle this path and
        that one reads stdin (see the TODO there). The plumbing is whole from
        here down so the day it is wired the dialog is already the contract
        ``ssh-connect.md`` §3.7 designed against paramiko's handler.
        """
        answer = await self._modal(
            "connect_keyboard",
            title=title or "Two-factor authentication",
            body=instructions,
            fields=[{"prompt": text, "echo": bool(echo)} for text, echo in fields],
        )
        if not isinstance(answer, list):
            return None
        return [str(item) for item in answer]

    # -- the session boundary --------------------------------------------------

    async def _adopt_remote(self, runtime: RemoteRuntime, connected: ConnectedRemote) -> None:
        """One session, one host: point everything at the machine just dialled.

        The state a fresh ``--ssh`` launch lands in is a controller assembled
        from the remote engine factory, the remote project root and the config
        read off the target - so those three go over together, through
        ``SessionController.rebind``, which is the core half of this increment.
        Not a new controller: the live one is parked on ``prompt_new_session``
        and reads all three when it BUILDS, which has not happened yet. Its
        window tabs, their services and their calibrations survive for the same
        reason ``/new`` keeps them - a browser window outlives the sessions run
        in it, and the browser did not move.

        A session already running is ended first (``request_new_session``, the
        one door that knows how to do it), and ``rebind`` refuses under a live
        one rather than trusting the caller - "host-hopping = new session"
        expressed as a precondition rather than as a silent swap.
        """
        if self._mcp_manager is not None:
            self._mcp_manager.set_status_hook(None)
        self._project_root = runtime.project_root
        self._config = runtime.config
        self._host = runtime.host
        self._remote_target = runtime.target
        self._dialled = connected.target
        self._mcp_manager = runtime.mcp_manager
        self._skills = runtime.skills
        self._mcp_announced.clear()
        # cli.py's engine factory reads a cell it owns, and the one this runtime
        # carries was built against the remote config - hand it over for the
        # same reason the service editor does (``_adopt_config``).
        self._on_config_change(runtime.config)
        live = self._last_view is not None and self._last_view.session_active
        if live:
            # Ends the conversation that belongs to the OLD machine and leaves
            # the controller parked on a fresh prompt. It runs as a flow, and
            # ``_reset_session`` drops ``session_active`` on its first line - so
            # the loop is yielded to before the rebind, which REFUSES under a
            # live session rather than trusting this ordering.
            self._controller.request_new_session()
            for _ in range(3):
                await asyncio.sleep(0)
        # All four ingredients together, MCP included: the runtime a connect
        # hands back carries the TARGET's MCP as well as its config and root,
        # and the source is stated here rather than assumed because this is the
        # one call that says which machine the next session is built from. It is
        # the same re-reading callable the constructor passed (``_mcp_rows``
        # reads ``_mcp_manager``, assigned above), so no source outlives the
        # machine it was about.
        self._controller.rebind(
            runtime.config,
            runtime.engine_factory,
            runtime.project_root,
            mcp_statuses=self._mcp_rows,
            skills=self._skill_rows,
        )
        self._automation.log_harness(
            KIND_SESSION, f"connected to {runtime.target}: this session's tools run over there"
        )
        if self._mcp_manager is not None:
            self._mcp_manager.set_status_hook(self._mcp_status_hook)
            self._push_mcp()
        self._push_tabs()
        self._push_sidebar()
        self._push_status()
        self._push_state_event()
        for warning in runtime.config.warnings:
            self.notify(warning, severity="warning", timeout=8)
        # A ``--gui --ssh`` launch deferred this so the first session would
        # belong to the box rather than to this PC; every other road here has
        # already started it and the call is free.
        self._start_controller()

    def _push_connect(self) -> None:
        """The whole dialog in one event; ``open: false`` is the closed state."""
        if self._dialog is None:
            self._bridge.send("connect", open=False)
            return
        self._bridge.send("connect", **self._dialog.event())

    def _remote_lines(self) -> list[str]:
        """The PROJECT block's remote marker: the standing half of ruling 6.

        Three facts, and each of them is one a user will otherwise go looking
        for on the wrong machine: which box the tools run on, whether the link
        is up, and where this session's permissions came from.
        """
        if not self._remote_target:
            return []
        live = bool(getattr(self._host, "connected", True))
        lines = [self._remote_target, LINK_LIVE if live else LINK_LOST]
        reconnects = int(getattr(self._host, "reconnects", 0) or 0)
        if reconnects:
            lines.append(LINK_RECONNECTS.format(count=reconnects))
        lines.append(
            self._config.permission_source
            or f"no permissions.json on {self._remote_target} - shipped defaults"
        )
        return lines

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
        self._automation.start_input()
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

    def press_enter(self) -> None:
        """The sidebar's PRESS ENTER (ui-monitor.md §11.8): tap Enter in the
        chat box now, for the auto-submit the page dropped. Scheduled like the
        retry above; the refusals are the controller's."""
        self._schedule(self._automation.press_enter())

    def copy_again(self) -> None:
        """The sidebar's COPY AGAIN (§11.8): run the auto-copy harvest now, for
        the finish the detectors never saw. Scheduled like the retry above."""
        self._schedule(self._automation.copy_again())

    # == no calibration door ==================================================
    # Everything made of PIXELS is the MONITOR PROCESS's (ui-monitor.md §10.2):
    # the service editor, the ELEMENTS column, the chat-region picker and
    # ``/identify`` are surfaces of ``agentclip-monitor``'s own window - the
    # child this app launched here, or the one already running on the machine
    # the browser is on. This window opens none of them and hosts none of them.
    #
    # §10.2 left five affordances behind pointing AT that window - F2, the
    # titlebar's **monitor UI** button, the sidebar's **Edit services...** and
    # **Set chat region...**, and ``/identify`` - each of which did nothing but
    # say where to go. §11.2 deleted all five: ``open_calibration`` and
    # ``show_identify_overlay`` are gone from this view, ``calibrate`` from the
    # bridge and ``/identify`` from the command table.

    def _after_calibration(self) -> None:
        """Tell the user ONCE when the sub-agent window becomes usable.

        ``MainScreen._after_calibration``, minus its sidebar repaint (the
        caller's ``_push_sidebar`` already carries the readiness line). The
        one-shot matters because the delegate tool is baked into the bootstrap:
        a window that just became ready reaches the model on the next ``/new``
        and not before, which is not something a readiness line says.
        """
        ready = self.delegation_available()
        if ready and not self._delegation_ready:
            self.notify("sub-agent slot ready - /new to give the model the delegate tool")
        self._delegation_ready = ready

    def _adopt_config(self, config: Config) -> None:
        """``MainScreen.update_config``, spelled for this shell's readouts.

        Everything here reads directly off ``self._config``; the controller is
        updated too, so the NEXT session (and any ``/new``) picks the edit up. A
        session already in flight keeps the Config snapshot its Engine was built
        from - that is the contract, not an omission.
        """
        self._config = config
        self._controller.update_config(config)
        # cli.py's engine factory reads a cell it owns, so the shell that
        # rebound the config has to hand it over - the TUI's equivalent is that
        # its closure reads the attribute the editor reassigned.
        self._on_config_change(config)
        # Which service each window drives is NOT re-derived here any more: it
        # is the monitor's answer (§10.5), and a config saved on this host says
        # nothing about it. What a fresh config can still change is the alarm
        # this machine plays, which ``live_preset`` folds in on its next read.
        self._push_tabs()
        self._push_sidebar()
        self._after_calibration()
        self._push_status()

    # == ChatView: sub-agent transport =========================================

    def delegation_available(self) -> bool:
        return can_delegate(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self.captured_for(AgentSlot.SUBAGENT),
        )

    def delegation_missing(self) -> tuple[str, ...]:
        return missing(
            self._automation.calibration(AgentSlot.SUBAGENT),
            self.captured_for(AgentSlot.SUBAGENT),
        )

    async def start_chat(self, session: SessionRef) -> bool:
        slot = AgentSlot.SUBAGENT if session.role == "subagent" else AgentSlot.MASTER
        return await self._automation.start_browser_chat(slot)

    async def end_chat(self, session: SessionRef) -> None:
        self._automation.end_browser_chat()

    # == ChatView: scheduling + lifecycle ======================================

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Put a controller flow on the GUI's asyncio loop (chat/runner.py)."""
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
        #
        # Two surfaces, not one. The rail draws every state at once and marks
        # this one; the status bar's watch segment draws the single sentence for
        # where BOTH machines are (``describe``), and the loop is half of that
        # pair - so a state change that never repainted the bar would leave it
        # saying the phase's story while the browser had moved on.
        self._push_rail()
        self._push_status()

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
        """The ELEMENTS column is another window's now: nothing to draw here.

        Implemented rather than dropped because it is ``AutomationView``'s
        (``driver/automation/view.py``) and a port method is not optional - the
        controller routes a mapping through it on every tick and on every
        hover-scan sighting, and a view that did not answer would strand the
        poller. This shell wires no ``crop_elements``, so what arrives is raw
        sightings it has no use for; the pictures are cut and drawn in
        ``monitor_ui/view.py``, off that window's own monitor
        (ui-monitor.md 6.4).
        """

    def show_paste_flash(self, text: str, *, retry: bool = False) -> None:
        self._bridge.send("flash", show=True, text=text, retry=retry)

    def hide_paste_flash(self) -> None:
        self._bridge.send("flash", show=False)

    def paint_armed(self, armed: bool) -> None:
        self._bridge.send("armed", armed=self._automation.os_armed)

    # == AutomationHost ========================================================

    def live_preset(self) -> ServicePreset:
        """The LIVE window's service, as the monitor last described it (§10.5).

        Derived on every read rather than cached: the monitor can retarget
        itself (a service picked in ITS window) and the answer this returns has
        to be the one the last tick's generation belongs to.
        """
        return self._preset_for(self._automation.live_slot)

    def captured_for(self, slot: AgentSlot) -> tuple[TemplateKind, ...]:
        """Which appearances the MONITOR holds for ``slot``'s service (§11.3).

        Read straight off the last ``Watched`` this window adopted, which is the
        whole of the change: this used to load a ``ServiceProfile`` from THIS
        machine's profile store, and on any desktop but the one the pictures
        were taken on that store is empty - so a green link and a fully
        calibrated browser still got NOT_CALIBRATED on every click.
        """
        return self._watched[slot].captured

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
        different conversation's. The search is the controller's
        (``AutomationController.find_all``); this shell, like the TUI, only has
        to keep the host method the sequences ask through.

        Empty - never raised - for every way this comes up empty, which in this
        slice is always: the GUI has no calibration surface, so no chat region
        is ever drawn.
        """
        return await self._automation.find_all(kind, slot, scene=scene)

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        return await self._automation.verified_copy_click(target)

    async def ingest_harvest(self) -> None:
        """A verified copy click landed: show a non-protocol reply as prose.

        Only inside the controller's one-shot ``prose_window`` - armed for the
        verified click and disarmed the moment this returns - because that is
        what makes THIS clipboard text known to be the model's reply rather than
        something the user copied. Protocol-shaped harvests are left alone
        entirely: the watcher ingests those on its own, and reading them here
        too would ingest them twice.
        """
        if not self._automation.prose_window:
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
            "we clicked the copy button ourselves, so it goes to the "
            "transcript as prose",
        )
        self._automation.set_loop_state(
            LoopState.INTERPRETING, "the reply has no CLIP blocks - showing it as prose"
        )
        self._controller.submit_clipboard(text, accept_prose=True)

    def copy_seen_note(self) -> str:
        """Nothing to add to a failed harvest's report, in this shell.

        This used to be "the poller last saw a copy button 12s ago", read off
        the detector object the Chat UI owned. There is no such object here any
        more (§10.2) - the detector lives in the monitor process, and its
        memory of a sighting has no field on the tick. Implemented rather than
        dropped because ``AutomationHost`` still asks, and ``""`` is the honest
        answer: the sentence the auto-copy recipe builds simply ends earlier.
        (§10.4 lists this as the wave's one deliberate loss, and names the way
        back: a tick field.)
        """
        return ""

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

    # == the monitor's target ==================================================

    def _start_detector_worker(self) -> None:
        """Retarget the monitor onto the LIVE window - the synchronous door.

        Every caller here is chrome (a live window moved, a config adopted,
        ``/new``) and none of them can await, while ``watch`` is a coroutine by
        contract - a round trip to another process. So the door stays
        synchronous and the retarget goes on the loop, where it runs in the
        order it was asked for: the monitor's own verbs never suspend half-way,
        so a suspend, a retarget and a resume queued back to back land in that
        order.
        """
        self._schedule(self._retarget_monitor())

    async def _retarget_monitor(self) -> None:
        """Name the LIVE window to the monitor, and adopt what it answers.

        The whole of §10.5's inversion in two lines. This used to compose a
        ``MonitorSpec`` - a service key, a rectangle, a finish checklist, a
        matcher - and send it; it now sends a WINDOW and reads the monitor's own
        answer back. Everything that made that spec (which service each window
        is, what its preset says, where its box was drawn) is a fact about the
        machine the browser is on, and the monitor is where those facts are
        edited, so a brain that composed them was composing them for a service
        somebody else was running (§10.0).

        One round trip rather than ``configure`` + ``watched``: the brain needs
        both halves before it can act on anything, and a retarget that got only
        the generation would be driving a service it had not been told about.
        """
        slot = self._automation.live_slot
        self._adopt_watched(slot, await self._monitor.watch(slot))

    def _adopt_watched(self, slot: AgentSlot, watched: Watched) -> None:
        """Make the monitor's answer this shell's picture of ``slot``.

        Every readout below is derived rather than remembered - the service key,
        the preset the recipes act on, the box, the DETECTION lines - so this is
        the ONE writer, and it is called from both doors that can hear an
        answer: a retarget we asked for, and a generation that changed on the
        far side without us (:meth:`_on_monitor_tick`).

        ``forget_verdicts`` comes after the answer and not before it: the
        generation is bumped by then, so a probe still in flight from the run
        that just ended is a ghost and cannot land on the state just cleared.
        """
        self._watched[slot] = watched
        self._watched_generation = watched.generation
        service = watched.service or ""
        # What the monitor SETTLED on. The brain holds no rectangle of its own -
        # the box was drawn on the monitor's desktop and lives in its store
        # (§9.1) - so it adopts the answer, or every recipe keeps saying "no
        # chat window is drawn" over a link whose far end can see the window
        # perfectly well.
        if self._automation.calibration(slot).chat_region != watched.region:
            self._automation.set_calibration(slot, watched.region)
        self._automation.forget_verdicts()
        tick = self._monitor.latest
        active = tick.active_detectors if tick is not None else ()
        self._automation.active_detectors = active
        self._push_tabs()
        peer = self._monitor_peer()
        if peer and service and not watched.profiled:
            # The trap, said where it bites: the key that monitor is driving
            # names no appearance on its own machine, so every element verdict
            # over there is NOT_CALIBRATED and nothing else explains it.
            self._paint_detection(MONITOR_UNPROFILED.format(service=service))
            said = (peer, service)
            if self._unprofiled_said != said:
                self._unprofiled_said = said
                self.notify(
                    MONITOR_UNPROFILED_TOAST.format(peer=peer, service=service),
                    severity="warning",
                    timeout=12,
                )
        elif watched.region is None:
            self._paint_detection(STALE_UNSET)
        elif not active:
            # The service's checklist is empty, or asks only for appearances it
            # has none of. Say so where the stale verdict would go: the
            # consequence (auto-copy will never fire) is otherwise invisible
            # until the user waits for a copy that never comes.
            self._paint_detection(STALE_OFF)
        else:
            # Whether the stale line is a live verdict or an explanation of its
            # own silence: it is the one detector with no appearance behind it.
            self._paint_detection(STALE_CALIBRATED if "stale" in active else STALE_UNTICKED)

    def _on_monitor_tick(self, tick: Tick) -> None:
        """Every tick, on the monitor's thread. Readout only, and non-blocking.

        Three jobs, and the first two are things this shell used to get from an
        object it owned. The active-detector list is the DETECTION block's "is anything
        even watching" line, and it rides every tick (§10.2: there is no
        detector up here to ask). And the GENERATION is how a service picked or
        a region redrawn in the MONITOR's own window reaches this one: the
        monitor bumped it without a frame from us, so a stamp we have not seen
        means "ask again" (§10.5).
        """
        self._automation.active_detectors = tick.active_detectors
        # ...and a third since §11.4: the sightings on this tick are the whole
        # of what "on screen" means in the F2 block. Filtered by change, so a
        # screen that has not moved costs one dict comparison per second.
        self._push_monitor_sees()
        if tick.generation != self._watched_generation:
            # Claimed here, on the tick thread, so the next tick down the wire
            # does not queue a second read of the same answer.
            self._watched_generation = tick.generation
            self._schedule(self._reread_watched())

    async def _reread_watched(self) -> None:
        """``watched()`` after a generation we did not ask for. Never raises.

        A read rather than a ``watch``: the monitor has already retargeted
        itself and asking it to do so again would bump the generation once more,
        which is a loop with a round trip in it.
        """
        try:
            watched = await self._monitor.watched()
        except Exception:  # noqa: BLE001 - a link that died mid-read has its own path
            return
        self._adopt_watched(self._automation.live_slot, watched)
        self._push_sidebar()
        self._push_status()
        # And F2, which is the block this answer changed the MOST: its rows and
        # its settings line are read straight off ``Watched``, and the push the
        # arriving tick already made was made against the previous one. Cheap to
        # repeat - :meth:`_push_monitor_sees` sends nothing when the payload has
        # not moved - and the alternative is a settings edit in the Monitor UI
        # that the block claiming to say what the monitor sees never shows.
        self._push_monitor_sees()

    # == the monitor link (split mode) =========================================
    # Everything ``--monitor host:port`` adds, and nothing else in this file
    # knows about it: the automation core is handed a
    # :class:`SwitchableMonitor` in the constructor and is never told which kind
    # of machine is behind it (ui-monitor.md §2.9). What lives here is the three
    # events only this object can see - a dial that landed, a link that went
    # away, a redial that came back - and the one sequence all three share.
    #
    # That sequence is §2.9's "re-derive from the screen", in order:
    #
    #   1. ``swap`` the new link in, so every parked ``observe`` and every
    #      subscriber is pointed at it before anything is asked of it;
    #   2. register ``on_disconnect`` BEFORE the first round trip, so a link
    #      that dies during the configure is still caught by the redial;
    #   3. ``configure`` (through ``_retarget_monitor``, which also drops the
    #      verdicts the old run left behind and repaints the DETECTION block) -
    #      the monitor kept polling and kept counting while nobody was attached,
    #      so until this lands we cannot tell a live tick from a ghost (§2.8);
    #   4. re-arm the clipboard WATCHER, which is a far-side thread and died
    #      with the far side's view of us;
    #   5. park the loop back on IDLE, from which phase 2's loop re-runs the
    #      recipe of the state the link dropped in.
    #
    # Nothing is buffered and nothing is replayed - the same honesty rule the
    # remote executor keeps (remote-executor.md §2.3).

    async def _launch_local_monitor(self) -> bool:
        """Start an ``agentclip-monitor`` on this PC, then dial it (§10.1).

        Two steps and no third: there is deliberately no readiness poll here.
        The dial IS the readiness check - the child needs a moment to bind its
        port, and the redial backoff below already handles "not listening yet",
        so a second liveness notion would be a second thing for the link to
        disagree with (``monitor_launch.py``).

        The loop is parked in DISCONNECTED with :data:`MONITOR_LOCAL_STARTING`
        BEFORE the spawn, and that is what makes the ordinary first-dial
        failures quiet: ``_park_disconnected`` toasts once per outage, and this
        sentence is that toast. A user watching a monitor come up should not be
        told twice that it has not come up yet.
        """
        self._launching = True
        self._park_disconnected(MONITOR_LOCAL_STARTING)
        try:
            launched = await asyncio.to_thread(
                self._launcher.start,
                self._project_root,
                global_config_path=self._global_config_path,
            )
        except Exception as exc:  # noqa: BLE001 - a spawn fails in the OS's own ways
            self._launching = False
            self._local_launched = False
            self._park_disconnected(MONITOR_LOCAL_FAILED.format(reason=exc))
            return False
        self._launching = False
        self._local_launched = True
        self._exited_said = False
        self._monitor_target = launched.target
        if await self._attach_monitor():
            return True
        self._begin_redial()
        return False

    async def _dial_monitor(self) -> None:
        """The first dial. A failure is not fatal - it starts the redial loop.

        A monitor is a machine that is *supposed* to outlive us (§2.8), so "the
        monitor is not up yet" is a state to sit in rather than a launch error:
        the window stays open, the loop says DISCONNECTED, and the backoff keeps
        trying until somebody starts one.
        """
        if not await self._attach_monitor():
            self._begin_redial()

    async def _dial_address(self, target: MonitorTarget) -> tuple[str, int]:
        """Where to actually open the socket - and, Via SSH, the tunnel first.

        A direct target IS its address. A Via-SSH one is a monitor bound to the
        target's own loopback, so the address is a local port this process owns
        and every byte through it rides the SSH connection the Executor already
        holds: ``open_tunnel`` opens one ``direct-tcpip`` channel and pumps it
        to a loopback listener (``executor/hosts/ssh.py:Tunnel``). No second
        login, no second host-key question, no external ``ssh -L`` (§9.2).

        Blocking, so it goes to a worker thread: it opens the channel EAGERLY,
        which is what makes "nothing is listening on that port over there" this
        call's failure - shown on the form the user just typed into - rather
        than a handshake that hangs up two layers later.

        A tunnel from the previous attempt is closed first, always. A redial
        opens a fresh one, because the old one is spent: a tunnel serves exactly
        one local connection and the link that used it is the one that died.
        """
        if not target.is_via_ssh():
            return target.host, target.dial_port()
        host = self._host
        if not self._ssh_target_is(target.via):
            raise ConnectionError(MONITOR_CONNECT_FIRST.format(name=target.via))
        self._close_tunnel()
        tunnel = await asyncio.to_thread(
            host.open_tunnel, target.host or MONITOR_LOOPBACK, target.dial_port()
        )
        self._tunnel = tunnel
        return str(tunnel.local_host), int(tunnel.local_port)

    def _ssh_target_is(self, name: str) -> bool:
        """Is the Executor connected to the saved SSH target called ``name``?

        The refusal this answers is deliberate (``MONITOR_CONNECT_FIRST``): a
        Monitor tab that quietly ran the SSH connect sequence would be ending
        the user's session - "one session, one host" makes a connect a session
        boundary - from behind a button that says "attach a monitor". So the
        tab asks for the connection it needs and names it, and the Executor tab
        beside it is one click away.

        Matched against the name the target was SAVED as and against what the
        connection calls itself, because a saved ``[remote.pi]`` whose host is
        ``raspberrypi.local`` is dialled under both.
        """
        host = self._host
        if host is None or getattr(host, "open_tunnel", None) is None:
            return False
        dialled = self._dialled
        if dialled is not None and dialled.name == name:
            return True
        return str(getattr(host, "target", "")) in (name, self._remote_target)

    def _close_tunnel(self) -> None:
        """Drop the SSH tunnel under the link, if there is one. Idempotent.

        Never fatal: a tunnel whose channel already died closes itself, and a
        second close of one is a no-op by its own contract - so a failure here
        would be a failure to tidy up after something that is already gone.
        """
        tunnel, self._tunnel = self._tunnel, None
        if tunnel is None:
            return
        with contextlib.suppress(Exception):
            tunnel.close()

    async def _attach_monitor(self) -> bool:
        """One dial attempt and everything a successful one owes. Never raises.

        Returns whether the link is up now. The two failure shapes are the same
        answer to the user - park in DISCONNECTED and keep trying - but they
        are caught separately because only the first has words worth showing:
        the dial's own exception names what refused (a closed port, a busy
        monitor, a version skew), while a configure that failed did so because
        the link we just made is already gone and its own hook is telling the
        story.
        """
        target, switch = self._monitor_target, self._switch
        if target is None:  # pragma: no cover - guarded by callers
            return False
        peer = target.describe()
        try:
            host, port = await self._dial_address(target)
            link = await self._dial(host, port, target.token, self._config.gui.theme)
        except Exception as exc:  # noqa: BLE001 - a dial fails in the transport's own ways
            self._close_tunnel()
            self._park_disconnected(self._dial_failure(peer, exc))
            return False
        was = self._monitor_server_id
        self._monitor_server_id = link.server_id
        previous = switch.swap(link)
        link.on_disconnect(lambda: self._monitor_dropped(peer))
        try:
            await self._retarget_monitor()
        except Exception as exc:  # noqa: BLE001 - the link died inside the handshake
            self._park_disconnected(self._dial_failure(peer, exc))
            return False
        # The watcher is a thread on the FAR machine and there is none after a
        # reconnect. Re-armed off the ARMED switch rather than off what the
        # watcher was doing before the drop: the flag is the user's standing
        # answer to "may this tool touch the machine", and it is the only one of
        # the two that survived the link.
        switch.watch_clipboard(self._automation.os_armed)
        self._automation.set_loop_state(LoopState.IDLE, f"monitor link up ({peer})")
        self._monitor_failure = ""
        self._exited_said = False
        self._push_link()
        self.notify(MONITOR_UP.format(peer=peer))
        if was is not None and was != link.server_id:
            self.notify(MONITOR_RESTARTED.format(peer=peer), severity="warning", timeout=8)
        self._schedule(previous.close())
        return True

    def _dial_failure(self, peer: str, exc: BaseException) -> str:
        """Why the dial did not land - plus, for a child of ours, that it is gone.

        The transport's own sentence says the socket refused; it cannot say
        WHY, and for a monitor this app started the why is usually "the process
        exited". So :data:`LOCAL_MONITOR_EXITED` is appended when the launcher
        says the child is not running any more - which also names the thing to
        do about it, because relaunching is a button on the Monitor tab and not
        something the backoff can do on the user's behalf (§10.1).
        """
        reason = MONITOR_RETRY.format(peer=peer, reason=exc)
        if not self._local_launched or self._launcher.alive():
            return reason
        return f"{reason} - {LOCAL_MONITOR_EXITED.format(code=self._launcher.exit_code())}"

    def _monitor_dropped(self, peer: str) -> None:
        """``on_disconnect``: the link went away for a reason nobody asked for.

        Called once per link, on whichever task noticed the EOF, and it does
        exactly two things - says so and starts redialling. Deliberately not a
        close of anything: the monitor on the other end is a standing process
        (§2.8) and the socket under this one is already dead.
        """
        if self._monitor_closing:
            return
        self._park_disconnected(MONITOR_LOST.format(peer=peer))
        self._begin_redial()

    def _park_disconnected(self, reason: str) -> None:
        """Loop → DISCONNECTED, and say why - once per outage, not per attempt.

        ``set_loop_state`` already swallows a repeat, so the guard here is only
        about the TOAST: a monitor that is down for ten minutes must not stack
        one notification per backoff round on top of the one that already says
        the true thing.
        """
        self._monitor_failure = reason
        fresh = self._automation.loop_state is not LoopState.DISCONNECTED
        self._automation.set_loop_state(LoopState.DISCONNECTED, reason)
        self._push_link()
        # ...and one exception to "once per outage": the child we started has
        # DIED, which is a new fact about a link that was already down and the
        # only one the user has to act on. Once per child, never per attempt.
        exited = (
            self._local_launched
            and LOCAL_MONITOR_EXITED.format(code=self._launcher.exit_code()) in reason
        )
        if fresh or (exited and not self._exited_said):
            self._exited_said = bool(exited)
            self.notify(reason, severity="warning", timeout=8)

    def _begin_redial(self) -> None:
        """Start the redial loop, unless one is already running or we are going
        away. One at a time: a second disconnect arriving mid-backoff must not
        double the dial rate."""
        if self._redialling or self._monitor_closing:
            return
        self._redialling = True
        self._schedule(self._redial_loop())

    async def _redial_loop(self) -> None:
        """Dial again on a doubling backoff until it lands or the window closes.

        The wait comes FIRST, before the attempt: the dial that just failed was
        a moment ago, and a monitor that is restarting needs the second, not the
        immediate retry. Nothing here gives up - the far side is a standing
        process somebody will start again, and the only thing that ends this
        loop is the window it belongs to going away.
        """
        try:
            attempt = 0
            while not self._monitor_closing:
                await asyncio.sleep(min(MONITOR_BACKOFF_START * 2**attempt, MONITOR_BACKOFF_CAP))
                if self._monitor_closing:
                    return
                if await self._attach_monitor():
                    return
                attempt += 1
        finally:
            self._redialling = False

    def _paint_detection(self, stale_line: str) -> None:
        """Repaint the DETECTION block for the LIVE window - the only writer.

        ``MainScreen._paint_detection``, minus its paint-epoch stamp. That
        filter exists because Textual routes a cross-thread ``post_message``
        through ``call_soon_threadsafe`` and can therefore deliver an outgoing
        run's last paint AFTER the rebuild that replaced it; this shell's bridge
        is one FIFO with one drainer, so ordering is structural and a reset
        queued here cannot be overtaken (``webview/bridge.py``). The generation
        filter still does its own half: a ghost probe is dropped inside
        ``consume_*`` on the poller thread and never becomes a paint at all.

        The send-gate line is deliberately re-derived rather than reset: a
        rebuild does not un-paste the outbound the gate is holding for.

        The busy/idle rows are plain resting lines now. They used to be able to
        say "ticked but not captured", which took the service's finish CHECKLIST
        and the machine's captures - and both of those are the monitor's since
        §10.5, with no field on :class:`Watched` between them. What survives the
        distinction is the STALE row below: an empty ``active_detectors`` is
        exactly "nothing will ever produce a verdict here", and that is the half
        the user has to act on.
        """
        self.paint_detection(TemplateKind.SEND_READY, self._automation.send_gate_line())
        for kind in (TemplateKind.BUSY, TemplateKind.IDLE):
            self.paint_detection(kind, PROBE_RESTING)
        self.paint_detection(TemplateKind.COPY, COPY_RESTING)
        self.paint_stale(stale_line)
        self._push_sidebar()  # the heading names the window these lines are about

    def _live_has(self, kind: TemplateKind) -> bool:
        """Has the LIVE window's service a capture of ``kind``? Called on the
        POLLER thread, so it reads immutable state and nothing else."""
        return kind in self._watched[self._automation.live_slot].captured

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
        # The banner lives and dies with the flag, not with the note: whatever
        # ended the park (an answer, an Esc dismissal, a /new) cleared
        # ``awaiting_answer``, and the stashed question goes with it rather than
        # hanging over the composer of a conversation that has moved on.
        awaiting = bool(view and view.awaiting_answer)
        if not awaiting:
            self._pending_question = None
        self._bridge.send(
            "state",
            session_active=bool(view and view.session_active),
            busy=bool(view and view.busy),
            pending_approval=bool(view and view.pending_approval),
            awaiting_answer=awaiting,
            question=self._pending_question or "",
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
            yolo=snap.yolo if snap else self._controller.yolo,
            composer_mode=mode,
            composer_placeholder=placeholder,
            composer_enabled=enabled,
        )

    def _composer_mode(self, view: SessionView | None) -> tuple[str, str, bool]:
        """The brief's precedence table, first match wins (main-chat.md §3).

        The newline key: the GUI hint says Shift+Enter, which is what every web
        composer means, but Ctrl+J (the TUI's newline key) inserts one too so
        the muscle memory transfers - recorded in gui.md §2.
        """
        if self._awaiting_new_session:
            return (
                "task",
                "Describe the task · Enter starts the session · Shift+Enter newline",
                True,
            )
        if view is None or not view.session_active:
            return "idle", "no session", False
        if view.awaiting_answer:
            return "answer", "Answer the model · Enter sends · Shift+Enter newline", True
        if view.question_dismissed:
            # A dismissed question leaves the flow parked, so every "not busy"
            # test below says no - but this is the one busy state whose way out
            # is a typed message, so the box stays open and ordinary.
            return "message", "Question dismissed · your next message answers it", True
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
        mode_class = "st-plan" if mode == "plan" else "st-dim"
        divisor = self._config.general.chars_per_token
        # The edits slot follows the same rule, so a `/yolo` armed at the start
        # prompt is visible before the session it will govern exists.
        if snap.yolo if snap else self._controller.yolo:
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
                # Tokens in both slots, off the same divisor, so ``out`` stays a
                # true fraction of the budget beside it: this bar is where a user
                # decides whether the next turn will fit, and that is a token
                # judgement, never a character one.
                "text": f"{snap.service_key} {fmt_tokens_compact(snap.budget_chars, divisor)} tok"
                if snap
                else "no session",
                "cls": "",
            },
            {
                "id": "out",
                "text": f"out {fmt_tokens_compact(snap.last_outbound_chars, divisor)}"
                f"/{fmt_tokens_compact(snap.budget_chars, divisor)} tok (1/1)"
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
        # Its own slot beside the edits one, never folded into it: "everything
        # auto-approves" and "everything auto-denies" can be true at once, and
        # that is the pair a user must never misread. Absent when off, the way
        # every hiding segment hides.
        if snap.unattended if snap else self._controller.unattended:
            segments.append({"id": "unattended", "text": "⚠ UNATTENDED", "cls": "st-unattended"})
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
        """The words come from ``describe``; the glyph and the colour are ours.

        AgentClip runs two state machines on purpose (ui-monitor.md §2.5) and
        this segment is the one line that has to hold both - so the sentence is
        looked up rather than composed here, and what is left in this method is
        the shell's own two contributions: the styling, and the handful of
        situations only a SESSION knows about (an approval, a question, a start
        prompt, a machine with no clipboard, a paused watcher). Those sit
        between the loop's own claim and the phase's, in the order they are
        evaluated - first match wins.
        """
        view = self._last_view
        snap = view.snapshot if view is not None else None
        phase = snap.phase if snap else Phase.IDLE
        loop = self._automation.loop_state
        label = describe(phase, loop)
        if loop in ATTENTION_STATES:
            # The two moments the app is stuck on a human at the browser end.
            # Above everything below, which is ``describe``'s own precedence
            # rule: no phase wording may bury "paste it yourself".
            return f"■ {label}", "st-attn"
        if phase is Phase.DONE:
            return f"✓ {label}", "st-done"
        if view is not None and view.pending_approval:
            return "■ APPROVE NEEDED", "st-attn"
        if view is not None and view.awaiting_answer:
            return "■ ANSWER NEEDED", "st-attn"
        if view is not None and view.question_dismissed:
            # Before `busy`, which is technically true: the model is parked on a
            # question the user set aside, and "working..." would be a lie about
            # who the floor belongs to.
            return "■ QUESTION PARKED", "st-attn"
        if self._awaiting_new_session:
            # The session worker is technically busy here too (parked on the
            # inline prompt) but there is no turn in flight - nothing for the
            # user to wait on - so the bar must not say "working".
            return f"○ {describe(Phase.IDLE, loop)}", "st-dim"
        if view is not None and view.busy:
            # ``busy`` IS ``Phase.REVIEW``'s meaning - a turn in flight - stated
            # by the session rather than by the snapshot, so it is asked as that
            # phase. The loop still outranks it: a turn running while the browser
            # is mid-generation reads "generating...", not "working...".
            return f"● {describe(Phase.REVIEW, loop)}", "st-busy"
        if self._provider.name == "manual":
            return "✗ manual paste", "st-err"
        if self._watch_paused:
            return "○ paused", "st-dim"
        if view is not None and view.session_active and phase is Phase.AWAITING_REPLY:
            return f"● {label}", "st-armed"
        return f"○ {describe(Phase.IDLE, loop)}", "st-dim"

    def _push_rail(self) -> None:
        """The STATE rail: one row per LoopState, in loop order.

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
                    "mark": "active" if state is active else ("legal" if state in legal else "dim"),
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
        watched = self._watched[slot]
        service = watched.service or ""
        preset = self._preset_for(slot) if service else None
        budget_divisor = self._config.general.chars_per_token
        cal = self._automation.calibration(slot)
        region = cal.chat_region
        live_window = _WINDOW_NAMES[
            SUBAGENT_WINDOW if self._automation.live_slot is AgentSlot.SUBAGENT else MASTER_WINDOW
        ]
        self._bridge.send(
            "sidebar",
            project=_short_root(self._project_root),
            # The PROJECT block's standing remote marker and its "reconnect now"
            # button - the half of ruling 6 a toast cannot do, and the half of
            # ruling 5 that is visible. Empty on a local session: a block
            # answering a question nobody asked is worse than no block.
            remote=self._remote_target,
            remote_lines=self._remote_lines(),
            can_connect=self._remote is not None,
            # READ-ONLY since §10.5: the service is the monitor's, and there is
            # nothing here to pick from. One line saying which one it settled on
            # and where that came from, so a user reading a budget knows whose
            # budget it is - and the door under it says where to change it.
            service=f"{preset.label} ({service}){SERVICE_FROM_MONITOR}"
            if preset and service
            else SERVICE_UNWATCHED,
            # Both units, and characters first: these two numbers are the
            # preset's CONFIGURED values (what a user edits in the service editor
            # or config.toml, both of which are in characters), and the token
            # estimate beside each is what says whether the budget is big enough.
            service_label=(
                f"· {fmt_budget(preset.max_paste_chars, budget_divisor)} per paste "
                f"· {fmt_budget(preset.total_context_chars, budget_divisor)} context"
            )
            if preset and service
            else "",
            profile_note=f"appearance: {describe_captured(self.captured_for(slot))}{PROFILE_HINT}",
            window=window,
            region=f"{region.describe()} · chatbot window" if region is not None else REGION_UNSET,
            slot_note=self._slot_note(slot),
            detection_title=f"DETECTION · {live_window}",
        )
        # The F2 block reads the same two things this one does - the selected
        # window and what the monitor last said about it - so it is repainted by
        # the same moments, and its own change filter makes the extra call free.
        self._push_monitor_sees()

    def _push_monitor_sees(self) -> None:
        """The MONITOR SEES block (F2), pushed only when its text changed (§11.4).

        Called from two places and they are two different threads: every
        :meth:`_push_sidebar` (the loop - a tab click, a retarget, an attach)
        and every tick (the monitor's thread). Nothing is mutated but
        :attr:`_sees_sent`, and the worst a race between them can do is send one
        identical payload twice, which the page paints idempotently.
        """
        payload = self._monitor_sees()
        if payload == self._sees_sent:
            return
        self._sees_sent = payload
        self._bridge.send("monitor_sees", **payload)

    def _monitor_sees(self) -> dict[str, Any]:
        """What F2's block would say right now: rows, settings, or the note.

        The SELECTED window, like every other per-window block in the sidebar -
        which appearances the monitor holds for the service THAT tab is pointed
        at. With no monitor attached there is nothing to report and one sentence
        saying so, because a column of "not captured" would be the wrong
        accusation entirely.
        """
        if not self._monitor_peer():
            return {"rows": [], "settings": "", "note": SEES_NO_MONITOR}
        watched = self._watched[_WINDOW_SLOTS[self._selected_window]]
        return {
            "rows": sees_rows(watched, self._monitor.latest),
            "settings": sees_settings(watched),
            "note": "",
        }

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
        captured = self.captured_for(slot)
        if can_delegate(cal, captured):
            return SLOT_NOTE_READY
        return SLOT_NOTE_MISSING + ", ".join(missing(cal, captured))

    def _mcp_counts(self) -> tuple[int, int | None]:
        """``connected`` over ``enabled`` - or ``None`` when there is no manager.

        Disabled entries are a config statement rather than a runtime hope, so
        they are out of both numbers' way - and an entry the loader REFUSED is
        the same kind of statement, one step earlier: it never became a server,
        so counting it as one still-to-connect would leave the denominator
        permanently un-reachable. Its row says `invalid config` instead, which
        is where that fact belongs. ``None`` hides the segment and the whole
        sidebar block: an install with no MCP servers gets exactly the bar it
        always had.
        """
        if self._mcp_manager is None:
            return 0, None
        statuses = list(self._mcp_manager.statuses())
        if not statuses:
            return 0, None
        connected = sum(1 for s in statuses if getattr(s, "state", "") == "connected")
        enabled = sum(1 for s in statuses if getattr(s, "state", "") not in ("disabled", "invalid"))
        return connected, enabled

    def _mcp_rows(self) -> Sequence[Any]:
        """The MCP source ``/mcp`` reads BEFORE a session exists.

        A method that re-reads :attr:`_mcp_manager` rather than the bound
        ``manager.statuses`` the block above uses, because a connect SWAPS the
        manager for the target's runtime (``_adopt_remote``): a callable that
        closed over the launch-time one would keep answering for the machine
        this window just left. ``()`` when there is no manager at all - which
        the controller renders as "MCP is not configured", the same answer it
        gives for a source that returns nothing.
        """
        manager = self._mcp_manager
        return manager.statuses() if manager is not None else ()

    def _skill_rows(self) -> SkillReport:
        """The skills source ``/skills`` reads BEFORE a session exists.

        ``_mcp_rows``' twin, re-reading :attr:`_skills` for the same reason: a
        connect swaps it for the target's, and a callable that closed over the
        launch-time one would name folders on the machine this window just left.
        """
        source = self._skills
        return source() if source is not None else NoSkills()

    def _push_mcp(self) -> None:
        """The sidebar's MCP block: one row per configured server, in config
        order. Absent - heading included - when there is no manager."""
        if self._mcp_manager is None:
            return
        statuses = list(self._mcp_manager.statuses())
        self._bridge.send(
            "mcp",
            rows=[
                {
                    "name": str(getattr(s, "name", "")),
                    "state": str(getattr(s, "state", "")),
                    "line": _mcp_line(s),
                }
                for s in statuses
            ],
        )

    # == services and profiles =================================================
    # None of this reads ``[services.*]`` or ``general.service`` any more. Which
    # service a window drives, and every preset field the brain acts on, is the
    # MONITOR's answer (§10.5) - held per slot in ``_watched`` and turned into
    # the automation's vocabulary by ``preset_from_watched``.

    def _service_for(self, slot: AgentSlot) -> str:
        """The service key the monitor says it is driving for ``slot``, or ""."""
        return self._watched[slot].service or ""

    def _preset_for(self, slot: AgentSlot) -> ServicePreset:
        """``slot``'s whole preset, built from the monitor's answer about it.

        The alarm knobs are folded in from this host's config, because the
        uh-oh sound plays on the machine the USER is at and that is this one
        (:func:`preset_from_watched`).
        """
        return preset_from_watched(self._watched[slot], alerts=self._config.preset())

    def engine_preset(self) -> ServicePreset | None:
        """The service the ENGINE should compose against, or None (§11.9).

        :meth:`live_preset`'s sibling, and the difference is the ``None``. The
        recipes always want a preset - an empty one describes a window nothing
        is being driven in, which is exactly what their guards read. The engine
        wants the monitor's answer ONLY when there is one: a window with no
        monitor attached (an idle start, §11.1) would otherwise compose against
        ``EMPTY_WATCHED``, whose paste budget is zero and whose bootstrap could
        never fit. So "the monitor has not named a service" is reported as
        nothing at all, and the engine falls back to this machine's
        ``[services.*]`` table (:class:`~agentclip.protocol.preset.LivePreset`).

        Read through on every call, never cached: that is what makes a budget
        edited in the Monitor UI mid-session reach the next composed turn.
        """
        watched = self._watched[self._automation.live_slot]
        if not watched.service or watched.max_paste_chars <= 0:
            return None
        return preset_from_watched(watched, alerts=self._config.preset())

    # == small helpers =========================================================

    def _session_running(self) -> bool:
        view = self._last_view
        return bool(view and view.session_active)

    def _remember_own_window(self) -> None:
        """Record the foreground window at a moment the user is provably
        interacting with AgentClip. The HANDLE is OS state both shells snap
        focus back to, so it is kept below."""
        self._automation.set_own_window(foreground_window())


def _no_config(config: Config) -> None:
    """The config hand-back a view nobody wired an engine factory into gets."""


def _no_schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """The scheduler a view nobody wired a loop into gets: close the coroutine
    rather than leak it, and do nothing. Tests inject a recorder; the runner
    injects the real loop."""
    coro.close()


def _no_exit() -> None:
    """The exit a view with no window behind it gets."""
