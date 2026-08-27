"""The sidebar, the status bar, the STATE rail and the harness log.

Parity increment 2's surface, and the reason it gets a file of its own: what is
tested here is not "did an event go out" (``tests/shell/chat/test_view.py`` pins that
for every port method) but the DECISIONS these three widgets encode, all of
which are made on the Python side so the two shells cannot grow two answers to
them - the rail's legal-next brightness, the status bar's eleven segments and their
priority rules, and the keys whose state those surfaces show reaching the same
controller methods the TUI's bindings do.

The parity contract is ``docs/design/ui-briefs/sidebar-status-log.md`` (§2 the
anatomy, §3 the states, §6 the invariants) and, for the keys,
``docs/design/ui-briefs/modals-keys-esc.md`` §5.1. Nothing here opens a window.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from agentclip.driver.automation.harness_log import HARNESS_LOG_MAX, KIND_GATE
from agentclip.driver.automation.loop_state import LOOP_TRANSITIONS, LoopState
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.slot import AgentSlot
from agentclip.engine.engine import Phase
from agentclip.shell.chat.view import (
    COPY_RESTING,
    DETECTOR_LABEL,
    PROBE_RESTING,
    SERVICE_UNWATCHED,
    STALE_UNSET,
    SUBAGENT_WINDOW,
    GuiView,
)
from agentclip.shell.webview.bridge import JsApi
from tests.shell.chat.conftest import HARNESS_SERVICE, Harness
from tests.shell.chat.test_view import ControllerSpy, session_view, snapshot

ASSETS = Path(__file__).resolve().parents[3] / "src" / "agentclip" / "shell" / "chat" / "assets"


class KeySpy(ControllerSpy):
    """``ControllerSpy`` plus the five session keys' controller calls.

    Deliberately the same object the gate tests use: every one of these is a
    plain forward, and what a test wants to know is *which door* the key opened,
    not what happened behind it (``tests/shell/app`` owns that).
    """

    def __init__(self) -> None:
        super().__init__()
        self.cycles = 0
        self.recopies = 0
        self.ingests = 0
        self.reinstructs = 0
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def cycle_permission_mode(self) -> None:
        self.cycles += 1

    def recopy(self) -> None:
        self.recopies += 1

    def force_ingest(self) -> None:
        self.ingests += 1

    def reinstruct(self) -> None:
        self.reinstructs += 1


class FakeProvider:
    """A clipboard provider that is not the manual one, so the watch segment and
    the `w` key can be exercised past their manual-mode refusal."""

    name = "windows"

    def read_text(self) -> str | None:  # pragma: no cover - never called here
        return None

    def write_text(self, text: str) -> None:  # pragma: no cover - never called here
        return None


def use_real_provider(harness: Harness) -> FakeProvider:
    """Point BOTH halves at a non-manual provider.

    The view reads its own for the watch segment's "manual paste" branch; the
    MONITOR holds the one the watcher would actually poll (ui-monitor.md §2.11),
    and ``start_input`` refuses a manual backend outright.
    """
    provider = FakeProvider()
    harness.view._provider = provider  # type: ignore[assignment]
    harness.monitor.clipboard = provider  # type: ignore[assignment]
    return provider


class FakeStatus:
    def __init__(self, name: str, state: str, tool_count: int = 0, detail: str = "") -> None:
        self.name = name
        self.state = state
        self.tool_count = tool_count
        self.detail = detail


class FakeMcp:
    """The ``McpStatusSource`` the view is handed, structurally."""

    def __init__(self, *statuses: FakeStatus) -> None:
        self._statuses = statuses
        self.hook: Any = None

    def statuses(self) -> tuple[FakeStatus, ...]:
        return self._statuses

    def set_status_hook(self, cb: Any) -> None:
        self.hook = cb


def segments(harness: Harness) -> dict[str, dict[str, str]]:
    """The last status bar, keyed by segment id. Absent means hidden."""
    return {seg["id"]: seg for seg in harness.flush().last("status")["segments"]}


def segment_order(harness: Harness) -> list[str]:
    return [seg["id"] for seg in harness.flush().last("status")["segments"]]


def api_of(harness: Harness) -> JsApi:
    """The page's side of the bridge, wired straight at the view.

    The runner marshals each call onto its loop; a test has no loop, so it hands
    the view in directly - the two are the same object graph minus the hop.
    """
    return JsApi(harness.view)  # type: ignore[arg-type]


# == the STATE rail ============================================================


@pytest.mark.parametrize("active", list(LoopState))
def test_the_rail_draws_every_state_and_marks_the_legal_next_ones(
    harness: Harness, active: LoopState
) -> None:
    """One row per LoopState, the active one marked, ``LOOP_TRANSITIONS``'s
    legal next moves at normal brightness and everything else dim. Display only:
    the table is the rail's brightness table, not a state-machine guard."""
    # Via a state it is not, because a no-op transition neither repaints nor
    # logs - the loop starts on IDLE and ``set_loop_state`` is the sole writer.
    harness.view.automation.set_loop_state(LoopState.WAIT_SEND, "test")
    harness.view.automation.set_loop_state(active, "test")
    event = harness.flush().last("rail")

    assert event["loop"] == active.name
    assert [row["state"] for row in event["rows"]] == [state.name for state in LoopState]
    legal = LOOP_TRANSITIONS[active]
    for row in event["rows"]:
        state = LoopState[row["state"]]
        if state is active:
            assert row["mark"] == "active"
        elif state in legal:
            assert row["mark"] == "legal"
        else:
            assert row["mark"] == "dim"


