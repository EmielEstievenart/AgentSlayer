"""Byte-for-byte fidelity of tool-call payloads whose CODE looks like MARKDOWN.

A C++ lambda introducer is textually identical to a markdown inline link -
`[this](int a)` is `[label](target)` - and a host chat that renders reply text
as prose will rewrite exactly that shape on copy (the same transport family as
tolerance #14's flattening). These tests pin down that AgentClip's OWN pipeline
never does: whatever arrives between a heredoc's opener and terminator lines
must come out of the parser identical, byte for byte, however link-shaped it
looks. If one of these ever fails, a transform crept into the ingest path -
fix the transform, never the fixtures.

The inverse case - the HOST eats the brackets before AgentClip sees the text -
is deliberately not a test here: the parser cannot detect it (the corrupted
payload is well-formed CLIP) and must not guess at repairs. That blindness is
documented in docs/design/protocol.md's transport notes.
"""

from __future__ import annotations

from agentclip.protocol.parser import parse_reply

# Every bracket-then-parenthesis shape from the field report that prompted this
# file: C++ capture lists (empty, single, multi), subscript-then-call in two
# languages, and one REAL markdown link - which must survive untouched too,
# because a payload is bytes, not prose.
LINK_SHAPED_LINES = (
    "set_receive_handler([this](uint8_t const* d, std::size_t n, Connection*) {",
    "async_wait([this](const boost::system::error_code& e) { tick(e); });",
    "register_rx_callback([this, bus_nr](autom::can_message_s_t& m) {",
    "[this, x](const T& y) { return y; }",
    "auto noop = [](){};",
    "handlers[0](1);",
    'd["k"](x)',
    "[docs](https://example.com)",
)


def _reply(find: str, replace: str) -> str:
    return (
        "~~~~\n"
        "===CLIP:CALL id=1 tool=edit_file===\n"
        "path: can_router/route_app/main.cpp\n"
        "find <<F1\n"
        f"{find}\n"
        "F1\n"
        "replace <<R1\n"
        f"{replace}\n"
        "R1\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 chat=ivory-vale===\n"
        "~~~~\n"
    )


def test_link_shaped_payloads_round_trip_byte_for_byte() -> None:
    find = LINK_SHAPED_LINES[0]
    replace = "\n".join(LINK_SHAPED_LINES)
    reply = parse_reply(_reply(find, replace))

    assert list(reply.warnings) == []
    (call,) = reply.calls
    assert list(call.issues) == []
    assert call.tool == "edit_file"
    assert call.params["find"] == find
    assert call.params["replace"] == replace


def test_a_paren_inside_the_parameter_list_survives_too() -> None:
    """The report's nesting case: `[^)]`-style link strippers corrupt a lambda
    whose parameter list itself contains `)` even more strangely. AgentClip
    must not care - a heredoc body ends at its terminator line and nowhere
    else."""
    gnarly = "queue.post([h = std::move(h)](auto (*fn)(int), int v) { fn(v); h(); });"
    reply = parse_reply(_reply(gnarly, gnarly + "\n" + gnarly))

    assert list(reply.warnings) == []
    (call,) = reply.calls
    assert call.params["find"] == gnarly
    assert call.params["replace"] == gnarly + "\n" + gnarly
