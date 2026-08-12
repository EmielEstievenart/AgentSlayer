"""ChatComposer: the persistent chat input docked at the bottom of MainScreen.

A small ``TextArea`` subclass that *sends on Enter* (chat convention) instead of
inserting a newline, so the steady-state loop reads like any chat app: type a
message, press Enter. Multi-line input still works two ways - pasting preserves
newlines (paste is a Paste event, not a stream of Enter keypresses) and ``ctrl+j``
inserts a literal newline. ``escape`` is two-stage: with text in the box it
clears the box (keeping focus, so the rewrite starts immediately), and on an
already-empty box it blurs, so the main screen's single-key shortcuts
(u/c/i/w/e/x) become reachable again ("command mode"). Clearing is an *edit*, so
``ctrl+z`` gives the message straight back - a key that can throw away a
paragraph of typing must never be the last word on it.

It also drives the slash-command popup (``CommandPopup``, §3.3a): the box is the
only thing that knows what has been typed, so it decides when the popup is up
and it owns the four keys that mean something different while it is. Enter is
the interesting one - it *completes* instead of sending, which is safe precisely
because completing appends a trailing space and a space closes the popup, so the
very next Enter sends as it always did. It is only safe, though, while there is
something the user actually chose: a bare ``/`` lists every command but arms
none of them (``preselect``), so slash-Enter-Enter runs nothing at all.

The popup is a sibling widget rather than a child: this is a TextArea, and the
list has to render *above* the box. The composer finds it on the screen instead
of holding a reference, which keeps ``MainScreen`` free to lay both out where it
likes and keeps this widget usable (popup-less) without one.

``verbatim`` is the suppression switch MainScreen sets: while the next send is
consumed literally - an answer to the model's ``ask_user`` - a leading slash is
TEXT, not a command, so no popup may appear.

The MainScreen owns every bit of routing; this widget only emits ``Submitted``.
"""

from __future__ import annotations

from contextlib import suppress

from textual import events, on
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import TextArea

from agentclip.app.commands import ChatCommand, match_prefix
from agentclip.tui.widgets.command_popup import CommandPopup


class ChatComposer(TextArea):
    """A chat-style input: Enter sends, ctrl+j newline, esc clears then blurs."""

    class Submitted(Message):
        """Posted when the user presses Enter in the composer."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    _verbatim: bool = False  # class-level default; MainScreen sets it per mode

    # -- slash-command popup ---------------------------------------------------

    @property
    def verbatim(self) -> bool:
        """True while the next send is taken literally (an ``ask_user`` answer)."""
        return self._verbatim

    @verbatim.setter
    def verbatim(self, value: bool) -> None:
        self._verbatim = value
        self.sync_popup()

    @property
    def popup(self) -> CommandPopup | None:
        """The command list mounted alongside us, if the screen has one."""
        if not self.is_mounted:
            return None
        with suppress(NoMatches):
            return self.screen.query_one(CommandPopup)
        return None

    def sync_popup(self) -> None:
        """Match the popup to what is in the box right now.

        The single place the popup's visibility is decided, so every route into
        the text - typing, pasting, a completion, ``reset``, the screen loading a
        draft - lands on the same rule. Called on every ``Changed`` and whenever
        MainScreen re-evaluates the box's mode.
        """
        popup = self.popup
        if popup is None:
            return
        if self._verbatim or self.disabled:
            popup.hide()
            return
        # A bare "/" lists everything but arms nothing: the user has typed no
        # letter, so there is no command they can be said to have reached for,
        # and a pre-selected top row would put "/" + Enter + Enter one careless
        # moment away from running it. One typed character (or one arrow press,
        # which the popup handles itself) is what makes a row the answer.
        popup.show(match_prefix(self.text), preselect=len(self.text) > 1)

    @on(TextArea.Changed)
    def _text_changed(self, event: TextArea.Changed) -> None:
        self.sync_popup()

    def _complete(self, command: ChatCommand) -> None:
        """Replace the half-typed command with the real one, ready for its argument.

        The trailing space is load-bearing twice over: it is where `/yolo on` is
        typed next, and it is what closes the popup (a line with whitespace is no
        longer a command in progress), which is what makes the following Enter a
        plain send again.
        """
        self.load_text(f"{command.slash} ")
        self.move_cursor(self.document.end)
        popup = self.popup
        if popup is not None:
            popup.hide()

    async def _on_key(self, event: events.Key) -> None:
        # While the popup is up it owns four keys, and only those four: the
        # arrows pick a row, Enter/Tab complete it, Escape dismisses the list
        # without touching the text. Everything else keeps editing (and each
        # edit re-filters the list underneath).
        popup = self.popup
        if popup is not None and popup.is_open:
            if event.key in ("up", "down"):
                event.stop()
                event.prevent_default()
                popup.move(-1 if event.key == "up" else 1)
                return
            if event.key in ("enter", "tab"):
                command = popup.highlighted
                event.stop()
                event.prevent_default()
                # No highlight (a bare "/" nobody has narrowed) completes
                # nothing - and, just as importantly, still does not SEND: the
                # key is swallowed either way, so the list stays up waiting to
                # be narrowed instead of the box firing off a lone slash.
                if command is not None:
                    self._complete(command)
                return
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                popup.hide()  # the box keeps its text AND its focus
                return
        # Enter sends (TextArea's default would insert "\n"); ctrl+j keeps the
        # literal-newline escape hatch. Everything else falls through to the
        # normal TextArea editing keys.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if event.key == "ctrl+j":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if self.text:
                # Stage one: empty the box but KEEP the focus, so the cursor is
                # already sitting where the rewrite gets typed. Deliberately an
                # edit and not ``load_text``/``reset`` - those throw the undo
                # history away, and a single key that can destroy a paragraph of
                # typing has to be one ctrl+z from handing it back. The
                # checkpoint is what makes that undo restore *exactly* what was
                # there: without it the clear can be batched with the last few
                # keystrokes, and ctrl+z would return a half-typed line.
                self.history.checkpoint()
                self.clear()
                return
            # Stage two: an empty box has nothing to lose, so escape means what
            # it always did - drop to the screen's single-key "command mode".
            self.screen.set_focus(None)
            return
        await super()._on_key(event)

    def reset(self) -> None:
        """Clear the box after a message is sent.

        Deliberately ``load_text`` and not the undoable clear escape uses: this
        drops the undo history, and a message that has already *left* is not one
        ctrl+z should be able to resurrect into the box behind it.
        """
        self.load_text("")
