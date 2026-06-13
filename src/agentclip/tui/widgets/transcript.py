"""TranscriptPanel: one mounted widget per session event (tui.md section 4).

Every newly mounted event widget is ``anchor()``-ed so the panel stays pinned
to the bottom while content streams in (Textual >= 4 semantics) and releases
when the user scrolls up. Children are pruned beyond MAX_EVENTS to bound
layout cost. ``entries`` mirrors every event as plain text - it is the
assertion surface for the Pilot smoke test and a cheap in-memory postmortem.

``log`` is a richer, *unpruned* record of the same events (timestamp, full raw
protocol block, full outbound payload). ``render_log`` turns it into an
AI-paste-friendly markdown document for debugging - the "export chat log"
feature. It is kept separate from ``entries`` precisely so the export survives
the 500-event display prune and carries the verbatim payloads the rendered
widgets drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Static

from agentclip.protocol.types import Outbound, ToolCall


def _fence(body: str) -> str:
    """A backtick fence guaranteed longer than any backtick run inside ``body``.

    Model prose and outbound payloads can themselves contain ``` code fences;
    a fixed three-backtick fence would be closed early by them. This is the
    standard CommonMark trick: count the longest run, fence with one more.
    """
    longest = run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


@dataclass
class LogEvent:
    time: str  # HH:MM:SS, local
    headline: str
    body: str = ""
    fenced: bool = False  # wrap the body in a code fence (verbatim payloads/blocks)


class TranscriptPanel(VerticalScroll):
    MAX_EVENTS = 500

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self.entries: list[str] = []
        # NB: not ``log`` - Textual's Widget.log is the built-in logging helper.
        self.event_log: list[LogEvent] = []

    def _record(self, headline: str, body: str = "", *, fenced: bool = False) -> None:
        self.event_log.append(
            LogEvent(datetime.now().strftime("%H:%M:%S"), headline, body, fenced)
        )

    async def _add(self, widget: Widget, entry: str) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.MAX_EVENTS:
            del self.entries[: len(self.entries) - self.MAX_EVENTS]
        await self.mount(widget)
        widget.anchor()
        while len(self.children) > self.MAX_EVENTS:
            await self.children[0].remove()

    async def add_user(self, text: str) -> None:
        self._record("you", text)
        block = Vertical(
            Static(Text("you"), classes="msg-head msg-you"),
            Markdown(text),
            classes="ev-user",
        )
        await self._add(block, f"you: {text}")

    async def add_prose(self, text: str) -> None:
        self._record("assistant", text)
        block = Vertical(
            Static(Text("assistant"), classes="msg-head msg-assistant"),
            Markdown(text),
            classes="ev-prose",
        )
        await self._add(block, f"llm: {text}")

    async def add_call(self, call: ToolCall) -> None:
        target = (
            call.params.get("path")
            or call.params.get("command")
            or call.params.get("pattern")
            or call.params.get("question")
            or ""
        )
        summary = f"▶ call {call.id} {call.tool} {target}".rstrip()
        raw = call.raw.strip("\n")
        self._record(f"tool call {call.id} - {call.tool} {target}".rstrip(), raw, fenced=True)
        children: list[Widget] = [Static(Text(summary), classes="call-summary")]
        if raw:
            children.append(
                Collapsible(
                    Static(Text(raw)),
                    title=f"raw block ({len(raw.splitlines())} lines)",
                    collapsed=True,
                )
            )
        await self._add(Vertical(*children, classes="ev-call"), summary)

    async def add_note(self, text: str) -> None:
        self._record(text)
        await self._add(Static(Text(text), classes="ev-note"), text)

    async def add_error(self, text: str) -> None:
        self._record(f"ERROR: {text}")
        await self._add(Static(Text(text), classes="ev-error"), text)

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        payload = outbound.chunks[0]
        note = f"→ {label} ({outbound.total_chars:,} chars)"
        self._record(f"{note} [outbound turn {outbound.turn}]", payload, fenced=True)
        block = Vertical(
            Static(Text(note), classes="ev-note"),
            Collapsible(
                Static(Text(payload)),
                title=f"outbound turn {outbound.turn} ({outbound.total_chars:,} chars)",
                collapsed=True,
            ),
            classes="ev-call",
        )
        await self._add(block, f"{note}\n{payload}")

    def render_log(self, meta_lines: list[str]) -> str:
        """Format the full event log as an AI-paste-friendly markdown document."""
        lines = ["# AgentClip chat log", ""]
        lines += [f"- {m}" for m in meta_lines]
        lines += ["", "---", ""]
        for ev in self.event_log:
            lines.append(f"## [{ev.time}] {ev.headline}")
            lines.append("")
            body = ev.body.rstrip("\n")
            if body:
                if ev.fenced:
                    fence = _fence(body)
                    lines += [fence, body, fence, ""]
                else:
                    lines += [body, ""]
        return "\n".join(lines).rstrip() + "\n"

    async def clear_events(self) -> None:
        self.entries.clear()
        self.event_log.clear()
        await self.remove_children()
