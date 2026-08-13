"""Unit tests for the MCP client runtime (agentclip.mcp.client).

Everything here talks to a **real** SDK client over the in-process transport
(docs/design/mcp.md section 7): an `MCPServer` with real `@server.tool()`
functions, handed to the manager through its `_inproc_targets` seam, so the
protocol, the pydantic models and the error shapes are the SDK's own - only the
subprocess is missing. No test spawns anything.

The manager's facade is synchronous even though its guts are not, so these are
plain sync tests; the one place asyncio shows through is `wait_ready`, which
every test uses instead of sleeping.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentclip.mcp.client import McpCallError, McpManager
from agentclip.mcp.types import McpLocalServer, McpRemoteServer, McpServerStatus, tool_id

# Generous enough that a slow CI machine does not flake, short enough that a
# genuinely stuck connect fails the test instead of hanging it.
READY_S = 10.0


def entry(name: str, *, enabled: bool = True, timeout_ms: int = 5000) -> McpLocalServer:
    """A config entry whose transport is never used: every test that connects
    supplies an in-process target for this name, which wins over the command."""
    return McpLocalServer(
        name=name, command=("never-spawned",), enabled=enabled, timeout_ms=timeout_ms
    )


def demo_server(name: str = "demo") -> Any:
    """An in-process MCP server with one tool of each shape the tests need."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)

    @server.tool(description="Echo the text back.")
    def echo(text: str) -> str:
        return f"echo:{text}"

    @server.tool()
    def boom() -> str:
        raise RuntimeError("tool exploded")

    return server


def one_tool_server(name: str, tool_name: str = "t") -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)

    @server.tool(name=tool_name)
    def _handler() -> str:
        return f"from {name}"

    return server


@pytest.fixture
def managers() -> Iterator[list[McpManager]]:
    """Closes whatever a test built, even when it fails - a leaked manager owns
    a live loop thread and (in the timeout test) a sleeping server task."""
    built: list[McpManager] = []
    yield built
    for manager in built:
        manager.close()


def start(
    managers: list[McpManager],
    servers: list[McpLocalServer],
    targets: dict[str, object],
    *,
    root: Path | None = None,
) -> McpManager:
    manager = McpManager(servers, root or Path("."), _inproc_targets=targets)
    managers.append(manager)
    manager.ensure_started()
    return manager


# -- connecting and listing ----------------------------------------------------


def test_connect_lists_and_caches_tools(managers: list[McpManager], tmp_path: Path) -> None:
    manager = start(managers, [entry("demo")], {"demo": demo_server()}, root=tmp_path)
    assert manager.wait_ready(READY_S) is True

    (status,) = manager.statuses()
    assert status.state == "connected"
    assert status.detail == ""
    assert status.tool_count == 2

    tools = manager.tools()
    assert {t.id for t in tools} == {"demo_echo", "demo_boom"}

    echo = manager.schema("demo_echo")
    assert echo is not None
    assert echo.server == "demo"
    assert echo.name == "echo"
    assert echo.description == "Echo the text back."
    schema = json.loads(echo.input_schema_json)
    assert schema["type"] == "object"
    assert "text" in schema["properties"]
    # Compact separators: the listing is spent against the paste budget.
    assert ", " not in echo.input_schema_json

    assert manager.schema("demo_nope") is None


def test_statuses_are_pending_before_start(tmp_path: Path) -> None:
    manager = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    (status,) = manager.statuses()
    assert status.state == "pending"
    assert manager.tools() == ()


def test_composite_ids_are_sanitized(managers: list[McpManager], tmp_path: Path) -> None:
    manager = start(
        managers,
        [entry("git hub")],
        {"git hub": one_tool_server("git hub", "read.file")},
        root=tmp_path,
    )
    assert manager.wait_ready(READY_S) is True

    (info,) = manager.tools()
    assert info.id == tool_id("git hub", "read.file") == "git_hub_read_file"
    assert info.name == "read.file"


# -- calling -------------------------------------------------------------------


