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

``up``/``down`` walk what has already been sent (``SendHistory``), which is the
third thing those two keys mean here and the last one tried: the popup gets them
first, the multi-line editor gets them whenever the caret still has somewhere to
go, and only from the top or bottom edge of the document do they recall. That
ordering is the whole design - a pasted traceback must stay navigable, and a
one-line box must feel like every other chat app.

The MainScreen owns every bit of routing; this widget only emits ``Submitted``.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from textual import events, on
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import TextArea

from agentclip.shell.app.commands import ChatCommand, match_prefix
from agentclip.shell.tui.widgets.command_popup import CommandPopup

# How many sends the arrows can reach back through. Session-local and in memory
# only: this is a convenience for retyping the last thing, not a transcript -
# the transcript is the transcript, and `l` exports it.
HISTORY_LIMIT = 50


class SendHistory:
    """What has been sent from the box this run, and where the arrows stand in it.

    Pure state with no widget in it, so the rules that are easy to get subtly
    wrong - what "past the newest" restores, when a duplicate collapses, what
    the cap throws away - are testable without a running app.

    The model is readline's, and the piece worth naming is the DRAFT: browsing
    starts by putting whatever the user had half-typed somewhere safe, and
    walking back down past the newest entry hands it back. That is what makes an
    accidental ``up`` cost nothing, and it is why recall does not need an undo.
    """

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self._entries: list[str] = []  # oldest first
        self._limit = limit
        # None means "not browsing", which is a different state from "browsing
        # the newest": only in the first is `down` the editor's key again.
        self._index: int | None = None
        self._draft = ""

    @property
    def entries(self) -> tuple[str, ...]:
        return tuple(self._entries)

    @property
    def browsing(self) -> bool:
        return self._index is not None

    def push(self, text: str) -> None:
        """Remember a send, and stop browsing - a send ends the walk."""
        self.reset()
        if not text.strip():
            return
        # Consecutive duplicates collapse. Sending the same thing twice in a row
        # is common (a retried `/identify`, a repeated "continue") and it would
        # otherwise cost two presses of `up` to get past one message.
        if self._entries and self._entries[-1] == text:
            return
        self._entries.append(text)
        del self._entries[: -self._limit]  # a no-op until the cap is reached

    def older(self, current: str) -> str | None:
        """The entry before the one on show, or ``None`` when there is none.

        ``None`` is not an error - it is this class declining the key, so the
        caller lets the editor have it. At the oldest entry that is deliberate:
        `up` goes back to meaning "move the caret", which is what the user gets
        in every other box, rather than silently doing nothing.
        """
        if self._index is None:
            if not self._entries:
                return None
            self._draft = current  # only ever captured on the way IN
            self._index = len(self._entries) - 1
        elif self._index == 0:
            return None
        else:
            self._index -= 1
        return self._entries[self._index]

    def newer(self) -> str | None:
        """The entry after the one on show - or, past the newest, the draft.

        ``None`` while not browsing: `down` in a box nobody has walked up from
        is an ordinary caret key and must stay one.
        """
        if self._index is None:
            return None
        if self._index == len(self._entries) - 1:
            draft = self._draft  # read before reset(), which is what clears it
            self.reset()
            return draft  # may be "" - an empty box is a perfectly good draft
        self._index += 1
        return self._entries[self._index]

    def reset(self) -> None:
        """Leave browse mode. What is in the box is now the box's own business."""
        self._index = None
        self._draft = ""


class ChatComposer(TextArea):
    """A chat-style input: Enter sends, ctrl+j newline, esc clears then blurs."""

    class Submitted(Message):
        """Posted when the user presses Enter in the composer."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    _verbatim: bool = False  # class-level default; MainScreen sets it per mode

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history = SendHistory()
        # How many ``Changed`` messages still in flight are OUR doing. Textual
        # posts ``Changed`` rather than raising it, so a boolean set around the
        # ``load_text`` below would already be back to False by the time the
        # message arrived - and the recall would read as a user edit and undo
        # its own browse position on the spot. One recall is exactly one
        # ``load_text`` is exactly one ``Changed``, so counting is honest.
        self._recall_echoes = 0

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
        # Editing is how the user leaves the send history: whatever is in the box
        # after a keystroke is theirs, not the entry they had walked back to, and
        # the next `up` starts again from the newest. Our own recalls are the one
        # exception, and they are counted rather than flagged (see __init__).
        if self._recall_echoes:
            self._recall_echoes -= 1
        else:
            self._history.reset()
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
        # ...and once the popup has had its refusal, up/down walk the sends -
        # but ONLY from the edge of the document. Anywhere else they are the
        # editor's keys, because a pasted traceback has to stay navigable and a
        # key that sometimes moves the caret and sometimes replaces the whole
        # box would be unusable. On a one-line box (the overwhelmingly common
        # case) both edges are the same line, so it behaves like every other
        # chat app with no rule to learn.
        recalled: str | None = None
        if event.key == "up" and self.cursor_at_first_line:
            recalled = self._history.older(self.text)
        elif event.key == "down" and self.cursor_at_last_line:
            recalled = self._history.newer()
        # ``None`` from either is the history DECLINING the key (the oldest entry
        # is reached, or nothing has been sent), and a declined key falls through
        # to the editor rather than being swallowed - an arrow that does nothing
        # at all is worse than one that moves the caret nowhere useful.
        if self._recall(recalled):
            event.stop()
            event.prevent_default()
            return
        # Enter sends (TextArea's default would insert "\n"); ctrl+j keeps the
        # literal-newline escape hatch. Everything else falls through to the
        # normal TextArea editing keys.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.submit()
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

    # -- sending, and the memory of it -----------------------------------------

    def submit(self) -> None:
        """Send what is in the box. The composer's one send door.

        A method rather than two lines in the Enter branch because Enter is not
        the only way in: MainScreen's ``ctrl+s``/``ctrl+enter`` are priority
        bindings that send *without focusing the box*, and a send that skipped
        this would be a hole in the history the arrows walk - the user would
        press `up` and not find the message they had just watched leave.
        """
        self._history.push(self.text)
        self.post_message(self.Submitted(self.text))

    def _recall(self, text: str | None) -> bool:
        """Put a remembered send in the box, caret at the end. False = declined.

        ``load_text`` throws the undo history away, which is the right trade
        here and only here: what a recall replaces is recoverable by walking
        back DOWN to the draft, so the key that overwrites the box is also the
        key that gives it back - a better guarantee than ctrl+z, and one the
        user can find without knowing about ctrl+z.
        """
        if text is None:
            return False
        self._recall_echoes += 1
        self.load_text(text)
        self.move_cursor(self.document.end)
        return True

    def reset(self) -> None:
        """Clear the box after a message is sent.

        Deliberately ``load_text`` and not the undoable clear escape uses: this
        drops the undo history, and a message that has already *left* is not one
        ctrl+z should be able to resurrect into the box behind it.
        """
        self._history.reset()  # the walk ends where the message does
        self.load_text("")
