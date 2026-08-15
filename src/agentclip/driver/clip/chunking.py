"""Splitting an outbound payload into the pieces a streamed delivery pastes.

The transport half of ``ServicePreset.delivery == "stream"``: a web chat box
handed one very large paste stalls for seconds with nothing on screen, so the
payload is walked in as a run of clipboard-write + Ctrl+V bursts instead. This
module owns only the split; the pasting lives in the TUI's ``copy_outbound``.

A leaf, like the rest of ``clip``: pure text in, pure text out, no clipboard and
no OS calls, so the boundary rules below are unit-testable on their own.

Chunks are pasted into a live editor, so two joins may never fall between the
halves of one thing: a CRLF pair (the browser would see a bare CR, and some
editors turn that into a submitted line) and a surrogate pair (a lone surrogate
is not text at all - payloads reach us through ``surrogatepass``). Newlines are
preferred as boundaries for the same reason the size limit is soft: a chunk that
ends mid-word is invisible in the box, but one that ends mid-line is where a
dropped burst is hardest to spot.
"""

from __future__ import annotations

# Big enough that a normal turn is a handful of bursts rather than dozens, small
# enough that each one lands in a web chat box without the stall this exists to
# avoid. Not derived from ``max_paste_chars``: that is the SERVICE's budget for
# one whole message, and a stream is one message either way.
STREAM_CHUNK_CHARS = 1_500


def _safe_cut(text: str, cut: int) -> int:
    """``cut``, moved back one if it would land inside a pair that must not be
    split. Only ever moves back by one - both pairs are two units long."""
    if 0 < cut < len(text):
        before, after = text[cut - 1], text[cut]
        if (before == "\r" and after == "\n") or (
            "\ud800" <= before <= "\udbff" and "\udc00" <= after <= "\udfff"
        ):
            return cut - 1
    return cut


def split_for_stream(text: str, limit: int = STREAM_CHUNK_CHARS) -> list[str]:
    """``text`` as the chunks a streamed delivery pastes, in order.

    Always at least one chunk, and ``"".join(split_for_stream(t)) == t`` for
    every input - the payload is delivered whole or the caller falls back, so
    the split may never lose or add a character. Text that already fits is a
    single chunk (an exactly-``limit``-long payload included), which is what
    keeps a short outbound in stream mode from costing more than one burst.

    Greedy, with a soft boundary: each chunk takes up to ``limit`` characters
    and ends after the last newline in that window, falling back to a hard cut
    at ``limit`` when the window holds no newline at all (one unbroken line, a
    minified blob). ``_safe_cut`` then keeps that cut out of a CRLF or surrogate
    pair, so a hard-cut chunk can come back one character short.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        if len(text) - start <= limit:
            chunks.append(text[start:])
            break
        window_end = start + limit
        newline = text.rfind("\n", start, window_end)
        # Past the newline, not before it: the line break belongs to the chunk
        # that carries the line, so a rejoin cannot double or drop one.
        cut = _safe_cut(text, newline + 1 if newline >= start else window_end)
        if cut <= start:  # limit=1 against a pair that may not be split
            cut = start + limit
        chunks.append(text[start:cut])
        start = cut
    return chunks