def test_the_rail_never_draws_idle_as_reachable_from_everywhere(harness: Harness) -> None:
    """``/new`` sends every state home to IDLE and that is a RESET, not a
    transition - drawing it as a legal move would make the brightness
    meaningless (loop_state.py's header)."""
    harness.view.automation.set_loop_state(LoopState.WAIT_GENERATE, "test")
    rows = {row["state"]: row["mark"] for row in harness.flush().last("rail")["rows"]}
    assert rows["IDLE"] == "dim"


def test_a_loop_paint_is_drawn_from_the_controller_not_the_payload(harness: Harness) -> None:
    """``paint_loop_state`` may be raised on the poller thread, so the flag is
    re-read rather than trusted: a state that crossed as data would be whatever
    was true when it was sent."""
    harness.view.paint_loop_state(LoopState.AUTO_COPY)  # never actually set
    assert harness.flush().last("rail")["loop"] == "IDLE"


# == the status bar ============================================================


def test_the_bar_carries_the_eleven_segments_in_order(harness: Harness) -> None:
    view = harness.view
    view._provider = FakeProvider()  # type: ignore[assignment]
    view._mcp_manager = FakeMcp(FakeStatus("fs", "connected", 4))
    view.set_os_armed(False)
    view.render_state(
        session_view(
            snapshot=snapshot(
                instructions_armed=True, has_extra_instructions=True, unattended=True
            )
        )
    )
    assert segment_order(harness) == [
        "mode",
        "watch",
        "armed",
        "service",
        "out",
        "turn",
        "instr",
        "edits",
        "unattended",
        "mcp",
        "root",
    ]


def test_the_four_hiding_segments_are_absent_rather_than_blank(harness: Harness) -> None:
    """``armed``/``instr``/``unattended``/``mcp`` hide by NOT BEING DRAWN,
    reserving no padding - an install with no MCP servers gets exactly the bar it
    always had."""
    harness.view.render_state(session_view())
    assert segment_order(harness) == ["mode", "watch", "service", "out", "turn", "edits", "root"]


def test_with_no_session_the_bar_says_so_in_three_places(harness: Harness) -> None:
    harness.view.render_state(session_view(session_active=False, snapshot=None))
    bar = segments(harness)
    assert bar["service"]["text"] == "no session"
    assert bar["out"]["text"] == "out -"
    assert bar["turn"]["text"] == "turn -"
    # ...and the mode falls back to the controller's mirror, which is what
    # shift+tab would be changing before any engine exists.
    assert bar["mode"]["text"] == "MODE:build"


@pytest.mark.parametrize(
    ("mode", "cls"),
    [("build", "st-dim"), ("plan", "st-plan")],
)
def test_each_permission_mode_has_its_own_style_and_never_red(
    harness: Harness, mode: str, cls: str
) -> None:
    """Red is reserved for the two "something is off" badges (⚡ YOLO,
    ⛔ DISARMED); the mode segment never borrows it, and never hides."""
    harness.view.render_state(session_view(snapshot=snapshot(mode=mode)))
    seg = segments(harness)["mode"]
    assert seg["text"] == f"MODE:{mode}"
    assert seg["cls"] == cls


def test_yolo_wins_the_edits_slot_over_auto_accept(harness: Harness) -> None:
    """The two booleans are independent and both can be true; there is no
    combined rendering, and YOLO always wins the display."""
    harness.view.render_state(
        session_view(snapshot=snapshot(yolo=True, auto_accept_edits=True))
    )
    seg = segments(harness)["edits"]
    assert seg["text"] == "⚡ YOLO"
    assert seg["cls"] == "st-yolo"


@pytest.mark.parametrize(
    ("yolo", "auto", "text"),
    [(False, True, "EDITS:auto"), (False, False, "EDITS:ask")],
)
def test_the_edits_segment_without_yolo(
    harness: Harness, yolo: bool, auto: bool, text: str
) -> None:
    harness.view.render_state(session_view(snapshot=snapshot(yolo=yolo, auto_accept_edits=auto)))
    assert segments(harness)["edits"]["text"] == text


