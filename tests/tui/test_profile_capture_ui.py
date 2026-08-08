"""Pilot tests for the appearance-capture buttons as one family.

Six appearances, one code path (``ServiceEditorScreen._capture_template``): draw
a box, anchor the pixels, write them to the profile store under the SERVICE the
editor has selected. The buttons differ only in which ``TemplateKind`` they
pass, so the interesting properties are the ones they share - plus the two that
justify the whole model: a capture outlives the app run, and it reaches the main
screen (cache, sidebar summary, detector poller) when the editor closes.

Captures live in the service editor rather than the sidebar because they are a
per-service *setting*, and this is the one suite that drives that UI end to end;
every other Pilot suite seeds the store directly through the ``seed_templates``
fixture. The picker and the GDI capture are monkeypatched at their use site
(agentclip.tui.screens.service_editor); the autouse fixture in conftest.py keeps
every profile write inside tmp_path. The busy/idle detectors' live readouts and
the arm/fire machinery they drive live in test_finish_signal_ui.py.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

import agentclip.tui.screens.service_editor as editor_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.picker import ScreenPickError
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import ProfileStoreError, load_profile
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.service_editor import (
    ServiceEditorScreen,
    capture_button_id,
    template_status_id,
)

BOX = ScreenRegion(200, 150, 64, 64)
SIZE = (120, 45)

# Every capture button, and the appearance it files. The editor ids are the
# contract these tests key on.
BUTTONS = {f"#{capture_button_id(kind)}": kind for kind in TemplateKind}
STATUS_ID = {kind: f"#{template_status_id(kind)}" for kind in TemplateKind}


def _frame(region: ScreenRegion) -> RegionImage:
    """A capture of ``region`` varied enough for Template.build to anchor on."""
    size = region.width * region.height * 4
    return RegionImage(region.width, region.height, (bytes(range(256)) * (size // 256 + 1))[:size])


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
        global_config_path=tmp_path / "config.toml",
        profile_root=profile_root,
    )
    return app


def _label(screen: object, widget_id: str) -> str:
    assert screen is not None
    return str(screen.query_one(widget_id, Static).render())  # type: ignore[attr-defined]


async def _open_editor(app: AgentClipApp, pilot: Pilot) -> ServiceEditorScreen:
    await pilot.press("f2")
    await _wait_for(pilot, lambda: isinstance(app.screen, ServiceEditorScreen), "editor opened")
    editor = app.screen
    assert isinstance(editor, ServiceEditorScreen)
    return editor


async def _ready(app: AgentClipApp, pilot: Pilot) -> ServiceEditorScreen:
    """The common preamble: app armed for a task, editor open on the default service."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return await _open_editor(app, pilot)


async def _press(editor: ServiceEditorScreen, pilot: Pilot, button_id: str) -> None:
    button = editor.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "capture button laid out")
    await pilot.click(button_id)


def _patch_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor_mod, "pick_region", lambda prompt=None: BOX)
    monkeypatch.setattr(editor_mod, "capture_region", _frame)


class _Picker:
    """Records the prompt each button asks with."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> ScreenRegion:
        self.prompts.append(prompt)
        return BOX


def _notes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        editor_mod.ServiceEditorScreen,
        "notify",
        lambda self, message, *a, **kw: seen.append(str(message)),
    )
    return seen


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
        editor = await _ready(app, pilot)
        assert "not captured" in _label(editor, STATUS_ID[kind])
        key = editor._selected_key
        assert key is not None

        await _press(editor, pilot, button_id)
        await _wait_for(
            pilot,
            lambda: load_profile(profile_root, key).has(kind),
            f"{kind.label} captured",
        )

        # On disk, under the selected service, ready for the next run...
        template = load_profile(profile_root, key).get(kind)
        assert template is not None
        assert (template.width, template.height) == (BOX.width, BOX.height)
        # ...and said so in the editor's own readout.
        await _wait_for(
            pilot,
            lambda: "64×64 · captured" in _label(editor, STATUS_ID[kind]),
            "the readout refreshed",
        )
        assert editor._profiles_changed


async def test_each_button_asks_for_its_own_appearance_in_its_own_words(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt comes from TemplateKind, not from the caller: what makes a
    good capture is a fact about the appearance and has to read identically
    wherever the user is asked for it - including the warning not to box an
    animated spinner."""
    picker = _Picker()
    monkeypatch.setattr(editor_mod, "pick_region", picker)
    monkeypatch.setattr(editor_mod, "capture_region", _frame)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)

        for button_id, kind in BUTTONS.items():
            before = len(picker.prompts)
            await _press(editor, pilot, button_id)
            await _wait_for(
                pilot, lambda n=before: len(picker.prompts) > n, f"{button_id} picked"
            )
            assert picker.prompts[-1] == kind.prompt

        assert "avoid animated spinners" in TemplateKind.BUSY.prompt


