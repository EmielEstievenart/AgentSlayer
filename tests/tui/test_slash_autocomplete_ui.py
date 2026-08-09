"""Pilot tests for slash-command autocomplete in the chat box (tui.md §3.3a).

The feature is three rules and they interact, so they are tested through real
keypresses rather than by poking the widget: the popup opens on a bare `/` and
narrows as characters arrive, four keys mean something different while it is up
(arrows, Enter/Tab, Escape), and it must not appear at all while the box's next
send is taken verbatim - the task that starts a session, and an answer to the
model's question, where a leading slash is text.

The Enter rule is the one worth stating twice: with the popup up, Enter
*completes* rather than sends. That is only safe because completing appends a
space, which closes the popup, so the second Enter is an ordinary send - and
these tests press it twice for exactly that reason.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot

from agentclip.app.commands import COMMANDS
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured

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


async def test_down_then_enter_completes_and_sends_nothing(tmp_path: Path) -> None:
    """Enter with the popup up completes the highlighted row - it must not send,
    or the user could never see the list they just opened."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup
        writes_before = len(fake.written)

        await pilot.press("slash")
        assert popup.index == 0
        assert popup.highlighted is not None and popup.highlighted.name == "yolo"

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


async def test_up_wraps_to_the_last_command(tmp_path: Path) -> None:
    app, _, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await pilot.press("slash")
        await pilot.press("up")
        assert popup.index == len(COMMANDS) - 1
        assert popup.highlighted is not None and popup.highlighted.name == "help"


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


async def test_escape_closes_the_popup_first_and_blurs_second(tmp_path: Path) -> None:
    """Escape has to keep its old job (drop to command mode) without eating the
    text: with a list up it dismisses the list only."""
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

        await pilot.press("escape")  # no popup now: the old blur behaviour
        assert app.focused is not main.composer


async def test_no_popup_while_the_task_is_being_typed(tmp_path: Path) -> None:
    """The first message IS the task, verbatim - a slash there is a word, not a
    command, so offering to complete it would misrepresent what Enter does."""
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        popup = main.command_popup

        await pilot.press("slash")
        assert not popup.is_open
        assert main.composer.verbatim

        main.composer.load_text("/deploy the thing")
        await pilot.pause()
        assert not popup.is_open

        await pilot.press("enter")  # one Enter, because no popup ate it
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "session flow settled")

        # The slash-leading task went to the model verbatim.
        assert any("you: /deploy the thing" in e for e in main.transcript.entries)
        assert "/deploy the thing" in fake.written[-1]
        assert not main.composer.verbatim  # ...and the box is a chat box again


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
