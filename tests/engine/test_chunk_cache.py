"""The fetch_chunk cache: what the engine keeps of a body it had to cut, and
for how long.

Both truncation passes are exercised, because either can be the one that cuts a
given body and a hint carried by only one of them is a hint the model gets only
sometimes: the ENGINE pass (engine/results.fit_results, limits.max_result_chars)
and the COMPOSER pass (protocol/composer.results, preset.max_paste_chars).

The rest is lifetime. A fetch always happens in a LATER turn than the truncation
that made it necessary, so the cache has to outlive a turn - and it must not
outlive the model's interest in it, or an expired id would serve output from
three tasks ago as if it were fresh.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.engine.engine import Engine, NewTurn, Send

from ..conftest import CHAT_NAME

# 300 lines x 67 chars = 20,100: past the 6,000-char per-result cap so the
# ENGINE pass cuts it, and well inside caps.read_file_span_lines (600) so
# read_file hands over the whole file rather than a span.
BIG = "".join(f"line {i:04d} {'x' * 55}\n" for i in range(1, 301))

# 80 lines x 65 chars = 5,200: UNDER the per-result cap, so the engine pass
# leaves it alone - three of these in one payload are what makes the composer
# pass do the cutting instead.
MID = "".join(f"mid {i:03d} {'y' * 56}\n" for i in range(1, 81))

SMALL = "the whole file, comfortably inside every budget\n"


@pytest.fixture
def chunk_engine(project: Path, make_engine) -> Engine:
    (project / "big.txt").write_text(BIG, encoding="utf-8", newline="")
    (project / "mid.txt").write_text(MID, encoding="utf-8", newline="")
    (project / "small.txt").write_text(SMALL, encoding="utf-8", newline="")
    engine = make_engine()
    engine.start_task("read the files")
    return engine


def _reply(*calls: str) -> str:
    body = "\n".join(calls)
    return f"~~~~\n{body}\n===CLIP:EOM calls={len(calls)} chat={CHAT_NAME}===\n~~~~\n"


def _read(call_id: int, path: str) -> str:
    return f"===CLIP:CALL id={call_id} tool=read_file===\npath: {path}\n===CLIP:END==="


def _fetch(call_id: int, chunk_id: str, part: int) -> str:
    return (
        f"===CLIP:CALL id={call_id} tool=fetch_chunk===\n"
        f"id: {chunk_id}\npart: {part}\n===CLIP:END==="
    )


def _run(engine: Engine, reply: str) -> str:
    """Ingest one reply, run its whole plan, return the outbound payload text."""
    step = engine.ingest(reply)
    assert isinstance(step, NewTurn), step
    out = engine.execute()
    assert isinstance(out, Send), out
    return out.outbound.chunks[0]


def _only_id(engine: Engine) -> str:
    assert len(engine._chunk_cache) == 1, engine._chunk_cache
    return next(iter(engine._chunk_cache))


# -- filling it ---------------------------------------------------------------


def test_a_body_the_engine_pass_cut_is_cached_and_its_id_named_in_the_payload(
    chunk_engine: Engine,
) -> None:
    payload = _run(chunk_engine, _reply(_read(1, "big.txt")))
    chunk_id = _only_id(chunk_engine)
    entry = chunk_engine._chunk_cache[chunk_id]
    assert entry.marker in payload
    assert f"fetch_chunk id={chunk_id} part=1..{len(entry.parts)}" in payload
    # The point of the whole thing: text the payload does NOT carry is text the
    # cache still has.
    middle = "line 0150"
    assert middle not in payload
    assert middle in "".join(entry.parts)


def test_a_body_the_composer_pass_cut_is_cached_too(chunk_engine: Engine) -> None:
    """Three mid-sized reads: each is under the per-result cap, so the engine
    pass passes them through untouched and the whole-payload fit is what cuts."""
    payload = _run(
        chunk_engine,
        _reply(_read(1, "mid.txt"), _read(2, "mid.txt"), _read(3, "mid.txt")),
    )
    assert chunk_engine._chunk_cache
    for entry in chunk_engine._chunk_cache.values():
        assert entry.marker in payload
        assert "fetch_chunk id=" in entry.marker


def test_a_payload_that_cut_nothing_caches_nothing(chunk_engine: Engine) -> None:
    payload = _run(chunk_engine, _reply(_read(1, "small.txt")))
    assert "fetch_chunk id=" not in payload
    assert chunk_engine._chunk_cache == {}


def test_an_id_minted_for_a_body_that_survived_whole_is_dropped(chunk_engine: Engine) -> None:
    """Ids are spent before composing, because the marker has to name a part
    count that only the ORIGINAL body knows. Which bodies were really cut is
    knowable only afterwards - so unminted-by-hindsight ids simply vanish, and
    the ids a session hands out are not consecutive."""
    _run(chunk_engine, _reply(_read(1, "mid.txt")))  # 5,200 chars: nothing cut
    assert chunk_engine._chunk_cache == {}
    assert chunk_engine._next_chunk_id > 1  # ...but an id was spent


# -- fetching from it ---------------------------------------------------------


def test_a_fetch_returns_the_slice_and_the_cache_survives_its_own_fetch(
    chunk_engine: Engine,
) -> None:
    """The rule a fetch depends on most: fetching must not evict what is being
    fetched, or a model working through parts 1..K loses the cache to part 1."""
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    chunk_id = _only_id(chunk_engine)
    entry = chunk_engine._chunk_cache[chunk_id]
    assert len(entry.parts) > 2

    payload = _run(chunk_engine, _reply(_fetch(1, chunk_id, 2)))
    assert f"part 2/{len(entry.parts)}" in payload
    assert "line 0150" in payload  # the text the first payload had to drop
    assert _only_id(chunk_engine) == chunk_id

    # ...and again, several turns later.
    payload = _run(chunk_engine, _reply(_fetch(1, chunk_id, 3)))
    assert f"part 3/{len(entry.parts)}" in payload
    assert _only_id(chunk_engine) == chunk_id


def test_every_part_together_is_the_body_the_model_never_saw_whole(
    chunk_engine: Engine,
) -> None:
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    entry = chunk_engine._chunk_cache[_only_id(chunk_engine)]
    joined = "".join(entry.parts)
    assert len(joined) == entry.original_chars
    assert joined.endswith(BIG.rstrip("\n"))  # read_file's own header rides in front
    assert not entry.capped


def test_a_fetch_never_needs_an_approval(chunk_engine: Engine) -> None:
    """Allow-by-default, `auto` kind: the turns above ran to Send with no gate
    in the way, which is the assertion - stated once, on purpose."""
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    payload = _run(chunk_engine, _reply(_fetch(1, _only_id(chunk_engine), 1)))
    assert "status=ok" in payload


# -- losing it ----------------------------------------------------------------


def test_a_later_turn_that_cut_nothing_and_fetched_nothing_clears_it(
    chunk_engine: Engine,
) -> None:
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    chunk_id = _only_id(chunk_engine)

    _run(chunk_engine, _reply(_read(1, "small.txt")))
    assert chunk_engine._chunk_cache == {}

    payload = _run(chunk_engine, _reply(_fetch(1, chunk_id, 1)))
    assert "status=error" in payload
    assert f"no cached output under id '{chunk_id}'" in payload
    assert "re-run the tool that produced the output" in payload


def test_new_truncations_replace_the_cache_wholesale(chunk_engine: Engine) -> None:
    """"The output you were just handed" has to mean literally that."""
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    first = _only_id(chunk_engine)

    payload = _run(chunk_engine, _reply(_read(1, "big.txt")))
    second = _only_id(chunk_engine)
    assert second != first
    assert chunk_engine._chunk_cache[second].marker in payload

    payload = _run(chunk_engine, _reply(_fetch(1, first, 1)))
    assert "status=error" in payload
    assert second in payload  # the error names what IS still cached


def test_a_part_out_of_range_is_answered_without_touching_the_cache(
    chunk_engine: Engine,
) -> None:
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    chunk_id = _only_id(chunk_engine)
    parts = len(chunk_engine._chunk_cache[chunk_id].parts)

    payload = _run(chunk_engine, _reply(_fetch(1, chunk_id, parts + 1)))
    assert "status=error" in payload
    assert f"between 1 and {parts}" in payload
    assert _only_id(chunk_engine) == chunk_id  # a bad part is not an eviction


def test_the_handler_reads_the_very_dict_the_engine_mutates(chunk_engine: Engine) -> None:
    """Identity, not a copy. The context is built once at __init__ and a fetch
    lands turns later, so a rebind anywhere in the engine would leave the
    handler looking at the cache as it was when the session started."""
    assert chunk_engine._ctx.chunk_cache is chunk_engine._chunk_cache
    _run(chunk_engine, _reply(_read(1, "big.txt")))
    assert chunk_engine._ctx.chunk_cache is chunk_engine._chunk_cache
