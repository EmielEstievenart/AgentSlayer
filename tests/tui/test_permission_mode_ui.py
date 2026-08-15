"""Pilot tests for the permission mode's UI half (tui.md 2.6a, 3.3).

Two surfaces and one wire. The surface the user reads is the leftmost status
segment (``#seg-mode``, always shown, all three modes); the surface they press
is shift+tab, which has to work from the composer - i.e. from the app's default
focus - because that is where a user's hands are, and because Textual's own
Screen binds shift+tab to ``focus_previous``, which the MainScreen binding
deliberately overrides.

The wire is the last test: switching to plan and then feeding the session a real
write_file call proves the keystroke reached the *engine's* policy and not just
the paint - the call is auto-denied, no gate opens, the file is never written,
and the turn still completes. An edit rather than a command on purpose: it needs
nothing from the allowlist and no shell, so it stays decoupled from whatever
``run_command``'s parameters become.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Static

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured

UTILS_PY = '''"""Utility helpers."""


def parse_date(s):
    return s
'''

REPLY_WITH_WRITE = """I'll add the file.

~~~~
===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
alpha
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
    assert config.approval.mode == "ask", "the suite assumes the shipped default"
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake, project


async def _start_session(
    app: AgentClipApp, pilot: Pilot, task: str = "Tidy up src/utils.py."
) -> None:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.load_text(task)
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")


def _mode_segment(app: AgentClipApp) -> Static:
    main = app.main_screen
    assert main is not None
    return main.status_bar.query_one("#seg-mode", Static)


def _mode_text(app: AgentClipApp) -> str:
    return str(_mode_segment(app).render())


async def _cycle_to(app: AgentClipApp, pilot: Pilot, expected: str) -> None:
    """One shift+tab, then wait for the bar to say ``expected``.

    Polled rather than asserted straight after the press: the mode change goes
    out to the engine off the UI loop and comes back as a status push, exactly
    like /yolo.
    """
    await pilot.press("shift+tab")
    await _wait_for(pilot, lambda: _mode_text(app) == expected, f"status bar to read {expected}")


async def test_status_bar_shows_the_mode_before_any_session(tmp_path: Path) -> None:
    """The segment is painted from the configured default while the app is still
    parked on the start prompt - it is the leftmost cell of the bar from the
    first frame, not something a session brings with it."""
    app, _fake, _project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        assert _mode_text(app) == "MODE:ask"
        assert _mode_segment(app).has_class("st-dim")
        # Leftmost: the first .seg child of the bar, ahead of the watcher.
        first = main.status_bar.query(".seg").first()
        assert first.id == "seg-mode"


async def test_shift_tab_cycles_the_mode_from_the_composer(tmp_path: Path) -> None:
    """The whole feature in one keypress, pressed from where the user's hands
    are: the composer holds focus by default (it is a TextArea, which is why the
    binding is priority) and each shift+tab moves one step round the cycle."""
    app, _fake, _project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.composer.focus()
        await pilot.pause()
        assert app.focused is main.composer, f"composer should be focused, got {app.focused!r}"
        assert _mode_text(app) == "MODE:ask"

        await _cycle_to(app, pilot, "MODE:plan")
        assert app.focused is main.composer, "shift+tab must not move focus"
        assert _mode_segment(app).has_class("st-plan")
        assert main._controller.permission_mode == "plan"

        await _cycle_to(app, pilot, "MODE:unattended")
        assert _mode_segment(app).has_class("st-unattended")
        assert main._controller.permission_mode == "unattended"

        # ...and back round to the start.
        await _cycle_to(app, pilot, "MODE:ask")
        assert _mode_segment(app).has_class("st-dim")
        assert main._controller.permission_mode == "ask"


async def test_plan_mode_denies_an_edit_without_opening_a_gate(
    tmp_path: Path,
) -> None:
    """The end-to-end proof that the key reaches the engine's policy: in plan
    mode a write_file call is refused outright - no approval gate, no file - and
    the turn still finishes and copies its results, because a plan-mode denial
    is a result the model reads, not an abort."""
    app, fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        await _cycle_to(app, pilot, "MODE:plan")
        assert main._snap is not None and main._snap.mode == "plan"

        writes_before = len(fake.written)
        main.post_message(ClipboardCaptured(REPLY_WITH_WRITE))
        await _wait_for(pilot, lambda: len(fake.written) > writes_before, "results copied")
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")
        await _wait_for(pilot, lambda: not main.busy, "turn settled")

        assert not main.pending_approval, "plan mode must never open a gate"
        assert not (project / "notes.txt").exists(), "plan mode must not write"
        assert "plan mode is active" in fake.written[-1]
        assert any("plan mode" in entry for entry in main.transcript.entries)


async def test_shift_tab_without_a_session_arms_the_next_one(tmp_path: Path) -> None:
    """Pressed at the start prompt it is a real setting, not a no-op.

    "Only explore, do not change anything" is a decision about the task the user
    is *about* to type, so the cycle has to work before there is an engine to
    tell: the controller holds the mode, the segment moves at once, and the
    session started afterwards is armed with it before its bootstrap goes out -
    which is what the snapshot agreeing with the bar proves."""
    app, _fake, _project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _cycle_to(app, pilot, "MODE:plan")

        assert main._controller.permission_mode == "plan"
        assert _mode_segment(app).has_class("st-plan")
        assert main._snap is None  # no session behind it yet
        assert main.awaiting_new_session  # still waiting for the task, unharmed

        await _start_session(app, pilot)

        assert _mode_text(app) == "MODE:plan"
        assert main._snap is not None and main._snap.mode == "plan"
