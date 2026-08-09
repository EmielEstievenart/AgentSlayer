"""Unit tests for the streamed-delivery splitter (``clip.chunking``).

Pure text in, pure text out - no clipboard, no screen. The properties that
matter are the ones a chunk lands in a live chat box against: nothing is lost or
added, a newline is preferred as the seam, and two joins may never fall inside a
CRLF or a surrogate pair.
"""

from __future__ import annotations

import pytest

from agentclip.clip.chunking import STREAM_CHUNK_CHARS, split_for_stream


def test_short_text_is_one_chunk() -> None:
    assert split_for_stream("hello", limit=100) == ["hello"]


def test_empty_text_is_still_one_chunk() -> None:
    """The caller pastes what it is given; "nothing to send" is not this
    function's decision to make."""
    assert split_for_stream("", limit=100) == [""]


def test_exactly_the_limit_is_one_chunk() -> None:
    text = "x" * 50
    assert split_for_stream(text, limit=50) == [text]


def test_one_over_the_limit_splits() -> None:
    text = "x" * 51
    chunks = split_for_stream(text, limit=50)
    assert chunks == ["x" * 50, "x"]


def test_a_line_without_newlines_is_hard_split() -> None:
    text = "y" * 25
    chunks = split_for_stream(text, limit=10)
    assert chunks == ["y" * 10, "y" * 10, "y" * 5]
    assert "".join(chunks) == text


def test_the_seam_prefers_the_last_newline_in_the_window() -> None:
    """The break lands AFTER the newline, so the line break travels with the
    line it ends rather than opening the next chunk."""
    text = "aaaa\nbbbb\ncccc\n"
    chunks = split_for_stream(text, limit=12)
    assert chunks == ["aaaa\nbbbb\n", "cccc\n"]


def test_a_crlf_pair_is_never_split() -> None:
    """A bare CR arriving on its own is a line ending some editors act on; the
    cut backs off one character rather than produce one."""
    text = "ab\r\ncd"
    chunks = split_for_stream(text, limit=3)
    assert all(not chunk.endswith("\r") for chunk in chunks)
    assert "".join(chunks) == text


def test_a_surrogate_pair_is_never_split() -> None:
    """An astral character is one code point in Python, so this only ever bites
    on the surrogate PAIRS that survive a surrogatepass decode - which is
    exactly how a payload reaches the clipboard. A cut between the halves would
    hand a backend a lone surrogate, which is not text at all."""
    # chr() rather than a literal: a source-level emoji is ONE code point in
    # Python 3 and could never be split - the pair is the case under test.
    text = "ab" + chr(0xD83D) + chr(0xDE00) + "cd"  # U+1F600, still in halves
    chunks = split_for_stream(text, limit=3)
    assert chunks[0] == "ab"  # backed off one rather than land between the halves
    assert "".join(chunks) == text


def test_nothing_is_lost_or_added_across_a_mixed_payload() -> None:
    text = ("a line\r\n" * 40) + ("z" * 300) + "\n" + ("tail\n" * 5)
    for limit in (7, 16, 64, 199, 1_000):
        chunks = split_for_stream(text, limit=limit)
        assert "".join(chunks) == text, limit
        assert all(chunks), limit  # an empty chunk would be a wasted paste
        assert all(len(chunk) <= limit for chunk in chunks), limit


def test_the_default_limit_is_the_module_constant() -> None:
    text = "q" * (STREAM_CHUNK_CHARS + 1)
    assert len(split_for_stream(text)) == 2


def test_a_zero_limit_is_refused() -> None:
    with pytest.raises(ValueError):
        split_for_stream("anything", limit=0)
