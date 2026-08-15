"""Pilot tests for the chat-style UI: the persistent composer and the gate.

Complements test_smoke.py (the full approve-an-edit loop). Here we exercise the
surfaces directly: the modal-free startup (empty chat + focused composer + the
settings sidebar), sending a follow-up from the docked chat box, and the focus
hand-off at the approval gate (composer disabled, Approve button focused so the
bare-letter y still approves).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured
from agentclip.tui.screens.confirm import ConfirmScreen
from agentclip.tui.screens.summary import SummaryScreen
from agentclip.tui.widgets.sidebar import Sidebar

from .conftest import send_composer

UTILS_PY = '''"""Utility helpers."""


def parse_date(s):
    return s
'''

REPLY_WITH_EDIT = """I'll fix it.

~~~~
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return s
EOT
replace <<EOT
    return s.strip()
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

REPLY_TASK_DONE = """All set - nothing else to change.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Tidied up src/utils.py; nothing else to do.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

REPLY_ASK_USER = """I need to know where to write.

~~~~
===CLIP:CALL id=1 tool=ask_user===
question <<EOT
Which absolute path should I write to?
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

# An edit reply for the turn AFTER a post-task_done follow-up reopens the session
# (the follow-up TASK is turn 2, so its reply echoes turn=2).
REPLY_EDIT_TURN2 = """On it.

~~~~
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return s
EOT
replace <<EOT
    return s.strip()
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> tuple[AgentClipApp, FakeClipboard, Path]:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "utils.py").write_text(UTILS_PY, encoding="utf-8", newline="")
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        # app.app_config, not the closed-over `config`: lets a service-editor save
        # take effect for the next session started in this test, same as cli.py.
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake, project


async def _start_session(
    app: AgentClipApp, pilot: Pilot, task: str = "Tidy up src/utils.py."
) -> None:
    """Start a session the only way there is: type the task, press Enter."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.load_text(task)
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")


async def _send_command(app: AgentClipApp, pilot: Pilot, command: str) -> None:
    """Type a slash command and send it - which takes two Enters, because the
    first completes the autocomplete row (§3.3a; see ``send_composer``). The
    autocomplete rules themselves live in test_slash_autocomplete_ui.py."""
    await send_composer(app, pilot, command)


async def test_startup_is_modal_free_with_sidebar(tmp_path: Path) -> None:
    """The launch experience (tui.md section 1.3): no modal - an empty chat, a
    focused composer, and a settings sidebar whose service picker seeds the
    session that the first message starts. F3 hides/shows the column."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        # No modal anywhere: the main screen owns the terminal from the first frame.
        assert app.screen is main
        assert not isinstance(app.screen, ModalScreen)
        # An empty transcript and a usable, focused chat box.
        assert not main.transcript.entries
        assert not main.composer.disabled
        assert app.focused is main.composer, f"composer should be focused, got {app.focused!r}"

        # The sidebar is mounted, unlocked, and defaults to config.general.service.
        sidebar = main.query_one(Sidebar)
        select = sidebar.query_one("#service-select", Select)
        assert not select.disabled
        assert select.value == app.app_config.general.service == "chatgpt-attach"
        assert sidebar.service == "chatgpt-attach"

        # Pick a different service, then start the session from the composer.
        select.value = "claude"
        await pilot.pause()
        assert sidebar.service == "claude"
        main.composer.load_text("Tidy up src/utils.py.")
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        # The sidebar's choice is what the session (and the status bar) runs with.
        assert main._snap is not None
        assert main._snap.service_key == "claude"
        assert main._snap.budget_chars == app.app_config.services["claude"].max_paste_chars
        # ...and it is locked for the duration of the session.
        assert select.disabled

        # F3 toggles the column (diffs want the horizontal room).
        assert sidebar.display
        await pilot.press("f3")
        await pilot.pause()
        assert not sidebar.display
        await pilot.press("f3")
        await pilot.pause()
        assert sidebar.display


async def test_edit_services_button_opens_settings(tmp_path: Path) -> None:
    """The sidebar's "Edit services..." button is the door to the settings screen
    (an F2 stub today; the service editor lands on the same hook)."""
    app, fake, _ = _make_app(tmp_path)
    calls: list[bool] = []
    app.action_settings = lambda: calls.append(True)  # type: ignore[method-assign]
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        button = main.query_one("#edit-services-btn", Button)
        await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
        await pilot.click("#edit-services-btn")
        await _wait_for(pilot, lambda: bool(calls), "settings action invoked")


async def test_empty_task_does_not_start_a_session(tmp_path: Path) -> None:
    """Enter on an empty composer must not start a session (it warns instead)."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await pilot.press("enter")
        await pilot.pause(0.1)
        assert main.awaiting_new_session  # still waiting
        assert not main.session_active
        assert not fake.written

        # A real task still starts it afterwards.
        main.composer.load_text("Tidy up src/utils.py.")
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.session_active, "session armed")


