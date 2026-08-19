"""Pilot tests for slash-command autocomplete in the chat box (tui.md §3.3a).

The feature is three rules and they interact, so they are tested through real
keypresses rather than by poking the widget: the popup opens on a bare `/` and
narrows as characters arrive, four keys mean something different while it is up
(arrows, Enter/Tab, Escape), and it must not appear at all while the box's next
send is taken verbatim - an answer to the model's question, where a leading
slash is text. The task prompt is NOT such a mode: Enter dispatches commands
there too (§1.3), so the popup is offered.

The Enter rule is the one worth stating twice: with the popup up, Enter
*completes* rather than sends. That is only safe because completing appends a
space, which closes the popup, so the second Enter is an ordinary send - and
these tests press it twice for exactly that reason. It is only safe at all
while there is something the user CHOSE: a bare `/` lists everything and
highlights nothing, so slash-Enter-Enter runs no command whatsoever, which is
the first test below.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.shell.app.commands import COMMANDS
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.messages import ClipboardCaptured

UTILS_PY = '''"""Utility helpers."""


def parse_date(s):
    return s
'''

REPLY_ASK_USER = """I need to know where to write.

~~~~
===CLIP:CALL id=1 tool=ask_user===
question <<EOT
Which absolute path should I write to?
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
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.load_text(task)
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")


def _names(app: AgentClipApp) -> list[str]:
    main = app.main_screen
    assert main is not None
    return [command.name for command in main.command_popup.matches]


async def test_slash_opens_the_list_and_typing_filters_it(tmp_path: Path) -> None:
    """`/` offers every command, each further character narrows, and the three
    ways a line stops being a command in progress each close it."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup
        assert not popup.is_open  # nothing typed yet: no popup on an empty box

        await pilot.press("slash")
        assert popup.is_open
        assert popup.display
        assert _names(app) == [command.name for command in COMMANDS]

        await pilot.press("y")  # "/y"
        assert popup.is_open
        assert _names(app) == ["yolo"]

        await pilot.press("x")  # "/yx" matches nothing
        assert not popup.is_open
        assert main.composer.text == "/yx"  # the text is untouched; only the list went

        await pilot.press("backspace")  # back to "/y"
        assert _names(app) == ["yolo"]

        await pilot.press("space")  # "/y " - whitespace ends the token
        assert not popup.is_open

        # The literal-slash escape hatch never offers a completion.
        main.composer.reset()
        await pilot.press("slash", "slash")
        assert not popup.is_open
        await pilot.press("n", "e", "w")  # "//new" is a message, not a command
        assert not popup.is_open


async def test_every_command_gets_exactly_one_row(tmp_path: Path) -> None:
    """Regression: the popup's height must be its match count.

    `/abort`'s summary is wider than the chat column at every size we run at. If
    it *wrapped* it would take two rows, the highlight would stop lining up with
    the commands, and the popup's max-height would silently clip the last entry
    off the bottom - `/help` would simply not be there. It is cut, not wrapped,
    so a narrow terminal loses characters rather than a whole command.
    """
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:  # deliberately cramped
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        await pilot.press("slash")
        await pilot.pause()

        popup = main.command_popup
        assert popup.matches == COMMANDS
        assert popup.region.height == len(COMMANDS) + 2  # one row each, + the border
        # It sits directly above the box, and the box is still fully on screen.
        assert popup.region.y + popup.region.height == main.composer.region.y
        assert main.composer.region.y + main.composer.region.height <= 24


async def test_a_bare_slash_highlights_nothing_and_two_enters_run_nothing(
    tmp_path: Path,
) -> None:
    """The two-keystroke accident this rule exists to close.

    With the top row pre-selected, `/` + Enter + Enter ran COMMANDS[0] - and
    when that was `/yolo`, a stray slash in the chat box was two Enters away
    from silently disabling every approval gate in the app. A list the user has
    not narrowed arms nothing: Enter completes nothing AND still does not send,
    so a bare slash cannot execute anything at all.
    """
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup
        writes_before = len(fake.written)

        await pilot.press("slash")
        assert popup.is_open  # the list is up: this is discovery, not a dead key
        assert popup.index is None
        assert popup.highlighted is None

        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert main.composer.text == "/"  # untouched: no completion, no send
        assert popup.is_open
        assert main._snap is not None and not main._snap.yolo  # nothing ran
        assert main.session_active and not main.awaiting_new_session
        assert len(fake.written) == writes_before


async def test_down_then_enter_completes_the_highlighted_row(tmp_path: Path) -> None:
    """One arrow press is what arms an unnarrowed list, and Enter then completes
    the highlighted row - it must not send, or the user could never see the list
    they just opened."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup
        writes_before = len(fake.written)

        await pilot.press("slash")
        assert popup.index is None

        await pilot.press("down")  # arms the list at its top row
        assert popup.index == 0
        assert popup.highlighted is not None and popup.highlighted.name == COMMANDS[0].name

        await pilot.press("down")
        assert popup.index == 1
        assert popup.highlighted is not None and popup.highlighted.name == "new"

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert main.composer.text == "/new "  # completed, with the argument space
        assert not popup.is_open  # the space closed it
        # Nothing reached the controller: /new would have re-armed the start flow.
        assert main.session_active
        assert not main.awaiting_new_session
        assert len(fake.written) == writes_before

        # ...and the box still has focus, so typing continues where it left off.
        assert app.focused is main.composer


