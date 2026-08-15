"""Pilot tests for cancelling a running tool call (ctrl+x).

The affordance only exists while the RunningBar is up, and pressing it must do
the whole job by itself: kill the command, skip whatever was queued behind it,
and get those results onto the clipboard for the model - the user should not
have to send anything afterwards. A real subprocess is used (the point is that
it actually dies), so the command drops a marker file first and the test only
cancels once that marker proves it is mid-flight.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.messages import ClipboardCaptured
from agentclip.shell.tui.screens.main import MainScreen

UTILS_PY = '''"""Utility helpers."""


def parse_date(s):
    return s
'''

# id=1 runs long (announcing itself via a marker file in the project root, which
# is run_command's cwd); id=2 is queued behind it and must never run.
SLOW_COMMAND = (
    "python -c \"import pathlib, time; print('working', flush=True); "
    "pathlib.Path('running.txt').write_text('go'); time.sleep(15)\""
)

REPLY_SLOW_COMMAND = f"""Let me run that.

~~~~
===CLIP:CALL id=1 tool=run_command===
command: {SLOW_COMMAND}
reason: watch it work for a while
timeout: 15
===CLIP:END===
===CLIP:CALL id=2 tool=read_file===
path: src/utils.py
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
~~~~
"""


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 30.0
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
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake, project


@pytest.fixture(autouse=True)
def _no_real_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here draws a click region, so the paste is never attempted - but
    a real Ctrl+V escaping into the test runner's window is unforgivable."""
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)


async def _start_session(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.load_text("Run the slow thing.")
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


async def test_ctrl_x_cancels_the_running_call_and_sends_the_results(tmp_path: Path) -> None:
    app, fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)

        main.post_message(ClipboardCaptured(REPLY_SLOW_COMMAND))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate for the command")
        writes_before = len(fake.written)
        await pilot.press("y")

        # The spinner is up (so the cancel key is live) and the command is
        # provably running before we press anything.
        await _wait_for(pilot, lambda: main.executing, "execute in flight")
        assert main.running_bar.display
        assert "ctrl+x" in str(main.running_bar.render())
        await _wait_for(pilot, lambda: (project / "running.txt").exists(), "command started")

        started = time.monotonic()
        await pilot.press("ctrl+x")

        await _wait_for(pilot, lambda: len(fake.written) > writes_before, "results copied out")
        assert time.monotonic() - started < 13  # the 15s sleep was killed, not waited out
        payload = fake.written[-1]
        # The killed command and the call queued behind it are both reported.
        assert "===CLIP:RESULT id=1 status=error code=cancelled===" in payload
        assert "cancelled by the user before completion" in payload
        assert "===CLIP:RESULT id=2 status=error code=cancelled===" in payload
        assert "skipped: the user cancelled this batch" in payload
        assert "parse_date" not in payload  # id=2 never read the file

        # The turn ended cleanly: armed for the model's next reply, spinner gone.
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")
        await _wait_for(pilot, lambda: not main.busy, "turn settled")
        assert not main.executing
        assert not main.running_bar.display


async def test_ctrl_x_while_idle_is_a_no_op(tmp_path: Path) -> None:
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)
        writes_before = len(fake.written)

        assert not main.executing
        await pilot.press("ctrl+x")
        # Calling straight through the port must be inert too (the binding's
        # check_action is a UI nicety, not the guard).
        main._controller.cancel_execution()
        await pilot.pause(0.1)

        assert len(fake.written) == writes_before  # nothing was sent to the model
        assert main.phase_name == "AWAITING_REPLY"
        assert not main.busy
        assert not main.running_bar.display
