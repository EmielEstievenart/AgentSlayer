"""GuiView: every port method, and what the page is told about it.

``tests/shell/app`` pins what the session controller DECIDES and ``tests/driver/automation``
what the automation decides; this pins the third thing - that the GUI's adapter
turns each of those decisions into a well-formed event, and that the handful of
methods this slice implements smaller than the TUI's say so out loud rather than
returning silently (which is how a controller flow gets stranded).

Nothing here opens a window: the bridge's sink is a list (tests/shell/gui/conftest.py).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agentclip.driver.automation.host import AutomationHost
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.view import AutomationView
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.slot import AgentSlot
from agentclip.engine.engine import Decision, PendingAction, Phase, StatusSnapshot
from agentclip.protocol.types import Outbound, ToolCall
from agentclip.shell.app.types import SessionRef, SessionSpec
from agentclip.shell.app.view import ChatView, RunCall, SessionView
from agentclip.shell.gui.bridge import JsApi
from agentclip.shell.gui.view import MASTER_WINDOW, SUBAGENT_WINDOW, GuiView
from tests.shell.gui.conftest import Harness, settle


def port_methods(port: type) -> list[str]:
    return sorted(
        name
        for name, value in vars(port).items()
        if not name.startswith("_") and callable(value)
    )


def snapshot(phase: Phase = Phase.AWAITING_REPLY, **kwargs: object) -> StatusSnapshot:
    fields: dict[str, object] = {
        "phase": phase,
        "turn": 3,
        "service_key": "chatgpt",
        "budget_chars": 12000,
        "auto_accept_edits": False,
        "yolo": False,
        "mode": "ask",
        "session_dir": Path("."),
        "last_outbound_chars": 400,
    }
    fields.update(kwargs)
    return StatusSnapshot(**fields)  # type: ignore[arg-type]


def session_view(**kwargs: object) -> SessionView:
    fields: dict[str, object] = {
        "session_active": True,
        "busy": False,
        "pending_approval": False,
        "awaiting_answer": False,
        "has_outbound": True,
        "snapshot": snapshot(),
    }
    fields.update(kwargs)
    return SessionView(**fields)  # type: ignore[arg-type]


def call(name: str = "write_file", **params: str) -> ToolCall:
    return ToolCall(id=1, tool=name, params=dict(params), raw=f"CALL {name}\nbody\nEND")


class ControllerSpy:
    """Stands in for the SessionController the view builds for itself.

    ``GuiView`` constructs its own controller exactly as ``MainScreen`` does, so
    a test that wants to watch which controller call an answer makes swaps this
    in afterwards - the same reach the Pilot suites make into the screen.
    """

    def __init__(self) -> None:
        self.decisions: list[tuple[Decision, str | None]] = []
        self.messages: list[str] = []
        self.cancels = 0
        self.permission_mode = "ask"
        self.yolo = False  # the status bar's fallback when there is no snapshot

    def submit_decision(self, decision: Decision, note: str | None) -> None:
        self.decisions.append((decision, note))

    def submit_message(self, text: str) -> None:
        self.messages.append(text)

    def cancel_execution(self) -> None:
        self.cancels += 1


# == the ports are satisfied, all of them =====================================


@pytest.mark.parametrize("port", [ChatView, AutomationView, AutomationHost])
def test_the_gui_view_implements_every_method_of_every_port(port: type) -> None:
    """Structural, like MainScreen's conformance: the three protocols are what
    the two controllers call, and a missing method is a crash mid-turn rather
    than a type error at import."""
    missing = [name for name in port_methods(port) if not callable(getattr(GuiView, name, None))]
    assert not missing, f"GuiView is missing {port.__name__} methods: {missing}"


# == transcript ================================================================


async def test_every_transcript_add_produces_its_own_event(harness: Harness) -> None:
    view = harness.view
    await view.add_user("do **the** thing")
    await view.add_prose("here is why")
    await view.add_call(call("run_command", command="pytest -q"))
    await view.add_note("chat name: amber-falcon")
    await view.add_error("engine said no")
    await view.add_outbound(Outbound(kind="results", chunks=("payload",), total_chars=7, turn=2), "results")

    events = harness.flush().of_type("transcript")
    assert [event["kind"] for event in events] == [
        "user",
        "prose",
        "call",
        "note",
        "error",
        "outbound",
    ]
    assert events[0]["text"] == "do **the** thing"  # markdown is the page's to render
    assert events[2]["summary"] == "▶ call 1 run_command pytest -q"
    assert events[2]["raw"].startswith("CALL run_command")
    assert events[5]["payload"] == "payload"
    assert events[5]["turn"] == 2


async def test_the_transcript_export_carries_the_verbatim_payloads(harness: Harness) -> None:
    """``render_log`` is the export, and it must survive whatever the page
    prunes - so the text lives here, not in the DOM."""
    view = harness.view
    await view.add_user("task")
    await view.add_call(call("read_file", path="src/x.py"))
    assert view.has_transcript_events()

    log = view.render_log(["service: chatgpt"])
    assert log.startswith("# AgentClip chat log")
    assert "- service: chatgpt" in log
    assert "## [" in log and "you" in log
    assert "CALL read_file" in log  # fenced, verbatim


async def test_clearing_the_transcript_resets_the_automation_too(harness: Harness) -> None:
    """``/new`` is a session teardown, not a widget wipe: the pointers go home,
    the reply gate shuts and the poller is rebuilt - the calibrations do not."""
    view = harness.view
    await view.add_user("task")
    view.automation.open_reply_gate()
    harness.flush().clear()

    await view.clear_transcript()

    assert not view.has_transcript_events()
    assert view.automation.awaiting_pasted_reply is False
    assert view.automation.loop_state is LoopState.IDLE
    assert harness.flush().of_type("transcript_clear")


# == session views (one persistent transcript per window) =====================


async def test_a_delegated_run_opens_with_a_divider_in_the_sub_agents_transcript(
    harness: Harness,
) -> None:
    view = harness.view
    ref = SessionRef(id="sub-1", role="subagent", title="rename the helper", chat_name="jade-otter")
    await view.open_session_view(ref)
    await view.add_prose("sub-agent thinking")
    await view.finish_session_view("sub-1", "sub-agent finished", ok=True)

    events = harness.flush().of_type("transcript")
    assert "── task: rename the helper ──" in events[0]["text"]
    assert events[1]["text"] == "sub-agent thinking"
    assert events[2]["text"] == "sub-agent finished"
    assert events[2]["ok"] is True
    # All three in the SUB-AGENT window's transcript, none in the master's.
    assert [event["window"] for event in events] == [SUBAGENT_WINDOW] * 3


def test_focusing_an_unknown_session_is_harmless(harness: Harness) -> None:
    """The controller focuses by id; an id no window claims is ignored rather
    than fatal, and rather than guessing - a transcript line lost beats one
    written into the wrong conversation's panel."""
    harness.view.focus_session_view("sub-99")
    assert harness.view._focused_window == MASTER_WINDOW
    assert not harness.flush().of_type("focus_session")


