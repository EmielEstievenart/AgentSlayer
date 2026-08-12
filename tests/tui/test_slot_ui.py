"""Pilot tests for what selecting a window tab does to the sidebar.

Selecting a tab is what the AGENT SLOT picker used to be: it points the
sidebar's "Set chat region..." button AND its service picker at that window, and
repaints every readout from that window's state. MASTER is the chat the session
runs in, SUBAGENT the second window a delegated sub-agent gets, and choosing
between them does NOT change which window the automation drives (that is
``start_browser_chat``'s job, tested in test_subagent_slot_ui.py).

Two things are per window and pull in opposite directions, which is what most of
this file is about. The drawn rectangle is obviously per window. The **service**
is too - a big-context chat for the conversation you steer, something cheap and
fast for delegated sub-tasks - so what a window's appearances are, whether
delegation is ready, and which preset a session is bootstrapped from all depend
on which tab is selected. What is NOT per window is a service's captures: two
tabs on the same service share them, which is what makes the second window cost
one drag.

Appearances are seeded straight into the profile store here, exactly as a
capture would leave them: how they get there is the editor's story
(test_profile_capture_ui.py), and none of these tests are about it.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import (
    MISSING_CHAT_REGION,
    MISSING_COPY,
    MISSING_NEWCHAT,
    AgentSlot,
)
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MASTER_WINDOW, SUBAGENT_WINDOW, MainScreen
from agentclip.tui.widgets.sidebar import SLOT_NOTE_MASTER, SLOT_NOTE_READY

from .conftest import send_composer

MASTER_REGION = ScreenRegion(10, 20, 300, 400)
SUB_REGION = ScreenRegion(900, 20, 300, 400)
# Tall enough that every sidebar button is on screen: Pilot refuses to click a
# widget outside the visible region.
SIZE = (110, 100)

# Which window tab each slot lives on. Selecting the tab is what points the
# sidebar at a slot now; the mapping is MainScreen's seam for an N-window bar.
WINDOW_OF = {AgentSlot.MASTER: MASTER_WINDOW, AgentSlot.SUBAGENT: SUBAGENT_WINDOW}


def _frame(region: ScreenRegion) -> RegionImage:
    """Pixels that differ per region, so a window's snapshots are distinguishable."""
    fill = bytes([region.left % 251])
    return RegionImage(region.width, region.height, fill * (region.width * region.height * 4))


class _Picker:
    """Stand-in for the tkinter overlay: hands back whatever region is armed."""

    def __init__(self) -> None:
        self.region: ScreenRegion | None = None
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> ScreenRegion | None:
        self.prompts.append(prompt)
        return self.region


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _service_key(app: AgentClipApp) -> str:
    """The service both tabs start on - the one a seeded appearance has to be
    filed under for this app to load it."""
    config = app.app_config
    configured = config.general.service
    return configured if configured in config.services else next(iter(sorted(config.services)))


def _other_service(app: AgentClipApp) -> str:
    """Any preset that is not the default - what a second window gets pointed at."""
    return next(key for key in sorted(app.app_config.services) if key != _service_key(app))


def _make_app(tmp_path: Path, profile_root: Path) -> AgentClipApp:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    app = AgentClipApp(
        config=config,
        provider=FakeClipboard(),
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        profile_root=profile_root,
    )
    return app


def _patch_screen(monkeypatch: pytest.MonkeyPatch) -> _Picker:
    picker = _Picker()
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: True)
    # No live poller: nothing here is about the finish detectors, and its stale
    # readout rewrites a wrapping line in the sidebar on its own schedule -
    # which reflows every button below it, so a probe landing between a click's
    # mouse-down and mouse-up moves the button out from under the pointer and
    # the press is silently lost. (The poller itself is test_stale_detector_ui's.)
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)

    async def fake_find_all(
        self: MainScreen,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        """Stand-in for the in-region appearance search: every appearance the
        window's OWN service has captured is "found" exactly once, filling that
        window - which is what makes a shared capture resolve to two different
        rectangles, and an unshared one to none at all."""
        target = slot if slot is not None else self._live
        cal = self._slots[target]
        if cal.chat_region is None or not self._profile_for(target).has(kind):
            return []
        return [cal.chat_region]

    monkeypatch.setattr(MainScreen, "_find_all", fake_find_all)
    return picker


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    # ...and for its press animation to be over. Textual's Button ignores a
    # click outright while the "-active" class is still on it, so two presses of
    # the SAME button close together silently become one - which is exactly the
    # shape of this suite (draw the master's window, switch tab, draw the
    # sub-agent's) and reads as a click that vanished.
    await _wait_for(pilot, lambda: not button.has_class("-active"), f"{button_id} idle again")
    await pilot.click(button_id)


async def _select_slot(app: AgentClipApp, pilot: Pilot, slot: AgentSlot) -> None:
    """Select that window's tab - the screen's own funnel, which is what the
    click handler calls (the click path itself is test_tabs_ui's)."""
    main = app.main_screen
    assert main is not None
    main._select_window(WINDOW_OF[slot])
    await _wait_for(pilot, lambda: main._calibrating is slot, f"{slot} selected")
    await pilot.pause()


async def _pick_service(app: AgentClipApp, pilot: Pilot, key: str) -> None:
    """Point the SELECTED tab at another service, through the real picker."""
    main = app.main_screen
    assert main is not None
    main.sidebar.service_select.value = key
    await _wait_for(pilot, lambda: main._selected_service() == key, f"{key} selected")


async def _calibrate(
    app: AgentClipApp, pilot: Pilot, picker: _Picker, button_id: str, region: ScreenRegion
) -> None:
    picker.region = region
    before = len(picker.prompts)
    await _press(app, pilot, button_id)
    await _wait_for(pilot, lambda: len(picker.prompts) > before, f"{button_id} picker ran")
    # ...and then for the one-overlay-at-a-time guard to be released. The picker
    # returns from inside the worker, so ``_picker_open`` is still held for a
    # beat afterwards - and a press landing in that beat is REFUSED, not queued.
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: not main._picker_open, "the picker guard released")
    await pilot.pause()


