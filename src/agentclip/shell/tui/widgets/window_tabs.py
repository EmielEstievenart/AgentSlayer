"""WindowTabs: the two-row tab bar whose tabs ARE browser windows (tui.md 1.6).

A tab used to be one *session view* - a transcript that appeared when a
sub-agent ran and vanished with the session. It is now one *browser window*
AgentClip drives, which is a much longer-lived thing: the window exists before
any session does, keeps its own service and its own drawn rectangle, and is
still there after `/new`. Selecting a tab therefore does two things at once -
it shows that window's transcript and it points every per-window control in the
sidebar (the service picker, "Set chat region...") at that window.

Two rows, because the windows are a two-level tree: **row 1 is the master
windows**, **row 2 is the sub-agent windows of whichever master is selected**.
Today that is exactly one of each (`m1` and `m1-s1`), so the bar reads as two
short title lines; the shape is here because the ids, the message and the
rebuild are the whole difference between one-of-each and N-of-each, and getting
them right later would mean re-teaching every caller what a tab is.

**Why not `textual.widgets.Tabs`?** Because two rows would be two independent
`Tabs.active` values fighting over one selection, and the arithmetic does not
work out. A row whose only tab is already active swallows a click silently
(`Tabs._activate_tab` assigns the same value, so the reactive never fires and no
`TabActivated` is posted) - which is precisely the 1x1 case here, where clicking
either row would do nothing at all. Both rows also auto-activate their first tab
on mount, so a naive handler would end up selecting whichever row mounted last.
A tab strip that owns ONE selection across both rows is a dozen lines and has
neither problem.

Deliberately not focusable: the composer is the one thing on this screen that
wants focus, and a tab that steals it on click (as `Tabs` does) costs the user a
click to get it back. Keyboard navigation is `F6` on the screen instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

# The row ids, so the screen (and its tests) can reach either strip by name.
MASTER_ROW_ID = "win-row-master"
SUB_ROW_ID = "win-row-sub"


def tab_id(window: str) -> str:
    """The widget id of ``window``'s tab. One place, because tests, CSS and the
    click handler all have to agree on it."""
    return f"win-{window}"


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """One browser window in the bar, plus the sub-agent windows hanging off it.

    ``subs`` is only ever populated on a master: a sub-agent window cannot open
    a sub-agent window of its own (nesting is excluded by construction, see
    protocol.md 3), so the tree is exactly two levels deep.
    """

    id: str
    label: str
    subs: tuple[WindowSpec, ...] = field(default_factory=tuple)


class WindowTab(Static):
    """One clickable window tab. Reports the click and nothing else."""

    class Picked(Message):
        """The user clicked this tab. ``WindowTabs`` turns it into a selection."""

        def __init__(self, window: str) -> None:
            self.window = window
            super().__init__()

    def __init__(self, window: str, label: str) -> None:
        super().__init__(id=tab_id(window), classes="window-tab")
        self.window = window
        self._label = label
        self.update(Text(label))

    @property
    def label_text(self) -> str:
        """The tab's undecorated text - the assertion surface for the state
        glyphs (``▶``/``✓``) the screen writes into it."""
        return self._label

    def set_label(self, label: str) -> None:
        self._label = label
        self.update(Text(label))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Picked(self.window))


class WindowTabs(Vertical):
    """The two-row strip. Owns exactly one selection across both rows."""

    DEFAULT_CSS = """
    WindowTabs {
        height: auto;
    }
    WindowTabs .window-row {
        height: 1;
        width: 1fr;
    }
    WindowTabs #win-row-sub {
        /* Indented, so the second row reads as "belonging to" the first rather
           than as a second, equal set of windows. */
        padding-left: 3;
    }
    WindowTabs .window-tab {
        width: auto;
        height: 1;
        padding: 0 2;
        color: $foreground 45%;
    }
    WindowTabs .window-tab:hover {
        color: $foreground 75%;
    }
    WindowTabs .window-tab.-selected {
        color: $foreground;
        text-style: bold;
        background: $panel;
    }
    """

    class WindowSelected(Message):
        """The user picked a window tab.

        Posted ONLY for a click. Programmatic moves go through ``select()``,
        whose caller already knows what it asked for - re-announcing them would
        make every "show this window" call reentrant.
        """

        def __init__(self, window: str) -> None:
            self.window = window
            super().__init__()

    def __init__(self, masters: Sequence[WindowSpec], *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        if not masters:
            raise ValueError("WindowTabs needs at least one master window")
        self._masters = tuple(masters)
        self._selected = self._masters[0].id
        self._selected_master = self._masters[0].id

    def compose(self) -> ComposeResult:
        with Horizontal(id=MASTER_ROW_ID, classes="window-row"):
            for spec in self._masters:
                yield WindowTab(spec.id, spec.label)
        with Horizontal(id=SUB_ROW_ID, classes="window-row"):
            for spec in self._master(self._selected_master).subs:
                yield WindowTab(spec.id, spec.label)

    def on_mount(self) -> None:
        self._paint_selection()

    # -- the window tree ------------------------------------------------------

    def _master(self, window: str) -> WindowSpec:
        for spec in self._masters:
            if spec.id == window:
                return spec
        raise KeyError(window)

    def master_of(self, window: str) -> str:
        """Which master window ``window`` hangs off (itself, for a master)."""
        for spec in self._masters:
            if spec.id == window or any(sub.id == window for sub in spec.subs):
                return spec.id
        raise KeyError(window)

    def order(self) -> list[str]:
        """The windows F6 cycles, in the order they are on screen: every master,
        then the sub-agent windows of the selected one."""
        return [spec.id for spec in self._masters] + [
            sub.id for sub in self._master(self._selected_master).subs
        ]

    # -- selection ------------------------------------------------------------

    @property
    def selected(self) -> str:
        return self._selected

    def select(self, window: str) -> None:
        """Show ``window`` as the selected tab (no message - see WindowSelected).

        Selecting a sub-agent window of a *different* master also re-roots the
        second row on that master, which is the one piece of N-master mechanics
        that cannot be deferred: the row's contents are a function of the
        selection, not a fixed list.
        """
        master = self.master_of(window)  # raises KeyError on an unknown window
        self._selected = window
        if master != self._selected_master:
            self._selected_master = master
            self._rebuild_sub_row()
        self._paint_selection()

    def _rebuild_sub_row(self) -> None:
        row = self.query_one(f"#{SUB_ROW_ID}", Horizontal)
        row.remove_children()
        row.mount_all(
            WindowTab(spec.id, spec.label) for spec in self._master(self._selected_master).subs
        )

    def _paint_selection(self) -> None:
        for tab in self.query(WindowTab):
            tab.set_class(tab.window == self._selected, "-selected")

    def on_window_tab_picked(self, message: WindowTab.Picked) -> None:
        message.stop()
        if message.window == self._selected:
            # Re-announced anyway: the screen may have moved the transcript's
            # focus elsewhere since, and "click the tab I am already on" has to
            # remain a way to say "show me this window".
            self.post_message(self.WindowSelected(message.window))
            return
        self.select(message.window)
        self.post_message(self.WindowSelected(message.window))

    # -- labels ---------------------------------------------------------------

    def tab(self, window: str) -> WindowTab:
        return self.query_one(f"#{tab_id(window)}", WindowTab)

    def set_label(self, window: str, label: str) -> None:
        """Repaint one tab's text - the window's name plus whatever live state
        the screen wants on it (a running sub-agent, its service key)."""
        self.tab(window).set_label(label)
