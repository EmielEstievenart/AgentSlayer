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

Long summaries ellipsize rather than wrap (``no_wrap`` + ``overflow="ellipsis"``)
so one command is always exactly one row: the popup's height is then the number
of matches, which is what keeps it compact at the narrow widths the Pilot suites
run at, and what makes "the third row is highlighted" mean what it looks like.
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
        self._index = 0

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
    def index(self) -> int:
        """Which row is highlighted (0-based)."""
        return self._index

    @property
    def highlighted(self) -> ChatCommand | None:
        """The command Enter/Tab would complete, or None while closed."""
        if not self.is_open:
            return None
        return self._matches[self._index]

    # -- driven by the composer -----------------------------------------------

    def show(self, matches: tuple[ChatCommand, ...]) -> None:
        """List ``matches``, or close if there are none.

        The highlight survives a repaint of the *same* list (a redundant refresh
        must not throw away the user's arrow presses) and resets to the top
        whenever the filter actually changed, because the old position means
        nothing against a different set of commands.
        """
        if not matches:
            self.hide()
            return
        if matches != self._matches:
            self._matches = matches
            self._index = 0
        self.display = True
        self._paint()

    def hide(self) -> None:
        """Close the popup and forget the highlight."""
        self._matches = ()
        self._index = 0
        self.display = False

    def move(self, delta: int) -> None:
        """Move the highlight, wrapping at both ends (up from the top is the
        fastest way to the last command in a short list)."""
        if not self.is_open:
            return
        self._index = (self._index + delta) % len(self._matches)
        self._paint()

    # -- painting -------------------------------------------------------------

    def _paint(self) -> None:
        text = Text(no_wrap=True, overflow="ellipsis")
        for row, command in enumerate(self._matches):
            if row:
                text.append("\n")
            selected = row == self._index
            text.append(f"{_ARROW} " if selected else "  ")
            text.append(command.label, style="bold reverse" if selected else "bold")
            text.append(f"  {command.summary}", style="" if selected else "dim")
        self.update(text)
