"""The chat box's `up`/`down` send history (tui.md §3.3d).

Two suites, split the way the feature is: `SendHistory` is pure state and its
rules - the cap, the consecutive-duplicate collapse, what "past the newest"
hands back - are asserted directly, with no app and no Pilot, because those are
exactly the rules that are cheap to get subtly wrong and expensive to debug
through a terminal. Everything about *who gets the key* is asserted through real
keypresses at the real box, because that question is an ordering question - the
popup, then the caret, then the history - and an ordering only exists at the
moment three claimants want the same press.

The Pilot half runs at the "Describe the task" prompt (the same ground
`test_composer_escape_ui.py` stands on, and for the same reason: the box behaves
identically there and in a live session, and the prompt costs no session setup).
The sends it makes are SLASH COMMANDS, deliberately - at the prompt a plain
message would start a session and drive the automation, while a slash line is
dispatched and leaves the prompt standing (`MainScreen._submit_text`). They are
still ordinary composer sends, which is all the history knows about.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.widgets.composer import HISTORY_LIMIT, SendHistory

# == SendHistory, on its own ===================================================


def test_the_newest_send_is_the_first_thing_up_reaches() -> None:
    history = SendHistory()
    history.push("first")
    history.push("second")

    assert history.older("") == "second"
    assert history.older("") == "first"


def test_up_stops_declining_at_the_oldest_entry_rather_than_doing_nothing() -> None:
    """Declining is not the same as swallowing: ``None`` hands the key back to
    the editor, so at the end of the walk `up` means what it means everywhere
    else instead of becoming a dead key."""
    history = SendHistory()
    history.push("only")

    assert history.older("") == "only"
    assert history.older("") is None
    assert history.browsing  # ...and the walk is still where it was


def test_down_past_the_newest_hands_the_draft_back() -> None:
    """The rule that makes an accidental `up` cost nothing, and the reason a
    recall needs no undo: the key that replaced the box gives it back."""
    history = SendHistory()
    history.push("sent")

    assert history.older("half a thought") == "sent"
    assert history.newer() == "half a thought"
    assert not history.browsing


def test_an_empty_draft_is_a_draft_like_any_other() -> None:
    """`up` from an empty box then `down` empties it again - "" is an answer,
    not a missing one, and a version that treated it as missing would leave the
    last send sitting in the box."""
    history = SendHistory()
    history.push("sent")

    assert history.older("") == "sent"
    assert history.newer() == ""


def test_down_declines_the_key_when_nobody_has_walked_up() -> None:
    history = SendHistory()
    history.push("sent")

    assert history.newer() is None


def test_the_draft_is_captured_on_the_way_in_and_only_then() -> None:
    """A second `up` must not overwrite it with the entry now on show, or
    walking two back and one forward would lose what the user was typing."""
    history = SendHistory()
    history.push("older")
    history.push("newer")

    assert history.older("mine") == "newer"
    assert history.older("newer") == "older"
    assert history.newer() == "newer"
    assert history.newer() == "mine"


def test_a_blank_send_is_not_remembered() -> None:
    history = SendHistory()
    history.push("   \n  ")

    assert history.entries == ()
    assert history.older("") is None


def test_sending_the_same_thing_twice_running_collapses_to_one_entry() -> None:
    """Common enough to be worth the rule - a retried command, a repeated
    "continue" - and it would otherwise cost two presses to get past one
    message. Only CONSECUTIVE repeats collapse; the same text later is a
    genuine second entry, because by then it is a different place in the walk."""
    history = SendHistory()
    history.push("continue")
    history.push("continue")
    history.push("something else")
    history.push("continue")

    assert history.entries == ("continue", "something else", "continue")


def test_the_history_is_capped_and_drops_the_oldest_first() -> None:
    history = SendHistory(limit=3)
    for index in range(5):
        history.push(f"message {index}")

    assert history.entries == ("message 2", "message 3", "message 4")


def test_the_cap_is_a_convenience_not_an_archive() -> None:
    """Stated as a number so a change to it is a decision someone made: the
    transcript is the transcript (`l` exports it), and this is a box's memory of
    the last few things typed into it."""
    assert HISTORY_LIMIT == 50
    assert len(SendHistory().entries) == 0


def test_a_send_ends_the_walk() -> None:
    """Whatever the user was browsing, the message that left is now the newest
    and the next `up` starts from it."""
    history = SendHistory()
    history.push("first")
    history.older("")
    history.push("second")

    assert not history.browsing
    assert history.older("") == "second"


# == who gets the press ========================================================


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


async def _send(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """One composer send, through the real Enter key.

    The trailing space in every caller's text is not decoration: a bare command
    is a command *in progress*, so the popup would be up and Enter would
    complete instead of sending (§3.3a). A space is what a completion appends
    and what closes the list, which is exactly the state a user is in when they
    press Enter on a command.
    """
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.move_cursor(main.composer.document.end)
    await pilot.pause()
    assert not main.command_popup.is_open
    await pilot.press("enter")
    await pilot.pause()


async def test_up_recalls_the_last_thing_that_was_sent(tmp_path: Path) -> None:
    """The whole point of the feature, in three presses: send it, clear the box,
    press up and have it back."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")
        # The dispatch already cleared the box and left the caret in it - an
        # `escape` here would be stage TWO of the chain (empty box: let go of
        # the focus) and the next press would land on the screen, not the box.
        assert main.composer.text == ""

        await pilot.press("up")
        assert main.composer.text == "/help "
        # ...with the caret at the END, ready to be edited or sent again.
        assert main.composer.cursor_location == main.composer.document.end