async def _editor_visit(app: AgentClipApp, pilot: Pilot) -> None:
    """What coming back from the service editor does to this screen.

    Capturing an appearance is the editor's job now, so a mid-run capture
    reaches the sidebar exactly this way: the per-run profile cache is dropped
    and everything downstream of "what does this service look like?" repaints -
    the appearance summary, the window readiness note, the detectors.
    """
    main = app.main_screen
    assert main is not None
    main.update_config(app.app_config)
    await pilot.pause()


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Send a composer line - see ``send_composer`` for why /new takes two Enters."""
    await send_composer(app, pilot, text)


async def test_the_two_windows_are_independent(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drawing the sub-agent window must not disturb the master's, and the
    legacy attribute name keeps meaning "the master window"."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main._calibrating is AgentSlot.MASTER
        assert main._live is AgentSlot.MASTER

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)

        assert main._slots[AgentSlot.MASTER].chat_region == MASTER_REGION
        assert main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION
        assert main._chat_region == MASTER_REGION  # the compatibility proxy
        assert main._live is AgentSlot.MASTER  # selecting a tab never retargets


async def test_switching_tabs_repaints_the_window_but_not_a_shared_appearance(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Two tabs on ONE service: the CHAT WINDOW block changes with the tab and
    the appearance summary does not - that sharing is what makes the second
    window cost one drag."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.NEW_CHAT, TemplateKind.COPY)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        assert MASTER_REGION.describe() in _label(app, "#side-region")
        assert "2/7 captured" in _label(app, "#side-profile-note")

        # The sub-agent window has no rectangle drawn, so the per-window row
        # reads as unset - but both tabs are on the same service, whose captures
        # stay put.
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        assert "not set" in _label(app, "#side-region")
        assert "2/7 captured" in _label(app, "#side-profile-note")
        assert main._active_profile().has(TemplateKind.COPY)

        # ...and switching back restores the master's window, from stored state.
        await _select_slot(app, pilot, AgentSlot.MASTER)
        assert MASTER_REGION.describe() in _label(app, "#side-region")
        assert "2/7 captured" in _label(app, "#side-profile-note")
        assert main._active_profile().has(TemplateKind.NEW_CHAT)
        assert _label(app, "#side-slot-note") == SLOT_NOTE_MASTER


async def test_each_tab_carries_its_own_service(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker edits the SELECTED tab and nothing else, and switching tabs
    puts each window's own key back in it."""
    _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    default, other = _service_key(app), _other_service(app)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert main.sidebar.service == default  # both windows start together

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _pick_service(app, pilot, other)

        assert main._service_for(AgentSlot.SUBAGENT) == other
        assert main._service_for(AgentSlot.MASTER) == default  # untouched
        assert other in main.chat_tabs.tab(SUBAGENT_WINDOW).label_text

        # Back to the master tab: the picker shows the master's key again, and
        # doing so must NOT re-report a service switch onto the master window.
        await _select_slot(app, pilot, AgentSlot.MASTER)
        assert main.sidebar.service == default
        assert main._service_for(AgentSlot.SUBAGENT) == other
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        assert main.sidebar.service == other


