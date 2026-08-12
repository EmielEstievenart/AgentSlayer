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
    peek_chat_name,
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
        "eom": {
            "present": reply.eom.present,
            "calls": reply.eom.calls,
            "turn": reply.eom.turn,
            "chat": reply.eom.chat,
        },
        "truncated": reply.truncated,
        "ack_part": reply.ack_part,
        "ack_total": reply.ack_total,
        "ack_chat": reply.ack_chat,
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
        "027-flattened-eom",
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


def test_ack_carries_the_chat_name() -> None:
    reply = parse_reply("===CLIP:ACK 1/3 chat=amber-falcon===\n")
    assert reply.kind == "ack"
    assert reply.ack_chat == "amber-falcon"
    assert (reply.ack_part, reply.ack_total) == (1, 3)


def test_nack_carries_the_chat_name() -> None:
    reply = parse_reply("===CLIP:NACK reason=truncated chat=amber-falcon===\n")
    assert reply.kind == "nack"
    assert reply.ack_chat == "amber-falcon"
    assert reply.nack_reason == "truncated"


def test_ack_without_chat_name_parses_with_chat_none() -> None:
    # The parser never rejects: the engine's ingest gate decides.
    assert parse_reply("===CLIP:ACK 1/3===").ack_chat is None


# --- chat name on the EOM ------------------------------------------------------


def _eom_reply(eom: str) -> str:
    return f"===CLIP:CALL id=1 tool=read_file===\npath: a.py\n===CLIP:END===\n{eom}\n"


def test_eom_chat_name_parsed() -> None:
    reply = parse_reply(_eom_reply("===CLIP:EOM calls=1 chat=amber-falcon==="))
    assert reply.eom.present
    assert reply.eom.chat == "amber-falcon"
    assert reply.eom.turn is None  # turn is optional now
    assert not reply.truncated


def test_eom_chat_and_turn_together() -> None:
    """turn= stays parseable: AgentClip still stamps it on its own payloads and
    a model echoing both must not be rejected."""
    reply = parse_reply(_eom_reply("===CLIP:EOM calls=1 turn=7 chat=amber-falcon==="))
    assert (reply.eom.calls, reply.eom.turn, reply.eom.chat) == (1, 7, "amber-falcon")


def test_eom_chat_order_free_and_case_insensitive() -> None:
    reply = parse_reply(_eom_reply("===CLIP:EOM CHAT=Amber-Falcon calls=1==="))
    assert reply.eom.chat == "amber-falcon"


def test_eom_chat_strips_decorative_quoting() -> None:
    assert parse_reply(_eom_reply("===CLIP:EOM calls=1 chat=`amber-falcon`===")).eom.chat == (
        "amber-falcon"
    )
    assert parse_reply(_eom_reply('===CLIP:EOM calls=1 chat="amber-falcon"===')).eom.chat == (
        "amber-falcon"
    )


def test_eom_without_chat_is_present_with_chat_none() -> None:
    reply = parse_reply(_eom_reply("===CLIP:EOM calls=1==="))
    assert reply.eom.present and reply.eom.chat is None


def test_missing_eom_has_no_chat_and_flags_truncation() -> None:
    reply = parse_reply("===CLIP:CALL id=1 tool=read_file===\npath: a.py\n===CLIP:END===\n")
    assert not reply.eom.present
    assert reply.eom.chat is None
    assert reply.truncated


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


_SPACED_BLOCK = (
    "===CLIP:CALL id=1 tool=write_file===\n"
    "path: a.txt\n"
    "{opener}\n"
    "hello\n"
    "world\n"
    "EOT\n"
    "===CLIP:END===\n"
    "===CLIP:EOM calls=1===\n"
)


@pytest.mark.parametrize(
    "opener",
    [
        "content << EOT",  # canonical since the HTML-mangling incident
        "content <<EOT",  # the old canonical spelling, still accepted
        "content <<  EOT",
        "content: << EOT",
        "content <<< EOT",
    ],
)
def test_heredoc_opener_spacing_is_equivalent(opener: str) -> None:
    reply = parse_reply(_SPACED_BLOCK.format(opener=opener))
    assert reply.calls[0].params == {"path": "a.txt", "content": "hello\nworld"}
    assert reply.calls[0].issues == ()
    assert not reply.truncated


