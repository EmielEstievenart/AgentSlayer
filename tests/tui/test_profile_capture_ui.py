"""Pilot tests for the appearance-capture buttons as one family.

Six appearances, one code path (``_capture_template``): draw a box, keep the
pixels, file them under the ACTIVE SERVICE and write them to disk. The buttons
differ only in which ``TemplateKind`` they pass, so the interesting properties
are the ones they share - and the one that justifies the whole model, which is
that a capture outlives the app run.

The picker and the GDI capture are monkeypatched at their use site
(agentclip.tui.screens.main); the autouse fixture in conftest.py keeps every
profile write inside tmp_path. The busy/idle detectors' live readouts and the
arm/fire machinery they drive live in test_finish_signal_ui.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import ProfileStoreError, load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp

BOX = ScreenRegion(200, 150, 64, 64)
SIZE = (110, 100)

# Every capture button, and the appearance it files. The sidebar ids are the
# contract these tests key on.
BUTTONS = {
    "#set-chatbox-initial-btn": TemplateKind.CHATBOX_INITIAL,
    "#set-chatbox-ongoing-btn": TemplateKind.CHATBOX_ONGOING,
    "#set-busy-btn": TemplateKind.BUSY,
    "#set-idle-btn": TemplateKind.IDLE,
    "#set-copy-btn": TemplateKind.COPY,
    "#set-newchat-btn": TemplateKind.NEW_CHAT,
}
STATUS_ID = {
    TemplateKind.CHATBOX_INITIAL: "#side-chatbox-initial",
    TemplateKind.CHATBOX_ONGOING: "#side-chatbox-ongoing",
    TemplateKind.BUSY: "#side-busy",
    TemplateKind.IDLE: "#side-idle",
    TemplateKind.COPY: "#side-copy",
    TemplateKind.NEW_CHAT: "#side-newchat",
}


def _frame(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


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


def _label(app: AgentClipApp, widget_id: str) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one(widget_id, Static).render())


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


def _patch_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: BOX)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.02)


class _Picker:
    """Records the prompt each button asks with."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> ScreenRegion:
        self.prompts.append(prompt)
        return BOX


# -- the shared capture path ----------------------------------------------------


@pytest.mark.parametrize(("button_id", "kind"), list(BUTTONS.items()))
async def test_every_capture_button_files_its_appearance_and_saves_it(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    button_id: str,
    kind: TemplateKind,
) -> None:
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert "not captured" in _label(app, STATUS_ID[kind])
        key = main._selected_service()

        await _press(app, pilot, button_id)
        await _wait_for(
            pilot, lambda: main._active_profile().has(kind), f"{kind.label} captured"
        )

        template = main._active_profile().get(kind)
        assert template is not None
        assert (template.width, template.height) == (BOX.width, BOX.height)
        assert "64×64 · captured" in _label(app, STATUS_ID[kind])
        # ...and on disk, under the selected service, ready for the next run.
        assert load_profile(profile_root, key).has(kind)


async def test_each_button_asks_for_its_own_appearance_in_its_own_words(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt comes from TemplateKind, not from the caller: what makes a
    good capture is a fact about the appearance and has to read identically
    wherever the user is asked for it - including the warning not to box an
    animated spinner."""
    picker = _Picker()
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        for button_id, kind in BUTTONS.items():
            before = len(picker.prompts)
            await _press(app, pilot, button_id)
            await _wait_for(
                pilot, lambda n=before: len(picker.prompts) > n, f"{button_id} picked"
            )
            assert picker.prompts[-1] == kind.prompt

        assert "avoid animated spinners" in TemplateKind.BUSY.prompt


async def test_captures_persist_across_a_restart(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of moving appearances off the slot: capture once, and
    every later run of the app starts already knowing what the service looks
    like. A second AgentClipApp over the same profile root proves it."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        for button_id, kind in (
            ("#set-busy-btn", TemplateKind.BUSY),
            ("#set-copy-btn", TemplateKind.COPY),
        ):
            await _press(app, pilot, button_id)
            await _wait_for(
                pilot, lambda k=kind: main._active_profile().has(k), f"{kind.label} captured"
            )

    again = _make_app(tmp_path, profile_root)
    async with again.run_test(size=SIZE) as pilot:
        main = again.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        profile = main._active_profile()
        assert profile.has(TemplateKind.BUSY)
        assert profile.has(TemplateKind.COPY)
        assert not profile.has(TemplateKind.IDLE)
        # ...and the sidebar says so without anyone pressing anything.
        assert "captured" in _label(again, "#side-busy")
        assert "not captured" in _label(again, "#side-idle")


async def test_a_save_failure_is_reported_but_the_capture_still_works(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the run as well as the file would be strictly worse: the template
    stays usable now, and the toast says it will not survive a restart."""
    _patch_picker(monkeypatch)
    notes: list[str] = []
    monkeypatch.setattr(
        main_mod.MainScreen, "notify", lambda self, message, *a, **kw: notes.append(str(message))
    )

    def boom(root: Path, key: str, kind: TemplateKind, image: RegionImage) -> None:
        raise ProfileStoreError("disk is full")

    monkeypatch.setattr(main_mod, "save_template", boom)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-busy-btn")
        await _wait_for(
            pilot, lambda: main._active_profile().has(TemplateKind.BUSY), "busy captured anyway"
        )
        assert any("not saved for next time" in note for note in notes)
        assert any("disk is full" in note for note in notes)
        assert "captured" in _label(app, "#side-busy")


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: None)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-busy-btn")
        await _press(app, pilot, "#set-idle-btn")
        await pilot.pause(0.2)
        assert main._active_profile().captured == ()
        assert "not captured" in _label(app, "#side-busy")
        assert "not captured" in _label(app, "#side-idle")


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "pick_region", boom)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-busy-btn")
        await pilot.pause(0.2)
        assert main._active_profile().captured == ()
        assert "not captured" in _label(app, "#side-busy")
        assert main._picker_open is False  # the guard released; the button still works


async def test_capture_failure_files_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: BOX)
    monkeypatch.setattr(main_mod, "capture_region", boom)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-busy-btn")
        await _press(app, pilot, "#set-idle-btn")
        await pilot.pause(0.2)
        assert main._active_profile().captured == ()
        assert main._detector_worker is None  # nothing to watch, nothing watching


async def test_an_unsearchable_box_is_refused(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box narrower than one anchor cannot be searched for, so filing it
    would only produce a template that never matches anything."""
    sliver = ScreenRegion(10, 10, 4, 40)
    monkeypatch.setattr(main_mod, "pick_region", lambda prompt=None: sliver)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-busy-btn")
        await pilot.pause(0.2)
        assert main._active_profile().captured == ()
        assert "not captured" in _label(app, "#side-busy")


# -- the captures are the SERVICE's, not a slot's --------------------------------


async def test_a_capture_is_shared_by_both_slots_and_survives_new(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appearances describe the service, so the slot picker does not change
    them and a session teardown does not clear them."""
    from agentclip.screen.slot import AgentSlot

    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")

        await _press(app, pilot, "#set-copy-btn")
        await _wait_for(
            pilot, lambda: main._active_profile().has(TemplateKind.COPY), "copy captured"
        )

        main.sidebar.slot_select.value = str(AgentSlot.SUBAGENT)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.SUBAGENT, "slot switched")
        await pilot.pause()
        assert "captured" in _label(app, "#side-copy")

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert main._active_profile().has(TemplateKind.COPY)
        assert "captured" in _label(app, "#side-copy")
