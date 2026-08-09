"""CommandPopup: the slash-command list that pops up above the chat box.

Discovery, not decoration: the commands of `tui.md` §3.3a are otherwise only
findable by already knowing `/help` exists. Typing `/` puts the whole list one
keystroke away, and each further character narrows it.

Two deliberate shapes here.

It is a single ``Static`` painting a Rich ``Text``, not a list of child widgets
and not an ``OptionList``. One renderable means the highlight moves within a
frame (no mount/unmount round-trip to await), and it means the popup can never
take focus - which matters, because focus must stay in the composer the entire
time. Navigation therefore arrives as method calls (:meth:`move`,
:meth:`highlighted`) from ``ChatComposer``'s key interception rather than as key
events of its own; this widget knows nothing about keys.

One command is always exactly one row, which is what makes the popup's height
its match count and "the third row is highlighted" mean what it looks like. A
summary wider than the chat column (`/abort`'s is, at every size we run at) is
therefore cut rather than wrapped - by ``text-wrap: nowrap; text-overflow:
ellipsis`` on ``#cmd-popup``, since Textual re-wraps a widget's renderable to
its own width and the Rich ``Text``'s own settings do not survive that. Wrapping
would not merely look untidy: the second row would push the last command out
from under the popup's ``max-height`` and `/help` would silently not be there.

**A list the user has not narrowed highlights nothing.** ``preselect`` is how
the composer says whether a row may start out chosen, and a bare ``/`` says no:
with an unconditional top-row highlight, slash-Enter-Enter *ran the first
command* - two keystrokes past a character typed by accident, at a box whose
whole job is sending text. Enter with nothing highlighted completes nothing (it
is still swallowed, so it cannot send the bare slash either), one arrow press or
one typed letter arms a row, and everything downstream is unchanged.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from agentclip.app.commands import ChatCommand

_ARROW = "▸"  # the highlight marker; BMP-only, like every glyph we ship


class CommandPopup(Static):
    """The filtered command list, hidden unless a command is being typed."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__("", id=id)
        self._matches: tuple[ChatCommand, ...] = ()
        self._index: int | None = None

    def on_mount(self) -> None:
        self.display = False

    # -- state ----------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True while the popup is up - the one flag the composer's keys read."""
        return bool(self._matches) and self.display

    @property
    def matches(self) -> tuple[ChatCommand, ...]:
        """The commands currently listed, top to bottom."""
        return self._matches

    @property
    def index(self) -> int | None:
        """Which row is highlighted (0-based), or None while none is.

        None is the resting state of an unnarrowed list, not an error: the user
        has opened the menu without saying which entry they want, and nothing
        should be one keystroke from running.
        """
        return self._index

    @property
    def highlighted(self) -> ChatCommand | None:
        """The command Enter/Tab would complete, or None if there is no choice
        to complete - either the popup is closed or no row is highlighted."""
        if not self.is_open or self._index is None:
            return None
        return self._matches[self._index]

    # -- driven by the composer -----------------------------------------------

    def show(self, matches: tuple[ChatCommand, ...], *, preselect: bool) -> None:
        """List ``matches``, or close if there are none.

        The highlight survives a repaint of the *same* list (a redundant refresh
        must not throw away the user's arrow presses) and is re-decided whenever
        the filter actually changed, because the old position means nothing
        against a different set of commands. ``preselect`` is the caller's
        answer to "has the user narrowed this at all?" - True puts the highlight
        on the top row, False leaves the list showing but unarmed.
        """
        if not matches:
            self.hide()
            return
        if matches != self._matches:
            self._matches = matches
            self._index = 0 if preselect else None
        self.display = True
        self._paint()

    def hide(self) -> None:
        """Close the popup and forget the highlight."""
        self._matches = ()
        self._index = None
        self.display = False

    def move(self, delta: int) -> None:
        """Move the highlight, wrapping at both ends (up from the top is the
        fastest way to the last command in a short list).

        From *no* highlight, an arrow press is what arms the list: down picks
        the first row, up the last - the same thing wrapping would have done
        from either edge.
        """
        if not self.is_open:
            return
        if self._index is None:
            self._index = 0 if delta > 0 else len(self._matches) - 1
        else:
            self._index = (self._index + delta) % len(self._matches)
        self._paint()

    # -- painting -------------------------------------------------------------

    def _paint(self) -> None:
        text = Text()  # one line per command; #cmd-popup's CSS keeps it that way
        for row, command in enumerate(self._matches):
            if row:
                text.append("\n")
            selected = row == self._index
            text.append(f"{_ARROW} " if selected else "  ")
            text.append(command.label, style="bold reverse" if selected else "bold")
            text.append(f"  {command.summary}", style="" if selected else "dim")
        self.update(text)
