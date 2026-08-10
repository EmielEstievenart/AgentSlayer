"""The slash-command registry, and the two things that must derive from it.

The point of ``agentclip.app.commands`` is that there is exactly one list. So
these tests are less about the strings than about the joins: the controller's
dispatch table covers the registry and nothing else, `/help` and the "unknown
command" hint are rendered from the same entries, and the autocomplete trigger
agrees with the dispatcher about what counts as a command in progress.
"""

from __future__ import annotations

from agentclip.app.commands import (
    COMMANDS,
    ChatCommand,
    command_list,
    help_text,
    lookup,
    match_prefix,
)
from agentclip.app.controller import SessionController

from .conftest import FakeChatView


def test_the_registry_is_the_documented_commands() -> None:
    """tui.md §3.3a's list, in the order the user meets them - which is also the
    order the popup lists them in, so the most destructive one is last."""
    assert [command.name for command in COMMANDS] == [
        "help",
        "new",
        "abort",
        "identify",
        "log",
        "armed",
        "yolo",
    ]
    assert lookup("yolo") is not None and lookup("yolo").arg == "[on|off]"  # type: ignore[union-attr]
    assert all(command.summary for command in COMMANDS)


def test_yolo_is_never_the_first_row() -> None:
    """Regression (tui.md §3.3a): the popup renders this tuple as typed, so the
    top row is whatever a stray Enter is nearest to - and `/yolo` turns EVERY
    approval gate off. It goes last, behind the reversible ones."""
    assert COMMANDS[-1].name == "yolo"
    assert COMMANDS[0].name == "help"  # ...and the top row is the harmless one


def test_lookup_resolves_aliases_and_case() -> None:
    help_command = lookup("help")
    assert help_command is not None
    assert lookup("commands") is help_command
    assert lookup("?") is help_command
    assert lookup("HELP") is help_command
    assert lookup("Yolo") is lookup("yolo")
    assert lookup("nope") is None
    assert lookup("") is None
    assert lookup("  ") is None


def test_dispatch_covers_the_registry_exactly(controller: SessionController) -> None:
    """A command in the table with no handler would be an undiscoverable dead
    row in the popup; a handler with no table entry would be a hidden feature."""
    handlers = controller._command_handlers()
    assert set(handlers) == {command.name for command in COMMANDS}


def test_help_text_lists_every_command_with_its_hint() -> None:
    text = help_text()
    assert text.startswith("commands:  ")
    for command in COMMANDS:
        assert command.label in text
        assert command.summary in text
    # The two escape hatches that are easy to confuse stay distinguished in it.
    assert "/abort" in text and "ctrl+x" in text
    assert "/yolo [on|off]" in text  # the argument hint travels with the name


def test_unknown_command_hint_lists_every_command() -> None:
    hint = command_list()
    for command in COMMANDS:
        assert command.slash in hint
    # An English list, not a dump.
    assert hint == "/help, /new, /abort, /identify, /log, /armed, or /yolo"


def test_identify_dispatches_to_the_view_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """The one command with no session gate. It is a calibration aid, and the
    states it is most needed in - nothing armed, or a run wedged against a chat
    window the tool cannot read - are exactly the states a gate would lock it out
    of. It cannot touch a session either: the view captures one frame and draws
    on top of it."""
    controller.submit_message("/identify")
    assert view.identify_overlays == 1
    assert view.toasts() == []  # no refusal, no "start a session first"


def test_log_dispatches_to_the_view_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/identify`'s rule again, and for the same reason: the harness log is
    read when the automation has stopped making sense, which is routinely after
    a run wedged or ended. Gating it behind a live session would lock it away
    exactly when it is wanted, and the controller has nothing to add - every
    decision in the log was taken on the view's side of the port."""
    controller.submit_message("/log")
    assert view.harness_log_toggles == 1
    assert view.toasts() == []  # no refusal, no "start a session first"
    assert view.events == []  # and nothing lands in the transcript
    # ...and it is a toggle on the far side, so the second one puts it away
    # rather than stacking a second view of the same log.
    controller.submit_message("/log")
    assert view.harness_log_toggles == 2


def test_armed_dispatches_to_the_view_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/identify`'s reasoning, one step sharper. The armed switch is what a user
    reaches for when the tool is doing something to their screen they want
    stopped, so a session gate would lock it away at the exact moment it is
    wanted. Bare = toggle, and the target travels as ``None`` because the flag
    lives in the view - the controller has nothing to toggle against."""
    controller.submit_message("/armed")
    assert view.armed_targets == [None]
    assert view.os_armed is False
    assert view.toasts() == []  # no refusal, no "start a session first"

    controller.submit_message("/armed")
    assert view.armed_targets == [None, None]
    assert view.os_armed is True


def test_armed_takes_an_explicit_on_or_off(
    controller: SessionController, view: FakeChatView
) -> None:
    """Explicit beats toggle for the case that matters: a user who is not sure
    what state they are in can say which one they want and get it."""
    controller.submit_message("/armed off")
    controller.submit_message("/armed off")  # ...and saying it twice is idempotent
    assert view.armed_targets == [False, False]
    assert view.os_armed is False

    controller.submit_message("/armed ON")
    assert view.armed_targets == [False, False, True]
    assert view.os_armed is True


def test_an_unparseable_armed_argument_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    """A typo must never be read as either half of a safety switch."""
    controller.submit_message("/armed maybe")
    assert view.armed_targets == []
    assert view.os_armed is True
    assert any("usage: /armed [on|off]" in message for message, _ in view.notifications)


def test_the_engine_never_hears_about_the_armed_switch(
    controller: SessionController, view: FakeChatView
) -> None:
    """The line between this and `/yolo`: YOLO is one session's policy (audited
    into that session's log, dead when it ends), while ARMED is a property of the
    machine the user is sitting at. So this one starts no engine call and needs
    no session - it never even reaches for one."""
    controller.submit_message("/armed off")
    assert view.armed_targets == [False]
    assert controller._engine is None
    assert view.events == []  # no transcript note, no audit trail


def test_match_prefix_narrows_as_the_user_types() -> None:
    assert match_prefix("/") == COMMANDS  # a bare slash offers everything
    assert [c.name for c in match_prefix("/y")] == ["yolo"]
    assert [c.name for c in match_prefix("/i")] == ["identify"]
    assert [c.name for c in match_prefix("/n")] == ["new"]
    assert [c.name for c in match_prefix("/yolo")] == ["yolo"]
    assert [c.name for c in match_prefix("/YO")] == ["yolo"]


def test_match_prefix_is_empty_for_everything_that_is_not_a_command_in_progress() -> None:
    assert match_prefix("") == ()
    assert match_prefix("hello") == ()
    assert match_prefix(" /y") == ()  # not a leading slash
    assert match_prefix("//escaped") == ()  # the literal-slash escape hatch
    assert match_prefix("/xyz") == ()  # nothing matches
    assert match_prefix("/yolo ") == ()  # committed: typing the argument now
    assert match_prefix("/yolo on") == ()
    assert match_prefix("/etc/hosts") == ()  # a path is not a prefix of anything
    assert match_prefix("/\n") == ()


def test_aliases_are_dispatch_only_and_never_offered() -> None:
    """`/commands` runs, but the popup teaches one spelling per command."""
    assert lookup("commands") is not None
    assert match_prefix("/comm") == ()
    assert all(command.name != "commands" for command in COMMANDS)


def test_label_falls_back_to_the_bare_name_without_an_argument() -> None:
    assert ChatCommand(name="new", summary="x").label == "/new"
    assert ChatCommand(name="yolo", summary="x", arg="[on|off]").label == "/yolo [on|off]"
