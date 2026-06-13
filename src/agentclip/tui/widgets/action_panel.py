"""ActionPanel: the bottom-drawer approval gate (tui.md sections 2, 5).

Hidden when idle. At a gate it renders the precomputed ``PendingAction.preview``
(unified diff via rich.syntax.Syntax with the pygments ``diff`` lexer - no
textual[syntax] extra; full highlighted content under a NEW FILE banner for
brand-new files; the literal command line for run_command) inside a boldly
bordered drawer with big Approve / Reject buttons, so the prompt is unmissable.

The buttons emit a single :class:`ActionPanel.Decision` message; the MainScreen
owns all key handling and resolves the gate. ``ask_user`` answering lives on the
persistent chat composer now, not here - this widget is approval-only.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Static

from agentclip.engine.engine import PendingAction


def preview_renderable(action: PendingAction) -> RenderableType:
    """The Rich renderable for one gated call's preview."""
    if action.kind == "command":
        command = action.call.params.get("command", "")
        text = Text()
        text.append(f"$ {command}\n\n", style="bold")
        text.append("not on the allowlist - approve to run once in the project root", style="dim")
        timeout = action.call.params.get("timeout")
        if timeout:
            text.append(f"\ntimeout: {timeout}s", style="dim")
        return text
    preview = action.preview
    first, _, rest = preview.partition("\n")
    if first.startswith("NEW FILE"):
        path = action.call.params.get("path", "file.txt")
        lexer = Syntax.guess_lexer(path, code=rest)
        return Group(
            Text(first, style="bold green"),
            Syntax(rest, lexer, theme="ansi_dark", line_numbers=True, word_wrap=False),
        )
    if preview.lstrip().startswith(("---", "+++", "@@")):
        return Syntax(preview, "diff", theme="ansi_dark", word_wrap=False)
    return Text(preview)


class ActionPanel(Vertical):
    class Decision(Message):
        """A button in the approval drawer was pressed."""

        def __init__(self, choice: str) -> None:  # "approve" | "approve_edits" | "reject"
            self.choice = choice
            super().__init__()

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self.current_action: PendingAction | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="action-title")
        yield Static("", id="action-queue")
        with VerticalScroll(id="action-body"):
            yield Static("", id="action-content")
        with Horizontal(id="action-buttons"):
            yield Button("Approve  (y)", id="approve-btn", variant="success")
            yield Button("Approve + auto-edits  (a)", id="approve-edits-btn", variant="primary")
            yield Button("Reject  (n)", id="reject-btn", variant="error")
        with Horizontal(id="action-footer"):
            yield Static("", id="action-hints")
            yield Input(
                placeholder="optional reason - enter to send, esc to cancel",
                id="reject-reason",
            )

    def on_mount(self) -> None:
        self.display = False
        self.query_one("#reject-reason").display = False

    # -- approval mode --------------------------------------------------------

    def show_approval(self, action: PendingAction, position: str, queue: str) -> None:
        self.current_action = action
        target = action.call.params.get("path") or action.call.params.get("command", "")
        title = f"APPROVE  ·  call {position}  ·  {action.call.tool} {target}".rstrip()
        self.query_one("#action-title", Static).update(Text(title))
        self.query_one("#action-queue", Static).update(Text(queue))
        self.query_one("#action-content", Static).update(preview_renderable(action))
        is_edit = action.kind == "edit"
        self.query_one("#approve-edits-btn", Button).display = is_edit
        hints = "press y to approve · n to reject"
        if is_edit:
            hints += " · a to approve + auto-accept edits this session"
        self.query_one("#action-hints", Static).update(hints)
        self.query_one("#reject-reason").display = False
        self.display = True

    def focus_default(self) -> None:
        """Move focus onto the Approve button so y/n/a bubble to the screen."""
        if self.is_mounted:
            self.query_one("#approve-btn", Button).focus()

    def hide_panel(self) -> None:
        if not self.is_mounted:
            return
        self.current_action = None
        self.display = False

    # -- reject reason input --------------------------------------------------

    def open_reject_input(self) -> None:
        box = self.query_one("#reject-reason", Input)
        box.value = ""
        box.display = True
        box.focus()

    def close_reject_input(self) -> None:
        self.query_one("#reject-reason", Input).display = False
        if self.display:
            self.focus_default()

    # -- buttons --------------------------------------------------------------

    @on(Button.Pressed, "#approve-btn")
    def _on_approve(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Decision("approve"))

    @on(Button.Pressed, "#approve-edits-btn")
    def _on_approve_edits(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Decision("approve_edits"))

    @on(Button.Pressed, "#reject-btn")
    def _on_reject(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Decision("reject"))
