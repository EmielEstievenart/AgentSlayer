"""cli.make_engine_factory: what an EngineRequest turns into.

The factory hands back a ``LocalLink`` (docs/design/remote-executor.md section
2.2), but what these tests are ABOUT is the engine inside it - the registry it
was sized with, the bootstrap it composes, the session log it opens - so every
factory here is unwrapped once, at the seam, by :func:`_engines`. The link
itself is tested in tests/shell/app/test_link.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import agentclip.engine.link.factory as factory_mod
from agentclip.cli import LinkFactory, make_engine_factory
from agentclip.config import Config, load_config, project_permissions_path
from agentclip.engine.engine import Engine
from agentclip.engine.link.factory import (
    _MCP_SECTION6_SCAFFOLD,
    _MCP_TASK_FALLBACK,
    EngineRequest,
)
from agentclip.engine.states import Decision
from agentclip.executor.mcp.client import McpManager
from agentclip.executor.tools.registry import default_registry
from agentclip.protocol.spec import render_spec
from agentclip.shell.app.link import Link, LocalLink


def _engines(
    factory: Callable[[EngineRequest | str], Link],
) -> Callable[[EngineRequest | str], Engine]:
    """The factory's engines, straight: these tests assert on what was BUILT."""

    def build(request: EngineRequest | str) -> Engine:
        link = factory(request)
        assert isinstance(link, LocalLink)
        return link.engine

    return build


