"""load_config's MCP wiring (docs/design/mcp.md section 1): the ``[mcp]`` table,
which files the loader is pointed at, and - the part with teeth - which files it
is NOT pointed at: nothing at all when ``enabled = false``, and never the
project's opencode.json in a remote session, because a ``local`` MCP server is a
command THIS PC will run and the remote machine must not get to pick it.

Pure loader behaviour (entry parsing, merge semantics, placeholder substitution)
lives in tests/mcp/test_config.py; here every test goes through load_config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentclip.config
from agentclip.config import load_config
from agentclip.mcp.types import McpLocalServer, McpRemoteServer


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def global_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _write_opencode(path: Path, mcp: dict) -> None:
    path.write_text(json.dumps({"mcp": mcp}), encoding="utf-8")


class _RemoteHost:
    """The least Host a remote session needs here: load_config only ever calls
    ``read_bytes`` (for the project's .agentclip.toml), and a remote machine
    without one answers FileNotFoundError. Everything else may not be touched -
    the whole point under test is that the MCP reader never goes through it."""

    name = "fake-remote"
    case_sensitive = True

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        raise FileNotFoundError(path)


# -- the [mcp] table and its defaults -----------------------------------------


def test_mcp_defaults_and_no_real_file_is_read(project: Path, global_path: Path, tmp_path: Path) -> None:
    """With no [mcp] table and no override, the servers come from
    default_opencode_config_path() - which the suite-wide _no_real_opencode_config
    fixture points at a nonexistent tmp file. This test is the proof that the
    fixture isolates the MCP reader exactly as it isolates the permission reader
    (docs/design/mcp.md section 7): the developer's real opencode.json is never
    opened, because load_config resolves the path through that one function."""
    # The attribute lookup must go through the module (the fixture patches
    # agentclip.config.default_opencode_config_path, not a from-import binding).
    default = agentclip.config.default_opencode_config_path()
    assert default.is_relative_to(tmp_path)  # the fixture's tmp path, not ~/.config
    assert not default.exists()

    config = load_config(project, global_config_path=global_path)

    assert config.mcp.enabled is True
    assert config.mcp.opencode_config == ""
    assert config.mcp_servers.servers == ()
    assert config.mcp_servers.source == ""
    assert config.warnings == ()  # absent file is silent, like the permission reader


def test_default_path_function_is_shared_not_copied(
    project: Path, global_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repointing default_opencode_config_path (the same seam the permission
    reader uses) repoints the MCP reader too - i.e. the loader reuses that one
    function rather than carrying a private copy of the default path."""
    ocjson = tmp_path / "shared-default.json"
    _write_opencode(ocjson, {"echo": {"type": "local", "command": ["echo", "hi"]}})
    monkeypatch.setattr("agentclip.config.default_opencode_config_path", lambda: ocjson)

    config = load_config(project, global_config_path=global_path)

    assert len(config.mcp_servers.servers) == 1
    server = config.mcp_servers.servers[0]
    assert isinstance(server, McpLocalServer)
    assert server.command == ("echo", "hi")
    assert config.mcp_servers.source == str(ocjson)


def test_opencode_config_override_wins_over_default(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    ocjson = tmp_path / "elsewhere.json"
    _write_opencode(
        ocjson,
        {"github": {"type": "local", "command": ["npx", "gh-mcp"], "timeout": 5000}},
    )
    # TOML literal string (single quotes): a Windows path's backslashes must not
    # be read as escapes.
    global_path.write_text(f"[mcp]\nopencode_config = '{ocjson}'\n", encoding="utf-8")

    config = load_config(project, global_config_path=global_path)

    assert config.mcp.opencode_config == str(ocjson)
    assert [s.name for s in config.mcp_servers.servers] == ["github"]
    server = config.mcp_servers.servers[0]
    assert isinstance(server, McpLocalServer)
    assert server.timeout_ms == 5000  # milliseconds preserved, not converted
    assert config.mcp_servers.source == str(ocjson)


def test_disabled_means_no_file_is_opened(project: Path, global_path: Path, tmp_path: Path) -> None:
    """[mcp] enabled=false must not even read the file: the override points at
    JSON that would WARN if parsed, and the warning's absence is the proof."""
    ocjson = tmp_path / "broken.json"
    ocjson.write_text("{this is not json", encoding="utf-8")
    global_path.write_text(
        f"[mcp]\nenabled = false\nopencode_config = '{ocjson}'\n", encoding="utf-8"
    )

    config = load_config(project, global_config_path=global_path)

    assert config.mcp.enabled is False
    assert config.mcp_servers.servers == ()
    assert config.mcp_servers.source == ""
    assert not any(str(ocjson) in w for w in config.warnings)


# -- the project layer: local sessions only -----------------------------------


def test_local_session_reads_project_opencode_json(project: Path, global_path: Path) -> None:
    _write_opencode(project / "opencode.json", {"fs": {"type": "local", "command": ["fs-mcp"]}})

    config = load_config(project, global_config_path=global_path)

    assert [s.name for s in config.mcp_servers.servers] == ["fs"]
    assert config.mcp_servers.source == str(project / "opencode.json")


def test_remote_session_skips_project_opencode_json(project: Path, global_path: Path) -> None:
    """The same project file that feeds a local session contributes NOTHING when
    the config is loaded for a remote host (docs/design/mcp.md section 1): in a
    remote session project_root belongs to the other machine, and its
    opencode.json could name any command for this PC to spawn."""
    _write_opencode(project / "opencode.json", {"evil": {"type": "local", "command": ["evil"]}})

    local = load_config(project, global_config_path=global_path)
    remote = load_config(project, global_config_path=global_path, host=_RemoteHost())

    assert [s.name for s in local.mcp_servers.servers] == ["evil"]  # the contrast
    assert remote.mcp_servers.servers == ()
    assert remote.mcp_servers.source == ""


def test_project_layer_merges_per_field_over_global(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    """Global declares the server; the project layer retunes ONE field and the
    rest survives - OpenCode's deep merge, project over global."""
    ocjson = tmp_path / "global-opencode.json"
    _write_opencode(
        ocjson,
        {"api": {"type": "remote", "url": "https://api.example/mcp", "headers": {"a": "1"}}},
    )
    _write_opencode(project / "opencode.json", {"api": {"timeout": 1234}})
    global_path.write_text(f"[mcp]\nopencode_config = '{ocjson}'\n", encoding="utf-8")

    config = load_config(project, global_config_path=global_path)

    assert [s.name for s in config.mcp_servers.servers] == ["api"]
    server = config.mcp_servers.servers[0]
    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://api.example/mcp"  # global field survives
    assert server.headers == (("a", "1"),)
    assert server.timeout_ms == 1234  # the project's one overridden field
    assert config.mcp_servers.source == f"{ocjson}, {project / 'opencode.json'}"