def test_the_edits_slot_shows_a_yolo_armed_before_any_session(harness: Harness) -> None:
    """The mode segment's rule, applied to the other switch a user can throw at
    the start prompt: with no snapshot to read, the controller's mirror is what
    the next session will start in, so the badge has to say so."""
    harness.view._controller.submit_message("/yolo on")
    assert segments(harness)["edits"]["text"] == "⚡ YOLO"


def test_a_disarmed_yolo_session_shows_both_badges(harness: Harness) -> None:
    """The pair a user must be able to see - which is exactly why ``armed`` is
    its own slot rather than folded into ``edits``."""
    harness.view.set_os_armed(False)
    harness.view.render_state(session_view(snapshot=snapshot(yolo=True)))
    bar = segments(harness)
    assert bar["armed"]["text"] == "⛔ DISARMED"
    assert bar["edits"]["text"] == "⚡ YOLO"


def test_the_unattended_badge_shows_beside_the_yolo_one(harness: Harness) -> None:
    """The other pair that has to be readable at once: everything auto-approves
    AND everything auto-denies. Its own slot for exactly that reason."""
    harness.view.render_state(session_view(snapshot=snapshot(yolo=True, unattended=True)))
    bar = segments(harness)
    assert bar["edits"]["text"] == "⚡ YOLO"
    assert bar["unattended"]["text"] == "⚠ UNATTENDED"
    assert bar["unattended"]["cls"] == "st-unattended"


def test_the_unattended_badge_shows_before_any_session(harness: Harness) -> None:
    """The mode segment's rule on the toggle a user can throw at the start
    prompt: with no snapshot, the controller's mirror is what the next session
    starts in."""
    spy = ControllerSpy()
    spy.unattended = True
    harness.view._controller = spy  # type: ignore[assignment]
    harness.view.render_state(session_view(session_active=False, snapshot=None))
    assert segments(harness)["unattended"]["text"] == "⚠ UNATTENDED"


def test_arming_again_takes_the_badge_away(harness: Harness) -> None:
    harness.view.set_os_armed(False)
    harness.view.set_os_armed(True)
    assert "armed" not in segments(harness)


@pytest.mark.parametrize(
    ("kwargs", "awaiting_new", "paused", "manual", "text", "cls"),
    [
        ({"snapshot": snapshot(Phase.DONE)}, False, False, False, "✓ done - reply to continue",
         "st-done"),
        ({"pending_approval": True}, False, False, False, "■ APPROVE NEEDED", "st-attn"),
        ({"awaiting_answer": True}, False, False, False, "■ ANSWER NEEDED", "st-attn"),
        # awaiting_new_session masks busy: the worker is parked on the inline
        # prompt, so there is no turn for the user to wait on.
        ({"busy": True}, True, False, False, "○ idle", "st-dim"),
        ({"busy": True}, False, False, False, "● working...", "st-busy"),
        ({}, False, False, True, "✗ manual paste", "st-err"),
        ({}, False, True, False, "○ paused", "st-dim"),
        ({}, False, False, False, "● ready - paste the reply", "st-armed"),
        ({"session_active": False, "snapshot": None}, False, False, False, "○ idle", "st-dim"),
    ],
)
def test_the_watch_segments_precedence_order(
    harness: Harness,
    kwargs: dict[str, object],
    awaiting_new: bool,
    paused: bool,
    manual: bool,
    text: str,
    cls: str,
) -> None:
    """The nine renderings, in the order they are evaluated - first match wins.
    A frontend that reordered these would tell a user to paste a reply while a
    turn is running."""
    view = harness.view
    if not manual:
        view._provider = FakeProvider()  # type: ignore[assignment]
    view._awaiting_new_session = awaiting_new
    view._watch_paused = paused
    view.render_state(session_view(**kwargs))
    seg = segments(harness)["watch"]
    assert (seg["text"], seg["cls"]) == (text, cls)


@pytest.mark.parametrize(
    ("state", "text", "cls"),
    [
        (LoopState.WAIT_GENERATE, "● generating...", "st-armed"),
        (LoopState.WAIT_SEND, "● press Enter to send", "st-armed"),
        (LoopState.AUTO_INSERT, "● pasting into the chat box", "st-armed"),
        (LoopState.AUTO_COPY, "● copying the reply", "st-armed"),
        # The two "the next move is yours" states outrank everything the
        # session could say, which is ``describe``'s own precedence rule.
        (LoopState.MANUAL_INSERT, "■ paste it yourself - Ctrl+V into the chat box", "st-attn"),
        (LoopState.MANUAL_COPY, "■ copy the reply yourself", "st-attn"),
        # Both deferring states hand the label back to the phase.
        (LoopState.IDLE, "● ready - paste the reply", "st-armed"),
        (LoopState.INTERPRETING, "● ready - paste the reply", "st-armed"),
    ],
)
def test_the_watch_segment_speaks_for_the_browser_too(
    harness: Harness, state: LoopState, text: str, cls: str
) -> None:
    """One label for two state machines (ui-monitor.md §2.5, §6.3).

    The words are ``describe(phase, loop_state)``'s, not this shell's: where the
    round trip through the browser has something to say it outranks the phase,
    and where it has not the phase speaks. What stays the shell's is the glyph
    and the colour.
    """
    view = harness.view
    view._provider = FakeProvider()  # type: ignore[assignment]
    view.render_state(session_view())  # AWAITING_REPLY, session live
    view.automation.set_loop_state(state, "the test put it here")
    seg = segments(harness)["watch"]
    assert (seg["text"], seg["cls"]) == (text, cls)


