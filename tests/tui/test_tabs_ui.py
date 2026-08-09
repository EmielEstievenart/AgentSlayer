"""Pilot tests for the window tab bar: two rows, one tab per browser window.

A tab is a *window* now, not a session view, and that is the whole shape these
pin down (main.py's ``_select_window`` / ``open_session_view`` / ``render_log``):

* two rows of fixed tabs - master windows over the selected master's sub-agent
  windows - present before any session and still there after ``/new``;
* selecting one shows that window's transcript AND points the sidebar at it
  (what the AGENT SLOT picker used to do), while never moving ``_live``;
* a delegation appends a divider + its run to the sub-agent window's persistent
  panel instead of minting a pane, and the tab carries the live state (``▶``
  running, ``✓`` after a run that handed a result back, ``✗`` after one that did
  not, bare before the first one);
* which service each window tab starts on, including the blank-means-master
  rule for ``[general] subagent_service``;
* every later ``add_*`` lands in the FOCUSED window and no other;
* ``/new`` empties both transcripts and keeps both tabs;
* the export still carries one heading per RUN, not one per window.

The load-bearing distinction is focused-panel vs selected-tab: the user
browsing tabs (by click or by F6) must never move where the controller writes,
because output landing in the tab someone happens to be reading looks exactly
like data loss. Three tests exist only for that.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from textual.pilot import Pilot

from agentclip.app.types import SessionRef
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import Config, GeneralConfig, load_config
from agentclip.screen.slot import AgentSlot
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MASTER_WINDOW, SUBAGENT_WINDOW, MainScreen
from agentclip.tui.widgets.window_tabs import WindowTab

from .conftest import send_composer

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


def _entries(main: MainScreen, window: str) -> list[str]:
    return main._panels[window].entries


def _label(main: MainScreen, window: str) -> str:
    return main.chat_tabs.tab(window).label_text


# -- which service each window tab starts on ----------------------------------
#
# ``_initial_services`` is a pure function of the Config, so it is checked
# without a Pilot: it is the whole of "[general] subagent_service" reaching the
# UI, and the blank default is what keeps that key invisible to everybody
# running one service in both windows.


def _config_with(**general: str) -> Config:
    return replace(Config(), general=replace(GeneralConfig(), **general))


def test_a_blank_subagent_service_puts_both_tabs_on_the_master_s() -> None:
    services = MainScreen._initial_services(_config_with(service="claude"))
    assert services == {MASTER_WINDOW: "claude", SUBAGENT_WINDOW: "claude"}


def test_a_named_subagent_service_points_the_second_tab_at_it() -> None:
    services = MainScreen._initial_services(
        _config_with(service="claude", subagent_service="gemini")
    )
    assert services == {MASTER_WINDOW: "claude", SUBAGENT_WINDOW: "gemini"}


def test_a_subagent_service_naming_no_preset_falls_back_to_the_master_s() -> None:
    """load_config warns and blanks an unknown key, but a hand-built Config (or
    a preset deleted since) must not point a window at a service that is in no
    picker - ``Config.preset()``'s fallback would then drive the automation."""
    services = MainScreen._initial_services(
        _config_with(service="claude", subagent_service="no-such-service")
    )
    assert services == {MASTER_WINDOW: "claude", SUBAGENT_WINDOW: "claude"}