async def test_up_arms_the_list_at_its_last_command(tmp_path: Path) -> None:
    """Up from no highlight lands where wrapping from the top would have."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash")
        await pilot.press("up")
        assert popup.index == len(COMMANDS) - 1
        assert popup.highlighted is not None and popup.highlighted.name == COMMANDS[-1].name

        await pilot.press("down")  # ...and it wraps from there as before
        assert popup.index == 0


async def test_one_typed_letter_arms_the_narrowed_list(tmp_path: Path) -> None:
    """The explicit path stays two keystrokes: `/y` names exactly one command,
    so Enter completes it - narrowing IS the choice."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash", "y")
        assert _names(app) == ["yolo"]
        assert popup.index == 0
        assert popup.highlighted is not None and popup.highlighted.name == "yolo"

        await pilot.press("enter")
        assert main.composer.text == "/yolo "

        # ...and backspacing to a bare slash disarms it again.
        main.composer.load_text("/")
        main.composer.move_cursor(main.composer.document.end)
        await pilot.pause()
        assert popup.is_open and popup.index is None


async def test_tab_completes_the_same_way_as_enter(tmp_path: Path) -> None:
    """Tab is the muscle-memory key for completion; it must not move focus here."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash", "y")
        assert _names(app) == ["yolo"]
        await pilot.press("tab")
        await pilot.pause(0.1)

        assert main.composer.text == "/yolo "
        assert not popup.is_open
        assert app.focused is main.composer  # tab completed instead of tabbing away
        assert main._snap is not None and not main._snap.yolo  # not sent yet


async def test_enter_after_the_completion_sends_the_command(tmp_path: Path) -> None:
    """The second Enter is an ordinary send: the completed command runs."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        await pilot.press("slash", "h")  # "/h" -> /help
        await pilot.press("enter")  # completes to "/help "
        assert main.composer.text == "/help "
        await pilot.press("enter")  # sends

        await _wait_for(
            pilot,
            lambda: any("commands:" in entry for entry in main.transcript.entries),
            "the /help note in the transcript",
        )
        note = next(e for e in main.transcript.entries if "commands:" in e)
        for command in COMMANDS:
            assert command.slash in note
        assert main.composer.text == ""  # the send cleared the box


async def test_typing_a_whole_command_then_two_enters_runs_it(tmp_path: Path) -> None:
    """The same flow for a user who never looks at the list: `/yolo` is still a
    completion candidate while it is typed, so it takes a completing Enter and a
    sending one - and YOLO really does come on."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        await pilot.press("slash", "y", "o", "l", "o")
        assert main.command_popup.is_open
        await pilot.press("enter")
        assert main.composer.text == "/yolo "
        await pilot.press("enter")

        await _wait_for(pilot, lambda: main._snap is not None and main._snap.yolo, "yolo armed")


async def test_escape_closes_the_popup_before_the_composer_sees_it(tmp_path: Path) -> None:
    """The popup gets first refusal on Escape, ahead of both of the composer's
    own stages (§3.3c): with a list up it dismisses the list ONLY - neither the
    half-typed command nor the focus may move, or the key that closes the list
    would take the command with it. Only once the list is gone does Escape mean
    what it means at any other line."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash", "n")
        assert popup.is_open

        await pilot.press("escape")
        assert not popup.is_open
        assert main.composer.text == "/n"  # the text survived
        assert app.focused is main.composer  # ...and so did the focus

        await pilot.press("escape")  # no popup now: stage one clears the box
        assert main.composer.text == ""
        assert app.focused is main.composer

        await pilot.press("escape")  # empty now: stage two blurs, as it always did
        assert app.focused is not main.composer


async def test_the_popup_is_offered_at_the_task_prompt_where_slashes_are_commands(
    tmp_path: Path,
) -> None:
    """Enter runs commands at the "Describe the task" prompt (§1.3), so hiding the
    popup there would hide the completion for a key that dispatches - the exact
    misrepresentation the suppression rule exists to prevent. The task that must
    begin with a slash is typed `//...`, and that escape closes the popup itself."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        popup = main.command_popup

        await pilot.press("slash")
        assert popup.is_open  # nothing armed is exactly where /identify is needed
        assert not main.composer.verbatim

        main.composer.load_text("//deploy the thing")  # the literal-slash escape
        await pilot.pause()
        assert not popup.is_open

        await pilot.press("enter")  # one Enter, because no popup ate it
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        # One slash stripped: what follows it is the task, sent to the model.
        assert any("you: /deploy the thing" in e for e in main.transcript.entries)
        assert "/deploy the thing" in fake.written[-1]


async def test_no_popup_while_answering_and_the_answer_still_wins(tmp_path: Path) -> None:
    """The standing invariant (§3.3a precedence): while the model is waiting for
    an answer the box's text is the answer. The popup must stay out of the way,
    and `/abort` typed there must be delivered, not run."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        main.post_message(ClipboardCaptured(REPLY_ASK_USER))
        await _wait_for(pilot, lambda: main.awaiting_answer, "model asked a question")
        assert main.composer.verbatim

        await pilot.press("slash", "a")
        assert not popup.is_open
        assert main.composer.text == "/a"

        writes_before = len(fake.written)
        main.composer.load_text("/abort")
        await pilot.pause()
        assert not popup.is_open
        await pilot.press("enter")  # a single Enter answers: nothing intercepted it

        await _wait_for(pilot, lambda: not main.awaiting_answer, "answer accepted")
        await _wait_for(
            pilot, lambda: len(fake.written) > writes_before, "results copied after answering"
        )
        assert "/abort" in fake.written[-1]
        assert any("you: /abort" in e for e in main.transcript.entries)


async def test_the_popup_closes_when_the_box_is_disabled(tmp_path: Path) -> None:
    """A gate disables the box and moves focus to Approve; a list left hanging
    over a dead composer would be advertising keys that no longer work."""
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash")
        assert popup.is_open

        main.composer.disabled = True
        main.composer.sync_popup()
        assert not popup.is_open
