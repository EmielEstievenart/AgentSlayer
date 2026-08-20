"""fetch_chunk: the slicing, the marker, and the handler's four answers.

The engine owns WHEN a body is cached (tests/engine/test_chunk_cache.py); this
file is about what a cached body is and what a fetch of it does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import Config, LimitsConfig, caps_for_budget
from agentclip.executor.permissions import DEFAULT_CONFIG, TOOL_PERMISSIONS, permission_target
from agentclip.executor.tools.chunks import (
    CACHE_BODY_CAP,
    FETCH_CHUNK_DOC,
    FETCH_CHUNK_SPEC,
    CachedChunks,
    chunk_chars_for,
    chunk_marker,
    slice_body,
)
from agentclip.executor.tools.registry import ToolContext, default_registry
from agentclip.executor.tools.sandbox import Workspace
from agentclip.protocol.types import ERROR_CODES, ToolCall

LINES = "".join(f"line {i:04d} {'x' * 40}\n" for i in range(1, 201))


def make_ctx(root: Path, cache: dict[str, CachedChunks] | None = None) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root, Config().excluded_names()),
        limits=LimitsConfig(),
        caps=caps_for_budget(12_000),
        chunk_cache=cache if cache is not None else {},
    )


def call(**params: str) -> ToolCall:
    return ToolCall(id=1, tool="fetch_chunk", params=dict(params), raw="")


def cached(body: str, chunk_chars: int = 500, **kwargs: object) -> CachedChunks:
    return CachedChunks.of(
        "c7", body, call_id=3, turn=12, tool="run_command", chunk_chars=chunk_chars, **kwargs
    )


# -- slicing ------------------------------------------------------------------


@pytest.mark.parametrize("chunk_chars", [1, 7, 47, 500, 5_000, 10 ** 6])
def test_the_parts_are_the_body_with_nothing_added_or_lost(chunk_chars: int) -> None:
    """The promise a model reassembling parts 1..K is relying on."""
    parts = slice_body(LINES, chunk_chars)
    assert "".join(parts) == LINES
    assert all(len(p) <= chunk_chars for p in parts)
    assert all(parts)  # no empty part ever occupies a number


def test_parts_are_contiguous_and_cut_on_line_boundaries() -> None:
    parts = slice_body(LINES, 500)
    assert len(parts) > 1
    # Every part but the last ends where a line ends, because no line here is
    # longer than a part.
    assert all(p.endswith("\n") for p in parts[:-1])
    assert parts[0].startswith("line 0001")
    assert parts[-1].endswith("line 0200 " + "x" * 40 + "\n")


def test_a_line_longer_than_a_part_is_hard_split() -> None:
    """Minified JSON and base64 blobs have no newlines at all, and they are
    exactly the output that overflows a budget - refusing to cut them would
    mean a part that cannot be served."""
    body = "z" * 1_000
    parts = slice_body(body, 300)
    assert parts == ("z" * 300, "z" * 300, "z" * 300, "z" * 100)
    assert "".join(parts) == body


def test_a_trailing_newline_survives_the_round_trip() -> None:
    for body in ("a\nb\n", "a\nb", "\n", "one line"):
        assert "".join(slice_body(body, 4)) == body


def test_an_empty_body_is_one_empty_part() -> None:
    assert slice_body("", 100) == ("",)


def test_slicing_refuses_a_zero_chunk_size() -> None:
    with pytest.raises(ValueError):
        slice_body(LINES, 0)


# -- sizing -------------------------------------------------------------------


def test_a_part_is_clamped_under_both_caps_that_could_cut_it() -> None:
    """Whichever cap is tighter wins, and the header's room comes off the top."""
    assert chunk_chars_for(12_000, 6_000) == 6_000 - 200  # per-result cap binds
    assert chunk_chars_for(4_000, 6_000) == int(4_000 * 0.60) - 200  # paste budget binds


def test_a_part_never_sizes_to_zero_on_an_absurd_config() -> None:
    assert chunk_chars_for(400, 200) > 0


# -- the marker ---------------------------------------------------------------


def test_the_marker_teaches_the_exact_call() -> None:
    """It has to: the model reads it inside a body it is being handed, turns
    away from the catalog, and needs no other doc to act."""
    marker = chunk_marker("c7", 4)
    assert "fetch_chunk" in marker and "id=c7" in marker and "part=1..4" in marker
    assert "\n" not in marker  # one line, inside a body that is short of room


def test_the_marker_admits_when_even_the_cache_was_capped() -> None:
    plain = chunk_marker("c7", 4)
    capped = chunk_marker("c7", 4, capped=True)
    assert str(CACHE_BODY_CAP) in capped and "full output" not in capped
    assert "full output" in plain