async def test_quit_while_awaiting_a_new_session_does_not_warn(
    tmp_path: Path, new_chat_click_lands: None
) -> None:
    """Waiting for a new session parks the session worker on a future, so the
    controller reports `busy` - but there is no turn to lose: quitting from the
    start screen must exit, not push the mid-turn warning."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        await _send_command(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "inline start flow re-armed")
        assert main.busy  # the session flow is parked on the inline prompt

        await app.action_quit()
        assert not isinstance(app.screen, ConfirmScreen)


async def test_followup_via_composer(tmp_path: Path) -> None:
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        # The chat box is enabled AND auto-focused when armed (chat-first design).
        composer = main.composer
        assert not composer.disabled
        assert app.focused is composer, (
            f"composer should auto-focus when armed, got {app.focused!r}"
        )
        writes_before = len(fake.written)

        # Type a follow-up and press Enter -> ChatComposer.Submitted -> follow-up flow.
        composer.load_text("Also add a docstring.")
        await pilot.press("enter")

        await _wait_for(pilot, lambda: len(fake.written) > writes_before, "follow-up copied")
        follow_up = fake.written[-1]
        assert "Also add a docstring." in follow_up
        assert any("you: Also add a docstring." in e for e in main.transcript.entries)
        # Composer is cleared after sending.
        assert main.composer.text == ""


async def test_followup_after_task_done(tmp_path: Path) -> None:
    """task_done completes the session but must not trap the user: no summary
    modal pops, the composer stays enabled, a follow-up reopens the session, and
    a full (gated) edit turn then runs end to end in the reopened session."""
    app, fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_TASK_DONE))
        await _wait_for(pilot, lambda: main.phase_name == "DONE", "task marked done")
        await _wait_for(pilot, lambda: not main.busy, "done flow settled")

        # The summary modal must NOT auto-open - the user stays in the chat.
        assert not isinstance(app.screen, SummaryScreen)
        assert app.screen is main
        # The model's summary made it into the transcript (not lost behind a modal).
        assert any("Tidied up src/utils.py" in e for e in main.transcript.entries)

        # The chat box stays enabled and auto-focused so a follow-up is possible.
        await _wait_for(pilot, lambda: not main.composer.disabled, "composer usable after done")
        await _wait_for(pilot, lambda: app.focused is main.composer, "composer focused after done")

        writes_before = len(fake.written)
        main.composer.load_text("One more thing: add a README note.")
        await pilot.press("enter")

        await _wait_for(pilot, lambda: len(fake.written) > writes_before, "follow-up copied")
        assert "One more thing: add a README note." in fake.written[-1]
        # The follow-up reopened the session: armed for the next reply again.
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "session reopened")
        await _wait_for(pilot, lambda: not main.busy, "follow-up flow settled")
        assert any("you: One more thing: add a README note." in e for e in main.transcript.entries)

        # A full gated edit turn now works in the reopened session, end to end.
        main.post_message(ClipboardCaptured(REPLY_EDIT_TURN2))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate after reopen")
        await pilot.press("y")
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed after reopen turn")
        on_disk = (project / "src" / "utils.py").read_text(encoding="utf-8")
        assert "s.strip()" in on_disk  # the post-reopen edit landed on disk


async def test_summary_reachable_after_done(tmp_path: Path) -> None:
    """task_done does not force-open the summary, but it must stay one keypress
    away: pressing e in DONE opens the SummaryScreen with the model's summary."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_TASK_DONE))
        await _wait_for(pilot, lambda: main.phase_name == "DONE", "task marked done")
        await _wait_for(pilot, lambda: not main.busy, "done flow settled")

        # Esc blurs the chat box so the bare-letter `e` reaches the screen binding.
        await _wait_for(pilot, lambda: app.focused is main.composer, "composer focused after done")
        await pilot.press("escape")
        await pilot.press("e")
        await _wait_for(
            pilot, lambda: isinstance(app.screen, SummaryScreen), "summary opened on demand"
        )
        summary_screen = app.screen
        assert isinstance(summary_screen, SummaryScreen)
        assert "Tidied up src/utils.py" in summary_screen._summary

        # Closing it returns to the chat, still completed and still continuable.
        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "back to the chat")
        assert main.phase_name == "DONE"
        assert not main.composer.disabled


