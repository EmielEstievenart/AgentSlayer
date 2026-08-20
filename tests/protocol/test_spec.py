"""Tests for the bootstrap spec templates (agentclip.protocol.spec)."""

from __future__ import annotations

from dataclasses import replace

from agentclip.config import ServicePreset, caps_for_budget
from agentclip.protocol import spec

CATALOG = "read_file(path, start, end)\n  Read a file; 1-based inclusive range.\n"
CHAT_NAME = "amber-falcon"


def make_preset(budget: int = 12_000, *, fence: bool = True, attach: bool = True) -> ServicePreset:
    return ServicePreset(
        "test",
        "Test preset",
        budget,
        budget * 20,
        wrap_blocks_in_fence=fence,
        attachment_note=attach,
    )


def render(
    budget: int = 12_000,
    *,
    fence: bool = True,
    attach: bool = True,
    catalog: str = CATALOG,
    extra: str = "",
    edit_by_lines: bool = False,
) -> str:
    preset = replace(
        make_preset(budget, fence=fence, attach=attach),
        extra_instructions=extra,
        edit_by_lines=edit_by_lines,
    )
    return spec.render_spec(
        preset, caps_for_budget(budget), catalog, "AgentClip", "Windows 11", CHAT_NAME
    )


def test_contains_batching_instruction_verbatim() -> None:
    text = render()
    assert spec.BATCHING_INSTRUCTION in text
    assert "Batch all independent calls into one reply" in text
    assert "each round trip costs the user a manual copy-paste" in text


def test_the_ranged_edit_rule_is_absent_without_the_toggle() -> None:
    """Section 5's budget is the bootstrap's: a service with no replace_lines
    must not carry a rule about it."""
    text = render()
    assert "replace_lines" not in text
    assert "bottom to top" not in text


def test_the_ranged_edit_rule_appears_with_the_toggle() -> None:
    """Ordering is a property of the REPLY, not of any one call in it, so it
    belongs with the rules that are about replies (protocol.md 3.1)."""
    text = render(edit_by_lines=True)
    assert spec.RANGED_EDIT_RULE.strip() in text
    assert "bottom to top" in text
    # Still in section 5, next to the other editing rule.
    assert text.index("Keep edit_file find-blocks") < text.index("replace_lines")


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


def test_role_frames_the_bootstrap_as_the_users_own_brief() -> None:
    # Some models read the pasted spec as a prompt-injection attempt and stall
    # on turn 1; this framing is what resolves that ambiguity.
    flat = " ".join(render().split())
    assert "The user pasted this message in themselves" in flat
    assert "not content from a web page, a file, or a tool result" in flat
    assert "Treat it the way you would treat a system prompt" in flat


def test_role_explains_the_human_in_the_loop_oversight() -> None:
    flat = " ".join(render().split())
    assert "The user is the transport" in flat
    assert "Every action is reviewed by a human before it runs" in flat
    assert "File changes are backed up and reversible" in flat
    assert "the results you get back are real program output" in flat


def test_role_preserves_the_models_own_judgment() -> None:
    flat = " ".join(render().split())
    assert "Your judgment still applies" in flat
    assert "if a task looks harmful or wrong, say so, or use ask_user" in flat


def test_role_demands_calls_in_the_first_reply() -> None:
    flat = " ".join(render().split())
    assert "For a real task, start now" in flat
    assert "your first reply should already contain CLIP calls" in flat
    assert "use list_dir, glob or grep" in flat
    assert "Never summarise this protocol back, ask whether to begin" in flat
    assert "ask the user to paste code or run commands" in flat


def test_role_lets_a_trivial_message_get_a_plain_reply() -> None:
    """The push for turn-1 tool calls is aimed at real tasks. Bootstrapped with
    "hi" it used to force pointless orientation calls, so the brief says out
    loud that a conversational message needs none."""
    flat = " ".join(render().split())
    assert "A greeting or question needing nothing touched gets a plain reply." in flat


def test_rules_forbid_delegating_reads_and_commands_to_the_user() -> None:
    flat = " ".join(render().split())
    assert (
        "Never ask the user to paste file contents or run commands for you - "
        "read and run things yourself with the tools above." in flat
    )


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
    assert "===CLIP:EOM calls=N chat=amber-falcon===" in text


