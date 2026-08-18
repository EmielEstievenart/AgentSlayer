"""Pilot tests for `/identify`: the debug view of what the tool can see.

The command is one straight line - live slot's drawn window, one capture of it,
the pure search (screen.identify), a read-only fullscreen overlay drawn by a
child process, a summary toast - and every interesting property is about the
ORDER of those steps rather than about any one of them:

* the capture happens before the overlay exists (an overlay in the frame would
  be identified as part of the chat window);
* the finish detectors are suspended around the overlay and resumed after, for
  the same reason the region picker suspends them (§3.4e) - a fullscreen window
  appearing and vanishing over the browser they watch is the sustained large
  delta that arms the auto-copy trigger on staleness alone;
* with no window drawn there is nothing to identify, and the command says so
  instead of throwing an empty overlay over the desktop.

The real overlay spawns a child process that covers the user's screen, so it is
monkeypatched at its use site (``main_mod.draw_identify_overlay``) - and blocked
suite-wide underneath that (tests/conftest.py), so a test that forgets goes red
rather than fullscreen. The appearance the search finds is seeded straight into
the profile store, as every suite here does; the capture flow itself is covered
once, in test_profile_capture_ui.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from textual.pilot import Pilot

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.identify import CHAT_REGION_LABEL, IdentifiedElement
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.main import MainScreen

from .conftest import send_composer, template_image

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
COPY_SIZE = (24, 24)
# Where the seeded copy icon sits inside the captured frame, and the absolute
# rectangle that implies - the box has to be drawn on the second one.
COPY_AT = (300, 200)
COPY_RECT = ScreenRegion(
    CHAT_REGION.left + COPY_AT[0], CHAT_REGION.top + COPY_AT[1], *COPY_SIZE
)
SIZE = (110, 100)

NO_REGION_TOAST = "no chat window drawn for this tab"


@pytest.fixture(autouse=True)
def _no_detector_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the finish detectors, and a live poller would be
    capturing the same faked frame on its own schedule while these tests drive
    the composer. (The poller itself is test_stale_detector_ui's.) The
    suspend/resume bracket is still observed - it is recorded around the call,
    not through the worker it stops."""
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)


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


def _service_key(app: AgentClipApp) -> str:
    config = app.app_config
    configured = config.general.service
    return configured if configured in config.services else next(iter(sorted(config.services)))


def _scene_with_the_copy_icon() -> RegionImage:
    """A capture of the chat region with the seeded appearance planted in it.

    The background is a plain LCG so no anchor of the icon matches it by
    accident; the icon is the same image the profile store was seeded with, so
    the search finds it exactly where it was put.
    """
    icon = template_image(*COPY_SIZE)
    state = 7
    pixels = bytearray(CHAT_REGION.width * CHAT_REGION.height * 4)
    for index in range(len(pixels)):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        pixels[index] = (state >> 16) & 0xFF
    x, y = COPY_AT
    for row in range(icon.height):
        start = ((y + row) * CHAT_REGION.width + x) * 4
        source = row * icon.width * 4
        pixels[start : start + icon.width * 4] = icon.pixels[source : source + icon.width * 4]
    return RegionImage(CHAT_REGION.width, CHAT_REGION.height, bytes(pixels))


def _toasts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    notes: list[str] = []
    monkeypatch.setattr(
        MainScreen, "notify", lambda self, message, *a, **kw: notes.append(str(message))
    )
    return notes


def _said(notes: list[str], fragment: str) -> bool:
    return any(fragment in note for note in notes)


def _record_overlay(
    monkeypatch: pytest.MonkeyPatch, order: list[str] | None = None
) -> list[Sequence[IdentifiedElement]]:
    """Stand in for the child process, recording what it was asked to draw."""
    drawn: list[Sequence[IdentifiedElement]] = []

    def fake_overlay(elements: Sequence[IdentifiedElement]) -> None:
        if order is not None:
            order.append("overlay")
        drawn.append(list(elements))

    monkeypatch.setattr(main_mod, "draw_identify_overlay", fake_overlay)
    return drawn