async def test_gate_focus_lets_y_approve(tmp_path: Path) -> None:
    app, fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_WITH_EDIT))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate")

        # At the gate the composer yields focus to the Approve button, and the
        # composer is disabled so bare-letter keys can't be swallowed by it.
        assert main.composer.disabled
        approve = main.action_panel.query_one("#approve-btn", Button)
        assert app.focused is approve, f"expected Approve focused, got {app.focused!r}"
        # The auto-accept-edits button is shown for an edit gate.
        assert main.action_panel.query_one("#approve-edits-btn", Button).display

        # y bubbles past the focused Button to the screen binding and approves.
        await pilot.press("y")
        await _wait_for(pilot, lambda: len(fake.written) >= 2, "results copied")
        on_disk = (project / "src" / "utils.py").read_text(encoding="utf-8")
        assert "s.strip()" in on_disk
        # Back to armed, composer usable again.
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")
        await _wait_for(pilot, lambda: not main.composer.disabled, "composer re-enabled")


async def test_export_chat_log(tmp_path: Path) -> None:
    app, fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        # Run a full turn so the log has the model's prose, a tool call (with its
        # raw block) and the outbound payload - the "together with AI" content.
        main.post_message(ClipboardCaptured(REPLY_WITH_EDIT))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate")
        await pilot.press("y")
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")
        await _wait_for(pilot, lambda: not main.busy, "turn settled")

        # Esc blurs the chat box so the bare-letter `l` reaches the screen binding.
        assert main.composer.disabled is False
        await pilot.press("escape")
        await pilot.press("l")

        assert main._snap is not None
        session_dir = main._snap.session_dir
        await _wait_for(
            pilot,
            lambda: any(session_dir.glob("chat-log-*.md")),
            "chat log written",
        )
        log_path = next(iter(session_dir.glob("chat-log-*.md")))
        text = log_path.read_text(encoding="utf-8")
        assert text.startswith("# AgentClip chat log")
        assert "Tidy up src/utils.py." in text  # the user's task
        assert "I'll fix it." in text  # the model's prose
        assert "tool call 1 - edit_file src/utils.py" in text  # the tool call headline
        assert "===CLIP:CALL id=1 tool=edit_file===" in text  # the verbatim raw block
        # The outbound payload (results pasted back to the model) is captured too.
        assert "===CLIP:RESULT" in text and "outbound turn" in text
        # And the export left a breadcrumb in the transcript.
        assert any("chat log exported" in e for e in main.transcript.entries)


async def test_yolo_command_auto_approves_every_call(tmp_path: Path) -> None:
    """Typing /yolo in the chat box flips YOLO on: an edit that normally opens
    the approval gate now runs unattended - results are copied back WITHOUT any
    approval keypress, and the status bar shows the YOLO badge."""
    app, fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send_command(app, pilot, "/yolo")
        await _wait_for(pilot, lambda: main._snap is not None and main._snap.yolo, "yolo armed")
        assert main.composer.text == ""  # the command cleared the box
        # The status bar shows the YOLO badge (st-yolo class) in the edits segment.
        assert main.status_bar.query_one("#seg-edits").has_class("st-yolo")

        # The edit would normally gate; reaching "results copied" with no `y` press
        # proves it ran unattended (a gate would block here until a decision).
        writes_before = len(fake.written)
        main.post_message(ClipboardCaptured(REPLY_WITH_EDIT))
        await _wait_for(
            pilot, lambda: len(fake.written) > writes_before, "results copied unattended"
        )
        assert not main.pending_approval  # the gate never opened
        on_disk = (project / "src" / "utils.py").read_text(encoding="utf-8")
        assert "s.strip()" in on_disk
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")


