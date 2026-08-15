"""Pilot tests for the sidebar's STATE rail (tui.md section 1.3).

The rail is an eight-line readout of ``automation.loop_state.LoopState`` painted at
the very top of the sidebar - where in the browser-automation loop (idle, auto
insert, manual insert, wait send, wait generate, auto copy, manual copy,
interpreting) the live window is, and which states ``LOOP_TRANSITIONS`` says
are legally next - so "what is the tool doing to the browser right now" never
needs a click. Deliberately NOT ``engine.states.Phase``: the engine's machine
says where the task is, this one where the paste-send-generate-copy round trip
is.

Reuses the same "no launch modal, type the task, Enter starts it" flow
test_chat_ui.py drives (``_start_session``) to walk the rail through real
transitions rather than poking the state by hand. In the headless test
environment no chat window is calibrated, so the auto-insert attempt refuses
to click and resolves to MANUAL_INSERT - which is itself the transition under
test.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Static

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.messages import ClipboardCaptured
from agentclip.shell.tui.widgets.sidebar import Sidebar, state_row_id

# The rail's whole state list, in the order the sidebar paints them - the same
# tuple ``Sidebar._LOOP_ORDER`` uses: the loop's running order.
_ALL_LOOP_STATES = (
    LoopState.IDLE,
    LoopState.AUTO_INSERT,
    LoopState.MANUAL_INSERT,
    LoopState.WAIT_SEND,
    LoopState.WAIT_GENERATE,
    LoopState.AUTO_COPY,
    LoopState.MANUAL_COPY,
    LoopState.INTERPRETING,
)

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


def _state_row(app: AgentClipApp, state: LoopState) -> Static:
    assert app.main_screen is not None
    return app.main_screen.query_one(f"#{state_row_id(state)}", Static)


def _active_state(app: AgentClipApp) -> LoopState | None:
    for state in _ALL_LOOP_STATES:
        if _state_row(app, state).has_class("side-state-active"):
            return state
    return None


def _legal_states(app: AgentClipApp) -> set[LoopState]:
    return {s for s in _ALL_LOOP_STATES if _state_row(app, s).has_class("side-state-legal")}


async def test_state_rail_shows_idle_on_launch(tmp_path: Path) -> None:
    """Nothing is outstanding: the rail shows IDLE active, with AUTO_INSERT -
    the only move ``LOOP_TRANSITIONS[IDLE]`` allows - at normal brightness."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        assert _active_state(app) is LoopState.IDLE
        assert "idle" in str(_state_row(app, LoopState.IDLE).render())
        assert _legal_states(app) == {LoopState.AUTO_INSERT}
        # everything else is neither active nor legal-next (dim).
        for state in _ALL_LOOP_STATES:
            if state not in (LoopState.IDLE, LoopState.AUTO_INSERT):
                row = _state_row(app, state)
                assert not row.has_class("side-state-active")
                assert not row.has_class("side-state-legal")


async def test_state_rail_rows_are_the_whole_loop(tmp_path: Path) -> None:
    """One row per LoopState, in loop order, and nothing else - in particular
    no leftover ``engine.states.Phase`` rows (the rail this one replaced)."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        sidebar = main.query_one(Sidebar)
        rail_ids = [row.id for row in sidebar.query(".side-state-row")]
        assert rail_ids == [state_row_id(state) for state in _ALL_LOOP_STATES]


async def test_state_rail_resolves_the_insert_attempt(tmp_path: Path) -> None:
    """Starting a task copies the bootstrap and tries to insert it into the
    browser itself. With no chat window calibrated the click is refused, so the
    auto insert resolves to MANUAL_INSERT - the user's Ctrl+V - and the
    legal-next styling moves with it rather than staying on IDLE's set."""
    app, _fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)

        assert _active_state(app) is LoopState.MANUAL_INSERT
        assert _legal_states(app) == {LoopState.WAIT_SEND, LoopState.WAIT_GENERATE}

        idle_row = _state_row(app, LoopState.IDLE)
        assert not idle_row.has_class("side-state-active")
        assert not idle_row.has_class("side-state-legal")


async def test_state_rail_walks_a_real_turn(tmp_path: Path) -> None:
    """A harvested reply lands in INTERPRETING and stays there while the
    approval gate holds (the reply is still being acted on); approving runs the
    turn, whose next outbound restarts the loop at the insert attempt."""
    app, _fake, project = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_WITH_EDIT))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate")
        assert _active_state(app) is LoopState.INTERPRETING
        assert _legal_states(app) == {LoopState.AUTO_INSERT, LoopState.IDLE}

        await pilot.press("y")
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")
        await _wait_for(pilot, lambda: not main.busy, "turn settled")
        # The results payload went out and its insert attempt resolved (no
        # calibration here, so to the manual fallback): the loop went round.
        assert _active_state(app) is LoopState.MANUAL_INSERT
        on_disk = (project / "src" / "utils.py").read_text(encoding="utf-8")
        assert "s.strip()" in on_disk


async def test_state_rail_settles_idle_after_task_done(tmp_path: Path) -> None:
    """task_done ends the turn with nothing outstanding and no next outbound:
    once the done flow settles, the rail is back at IDLE waiting for the user."""
    app, _fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_TASK_DONE))
        await _wait_for(pilot, lambda: main.phase_name == "DONE", "task marked done")
        await _wait_for(pilot, lambda: not main.busy, "done flow settled")

        await _wait_for(
            pilot, lambda: _active_state(app) is LoopState.IDLE, "rail settled back to idle"
        )
        assert _legal_states(app) == {LoopState.AUTO_INSERT}
