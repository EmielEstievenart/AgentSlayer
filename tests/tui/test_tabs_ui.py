"""Pilot tests for the tabbed transcript: one pane per session view.

The contract these pin down (main.py's ``transcript`` / ``open_session_view``):

* a sub-agent run gets its own pane, mounted and FOCUSED, labelled ``▶ title``;
* every later ``add_*`` lands in the focused pane and in no other one;
* finishing annotates the pane and ticks the tab, leaving both readable;
* focusing the master again reroutes output back;
* ``/new`` takes the sub panes away with the rest of the session;
* the export carries every pane, not just the visible one.

The load-bearing distinction is focused-panel vs visible-tab: the user browsing
tabs (by click or by F6) must never move where the controller writes, because
output landing in the tab someone happens to be reading looks exactly like data
loss. Two tests exist only for that.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot

from agentclip.app.types import SessionRef
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.slot import AgentSlot
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen

SIZE = (110, 40)

SUB_ONE = SessionRef(id="sub-1", role="subagent", title="read the docs", chat_name="jade-otter")
SUB_TWO = SessionRef(id="sub-2", role="subagent", title="survey the tests", chat_name="teal-moth")


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


async def _start_session(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = await _ready(app, pilot)
    main.composer.load_text("Do the thing.")
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


def _entries(main: MainScreen, view_id: str) -> list[str]:
    return main._panels[view_id].entries


async def test_opening_a_session_view_mounts_and_focuses_a_pane(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        assert main.chat_tabs.tab_count == 1  # just the master's

        await main.open_session_view(SUB_ONE)
        await pilot.pause()

        tabs = main.chat_tabs
        assert tabs.tab_count == 2
        assert tabs.get_tab("tab-sub-1").label_text == "▶ read the docs"
        # Mounted, shown AND focused: the controller writes here next.
        assert tabs.active == "tab-sub-1"
        assert main._focused_panel == "sub-1"
        assert main.transcript is main._panels["sub-1"]
        assert main.transcript.is_mounted


async def test_output_after_focusing_lands_only_in_that_pane(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.add_note("master line")
        await main.open_session_view(SUB_ONE)
        await main.add_user("read every file under src/")
        await main.add_note("sub-agent line")
        await pilot.pause()

        assert any("sub-agent line" in e for e in _entries(main, "sub-1"))
        assert any("you: read every file under src/" in e for e in _entries(main, "sub-1"))
        # The master's transcript is untouched by the sub-agent's output.
        assert any("master line" in e for e in _entries(main, "master"))
        assert not any("sub-agent line" in e for e in _entries(main, "master"))


async def test_finishing_ticks_the_tab_and_leaves_it_readable(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_note("did the work")

        await main.finish_session_view("sub-1", "sub-agent finished - result handed back")
        await pilot.pause()

        assert main.chat_tabs.get_tab("tab-sub-1").label_text == "✓ read the docs"
        entries = _entries(main, "sub-1")
        assert any("sub-agent finished" in e for e in entries)
        assert any("did the work" in e for e in entries)  # nothing was cleared
        assert main._panels["sub-1"].is_mounted  # nor unmounted


async def test_focusing_the_master_reroutes_output_back(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_note("inside the sub-run")

        main.focus_session_view("master")
        await main.add_note("back on the master")
        await pilot.pause()

        assert main.chat_tabs.active == "tab-master"
        assert any("back on the master" in e for e in _entries(main, "master"))
        assert not any("back on the master" in e for e in _entries(main, "sub-1"))
        # ...and the sub-agent's own transcript is still intact behind its tab.
        assert any("inside the sub-run" in e for e in _entries(main, "sub-1"))


async def test_clicking_a_tab_does_not_redirect_the_controller(tmp_path: Path) -> None:
    """The whole reason ``_focused_panel`` exists: the user reading the master
    tab mid-delegation must not divert the sub-agent's output into it."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await pilot.pause()

        master_tab = main.chat_tabs.get_tab("tab-master")
        await _wait_for(pilot, lambda: master_tab.region.width > 0, "master tab laid out")
        await pilot.click(master_tab)
        await _wait_for(pilot, lambda: main.chat_tabs.active == "tab-master", "master tab shown")

        # The user is LOOKING at the master; the controller is still writing to
        # the sub-agent's view.
        assert main._focused_panel == "sub-1"
        await main.add_note("still the sub-agent talking")
        await pilot.pause()
        assert any("still the sub-agent" in e for e in _entries(main, "sub-1"))
        assert not any("still the sub-agent" in e for e in _entries(main, "master"))


