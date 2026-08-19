"""peek_chat_name: which chat a pasted text claims to come from."""

from __future__ import annotations

from agentclip.protocol.parser import peek_chat_name

REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def test_reads_the_eom_chat_name() -> None:
    assert peek_chat_name(REPLY) == "amber-falcon"


def test_absent_chat_attribute_is_none() -> None:
    assert peek_chat_name(REPLY.replace(" chat=amber-falcon", "")) is None


def test_text_without_any_sentinel_is_none() -> None:
    assert peek_chat_name("Sure! Here is what I would do...") is None
    assert peek_chat_name("") is None


def test_a_call_block_alone_carries_no_chat_name() -> None:
    # Only EOM/ACK/NACK lines are stamped; a truncated reply has no name.
    truncated = "===CLIP:CALL id=1 tool=read_file===\npath: README.md\n"
    assert peek_chat_name(truncated) is None


def test_ack_and_nack_lines_are_routable_too() -> None:
    assert peek_chat_name("===CLIP:ACK 2/3 chat=silver-otter===") == "silver-otter"
    assert (
        peek_chat_name("===CLIP:NACK reason=truncated chat=silver-otter===")
        == "silver-otter"
    )


def test_last_eom_wins() -> None:
    # A model that quotes an older message and then ends its own reply: the
    # trailing line is the one that says where this paste came from.
    text = (
        "===CLIP:EOM turn=4 chat=amber-falcon===\n"
        "here is my actual reply\n"
        "===CLIP:EOM calls=0 chat=silver-otter===\n"
    )
    assert peek_chat_name(text) == "silver-otter"


def test_survives_crlf_bom_and_fences() -> None:
    fenced = "~~~~\r\n" + REPLY.replace("\n", "\r\n") + "~~~~\r\n"
    assert peek_chat_name("﻿" + fenced) == "amber-falcon"


def test_normalizes_case_and_quoting_like_the_engine_gate() -> None:
    assert peek_chat_name(REPLY.replace("amber-falcon", "`Amber-Falcon`")) == "amber-falcon"


def test_trailing_whitespace_and_decorative_equals_tolerated() -> None:
    assert peek_chat_name("===CLIP:EOM calls=1 chat=amber-falcon=====   \n") == "amber-falcon"
