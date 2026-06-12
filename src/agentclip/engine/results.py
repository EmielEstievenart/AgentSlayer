"""Result-size helpers: middle/tail truncation and per-result body fitting.

The composer (protocol/composer.py) owns fitting the WHOLE results payload to
the paste budget; these helpers enforce the per-result cap (limits.
max_result_chars) before composition and provide the engine's fallback when
even the composer's line-boundary truncation cannot fit (its water-filling
never cuts a body's first/last line; truncate_middle here can cut anything).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from agentclip.protocol.composer import TRUNCATION_MARKER
from agentclip.protocol.types import ToolResult


def truncate_middle(text: str, max_chars: int, marker: str = TRUNCATION_MARKER) -> str:
    """Cut the middle of ``text`` so the result is <= max_chars, keeping the
    head and the tail (where openings and verdicts live). Prefers cutting on
    line boundaries; falls back to a plain character cut."""
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(marker) - 2  # two joining newlines
    if budget <= 0:
        return marker[:max_chars]
    head_budget = budget // 2
    tail_budget = budget - head_budget
    head = text[:head_budget]
    tail = text[len(text) - tail_budget :]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]
    return f"{head}\n{marker}\n{tail}"


def truncate_tail(text: str, max_chars: int, marker: str = TRUNCATION_MARKER) -> str:
    """Keep the TAIL of ``text`` (test/build verdicts live at the end),
    prefixed with the in-band marker. Result is <= max_chars."""
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(marker) - 1  # one joining newline
    if budget <= 0:
        return marker[:max_chars]
    tail = text[len(text) - budget :]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]
    return f"{marker}\n{tail}"


def fit_results(results: Sequence[ToolResult], budget: int) -> tuple[ToolResult, ...]:
    """Cap every result body to ``budget`` chars via middle truncation.

    Statuses, codes, and user notes are untouched; only over-long bodies
    shrink, each carrying the in-band truncation marker.
    """
    out: list[ToolResult] = []
    for result in results:
        if len(result.body) > budget:
            result = replace(result, body=truncate_middle(result.body, budget))
        out.append(result)
    return tuple(out)