async def test_both_window_tabs_exist_before_any_session(tmp_path: Path) -> None:
    """The tabs are furniture, not session state: the windows they name are open
    browser windows, so both are there from launch with the master selected."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        tabs = main.chat_tabs

        assert [tab.window for tab in tabs.query(WindowTab)] == [MASTER_WINDOW, SUBAGENT_WINDOW]
        assert tabs.selected == MASTER_WINDOW
        assert main._calibrating is AgentSlot.MASTER
        assert main._focused_panel == MASTER_WINDOW
        # Each tab names the service its window is pointed at; the sub-agent's
        # carries no glyph until something has actually run in it.
        service = main._service_for(AgentSlot.MASTER)
        assert _label(main, MASTER_WINDOW) == f"MASTER · {service}"
        assert _label(main, SUBAGENT_WINDOW) == f"SUB-AGENT · {service}"
        # Only the selected window's transcript is on screen.
        assert main._panels[MASTER_WINDOW].display
        assert not main._panels[SUBAGENT_WINDOW].display


async def test_opening_a_run_appends_a_divider_and_focuses_the_sub_window(
    tmp_path: Path,
) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)

        await main.open_session_view(SUB_ONE)
        await pilot.pause()

        assert any("── task: read the docs ──" in e for e in _entries(main, SUBAGENT_WINDOW))
        assert _label(main, SUBAGENT_WINDOW).startswith("▶ SUB-AGENT")
        # Shown, focused AND pointed at: the controller writes here next, the
        # user sees it, and the sidebar now configures the sub-agent's window.
        assert main.chat_tabs.selected == SUBAGENT_WINDOW
        assert main._calibrating is AgentSlot.SUBAGENT
        assert main._focused_panel == SUBAGENT_WINDOW
        assert main.transcript is main._panels[SUBAGENT_WINDOW]
        assert main._live is AgentSlot.MASTER  # ...and the automation did NOT move


async def test_output_after_focusing_lands_only_in_that_window(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.add_note("master line")
        await main.open_session_view(SUB_ONE)
        await main.add_user("read every file under src/")
        await main.add_note("sub-agent line")
        await pilot.pause()

        assert any("sub-agent line" in e for e in _entries(main, SUBAGENT_WINDOW))
        assert any(
            "you: read every file under src/" in e for e in _entries(main, SUBAGENT_WINDOW)
        )
        # The master's transcript is untouched by the sub-agent's output.
        assert any("master line" in e for e in _entries(main, MASTER_WINDOW))
        assert not any("sub-agent line" in e for e in _entries(main, MASTER_WINDOW))


async def test_a_second_run_appends_under_a_new_divider(tmp_path: Path) -> None:
    """The sub-agent WINDOW's transcript is one scroll of everything that ever
    ran in it - the divider is what says where one sub-task ended."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)

        await main.open_session_view(SUB_ONE)
        await main.add_note("first run output")
        await main.finish_session_view("sub-1", "run one ended", True)
        await main.open_session_view(SUB_TWO)
        await main.add_note("second run output")
        await pilot.pause()

        entries = _entries(main, SUBAGENT_WINDOW)
        assert [e for e in entries if e.startswith("── task:")] == [
            "── task: read the docs ──",
            "── task: survey the tests ──",
        ]
        assert any("first run output" in e for e in entries)  # nothing was cleared
        assert any("second run output" in e for e in entries)
        assert _label(main, SUBAGENT_WINDOW).startswith("▶ ")  # run two is in flight