def test_a_loop_state_repaints_the_status_bar_as_well_as_the_rail(harness: Harness) -> None:
    """``paint_loop_state`` writes two surfaces: the rail marks the row, and the
    bar re-reads the sentence - a rail that moved without the label following it
    would leave the bar telling the phase's story about a browser that had gone
    somewhere else."""
    harness.view._provider = FakeProvider()  # type: ignore[assignment]
    harness.view.render_state(session_view())
    harness.flush().clear()
    harness.view.automation.set_loop_state(LoopState.WAIT_GENERATE, "the model started")
    recorder = harness.flush()
    assert recorder.last("rail")["loop"] == "WAIT_GENERATE"
    watch = {seg["id"]: seg for seg in recorder.last("status")["segments"]}["watch"]
    assert watch["text"] == "● generating..."


def test_a_sub_agent_run_rebadges_the_whole_watch_segment(harness: Harness) -> None:
    """Everything the segment reports during a delegation - the phase, the
    approval, the question - is the SUB-AGENT's, not the conversation the user
    is watching, so the glyph is replaced rather than prefixed."""
    harness.view._provider = FakeProvider()  # type: ignore[assignment]
    harness.view.render_state(
        session_view(pending_approval=True, session_role="subagent", session_title="lint it")
    )
    seg = segments(harness)["watch"]
    assert seg["text"] == "◆ SUB-AGENT · APPROVE NEEDED"
    assert seg["cls"] == "st-sub"


def test_the_mcp_segment_counts_connected_over_enabled(harness: Harness) -> None:
    """Disabled entries are a config statement rather than a runtime hope, so
    they are out of both numbers' way - and so is an entry the config loader
    refused, which never became a server anyone is waiting on."""
    harness.view._mcp_manager = FakeMcp(
        FakeStatus("a", "connected", 2),
        FakeStatus("b", "failed"),
        FakeStatus("c", "disabled"),
        FakeStatus("d", "invalid", detail="mcp.d: url must be a non-empty string"),
    )
    harness.view.render_state(session_view())
    assert segments(harness)["mcp"]["text"] == "mcp 1/2"


def test_the_instr_segment_is_lit_only_while_the_reinject_is_armed(harness: Harness) -> None:
    harness.view.render_state(session_view(snapshot=snapshot(instructions_armed=True)))
    assert segments(harness)["instr"]["text"] == "✎ INSTR"
    harness.view.render_state(session_view(snapshot=snapshot(instructions_armed=False)))
    assert "instr" not in segments(harness)


# == the sidebar's blocks ======================================================


def test_the_sidebar_carries_every_block_the_column_draws(harness: Harness) -> None:
    """Through ``start`` rather than the push, because the mount order is part
    of the contract: the column has to say something truthful from the first
    frame, before any calibration exists."""
    harness.view._controller = KeySpy()  # type: ignore[assignment]
    harness.view.start()
    event = harness.flush().last("sidebar")
    assert event["project"].endswith("project")
    # The SERVICE line is READ-ONLY since §10.5: it names the key the MONITOR
    # answered with, and says where that came from, instead of offering a list
    # this window could pick out of.
    assert event["service"] == f"Claude.ai ({HARNESS_SERVICE}) · from the monitor"
    assert "services" not in event
    # Both units on the budget caption: characters because that is what the
    # preset is configured in, the token estimate because that is what says
    # whether the budget is big enough (``shell/app/sizes.py``).
    assert "chars ≈ ~" in event["service_label"]
    assert "tokens per paste" in event["service_label"]
    assert "tokens context" in event["service_label"]
    assert event["profile_note"].startswith("appearance:")
    assert event["region"] == "not set - alt-tab to the chat yourself"
    assert event["slot_note"] == "the main agent's chat window"
    # The heading names the LIVE window, which parts company with the one the
    # user is reading for the whole of a delegation.
    assert event["detection_title"] == "DETECTION · MASTER"