def test_tilde_line_inside_heredoc_is_content_not_a_fence() -> None:
    # The fence-collision rule (bootstrap) exists for the chat client's markdown
    # renderer, which would close a ~~~~ fence early. The parser is immune either
    # way: inside a heredoc only the tag terminates, so tildes are just content.
    reply = parse_reply(
        "~~~~~\n"  # outer fence, wider than the content line below
        "===CLIP:CALL id=1 tool=write_file===\n"
        "path: README.md\n"
        "content << EOT\n"
        "~~~~\n"
        "still content\n"
        "EOT\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
        "~~~~~\n"
    )
    assert reply.calls[0].params["content"] == "~~~~\nstill content"
    assert not reply.truncated


# --- saw_fence: was there a fence at a STRUCTURAL position? ------------------
#
# Recorded by the parser, acted on by the engine (tolerance #15). "Structural"
# is the whole definition: a fence line the parser SKIPPED while reading
# grammar. Heredoc content is never read as grammar, so a reply writing a
# markdown file cannot look fenced because the file it writes is.

_UNFENCED_REPLY = (
    "===CLIP:CALL id=1 tool=read_file===\n"
    "path: README.md\n"
    "===CLIP:END===\n"
    "===CLIP:EOM calls=1 chat=amber-falcon===\n"
)


def test_saw_fence_is_true_for_a_tilde_fenced_reply() -> None:
    assert parse_reply(f"~~~~\n{_UNFENCED_REPLY}~~~~\n").saw_fence


def test_saw_fence_is_true_for_a_backtick_fenced_reply() -> None:
    # Backticks are discouraged (file content is full of them) but tolerated,
    # and a reply that used them still proves fence lines crossed the wire.
    assert parse_reply(f"```text\n{_UNFENCED_REPLY}```\n").saw_fence


def test_saw_fence_is_true_for_a_fence_inside_a_call_body() -> None:
    # A model that fences each block separately: the fence lands inside the
    # CALL-body scan rather than at top level, and counts just the same.
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=read_file===\n"
        "```\n"
        "path: README.md\n"
        "```\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 chat=amber-falcon===\n"
    )
    assert reply.saw_fence
    assert reply.calls[0].params == {"path": "README.md"}


def test_saw_fence_is_false_for_an_unfenced_reply() -> None:
    assert not parse_reply(_UNFENCED_REPLY).saw_fence


def test_a_fence_inside_heredoc_content_does_not_count() -> None:
    """The point of defining saw_fence structurally: a reply WRITING a markdown
    file is full of ``` lines, and none of them says anything about how the
    reply itself was rendered."""
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=write_file===\n"
        "path: README.md\n"
        "content << EOT\n"
        "# Title\n"
        "```python\n"
        "print('hi')\n"
        "```\n"
        "~~~~\n"
        "EOT\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 chat=amber-falcon===\n"
    )
    assert not reply.saw_fence
    assert reply.calls[0].params["content"].count("```") == 2


def test_saw_fence_on_the_golden_pair() -> None:
    """001 is the fenced reply, 002 the same reply unfenced - the fixtures the
    per-block copy button legitimately produces."""
    fenced = (GOLDEN_DIR / "001-two-calls-fenced.input.txt").read_text(encoding="utf-8")
    unfenced = (GOLDEN_DIR / "002-two-calls-crlf-nofence.input.txt").read_text(encoding="utf-8")
    assert parse_reply(fenced).saw_fence is True
    assert parse_reply(unfenced).saw_fence is False


