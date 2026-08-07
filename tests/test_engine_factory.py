"""cli.make_engine_factory: what an EngineRequest turns into."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentclip.app.types import EngineRequest
from agentclip.cli import make_engine_factory
from agentclip.config import Config, load_config


@pytest.fixture
def build(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()

    def get_config() -> Config:
        return load_config(root, global_config_path=root / "no-such-global.toml")

    return make_engine_factory(get_config, root)


def test_a_plain_service_key_still_works(build) -> None:
    """Backwards compatible: existing call sites pass the bare service key."""
    engine = build("claude")
    assert engine.role == "master"
    assert engine.chat_name
    assert "===CLIP:CALL id=1 tool=delegate===" not in engine.start_task("t").chunks[0]


def test_master_request_without_delegation_has_no_delegate_tool(build) -> None:
    engine = build(EngineRequest(service="claude"))
    assert engine.role == "master"
    payload = engine.start_task("t").chunks[0]
    assert "===CLIP:CALL id=1 tool=delegate===" not in payload
    assert "You are a sub-agent" not in payload


def test_master_request_with_delegation_advertises_the_tool(build) -> None:
    engine = build(EngineRequest(service="claude", allow_delegate=True))
    payload = engine.start_task("t").chunks[0]
    assert "===CLIP:CALL id=1 tool=delegate===" in payload
    assert "delegate(task*, context)" in payload


def test_subagent_request_omits_delegate_and_swaps_the_brief(build) -> None:
    engine = build(
        EngineRequest(service="claude", role="subagent", allow_delegate=True)
    )
    assert engine.role == "subagent"
    payload = engine.start_task("the delegated task").chunks[0]
    assert "===CLIP:CALL id=1 tool=delegate===" not in payload
    assert "You are a sub-agent." in payload
    assert "task_done(summary, result*)" in payload


def test_each_engine_draws_its_own_chat_name(build) -> None:
    names = {build(EngineRequest(service="claude")).chat_name for _ in range(6)}
    assert len(names) > 1  # 2,916 combinations: 6 identical draws is impossible


def test_a_pinned_chat_name_wins_over_the_generator(build) -> None:
    engine = build(EngineRequest(service="claude", chat_name="teal-otter"))
    assert engine.chat_name == "teal-otter"
    assert "chat=teal-otter" in engine.start_task("t").chunks[0]


def test_the_session_log_records_role_and_parent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    build = make_engine_factory(
        lambda: load_config(root, global_config_path=root / "nope.toml"), root
    )
    engine = build(
        EngineRequest(
            service="claude",
            role="subagent",
            chat_name="teal-otter",
            parent_chat_name="amber-falcon",
        )
    )
    engine.start_task("t")
    transcripts = sorted((root / ".agentclip" / "sessions").glob("*/transcript.jsonl"))
    events = [
        json.loads(line)
        for line in transcripts[0].read_text(encoding="utf-8").splitlines()
    ]
    session_event = next(e for e in events if e["t"] == "session")
    assert session_event["role"] == "subagent"
    assert session_event["chat_name"] == "teal-otter"
    assert session_event["parent_chat_name"] == "amber-falcon"