def test_the_detection_heading_follows_the_live_window(harness: Harness) -> None:
    harness.view.automation.select_live_slot(AgentSlot.SUBAGENT)
    harness.view._push_sidebar()
    assert harness.flush().last("sidebar")["detection_title"] == "DETECTION · SUB-AGENT"


def test_the_service_line_says_so_when_the_monitor_has_not_answered(
    harness: Harness,
) -> None:
    """A window the monitor has said nothing about - no link yet, or a monitor
    with nothing configured for it - gets an honest line rather than a blank.

    The sub-agent tab is that window in the default harness: only the LIVE slot
    has been watched, and looking at a tab is not driving it (§10.5's
    ``watch(slot)`` is the retarget, not the selection).
    """
    harness.view.select_window(SUBAGENT_WINDOW)
    event = harness.flush().last("sidebar")
    assert event["service"] == SERVICE_UNWATCHED
    assert event["service_label"] == ""


async def test_a_rebuilt_detector_paints_the_resting_lines_it_owns(harness: Harness) -> None:
    """The DETECTION block's only writer is the detector machinery, and every
    exit - including the two that start nothing - leaves the lines saying what
    just became true. With no region drawn that is the "no chat region" one.

    Awaited rather than pressed through ``_start_detector_worker``: retargeting
    the monitor is a coroutine (``UIMonitor.configure``), and the synchronous
    door only puts it on the loop.
    """
    await harness.view._retarget_monitor()
    painted = {
        event["kind"]: event["text"] for event in harness.flush().of_type("detection")
    }
    assert painted["STALE"] == STALE_UNSET
    assert painted["BUSY"] == PROBE_RESTING
    assert painted["IDLE"] == PROBE_RESTING
    assert painted["COPY"] == COPY_RESTING
    assert "SEND_READY" in painted  # re-derived from the gate, never reset


def test_a_tick_from_the_monitor_reaches_the_detection_block(harness: Harness) -> None:
    """The subscription the controller takes out on the machine, end to end.

    Since ui-monitor.md phase 6.1 nothing up here polls: the monitor pushes one
    :class:`~agentclip.driver.monitor.protocol.Tick` per observation and the
    controller unpacks it into the ``consume_*`` calls that write these lines.
    So the check that this shell is still plugged into the screen is a tick fed
    by hand - no poller, no capture, no template search.
    """
    # What a retarget onto a calibrated window would have set: a probe from a
    # detector this run does not report is dropped by name, so the run has to
    # say it reports one.
    harness.view.automation.active_detectors = ("busy",)
    harness.flush().clear()
    harness.monitor.feed(
        harness.monitor.make_tick(busy=BusyProbe(state=BusyState.MATCH, diff=0.4))
    )
    painted = {event["kind"]: event["text"] for event in harness.flush().of_type("detection")}
    assert "BUSY" in painted
    assert painted["BUSY"] != PROBE_RESTING


def test_a_tick_from_a_run_the_monitor_has_left_never_lands(harness: Harness) -> None:
    """The ghost filter, from this end: a verdict about the window the
    automation was driving before a retarget must not repaint a block that now
    names the other one (ui-monitor.md §4.2)."""
    harness.view.automation.active_detectors = ("busy",)
    stale_run = harness.monitor.generation
    harness.monitor.retarget()
    harness.flush().clear()
    harness.monitor.feed(
        harness.monitor.make_tick(
            generation=stale_run, busy=BusyProbe(state=BusyState.MATCH, diff=0.4)
        )
    )
    assert harness.flush().of_type("detection") == []


def test_only_the_four_deciding_kinds_get_a_detection_line(harness: Harness) -> None:
    """The two chat boxes and the new-chat button are searched every tick like
    everything else and decide nothing, so they have no line at all."""
    for kind in TemplateKind:
        harness.view.paint_detection(kind, "whatever")
    kinds = {event["kind"] for event in harness.flush().of_type("detection")}
    assert kinds == {kind.name for kind in DETECTOR_LABEL}


def test_a_detection_line_carries_the_label_it_is_named_with(harness: Harness) -> None:
    harness.view.paint_detection(TemplateKind.BUSY, "MATCH")
    event = harness.flush().last("detection")
    assert (event["label"], event["text"]) == ("busy", "MATCH")


# == the MCP block =============================================================


def test_the_mcp_block_is_one_row_per_server_in_config_order(harness: Harness) -> None:
    harness.view._mcp_manager = FakeMcp(
        FakeStatus("fs", "connected", 1),
        FakeStatus("gh", "needs_auth", detail="run /mcp"),
        FakeStatus("off", "disabled"),
    )
    harness.view._push_mcp()
    rows = harness.flush().last("mcp")["rows"]
    assert [row["name"] for row in rows] == ["fs", "gh", "off"]
    assert rows[0]["line"] == "fs · connected · 1 tool"
    assert rows[1]["line"] == "gh · needs auth · run /mcp"
    assert rows[2]["line"] == "off · disabled"