async def test_finishing_rebadges_the_tab_and_leaves_the_transcript(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_note("did the work")

        await main.finish_session_view("sub-1", "sub-agent finished - result handed back", True)
        await pilot.pause()

        service = main._service_for(AgentSlot.SUBAGENT)
        assert _label(main, SUBAGENT_WINDOW) == f"✓ SUB-AGENT · {service}"
        entries = _entries(main, SUBAGENT_WINDOW)
        assert any("sub-agent finished" in e for e in entries)
        assert any("did the work" in e for e in entries)  # nothing was cleared
        assert main._panels[SUBAGENT_WINDOW].is_mounted  # nor unmounted


async def test_a_failed_run_gets_the_failure_glyph(tmp_path: Path) -> None:
    """A refused delegation ends through the same call as a delivered one, so
    the outcome has to travel with it. Badging the tab ✓ over a run that handed
    nothing back - under a note claiming a result HAD been handed back - is the
    one reading of that tab a user would take at face value."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_error("could not open a fresh chat for the sub-agent")

        await main.finish_session_view("sub-1", "sub-agent run ended WITHOUT a result", False)
        await pilot.pause()

        service = main._service_for(AgentSlot.SUBAGENT)
        assert _label(main, SUBAGENT_WINDOW) == f"✗ SUB-AGENT · {service}"

        # ...and the next run that DOES deliver takes the tab back.
        await main.open_session_view(SUB_TWO)
        await pilot.pause()
        assert _label(main, SUBAGENT_WINDOW).startswith("▶ ")
        await main.finish_session_view("sub-2", "result handed back", True)
        await pilot.pause()
        assert _label(main, SUBAGENT_WINDOW) == f"✓ SUB-AGENT · {service}"


async def test_focusing_the_master_reroutes_output_back(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_note("inside the sub-run")

        main.focus_session_view("master")
        await main.add_note("back on the master")
        await pilot.pause()

        assert main.chat_tabs.selected == MASTER_WINDOW
        assert any("back on the master" in e for e in _entries(main, MASTER_WINDOW))
        assert not any("back on the master" in e for e in _entries(main, SUBAGENT_WINDOW))
        # ...and the sub-agent's own transcript is still intact behind its tab.
        assert any("inside the sub-run" in e for e in _entries(main, SUBAGENT_WINDOW))


async def test_clicking_a_tab_does_not_redirect_the_controller(tmp_path: Path) -> None:
    """The whole reason ``_focused_panel`` exists: the user reading the master
    tab mid-delegation must not divert the sub-agent's output into it."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)
        await pilot.pause()

        master_tab = main.chat_tabs.tab(MASTER_WINDOW)
        await _wait_for(pilot, lambda: master_tab.region.width > 0, "master tab laid out")
        await pilot.click(master_tab)
        await _wait_for(
            pilot, lambda: main.chat_tabs.selected == MASTER_WINDOW, "master tab shown"
        )

        # The user is LOOKING at the master (and the sidebar now configures it);
        # the controller is still writing to the sub-agent's window.
        assert main._calibrating is AgentSlot.MASTER
        assert main._focused_panel == SUBAGENT_WINDOW
        await main.add_note("still the sub-agent talking")
        await pilot.pause()
        assert any("still the sub-agent" in e for e in _entries(main, SUBAGENT_WINDOW))
        assert not any("still the sub-agent" in e for e in _entries(main, MASTER_WINDOW))


async def test_clicking_a_tab_never_moves_the_live_window(tmp_path: Path) -> None:
    """Looking at a window is not driving it. A click here while a sub-agent is
    mid-run must not send the next paste into the master's chat."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        main._live = AgentSlot.SUBAGENT  # as a delegation would leave it

        sub_tab = main.chat_tabs.tab(SUBAGENT_WINDOW)
        await _wait_for(pilot, lambda: sub_tab.region.width > 0, "sub tab laid out")
        await pilot.click(sub_tab)
        await _wait_for(
            pilot, lambda: main.chat_tabs.selected == SUBAGENT_WINDOW, "sub tab shown"
        )
        assert main._live is AgentSlot.SUBAGENT

        master_tab = main.chat_tabs.tab(MASTER_WINDOW)
        await pilot.click(master_tab)
        await _wait_for(
            pilot, lambda: main.chat_tabs.selected == MASTER_WINDOW, "master tab shown"
        )
        assert main._live is AgentSlot.SUBAGENT


async def test_f6_cycles_the_selected_tab_without_moving_the_focus(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.open_session_view(SUB_ONE)  # the controller focuses the sub window
        await pilot.pause()
        assert main.chat_tabs.selected == SUBAGENT_WINDOW

        await pilot.press("f6")  # wraps back to the master
        await pilot.pause()
        assert main.chat_tabs.selected == MASTER_WINDOW
        assert main._calibrating is AgentSlot.MASTER
        await pilot.press("f6")
        await pilot.pause()
        assert main.chat_tabs.selected == SUBAGENT_WINDOW
        assert main._calibrating is AgentSlot.SUBAGENT

        # Browsing only: output still goes to the window the controller focused,
        # and the automation is still driving whatever it was driving.
        assert main._focused_panel == SUBAGENT_WINDOW
        assert main._live is AgentSlot.MASTER
        await pilot.press("f6")
        await main.add_note("from the sub-agent")
        await pilot.pause()
        assert any("from the sub-agent" in e for e in _entries(main, SUBAGENT_WINDOW))


async def test_render_log_carries_every_run_under_its_own_heading(tmp_path: Path) -> None:
    """One panel, many runs - so the export slices it back apart. Five sub-tasks
    under one heading is a wall; each under its own title is the readable thing
    an export is for."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _ready(app, pilot)
        await main.add_user("tidy up the repo")

        await main.open_session_view(SUB_ONE)
        await main.add_note("scanned 12 files")
        await main.finish_session_view("sub-1", "run one ended", True)
        main.focus_session_view("master")
        await main.add_note("delegation returned")

        await main.open_session_view(SUB_TWO)
        await main.add_note("counted the tests")
        main.focus_session_view("master")
        await pilot.pause()

        assert main.has_transcript_events()
        text = main.render_log(["Project: demo"])
        assert text.startswith("# AgentClip chat log")
        assert "Project: demo" in text
        assert "tidy up the repo" in text  # the master's transcript
        assert "delegation returned" in text
        # ...followed by ONE heading per run, in the order they ran.
        first = text.index("## sub-agent: read the docs (jade-otter)")
        second = text.index("## sub-agent: survey the tests (teal-moth)")
        assert text.index("delegation returned") < first < second
        # Each heading carries its own run's events and not the other's.
        assert "scanned 12 files" in text[first:second]
        assert "counted the tests" not in text[first:second]
        assert "counted the tests" in text[second:]


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


async def test_new_clears_both_transcripts_and_keeps_both_tabs(tmp_path: Path) -> None:
    """/new is the session teardown. The windows are not session state - the
    browser is still open and still drawn - so only their transcripts go."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        await main.open_session_view(SUB_ONE)
        await main.add_note("sub-agent output")
        await main.finish_session_view("sub-1", "run one ended", True)
        await pilot.pause()
        assert _label(main, SUBAGENT_WINDOW).startswith("✓ ")

        await send_composer(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "inline start flow re-armed")
        await pilot.pause()

        assert [tab.window for tab in main.chat_tabs.query(WindowTab)] == [
            MASTER_WINDOW,
            SUBAGENT_WINDOW,
        ]
        assert main.chat_tabs.selected == MASTER_WINDOW
        assert main._focused_panel == MASTER_WINDOW
        assert not _entries(main, MASTER_WINDOW)
        assert not _entries(main, SUBAGENT_WINDOW)
        assert not main.has_transcript_events()
        assert main._sub_runs == []
        # ...and the tab is back to "never ran".
        assert _label(main, SUBAGENT_WINDOW).startswith("SUB-AGENT")

        # A fresh session reuses the run id, and starts a fresh divider for it.
        await main.open_session_view(SUB_ONE)
        await pilot.pause()
        assert _entries(main, SUBAGENT_WINDOW) == ["── task: read the docs ──"]


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
