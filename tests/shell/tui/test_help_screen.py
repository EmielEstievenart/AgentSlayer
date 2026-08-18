"""The F1 cheatsheet, pinned to the things it claims to describe.

A help screen is prose, and prose about a moving UI rots silently - the version
this replaced still offered "F2 settings (lands in M3)" three waves after F2
became the whole service-profile editor, still described a tab per delegation
after tabs became browser windows, and carried a fifth hand-written copy of the
command list. So the command section is *rendered* from the registry and pinned
to it here (the same join tests/shell/app/test_commands.py makes for the controller's
dispatch table), and the handful of claims that are checkable are checked.
"""

from __future__ import annotations

from agentclip.shell.app.commands import COMMANDS
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.help import commands_block, help_text
from agentclip.shell.tui.screens.main import MainScreen


def test_the_command_section_is_the_registry() -> None:
    """Every command, with its argument hint and its summary, and nothing that
    is not a command - a row here that no longer dispatches is worse than no
    help at all."""
    rows = [row for row in commands_block().splitlines() if row.strip()]
    assert len(rows) == len(COMMANDS)
    for row, command in zip(rows, COMMANDS, strict=True):
        assert row.strip().startswith(command.label)
        assert command.summary in row


def test_the_help_text_carries_the_command_rows() -> None:
    text = help_text()
    for command in COMMANDS:
        assert command.label in text
        assert command.summary in text


def test_it_documents_the_keys_the_main_screen_actually_binds() -> None:
    """Every single-key shortcut the Session section lists has to exist. The
    reverse is not required - `x` and the F-keys are documented elsewhere in the
    same text - but a key in the cheatsheet that the screen does not bind is a
    dead instruction."""
    # The cheatsheet writes function keys the way keyboards do.
    text = help_text().lower()
    bound = {binding.key for binding in MainScreen.BINDINGS}
    for key in ("u", "c", "i", "w", "t", "e", "l", "x", "ctrl+x", "f6", "f3"):
        assert key in bound, key
        assert key in text, key


def test_it_names_the_screens_the_app_binds_and_no_stale_ones() -> None:
    text = help_text()
    app_keys = {binding.key for binding in AgentClipApp.BINDINGS}
    assert {"f1", "f2", "f4"} <= app_keys
    assert "F2  service profiles" in text
    assert "F4  appearance" in text
    assert "F3  hide/show the sidebar" in text  # MainScreen's, not the app's
    # The three claims the previous version was stale on.
    assert "lands in M3" not in text
    assert "its own tab" not in text  # a delegation appends to a window, not a tab
    assert "browser window" in text.lower()


def test_it_describes_what_the_arrows_do_at_the_chat_box() -> None:
    """Up/Down mean three things in that box depending on what is on screen
    (§3.3d), and the first/last-line rule is the half a user would otherwise
    only discover by pressing one in the middle of a pasted traceback."""
    text = help_text()
    assert "Up/Down walk back through what you have already sent this run" in text
    assert "FIRST/LAST line" in text
    assert "half-way through typing" in text  # the draft comes back


def test_it_describes_the_autocomplete_rules_that_decide_what_enter_does() -> None:
    """The popup changes what Enter means, which is the one thing a user cannot
    work out by looking at the box."""
    text = help_text()
    assert "Nothing is highlighted until you press a letter or an arrow" in text
    assert "COMPLETES" in text and "the next Enter sends it" in text
