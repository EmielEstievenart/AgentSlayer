"""HelpScreen: static key/flow cheatsheet (F1 or ?)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

HELP_TEXT = """\
Approval gate
  y  approve the pending call          n  reject (optional reason, enter sends)
  a  approve + auto-accept all further edits this session (never commands)

Session
  u  undo last turn (confirm; copies a revert notice for the model)
  c  re-copy the last outbound payload    i  force-ingest the clipboard now
  w  pause/resume the clipboard watcher   t  send a follow-up message
  e  end session / show the summary       x  expand the last collapsed output

App
  F1 or ?  this help        F2  settings (lands in M3 - edit .agentclip.toml)
  ctrl+p   command palette  ctrl+q  quit (confirms when a turn is mid-flight)

The loop: AgentClip copies a payload - paste it into your chat and send.
Click the reply's Copy button; AgentClip detects it, gates edits/commands,
executes everything, and copies the combined results - paste them back.
Repeat until the model sends task_done."""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "close", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static("AGENTCLIP HELP", classes="title")
            yield Static(Text(HELP_TEXT))
            yield Static("escape close", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)
