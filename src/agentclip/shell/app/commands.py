"""The chat slash-command registry: one table, every consumer.

The commands the composer accepts (`tui.md` §3.3a) used to exist three times
over - the controller's dispatch chain, the `/help` note it prints, and the
"unknown command" hint - so adding one meant editing prose in three places and
the drift showed up as a command nobody could discover. They are declared once
here instead, as plain data:

* :data:`COMMANDS` is the table, in the order the user should meet them;
* :func:`lookup` resolves a typed name (aliases and case included) to its entry;
* :func:`help_text` renders `/help`, and :func:`command_list` the "try …" hint;
* :func:`match_prefix` answers the composer's autocomplete question - which
  commands is this half-typed line reaching for.

It lives in the ``app`` layer, not ``tui``, because the *dispatch* does: the
controller owns the session lifecycle, so any front-end that forwards composer
text inherits the commands. That keeps this module to plain string work - no
Textual import - which is what lets the popup widget (``tui``) and the
controller (``app``) read the very same tuple.

Aliases are deliberately *dispatch-only*: `/commands` and `/?` still run
`/help`, but they are not offered by the popup, which lists one obvious name per
command rather than teaching three spellings of the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatCommand:
    """One slash command: what it is called, what it takes, what it does."""

    name: str
    """Canonical name, without the leading slash (``"yolo"``)."""

    summary: str
    """One line, used verbatim by both `/help` and the autocomplete popup."""

    arg: str = ""
    """Argument hint as the user should type it (``"[on|off]"``), or empty."""

    aliases: tuple[str, ...] = ()
    """Other spellings that dispatch here; never listed to the user."""

    @property
    def slash(self) -> str:
        """The command as typed: ``/yolo``."""
        return f"/{self.name}"

    @property
    def label(self) -> str:
        """The command with its argument hint: ``/yolo [on|off]``."""
        return f"{self.slash} {self.arg}" if self.arg else self.slash


# Order is a safety property, not a style choice. The popup lists this tuple as
# typed, so whatever sits at the top is what one careless arrow press (or a
# future default highlight) lands on - and `/yolo` disables every approval gate
# in the app. It therefore goes LAST, behind the harmless and the reversible,
# with `/help` first because it is the one a lost user is reaching for. The
# popup additionally refuses to pre-select anything until the user has typed at
# least one letter (see :func:`match_prefix`'s callers), so "the first row" is
# not reachable by Enter alone either; this ordering is the second lock on the
# same door.
COMMANDS: tuple[ChatCommand, ...] = (
    ChatCommand(
        name="help",
        summary="list the commands",
        aliases=("commands", "?"),
    ),
    ChatCommand(
        name="new",
        summary="clear the chat and start a new session (in a new browser chat)",
    ),
    ChatCommand(
        name="abort",
        summary="end the sub-agent run in flight (ctrl+x only cancels the calls running now)",
    ),
    ChatCommand(
        name="log",
        summary="show why the harness moved through its recent states",
    ),
    ChatCommand(
        name="mcp",
        summary="list the MCP servers: state, tools, and what went wrong",
    ),
    ChatCommand(
        name="skills",
        summary="list the loaded skills and the folder each came from",
    ),
    ChatCommand(
        name="armed",
        arg="[on|off]",
        summary="toggle whether the tool may touch the screen at all (same as F5)",
    ),
    ChatCommand(
        name="mode",
        arg="[plan|build]",
        summary="set the permission mode (bare /mode says which one you are in)",
    ),
    ChatCommand(
        name="theme",
        arg="[name]",
        summary="set the appearance (bare /theme lists the themes)",
    ),
    ChatCommand(
        name="config",
        arg="[global|local|global reset|local reset]",
        summary="open the permissions/MCP file: bare /config says where both live",
    ),
    # Second-to-last, and for `/yolo`'s reason turned around: it is the other
    # command that answers gates without asking, so it belongs with it at the
    # bottom - but it only ever REFUSES, so it goes above the one that approves.
    ChatCommand(
        name="unattended",
        arg="[on|off]",
        summary="toggle auto-deny for whatever would ask you (you're away from the PC)",
    ),
    ChatCommand(
        name="yolo",
        arg="[on|off]",
        summary="toggle auto-approve-everything",
    ),
)


def lookup(name: str) -> ChatCommand | None:
    """The command a typed name means, or None if there is no such command.

    ``name`` is the bare word after the slash; matching is case-insensitive and
    includes aliases, so ``"Commands"`` finds `/help`."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    for command in COMMANDS:
        if wanted == command.name or wanted in command.aliases:
            return command
    return None


def match_prefix(text: str) -> tuple[ChatCommand, ...]:
    """The commands a composer line is reaching for - empty when none is.

    The autocomplete trigger, kept here so its rules and the dispatch rules
    cannot drift apart. A line qualifies only while it is a single bare token
    starting with one slash: ``"/"`` offers everything, ``"/y"`` narrows to
    `/yolo`, and each of the three ways a line stops being a command in progress
    closes the popup on its own - ``"//escaped"`` is the literal-slash escape
    hatch, ``"/yolo "`` has committed to a command and is typing its argument,
    and ``"/xyz"`` matches nothing at all.
    """
    if not text.startswith("/") or text.startswith("//"):
        return ()
    token = text[1:]
    if any(char.isspace() for char in token):
        return ()
    prefix = token.lower()
    return tuple(command for command in COMMANDS if command.name.startswith(prefix))


def help_text() -> str:
    """The `/help` note: every command, its argument hint and its summary."""
    listing = "  ·  ".join(f"{command.label} - {command.summary}" for command in COMMANDS)
    return f"commands:  {listing}"


def command_list() -> str:
    """The commands as an English list - ``/help, /new, ..., or /yolo``."""
    names = [command.slash for command in COMMANDS]
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])}, or {names[-1]}"
