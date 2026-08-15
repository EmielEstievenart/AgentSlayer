"""cli.make_engine_factory: what an EngineRequest turns into."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentclip.app.types import EngineRequest
from agentclip.cli import _MCP_SECTION6_SCAFFOLD, _MCP_TASK_FALLBACK, make_engine_factory
from agentclip.config import Config, load_config
from agentclip.executor.mcp.client import McpManager
from agentclip.executor.mcp.types import McpLocalServer
from agentclip.executor.tools.registry import default_registry
from agentclip.protocol.spec import render_spec


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


# == MCP catalog sizing (docs/design/mcp.md section 5: the budget rule) ========
#
# These go through the REAL McpManager over the SDK's in-process transport
# (the `_inproc_targets` seam, exactly like tests/executor/mcp/test_client.py): real
# protocol, real cached tool listing, no subprocess. The budget arithmetic is
# exercised by writing a project .agentclip.toml whose service budget is
# derived from a measured MCP-free spec, so the tests hold as the spec prose
# drifts.

READY_S = 10.0

# Pinned so the measured spec and the built engine agree on every chat-name
# substitution (generated names vary in length).
PINNED_CHAT = "amber-falcon"

# Enough tools with fat descriptions that the full listing (~6k chars) dwarfs
# every listing budget these tests grant - so the "+N more" degradation is
# guaranteed territory, never a lucky fit.
_TOOL_COUNT = 40


def _demo_server() -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("demo")
    description = "does one demonstration thing, at length, " * 3

    def make_handler(i: int) -> Any:
        def handler() -> str:
            return f"result {i}"

        return handler

    for i in range(_TOOL_COUNT):
        server.tool(name=f"tool_{i:02d}", description=f"{description}#{i}")(make_handler(i))
    return server


@pytest.fixture
def mcp_manager(tmp_path: Path) -> Iterator[McpManager]:
    manager = McpManager(
        [McpLocalServer(name="demo", command=("never-spawned",))],
        tmp_path,
        _inproc_targets={"demo": _demo_server()},
    )
    # Settled BEFORE any engine is built, so the listing the catalog renders is
    # deterministic (the production path tolerates a racing connect; a test
    # asserting listing content must not).
    manager.ensure_started()
    assert manager.wait_ready(READY_S)
    yield manager
    manager.close()


def _project_with_budget(root: Path, max_paste_chars: int) -> None:
    (root / ".agentclip.toml").write_text(
        f'[general]\nservice = "tuned"\n\n[services.tuned]\n'
        f"max_paste_chars = {max_paste_chars}\n",
        encoding="utf-8",
    )


def _mcp_free_spec_len(root: Path) -> int:
    """What build() measures: the MCP-free sections 1-5 for the tuned preset."""
    cfg = load_config(root, global_config_path=root / "no-such-global.toml")
    return len(
        render_spec(
            cfg.preset(),
            cfg.caps(),
            default_registry().render_catalog(),
            root.name,
            "TestOS",
            PINNED_CHAT,
            role="master",
        )
    )


def _mcp_factory(root: Path, manager: McpManager | None):
    return make_engine_factory(
        lambda: load_config(root, global_config_path=root / "no-such-global.toml"),
        root,
        chat_name=PINNED_CHAT,
        os_name="TestOS",
        # The tmp project doubles as HOME so the developer's real skill folders
        # cannot leak into the catalog these budgets are derived from.
        home=root,
        mcp_manager=manager,
    )


def _tuned_root(tmp_path: Path, room: int) -> Path:
    """A project whose 'tuned' preset leaves exactly ~`room` chars of measured
    room for the MCP addition (same caps bucket as the 12k placeholder, so the
    placeholder measurement transfers exactly)."""
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    _project_with_budget(root, 12_000)  # placeholder in the 8k..32k caps bucket
    spec_len = _mcp_free_spec_len(root)
    # These requests carry no task_chars, so the factory reserves the fat
    # fallback + scaffold; adding both keeps `room` meaning what it says.
    budget = spec_len + _MCP_TASK_FALLBACK + _MCP_SECTION6_SCAFFOLD + room
    assert 8_000 < budget <= 32_000  # stays in the placeholder's caps bucket
    _project_with_budget(root, budget)
    assert _mcp_free_spec_len(root) == spec_len  # the transfer actually held
    return root


def test_no_manager_is_todays_exact_registry(tmp_path: Path) -> None:
    """Zero behaviour change when MCP is unconfigured: no manager means the
    registry (and therefore the whole bootstrap) is byte-for-byte pre-MCP."""
    root = tmp_path / "project"
    root.mkdir()
    engine = _mcp_factory(root, None)("claude")
    assert engine._registry.names() == default_registry().names()
    assert engine.build_warnings == ()
    payload = engine.start_task("t").chunks[0]
    assert "mcp" not in payload


def test_mcp_tools_advertised_when_they_fit(tmp_path: Path, mcp_manager: McpManager) -> None:
    root = _tuned_root(tmp_path, room=6_000)
    engine = _mcp_factory(root, mcp_manager)("tuned")
    assert engine.build_warnings == ()
    payload = engine.start_task("t").chunks[0]
    assert "mcp_schema(tool)" in payload
    assert "mcp(tool*, args)" in payload
    assert "demo_tool_00" in payload  # the composite id, listed
    assert len(payload) <= load_config(
        root, global_config_path=root / "no-such-global.toml"
    ).preset().max_paste_chars


def test_listing_degrades_before_the_budget_breaks(
    tmp_path: Path, mcp_manager: McpManager
) -> None:
    """Mid-sized room: the listing is clipped with the +N more footer rather
    than the specs being dropped - degradation step 2 before step 3."""
    root = _tuned_root(tmp_path, room=1_500)
    engine = _mcp_factory(root, mcp_manager)("tuned")
    assert engine.build_warnings == ()
    payload = engine.start_task("t").chunks[0]
    assert "mcp(tool*, args)" in payload
    assert "more MCP tool(s) not listed" in payload  # the full 6k listing did not ride
    budget = load_config(
        root, global_config_path=root / "no-such-global.toml"
    ).preset().max_paste_chars
    assert len(payload) <= budget


def test_mcp_dropped_with_warning_when_room_is_hopeless(
    tmp_path: Path, mcp_manager: McpManager
) -> None:
    """The binding invariant: a preset that bootstrapped before MCP existed
    must never raise BudgetExceeded because MCP appeared. With ~40 chars of
    room the specs are dropped, the warning surfaces on the engine, and the
    bootstrap still arms."""
    root = _tuned_root(tmp_path, room=40)
    engine = _mcp_factory(root, mcp_manager)("tuned")
    assert engine.build_warnings != ()
    assert "paste budget too small for MCP tools" in engine.build_warnings[0]
    payload = engine.start_task("Fix the date parser in src/utils.py.").chunks[0]  # no raise
    assert "tool=mcp" not in payload
    budget = load_config(
        root, global_config_path=root / "no-such-global.toml"
    ).preset().max_paste_chars
    assert len(payload) <= budget


@pytest.mark.parametrize("room", [0, 120, 400, 900, 2_500, 8_000])
def test_mcp_never_pushes_a_bootstrap_over_budget(
    tmp_path: Path, mcp_manager: McpManager, room: int
) -> None:
    """Sweep the room: whatever fits rides, whatever does not is dropped with a
    warning - and the bootstrap NEVER raises BudgetExceeded because of MCP."""
    root = _tuned_root(tmp_path, room=room)
    engine = _mcp_factory(root, mcp_manager)("tuned")
    payload = engine.start_task("Fix the date parser.").chunks[0]  # must not raise
    budget = load_config(
        root, global_config_path=root / "no-such-global.toml"
    ).preset().max_paste_chars
    assert len(payload) <= budget
    included = "mcp(tool*, args)" in payload
    assert included == (engine.build_warnings == ())


def test_all_disabled_servers_add_no_catalog_text(tmp_path: Path) -> None:
    """A manager exists (statuses can say 'disabled') but degradation step 1
    holds: no enabled server, no MCP prose in the catalog, no warning either."""
    root = _tuned_root(tmp_path, room=6_000)
    manager = McpManager(
        [McpLocalServer(name="demo", command=("never-spawned",), enabled=False)], root
    )
    try:
        engine = _mcp_factory(root, manager)("tuned")
        assert engine.build_warnings == ()
        assert engine._registry.names() == default_registry().names()
    finally:
        manager.close()


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
