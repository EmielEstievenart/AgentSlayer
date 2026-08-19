"""Pilot tests for `r` - the extra-instructions re-inject (tui.md 3.4h).

The engine owns the flag and the controller owns both refusals (covered in
tests/engine and tests/shell/app); what is pinned here is the screen's two halves.

First, the GATING, which is unusual on this screen: `r` is hidden outright
rather than dimmed on a service that carries no instructions, because unlike
`u` or `i` it is not a key that becomes available in a moment - on that service
it never does. And second, the READOUT: the status bar's `INSTR` segment is the
only thing that says the flag is lit, which is the whole reason the key is a
toggle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.engine.engine import Phase, StatusSnapshot
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.main import MainScreen


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> AgentClipApp:
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    app = AgentClipApp(
        config=config,
        provider=FakeClipboard(),
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
    )
    return app


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


def _snapshot(*, armed: bool, has: bool = True) -> StatusSnapshot:
    return StatusSnapshot(
        phase=Phase.AWAITING_REPLY,
        turn=3,
        service_key="claude",
        budget_chars=24_000,
        auto_accept_edits=False,
        yolo=False,
        mode="build",
        unattended=False,
        session_dir=Path("."),
        last_outbound_chars=120,
        has_extra_instructions=has,
        instructions_armed=armed,
    )


async def test_the_key_is_hidden_on_a_service_with_nothing_to_re_inject(
    tmp_path: Path,
) -> None:
    """`False`, not `None`: a dimmed key in the footer would be advertising
    something this service can never do."""
    app = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        main.session_active = True
        main.has_extra_instructions = False
        await pilot.pause()

        assert main.check_action("reinstruct", ()) is False


async def test_the_key_is_live_once_a_session_carries_instructions(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        main.has_extra_instructions = True
        main.session_active = False
        await pilot.pause()
        assert main.check_action("reinstruct", ()) is None  # dimmed: no session yet

        main.session_active = True
        await pilot.pause()
        assert main.check_action("reinstruct", ()) is True


async def test_the_screen_follows_the_engine_snapshot_not_the_config(
    tmp_path: Path,
) -> None:
    """A service edited mid-session has not reached the running engine, so the
    reactive is fed from the pushed snapshot and from nothing else."""
    app = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        assert main.has_extra_instructions is False

        main._controller._snap = _snapshot(armed=False)
        main._controller._push_state()
        await pilot.pause()

        assert main.has_extra_instructions is True


async def test_the_status_segment_lights_only_while_armed(tmp_path: Path) -> None:
    """The one readout the toggle exists for - and hidden the rest of the time,
    so it never becomes furniture the eye stops reading."""
    app = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        segment = main.status_bar.query_one("#seg-instr", Static)

        main._snap = _snapshot(armed=False)
        main._paint_status()
        await pilot.pause()
        assert segment.display is False

        main._snap = _snapshot(armed=True)
        main._paint_status()
        await pilot.pause()
        assert segment.display is True
        assert "INSTR" in str(segment.render())


async def test_the_action_asks_the_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole decision - both refusals included - lives on the far side of
    this call, because only the engine knows what the session's preset says."""
    calls: list[str] = []
    app = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        monkeypatch.setattr(
            type(main._controller), "reinstruct", lambda self: calls.append("asked")
        )

        main.action_reinstruct()
        await pilot.pause()

        assert calls == ["asked"]