# == state + the composer's precedence table ==================================


@pytest.mark.parametrize(
    ("kwargs", "mode", "enabled"),
    [
        ({"awaiting_answer": True}, "answer", True),
        ({"pending_approval": True}, "idle", False),
        ({"busy": True}, "idle", False),
        ({}, "message", True),
        ({"snapshot": None, "session_active": False}, "idle", False),
    ],
)
def test_the_composer_mode_follows_the_briefs_precedence(
    harness: Harness, kwargs: dict[str, object], mode: str, enabled: bool
) -> None:
    harness.view.render_state(session_view(**kwargs))
    event = harness.flush().last("state")
    assert event["composer_mode"] == mode
    assert event["composer_enabled"] is enabled


def test_done_is_its_own_composer_mode(harness: Harness) -> None:
    harness.view.render_state(session_view(snapshot=snapshot(Phase.DONE)))
    event = harness.flush().last("state")
    assert event["composer_mode"] == "done"
    assert "follow-up" in event["composer_placeholder"]


def test_the_sub_agent_run_keeps_the_box_open_for_abort(harness: Harness) -> None:
    harness.view.render_state(session_view(busy=True, session_role="subagent", session_title="x"))
    event = harness.flush().last("state")
    assert event["composer_mode"] == "abort"
    assert "/abort" in event["composer_placeholder"]


