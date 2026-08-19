"""Tests for session chat names (agentclip.protocol.names)."""

from __future__ import annotations

import random
import re

from agentclip.protocol.names import (
    ADJECTIVES,
    CHAT_NAME_RE,
    NOUNS,
    generate_chat_name,
    normalize_chat_name,
)

_FORM = re.compile(r"^[a-z]+-[a-z]+$")


def test_word_lists_are_large_and_clean() -> None:
    for words in (ADJECTIVES, NOUNS):
        assert len(words) >= 40
        assert len(set(words)) == len(words)  # no duplicates
        assert all(re.fullmatch(r"[a-z]+", w) for w in words)


def test_generated_name_is_adjective_hyphen_noun() -> None:
    for _ in range(200):
        name = generate_chat_name()
        assert _FORM.match(name)
        assert CHAT_NAME_RE.match(name)
        adjective, _, noun = name.partition("-")
        assert adjective in ADJECTIVES
        assert noun in NOUNS


def test_generation_is_deterministic_for_a_seeded_rng() -> None:
    a = [generate_chat_name(random.Random(1234)) for _ in range(5)]
    assert len(set(a)) == 1  # a fresh Random(1234) always yields the same name
    first = random.Random(99)
    second = random.Random(99)
    assert [generate_chat_name(first) for _ in range(10)] == [
        generate_chat_name(second) for _ in range(10)
    ]


def test_names_vary() -> None:
    rng = random.Random(7)
    assert len({generate_chat_name(rng) for _ in range(50)}) > 1


def test_normalize_strips_case_whitespace_and_decoration() -> None:
    assert normalize_chat_name("  Amber-Falcon ") == "amber-falcon"
    assert normalize_chat_name("`amber-falcon`") == "amber-falcon"
    assert normalize_chat_name('"amber-falcon"') == "amber-falcon"
    assert normalize_chat_name("'amber-falcon'") == "amber-falcon"


def test_normalize_maps_empty_and_none_to_none() -> None:
    assert normalize_chat_name(None) is None
    assert normalize_chat_name("") is None
    assert normalize_chat_name("   ") is None
    assert normalize_chat_name("``") is None
