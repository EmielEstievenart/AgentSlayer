"""HelpScreen: static key/flow cheatsheet (F1 or ?).

The command section is the one part that is NOT static: it is rendered from
``agentclip.shell.app.commands.COMMANDS``, the same tuple the popup, the `/help` note
and the unknown-command hint come from. It used to be a fifth hand-written copy
of that list, which is exactly how a help screen ends up documenting a key that
moved two releases ago - and a test pins the two together.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from agentclip.shell.app.commands import COMMANDS

# Width of the command column, so the summaries line up under each other.
_COMMAND_COLUMN = 16


def commands_block() -> str:
    """The chat-command section's body, one indented row per registry entry."""
    return "\n".join(
        f"  {command.label:<{_COMMAND_COLUMN}}{command.summary}" for command in COMMANDS
    )


def help_text() -> str:
    """The whole cheatsheet. A function, not a constant, because the command
    rows are read off the registry every time it is shown."""
    return f"""\
Chat box (bottom of the screen)
  Type a message and press Enter to send it to the model.
  Ctrl+J inserts a newline; pasting keeps its newlines.
  Esc clears the box (Ctrl+Z puts the text back); Esc on an EMPTY box frees the
  single-key shortcuts below - press t (or click) to type again.

Chat commands (type in the chat box, leading slash)
{commands_block()}
  Typing "/" pops the list up above the box; each further character narrows it.
  Nothing is highlighted until you press a letter or an arrow - then Enter (or
  Tab) COMPLETES the highlighted row and the next Enter sends it. Esc closes the
  list without touching your text. "//text" sends a literal leading slash.

Window tabs (top of the chat column) - a tab is a BROWSER WINDOW, not a session
  Row 1 is the master window - the chat you steer. Row 2 is the sub-agent
  window a delegated sub-task runs in. Each carries its own service (shown on
  the tab), its own drawn rectangle, and its own transcript that simply
  accumulates: a delegated run appends a divider and its output rather than
  minting a tab, so one window is one scroll of everything that happened in it.
  F6 (or a click) selects the next tab. Selecting only changes what YOU see and
  what the sidebar configures - never where output lands, and never which
  window the automation is driving.

Sub-agents (only when the sub-agent window is calibrated, see the sidebar)
  The model can hand one bounded sub-task to a fresh sub-agent in its own chat.
  The sub-agent tab shows ▶ while it runs, ✓ when it handed a result back and ✗
  when it ended without one; the status bar goes magenta and the approval box
  says "SUB-AGENT" - you still approve every edit and command.
  ctrl+x cancels the calls running right now; /abort ends the whole run.

Approval (the bordered box above the chat)
  y  approve      n  reject (optional reason)      a  approve + auto-accept edits
  ...or click the Approve / Reject buttons. (a / auto-accept never runs commands.)

Permission mode (bottom-left of the status bar, e.g. MODE:ask)
  shift+tab cycles it: ask -> plan -> unattended -> ask (/mode names one directly).
  ask         approvals as usual - you are asked about every edit and command.
  plan        every edit and command is REFUSED, so the model can only read and
              propose. Switch back to ask when you want the plan carried out.
  unattended  anything that would have asked you is refused instead - for when
              you walk away. Allowed things still run; nothing waits on you.
  The key works while you are typing and while a turn is running.

Session  (press Esc first if the chat box has focus - twice if you have typed)
  u  undo last turn (confirm; copies a revert notice for the model)
  c  re-copy the last outbound payload    i  force-ingest the clipboard now
     press c TWICE (quickly) and AgentClip also pastes it into the chat for
     you, exactly as it does when it sends a payload of its own
  r  re-send this service's extra instructions with the next message (only
     shown when the service has any - set them with F2). The chat may drift
     back to mangling code halfway through a long session; this reminds it.
  w  pause/resume the clipboard watcher   t  jump to the chat box
  e  end session / show the summary       x  expand the last collapsed output
  l  export the full chat log to a file (raw blocks + payloads, for debugging)
  F6 select the next window tab (see above)
  ctrl+x  cancel the tool calls running now (the run panel). The running
          command is killed, later calls are skipped, and the model is told -
          the results are copied out as usual, so the turn ends cleanly.
  ctrl+o  show/hide what the running command is printing, live, under the list
          of this turn's calls (clicking the panel does the same). The list
          itself is always there while a turn runs: one row per call, the
          running one marked, the rest queued behind it.

App
  F1 or ?  this help
  F2  service profiles: sizes, what each service LOOKS like (the captures the
      automation clicks by), and which finish signals it may watch for
  F3  hide/show the sidebar     F4  appearance (themes)
  F5  ARM / DISARM the tool (also /armed). Disarmed it still watches and shows
      everything - detection, the crops, the STATE rail - but never clicks,
      pastes, moves your mouse or watches your clipboard. Payloads still land
      on the clipboard for you to paste; press i to ingest a reply by hand.
  F7  hide/show ELEMENTS - close-ups of the send/busy/idle/copy pictures the
      automation is recognising right now, in the window it is driving
  F8  hide/show the HARNESS DECISION LOG along the bottom (also /log) - every
      move the loop makes and WHY, as it happens. Scroll up and it holds still;
      scroll back to the bottom and it follows the newest line again.
  ctrl+p   command palette  ctrl+q  quit (confirms when a turn is mid-flight)

The loop: AgentClip copies a payload - paste it into your chat and send.
Click the reply's Copy button; AgentClip detects it, shows what's running,
gates edits/commands, then copies the combined results - paste them back.
Repeat until the model sends task_done."""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "close", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static("AGENTCLIP HELP", classes="title")
            yield Static(Text(help_text()))
            yield Static("escape close", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)