def test_the_state_event_carries_what_the_status_chrome_needs(harness: Harness) -> None:
    harness.view.render_state(session_view(snapshot=snapshot(yolo=True)))
    event = harness.flush().last("state")
    assert event["service"] == "chatgpt"
    assert event["turn"] == 3
    assert event["phase"] == "AWAITING_REPLY"
    assert event["yolo"] is True
    assert event["permission_mode"] == "ask"


# == the approval gate =========================================================


def gate_action(kind: str = "edit", **kwargs: object) -> PendingAction:
    fields: dict[str, object] = {
        "call": call("write_file", path="src/x.py"),
        "kind": kind,
        "preview": "--- a\n+++ b\n@@\n-old\n+new",
        "auto_reason": None,
        "always_pattern": None,
    }
    fields.update(kwargs)
    return PendingAction(**fields)  # type: ignore[arg-type]


def test_the_gate_opens_with_the_title_the_verdict_was_computed_from(
    harness: Harness,
) -> None:
    """A decoy ``command:`` on an mcp call must not repaint the gate as a shell
    line (main-chat.md section 6)."""
    action = PendingAction(
        call=call("mcp", tool="github.create_issue", command="git status", path="/etc/passwd"),
        kind="auto",
        preview="{...}",
        auto_reason=None,
    )
    harness.view.show_gate(action, "2/5", "✓1 read_file  ▶2 mcp")
    event = harness.flush().last("gate")
    assert event["open"] is True
    assert event["title"] == "APPROVE · call 2/5 · mcp github.create_issue"
    assert event["queue"] == "✓1 read_file  ▶2 mcp"
    assert event["preview"] == "{...}"


def test_the_third_button_is_offered_on_exactly_the_tuis_terms(harness: Harness) -> None:
    view = harness.view
    view.show_gate(gate_action("command"), "1/1", "")
    assert harness.flush().last("gate")["always_label"] == ""

    view.show_gate(gate_action("edit"), "1/1", "")
    assert harness.flush().last("gate")["always_label"] == "Approve + auto-edits"

    view.show_gate(gate_action("command", always_pattern="git commit *"), "1/1", "")
    assert harness.flush().last("gate")["always_label"] == "Always: git commit *"

    view.show_gate(gate_action("auto", always_pattern="*"), "1/1", "")
    assert harness.flush().last("gate")["always_label"] == "Always: calls like this one"


def test_a_diff_preview_is_labelled_as_one_for_the_page_to_colour(harness: Harness) -> None:
    """The page colours the diff by hand (no libraries), but WHICH renderer runs
    is decided here - the same branch ``ActionPanel.preview_renderable`` takes."""
    harness.view.show_gate(gate_action(), "1/1", "")
    event = harness.flush().last("gate")
    assert event["preview_kind"] == "diff"
    assert event["preview_body"] == "--- a\n+++ b\n@@\n-old\n+new"
    assert event["preview_head"] == ""


def test_a_brand_new_file_arrives_under_its_banner(harness: Harness) -> None:
    """A new file is all addition; the banner and the file are separate fields
    so the page can draw one as a heading and the other as numbered lines."""
    harness.view.show_gate(
        gate_action(preview="NEW FILE src/x.py (2 lines)\nimport os\nprint(os)"),
        "1/1",
        "",
    )
    event = harness.flush().last("gate")
    assert event["preview_kind"] == "new_file"
    assert event["preview_head"] == "NEW FILE src/x.py (2 lines)"
    assert event["preview_body"] == "import os\nprint(os)"


def test_a_command_gate_carries_the_line_and_the_models_stated_reason(
    harness: Harness,
) -> None:
    """Approving a command is a judgement about intent, not just syntax - so the
    model's own justification rides right under the command it justifies."""
    action = PendingAction(
        call=call("run_command", command="npm run build", reason="  the build   is stale ",
                  timeout="120"),
        kind="command",
        preview="npm run build\nreason: the build is stale\ncwd: /p",
        auto_reason=None,
    )
    harness.view.show_gate(action, "1/1", "")
    event = harness.flush().last("gate")
    assert event["preview_kind"] == "command"
    assert event["preview_head"] == "$ npm run build"
    assert event["reason"] == "reason: the build is stale"
    assert event["note"] == "not on the allowlist - approve to run once in the project root"
    assert event["timeout"] == "120"


