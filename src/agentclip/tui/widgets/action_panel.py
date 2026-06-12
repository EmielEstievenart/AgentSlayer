"""ActionPanel: the bottom-drawer approval gate / ask_user box (tui.md 2, 5, 9).

Hidden when idle. Approval mode renders the precomputed PendingAction.preview
(unified diff via rich.syntax.Syntax with the pygments ``diff`` lexer - no
textual[syntax] extra; full highlighted content under a NEW FILE banner for
brand-new files; the literal command line for run_command). Question mode
reveals a TextArea for the ask_user answer. The MainScreen owns all key
handling; this widget only renders and exposes its inputs.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static, TextArea

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
    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self.current_action: PendingAction | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="action-title")
        yield Static("", id="action-queue")
        with VerticalScroll(id="action-body"):
            yield Static("", id="action-content")
        with Horizontal(id="action-footer"):
            yield Static("", id="action-hints")
            yield Input(
                placeholder="optional reason - enter to send, esc to cancel",
                id="reject-reason",
            )
        yield TextArea(id="answer")

    def on_mount(self) -> None:
        self.display = False
        self.query_one("#reject-reason").display = False
        self.query_one("#answer").display = False

    # -- modes ----------------------------------------------------------------

    def show_approval(self, action: PendingAction, position: str, queue: str) -> None:
        self.current_action = action
        target = action.call.params.get("path") or action.call.params.get("command", "")
        title = f"APPROVE · call {position} · {action.call.tool} {target}".rstrip()
        self.query_one("#action-title", Static).update(Text(title))
        self.query_one("#action-queue", Static).update(Text(queue))
        self.query_one("#action-content", Static).update(preview_renderable(action))
        hints = "y approve · n reject"
        if action.kind == "edit":
            hints += " · a approve + auto-accept edits"
        self.query_one("#action-hints", Static).update(hints)
        self.query_one("#reject-reason").display = False
        self.query_one("#answer").display = False
        self.display = True
        self.query_one("#action-body").focus()

    def show_question(self, question: str) -> None:
        self.current_action = None
        self.query_one("#action-title", Static).update(Text("ANSWER · the model asks:"))
        self.query_one("#action-queue", Static).update("")
        self.query_one("#action-content", Static).update(Text(question))
        self.query_one("#action-hints", Static).update("type your answer · ctrl+enter send")
        self.query_one("#reject-reason").display = False
        answer = self.query_one("#answer", TextArea)
        answer.load_text("")
        answer.display = True
        self.display = True
        answer.focus()

    def hide_panel(self) -> None:
        if not self.is_mounted:
            return
        self.current_action = None
        self.display = False

    # -- inputs ----------------------------------------------------------------

    def open_reject_input(self) -> None:
        box = self.query_one("#reject-reason", Input)
        box.value = ""
        box.display = True
        box.focus()

    def close_reject_input(self) -> None:
        self.query_one("#reject-reason", Input).display = False
        if self.display:
            self.query_one("#action-body").focus()

    def answer_text(self) -> str:
        return self.query_one("#answer", TextArea).text