async def test_f6_cycles_the_visible_tab_without_moving_the_focus(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await pilot.press("f6")  # one tab: nothing to cycle to
        await pilot.pause()
        assert main.chat_tabs.active == "tab-master"

        await main.open_session_view(SUB_ONE)
        await main.open_session_view(SUB_TWO)
        await pilot.pause()
        assert main.chat_tabs.active == "tab-sub-2"

        await pilot.press("f6")  # wraps back to the master
        await pilot.pause()
        assert main.chat_tabs.active == "tab-master"
        await pilot.press("f6")
        await pilot.pause()
        assert main.chat_tabs.active == "tab-sub-1"
        # Browsing only: output still goes to the view the controller focused.
        assert main._focused_panel == "sub-2"
        await main.add_note("from the newest sub-agent")
        await pilot.pause()
        assert any("newest sub-agent" in e for e in _entries(main, "sub-2"))


async def test_render_log_carries_every_pane(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.add_user("tidy up the repo")
        await main.open_session_view(SUB_ONE)
        await main.add_note("scanned 12 files")
        main.focus_session_view("master")
        await main.add_note("delegation returned")
        await pilot.pause()

        assert main.has_transcript_events()
        text = main.render_log(["Project: demo"])
        assert text.startswith("# AgentClip chat log")
        assert "Project: demo" in text
        assert "tidy up the repo" in text  # the master's transcript
        assert "delegation returned" in text
        # ...followed by the sub-agent's, under its own heading.
        assert "## sub-agent: read the docs (jade-otter)" in text
        assert "scanned 12 files" in text
        assert text.index("delegation returned") < text.index("## sub-agent: read the docs")


async def test_has_transcript_events_sees_a_sub_agent_only_run(tmp_path: Path) -> None:
    """A sub-agent's transcript is transcript enough to export."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        assert not main.has_transcript_events()

        await main.open_session_view(SUB_ONE)
        await main.add_note("only the sub-agent said anything")
        await pilot.pause()
        assert main.has_transcript_events()


async def test_new_removes_the_sub_agent_panes(tmp_path: Path) -> None:
    """/new is the session teardown: the sub-agent tabs belong to the session
    that spawned them, and the next one numbers its runs from sub-1 again."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_note("sub-agent output")
        await pilot.pause()
        assert main.chat_tabs.tab_count == 2

        main.composer.load_text("/new")
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "inline start flow re-armed")
        await pilot.pause()

        assert main.chat_tabs.tab_count == 1
        assert main.chat_tabs.active == "tab-master"
        assert list(main._panels) == ["master"]
        assert main._focused_panel == "master"
        assert not main.transcript.entries  # the master pane was cleared too
        assert not main.has_transcript_events()

        # A fresh session reuses the id, and gets a fresh (empty) pane for it.
        await main.open_session_view(SUB_ONE)
        await pilot.pause()
        assert main.chat_tabs.tab_count == 2
        assert not _entries(main, "sub-1")


async def test_start_and_end_chat_map_onto_the_slot_primitives(tmp_path: Path) -> None:
    """The port adapters: a subagent ref drives the SUBAGENT window, anything
    else the master's, and end_chat is unconditional."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        calls: list[object] = []

        async def fake_start(slot: object) -> bool:
            calls.append(slot)
            return True

        main.start_browser_chat = fake_start  # type: ignore[method-assign]
        main.end_browser_chat = lambda: calls.append("end")  # type: ignore[method-assign]

        assert await main.start_chat(SUB_ONE) is True
        assert calls == [AgentSlot.SUBAGENT]
        master_ref = SessionRef(id="master", role="master", title="task", chat_name="amber-falcon")
        assert await main.start_chat(master_ref) is True
        assert calls[-1] is AgentSlot.MASTER

        await main.end_chat(SUB_ONE)
        assert calls[-1] == "end"
        assert main.delegation_available() is False  # nothing calibrated here