def _record_detector_bracket(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Log the suspend/overlay/resume order, keeping the real behaviour."""
    order: list[str] = []
    suspend, resume = MainScreen.suspend_detectors, MainScreen.resume_detectors
    monkeypatch.setattr(
        MainScreen, "suspend_detectors", lambda self: order.append("suspend") or suspend(self)
    )
    monkeypatch.setattr(
        MainScreen, "resume_detectors", lambda self: order.append("resume") or resume(self)
    )
    return order


async def _armed(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """A live session, idle, with the composer taking commands."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    await send_composer(app, pilot, "Say hello.")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


def _draw_the_region(main: MainScreen) -> None:
    """Give the master window a drawn rectangle, as the picker would.

    Written straight into the slot rather than driven through the sidebar
    button: /identify's precondition is *a region exists*, and how one gets
    there - overlay child process, cancel, slot routing - is
    test_chat_region_ui's whole subject.
    """
    main._chat_region = CHAT_REGION


async def test_identify_boxes_the_window_and_everything_found_inside_it(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The whole point: the drawn window plus every appearance the live service
    can find in it, in ABSOLUTE screen pixels, handed to the overlay in one go -
    and summarised in a toast, since the overlay is gone by the time the user
    looks back at the terminal."""
    app = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.COPY, size=COPY_SIZE)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: _scene_with_the_copy_icon())
    drawn = _record_overlay(monkeypatch)
    notes = _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        _draw_the_region(main)

        await send_composer(app, pilot, "/identify")
        await _wait_for(pilot, lambda: bool(drawn), "the overlay was asked to draw")

        elements = drawn[0]
        assert [element.label for element in elements] == [CHAT_REGION_LABEL, "copy"]
        assert elements[0].rect == CHAT_REGION  # where we were looking
        assert elements[0].diff is None  # ...was drawn, not matched
        assert elements[1].rect == COPY_RECT  # where the icon actually is
        assert elements[1].diff is not None and elements[1].diff <= TemplateKind.COPY.max_diff
        await _wait_for(pilot, lambda: _said(notes, "identified 1 elements"), "summary toast")
        assert _said(notes, "copy×1")
        assert main.session_active  # a debug view changes nothing about the run


async def test_identify_suspends_the_detectors_around_the_overlay(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Same bracket as the region picker, for the same reason: this is the same
    translucent fullscreen child process thrown over the very browser window the
    finish detectors are watching, and one appearing and vanishing arms the
    auto-copy trigger on staleness alone.

    The capture is recorded too, and it must come FIRST - a frame taken with the
    overlay up would identify the overlay as part of the chat window."""
    app = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.COPY, size=COPY_SIZE)
    order = _record_detector_bracket(monkeypatch)
    drawn = _record_overlay(monkeypatch, order)

    def fake_capture(region: ScreenRegion) -> RegionImage:
        order.append("capture")
        return _scene_with_the_copy_icon()

    monkeypatch.setattr(main_mod, "capture_region", fake_capture)
    _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        _draw_the_region(main)
        order.clear()  # the picker's own bracket is test_chat_region_ui's

        await send_composer(app, pilot, "/identify")
        await _wait_for(pilot, lambda: bool(drawn), "the overlay was asked to draw")
        await _wait_for(pilot, lambda: order[-1:] == ["resume"], "the bracket closed")
        assert order == ["capture", "suspend", "overlay", "resume"]


async def test_identify_with_no_window_drawn_explains_itself_and_draws_nothing(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nowhere to look, so a fullscreen overlay with one box on it
    would be a worse answer than a sentence."""
    app = _make_app(tmp_path, profile_root)
    drawn = _record_overlay(monkeypatch)
    notes = _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        assert main._chat_region is None

        await send_composer(app, pilot, "/identify")
        await _wait_for(pilot, lambda: _said(notes, NO_REGION_TOAST), "the refusal toast")
        await pilot.pause(0.1)
        assert drawn == []


async def test_a_failed_capture_is_reported_instead_of_drawn(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No frame, no answer - and the detectors are never suspended, because the
    overlay that would need it is never put up."""
    app = _make_app(tmp_path, profile_root)
    order = _record_detector_bracket(monkeypatch)
    drawn = _record_overlay(monkeypatch, order)
    notes = _toasts(monkeypatch)

    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("GDI is unavailable")

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        _draw_the_region(main)
        order.clear()
        monkeypatch.setattr(main_mod, "capture_region", boom)

        await send_composer(app, pilot, "/identify")
        await _wait_for(pilot, lambda: _said(notes, "could not capture"), "the capture error")
        await pilot.pause(0.1)
        assert drawn == []
        assert order == []


async def test_the_command_survives_an_overlay_that_will_not_run(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with no tkinter must get the error, not a wedged screen that
    can never be identified again - the second attempt has to be allowed."""
    app = _make_app(tmp_path, profile_root)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: _scene_with_the_copy_icon())
    attempts: list[int] = []

    def boom(elements: Sequence[IdentifiedElement]) -> None:
        attempts.append(1)
        raise main_mod.ScreenPickError("identify overlay unavailable: no tkinter")

    monkeypatch.setattr(main_mod, "draw_identify_overlay", boom)
    notes = _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        _draw_the_region(main)

        await send_composer(app, pilot, "/identify")
        await _wait_for(pilot, lambda: _said(notes, "no tkinter"), "the overlay error")
        await _wait_for(pilot, lambda: not main._picker_open, "the overlay latch released")

        await send_composer(app, pilot, "/identify")
        await _wait_for(pilot, lambda: len(attempts) == 2, "a second attempt was allowed")


# -- at the "Describe the task" prompt (tui.md §1.3) --------------------------
#
# The regression these pin: the task prompt used to take its text VERBATIM, so
# `/identify` typed there was never dispatched - it became the opening message
# of the session and went to the model. That is the worst possible place to lose
# it, because /identify is the one command with no session gate precisely so it
# works when nothing is armed, which is exactly this state.


async def _at_the_task_prompt(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """The launch state itself: no session, the composer waiting for a task."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


@pytest.mark.parametrize("typed", ["/identify", "/identify "])
async def test_identify_runs_at_the_task_prompt_instead_of_becoming_the_task(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None], typed: str,
) -> None:
    """The command runs and the prompt is still waiting - no session is started
    and nothing is sent to the model. The trailing-space spelling is the same
    line as far as dispatch is concerned (the text is stripped first); it is
    parametrized because it is what the user reported typing."""
    app = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.COPY, size=COPY_SIZE)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: _scene_with_the_copy_icon())
    drawn = _record_overlay(monkeypatch)
    _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _draw_the_region(main)

        await send_composer(app, pilot, typed)
        await _wait_for(pilot, lambda: bool(drawn), "the overlay was asked to draw")

        assert [element.label for element in drawn[0]] == [CHAT_REGION_LABEL, "copy"]
        assert main.awaiting_new_session  # the prompt never resolved...
        assert not main.session_active  # ...so no session, and nothing sent
        assert not any("/identify" in entry for entry in main.transcript.entries)


