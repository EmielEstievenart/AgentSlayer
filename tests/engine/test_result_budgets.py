"""How much of a big result the model really gets, at each paste budget.

This file pins a field failure end to end. An MCP tool answered with 1,172
lines; the user's preset carried 96,000 chars a paste; what arrived was 308
lines, and `fetch_chunk part=1` began at line 865 because lines 1-864 no longer
existed anywhere. Two independent bugs had lined up:

1. The handler tail-capped its own output before the ToolResult was built, so
   the truncation happened BEFORE the pass that caches what it cuts. The cache
   then faithfully held a gutted tail (executor/tools/shell.retain_output).
2. `[limits]` was a pair of fixed numbers - 8,000 and 6,000 - which meant a
   96,000-char budget was clamped to what a 12,000-char budget can hold
   (config.resolve_limits).

So the assertions here are deliberately about the WHOLE chain rather than any
one of its links: what the handler returns, what the payload shows, what the
cache holds, and what a fetch then serves - and that the two budgets, the big
one and the classic one, each get the treatment they should.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine, NewTurn, Send
from agentclip.executor.mcp.types import McpToolInfo
from agentclip.executor.tools.mcp_tools import make_mcp_specs
from agentclip.executor.tools.registry import ToolRegistry, default_registry

from ..conftest import CHAT_NAME, write_permissions

# Reusing the composer suite's heredoc reader rather than re-deriving one: the
# question these tests ask is "how much of the body did the model actually get",
# and the only honest answer is the text between the delivered heredoc's fences.
from ..protocol.test_composer import extract_result_bodies

# The size of the answer that started this, to the line. Two widths of it: one
# that fits inside a 96k preset's auto per-result cap and one that does not, so
# the same 1,172 lines exercise both "nothing needed cutting" and "cut, cached,
# fetched back".
FIELD_LINES = 1_172

TOOL = McpToolInfo(
    id="reports_dump",
    server="reports",
    name="dump",
    description="Dump the whole report.",
    input_schema_json=json.dumps({"type": "object"}, separators=(",", ":")),
)


def answer(width: int) -> str:
    """``FIELD_LINES`` numbered lines of ``width`` chars, LF-joined."""
    return "\n".join(f"line {i:04d} " + "x" * (width - 11) for i in range(1, FIELD_LINES + 1))


BIG = answer(80)  # ~94k: past a 96k preset's 48,000-char auto result cap
MID = answer(34)  # ~40k: the field report's own size, inside that cap


class StubServer:
    """A structural McpToolSource answering with one fixed text (see
    tests/executor/tools/test_mcp_tools.py - the runtime is never imported)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def tools(self) -> tuple[McpToolInfo, ...]:
        return (TOOL,)

    def schema(self, tool_id: str) -> McpToolInfo | None:
        return TOOL if tool_id == TOOL.id else None

    def call(self, tool_id: str, args: dict[str, Any]) -> str:
        return self._text


def mcp_registry(text: str) -> ToolRegistry:
    return default_registry(mcp_specs=make_mcp_specs(StubServer(text), max_listing_chars=2_000))


def build(
    project: Path,
    make_engine,
    *,
    text: str,
    service: str | None = None,
    limits: dict[str, int] | None = None,
) -> Engine:
    """An armed engine whose `mcp` tool answers with ``text``.

    `mcp` is `ask` under the shipped rules (an MCP tool can do anything); these
    tests are about output sizes and not about the gate, so the project allows
    it outright.
    """
    write_permissions(project, {"permission": {"mcp": {"*": "allow"}}})
    cfg: Config = load_config(project, global_config_path=project / "no-such-global.toml")
    if service is not None:
        cfg = replace(cfg, general=replace(cfg.general, service=service))
    if limits is not None:
        cfg = replace(cfg, limits=replace(cfg.limits, **limits))
    engine = make_engine(config=cfg, tools=mcp_registry(text))
    engine.start_task("dump the report")
    return engine


REPLY = (
    "~~~~\n"
    "===CLIP:CALL id=1 tool=mcp===\n"
    f"tool: {TOOL.id}\n"
    "===CLIP:END===\n"
    f"===CLIP:EOM calls=1 chat={CHAT_NAME}===\n"
    "~~~~\n"
)


def fetch(chunk_id: str, part: int) -> str:
    return (
        "~~~~\n"
        "===CLIP:CALL id=1 tool=fetch_chunk===\n"
        f"id: {chunk_id}\npart: {part}\n"
        "===CLIP:END===\n"
        f"===CLIP:EOM calls=1 chat={CHAT_NAME}===\n"
        "~~~~\n"
    )


def run(engine: Engine, reply: str) -> str:
    step = engine.ingest(reply)
    assert isinstance(step, NewTurn), step
    out = engine.execute()
    assert isinstance(out, Send), out
    return out.outbound.chunks[0]


def only_id(engine: Engine) -> str:
    assert len(engine._chunk_cache) == 1, engine._chunk_cache
    return next(iter(engine._chunk_cache))


# -- the field failure, end to end --------------------------------------------