# The real wreckage from the incident: a chat client read `<SUMMARY_TAG` as an
# HTML start tag, absorbed the rest of the reply as attributes, sorted them
# (note the ASCII order of the words), quoted them - eating one `=` per `===`
# run - and re-emitted one element.
MANGLED_OPENER = (
    'summary <<SUMMARY_TAG 01 16 All CTest. Completed SUMMARY_TAG="==CLIP:END===" '
    'SlayerGit Slice Spec Standards against and backend calls="1" chat="gentle-mesa===" '
    'code for full integration integration. passed review tests unit via ~~~~="==CLIP:EOM">'
)


def test_client_mangled_opener_is_a_fatal_per_call_issue() -> None:
    reply = parse_reply("===CLIP:CALL id=1 tool=task_done===\n" + MANGLED_OPENER + "\n")
    call = reply.calls[0]
    assert "client_mangled_heredoc" in [i.kind for i in call.issues]
    assert any(w.kind == "client_mangled_heredoc" for w in reply.warnings)
    # Deliberately nothing recovered: the words are in ASCII sort order, so a
    # "summary" built from them would be a bag of tokens.
    assert "summary" not in call.params


def test_opener_shaped_junk_without_a_sentinel_is_not_flagged() -> None:
    # Both halves are required, which is what keeps false positives at ~nil.
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=read_file===\n"
        "path: a.py\n"
        "note <<EOT and some trailing words\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
    )
    assert reply.calls[0].issues == ()
    assert reply.calls[0].params == {"path": "a.py"}


def test_sentinel_text_on_a_non_opener_line_is_not_flagged() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=write_file===\n"
        "path: doc.md\n"
        "content << EOT\n"
        'quoting the protocol: SUMMARY_TAG="==CLIP:END===" is what mangling looks like\n'
        "EOT\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1===\n"
    )
    assert reply.calls[0].issues == ()
    assert "CLIP:END" in reply.calls[0].params["content"]


# --- tolerance #14: a reply whose line breaks were lost in transport ----------

# The real wreckage from the second incident: the model's blocks were rendered
# outside a ~~~~ fence, so markdown ate the newlines and the copy handed over
# one line - the previous message's EOM, the closing fence, and the WHOLE next
# message riding behind it.
FLATTENED_EOM = (
    "===CLIP:EOM calls=1 chat=swift-forge===~~~~ ===CLIP:CALL id=1 "
    "tool=run_command=== command: echo hello world ===CLIP:END===\n"
)


def test_flattened_eom_keeps_its_chat_name_and_is_flagged() -> None:
    reply = parse_reply(FLATTENED_EOM)
    # The `===` terminator and everything behind it must not end up in the chat
    # name: the engine gates on it, and blaming the chat name for a transport
    # fault sends the user hunting the wrong thing.
    assert reply.eom.chat == "swift-forge"
    assert reply.eom.calls == 1
    warning = next(w for w in reply.warnings if w.kind == "flattened_reply")
    assert warning.line == 1
    assert "line breaks" in warning.detail
    # Nothing is reassembled from the glued text - not even the CALL header that
    # is plainly visible in it.
    assert reply.calls == ()


def test_a_flattened_call_header_is_flagged_too() -> None:
    reply = parse_reply(
        "===CLIP:CALL id=1 tool=run_command=== command: echo hello ===CLIP:END===\n"
        "===CLIP:EOM calls=1 chat=amber-falcon===\n"
    )
    assert any(w.kind == "flattened_reply" for w in reply.warnings)
    assert reply.eom.chat == "amber-falcon"
    assert reply.calls[0].tool == "run_command"
    assert "command" not in reply.calls[0].params  # never guessed back out


def test_trailing_words_without_a_sentinel_are_not_flattening() -> None:
    # Both halves are required here as in #13: trailing text AND `CLIP:` in it.
    reply = parse_reply(_eom_reply("===CLIP:EOM calls=1 chat=amber-falcon=== thanks!"))
    assert reply.warnings == ()
    assert reply.eom.chat == "amber-falcon"


def test_peek_chat_name_ignores_a_glued_terminator() -> None:
    assert peek_chat_name(FLATTENED_EOM) == "swift-forge"


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
