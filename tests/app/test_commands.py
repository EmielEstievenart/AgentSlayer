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


def test_the_registry_is_the_four_documented_commands() -> None:
    """tui.md §3.3a's list, in the order the user meets them - which is also the
    order the popup lists them in, so the most destructive one is last."""
    assert [command.name for command in COMMANDS] == ["help", "new", "abort", "yolo"]
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
    assert hint == "/help, /new, /abort, or /yolo"  # an English list, not a dump


def test_match_prefix_narrows_as_the_user_types() -> None:
    assert match_prefix("/") == COMMANDS  # a bare slash offers everything
    assert [c.name for c in match_prefix("/y")] == ["yolo"]
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