def test_a_ruleset_command_gate_says_no_rule_allows_it_rather_than_no_allowlist(
    harness: Harness,
) -> None:
    harness.view.show_gate(
        gate_action("command", always_pattern="npm *", call=call("run_command", command="npm ci")),
        "1/1",
        "",
    )
    assert harness.flush().last("gate")["note"] == "no rule allows this - approve to run it once"


def test_an_mcp_gate_shows_the_engines_preview_not_the_decoy_command(
    harness: Harness,
) -> None:
    """Params are model-authored: a decoy ``command:`` riding an mcp call must
    not repaint the gate as a harmless shell line (main-chat.md section 6). The
    args come in full because for mcp the args ARE the semantics."""
    action = PendingAction(
        call=call("mcp", tool="github.create_issue", args='{"title": "x"}', command="git status"),
        kind="command",
        preview='github.create_issue {"title": "x"}',
        auto_reason=None,
    )
    harness.view.show_gate(action, "1/1", "")
    event = harness.flush().last("gate")
    assert event["preview_kind"] == "mcp"
    assert event["preview_head"] == 'github.create_issue {"title": "x"}'
    assert event["preview_body"] == '{"title": "x"}'
    assert "git status" not in event["preview_head"]


def test_an_overlong_reason_is_clipped_rather_than_shipped_whole(harness: Harness) -> None:
    action = gate_action("command", call=call("run_command", command="ls", reason="x" * 400))
    harness.view.show_gate(action, "1/1", "")
    reason = harness.flush().last("gate")["reason"]
    assert reason.endswith("…")
    assert len(reason) < 220


def test_the_hint_names_which_stop_asking_answer_the_third_button_is(
    harness: Harness,
) -> None:
    """"Always allow" without saying always allow WHAT is not something to press
    blind, and the two modes buy different things (a remembered pattern until
    restart, or auto-accepted edits for the session)."""
    view = harness.view
    view.show_gate(gate_action("command"), "1/1", "")
    assert harness.flush().last("gate")["hint"] == "press y to approve · n to reject"

    view.show_gate(gate_action("edit"), "1/1", "")
    assert "auto-accept edits this session" in harness.flush().last("gate")["hint"]

    view.show_gate(gate_action("command", always_pattern="git commit *"), "1/1", "")
    hint = harness.flush().last("gate")["hint"]
    assert "a to always allow git commit * (until AgentClip restarts)" in hint

    view.show_gate(gate_action("auto", always_pattern="*"), "1/1", "")
    assert "always allow calls like this one" in harness.flush().last("gate")["hint"]


def test_a_sub_agents_gate_says_whose_call_it_is(harness: Harness) -> None:
    harness.view.render_state(session_view(session_role="subagent", session_title="rename"))
    harness.view.show_gate(gate_action(), "1/1", "")
    assert harness.flush().last("gate")["title"].startswith("SUB-AGENT ‹rename› · APPROVE")


def test_the_gate_round_trip_reaches_the_controller_call_the_tui_makes(
    harness: Harness,
) -> None:
    spy = ControllerSpy()
    harness.view._controller = spy  # type: ignore[assignment]

    harness.view.show_gate(gate_action("edit"), "1/1", "")
    harness.view.submit_decision("approve", "")
    harness.view.submit_decision("approve_always", "")
    harness.view.submit_decision("reject", "  not like that  ")
    harness.view.hide_gate()

    assert spy.decisions == [
        (Decision.APPROVE, None),
        (Decision.APPROVE_ALL_EDITS, None),  # legacy mode, edit gate
        (Decision.REJECT, "not like that"),
    ]
    assert harness.flush().last("gate")["open"] is False


def test_always_allow_remembers_the_rulesets_pattern_when_there_is_one(
    harness: Harness,
) -> None:
    spy = ControllerSpy()
    harness.view._controller = spy  # type: ignore[assignment]
    harness.view.show_gate(gate_action("command", always_pattern="git commit *"), "1/1", "")
    harness.view.submit_decision("approve_always", "")
    assert spy.decisions == [(Decision.APPROVE_ALWAYS, None)]


def test_always_allow_is_refused_where_the_page_should_not_have_offered_it(
    harness: Harness,
) -> None:
    """A command gate in legacy mode has no third answer - commands stay
    allowlist-or-prompt - so the call is dropped rather than reinterpreted."""
    spy = ControllerSpy()
    harness.view._controller = spy  # type: ignore[assignment]
    harness.view.show_gate(gate_action("command"), "1/1", "")
    harness.view.submit_decision("approve_always", "")
    assert spy.decisions == []