def test_a_refused_entry_is_an_invalid_row_that_carries_its_reason(
    harness: Harness,
) -> None:
    """A config the loader refused is drawn like the failures, not like
    `disabled`: the user did not ask for it, the state alone does not say why,
    and the reason is the only actionable thing on the row. The page puts the
    raw state on as a CSS class, which is where app.css's warn colour hangs.

    It reaches the block through the ordinary paint and nothing else - it was
    decided at load time, so no hook fires for it and no toast is raised.
    """
    reason = "config: /home/u/permissions.json: mcp.polarion: url must be a non-empty string"
    harness.view._mcp_manager = FakeMcp(
        FakeStatus("polarion", "invalid", detail=reason),
        FakeStatus("fs", "connected", 1),
    )
    harness.view._push_mcp()
    recorder = harness.flush()
    rows = recorder.last("mcp")["rows"]
    assert rows[0]["state"] == "invalid"
    assert rows[0]["line"] == f"polarion · invalid config · {reason}"
    assert recorder.of_type("toast") == []


def test_a_status_transition_repaints_the_block_and_still_toasts(harness: Harness) -> None:
    """The repaint reads ``statuses()`` rather than patching one row in - a
    connect can change a NEIGHBOUR's line too (shadowed tool ids) - and the
    toast the manager hook always raised is kept."""
    harness.view._mcp_manager = FakeMcp(FakeStatus("fs", "connected", 3))
    harness.view._mcp_status_hook(FakeStatus("fs", "connected", 3))
    recorder = harness.flush()
    assert recorder.last("mcp")["rows"][0]["line"] == "fs · connected · 3 tools"
    assert any("connected" in event["message"] for event in recorder.of_type("toast"))


def test_no_manager_means_no_block_and_no_segment(harness: Harness) -> None:
    harness.view._push_mcp()
    harness.view.render_state(session_view())
    recorder = harness.flush()
    assert recorder.of_type("mcp") == []
    assert "mcp" not in {seg["id"] for seg in recorder.last("status")["segments"]}


# == the harness log ===========================================================


def test_a_harness_entry_crosses_with_the_line_the_pane_prints(harness: Harness) -> None:
    """The fixed-width kind column is ``HarnessEntry.line``'s decision, taken
    once below both shells - the page renders a row, it does not lay one out."""
    harness.view.automation.log_harness(KIND_GATE, "the gate let go")
    event = harness.flush().last("harness")
    assert event["kind"] == "gate"
    assert event["text"] == "the gate let go"
    # The kind is padded to the widest of the seven ("clipboard"), so the log
    # reads as a column rather than a paragraph.
    assert event["line"] == f"{event['time']}  gate       the gate let go"


def test_the_log_survives_a_session_reset(harness: Harness) -> None:
    """A wedged user's first move is often ``/new``, and clearing the log there
    would destroy the evidence they are about to go looking for. The reset
    writes its own entry instead."""
    harness.view.automation.log_harness(KIND_GATE, "before the reset")
    harness.flush().clear()
    import asyncio

    asyncio.run(harness.view.clear_transcript())
    lines = [event["text"] for event in harness.flush().of_type("harness")]
    assert any("session reset" in line for line in lines)
    assert harness.view.automation.harness_log[0].text == "before the reset"


def test_toggling_the_log_flips_the_pane_rather_than_toasting(harness: Harness) -> None:
    """``/log`` and F8 are the same call - two ways to ask for one thing."""
    harness.view.toggle_harness_log()
    assert harness.flush().last("toggle")["what"] == "log"


def test_the_pages_log_buffer_is_bounded_to_the_deques_own_number() -> None:
    """Two bounds, deliberately the same: with one entry per line the page's
    tail and the deque prune in lockstep during a long run with the pane open.
    Pinned by reading the asset, because the page's copy is the one that would
    silently drift."""
    source = (ASSETS / "app.js").read_text(encoding="utf-8")
    found = re.search(r"var LOG_MAX = (\d+);", source)
    assert found, "app.js no longer declares LOG_MAX"
    assert int(found.group(1)) == HARNESS_LOG_MAX


# == the keys ==================================================================


def test_the_page_repaints_everything_it_could_have_missed(harness: Harness) -> None:
    """A page that reloaded has no state of its own to rebuild from, so
    ``ready`` re-pushes every surface composed on this side."""
    harness.view._mcp_manager = FakeMcp(FakeStatus("fs", "connected"))
    api_of(harness).ready()
    assert {"status", "state", "rail", "tabs", "sidebar", "mcp", "armed"} <= set(
        harness.flush().types
    )


