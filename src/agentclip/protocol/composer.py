"""Outbound payload composer: bootstrap / task / results / note rendering.

Stdlib-only protocol leaf (imports config + protocol.types + protocol.spec).
Every outbound payload ends with ===CLIP:EOM turn=N=== so the model can echo
the turn number back (stale-reply guard).

M1 chunking policy: single chunk only. A RESULTS payload that exceeds the
paste budget is fitted by middle-truncating the largest result bodies; a
bootstrap/task/note that cannot fit raises BudgetExceeded.
# M3: replace with PART/ACK chunked send
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from agentclip.config import BudgetCaps, ServicePreset
from agentclip.protocol.spec import SECTION_TASK_HEADER, render_spec
from agentclip.protocol.types import Outbound, ToolResult

# In-band marker substituted for the cut middle of an over-budget result body.
TRUNCATION_MARKER = "[truncated by AgentClip to fit the paste budget - request specific ranges]"

# Bounded refinement passes when fitting RESULTS payloads to the budget.
_FIT_ATTEMPTS = 10


class BudgetExceeded(Exception):
    """A payload cannot fit the preset's max_paste_chars (M1: no chunking)."""

    def __init__(self, needed_chars: int, budget_chars: int) -> None:
        super().__init__(
            f"payload needs {needed_chars} chars but the paste budget is {budget_chars}"
        )
        self.needed_chars = needed_chars
        self.budget_chars = budget_chars


def pick_heredoc_tag(content: str, base: str = "R") -> str:
    """Return a heredoc tag guaranteed not to collide with any line of content.

    A heredoc is terminated by a line equal to the tag after whitespace trim,
    so collision is checked against the stripped lines. Returns ``base``, or
    ``base + "x"``, ``base + "xx"``, ... until non-colliding.
    """
    lines = {line.strip() for line in content.split("\n")}
    tag = base
    while tag in lines:
        tag += "x"
    return tag


def _truncate_middle(body: str, target: int) -> str:
    """Middle-truncate body to roughly ``target`` chars on line boundaries.

    The first and last lines are always kept; the cut middle is replaced with
    TRUNCATION_MARKER. May still exceed ``target`` when even the minimal form
    (first line + marker + last line) is longer - the caller re-checks.
    """
    if len(body) <= target:
        return body
    lines = body.split("\n")
    if len(lines) <= 2:
        return body  # nothing cuttable without touching the first/last line
    head: list[str] = [lines[0]]
    tail: list[str] = [lines[-1]]
    middle = lines[1:-1]
    size = len(lines[0]) + len(TRUNCATION_MARKER) + len(lines[-1]) + 2  # + joins
    front, back = 0, len(middle)
    take_front = True
    while front < back:
        candidate = middle[front] if take_front else middle[back - 1]
        if size + len(candidate) + 1 > target:
            break
        size += len(candidate) + 1
        if take_front:
            head.append(middle[front])
            front += 1
        else:
            tail.insert(0, middle[back - 1])
            back -= 1
        take_front = not take_front
    if front >= back:  # defensive: everything fit after all
        return body
    return "\n".join([*head, TRUNCATION_MARKER, *tail])


def _fit_bodies(bodies: Sequence[str], available: int) -> list[str]:
    """Shrink the largest bodies so their total is <= available (approx).

    Finds the largest per-body cap T with sum(min(len(b), T)) <= available and
    middle-truncates every body longer than T. Only the largest bodies shrink;
    bodies already under the cap are untouched.
    """
    if not bodies:
        return []
    sizes = [len(b) for b in bodies]
    if sum(sizes) <= available:
        return list(bodies)
    lo, hi = 0, max(sizes)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(s, mid) for s in sizes) <= available:
            lo = mid
        else:
            hi = mid - 1
    cap = lo
    return [_truncate_middle(b, cap) if len(b) > cap else b for b in bodies]