async def test_repeated_ups_walk_back_and_downs_walk_forward(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")
        await _send(app, pilot, "/theme ")

        await pilot.press("up")
        assert main.composer.text == "/theme "  # newest first
        await pilot.press("up")
        assert main.composer.text == "/help "
        await pilot.press("down")
        assert main.composer.text == "/theme "


async def test_down_past_the_newest_gives_the_half_typed_draft_back(tmp_path: Path) -> None:
    """An accidental `up` has to cost nothing, or the arrows are a hazard at a
    box whose whole job is holding text somebody is writing."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")
        await pilot.press("d", "r", "a", "f", "t")
        assert main.composer.text == "draft"

        await pilot.press("up")
        assert main.composer.text == "/help "
        await pilot.press("down")
        assert main.composer.text == "draft"


async def test_up_inside_a_multi_line_message_moves_the_caret_instead(tmp_path: Path) -> None:
    """The guard that keeps a pasted traceback navigable. The SECOND press is
    the other half of the assertion: once the caret has nowhere left to go, the
    same key recalls - so the rule is "at the edge", not "never in multi-line
    text"."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")
        main.composer.load_text("line one\nline two")
        main.composer.move_cursor(main.composer.document.end)
        await pilot.pause()

        await pilot.press("up")
        assert main.composer.text == "line one\nline two"  # untouched
        assert main.composer.cursor_location[0] == 0  # the caret moved, that is all

        await pilot.press("up")
        assert main.composer.text == "/help "


async def test_down_above_the_last_line_moves_the_caret_instead(tmp_path: Path) -> None:
    """`down`'s mirror of the rule above: from anywhere but the last line it is
    the editor's key."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")
        main.composer.load_text("line one\nline two")
        main.composer.move_cursor((0, 0))
        await pilot.pause()

        await pilot.press("down")
        assert main.composer.text == "line one\nline two"
        assert main.composer.cursor_location[0] == 1


async def test_the_popup_still_owns_both_arrows_while_it_is_up(tmp_path: Path) -> None:
    """Priority regression, and the reason the history branch sits below the
    popup's: with a command list open the arrows pick a row. If the history had
    jumped the queue, the list would have become unpickable the moment anything
    had ever been sent."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None
        popup = main.command_popup

        await _send(app, pilot, "/help ")
        await pilot.press("slash")
        assert popup.is_open

        await pilot.press("up")
        assert main.composer.text == "/"  # the text is the popup's business, not ours
        assert popup.highlighted is not None
        await pilot.press("down")
        assert main.composer.text == "/"


async def test_typing_ends_the_walk_and_the_edit_becomes_the_new_draft(tmp_path: Path) -> None:
    """Editing puts the box back in charge. The second half is the part that
    would be easy to ship half-done: the edited text is what `down` hands back
    afterwards, so the walk restarts around what the user has NOW."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")

        await pilot.press("up")
        assert main.composer.text == "/help "
        await pilot.press("x")
        assert main.composer.text == "/help x"

        # Browsing is over, so `down` is an ordinary caret key again...
        await pilot.press("down")
        assert main.composer.text == "/help x"
        # ...and the walk restarts from the newest, around the NEW draft.
        await pilot.press("up")
        assert main.composer.text == "/help "
        await pilot.press("down")
        assert main.composer.text == "/help x"


async def test_a_recall_does_not_read_as_an_edit_and_undo_itself(tmp_path: Path) -> None:
    """The trap this feature has: Textual POSTS ``Changed`` rather than raising
    it, so a flag set around ``load_text`` is already back to False when the
    message lands and the recall ends the walk it just started. Two ups in a row
    reaching two different entries is the proof it does not."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        await _send(app, pilot, "/help ")
        await _send(app, pilot, "/theme ")

        await pilot.press("up")
        await pilot.pause()  # let the recall's own Changed land first
        await pilot.press("up")
        assert main.composer.text == "/help "


async def test_an_empty_history_leaves_the_arrows_to_the_editor(tmp_path: Path) -> None:
    """Nothing sent yet is the state the box spends its first minute in, and
    the arrows must not swallow themselves there."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _at_the_prompt(app, pilot)
        main = app.main_screen
        assert main is not None

        main.composer.load_text("one\ntwo")
        main.composer.move_cursor(main.composer.document.end)
        await pilot.pause()

        await pilot.press("up")
        assert main.composer.text == "one\ntwo"
        assert main.composer.cursor_location[0] == 0
        await pilot.press("up")  # first line, nothing to recall: still no recall
        assert main.composer.text == "one\ntwo"