def test_a_result_too_big_even_for_a_96k_budget_is_cut_but_never_lost(
    project: Path, make_engine
) -> None:
    """The whole chain, on the answer that started it.

    Nothing may tail-cap on the way in; the payload may show a middle cut, but
    only the kind that names its own way back; and part 1 has to begin where the
    output began, because that is the exact assertion the field failure broke.
    """
    engine = build(project, make_engine, text=BIG, service="copilot-work")
    payload = run(engine, REPLY)

    # Nothing between the server and the payload cut by policy: the honest
    # in-band marker belongs to the memory guard alone, and the guard is 512k.
    assert "[truncated: showing last" not in payload
    # What the model sees IS cut - by the engine, which cached what it cut.
    entry = engine._chunk_cache[only_id(engine)]
    assert entry.marker in payload
    body = extract_result_bodies(payload)[1]
    assert len(body) < len(BIG)
    assert body.startswith("line 0001 ")  # a middle cut: head...
    assert body.rstrip().endswith("x")  # ...and tail
    assert "line 0600" not in body  # ...with the middle gone

    # The cache holds the body the handler returned, and that body was whole.
    assert not entry.capped
    assert "".join(entry.parts) == BIG
    assert entry.parts[0].startswith("line 0001 ")

    # And a fetch really serves it from the top.
    served = extract_result_bodies(run(engine, fetch(entry.chunk_id, 1)))[1]
    assert served.splitlines()[0].startswith(f"part 1/{len(entry.parts)} of call 1 (mcp)")
    assert served.splitlines()[1] == BIG.splitlines()[0]


def test_the_parts_reassemble_into_exactly_what_the_tool_printed(
    project: Path, make_engine
) -> None:
    """1..K concatenated is the output, whitespace included - the promise a
    model working through the parts is relying on, over a real turn."""
    engine = build(project, make_engine, text=BIG, service="copilot-work")
    run(engine, REPLY)
    entry = engine._chunk_cache[only_id(engine)]
    assert "".join(entry.parts) == BIG
    assert len(entry.parts) > 1  # otherwise the reassembly proves nothing


def test_the_field_answer_now_arrives_whole_at_a_96k_budget(
    project: Path, make_engine
) -> None:
    """The other half of the fix: 1,172 lines / ~40k chars is simply not too big
    for a 96,000-char paste, so nothing should be cut at all.

    Before `[limits]` learned to scale, this arrived as its last 308 lines -
    `min(caps.command_tail_chars, 8_000)` - no matter how much room the user had
    actually paid for.
    """
    engine = build(project, make_engine, text=MID, service="copilot-work")
    payload = run(engine, REPLY)

    assert "[truncated" not in payload  # neither marker: nothing was cut
    assert engine._chunk_cache == {}
    assert extract_result_bodies(payload)[1] == MID
    assert f"line {FIELD_LINES:04d} " in payload


# -- the budgets either side of it --------------------------------------------


def test_the_classic_12k_preset_still_shows_about_six_thousand(
    project: Path, make_engine
) -> None:
    """No behaviour change where the old fixed defaults were right.

    `max_paste_chars // 2` is 6,000 at the 12,000-char presets, which is the
    number `[limits]` used to ship - so a default install sees exactly what it
    saw before, marker and all.
    """
    engine = build(project, make_engine, text=MID)  # the fixtures' chatgpt-attach
    payload = run(engine, REPLY)
    body = extract_result_bodies(payload)[1]
    assert 5_000 < len(body) <= 6_000
    assert engine._chunk_cache[only_id(engine)].marker in payload


def test_an_explicit_cap_beats_auto(project: Path, make_engine) -> None:
    """`[limits]` auto is a default, not a policy: a user who writes a number
    gets that number, at any budget."""
    engine = build(
        project, make_engine, text=MID, service="copilot-work", limits={"max_result_chars": 3_000}
    )
    body = extract_result_bodies(run(engine, REPLY))[1]
    assert 2_000 < len(body) <= 3_000


@pytest.mark.parametrize("service", ["chatgpt-attach", "claude", "copilot-work", "grok"])
def test_a_served_part_is_never_itself_truncated(
    project: Path, make_engine, service: str
) -> None:
    """The invariant `chunk_chars_for` exists for, over a real turn: a part plus
    its one-line header has to survive the very passes that cut the body it came
    from, or the way out is the way back in.

    12,000 is the floor here only because a smaller bootstrap cannot carry an
    MCP catalog at all (the factory drops the MCP tools for the session instead,
    docs/design/mcp.md section 5), so there is no MCP result to cut. The 4,000-
    and 6,000-char presets are held to the same invariant one layer down, in
    tests/executor/tools/test_chunks.py.
    """
    engine = build(project, make_engine, text=BIG, service=service)
    run(engine, REPLY)
    entry = engine._chunk_cache[only_id(engine)]
    served = extract_result_bodies(run(engine, fetch(entry.chunk_id, 2)))[1]
    assert "truncated" not in served
    assert served.split("\n", 1)[1] == entry.parts[1]
