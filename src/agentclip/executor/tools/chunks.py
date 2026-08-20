"""`fetch_chunk`: the way back to output a truncation pass threw away.

Two independent passes cut over-long result bodies before a payload goes out -
the per-result cap (`engine/results.fit_results`, `limits.max_result_chars`) and
the whole-payload fit (`protocol/composer.results`, `preset.max_paste_chars`) -
and until this tool existed, whatever they cut was gone. That was survivable
while every big body came from `read_file`, because the old marker's advice
("request specific ranges") is a real instruction there: ask again with start/
end and the file is still on disk. It is NOT survivable for anything whose
output cannot be re-derived by range - an MCP tool's 1,200-line answer arrived
as ~300 lines and the rest simply did not exist any more, which is the report
this module answers.

So the engine keeps the FULL body it was about to cut, sliced into numbered
parts, and the marker it stamps into the cut body names the id and the part
count. The model reads its own way out at the exact moment it needs it.

Why fixed slices of the WHOLE body, and not "the middle that was dropped"
--------------------------------------------------------------------------
Reconstructing precisely what was omitted would mean modelling both truncation
passes at once - one of which water-fills a per-body cap by binary search
against a payload whose size depends on every OTHER body in the turn - and then
staying correct as either changes. The slices here are computed from the
original body alone, before anything is cut, so they are stable, addressable
and independent of which pass did the cutting: part 3 of id c7 means one thing
forever. The price is that a part may repeat text the model already saw at the
head or tail of the truncated body. That is the cheap half of the trade.

Why a part always fits
----------------------
:func:`chunk_chars_for` sizes a part so that one part per turn can never itself
be truncated: it is clamped under `limits.max_result_chars` (or the per-result
pass would cut it) and held to a fraction of `max_paste_chars` (or the payload
pass would), with room reserved for the one-line header a fetch carries. A
model that fetches two parts in one reply is back in a crowded payload and may
get cut again - hence "one part per turn" in the marker, which is advice, not
an enforced limit.

The cache itself is the engine's (`Engine._chunk_cache`); this module owns only
its SHAPE and its lifetime rule is written where the eviction happens. Nothing
here is on the wire: `Outbound`/`ToolResult` are unchanged, so a remote session
needs no protocol change - the cache lives beside the engine that composed the
payload, which in a remote session is the engine on the target.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentclip.config import MAX_RETAINED_RESULT_CHARS
from agentclip.executor.tools.registry import (
    ToolContext,
    ToolError,
    ToolSpec,
    int_param,
    require,
    tool_handler,
)
from agentclip.protocol.types import ToolCall

# The most of one body the cache will hold - this module's name for the one
# retention number the system has (config.MAX_RETAINED_RESULT_CHARS, where the
# reasoning about its size lives). It is the SAME number the handlers guard their
# own output with, and that is the whole point: a handler that kept less than
# this could hand the cache a body already gutted, and no marker here would be
# able to say so. When it does bite, :func:`chunk_marker` says so.
CACHE_BODY_CAP = MAX_RETAINED_RESULT_CHARS

# How much of the paste budget one fetched part may occupy. Not the whole
# budget: a fetch shares its payload with the envelope, any notes, and whatever
# else the model called in the same reply, and a part that needed the entire
# budget to itself would be truncated by the very pass it exists to escape.
CHUNK_BUDGET_FRACTION = 0.60

# Room kept clear inside a part for the one-line header a fetch prefixes (and
# for the heredoc framing around it). Without it a part sized exactly at
# `max_result_chars` would come back one header longer than the per-result cap
# and be cut - the one truncation that must never happen.
FETCH_HEADER_RESERVE = 200

# Absolute floor for a part, for a config whose `max_result_chars` is set near
# its own 200-char minimum. It can exceed the cap it was clamped to, which means
# a fetch on such a config may itself be truncated - and then re-cached under a
# fresh id, so the model can still drill down. What the floor really prevents is
# a zero or negative chunk size, which has no sane slicing at all.
_MIN_CHUNK_CHARS = 200


def chunk_chars_for(max_paste_chars: int, max_result_chars: int) -> int:
    """The size of one cached part, from the two caps that could cut it."""
    room = min(int(max_paste_chars * CHUNK_BUDGET_FRACTION), max_result_chars)
    return max(_MIN_CHUNK_CHARS, room - FETCH_HEADER_RESERVE)


def slice_body(body: str, chunk_chars: int) -> tuple[str, ...]:
    """``body`` as contiguous parts of at most ``chunk_chars``, cut on line ends.

    ``"".join(slice_body(b, n)) == b`` for every b and every n >= 1: the parts
    are the body, in order, with nothing added and nothing dropped. That is the
    property the tool's promise rests on - a model reassembling parts 1..K must
    get back exactly what the tool printed, whitespace included.

    Lines are kept whole where they fit; a single line longer than a part is
    hard-split, because output with no newlines in it (minified JSON, a base64
    blob) is precisely the kind that overflows a budget, and refusing to cut it
    would mean a part that cannot be served.
    """
    if chunk_chars < 1:
        raise ValueError(f"chunk_chars must be >= 1, got {chunk_chars}")
    if not body:
        return ("",)
    pieces: list[str] = []
    lines = body.split("\n")
    last = len(lines) - 1
    for i, line in enumerate(lines):
        piece = line if i == last else line + "\n"
        while len(piece) > chunk_chars:
            pieces.append(piece[:chunk_chars])
            piece = piece[chunk_chars:]
        if piece:  # empty only for the tail after a trailing newline
            pieces.append(piece)
    parts: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > chunk_chars:
            parts.append(current)
            current = piece
        else:
            current += piece
    if current:
        parts.append(current)
    return tuple(parts)


def chunk_marker(chunk_id: str, parts: int, *, capped: bool = False) -> str:
    """The in-band marker for a body whose full text IS cached under ``chunk_id``.

    It teaches the exact call, because the moment the model reads it is the
    moment it needs it - a catalog entry three thousand characters up the
    conversation is not where a recovery instruction lands. Kept to one line and
    close to the plain marker's length: it is stamped into a body that is being
    cut precisely because there was no room.
    """
    what = f"first {CACHE_BODY_CAP} chars cached" if capped else "full output cached"
    return (
        f"[truncated by AgentClip - {what}: fetch_chunk id={chunk_id}"
        f" part=1..{parts}, one part per turn]"
    )


@dataclass(frozen=True, slots=True)
class CachedChunks:
    """One truncated result's full body, pre-sliced and ready to serve.

    Built by :meth:`of` BEFORE composition, because the marker has to name the
    part count and the part count is only known once the body is sliced. The
    engine mints one of these per candidate result, composes, and then keeps
    only the ones whose ``marker`` actually survived into the rendered payload -
    derived truth, the same discipline `_record_numbered_reads` follows: what
    the model can act on is what the model was really shown.
    """

    chunk_id: str
    marker: str
    parts: tuple[str, ...]
    call_id: int
    turn: int
    tool: str
    original_chars: int
    capped: bool  # the body was longer than CACHE_BODY_CAP; the tail is not held

    @classmethod
    def of(
        cls,
        chunk_id: str,
        body: str,
        *,
        call_id: int,
        turn: int,
        tool: str,
        chunk_chars: int,
        body_cap: int = CACHE_BODY_CAP,
    ) -> CachedChunks:
        kept = body[:body_cap]
        capped = len(kept) < len(body)
        parts = slice_body(kept, chunk_chars)
        return cls(
            chunk_id=chunk_id,
            marker=chunk_marker(chunk_id, len(parts), capped=capped),
            parts=parts,
            call_id=call_id,
            turn=turn,
            tool=tool,
            original_chars=len(body),
            capped=capped,
        )

    def header(self, part: int) -> str:
        """The one line a served part is prefixed with: where this text came from.

        One line and no more. The body below it is the tool's own output, and a
        multi-line preamble is indistinguishable from output to whatever the
        model does next with it.
        """
        origin = f"call {self.call_id}" + (f" ({self.tool})" if self.tool else "")
        return (
            f"part {part}/{len(self.parts)} of {origin} from turn {self.turn}"
            f" ({self.original_chars} chars total)"
        )


# Two lines and no worked example, unlike every other entry, because the
# bootstrap is the payload with no chunked fallback and ~250 chars of measured
# slack in its worst configuration (docs/design/protocol.md section 2, "Budget
# headroom"). It can afford to be this short because it is the one tool whose
# syntax is taught at the point of use: :func:`chunk_marker` spells out the id
# and the range inside the very body the model is reading when it needs them,
# which is both more specific than an example could be and costs the bootstrap
# nothing. Keep it under ~100 chars.
FETCH_CHUNK_DOC = """\
fetch_chunk(id*, part*)
  One part of a truncated result; its marker names the id and part count."""


def _unknown_chunk_hint(cache: dict[str, CachedChunks]) -> str:
    held = ", ".join(sorted(cache)) if cache else "nothing"
    return (
        "the cache only holds the last payload that truncated something, so this id has"
        f" expired - re-run the tool that produced the output. Cached now: {held}."
    )


@tool_handler
def _fetch_chunk(ctx: ToolContext, call: ToolCall) -> str:
    require(call, "id", "part")
    # Quotes stripped because the marker reads `id=c7` while a model that has
    # met JSON tools elsewhere may well write `id: "c7"`, and refusing that
    # would spend a turn teaching punctuation.
    chunk_id = call.params["id"].strip().strip("\"'")
    entry = ctx.chunk_cache.get(chunk_id)
    if entry is None:
        raise ToolError(
            "unknown_chunk",
            f"no cached output under id {chunk_id!r}",
            _unknown_chunk_hint(ctx.chunk_cache),
        )
    part = int_param(call, "part", 1)
    if not 1 <= part <= len(entry.parts):
        raise ToolError(
            "bad_param",
            f"part {part} does not exist: id={chunk_id} has {len(entry.parts)} part(s)",
            f"resend fetch_chunk with a part between 1 and {len(entry.parts)}.",
        )
    return f"{entry.header(part)}\n{entry.parts[part - 1]}"


# Always registered, never gated: truncation is a property of the transport, not
# of a preset or a permission, so there is no configuration in which a body can
# be cut and this tool be absent. `auto` approval and an allow-by-default rule
# for the same reason `mcp_schema` has them - it reads a cache the engine filled
# from output the user already approved, touches no file, and runs no command.
FETCH_CHUNK_SPEC = ToolSpec("fetch_chunk", "auto", _fetch_chunk, None, FETCH_CHUNK_DOC)