async def test_one_handler_serves_every_capture_button(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block is generated per TemplateKind and the kind is parsed back out
    of the pressed button's id, so a seventh appearance is an enum member and
    nothing else. Pressing all six in a row is the proof."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)
        key = editor._selected_key
        assert key is not None

        for button_id, kind in BUTTONS.items():
            await _press(editor, pilot, button_id)
            await _wait_for(
                pilot,
                lambda k=kind: load_profile(profile_root, key).has(k),
                f"{kind.label} captured",
            )

        assert load_profile(profile_root, key).captured == tuple(TemplateKind)
        await _wait_for(
            pilot,
            lambda: "6/6 captured" in _label(editor, "#svc-templates"),
            "the summary line repainted",
        )
        # ...and the whole set can now be forgotten again.
        assert editor.query_one("#svc-forget-templates-btn", Button).display


async def test_captures_persist_across_a_restart(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of moving appearances off the slot: capture once, and
    every later run of the app starts already knowing what the service looks
    like. A second AgentClipApp over the same profile root proves it."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)
        key = editor._selected_key
        assert key is not None
        for button_id, kind in (
            (f"#{capture_button_id(TemplateKind.BUSY)}", TemplateKind.BUSY),
            (f"#{capture_button_id(TemplateKind.COPY)}", TemplateKind.COPY),
        ):
            await _press(editor, pilot, button_id)
            await _wait_for(
                pilot,
                lambda k=kind: load_profile(profile_root, key).has(k),
                f"{kind.label} captured",
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
        assert "2/6 captured" in _label(main, "#side-profile-note")


# -- everything that can go wrong ------------------------------------------------


async def test_a_save_failure_files_nothing_and_says_so(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor keeps no profile of its own - the store IS the working copy -
    so a write that did not happen is a capture that did not happen, and the
    close must not claim otherwise."""
    _patch_picker(monkeypatch)
    notes = _notes(monkeypatch)

    def boom(root: Path, key: str, kind: TemplateKind, image: RegionImage) -> None:
        raise ProfileStoreError("disk is full")

    monkeypatch.setattr(editor_mod, "save_template", boom)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(pilot, lambda: any("disk is full" in note for note in notes), "reported")
        assert any("could not save the busy indicator" in note for note in notes)
        assert not editor._profiles_changed
        assert "not captured" in _label(editor, STATUS_ID[TemplateKind.BUSY])


async def test_cancelled_pick_changes_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(editor_mod, "pick_region", lambda prompt=None: None)
    monkeypatch.setattr(editor_mod, "capture_region", _frame)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.IDLE)}")
        await pilot.pause(0.2)
        assert editor._profiles_changed is False
        assert "not captured" in _label(editor, STATUS_ID[TemplateKind.BUSY])
        assert "not captured" in _label(editor, STATUS_ID[TemplateKind.IDLE])


async def test_picker_failure_is_reported_not_fatal(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(prompt: str | None = None) -> ScreenRegion:
        raise ScreenPickError("region picker unavailable: no tkinter")

    monkeypatch.setattr(editor_mod, "pick_region", boom)
    monkeypatch.setattr(editor_mod, "capture_region", _frame)
    notes = _notes(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(pilot, lambda: any("no tkinter" in note for note in notes), "reported")
        assert "not captured" in _label(editor, STATUS_ID[TemplateKind.BUSY])
        assert editor._capturing is False  # the guard released; the button still works


async def test_capture_failure_files_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("screen capture is not implemented yet")

    monkeypatch.setattr(editor_mod, "pick_region", lambda prompt=None: BOX)
    monkeypatch.setattr(editor_mod, "capture_region", boom)
    notes = _notes(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)
        key = editor._selected_key
        assert key is not None

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(pilot, lambda: any("not implemented" in note for note in notes), "reported")
        assert not load_profile(profile_root, key).captured
        assert editor._profiles_changed is False


async def test_an_unsearchable_box_is_refused(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box narrower than one anchor cannot be searched for, so filing it
    would only produce a template that never matches anything - and it is
    refused BEFORE anything reaches disk."""
    sliver = ScreenRegion(10, 10, 4, 40)
    monkeypatch.setattr(editor_mod, "pick_region", lambda prompt=None: sliver)
    monkeypatch.setattr(editor_mod, "capture_region", _frame)
    notes = _notes(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)
        key = editor._selected_key
        assert key is not None

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(
            pilot, lambda: any("cannot be searched for" in note for note in notes), "refused"
        )
        assert not load_profile(profile_root, key).captured
        assert "not captured" in _label(editor, STATUS_ID[TemplateKind.BUSY])


# -- one overlay at a time -------------------------------------------------------


async def test_a_second_capture_press_is_refused_while_one_is_in_flight(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the worker cannot kill the blocking child overlay process it
    spawned, so the only safe guard against stacked fullscreen overlays is
    refusing the press - and refusing escape with it, since closing out from
    under an in-flight capture strands the worker that still has to save."""
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    picks: list[str] = []

    def blocking_pick(prompt: str = "") -> ScreenRegion:
        picks.append(prompt)
        asyncio.run_coroutine_threadsafe(_wait(release), loop).result()
        return BOX

    async def _wait(event: asyncio.Event) -> None:
        await event.wait()

    monkeypatch.setattr(editor_mod, "pick_region", blocking_pick)
    monkeypatch.setattr(editor_mod, "capture_region", _frame)
    notes = _notes(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)
        key = editor._selected_key
        assert key is not None

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(pilot, lambda: editor._capturing, "the first pick is up")

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.IDLE)}")
        await pilot.press("escape")
        await pilot.pause()
        assert len(picks) == 1  # the second press never reached the overlay
        assert app.screen is editor  # ...and escape did not close it either
        assert any("already open" in note for note in notes)
        assert any("finish it or cancel it first" in note for note in notes)

        release.set()
        await _wait_for(
            pilot,
            lambda: load_profile(profile_root, key).has(TemplateKind.BUSY),
            "the first capture finished",
        )
        assert not load_profile(profile_root, key).has(TemplateKind.IDLE)


# -- nothing to file it under yet ------------------------------------------------


async def test_the_add_new_sentinel_has_nothing_to_capture_into(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service that has not been created has no key, so there is nowhere to
    put a PNG or a checklist - the controls go inert rather than vanish, so the
    column does not reflow while the form is being filled in."""
    from textual.widgets import Checkbox, Input, Select

    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        editor = await _ready(app, pilot)
        assert not editor.query_one(f"#{capture_button_id(TemplateKind.BUSY)}", Button).disabled

        editor.query_one("#svc-select", Select).value = "+add-new+"
        await pilot.pause()
        assert all(button.disabled for button in editor.query(f".{editor_mod.CAPTURE_CLASS}"))
        assert all(box.disabled for box in editor.query(Checkbox))

        # ...and they come alive on the add.
        editor.query_one("#svc-key", Input).value = "my-llm"
        await pilot.pause()
        editor.query_one("#svc-label", Input).value = "My LLM"
        await pilot.pause()
        editor.query_one("#svc-max", Input).value = "8000"
        await pilot.pause()
        editor.query_one("#svc-total", Input).value = "300000"
        await pilot.pause()
        await pilot.click("#svc-add-btn")
        await pilot.pause()

        assert not editor.query_one(f"#{capture_button_id(TemplateKind.COPY)}", Button).disabled
        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.COPY)}")
        await _wait_for(
            pilot,
            lambda: load_profile(profile_root, "my-llm").has(TemplateKind.COPY),
            "filed under the new key",
        )


# -- the capture reaches the main screen -----------------------------------------


async def test_closing_the_editor_propagates_the_capture_to_the_main_screen(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor writes the PNGs; the main screen is what caches them, paints
    a summary of them and hunts for them. Without the close reaching it, the
    sidebar would keep saying 0/6 and the poller would keep ignoring a template
    that is now on disk."""
    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        assert not main._active_profile().captured
        assert "0/6 captured" in _label(main, "#side-profile-note")

        editor = await _open_editor(app, pilot)
        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.COPY)}")
        await _wait_for(pilot, lambda: editor._profiles_changed, "copy captured")

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert main._active_profile().has(TemplateKind.COPY)  # the cache was dropped
        assert "1/6 captured" in _label(main, "#side-profile-note")


async def test_a_busy_capture_restarts_the_detector_poller(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticked busy signal with nothing captured runs nothing at all, so the
    capture is what turns the detector on - and it only takes effect because
    the close rebuilds the poller."""
    _patch_picker(monkeypatch)
    monkeypatch.setattr("agentclip.tui.screens.main._BUSY_POLL_S", 0.02)
    app = _make_app(tmp_path, profile_root)
    key = app.app_config.general.service
    app.app_config = replace(
        app.app_config,
        services={
            **app.app_config.services,
            key: replace(app.app_config.services[key], finish_signals=("busy",)),
        },
    )
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        main._chat_region = ScreenRegion(0, 0, 40, 40)
        main._start_detector_worker()
        await pilot.pause()
        # Ticked, but there is no busy appearance to hunt: nothing runs.
        assert main._active_detectors == ()
        assert main._detector_worker is None

        editor = await _open_editor(app, pilot)
        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.BUSY)}")
        await _wait_for(pilot, lambda: editor._profiles_changed, "busy captured")
        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        assert main._active_detectors == ("busy",)
        assert main._detector_worker is not None


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

        editor = await _open_editor(app, pilot)
        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.COPY)}")
        await _wait_for(pilot, lambda: editor._profiles_changed, "copy captured")
        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")

        main.sidebar.slot_select.value = str(AgentSlot.SUBAGENT)
        await _wait_for(pilot, lambda: main._calibrating is AgentSlot.SUBAGENT, "slot switched")
        await pilot.pause()
        assert main._active_profile().has(TemplateKind.COPY)
        assert "1/6 captured" in _label(main, "#side-profile-note")

        await main.clear_transcript()  # the /new teardown hook
        await pilot.pause()
        assert main._active_profile().has(TemplateKind.COPY)
        assert "1/6 captured" in _label(main, "#side-profile-note")


async def test_the_capture_lands_under_the_service_the_editor_has_selected(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not under the sidebar's active service: the editor is a per-service form,
    and the user may well be setting up a service they are not chatting with."""
    from textual.widgets import Select

    _patch_picker(monkeypatch)
    app = _make_app(tmp_path, profile_root)
    async with app.run_test(size=SIZE) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        active = main._selected_service()
        other = next(key for key in sorted(app.app_config.services) if key != active)

        editor = await _open_editor(app, pilot)
        editor.query_one("#svc-select", Select).value = other
        await _wait_for(pilot, lambda: editor._selected_key == other, "the other service selected")

        await _press(editor, pilot, f"#{capture_button_id(TemplateKind.IDLE)}")
        await _wait_for(
            pilot,
            lambda: load_profile(profile_root, other).has(TemplateKind.IDLE),
            "filed under the other service",
        )
        assert not load_profile(profile_root, active).captured

        await pilot.press("escape")
        await _wait_for(pilot, lambda: app.screen is main, "editor closed back to the chat")
        # The chat's own service is untouched, and the summary still says so.
        assert not main._active_profile().captured
        assert "0/6 captured" in _label(main, "#side-profile-note")
