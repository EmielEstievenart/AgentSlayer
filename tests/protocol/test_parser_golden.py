"""Golden-file tests for the CLIP/1 reply parser, plus unit tests for the
tolerances and property-style tests for normalized_hash stability.

Golden fixtures are byte-exact pairs NNN-name.input.txt / NNN-name.expected.json
in tests/protocol/golden/ (committed with `* -text` so CRLF/BOM bytes survive
checkout). The input is decoded as plain UTF-8 (NOT utf-8-sig: a BOM must reach
the parser) and the ParsedReply is serialized to a stable JSON shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentclip.protocol.parser import (
    looks_like_protocol,
    normalize,
    normalized_hash,
    parse_reply,
)
from agentclip.protocol.types import ParsedReply, ParseIssue

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_INPUTS = sorted(GOLDEN_DIR.glob("*.input.txt"))


def _issue_to_json(issue: ParseIssue) -> dict[str, object]:
    return {"kind": issue.kind, "line": issue.line, "detail": issue.detail}


def to_json(reply: ParsedReply) -> dict[str, object]:
    """Stable JSON shape for golden comparison. `raw` (verbatim block text) and
    `normalized_hash` are deliberately excluded -- raw is asserted separately
    and the hash is covered by the property tests below."""
    return {
        "kind": reply.kind,
        "calls": [
            {
                "id": call.id,
                "tool": call.tool,
                "params": call.params,
                "original_id": call.original_id,
                "issues": [_issue_to_json(i) for i in call.issues],
            }
            for call in reply.calls
        ],
        "prose": list(reply.prose),
        "warnings": [_issue_to_json(i) for i in reply.warnings],
        "eom": {"present": reply.eom.present, "calls": reply.eom.calls, "turn": reply.eom.turn},
        "truncated": reply.truncated,
        "ack_part": reply.ack_part,
        "ack_total": reply.ack_total,
        "nack_reason": reply.nack_reason,
    }


# --- golden corpus -----------------------------------------------------------


def test_golden_corpus_is_complete() -> None:
    """Every required fixture from the plan exists and every input has its
    expected twin."""
    names = {p.name.removesuffix(".input.txt") for p in GOLDEN_INPUTS}
    required = {
        "001-two-calls-fenced",
        "002-two-calls-crlf-nofence",
        "010-fenced-blocks",
        "011-crlf",
        "012-bom",
        "013-perplexity-citation-tail",
        "014-copilot-said-prefix",
        "015-ack",
        "016-nack",
        "017-heredoc-protocol-content",
        "020-missing-end",
        "021-unterminated-heredoc",
        "022-duplicate-ids",
        "023-truncated-mid-block",
        "024-calls-mismatch",
        "025-noise",
        "026-swallowed-call-recovery",
    }
    assert required <= names
    for p in GOLDEN_INPUTS:
        assert p.with_name(p.name.replace(".input.txt", ".expected.json")).is_file()


@pytest.mark.parametrize(
    "input_path",
    GOLDEN_INPUTS,
    ids=[p.name.removesuffix(".input.txt") for p in GOLDEN_INPUTS],
)
def test_golden(input_path: Path) -> None:
    text = input_path.read_bytes().decode("utf-8")
    expected_path = input_path.with_name(input_path.name.replace(".input.txt", ".expected.json"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert to_json(parse_reply(text)) == expected


def test_fixture_bytes_are_what_they_claim() -> None:
    """Guard against checkout/tooling mangling the byte-exact fixtures."""
    assert b"\r\n" in (GOLDEN_DIR / "002-two-calls-crlf-nofence.input.txt").read_bytes()
    assert b"\r\n" in (GOLDEN_DIR / "011-crlf.input.txt").read_bytes()
    assert (GOLDEN_DIR / "012-bom.input.txt").read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\xc2\xa0" in (GOLDEN_DIR / "013-perplexity-citation-tail.input.txt").read_bytes()


# --- looks_like_protocol -----------------------------------------------------


def test_looks_like_protocol_is_literal_substring_test() -> None:
    assert looks_like_protocol("===CLIP:EOM===")
    assert looks_like_protocol("blah\nfoo ===CLIP:ACK 1/2=== bar")
    assert not looks_like_protocol("=== CLIP:EOM ===")  # spaced variant fails the pre-filter
    assert not looks_like_protocol("plain prose, no protocol")
    assert not looks_like_protocol("")


# --- ACK / NACK forms --------------------------------------------------------


def test_ack_attr_form_accepted() -> None:
    reply = parse_reply("===CLIP:ACK part=2 total=3===\n")
    assert reply.kind == "ack"
    assert (reply.ack_part, reply.ack_total) == (2, 3)


def test_nack_positional_with_reason() -> None:
    reply = parse_reply("===CLIP:NACK 2/3 reason=truncated===\n")
    assert reply.kind == "nack"
    assert (reply.ack_part, reply.ack_total) == (2, 3)
    assert reply.nack_reason == "truncated"


def test_nack_without_part_info() -> None:
    reply = parse_reply("===CLIP:NACK reason=truncated===")
    assert reply.kind == "nack"
    assert reply.ack_part is None and reply.ack_total is None
    assert reply.nack_reason == "truncated"
    assert not reply.truncated  # ACK/NACK replies carry no EOM by design


# --- sentinel and param tolerances --------------------------------------------


def test_sentinel_case_and_equals_run_variance() -> None:
    reply = parse_reply(
        "====clip:call ID=1 tool=read_file=\npath: a.py\n====Clip:End\n===CLIP:eom calls=1"
    )
    assert reply.kind == "reply"
    assert len(reply.calls) == 1
    assert reply.calls[0].tool == "read_file"
    assert reply.calls[0].params == {"path": "a.py"}
    assert reply.eom.present and reply.eom.calls == 1
    assert not reply.truncated
    assert reply.warnings == ()


def test_key_equals_value_param_form_and_unknown_params_kept() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=edit_file===\n"
        "path=src/x.py\n"
        "occurrence=all\n"
        "bogus_extra: kept verbatim\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
    )
    # Parser does not validate params or tool names -- everything is kept.
    assert reply.calls[0].params == {
        "path": "src/x.py",
        "occurrence": "all",
        "bogus_extra": "kept verbatim",
    }


def test_triple_angle_heredoc_opener_tolerated() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=write_file===\n"
        "path: a.txt\n"
        "content <<<EOT\n"
        "hello\n"
        "EOT\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
    )
    assert reply.calls[0].params["content"] == "hello"
    assert not reply.truncated


def test_heredoc_tag_is_case_sensitive_and_trim_terminated() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=write_file===\n"
        "content <<EOT\n"
        "eot\n"  # wrong case: stays content
        "  EOT  \n"  # whitespace-trimmed terminator
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
    )
    assert reply.calls[0].params["content"] == "eot"
    assert not reply.truncated


def test_unknown_tool_name_passed_through_unvalidated() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=summon_demon===\nname: bob\n===CLIP:END===\n===CLIP:EOM calls=1===\n"
    )
    assert reply.calls[0].tool == "summon_demon"
    assert reply.calls[0].issues == ()


def test_missing_tool_attr_flagged_as_bad_header() -> None:
    reply = parse_reply("===CLIP:CALL id=1===\npath: a.py\n===CLIP:END===\n===CLIP:EOM calls=1===\n")
    assert reply.calls[0].tool == ""
    assert [i.kind for i in reply.calls[0].issues] == ["bad_header"]


# --- id renumbering ------------------------------------------------------------


def test_non_integer_id_renumbered_with_original_preserved() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=abc tool=read_file===\npath: a.py\n===CLIP:END===\n===CLIP:EOM calls=1===\n"
    )
    call = reply.calls[0]
    assert call.id == 1
    assert call.original_id == "abc"
    assert [w.kind for w in reply.warnings] == ["renumbered"]


def test_missing_id_assigned_with_warning() -> None:
    reply = parse_reply(
        "===CLIP:CALL tool=read_file===\npath: a.py\n===CLIP:END===\n===CLIP:EOM calls=1===\n"
    )
    call = reply.calls[0]
    assert call.id == 1
    assert call.original_id is None
    assert [w.kind for w in reply.warnings] == ["renumbered"]


# --- echoed outbound payloads ---------------------------------------------------


def test_echoed_results_payload_parses_as_prose_without_calls() -> None:
    text = (
        "===CLIP:RESULTS turn=4===\n"
        "===CLIP:RESULT id=1 status=ok===\n"
        "body <<R1\n"
        "replaced 1 occurrence at line 88\n"
        "R1\n"
        "===CLIP:END===\n"
        "===CLIP:EOM===\n"
    )
    reply = parse_reply(text)
    assert reply.kind == "reply"
    assert reply.calls == ()
    assert reply.eom.present
    assert not reply.truncated
    assert any("RESULT" in chunk for chunk in reply.prose)


def test_echoed_task_and_note_blocks_are_prose() -> None:
    text = "===CLIP:TASK===\nfix the bug in utils\n===CLIP:NOTE===\nundone turn 3\n===CLIP:EOM===\n"
    reply = parse_reply(text)
    assert reply.calls == ()
    assert reply.kind == "reply"
    assert "fix the bug in utils" in "\n".join(reply.prose)


def test_echoed_part_handshake_lines_are_prose() -> None:
    text = "===CLIP:PART 2/3===\nsome payload line\n===CLIP:PART-END 2/3===\n"
    reply = parse_reply(text)
    assert reply.kind == "reply"
    assert reply.calls == ()
    assert reply.ack_part is None  # PART is not an ACK


# --- raw fidelity ----------------------------------------------------------------


def test_raw_preserves_verbatim_block_text() -> None:
    text = (
        "===CLIP:CALL id=1 tool=run_command===\n"
        "command: pytest -q\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
    )
    call = parse_reply(text).calls[0]
    assert call.raw.startswith("===CLIP:CALL id=1 tool=run_command===")
    assert call.raw.endswith("===CLIP:END===")
    assert "command: pytest -q" in call.raw


# --- normalize -------------------------------------------------------------------


def test_normalize_strips_bom_and_crlf() -> None:
    assert normalize("\ufeffa\r\nb\rc") == "a\nb\nc"


def test_normalize_fixes_nbsp_on_sentinel_lines_only() -> None:
    nbsp_sentinel = "===CLIP:EOM\u00a0calls=1==="
    body = "data\u00a0line"
    out = normalize(f"{nbsp_sentinel}\n{body}")
    assert out.splitlines()[0] == "===CLIP:EOM calls=1==="
    assert out.splitlines()[1] == body  # non-sentinel lines untouched


# --- normalized_hash properties ----------------------------------------------------


_HASH_BASE = (
    "===CLIP:CALL id=1 tool=edit_file===\n"
    "path: src/utils.py\n"
    "find <<EOT\n"
    "    old line\n"
    "EOT\n"
    "replace <<EOT\n"
    "    new line\n"
    "EOT\n"
    "===CLIP:END===\n"
    "===CLIP:EOM calls=1===\n"
)


def test_hash_stable_fenced_vs_unfenced() -> None:
    fenced = "~~~~\n" + _HASH_BASE + "~~~~\n"
    backtick_fenced = "```text\n" + _HASH_BASE + "```\n"
    assert normalized_hash(fenced) == normalized_hash(_HASH_BASE)
    assert normalized_hash(backtick_fenced) == normalized_hash(_HASH_BASE)


def test_hash_stable_crlf_vs_lf() -> None:
    assert normalized_hash(_HASH_BASE.replace("\n", "\r\n")) == normalized_hash(_HASH_BASE)


def test_hash_stable_bom_vs_no_bom() -> None:
    assert normalized_hash("\ufeff" + _HASH_BASE) == normalized_hash(_HASH_BASE)


def test_hash_stable_per_line_trailing_whitespace() -> None:
    padded = "".join(line + "   \n" for line in _HASH_BASE.splitlines())
    assert normalized_hash(padded) == normalized_hash(_HASH_BASE)


def test_hash_differs_for_different_payloads() -> None:
    assert normalized_hash(_HASH_BASE) != normalized_hash(_HASH_BASE.replace("new", "newer"))


def test_hash_is_blake2b_128_hex_and_matches_parse_reply() -> None:
    h = normalized_hash(_HASH_BASE)
    assert len(h) == 32  # 16 bytes -> 32 hex chars
    int(h, 16)  # valid hex
    assert parse_reply(_HASH_BASE).normalized_hash == h


def test_hash_golden_fixture_pair_001_is_fence_invariant() -> None:
    """001 (fenced) and 002 (unfenced CRLF) carry the same protocol payload,
    so their dedup hashes must collide by construction."""
    fenced = (GOLDEN_DIR / "001-two-calls-fenced.input.txt").read_bytes().decode("utf-8")
    unfenced = (GOLDEN_DIR / "002-two-calls-crlf-nofence.input.txt").read_bytes().decode("utf-8")
    assert normalized_hash(fenced) == normalized_hash(unfenced)
