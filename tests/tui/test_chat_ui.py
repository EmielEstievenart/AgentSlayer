"""Pilot tests for the chat-style UI: the persistent composer and the gate.

Complements test_smoke.py (the full approve-an-edit loop). Here we exercise the
new surfaces directly: sending a follow-up from the docked chat box, and the
focus hand-off at the approval gate (composer disabled, Approve button focused
so the bare-letter y still approves).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Button, TextArea

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured
from agentclip.tui.screens.new_session import NewSessionScreen
from agentclip.tui.screens.summary import SummaryScreen

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
===CLIP:EOM calls=1 turn=1===
~~~~
"""

REPLY_TASK_DONE = """All set - nothing else to change.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Tidied up src/utils.py; nothing else to do.
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
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
===CLIP:EOM calls=1 turn=2===
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
        engine_factory=make_engine_factory(config, project),
        project_root=project,
    )
    return app, fake, project


async def _start_session(app: AgentClipApp, pilot: Pilot) -> None:
    await _wait_for(pilot, lambda: isinstance(app.screen, NewSessionScreen), "session modal")
    app.screen.query_one("#task", TextArea).load_text("Tidy up src/utils.py.")
    await pilot.press("ctrl+s")
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")


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