async def test_a_command_that_needs_a_session_refuses_at_the_task_prompt(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch, not exemption: each command's own gate answers. `/abort` wants a
    run to end, says there is none, and the prompt keeps waiting - what it must
    NOT do is become the task."""
    app = _make_app(tmp_path, profile_root)
    notes = _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)

        await send_composer(app, pilot, "/abort")
        await _wait_for(pilot, lambda: _said(notes, "no sub-agent run to abort"), "the refusal")
        assert main.awaiting_new_session
        assert not main.session_active


async def test_an_unknown_command_at_the_task_prompt_is_reported_not_started(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo is a typo here too - the usual hint, and no session begun on it."""
    app = _make_app(tmp_path, profile_root)
    notes = _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)

        await send_composer(app, pilot, "/foo")
        await _wait_for(pilot, lambda: _said(notes, "unknown command: /foo"), "the hint")
        assert main.awaiting_new_session
        assert not main.session_active
        assert main.composer.text == ""  # dispatch cleared the box


async def test_a_task_that_starts_with_a_slash_is_escaped_with_two(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow-up path's escape hatch, at the prompt: `//...` strips one slash
    and the rest is an ordinary task, so nothing is unsayable."""
    app = _make_app(tmp_path, profile_root)
    _toasts(monkeypatch)

    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)

        await send_composer(app, pilot, "//identify the flaky test")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        assert not main.awaiting_new_session
        assert any(
            "you: /identify the flaky test" in entry for entry in main.transcript.entries
        )