@pytest.fixture
def build(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()

    def get_config() -> Config:
        return load_config(root, global_config_path=root / "no-such-global.toml")

    return _engines(make_engine_factory(get_config, root))


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
# The builder OWNS the MCP runtime now (docs/design/remote-executor.md section
# 2.7), so these configure servers the way a user does - an `mcp` block in the
# project's permissions.json - and let it build its own manager. That manager is
# still the REAL McpManager over the SDK's in-process transport (the
# `_inproc_targets` seam, exactly like tests/executor/mcp/test_client.py): real
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
def inproc_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every manager the builder constructs, wired to an in-process server.

    The `_inproc_targets` seam (docs/design/mcp.md section 7) is now injected
    where the BUILDER reaches for the class, because the builder is what
    constructs the manager - there is no manager argument to hand a
    pre-connected one in through any more. ``ensure_started`` also settles
    before it returns, so the listing the catalog renders is deterministic (the
    production path tolerates a racing connect; a test asserting listing content
    must not).
    """

    class _InProcManager(McpManager):
        def __init__(
            self,
            servers: Any,
            project_root: Path,
            *,
            remote_target: str = "",
            rejected: Any = (),
            _inproc_targets: Any = None,
        ) -> None:
            super().__init__(
                servers,
                project_root,
                remote_target=remote_target,
                rejected=rejected,
                _inproc_targets={"demo": _demo_server()},
            )

        def ensure_started(self) -> None:
            super().ensure_started()
            assert self.wait_ready(READY_S)

    monkeypatch.setattr(factory_mod, "McpManager", _InProcManager)


@pytest.fixture
def factories() -> Iterator[Callable[[Path], LinkFactory]]:
    """Factories over a tmp project, all closed at the end of the test.

    Closing is the test's job now for the same reason it is ``cli.main``'s: the
    factory owns the MCP loop thread, so the thing to hand back is the factory.
    """
    made: list[LinkFactory] = []

    def make(root: Path) -> LinkFactory:
        factory = make_engine_factory(
            lambda: _cfg(root),
            root,
            chat_name=PINNED_CHAT,
            os_name="TestOS",
            # The tmp project doubles as HOME so the developer's real skill folders
            # cannot leak into the catalog these budgets are derived from.
            home=root,
        )
        made.append(factory)
        return factory

    yield make
    for factory in made:
        factory.close()


def _cfg(root: Path) -> Config:
    """The project's config, read from the project and nothing else.

    ``home`` is the tmp project too: the builder reads its MCP servers out of
    the config now, and a developer's real permissions.json must not be able to
    put a server into a test run.
    """
    return load_config(root, global_config_path=root / "no-such-global.toml", home=root)


def _project_with_budget(root: Path, max_paste_chars: int) -> None:
    (root / ".agentclip.toml").write_text(
        f'[general]\nservice = "tuned"\n\n[services.tuned]\n'
        f"max_paste_chars = {max_paste_chars}\n",
        encoding="utf-8",
    )


def _project_with_servers(root: Path, **servers: Any) -> None:
    """The user-facing way to configure MCP: the `mcp` block of permissions.json."""
    path = project_permissions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcp": servers}), encoding="utf-8")


DEMO_SERVER = {"type": "local", "command": ["never-spawned"]}


def _mcp_free_spec_len(root: Path) -> int:
    """What the builder measures: the MCP-free sections 1-5 for the tuned preset."""
    cfg = _cfg(root)
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


def test_no_servers_configured_is_todays_exact_registry(tmp_path: Path, factories) -> None:
    """Zero behaviour change when MCP is unconfigured: no servers means no
    manager, and the registry (and whole bootstrap) is byte-for-byte pre-MCP."""
    root = tmp_path / "project"
    root.mkdir()
    factory = factories(root)
    assert factory.statuses() == ()
    engine = _engines(factory)("claude")
    assert engine._registry.names() == default_registry().names()
    assert engine.build_warnings == ()
    payload = engine.start_task("t").chunks[0]
    assert "mcp" not in payload


def test_the_builder_makes_its_own_manager_from_the_config(
    tmp_path: Path, inproc_mcp, factories
) -> None:
    """Ownership, as built: nobody hands the factory a manager any more - it
    reads permissions.json itself and stands one up (remote-executor.md 2.7).
    Two servers, one disabled, so the statuses prove it read the real block."""
    root = tmp_path / "project"
    root.mkdir()
    _project_with_servers(
        root, demo=DEMO_SERVER, off={"type": "local", "command": ["nope"], "enabled": False}
    )
    factory = factories(root)
    assert {(s.name, s.state) for s in factory.statuses()} == {
        ("demo", "connected"),
        ("off", "disabled"),
    }
    # Idempotent, and the closed runtime is not silently rebuilt behind it.
    factory.close()
    factory.close()
    assert factory.statuses() == ()


def test_the_status_hook_reaches_the_manager(tmp_path: Path, inproc_mcp, factories) -> None:
    """The Shell's other MCP call: a hook registered on the factory is the one
    the manager fires, so the sidebar repaints without importing executor.mcp."""
    root = tmp_path / "project"
    root.mkdir()
    _project_with_servers(root, demo=DEMO_SERVER)
    factory = factories(root)
    seen: list[str] = []
    factory.set_status_hook(lambda status: seen.append(status.state))
    # The connect settled inside ensure_started, so the transitions have fired.
    assert [s.state for s in factory.statuses()] == ["connected"]
    assert "connected" in seen


def test_mcp_tools_advertised_when_they_fit(
    tmp_path: Path, inproc_mcp, factories
) -> None:
    root = _tuned_root(tmp_path, room=6_000)
    _project_with_servers(root, demo=DEMO_SERVER)
    engine = _engines(factories(root))("tuned")
    assert engine.build_warnings == ()
    payload = engine.start_task("t").chunks[0]
    assert "mcp_schema(tool)" in payload
    assert "mcp(tool*, args)" in payload
    assert "demo_tool_00" in payload  # the composite id, listed
    assert len(payload) <= _cfg(root).preset().max_paste_chars


def test_listing_degrades_before_the_budget_breaks(
    tmp_path: Path, inproc_mcp, factories
) -> None:
    """Mid-sized room: the listing is clipped with the +N more footer rather
    than the specs being dropped - degradation step 2 before step 3."""
    root = _tuned_root(tmp_path, room=1_500)
    _project_with_servers(root, demo=DEMO_SERVER)
    engine = _engines(factories(root))("tuned")
    assert engine.build_warnings == ()
    payload = engine.start_task("t").chunks[0]
    assert "mcp(tool*, args)" in payload
    assert "more MCP tool(s) not listed" in payload  # the full 6k listing did not ride
    budget = _cfg(root).preset().max_paste_chars
    assert len(payload) <= budget


def test_mcp_dropped_with_warning_when_room_is_hopeless(
    tmp_path: Path, inproc_mcp, factories
) -> None:
    """The binding invariant: a preset that bootstrapped before MCP existed
    must never raise BudgetExceeded because MCP appeared. With ~40 chars of
    room the specs are dropped, the warning surfaces on the engine, and the
    bootstrap still arms."""
    root = _tuned_root(tmp_path, room=40)
    _project_with_servers(root, demo=DEMO_SERVER)
    engine = _engines(factories(root))("tuned")
    assert engine.build_warnings != ()
    assert "paste budget too small for MCP tools" in engine.build_warnings[0]
    payload = engine.start_task("Fix the date parser in src/utils.py.").chunks[0]  # no raise
    assert "tool=mcp" not in payload
    budget = _cfg(root).preset().max_paste_chars
    assert len(payload) <= budget


@pytest.mark.parametrize("room", [0, 120, 400, 900, 2_500, 8_000])
def test_mcp_never_pushes_a_bootstrap_over_budget(
    tmp_path: Path, inproc_mcp, factories, room: int
) -> None:
    """Sweep the room: whatever fits rides, whatever does not is dropped with a
    warning - and the bootstrap NEVER raises BudgetExceeded because of MCP."""
    root = _tuned_root(tmp_path, room=room)
    _project_with_servers(root, demo=DEMO_SERVER)
    engine = _engines(factories(root))("tuned")
    payload = engine.start_task("Fix the date parser.").chunks[0]  # must not raise
    budget = _cfg(root).preset().max_paste_chars
    assert len(payload) <= budget
    included = "mcp(tool*, args)" in payload
    assert included == (engine.build_warnings == ())


def test_all_disabled_servers_add_no_catalog_text(tmp_path: Path, factories) -> None:
    """A manager exists (statuses can say 'disabled') but degradation step 1
    holds: no enabled server, no MCP prose in the catalog, no warning either."""
    root = _tuned_root(tmp_path, room=6_000)
    _project_with_servers(root, demo={"type": "local", "command": ["nope"], "enabled": False})
    factory = factories(root)
    engine = _engines(factory)("tuned")
    assert [s.state for s in factory.statuses()] == ["disabled"]
    assert engine.build_warnings == ()
    assert engine._registry.names() == default_registry().names()


# == entries the config loader refused =========================================
#
# The gate in `EngineBuilder._mcp` decides something the user sees: `cli._mcp_source`
# hands a Shell a status source only when `statuses()` is non-empty, so a
# builder that declines to make a manager is also a `/mcp` that answers "MCP is
# not configured". A config whose every entry is malformed used to land exactly
# there - denying the block existed, to a user looking straight at it.


def test_a_config_of_nothing_but_broken_entries_still_reports_them(
    tmp_path: Path, factories
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _project_with_servers(
        root,
        polarion={"type": "remote"},  # no url
        notes={"type": "sse", "url": "https://example.com/mcp"},  # unknown type
    )
    factory = factories(root)

    rows = factory.statuses()
    assert {r.name for r in rows} == {"polarion", "notes"}
    assert {r.state for r in rows} == {"invalid"}
    # The loader's own sentence, so the row says what the startup toast said.
    assert all(f"mcp.{r.name}" in r.detail for r in rows)


def test_refused_entries_add_no_catalog_text_either(tmp_path: Path, factories) -> None:
    """Degradation step 1 covers them for `disabled`'s reason, one step
    earlier: an entry that never typed can never produce a tool to describe, so
    the manager exists only to be looked at and the bootstrap is pre-MCP."""
    root = _tuned_root(tmp_path, room=6_000)
    _project_with_servers(root, broken={"type": "local", "command": []})
    factory = factories(root)
    engine = _engines(factory)("tuned")
    assert [s.state for s in factory.statuses()] == ["invalid"]
    assert engine.build_warnings == ()
    assert engine._registry.names() == default_registry().names()


def test_a_refused_entry_leads_the_servers_that_loaded(
    tmp_path: Path, inproc_mcp, factories
) -> None:
    """A config the runtime could not even read comes first: it is what a user
    scanning the list has to see before the servers that did load."""
    root = tmp_path / "project"
    root.mkdir()
    _project_with_servers(root, demo=DEMO_SERVER, broken={"type": "remote", "url": ""})
    factory = factories(root)

    assert [(s.name, s.state) for s in factory.statuses()] == [
        ("broken", "invalid"),
        ("demo", "connected"),
    ]


def test_the_session_log_records_role_and_parent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    build = _engines(
        make_engine_factory(lambda: load_config(root, global_config_path=root / "nope.toml"), root)
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


# -- the skill-folder carve-out, end to end ------------------------------------


def _skill_project(tmp_path: Path, *side_files: str) -> tuple[Path, Path, Path]:
    """(project root, home, skill folder) - the skill lives in HOME, which is
    the case the carve-out exists for: outside the project root entirely."""
    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "home"
    folder = home / ".claude" / "skills" / "deploy"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: ship it.\n---\nRun scripts/check.py\n", encoding="utf-8"
    )
    for rel in side_files:
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("print('CHECKED')\n", encoding="utf-8")
    return root, home, folder


def _skill_engine(root: Path, home: Path) -> tuple[LinkFactory, Engine]:
    factory = make_engine_factory(
        lambda: load_config(root, global_config_path=root / "nope.toml", home=home),
        root,
        chat_name=PINNED_CHAT,
        os_name="TestOS",
        home=home,
    )
    return factory, _engines(factory)("claude")


def _run(engine: Engine, tool: str, **params: str) -> str:
    body = "".join(f"{k}: {v}\n" for k, v in params.items())
    engine.ingest(
        f"===CLIP:CALL id=1 tool={tool}===\n{body}===CLIP:END===\n"
        f"===CLIP:EOM calls=1 chat={PINNED_CHAT}===\n"
    )
    return engine.execute().outbound.chunks[0]


def test_a_session_can_read_a_skills_side_file(tmp_path: Path) -> None:
    """The carve-out, end to end: the skill result names the folder and its
    files, and the very next call opens one of them.

    Discovery and the Workspace are built by the same builder through the same
    host, so the absolute path the model is handed is one the sandbox
    recognises - in a remote session both halves are the target's.
    """
    root, home, folder = _skill_project(tmp_path, "scripts/check.py")
    factory, engine = _skill_engine(root, home)
    try:
        engine.start_task("deploy the thing")
        loaded = _run(engine, "skill", name="deploy")
        assert f"folder: {folder}" in loaded
        assert "files: scripts/check.py" in loaded
        assert "print('CHECKED')" in _run(
            engine, "read_file", path=str(folder / "scripts" / "check.py")
        )
    finally:
        factory.close()


def test_a_session_may_not_write_into_a_skill_folder(tmp_path: Path) -> None:
    """Read-only, and the refusal says so rather than leaving the model to guess."""
    root, home, folder = _skill_project(tmp_path)
    factory, engine = _skill_engine(root, home)
    try:
        engine.start_task("t")
        engine.ingest(
            "===CLIP:CALL id=1 tool=delete_file===\n"
            f"path: {folder / 'SKILL.md'}\n"
            "===CLIP:END===\n"
            f"===CLIP:EOM calls=1 chat={PINNED_CHAT}===\n"
        )
        # The gate is not what stops this - the user approving would change
        # nothing, which is the point of testing it approved.
        (pending,) = engine.pending()
        engine.decide(pending.call.id, Decision.APPROVE)
        payload = engine.execute().outbound.chunks[0]
        assert "code=path_outside_workspace" in payload
        assert "read-only" in payload
        assert (folder / "SKILL.md").exists()
    finally:
        factory.close()


def test_a_skill_folder_dotenv_still_asks(tmp_path: Path) -> None:
    """The dotenv carve-out is basename-shaped, and the wildcard matcher's `*`
    crosses slashes - so it bites an ABSOLUTE skill-folder path unchanged, with
    no workspace-relative form to invent (executor/permissions.py, _relative).
    An ordinary side file, read a line above, is not gated at all."""
    root, home, folder = _skill_project(tmp_path, ".env")
    factory, engine = _skill_engine(root, home)
    try:
        engine.start_task("t")
        engine.ingest(
            "===CLIP:CALL id=1 tool=read_file===\n"
            f"path: {folder / '.env'}\n"
            "===CLIP:END===\n"
            f"===CLIP:EOM calls=1 chat={PINNED_CHAT}===\n"
        )
        (pending,) = engine.pending()  # asked, not silently allowed
        assert pending.call.tool == "read_file"
    finally:
        factory.close()