def test_a_body_over_the_cache_cap_is_held_to_it_and_says_so() -> None:
    entry = cached("y" * (CACHE_BODY_CAP + 5_000), chunk_chars=50_000)
    assert entry.capped
    assert sum(len(p) for p in entry.parts) == CACHE_BODY_CAP
    assert entry.original_chars == CACHE_BODY_CAP + 5_000
    assert str(CACHE_BODY_CAP) in entry.marker


# -- the handler --------------------------------------------------------------


def test_a_fetch_returns_the_slice_verbatim_under_a_one_line_header(tmp_path: Path) -> None:
    entry = cached(LINES)
    ctx = make_ctx(tmp_path, {"c7": entry})
    result = FETCH_CHUNK_SPEC.handler(ctx, call(id="c7", part="2"))
    assert result.status == "ok"
    header, _, rest = result.body.partition("\n")
    assert header == (
        f"part 2/{len(entry.parts)} of call 3 (run_command) from turn 12"
        f" ({len(LINES)} chars total)"
    )
    assert rest == entry.parts[1]  # byte for byte, no re-wrapping


def test_quotes_around_the_id_are_forgiven(tmp_path: Path) -> None:
    """The marker writes `id=c7`; a model that has met JSON tools writes
    `id: "c7"`. Spending a turn on punctuation would be absurd."""
    ctx = make_ctx(tmp_path, {"c7": cached(LINES)})
    assert FETCH_CHUNK_SPEC.handler(ctx, call(id='"c7"', part="1")).status == "ok"


def test_both_params_are_required(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, {"c7": cached(LINES)})
    for params in ({"id": "c7"}, {"part": "1"}):
        result = FETCH_CHUNK_SPEC.handler(ctx, call(**params))
        assert result.status == "error" and result.code == "missing_param"


def test_an_expired_id_says_so_and_names_the_way_back(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, {"c9": cached(LINES)})
    result = FETCH_CHUNK_SPEC.handler(ctx, call(id="c1", part="1"))
    assert result.status == "error" and result.code == "unknown_chunk"
    assert "re-run the tool that produced the output" in result.body
    assert "c9" in result.body  # ...and what IS still there


def test_an_empty_cache_still_answers_legibly(tmp_path: Path) -> None:
    result = FETCH_CHUNK_SPEC.handler(make_ctx(tmp_path), call(id="c1", part="1"))
    assert result.status == "error" and "nothing" in result.body


def test_a_part_out_of_range_names_the_valid_range(tmp_path: Path) -> None:
    entry = cached(LINES)
    ctx = make_ctx(tmp_path, {"c7": entry})
    for bad in ("0", "-1", str(len(entry.parts) + 1)):
        result = FETCH_CHUNK_SPEC.handler(ctx, call(id="c7", part=bad))
        assert result.status == "error" and result.code == "bad_param"
        assert f"between 1 and {len(entry.parts)}" in result.body


def test_a_non_numeric_part_is_a_bad_param(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, {"c7": cached(LINES)})
    result = FETCH_CHUNK_SPEC.handler(ctx, call(id="c7", part="two"))
    assert result.status == "error" and result.code == "bad_param"


def test_every_emitted_code_is_in_the_documented_closed_set(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, {"c7": cached(LINES)})
    for params in ({"id": "c7"}, {"id": "nope", "part": "1"}, {"id": "c7", "part": "99"}):
        result = FETCH_CHUNK_SPEC.handler(ctx, call(**params))
        assert result.code in ERROR_CODES


# -- registration and permissions ---------------------------------------------


def test_the_catalog_entry_stays_ultra_compact() -> None:
    """The bootstrap has ~150 chars of slack in its worst measured configuration
    (docs/design/protocol.md section 2). This entry is why it still does."""
    assert len(FETCH_CHUNK_DOC) <= 100
    assert "===CLIP:CALL" not in FETCH_CHUNK_DOC  # no worked example, on purpose
    assert "fetch_chunk" in default_registry().render_catalog()


def test_it_is_allowed_by_default_as_its_own_key() -> None:
    """Cache-only, no side effects - mcp_schema's reasoning exactly. Its own key
    rather than a fallback, so a user can still write a rule for it."""
    assert DEFAULT_CONFIG["permission"]["fetch_chunk"] == "allow"  # type: ignore[index]
    assert TOOL_PERMISSIONS["fetch_chunk"] == ("fetch_chunk", "id")
    assert permission_target("fetch_chunk", {"id": "c7"}, "auto") == ("fetch_chunk", "c7")
