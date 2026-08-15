"""Role variants of the bootstrap: the master brief vs the sub-agent brief."""

from __future__ import annotations

from agentclip.config import ServicePreset, caps_for_budget
from agentclip.protocol import spec
from agentclip.protocol.composer import Composer

CATALOG = "read_file(path, start, end)\n  Read a file; 1-based inclusive range.\n"
CHAT_NAME = "amber-falcon"


def render(role: str = "master") -> str:
    preset = ServicePreset("test", "Test preset", 12_000, 240_000)
    return spec.render_spec(
        preset,
        caps_for_budget(12_000),
        CATALOG,
        "AgentClip",
        "Windows 11",
        CHAT_NAME,
        role=role,  # type: ignore[arg-type]
    )


def test_default_role_is_master_and_its_text_is_untouched() -> None:
    assert render() == render("master")
    master = " ".join(render().split())
    assert "You are a sub-agent" not in master
    assert "SECTION 1 - ROLE You are a coding agent" in master
    assert "When the task is complete and verified, send task_done. Until then" in master
    assert "After task_done the session is over; do not emit further calls." in master


def test_subagent_keeps_the_anti_refusal_framing_verbatim() -> None:
    # Beats 1-3 of section 1 are what stop a model stalling on turn 1; a
    # sub-agent needs them exactly as much as a master does.
    sub = " ".join(render("subagent").split())
    assert "The user pasted this message in themselves" in sub
    assert "Every action is reviewed by a human before it runs" in sub
    assert "Your judgment still applies as it normally would" in sub
    assert "Start work immediately. Your first reply must already contain CLIP calls" in sub
    assert "Project root: AgentClip on Windows 11." in sub


def test_subagent_is_told_what_it_is_and_what_to_hand_back() -> None:
    flat = " ".join(render("subagent").split())
    assert "You are a sub-agent." in flat
    assert "you cannot see its conversation and it cannot see yours" in flat
    assert "you have no way to ask it anything" in flat
    assert "call task_done with a `result` heredoc containing the complete deliverable" in flat
    assert "the only thing handed back to the agent that delegated to you" in flat


def test_subagent_is_told_it_cannot_delegate_further() -> None:
    flat = " ".join(render("subagent").split())
    assert "You cannot hand work to a further sub-agent of your own" in flat
    # ...and the catalog it is handed never advertises the tool (see
    # tests/executor/tools/test_registry_roles.py); the spec text only reinforces it.
    assert "tool=delegate" not in render("subagent")


def test_subagent_last_rule_demands_the_result_param() -> None:
    sub = " ".join(render("subagent").split())
    assert "send task_done with `result` carrying the full deliverable" in sub
    assert "Until then every reply must contain at least one tool call" in sub


def test_transport_and_grammar_sections_are_role_independent() -> None:
    master, sub = render(), render("subagent")
    for header, nxt in (("SECTION 2", "SECTION 3"), ("SECTION 3", "SECTION 4")):
        assert master[master.index(header) : master.index(nxt)] == (
            sub[sub.index(header) : sub.index(nxt)]
        )


def test_no_unsubstituted_placeholders_in_either_role() -> None:
    for role in ("master", "subagent"):
        text = render(role)
        assert "{" not in text and "}" not in text, role


def test_composer_role_reaches_the_bootstrap() -> None:
    preset = ServicePreset("test", "Test preset", 12_000, 240_000)
    composer = Composer(
        preset,
        caps_for_budget(12_000),
        CATALOG,
        "AgentClip",
        "TestOS",
        CHAT_NAME,
        role="subagent",
    )
    assert composer.role == "subagent"
    payload = composer.bootstrap("do the delegated thing").chunks[0]
    assert "You are a sub-agent." in payload
    assert "do the delegated thing" in payload
    assert f"===CLIP:EOM turn=1 chat={CHAT_NAME}===" in payload


def test_composer_defaults_to_the_master_brief() -> None:
    preset = ServicePreset("test", "Test preset", 12_000, 240_000)
    composer = Composer(
        preset, caps_for_budget(12_000), CATALOG, "AgentClip", "TestOS", CHAT_NAME
    )
    assert composer.role == "master"
    assert "You are a sub-agent" not in composer.bootstrap("t").chunks[0]