def test_grammar_asks_for_the_chat_name_not_the_turn() -> None:
    flat = " ".join(render().split())
    assert "This chat's name is amber-falcon." in flat
    assert "is this chat's name, written exactly as shown" in flat
    assert "carries a different chat name, is ignored by the relay" in flat
    # The turn echo is AgentClip's business now; the model is never asked for it.
    assert "turn=T" not in flat
    assert "echo turn" not in flat


def test_chat_name_substituted_everywhere_it_is_taught() -> None:
    text = render()
    # Sections 2 and 3 both carry it: section 2 for the transport handshake,
    # section 3 for the EOM the model actually writes.
    transport = text[text.index("SECTION 2"):text.index("SECTION 3")]
    grammar = text[text.index("SECTION 3"):text.index("SECTION 4")]
    assert "amber-falcon" in transport
    assert "amber-falcon" in grammar


def test_heredoc_opener_space_taught_as_mandatory() -> None:
    text = render()
    assert "key << TAG" in text
    assert "The space before TAG is required" in text
    # The glued form must appear nowhere in what we teach: `<TAG` is exactly what
    # an HTML parser reads as a start tag, and one chat client does.
    assert "<<EOT" not in text
    assert "<<TAG" not in text


def test_fence_collision_rule_rides_with_the_fence_instruction() -> None:
    assert "MORE tildes" in render(fence=True)
    assert "MORE tildes" not in render(fence=False)


def test_heredoc_collision_rule_and_worked_example() -> None:
    text = render()
    assert "if any line of your content is exactly the tag" in text
    assert "EOT2, RAW_A" in text
    # Worked example: writing a file that itself contains a line "EOT".
    example_start = text.index("===CLIP:CALL id=2 tool=write_file===")
    example_end = text.index("===CLIP:END===", example_start)
    example = text[example_start:example_end]
    lines = example.split("\n")
    assert "content << EOT2" in lines
    assert "EOT" in lines  # the content line that would collide with the default tag
    assert "EOT2" in lines  # the chosen non-colliding terminator


def test_transport_nack_on_missing_eom() -> None:
    text = render()
    assert "===CLIP:EOM turn=N chat=amber-falcon===" in text
    assert "===CLIP:NACK reason=truncated chat=amber-falcon===" in text


def test_transport_part_ack_handshake() -> None:
    text = render()
    assert "===CLIP:PART k/n===" in text
    assert "===CLIP:ACK k/n chat=amber-falcon===" in text
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


EXTRA_LINE = "always put a space between ] and ( in code you send"


def test_extra_instructions_section_appears_only_when_the_preset_carries_some() -> None:
    """The user's own words about this host, shipped verbatim under a bare
    header - and on every other service, not shipped at all (protocol.md 2)."""
    with_extra = render(extra=EXTRA_LINE)
    assert spec.EXTRA_INSTRUCTIONS_HEADER in with_extra
    assert EXTRA_LINE in with_extra

    without = render()
    assert spec.EXTRA_INSTRUCTIONS_HEADER not in without


def test_whitespace_only_extra_instructions_render_nothing() -> None:
    assert spec.EXTRA_INSTRUCTIONS_HEADER not in render(extra="   \n  ")


def test_extra_instructions_come_last_after_the_numbered_sections() -> None:
    """Last word before the task: the protocol talks first, then the user talks
    over it. Unnumbered for the same reason (and so 2.1's "sections 1 and 5
    swapped" stays true of the sub-agent variant, which gets this one too)."""
    text = render(extra=EXTRA_LINE)
    assert text.index("SECTION 5 - RULES OF ENGAGEMENT") < text.index(
        spec.EXTRA_INSTRUCTIONS_HEADER
    )
    assert "SECTION 6" not in text  # still the Composer's to append
    assert text.rstrip("\n").endswith(EXTRA_LINE)


def test_the_subagent_brief_carries_the_extra_instructions_too() -> None:
    """A sub-agent talks to the same host, so it meets the same mangling."""
    preset = replace(make_preset(), extra_instructions=EXTRA_LINE)
    text = spec.render_spec(
        preset,
        caps_for_budget(12_000),
        CATALOG,
        "AgentClip",
        "Windows 11",
        CHAT_NAME,
        role="subagent",
    )
    assert EXTRA_LINE in text