async def test_picking_a_service_is_remembered_for_the_next_launch(
    tmp_path: Path,
    profile_root: Path,
    default_global_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching in the sidebar writes through to the global config.toml, so a
    restart comes up where the user left off rather than on the seeded default.

    ``default_global_config`` is the tmp file this app resolves to: ``_make_app``
    injects no path, so it takes ``default_global_config_path()`` - which the
    suite's autouse gate has already pointed here.
    """
    _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    default, other = _service_key(app), _other_service(app)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        # Nothing has been picked yet: merely starting up must not write.
        assert not default_global_config.exists()

        await _pick_service(app, pilot, other)
        await pilot.pause()

        # The picker edits the SELECTED tab only, so the two windows have just
        # parted company - and the file has to say so, or the sub-agent window
        # would come back somewhere it was never pointed.
        raw = tomllib.loads(default_global_config.read_text(encoding="utf-8"))
        assert raw["general"]["service"] == other
        assert raw["general"]["subagent_service"] == default
        reloaded = load_config(app.project_root, global_config_path=default_global_config)
        assert MainScreen._initial_services(reloaded) == {
            MASTER_WINDOW: other,
            SUBAGENT_WINDOW: default,
        }

        # Bringing the sub-agent window back onto the master's service drops the
        # pin again: agreeing is the blank case, not a setting.
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _pick_service(app, pilot, other)
        await pilot.pause()

        raw = tomllib.loads(default_global_config.read_text(encoding="utf-8"))
        assert raw["general"]["service"] == other
        assert "subagent_service" not in raw["general"]

    # And a fresh load of that file starts both windows where they were left.
    reloaded = load_config(app.project_root, global_config_path=default_global_config)
    assert MainScreen._initial_services(reloaded) == {
        MASTER_WINDOW: other,
        SUBAGENT_WINDOW: other,
    }


async def test_a_failed_remember_warns_instead_of_killing_the_switch(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remembering the pick is a convenience: an unwritable config must not take
    the switch (or the app) down with it."""
    _patch_screen(monkeypatch)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only config dir")

    monkeypatch.setattr(main_mod, "save_active_services", boom)
    app = _make_app(tmp_path, profile_root)
    other = _other_service(app)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _pick_service(app, pilot, other)
        await pilot.pause()

        assert main._service_for(AgentSlot.MASTER) == other  # the switch stuck
        assert app.is_running


async def test_a_second_service_has_its_own_appearances_and_readiness(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Readiness is composed from the SUB tab's window and the SUB tab's
    service. Captures under the master's service say nothing about a sub-agent
    window pointed somewhere else - and delegation must read as off until the
    chat it will actually open is calibrated."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    default, other = _service_key(app), _other_service(app)
    seed_templates(default, TemplateKind.NEW_CHAT, TemplateKind.COPY)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        await _wait_for(pilot, lambda: main.delegation_available(), "delegation ready")

        # Point the sub-agent window at a service with nothing captured: the
        # buttons the run would click are that service's, and it has none.
        await _pick_service(app, pilot, other)
        assert "0/7 captured" in _label(app, "#side-profile-note")
        note = _label(app, "#side-slot-note")
        assert MISSING_COPY in note and MISSING_NEWCHAT in note
        assert not main.delegation_available()

        # Seeding THAT service's captures is what turns it back on.
        seed_templates(other, TemplateKind.NEW_CHAT, TemplateKind.COPY)
        await _editor_visit(app, pilot)
        await _wait_for(
            pilot,
            lambda: _label(app, "#side-slot-note") == SLOT_NOTE_READY,
            "delegation reported ready again",
        )
        assert main.delegation_available()
        # The master tab's own appearance summary is unchanged by any of it.
        await _select_slot(app, pilot, AgentSlot.MASTER)
        assert "2/7 captured" in _label(app, "#side-profile-note")


async def test_the_note_reports_what_delegation_is_still_missing(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    key = _service_key(app)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        note = _label(app, "#side-slot-note")
        for gap in (MISSING_CHAT_REGION, MISSING_COPY, MISSING_NEWCHAT):
            assert gap in note

        # The window has to be drawn first: an appearance with nowhere to be
        # searched for is not yet a usable piece of the window.
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        assert MISSING_CHAT_REGION not in _label(app, "#side-slot-note")

        seed_templates(key, TemplateKind.NEW_CHAT)
        await _editor_visit(app, pilot)
        assert MISSING_NEWCHAT not in _label(app, "#side-slot-note")

        # A capture is a profile change, not a window one - the ready-toast seam
        # has to notice it too, or delegation would silently stay "off".
        seed_templates(key, TemplateKind.COPY)
        await _editor_visit(app, pilot)
        await _wait_for(
            pilot,
            lambda: _label(app, "#side-slot-note") == SLOT_NOTE_READY,
            "delegation reported ready",
        )
        assert main.delegation_available()


async def test_subagent_prompts_name_the_window_being_drawn_on(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both windows share the picker code, so the prompt is the only thing that
    tells the user which browser window to point at.

    Only the per-window picker says it: capturing an appearance is a question
    about the SERVICE, and the answer is the same whichever window it is drawn
    in - a window prefix there would be a lie."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        assert "SUB-AGENT window" not in picker.prompts[-1]

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        # The chat region is the ONLY per-window picker left, so it is the only
        # prompt that can name a window - every other button asks about the
        # service, whose answer is the same in either one.
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        assert "SUB-AGENT window" in picker.prompts[-1]
        # The appearance captures ask in words that come from TemplateKind, in
        # the service editor, which knows nothing about windows - so none of them
        # can name one even by accident.
        assert all("SUB-AGENT" not in kind.prompt for kind in TemplateKind)


async def test_the_tab_bar_is_never_locked_but_the_service_picker_is(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that wants to delegate has to be able to calibrate the
    sub-agent window after it started - but not to re-point either window at
    another service, because both were baked into the bootstrap (the master's
    budgets, the sub-agent's delegate-catalog readiness)."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main.sidebar.service_select.disabled

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        assert main.sidebar.service_select.disabled
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        assert main.sidebar.service_select.disabled  # locked on BOTH tabs

        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        assert main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION


async def test_the_session_spec_carries_a_service_per_window(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The master tab's preset bootstraps the conversation; the sub-agent tab's
    is what any delegation will run on. Both leave the view once, in the spec,
    because both are frozen for the session's life."""
    _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    default, other = _service_key(app), _other_service(app)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _pick_service(app, pilot, other)
        # Deliberately left on the SUB-AGENT tab: the master's bootstrap must
        # come from the master's tab, not from whatever the user is looking at.
        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        assert main._controller._stats.service == default
        assert main._controller._subagent_service == other


async def test_new_keeps_both_windows_and_sends_the_pointers_home(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
    new_chat_click_lands: None,
) -> None:
    """/new is a session teardown, not a recalibration: the service's windows
    have not moved, so both calibrations (and both services) survive and only
    the pointers - the selected tab and the live window - go home to MASTER."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    default, other = _service_key(app), _other_service(app)
    seed_templates(default, TemplateKind.NEW_CHAT)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        await _pick_service(app, pilot, other)
        main._live = AgentSlot.SUBAGENT  # as a delegation would leave it

        await _send(app, pilot, "Say hello.")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        await _send(app, pilot, "/new")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "new session prompt re-armed")

        assert main._slots[AgentSlot.MASTER].chat_region == MASTER_REGION
        assert main._slots[AgentSlot.SUBAGENT].chat_region == SUB_REGION
        assert main._service_for(AgentSlot.MASTER) == default
        assert main._service_for(AgentSlot.SUBAGENT) == other
        assert main._active_profile().has(TemplateKind.NEW_CHAT)
        assert main._calibrating is AgentSlot.MASTER
        assert main._live is AgentSlot.MASTER
        assert main.chat_tabs.selected == MASTER_WINDOW
        assert main.sidebar.service == default
        assert MASTER_REGION.describe() in _label(app, "#side-region")
        assert _label(app, "#side-slot-note") == SLOT_NOTE_MASTER


async def test_new_rederives_delegation_readiness_from_the_surviving_window(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """A sub-agent window calibrated to readiness stays ready across /new:
    ``_delegation_ready`` is re-derived from the surviving window instead of
    being zeroed, so the next session gets the delegate tool and the one-shot
    "window ready" toast has no False->True edge to re-fire on."""
    picker = _patch_screen(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    # Everything readiness asks of the SERVICE; the drag below is the only
    # thing left that belongs to the window.
    seed_templates(_service_key(app), TemplateKind.NEW_CHAT, TemplateKind.COPY)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)
        await _wait_for(pilot, lambda: main.delegation_available(), "sub-agent window ready")

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert main.delegation_available()
        assert main._delegation_ready is True
        assert main._calibrating is AgentSlot.MASTER


async def test_the_new_browser_chat_button_targets_the_selected_window(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The button is how the user *tests* a calibration, so it follows the tab
    bar - and it never moves the live window. The appearance is the selected
    tab's SERVICE's (captured once), but it is searched for in that tab's
    window, which is what sends the click to the sub-agent's."""
    picker = _patch_screen(monkeypatch)
    clicks: list[ScreenRegion] = []
    monkeypatch.setattr(
        main_mod, "click_region", lambda region, **kw: bool(clicks.append(region)) or True
    )
    app = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.NEW_CHAT)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_REGION)
        await _select_slot(app, pilot, AgentSlot.SUBAGENT)
        await _calibrate(app, pilot, picker, "#set-region-btn", SUB_REGION)

        await _press(app, pilot, "#newchat-btn")
        await _wait_for(pilot, lambda: clicks == [SUB_REGION], "the sub-agent's button clicked")
        assert main._live is AgentSlot.MASTER