def test_f5_toggles_the_armed_switch_in_both_directions(harness: Harness) -> None:
    """No session gate, in any state: ``None`` is the bare ``/armed`` and F5."""
    api = api_of(harness)
    api.armed(None)
    assert harness.view.automation.os_armed is False
    api.armed(None)
    assert harness.view.automation.os_armed is True
    api.armed(False)
    assert harness.view.automation.os_armed is False


def test_disarming_reports_the_watcher_as_paused(harness: Harness) -> None:
    """Truthful: nothing is polling the clipboard, and the bar has to say so
    even though the user never pressed `w`."""
    harness.view._provider = FakeProvider()  # type: ignore[assignment]
    api_of(harness).armed(False)
    harness.view.render_state(session_view())
    assert segments(harness)["watch"]["text"] == "○ paused"


def test_shift_tab_reaches_the_controllers_cycle(harness: Harness) -> None:
    spy = KeySpy()
    harness.view._controller = spy  # type: ignore[assignment]
    api_of(harness).mode()
    assert spy.cycles == 1


def test_c_forwards_to_the_controller_which_owns_the_double_tap(harness: Harness) -> None:
    """Both presses are the same call: the 1.5s window, the escalation to a
    re-delivery and the arm a fresh outbound drops all live in
    ``SessionController.recopy``, so the page presses the key twice and nothing
    here has to remember when."""
    spy = KeySpy()
    harness.view._controller = spy  # type: ignore[assignment]
    api = api_of(harness)
    api.recopy()
    api.recopy()
    assert spy.recopies == 2


def test_i_moves_the_rail_itself_and_then_asks_the_controller(harness: Harness) -> None:
    """The one place a key press moves the STATE rail without going through the
    detector machinery."""
    spy = KeySpy()
    harness.view._controller = spy  # type: ignore[assignment]
    api_of(harness).ingest()
    assert harness.view.automation.loop_state is LoopState.INTERPRETING
    assert spy.ingests == 1
    assert "you pressed i" in harness.view.automation.harness_log[-1].text


def test_r_reaches_the_controllers_reinstruct(harness: Harness) -> None:
    spy = KeySpy()
    harness.view._controller = spy  # type: ignore[assignment]
    api_of(harness).reinstruct()
    assert spy.reinstructs == 1


def test_the_retry_button_schedules_the_same_insert_the_auto_flow_runs(
    harness: Harness,
) -> None:
    api_of(harness).retry_insert()
    assert harness.scheduled and "retry_insert" in harness.scheduled[0]


@pytest.mark.parametrize(
    ("manual", "session", "armed", "expected"),
    [
        (True, True, True, "manual clipboard mode"),
        (False, False, True, "no session"),
        (False, True, False, "disarmed"),
    ],
)
def test_w_refuses_out_loud_where_the_tui_hides_the_key(
    harness: Harness, manual: bool, session: bool, armed: bool, expected: str
) -> None:
    """``check_action`` hides the binding outright in all three; a page has no
    footer to hide it from, so the refusal is a toast instead of silence."""
    view = harness.view
    if not manual:
        view._provider = FakeProvider()  # type: ignore[assignment]
    if session:
        view.render_state(session_view())
    if not armed:
        view.set_os_armed(False)
    harness.flush().clear()

    api_of(harness).watch()
    messages = [event["message"] for event in harness.flush().of_type("toast")]
    assert any(expected in message for message in messages), messages
    assert view.automation.watching is False


def test_w_pauses_and_resumes_a_running_watcher(harness: Harness) -> None:
    view = harness.view
    use_real_provider(harness)
    view.render_state(session_view())
    view.automation.start_input()
    assert view.automation.watching is True

    try:
        api = api_of(harness)
        api.watch()
        assert view.automation.watching is False
        # The mirror follows the controller rather than leading it.
        assert view._watch_paused is True
        api.watch()
        assert view.automation.watching is True
        assert view._watch_paused is False
    finally:
        view.automation.stop_input()


def test_there_is_no_way_at_all_to_pick_a_service_from_this_window(
    harness: Harness,
) -> None:
    """§10.5, stated as an absence.

    The service is the monitor's, so there is no ``set_service`` on the view, no
    ``service`` verb on the bridge and no ``<select>`` on the page - three
    surfaces, because a leftover on any one of them is a control that either
    throws or, worse, quietly disagrees with the machine driving the browser.
    """
    from importlib.resources import files

    from agentclip.shell.webview.bridge import JsApi, JsCalls

    assert not hasattr(harness.view, "set_service")
    assert not hasattr(JsApi, "service")
    assert not hasattr(JsCalls, "set_service")
    html = (files("agentclip.shell.chat.assets") / "index.html").read_text(encoding="utf-8")
    assert "<select id=\"service-select\"" not in html


