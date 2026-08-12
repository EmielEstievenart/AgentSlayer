"""Pilot tests for the RUN PANEL: per-call visibility while a turn executes (§8a).

What replaced *"Working - running 2 tool calls..."* has to answer three
questions the one-liner could not: which call is running, what is queued behind
it, and what is the running command actually printing. So the suite drives a
real turn with two commands in it - a slow one that announces itself with a
marker file (the same trick tests/tui/test_cancel_ui.py uses, and for the same
reason: the assertions must land while the command is provably mid-flight) and a
quick one queued behind it.

The output half is driven through ``CallOutput`` rather than a real command's
stdout. That message IS the documented seam - the engine's worker thread posts
exactly these from ``ToolContext.on_output`` (tui/messages.py), and the tool
layer's own tests already prove a real process produces them - so posting one
here tests the half that lives on this side of the thread boundary without
making the assertion wait on somebody's process scheduler.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import CallOutput, ClipboardCaptured
from agentclip.tui.screens.main import MainScreen

# id=1 announces itself with a marker file in the project root (run_command's
# cwd) and then lingers; id=2 is queued behind it.
SLOW_COMMAND = (
    "python -c \"import pathlib, time; print('build started', flush=True); "
    "pathlib.Path('running.txt').write_text('go'); time.sleep(3)\""
)
QUICK_COMMAND = "python -c \"print('second call')\""

REPLY_TWO_COMMANDS = f"""Running both of these.

~~~~
===CLIP:CALL id=1 tool=run_command===
command: {SLOW_COMMAND}
reason: build the thing
timeout: 20
===CLIP:END===
===CLIP:CALL id=2 tool=run_command===
command: {QUICK_COMMAND}
reason: check the thing
timeout: 20
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
    project.mkdir(parents=True)
    (project / "README.md").write_text("demo\n", encoding="utf-8")
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
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)


async def _start_session(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.load_text("Build and check.")
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


async def _approve_all(main: MainScreen, pilot: Pilot, gates: int) -> None:
    for _ in range(gates):
        await _wait_for(pilot, lambda: main.pending_approval, "an approval gate")
        await pilot.press("y")


def _rows_text(main: MainScreen) -> str:
    return str(main.run_panel.rows_view.render())


def _tail_text(main: MainScreen) -> str:
    return str(main.run_panel.tail.render())


async def test_the_panel_lists_every_call_and_marks_the_running_one(tmp_path: Path) -> None:
    app, _fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)

        main.post_message(ClipboardCaptured(REPLY_TWO_COMMANDS))
        await _approve_all(main, pilot, gates=2)

        await _wait_for(pilot, lambda: main.executing, "execute in flight")
        await _wait_for(pilot, lambda: (project / "running.txt").exists(), "command 1 started")
        await _wait_for(pilot, lambda: "▶ 1" in _rows_text(main), "call 1 marked running")

        rows = _rows_text(main)
        assert "run_command" in rows
        assert "python" in rows  # the command line is the row's detail
        assert "• 2" in rows  # ...and call 2 is visibly queued behind it
        assert main.run_panel.display and main.running_bar.display

        # Both resolve, and the whole region goes away with the turn.
        await _wait_for(pilot, lambda: not main.executing, "the turn finished", timeout=40)
        await _wait_for(pilot, lambda: not main.busy, "the flow settled")
        assert not main.run_panel.display
        assert not main.running_bar.display


async def test_rows_resolve_to_glyphs_as_each_call_finishes(tmp_path: Path) -> None:
    app, _fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)

        main.post_message(ClipboardCaptured(REPLY_TWO_COMMANDS))
        await _approve_all(main, pilot, gates=2)
        await _wait_for(pilot, lambda: (project / "running.txt").exists(), "command 1 started")

        # Call 1 resolves while call 2 is still going: the row is the only place
        # that ever says so, since the transcript only lands at the end.
        await _wait_for(pilot, lambda: "✓ 1" in _rows_text(main), "call 1 ticked off", timeout=40)
        await _wait_for(pilot, lambda: not main.executing, "the turn finished", timeout=40)


async def test_ctrl_o_reveals_the_running_commands_output(tmp_path: Path) -> None:
    app, _fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)

        main.post_message(ClipboardCaptured(REPLY_TWO_COMMANDS))
        await _approve_all(main, pilot, gates=2)
        await _wait_for(pilot, lambda: (project / "running.txt").exists(), "command 1 started")
        await _wait_for(pilot, lambda: "▶ 1" in _rows_text(main), "call 1 marked running")
        # The command's own first line has to have landed before anything is
        # injected, or the two interleave and the injected lines split apart.
        await _wait_for(
            pilot,
            lambda: "build started" in "\n".join(main._run_output_lines(1)),
            "the real command's output streamed in",
        )

        # Collapsed by default, and the row says how to open it.
        assert not main.run_panel.expanded
        assert not main.run_panel.tail.display
        assert "ctrl+o" in _rows_text(main)

        main.post_message(CallOutput(1, "compiling module one\ncompiling modu"))
        await pilot.pause()
        await pilot.press("ctrl+o")
        await _wait_for(pilot, lambda: main.run_panel.expanded, "the output pane opened")

        tail = _tail_text(main)
        assert "compiling module one" in tail
        assert "compiling modu" in tail  # the unfinished line shows too

        # ...and it keeps up while the command runs.
        main.post_message(CallOutput(1, "le two\n"))
        await _wait_for(
            pilot, lambda: "compiling module two" in _tail_text(main), "the tail followed"
        )

        await pilot.press("ctrl+o")
        assert not main.run_panel.expanded
        assert not main.run_panel.tail.display

        await _wait_for(pilot, lambda: not main.executing, "the turn finished", timeout=40)


async def test_the_output_buffer_is_dropped_when_the_turn_ends(tmp_path: Path) -> None:
    """The panel is per-turn: nothing of it survives into the next one."""
    app, _fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)

        main.post_message(ClipboardCaptured(REPLY_TWO_COMMANDS))
        await _approve_all(main, pilot, gates=2)
        await _wait_for(pilot, lambda: (project / "running.txt").exists(), "command 1 started")
        main.post_message(CallOutput(1, "some output\n"))
        await pilot.pause()
        assert "some output" in main._run_output_lines(1)

        await _wait_for(pilot, lambda: not main.executing, "the turn finished", timeout=40)
        assert main._run_output_lines(1) == []
        assert not main.run_panel.display


async def test_ctrl_o_is_inert_while_nothing_is_running(tmp_path: Path) -> None:
    app, _fake, _project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = await _start_session(app, pilot)
        assert not main.executing

        await pilot.press("ctrl+o")
        await pilot.pause()

        assert not main.run_panel.display
        assert not main.run_panel.expanded
        assert main.phase_name == "AWAITING_REPLY"
