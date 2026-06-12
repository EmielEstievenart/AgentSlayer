"""TranscriptPanel: one mounted widget per session event (tui.md section 4).

Every newly mounted event widget is ``anchor()``-ed so the panel stays pinned
to the bottom while content streams in (Textual >= 4 semantics) and releases
when the user scrolls up. Children are pruned beyond MAX_EVENTS to bound
layout cost. ``entries`` mirrors every event as plain text - it is the
assertion surface for the Pilot smoke test and a cheap in-memory postmortem.
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Static

from agentclip.protocol.types import Outbound, ToolCall


class TranscriptPanel(VerticalScroll):
    MAX_EVENTS = 500

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self.entries: list[str] = []

    async def _add(self, widget: Widget, entry: str) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.MAX_EVENTS:
            del self.entries[: len(self.entries) - self.MAX_EVENTS]
        await self.mount(widget)
        widget.anchor()
        while len(self.children) > self.MAX_EVENTS:
            await self.children[0].remove()

    async def add_user(self, text: str) -> None:
        await self._add(Markdown(text, classes="ev-user"), f"you: {text}")

    async def add_prose(self, text: str) -> None:
        await self._add(Markdown(text, classes="ev-prose"), f"llm: {text}")

    async def add_call(self, call: ToolCall) -> None:
        target = (
            call.params.get("path")
            or call.params.get("command")
            or call.params.get("pattern")
            or call.params.get("question")
            or ""
        )
        summary = f"▶ call {call.id} {call.tool} {target}".rstrip()
        children: list[Widget] = [Static(Text(summary), classes="call-summary")]
        raw = call.raw.strip("\n")
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
        await self._add(Static(Text(text), classes="ev-note"), text)

    async def add_error(self, text: str) -> None:
        await self._add(Static(Text(text), classes="ev-error"), text)

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        payload = outbound.chunks[0]
        note = f"→ {label} ({outbound.total_chars:,} chars)"
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

    async def clear_events(self) -> None:
        self.entries.clear()
        await self.remove_children()