def test_call_returns_joined_text(managers: list[McpManager], tmp_path: Path) -> None:
    manager = start(managers, [entry("demo")], {"demo": demo_server()}, root=tmp_path)
    assert manager.wait_ready(READY_S) is True
    assert manager.call("demo_echo", {"text": "hi"}) == "echo:hi"


def test_call_reports_server_side_failure(managers: list[McpManager], tmp_path: Path) -> None:
    manager = start(managers, [entry("demo")], {"demo": demo_server()}, root=tmp_path)
    assert manager.wait_ready(READY_S) is True

    with pytest.raises(McpCallError) as excinfo:
        manager.call("demo_boom", {})
    assert excinfo.value.code == "mcp_error"
    assert "tool exploded" in excinfo.value.message


def test_call_unknown_tool(managers: list[McpManager], tmp_path: Path) -> None:
    manager = start(managers, [entry("demo")], {"demo": demo_server()}, root=tmp_path)
    assert manager.wait_ready(READY_S) is True

    # "demo" is connected, so a wrong id under its prefix is genuinely unknown -
    # the mcp_unavailable attribution only applies to servers that are down.
    with pytest.raises(McpCallError) as excinfo:
        manager.call("demo_missing", {})
    assert excinfo.value.code == "unknown_tool"
    assert "demo_missing" in excinfo.value.message


def test_call_before_ensure_started_is_unavailable(tmp_path: Path) -> None:
    manager = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    with pytest.raises(McpCallError) as excinfo:
        manager.call("demo_echo", {})
    assert excinfo.value.code == "mcp_unavailable"


def test_per_call_timeout_names_the_ms(managers: list[McpManager], tmp_path: Path) -> None:
    """The SDK's read timeout cannot fire on the in-process transport, so the
    manager's own wait_for is the only thing standing between a wedged tool and
    a frozen handler thread."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("slow")

    @server.tool()
    async def nap() -> str:
        import anyio

        await anyio.sleep(2)
        return "awake"

    manager = start(
        managers, [entry("slow", timeout_ms=250)], {"slow": server}, root=tmp_path
    )
    assert manager.wait_ready(READY_S) is True

    started = time.monotonic()
    with pytest.raises(McpCallError) as excinfo:
        manager.call("slow_nap", {})
    elapsed = time.monotonic() - started

    assert excinfo.value.code == "mcp_error"
    assert "250 ms" in excinfo.value.message
    assert elapsed < 2.0


# -- failures, duplicates, disabled --------------------------------------------


def test_broken_target_fails_without_taking_the_healthy_one_down(
    managers: list[McpManager], tmp_path: Path
) -> None:
    # Not a transport, not a server: Client accepts it at construction and dies
    # entering it, which is what a real spawn/handshake failure looks like here.
    manager = start(
        managers,
        [entry("broken"), entry("demo")],
        {"broken": object(), "demo": demo_server()},
        root=tmp_path,
    )
    assert manager.wait_ready(READY_S) is True

    broken, demo = manager.statuses()
    assert broken.name == "broken"
    assert broken.state == "failed"
    assert broken.detail  # one line, whatever the SDK called it
    assert broken.tool_count == 0
    assert demo.state == "connected"

    assert {t.server for t in manager.tools()} == {"demo"}

    # The failed server's tools were never listed, so no cache entry can match -
    # but the id's prefix names it, and the honest code is mcp_unavailable with
    # the status, not unknown_tool (docs/design/mcp.md section 8).
    with pytest.raises(McpCallError) as excinfo:
        manager.call("broken_anything", {})
    assert excinfo.value.code == "mcp_unavailable"
    assert "failed" in excinfo.value.message

    # An id whose prefix names no configured server at all stays unknown_tool.
    with pytest.raises(McpCallError) as excinfo:
        manager.call("nobody_home", {})
    assert excinfo.value.code == "unknown_tool"


def test_duplicate_ids_first_config_order_wins(
    managers: list[McpManager], tmp_path: Path
) -> None:
    """"a b" and "a_b" sanitize to the same prefix, so both export `a_b_t`."""
    manager = start(
        managers,
        [entry("a b"), entry("a_b")],
        {"a b": one_tool_server("a b"), "a_b": one_tool_server("a_b")},
        root=tmp_path,
    )
    assert manager.wait_ready(READY_S) is True

    ids = [t.id for t in manager.tools()]
    assert ids == ["a_b_t"]
    (winner,) = [t for t in manager.tools() if t.id == "a_b_t"]
    assert winner.server == "a b"
    assert manager.call("a_b_t", {}) == "from a b"

    first, second = manager.statuses()
    assert first.detail == ""
    assert "a_b_t" in second.detail
    assert "'a b'" in second.detail


def test_disabled_server_never_connects(managers: list[McpManager], tmp_path: Path) -> None:
    manager = start(
        managers,
        [entry("off", enabled=False), entry("demo")],
        {"off": demo_server("off"), "demo": demo_server()},
        root=tmp_path,
    )
    assert manager.wait_ready(READY_S) is True

    off, demo = manager.statuses()
    assert off.state == "disabled"
    assert off.tool_count == 0
    assert demo.state == "connected"
    assert {t.server for t in manager.tools()} == {"demo"}


# -- the one real subprocess ---------------------------------------------------

STDIO_SERVER = """
import os
from mcp.server.mcpserver import MCPServer