class Composer:
    """Renders clipboard-ready outbound payloads for one session."""

    def __init__(
        self,
        preset: ServicePreset,
        caps: BudgetCaps,
        tool_catalog: str,
        workdir_name: str,
        os_name: str,
    ) -> None:
        self._preset = preset
        self._caps = caps
        self._tool_catalog = tool_catalog
        self._workdir_name = workdir_name
        self._os_name = os_name

    # -- public API ---------------------------------------------------------

    def bootstrap(self, task: str) -> Outbound:
        """The full protocol spec + tool catalog + initial task. Always turn 1."""
        spec_text = render_spec(
            self._preset, self._caps, self._tool_catalog, self._workdir_name, self._os_name
        )
        body = task.rstrip("\n")
        payload = (
            f"{spec_text}\n"
            f"{SECTION_TASK_HEADER}\n"
            "\n"
            "===CLIP:TASK===\n"
            f"{body}\n"
            "===CLIP:EOM turn=1===\n"
        )
        return self._single("bootstrap", payload, turn=1)

    def task(self, turn: int, text: str) -> Outbound:
        """A follow-up task/message from the user, mid- or post-session."""
        body = text.rstrip("\n")
        payload = f"===CLIP:TASK===\n{body}\n===CLIP:EOM turn={turn}===\n"
        return self._single("user_answer", payload, turn)

    def note(self, turn: int, text: str) -> Outbound:
        """An informational notice to the LLM (e.g. 'the user reverted turn 5')."""
        body = text.rstrip("\n")
        payload = f"===CLIP:NOTE===\n{body}\n===CLIP:EOM turn={turn}===\n"
        return self._single("note", payload, turn)

    def results(
        self,
        turn: int,
        results: Sequence[ToolResult],
        notes: Sequence[str] = (),
    ) -> Outbound:
        """The combined results payload for one executed turn.

        Over budget => fit by truncation: proportionally shrink the largest
        result bodies (sentinel lines are never touched; the first and last
        line of each body are always kept). Raises BudgetExceeded only when
        even maximal truncation cannot fit.
        """
        budget = self._preset.max_paste_chars
        bodies = [self._result_body(r) for r in results]
        payload = self._render_results(turn, results, bodies, notes)
        if len(payload) <= budget:
            return Outbound("results", (payload,), len(payload), turn)

        # M3: replace with PART/ACK chunked send
        overhead = len(payload) - sum(len(b) for b in bodies)
        available = budget - overhead
        for _ in range(_FIT_ATTEMPTS):
            if available < 0:
                break
            fitted = _fit_bodies(bodies, available)
            payload = self._render_results(turn, results, fitted, notes)
            if len(payload) <= budget:
                return Outbound("results", (payload,), len(payload), turn)
            available -= len(payload) - budget
        raise BudgetExceeded(len(payload), budget)

    # -- helpers ------------------------------------------------------------

    def _single(
        self,
        kind: Literal["bootstrap", "user_answer", "note"],
        payload: str,
        turn: int,
    ) -> Outbound:
        if len(payload) > self._preset.max_paste_chars:
            raise BudgetExceeded(len(payload), self._preset.max_paste_chars)
        return Outbound(kind, (payload,), len(payload), turn)

    @staticmethod
    def _result_body(result: ToolResult) -> str:
        body = result.body
        if result.user_note:
            user_line = f"user: {result.user_note}"
            body = f"{user_line}\n{body}" if body else user_line
        return body

    @staticmethod
    def _result_header(result: ToolResult) -> str:
        header = f"===CLIP:RESULT id={result.call_id} status={result.status}"
        if result.code is not None:
            header += f" code={result.code}"
        return header + "==="

    def _render_results(
        self,
        turn: int,
        results: Sequence[ToolResult],
        bodies: Sequence[str],
        notes: Sequence[str],
    ) -> str:
        lines: list[str] = [f"===CLIP:RESULTS turn={turn}==="]
        if notes:
            lines.append("===CLIP:NOTE===")
            lines.extend(notes)
            lines.append("===CLIP:END===")
        for result, body in zip(results, bodies, strict=True):
            tag = pick_heredoc_tag(body, base=f"R{result.call_id}")
            lines.append(self._result_header(result))
            lines.append(f"body <<{tag}")
            if body:
                lines.append(body)
            lines.append(tag)
            lines.append("===CLIP:END===")
        lines.append(f"===CLIP:EOM turn={turn}===")
        return "\n".join(lines) + "\n"
