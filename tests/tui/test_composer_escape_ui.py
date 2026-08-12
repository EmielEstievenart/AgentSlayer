"""Pilot tests for the chat box's two-stage `Esc` (tui.md §3.3c).

One key means three things here, and the whole design is the *order* they are
tried in, so they are tested through real keypresses at the real box rather than
by calling the handler: the slash popup gets first refusal, then a box with text
in it is cleared (focus kept), and only an already-empty box blurs to the
screen's single-key command mode.

The undo test is the one that earns its keep. Clearing has to go through the
TextArea's edit history, because the obvious implementation - `load_text("")`,
which is what `reset()` still uses after a send - throws that history away, and
a single keystroke that can destroy a paragraph of typing with no way back is
not a feature. `ctrl+z` restoring *exactly* the text that was there also pins
the checkpoint: batched with the preceding keystrokes, the undo would hand back
a half-typed line instead.

These run at the "Describe the task" prompt, where the composer is enabled and
focused before anything has been armed - the box behaves identically there and
in a live session, and the prompt costs no session setup.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.tui.app import AgentClipApp


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
    project.mkdir(parents=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake


async def _at_the_prompt(app: AgentClipApp, pilot: Pilot) -> None:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.focus()
    await pilot.pause()


async def test_escape_clears_the_box_and_keeps_the_focus(tmp_path: Path) -> None:
    """The commonest thing wanted from Esc at a chat box: scrap this, start again.

    Focus must survive, or "start again" costs a second key (`t`) before the
    first character can be typed - and the cursor is left at the top of the now
    empty document, ready for it.
    """
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await pilot.press("t", "i", "d", "y")
        assert main.composer.text == "tidy"

        await pilot.press("escape")
        assert main.composer.text == ""
        assert app.focused is main.composer
        assert main.composer.cursor_location == (0, 0)

        # ...and typing continues immediately, with no focus round-trip.
        await pilot.press("h", "i")
        assert main.composer.text == "hi"


async def test_ctrl_z_gives_the_cleared_text_back(tmp_path: Path) -> None:
    """The clear is an EDIT, not a reload.

    `load_text` (what `reset()` uses after a send) resets the undo history, so a
    composer cleared that way is gone for good. The checkpoint before the clear
    is what makes this undo restore the WHOLE line rather than unwinding into
    the middle of it along with the last few keystrokes.
    """
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None
        typed = "rename parse_date and update its callers"
        main.composer.load_text(typed)
        main.composer.move_cursor(main.composer.document.end)
        await pilot.pause()

        await pilot.press("escape")
        assert main.composer.text == ""

        await pilot.press("ctrl+z")
        await pilot.pause()
        assert main.composer.text == typed  # every character, not a prefix
        assert app.focused is main.composer


async def test_escape_on_an_empty_box_blurs_to_command_mode(tmp_path: Path) -> None:
    """Stage two is the old behaviour, unchanged - an empty box has nothing to
    lose, so Esc drops to the screen's single-key shortcuts."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None
        assert main.composer.text == ""

        await pilot.press("escape")
        assert app.focused is not main.composer


async def test_two_escapes_reach_command_mode_from_a_typed_line(tmp_path: Path) -> None:
    """The cost of stage one, stated: getting to command mode with a half-typed
    message takes two presses - and the first one is exactly what makes the
    second one safe to say it throws nothing away."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        main.composer.load_text("half a thought")
        await pilot.pause()

        await pilot.press("escape")  # clears, keeps focus
        assert app.focused is main.composer
        await pilot.press("escape")  # now empty: blurs
        assert app.focused is not main.composer


async def test_the_popup_still_gets_the_first_escape(tmp_path: Path) -> None:
    """Priority regression: with a command list up, Esc dismisses the LIST and
    touches neither the text nor the focus (§3.3a). If clearing had jumped the
    queue, the key that closes the popup would also eat the command being typed
    underneath it."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash", "n")
        assert popup.is_open

        await pilot.press("escape")
        assert not popup.is_open
        assert main.composer.text == "/n"  # the text survived the dismissal
        assert app.focused is main.composer

        await pilot.press("escape")  # no popup now: stage one clears it
        assert main.composer.text == ""
        assert app.focused is main.composer
