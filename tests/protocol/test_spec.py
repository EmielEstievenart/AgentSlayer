"""Tests for the bootstrap spec templates (agentclip.protocol.spec)."""

from __future__ import annotations

from agentclip.config import ServicePreset, caps_for_budget
from agentclip.protocol import spec

CATALOG = "read_file(path, start, end)\n  Read a file; 1-based inclusive range.\n"


def make_preset(budget: int = 12_000, *, fence: bool = True, attach: bool = True) -> ServicePreset:
    return ServicePreset(
        "test",
        "Test preset",
        budget,
        wrap_blocks_in_fence=fence,
        attachment_note=attach,
    )


def render(
    budget: int = 12_000,
    *,
    fence: bool = True,
    attach: bool = True,
    catalog: str = CATALOG,
) -> str:
    preset = make_preset(budget, fence=fence, attach=attach)
    return spec.render_spec(preset, caps_for_budget(budget), catalog, "AgentClip", "Windows 11")


def test_contains_batching_instruction_verbatim() -> None:
    text = render()
    assert spec.BATCHING_INSTRUCTION in text
    assert "Batch all independent calls into one reply" in text
    assert "each round trip costs the user a manual copy-paste" in text


def test_attachment_note_on() -> None:
    text = render(attach=True)
    assert "paste.txt" in text
    assert "read the ENTIRE attached file" in text


def test_attachment_note_off() -> None:
    text = render(attach=False)
    assert "paste.txt" not in text
    assert "attached text file" not in text


def test_fence_instruction_on() -> None:
    text = render(fence=True)
    assert "~~~~" in text
    assert "four tildes" in text
    assert "ONE fenced code block" in text


def test_fence_instruction_off() -> None:
    text = render(fence=False)
    assert "~~~~" not in text
    assert "four tildes" not in text


def test_workdir_and_os_substituted() -> None:
    text = render()
    assert "Project root: AgentClip on Windows 11." in text


def test_max_calls_substituted_per_budget_tier() -> None:
    assert "At most 3 calls per reply" in render(4_000)
    assert "At most 5 calls per reply" in render(6_000)
    assert "At most 8 calls per reply" in render(12_000)
    assert "At most 10 calls per reply" in render(96_000)


def test_no_unsubstituted_placeholders() -> None:
    # CATALOG contains no braces, so any brace is a missed .format() field.
    text = render()
    assert "{" not in text
    assert "}" not in text


def test_sections_present_in_order() -> None:
    text = render()
    headers = [
        "SECTION 1 - ROLE",
        "SECTION 2 - TRANSPORT WARNINGS",
        "SECTION 3 - HOW TO EMIT CALLS",
        "SECTION 4 - TOOL CATALOG",
        "SECTION 5 - RULES OF ENGAGEMENT",
    ]
    positions = [text.index(h) for h in headers]
    assert positions == sorted(positions)


def test_tool_catalog_embedded_between_sections_4_and_5() -> None:
    text = render()
    start = text.index("SECTION 4 - TOOL CATALOG")
    end = text.index("SECTION 5 - RULES OF ENGAGEMENT")
    assert "read_file(path, start, end)" in text[start:end]


def test_grammar_shows_call_end_eom_forms() -> None:
    text = render()
    assert "===CLIP:CALL id=1 tool=read_file===" in text
    assert "===CLIP:END===" in text
    assert "===CLIP:EOM calls=N turn=T===" in text


def test_grammar_turn_echo_instruction() -> None:
    assert "echo turn=N from my EOM line in yours" in render()


def test_heredoc_collision_rule_and_worked_example() -> None:
    text = render()
    assert "if any line of your content is exactly the tag" in text
    assert "EOT2, RAW_A" in text
    # Worked example: writing a file that itself contains a line "EOT".
    example_start = text.index("===CLIP:CALL id=2 tool=write_file===")
    example_end = text.index("===CLIP:END===", example_start)
    example = text[example_start:example_end]
    lines = example.split("\n")
    assert "content <<EOT2" in lines
    assert "EOT" in lines  # the content line that would collide with the default tag
    assert "EOT2" in lines  # the chosen non-colliding terminator


def test_transport_nack_on_missing_eom() -> None:
    text = render()
    assert "===CLIP:EOM turn=N===" in text
    assert "===CLIP:NACK reason=truncated===" in text


def test_transport_part_ack_handshake() -> None:
    text = render()
    assert "===CLIP:PART k/n===" in text
    assert "===CLIP:ACK k/n===" in text
    assert "concatenate all parts in order" in text


def test_rules_of_engagement_essentials() -> None:
    text = render()
    flat = " ".join(text.split())  # collapse line wrapping
    assert "NEVER modify files via run_command" in flat
    assert "write_file / edit_file / delete_file" in flat
    assert "Read before you edit" in flat
    assert "status=denied means the user said no: do not retry unchanged" in flat
    assert "Re-request narrower ranges" in flat
    assert "send task_done" in flat
    assert "at least one tool call" in flat
    assert "After task_done the session is over; do not emit further calls" in flat
    assert "Calls in one reply run in order" in flat


def test_uses_lf_line_endings_only() -> None:
    assert "\r" not in render()