def test_an_empty_reject_note_is_no_note(harness: Harness) -> None:
    spy = ControllerSpy()
    harness.view._controller = spy  # type: ignore[assignment]
    harness.view.submit_decision("reject", "   ")
    assert spy.decisions == [(Decision.REJECT, None)]


# == the run panel and the worker-thread family ===============================


def test_the_run_panel_gets_the_whole_plan_up_front(harness: Harness) -> None:
    rows = [
        RunCall(call_id=1, tool="read_file", detail="src/x.py"),
        RunCall(call_id=2, tool="run_command", detail="pytest -q", streams=True, glyph="✓"),
    ]
    harness.view.start_working("Working - running 2 tool calls...", rows)
    event = harness.flush().last("run")
    assert event["running"] is True
    assert event["calls"][1] == {
        "call_id": 2,
        "tool": "run_command",
        "detail": "pytest -q",
        "streams": True,
        "glyph": "✓",
    }
    harness.view.stop_working()
    assert harness.flush().last("run")["running"] is False


def test_the_call_family_is_safe_from_a_worker_thread(harness: Harness) -> None:
    """The port's one thread contract: these three arrive mid-``execute()`` from
    the engine's worker thread and must be non-blocking and ordered."""
    view = harness.view

    def worker() -> None:
        view.call_started(1, "run_command", "pytest -q")
        for i in range(20):
            view.call_output(1, f"line {i}\n")
        view.call_finished(1, "✓")

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    recorder = harness.flush()
    assert recorder.of_type("run_call")[0] == {
        "type": "run_call",
        "phase": "started",
        "call_id": 1,
        "tool": "run_command",
        "detail": "pytest -q",
        "streams": True,
    }
    assert recorder.of_type("run_call")[-1] == {
        "type": "run_call",
        "phase": "finished",
        "call_id": 1,
        "glyph": "✓",
    }
    chunks = [event["chunk"] for event in recorder.of_type("run_output")]
    assert chunks == [f"line {i}\n" for i in range(20)]


def test_only_a_run_command_row_is_expandable(harness: Harness) -> None:
    """``streams`` is what makes ctrl+o do anything, and it is decided here for
    a row the panel never planned - exactly as ``RunPanel.call_started`` does.
    Expanding a write_file row would open an empty pane and teach the user the
    key does nothing."""
    harness.view.call_started(7, "write_file", "src/x.py")
    harness.view.call_started(8, "run_command", "pytest -q")
    started = harness.flush().of_type("run_call")
    assert [event["streams"] for event in started] == [False, True]


def test_a_cancel_from_the_page_reaches_the_controller(harness: Harness) -> None:
    """ctrl+x's whole round trip: the page asks, the shell forwards, and the
    turn still finishes and reports back - it is not an abort."""
    spy = ControllerSpy()
    harness.view._controller = spy  # type: ignore[assignment]
    # Straight onto the view: the runner's marshalling is tests/shell/gui/test_runner's.
    api = JsApi(harness.view)
    api.cancel()
    assert spy.cancels == 1


def test_the_pages_gate_and_composer_calls_land_on_the_same_doors(harness: Harness) -> None:
    """``JsApi`` is a marshalling shim, not a second controller: every method
    forwards to the call the TUI's key bindings make."""
    spy = ControllerSpy()
    harness.view._controller = spy  # type: ignore[assignment]
    api = JsApi(harness.view)
    harness.view.show_gate(gate_action("edit"), "1/1", "")
    api.decide("approve", "")
    api.decide("reject", "wrong file")
    api.submit("a follow-up")
    assert spy.decisions == [(Decision.APPROVE, None), (Decision.REJECT, "wrong file")]
    assert spy.messages == ["a follow-up"]


# == toasts ====================================================================


def test_notify_and_alert_both_land_as_toasts(harness: Harness) -> None:
    harness.view.notify("approval needed", severity="warning", timeout=8)
    harness.view.alert("task done")
    events = harness.flush().of_type("toast")
    assert events[0]["severity"] == "warning"
    assert events[0]["timeout"] == 8
    assert events[1]["message"] == "task done"


# == the automation's paints ===================================================


