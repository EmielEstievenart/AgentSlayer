"""ActionPanel: the bottom-drawer approval gate (tui.md sections 2, 5).

Hidden when idle. At a gate it renders the precomputed ``PendingAction.preview``
(unified diff via rich.syntax.Syntax with the pygments ``diff`` lexer - no
textual[syntax] extra; full highlighted content under a NEW FILE banner for
brand-new files; the literal command line plus the model's stated reason for
run_command) inside a boldly
bordered drawer with big Approve / Reject buttons, so the prompt is unmissable.

The middle button is whichever "stop asking me" answer this session has: the
edits-only auto-accept, or - under a permission ruleset - "Always: <pattern>",
naming exactly what pressing it would remember (engine/approval.py).

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
from agentclip.tools.shell import reason_line


def preview_renderable(action: PendingAction) -> RenderableType:
    """The Rich renderable for one gated call's preview."""
    if action.kind == "command":
        # ONLY run_command's own preview comes from its `command` param. Any
        # other command-kind tool (mcp today) must render the engine-computed
        # preview: params are model-authored, and a decoy `command: git status`
        # riding an mcp call would otherwise repaint the gate as a harmless
        # shell line. The verdict path already ignores the decoy
        # (approval.py resolves the key via target()); the display has to tell
        # the same story.
        if action.call.tool != "run_command":
            text = Text()
            text.append(f"{action.preview}\n", style="bold")
            reason = reason_line(action.call)
            if reason:
                text.append(f"{reason}\n", style="italic")
            # The one-line preview clips args at 120 chars, and for mcp the
            # args ARE the semantics - show them in full here, where the
            # drawer body scrolls, so nothing rides below the fold of a "…".
            args = action.call.params.get("args", "")
            if args:
                text.append("\nargs:\n", style="bold")
                text.append(f"{args}\n")
            text.append("\n")
            text.append("no rule allows this - approve to run it once", style="dim")
            return text
        command = action.call.params.get("command") or action.preview
        text = Text()
        text.append(f"$ {command}\n", style="bold")
        # The model's own one-line justification, right under the command it
        # justifies: approving is a judgement about intent, not just syntax.
        reason = reason_line(action.call)
        if reason:
            text.append(f"{reason}\n", style="italic")
        text.append("\n")
        text.append(
            "no rule allows this - approve to run it once"
            if action.always_pattern is not None
            else "not on the allowlist - approve to run once in the project root",
            style="dim",
        )
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

    def show_approval(
        self, action: PendingAction, position: str, queue: str, prefix: str = ""
    ) -> None:
        """``prefix`` labels whose call this is - empty for the conversation the
        user started, ``"SUB-AGENT ‹title› · "`` while a delegated run is what
        is asking. It is the title line's job because the diff below it looks
        identical either way."""
        self.current_action = action
        # The title's target must be the param the VERDICT was computed from,
        # per tool - not "whatever params exist": an mcp call carrying a decoy
        # `command:` or `path:` param must not retitle the gate (same decoy the
        # preview body guards against, see preview_renderable).
        params = action.call.params
        if action.call.tool == "run_command":
            target = params.get("command", "")
        elif action.call.tool == "mcp":
            target = params.get("tool", "")
        else:
            target = params.get("path", "")
        title = f"{prefix}APPROVE  ·  call {position}  ·  {action.call.tool} {target}".rstrip()
        self.query_one("#action-title", Static).update(Text(title))
        self.query_one("#action-queue", Static).update(Text(queue))
        self.query_one("#action-content", Static).update(preview_renderable(action))
        # The middle button is the "stop asking me about these" answer. Under a
        # permission ruleset it is offered for every gated call and names the
        # exact pattern it would remember - a button that says "always allow"
        # without saying always allow WHAT is not something to press blind.
        always = action.always_pattern
        is_edit = action.kind == "edit"
        button = self.query_one("#approve-edits-btn", Button)
        button.display = always is not None or is_edit
        hints = "press y to approve · n to reject"
        if always is not None:
            # A bare "*" is the whole permission key (all edits, all reads...);
            # spelling it out beats a button labelled with one asterisk.
            what = "calls like this one" if always == "*" else always
            button.label = f"Always: {what}  (a)"
            hints += f" · a to always allow {what} (until AgentClip restarts)"
        else:
            button.label = "Approve + auto-edits  (a)"
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
