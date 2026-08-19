"""engine/results.py helpers: middle/tail truncation and per-result fitting."""

from __future__ import annotations

from agentclip.engine.results import TRUNCATION_MARKER, fit_results, truncate_middle, truncate_tail
from agentclip.protocol.types import ToolResult

LONG = "\n".join(f"line {i:04d} {'x' * 40}" for i in range(1, 201))


def test_truncate_middle_noop_under_cap() -> None:
    assert truncate_middle("short text", 100) == "short text"


def test_truncate_middle_keeps_head_and_tail() -> None:
    out = truncate_middle(LONG, 800)
    assert len(out) <= 800
    assert TRUNCATION_MARKER in out
    assert out.startswith("line 0001")
    assert "line 0200" in out.split("\n")[-1]


def test_truncate_middle_degenerate_budget() -> None:
    out = truncate_middle(LONG, 10)
    assert len(out) <= 10


def test_truncate_tail_keeps_the_end() -> None:
    out = truncate_tail(LONG, 500)
    assert len(out) <= 500
    assert out.startswith(TRUNCATION_MARKER)
    assert out.endswith("line 0200 " + "x" * 40)


def test_truncate_tail_noop_under_cap() -> None:
    assert truncate_tail("ok", 100) == "ok"


def test_fit_results_caps_only_oversized_bodies() -> None:
    small = ToolResult(call_id=1, status="ok", body="tiny", tool="read_file")
    big = ToolResult(call_id=2, status="ok", body=LONG, tool="read_file")
    fitted = fit_results([small, big], 600)
    assert fitted[0] is small or fitted[0].body == "tiny"
    assert len(fitted[1].body) <= 600
    assert TRUNCATION_MARKER in fitted[1].body
    assert fitted[1].call_id == 2 and fitted[1].status == "ok"