def test_the_automation_paints_all_reach_the_page(harness: Harness) -> None:
    view = harness.view
    view.paint_loop_state(LoopState.WAIT_SEND)
    view.paint_detection(TemplateKind.COPY, "copy button: not on screen")
    view.paint_stale("stale: unchanged for 2.0s")
    view.paint_elements({TemplateKind.COPY: None, TemplateKind.BUSY: None})
    view.show_paste_flash(">>> PRESS ENTER <<<", retry=True)
    view.hide_paste_flash()
    view.paint_armed(False)

    recorder = harness.flush()
    assert recorder.of_type("rail")[-1]["loop"] == "IDLE"  # painted from the controller
    assert recorder.of_type("detection") == [
        {
            "type": "detection",
            "kind": "COPY",
            "label": "copy",
            "text": "copy button: not on screen",
        },
        {"type": "detection", "kind": "STALE", "label": "", "text": "stale: unchanged for 2.0s"},
    ]
    # The column's own contract is pinned in tests/shell/gui/test_elements.py; what
    # matters here is that the paint reached the page at all.
    assert recorder.last("elements")["rows"]
    assert recorder.of_type("flash") == [
        {"type": "flash", "show": True, "text": ">>> PRESS ENTER <<<", "retry": True},
        {"type": "flash", "show": False},
    ]
    assert recorder.last("armed")["armed"] is True  # the flag, not the payload


def test_arming_says_so_and_repaints(harness: Harness) -> None:
    harness.view.set_os_armed(False)
    recorder = harness.flush()
    assert harness.view.automation.os_armed is False
    assert recorder.last("status")["armed"] is False
    assert any("DISARMED" in event["message"] for event in recorder.of_type("toast"))


# == delivery: the manual fallback, with nothing calibrated ===================


async def test_an_outbound_with_nothing_calibrated_takes_the_manual_path(
    harness: Harness,
) -> None:
    """The whole point of driving the real AutomationController: no chat window
    is drawn (the GUI has no calibration surface yet) and the manual provider
    refuses the write, so the delivery lands exactly where it should - the
    payload shown for a hand copy, no click, no synthetic Ctrl+V, and the
    "paste it yourself" banner up."""
    await harness.view.copy_outbound("===CLIP:TASK===\nhello\n")

    recorder = harness.flush()
    assert recorder.last("payload")["text"].startswith("===CLIP:TASK===")
    assert recorder.last("flash")["show"] is True
    assert recorder.last("flash")["retry"] is True
    assert harness.view.automation.loop_state is LoopState.MANUAL_INSERT
    # ...and the reply gate is open, because the payload is out either way.
    assert harness.view.automation.awaiting_pasted_reply is True


def test_park_off_clipboard_shows_the_payload_rather_than_swallowing_it(
    harness: Harness,
) -> None:
    """The GUI has no OSC-52. Showing the text where a human can select it is
    the honest equivalent (docs/design/gui.md section 2)."""
    harness.view.park_off_clipboard("payload text")
    recorder = harness.flush()
    assert recorder.last("payload")["text"] == "payload text"
    assert recorder.last("toast")["severity"] == "error"


# == blocking prompts ==========================================================


async def test_prompt_new_session_is_inline_and_the_composer_send_resolves_it(
    harness: Harness,
) -> None:
    view = harness.view
    pending = asyncio.ensure_future(view.prompt_new_session())
    await settle()
    assert harness.flush().last("state")["composer_mode"] == "task"

    view.submit_text("rename the helper")
    spec = await pending
    assert isinstance(spec, SessionSpec)
    assert spec.task == "rename the helper"
    assert spec.service  # whatever the master window is pointed at
    # ...and the box is emptied by the send, as the TUI's is.
    assert harness.flush().of_type("composer_reset")


async def test_a_slash_line_at_the_task_prompt_is_a_command_not_a_task(
    harness: Harness,
) -> None:
    view = harness.view
    spy = ControllerSpy()
    pending = asyncio.ensure_future(view.prompt_new_session())
    await settle()
    view._controller = spy  # type: ignore[assignment]

    view.submit_text("/identify")
    await settle()
    assert spy.messages == ["/identify"]
    assert not pending.done()  # the prompt is still up

    view.submit_text("//literally starts with a slash")
    spec = await pending
    assert spec is not None
    assert spec.task == "/literally starts with a slash"