def test_the_service_the_monitor_answers_with_is_the_one_the_brain_acts_on(
    harness: Harness,
) -> None:
    """The whole of §10.5 in one read: the key, the budgets and the fences the
    session builder and the recipes use all come out of ``Watched``, not out of
    this host's ``[services.*]`` table (whose ``general.service`` is a different
    preset entirely)."""
    view = harness.view
    assert view._config.general.service != HARNESS_SERVICE
    preset = view.live_preset()
    monitored = view._config.services[HARNESS_SERVICE]
    assert preset.key == HARNESS_SERVICE
    assert preset.max_paste_chars == monitored.max_paste_chars
    assert preset.total_context_chars == monitored.total_context_chars


def test_every_key_action_is_on_the_view_the_runner_marshals_to() -> None:
    """The runner is a one-line marshal per method; a name that existed on the
    bridge's protocol and not on the view would be a click that silently did
    nothing (``JsApi`` swallows what its call raises)."""
    for name in (
        "set_os_armed",
        "cycle_permission_mode",
        "toggle_watch",
        "recopy",
        "force_ingest",
        "reinstruct",
        "retry_insert",
    ):
        assert callable(getattr(GuiView, name, None)), name


# == what may be selected =====================================================
# The window is opened with `text_select=True` (tests/shell/chat/test_shell.py
# pins that end), which only hands the question to the page. These two pin the
# page's answer: what you READ can be dragged over and copied, what you PRESS
# cannot.


def test_the_stylesheet_turns_selection_off_for_click_targets_only() -> None:
    """Every ``user-select: none`` in the file has to be on something you press.

    The failure this catches is the easy one: a blanket rule - on ``body``, on
    ``*``, on ``.shell`` - that takes copy-out-of-the-transcript away again for
    the whole window, which is exactly what pywebview's default did before
    ``text_select`` was passed.
    """
    css = re.sub(r"/\*.*?\*/", "", (ASSETS / "app.css").read_text(encoding="utf-8"), flags=re.S)
    blocked = set()
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"user-select:\s*none", block.group(2)):
            blocked.update(part.strip() for part in block.group(1).split(","))
    assert blocked == {
        ".titlebar",  # the drag region
        ".keyhints",  # the cheatsheet strip, which also refuses the mousedown
        ".wintabs",  # the tab strip
        "button",  # every button label - the tabs and the connect rows too
        ".run-head",  # the line that toggles the run panel
        ".ev details summary",  # the disclosure line
        ".set-choice",  # a radio row you click by its label
    }


def test_the_run_panel_and_the_refocus_both_stand_off_a_live_selection() -> None:
    """The two handlers that would eat a selection made with the mouse: the
    panel-wide click-to-toggle (a drag ending in the command output still fires
    ``click`` on the panel) and the state push that puts the caret back in the
    composer (focusing a textarea collapses the document's selection)."""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "function draggedOutText()" in js
    assert js.count("draggedOutText()") == 3  # the definition and both guards
    toggle = js[js.index('el.run.addEventListener("click"') :]
    assert "if (draggedOutText()) return;" in toggle[: toggle.index("});")]


def test_a_transcript_note_keeps_the_line_breaks_the_controller_wrote() -> None:
    """`/skills`, `/mcp` and `/config` all answer with a BLOCK of text - a header
    and one indented row per thing. The node is filled by innerHTML from an
    escaped string, so without an explicit white-space rule the browser folds
    every one of those newlines into a space and the listing lands as a run-on
    paragraph. Pinned in the source, the way ``revealReply``'s branch is: there
    is no layout engine here to ask.

    ``pre-wrap`` specifically, not ``pre``: a long description still has to wrap
    inside the panel rather than push a horizontal scrollbar.
    """
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    rule = css[css.index(".ev-note {") :]
    rule = rule[: rule.index("}")]
    assert "white-space: pre-wrap;" in rule


# == revealing a finished reply ===============================================


def test_revealing_a_reply_parks_only_when_it_outgrows_the_viewport() -> None:
    """The GUI half of the TUI's ``_reveal_at`` (main-chat.md §6), which has no
    JS runner to assert against - so the branch is pinned in the source.

    Both cases matter and the second is the one that regressed: a reply that
    fits has to leave the panel following the bottom, because a park there
    would freeze the transcript for every event of the next turn.
    """
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function revealReply(win)") :]
    body = body[: body.index("\n  }")]
    # The reply is the transcript's tail, so its height is measured from the
    # first node down rather than from any node's own offsetHeight.
    assert "box.scrollHeight - node.offsetTop > box.clientHeight" in body
    oversized, fitting = body.split("box.scrollHeight - node.offsetTop > box.clientHeight", 1)
    assert "box.scrollTop = node.offsetTop;" in fitting[: fitting.index("return;")]
    tail = fitting[fitting.index("return;") :]
    assert "box.scrollTop = box.scrollHeight;" in tail
    assert "target.stick = true;" in tail and "target.parked = false;" in tail
    assert "box.hidden" in oversized  # the hidden-panel guard still comes first