server = MCPServer("stdio")


@server.tool()
def whereami() -> str:
    return os.getcwd() + "|" + os.environ.get("AGENTCLIP_MCP_TEST", "unset")


server.run()
"""


def test_real_stdio_server_spawns_with_env_and_cwd(
    managers: list[McpManager], tmp_path: Path
) -> None:
    """The only test that spawns anything: everything else runs in-process.

    It is here because three things on the local path exist nowhere else - the
    argv/env/cwd translation, the Windows loop pin (a Selector loop raises
    NotImplementedError on the spawn), and the fact that Client takes the
    transport stdio_client() returns rather than the parameters object.
    """
    work = tmp_path / "work"
    work.mkdir()
    server = McpLocalServer(
        name="stdio",
        command=(sys.executable, "-c", STDIO_SERVER),
        cwd="work",  # relative: resolved against the project root
        environment=(("AGENTCLIP_MCP_TEST", "hello"),),
        timeout_ms=20000,
    )
    manager = McpManager([server], tmp_path)
    managers.append(manager)
    manager.ensure_started()
    assert manager.wait_ready(READY_S) is True

    (status,) = manager.statuses()
    assert status.state == "connected", status.detail
    assert [t.id for t in manager.tools()] == ["stdio_whereami"]

    cwd, marker = manager.call("stdio_whereami", {}).split("|")
    assert Path(cwd).resolve() == work.resolve()
    # os.environ overlaid with the entry's environment - the SDK's own default
    # is a *filtered* environment that would have dropped this.
    assert marker == "hello"


# -- close ---------------------------------------------------------------------


def test_close_is_idempotent_and_survives_never_starting(tmp_path: Path) -> None:
    never = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    never.close()
    never.close()

    manager = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    manager.ensure_started()
    assert manager.wait_ready(READY_S) is True
    manager.close()
    manager.close()

    with pytest.raises(McpCallError) as excinfo:
        manager.call("demo_echo", {"text": "hi"})
    assert excinfo.value.code == "mcp_unavailable"


def test_ensure_started_after_close_does_nothing(tmp_path: Path) -> None:
    manager = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    manager.close()
    manager.ensure_started()
    assert manager.tools() == ()
    assert manager.statuses()[0].state == "pending"


# -- a remote session ----------------------------------------------------------


def test_a_stdio_server_in_a_remote_session_is_refused_by_name(
    managers: list[McpManager], tmp_path: Path
) -> None:
    """Reported, never spawned (docs/design/remote-ssh.md, "the target owns its
    policy"): the entry's argv and cwd describe the target, and this process
    only spawns here. The in-process target is deliberately supplied - it would
    connect happily in a local session, so a connected state here would prove
    the refusal happens too late to matter.
    """
    manager = McpManager(
        [entry("fs"), McpRemoteServer(name="api", url="https://example.invalid/mcp")],
        tmp_path,
        remote_target="ssh:box",
        _inproc_targets={"fs": demo_server("fs")},
    )
    managers.append(manager)
    manager.ensure_started()

    fs, _api = manager.statuses()
    assert fs.state == "failed"
    assert "stdio servers are not supported in a remote session" in fs.detail
    assert "ssh:box" in fs.detail  # which machine that command belongs to
    assert fs.tool_count == 0
    assert manager.tools() == ()  # nothing of its was listed, so nothing is callable
    with pytest.raises(McpCallError) as excinfo:
        manager.call("fs_echo", {"text": "hi"})
    assert excinfo.value.code == "mcp_unavailable"


def test_a_local_session_still_spawns_its_stdio_servers(
    managers: list[McpManager], tmp_path: Path
) -> None:
    """The contrast: the same entry, no remote target, connects as ever."""
    manager = start(managers, [entry("fs")], {"fs": demo_server("fs")}, root=tmp_path)
    assert manager.wait_ready(READY_S) is True
    (fs,) = manager.statuses()
    assert fs.state == "connected"


def test_a_remote_sessions_http_failure_names_the_machine_that_dialed(
    managers: list[McpManager], tmp_path: Path
) -> None:
    """The URL came off the target; the socket did not (design: "MCP transport
    stays on the host"). `localhost` fails by reaching the WRONG machine, so a
    bare connection error would send the user looking on the wrong box."""
    manager = McpManager(
        [McpRemoteServer(name="api", url="http://localhost:1/mcp", timeout_ms=5000)],
        tmp_path,
        remote_target="ssh:box",
    )
    managers.append(manager)
    manager.ensure_started()
    assert manager.wait_ready(READY_S) is True

    (api,) = manager.statuses()
    assert api.state == "failed"
    assert "dialed from this PC, not from ssh:box" in api.detail


def test_a_local_sessions_http_failure_says_nothing_about_machines(
    managers: list[McpManager], tmp_path: Path
) -> None:
    manager = McpManager(
        [McpRemoteServer(name="api", url="http://localhost:1/mcp", timeout_ms=5000)], tmp_path
    )
    managers.append(manager)
    manager.ensure_started()
    assert manager.wait_ready(READY_S) is True

    (api,) = manager.statuses()
    assert api.state == "failed"
    assert "dialed from this PC" not in api.detail


# -- the SDK-less install ------------------------------------------------------


def test_missing_sdk_is_a_state_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A None entry in sys.modules is what CPython leaves behind for a module it
    # refuses to import: `import mcp` raises ImportError against it.
    monkeypatch.setitem(sys.modules, "mcp", None)
    manager = McpManager([entry("demo"), entry("off", enabled=False)], tmp_path)
    manager.ensure_started()

    demo, off = manager.statuses()
    assert demo.state == "missing_sdk"
    assert "agentclip[mcp]" in demo.detail
    assert off.state == "disabled"

    assert manager.tools() == ()
    assert manager.wait_ready(0.1) is True
    with pytest.raises(McpCallError) as excinfo:
        manager.call("demo_echo", {})
    assert excinfo.value.code == "mcp_unavailable"
    manager.close()


# -- the status hook -----------------------------------------------------------


def test_status_hook_sees_every_transition(managers: list[McpManager], tmp_path: Path) -> None:
    seen: list[McpServerStatus] = []
    manager = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    managers.append(manager)
    manager.set_status_hook(seen.append)
    manager.ensure_started()
    assert manager.wait_ready(READY_S) is True

    states = [s.state for s in seen]
    assert states[:2] == ["connecting", "connected"]
    assert seen[-1].tool_count == 2


def test_status_hook_that_raises_is_disabled(managers: list[McpManager], tmp_path: Path) -> None:
    calls: list[str] = []

    def hook(status: McpServerStatus) -> None:
        calls.append(status.state)
        raise RuntimeError("listener is gone")

    manager = McpManager([entry("demo")], tmp_path, _inproc_targets={"demo": demo_server()})
    managers.append(manager)
    manager.set_status_hook(hook)
    manager.ensure_started()
    assert manager.wait_ready(READY_S) is True

    # It raised on its first transition and was never asked again, even though
    # "connected" followed - a broken listener is not the server's problem.
    assert calls == ["connecting"]
    assert manager.statuses()[0].state == "connected"