async def test_new_command_clears_and_restarts(
    tmp_path: Path, new_chat_click_lands: None
) -> None:
    """Typing /new clears the chat window and re-arms the inline start flow -
    still no modal, and the service picker unlocks so the next session can pick
    a different one."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        assert main.session_active
        assert main.transcript.entries  # the bootstrap left content in the window
        assert main.sidebar.service_select.disabled  # locked while a session runs

        await _send_command(app, pilot, "/new")

        await _wait_for(pilot, lambda: main.awaiting_new_session, "inline start flow re-armed")
        assert app.screen is main  # no modal was pushed
        assert not main.session_active
        assert not main.transcript.entries  # the window was cleared
        assert not main.sidebar.service_select.disabled  # picker unlocked again
        await _wait_for(pilot, lambda: not main.composer.disabled, "composer usable again")

        # And a second session starts the same way.
        main.composer.load_text("Second task, please.")
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.session_active, "second session armed")
        assert "Second task, please." in fake.written[-1]


def _watch_segment_text(app: AgentClipApp) -> str:
    main = app.main_screen
    assert main is not None
    return str(main.status_bar.query_one("#seg-watch", Static).render())


async def test_status_bar_reads_idle_while_awaiting_the_first_task(tmp_path: Path) -> None:
    """Regression: while parked on the inline start prompt (launch, and every
    /new - tui.md section 3.3) the session WORKER is technically busy, but
    there is no turn for the user to wait on. The bar must say idle, not
    "working...", or the user reads it as a hang."""
    app, _fake, _project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        assert _watch_segment_text(app) == "○ idle"
        assert main.status_bar.query_one("#seg-watch").has_class("st-dim")


async def test_status_bar_reads_idle_again_after_new(
    tmp_path: Path, new_chat_click_lands: None
) -> None:
    """Same regression, on the /new path: a session that started (and so
    legitimately showed "working..." at some point) must not leave that label
    behind once /new re-arms the inline start prompt."""
    app, _fake, _project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send_command(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "inline start flow re-armed")

        assert _watch_segment_text(app) == "○ idle"
        assert main.status_bar.query_one("#seg-watch").has_class("st-dim")


async def test_slash_leading_answer_is_delivered_not_hijacked(tmp_path: Path) -> None:
    """Regression: an ask_user answer beginning with '/' (e.g. an absolute path)
    must be delivered to the model verbatim, NOT intercepted as a chat command -
    otherwise the answer is dropped and the session wedges on the answer future."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_ASK_USER))
        await _wait_for(pilot, lambda: main.awaiting_answer, "model asked a question")

        writes_before = len(fake.written)
        main.composer.load_text("/etc/hosts")
        await pilot.press("enter")

        # The flow resumes (no longer awaiting), the answer rides into the results
        # payload verbatim, and YOLO did NOT toggle (it was not parsed as a command).
        await _wait_for(pilot, lambda: not main.awaiting_answer, "answer accepted")
        await _wait_for(
            pilot, lambda: len(fake.written) > writes_before, "results copied after answering"
        )
        assert "/etc/hosts" in fake.written[-1]
        assert any("you: /etc/hosts" in e for e in main.transcript.entries)
        assert main._snap is not None and not main._snap.yolo


async def test_unknown_slash_command_is_reported(tmp_path: Path) -> None:
    """An unknown /command is rejected, not sent to the model as a message."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        writes_before = len(fake.written)
        main.composer.load_text("/bogus")
        await pilot.press("enter")
        await pilot.pause(0.1)
        # Nothing was copied (no follow-up went out) and the box was cleared.
        assert len(fake.written) == writes_before
        assert main.composer.text == ""
        assert not any("you: /bogus" in e for e in main.transcript.entries)


async def test_reject_button_opens_reason(tmp_path: Path) -> None:
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_WITH_EDIT))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate")

        # Clicking Reject opens the optional-reason input (ActionPanel.Decision path).
        # Wait for the button to actually have geometry: pilot.click() reads the
        # widget's region synchronously, and show_approval only *schedules* layout.
        reject_btn = main.action_panel.query_one("#reject-btn", Button)
        await _wait_for(pilot, lambda: reject_btn.region.width > 0, "reject button laid out")
        await pilot.click("#reject-btn")
        await _wait_for(pilot, lambda: main.reject_open, "reject reason input opened")
        assert main.action_panel.query_one("#reject-reason").display
