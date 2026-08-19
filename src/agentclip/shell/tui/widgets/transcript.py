"""TranscriptPanel: one mounted widget per session event (tui.md section 4).

Auto-scroll: after each event lands, the panel either pins itself to the
bottom (the event fits in the visible area - Textual's container-level
``anchor()``, which releases when the user scrolls up and re-engages when they
return to the bottom) or, when the event is TALLER than the visible area,
scrolls the event's top to the top of the view and releases the pin so the
user reads the response from its first line instead of its last. While parked
like that, later small events (tool calls, notes) mount below without moving
the view; pinning resumes once the scroll position is back at the bottom.
NB: ``anchor()`` belongs on the scroll container - anchoring the event widgets
themselves (this file's original approach) is a silent no-op in Textual 8.

One INGESTED REPLY is several of those events (prose, then a widget per tool
call), and the per-event rule above cannot see that: a reply of small widgets
ends pinned at its last line with its opening scrolled away. So the controller
brackets the reply - ``begin_reply`` before the first event, ``reveal_reply``
after the last - and the panel parks the view at the reply's FIRST widget, which
is the same park a tall event gets, applied to the reply as a whole.

Children are pruned beyond MAX_EVENTS to bound layout cost. ``entries``
mirrors every event as plain text - it is the assertion surface for the Pilot
smoke test and a cheap in-memory postmortem.

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
        # True while the view is parked at the top of a response too tall to
        # fit - small follow-up events must not yank the scroll to the bottom.
        self._reading = False
        # The first widget of the reply being written, and the arm that claims
        # it: ``begin_reply`` sets the arm, the next event to mount takes it,
        # ``reveal_reply`` parks the view there. None between replies.
        self._reply_start: Widget | None = None
        self._await_reply_start = False

    def _record(self, headline: str, body: str = "", *, fenced: bool = False) -> None:
        self.event_log.append(LogEvent(datetime.now().strftime("%H:%M:%S"), headline, body, fenced))

    async def _add(
        self,
        widget: Widget,
        entry: str,
        *,
        markdown: tuple[Markdown, str] | None = None,
        beat: bool = False,
    ) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.MAX_EVENTS:
            del self.entries[: len(self.entries) - self.MAX_EVENTS]
        await self.mount(widget)
        if self._await_reply_start:
            self._reply_start = widget
            self._await_reply_start = False
        if markdown is not None:
            # Markdown mounts its blocks asynchronously; await it so the
            # widget has its real height before the fit-or-park decision.
            md, text = markdown
            await md.update(text)
        while len(self.children) > self.MAX_EVENTS:
            await self.children[0].remove()
        self.call_after_refresh(self._autoscroll, widget, beat)

    def _autoscroll(self, widget: Widget, beat: bool) -> None:
        """Fits in the view: pin the panel to the bottom. Taller than the
        view: park the event's top at the top of the view (release the pin) so
        reading starts at the first line. Parked: follow-up noise (calls,
        notes, outbound) holds the position until the scroll is back at the
        bottom, but a new conversational ``beat`` (user or assistant message)
        always re-applies the fit rule - a fresh response the user has not
        read yet outranks a stale reading position."""
        if not widget.is_mounted:  # pruned/cleared before the refresh landed
            return
        viewport = self.scrollable_content_region.height
        if viewport > 0 and widget.outer_size.height > viewport:
            self._park_at(widget)
            return
        if self._reading and not beat and self.scroll_y < self.max_scroll_y - 1:
            return  # still parked on a tall response - hold the position
        self._reading = False
        self.anchor()

    def _park_at(self, widget: Widget) -> None:
        """Put ``widget``'s top at the top of the view and hold it there."""
        if not widget.is_mounted:  # pruned/cleared before the refresh landed
            return
        self._reading = True
        self.release_anchor()  # compositor must stop chasing the bottom
        self.scroll_to_widget(widget, top=True, animate=False)

    def begin_reply(self) -> None:
        """The next event to land opens an ingested reply (``reveal_reply``)."""
        self._reply_start = None
        self._await_reply_start = True

    def reveal_reply(self) -> None:
        """Park the view at the top of the reply that just finished landing.

        One reply is prose plus a widget per tool call, and ``_autoscroll``
        judges each of them alone - so a reply whose every widget fits ends
        pinned at its LAST line, with the sentence it opened with off the top.
        This is the same park a single tall event gets, applied to the reply as
        a whole: its first line at the first row, as much of the rest as fits
        below it, and the parked rules from there (follow-up noise holds the
        position, scrolling back to the bottom resumes pinning).
        """
        widget = self._reply_start
        self._reply_start = None
        self._await_reply_start = False
        if widget is not None:
            # After the last event's own _autoscroll, which is queued ahead of
            # this one: the reply's start is where the view ends up.
            self.call_after_refresh(self._park_at, widget)

    async def add_user(self, text: str) -> None:
        self._record("you", text)
        md = Markdown()
        block = Vertical(
            Static(Text("you"), classes="msg-head msg-you"),
            md,
            classes="ev-user",
        )
        await self._add(block, f"you: {text}", markdown=(md, text), beat=True)

    async def add_prose(self, text: str) -> None:
        self._record("assistant", text)
        md = Markdown()
        block = Vertical(
            Static(Text("assistant"), classes="msg-head msg-assistant"),
            md,
            classes="ev-prose",
        )
        await self._add(block, f"llm: {text}", markdown=(md, text), beat=True)

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
        return ("\n".join(lines) + "\n" + self.render_events()).rstrip() + "\n"

    def render_events(self, start: int = 0, end: int | None = None) -> str:
        """This panel's events (or a slice of them), with no document header.

        Split out of ``render_log`` because an export is per *run*, not per
        panel: the master window's panel renders the whole document and each
        sub-agent RUN is appended under its own heading (MainScreen.render_log).
        Since the sub-agent window's panel now outlives its runs and simply
        accumulates them, the slice is what keeps the export per-run - the
        screen remembers where each run's events start and end in ``event_log``
        (which is never pruned, so those indices stay valid) and asks for that
        window of it. Merging the runs instead would produce one heading over
        five unrelated sub-tasks, which is exactly what makes an export
        unreadable.
        """
        lines: list[str] = []
        for ev in self.event_log[start:end]:
            lines.append(f"## [{ev.time}] {ev.headline}")
            lines.append("")
            body = ev.body.rstrip("\n")
            if body:
                if ev.fenced:
                    fence = _fence(body)
                    lines += [fence, body, fence, ""]
                else:
                    lines += [body, ""]
        return "\n".join(lines)

    async def clear_events(self) -> None:
        self.entries.clear()
        self.event_log.clear()
        self._reading = False
        self._reply_start = None
        self._await_reply_start = False
        self.anchor(False)
        await self.remove_children()