async def test_an_empty_task_is_refused_with_a_toast(harness: Harness) -> None:
    view = harness.view
    pending = asyncio.ensure_future(view.prompt_new_session())
    await settle()
    view.submit_text("   ")
    await settle()
    assert not pending.done()
    assert "describe the task" in harness.flush().last("toast")["message"]
    view.submit_text("real task")
    await pending


async def test_confirm_prompt_text_and_summary_round_trip_through_the_page(
    harness: Harness,
) -> None:
    view = harness.view

    pending = asyncio.ensure_future(view.confirm("Undo?", "this rolls back one turn"))
    await settle()
    modal = harness.flush().last("modal")
    assert modal["modal"] == "confirm" and modal["title"] == "Undo?"
    view.answer_prompt(modal["prompt_id"], True)
    assert await pending is True

    pending_text = asyncio.ensure_future(view.prompt_text("Paste the reply", "ctrl+v"))
    await settle()
    modal = harness.flush().last("modal")
    assert modal["modal"] == "text"
    view.answer_prompt(modal["prompt_id"], "the reply")
    assert await pending_text == "the reply"

    pending_summary = asyncio.ensure_future(view.show_summary([("turns", "3")], "all done"))
    await settle()
    modal = harness.flush().last("modal")
    assert modal["modal"] == "summary" and modal["rows"] == [["turns", "3"]]
    view.answer_prompt(modal["prompt_id"], "new")
    assert await pending_summary == "new"


async def test_a_stale_prompt_answer_resolves_nothing(harness: Harness) -> None:
    """The flows that open these are the ones an abort poisons: an answer to a
    question that is gone must not resolve the next one."""
    view = harness.view
    pending = asyncio.ensure_future(view.confirm("Undo?"))
    await settle()
    first = harness.flush().last("modal")["prompt_id"]
    view.answer_prompt(first, False)
    assert await pending is False
    view.answer_prompt(first, True)  # the double-tap: no future left to resolve

    pending_again = asyncio.ensure_future(view.confirm("Again?"))
    await settle()
    assert not pending_again.done()
    view.answer_prompt(harness.flush().last("modal")["prompt_id"], True)
    assert await pending_again is True


# == scheduling, exit, and the reduced-scope methods ===========================


def test_spawn_puts_the_flow_on_the_injected_loop(harness: Harness) -> None:
    async def flow() -> None:
        return None

    harness.view.spawn(flow())
    assert harness.scheduled and "flow" in harness.scheduled[0]


def test_exit_app_closes_the_window(harness: Harness) -> None:
    harness.view.exit_app()
    assert harness.exits == 1


def test_no_port_method_is_reduced_scope_any_more(harness: Harness) -> None:
    """The list this used to parametrize over is empty.

    ``toggle_harness_log`` came off it with parity increment 2 (the pane landed),
    the three session-view methods with increment 3, and the last two -
    ``show_identify_overlay`` and ``paint_elements`` - with increment 4: the
    overlay is the same child process the TUI shells out to, and the crops are
    real PNGs (tests/shell/gui/test_elements.py). What is left is a guard: a method
    re-reduced to a toast would be a parity regression, not a new smaller
    implementation, and it should have to delete this test to happen.
    """
    view = harness.view
    assert not view._elements  # nothing searched yet - the resting state
    view.show_identify_overlay()
    assert harness.scheduled, "/identify went nowhere"
    assert not harness.flush().of_type("toast")


async def test_delegation_is_unavailable_and_says_what_is_missing(harness: Harness) -> None:
    """Nothing is calibrated in this shell yet, so the model must be told the
    real gaps rather than be offered a tool the host cannot honour."""
    assert harness.view.delegation_available() is False
    assert harness.view.delegation_missing()


async def test_find_all_answers_empty_when_no_window_is_drawn(harness: Harness) -> None:
    assert await harness.view.find_all(TemplateKind.COPY) == []


async def test_find_all_refuses_a_slot_and_a_scene_together(harness: Harness) -> None:
    """A frame was taken from ONE window; translating its matches through
    another slot's rectangle would put them anywhere at all."""
    with pytest.raises(ValueError, match="never both"):
        await harness.view.find_all(
            TemplateKind.COPY,
            slot=AgentSlot.MASTER,
            scene=object(),  # type: ignore[arg-type] - refused before it is read
        )
