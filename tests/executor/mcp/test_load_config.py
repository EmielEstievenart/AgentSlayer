"""load_config's MCP wiring (docs/design/mcp.md section 1): the ``[mcp]`` table,
which files the loader is pointed at, and which MACHINE it reads them from -
both layers off the target in a remote session, along with the files and
variables they name (docs/design/remote-ssh.md, "the target owns its policy").
The one file it opens in no session at all is any of them when
``enabled = false``.

Pure loader behaviour (entry parsing, merge semantics, placeholder substitution)
lives in tests/executor/mcp/test_config.py; here every test goes through load_config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agentclip.config
from agentclip.config import load_config, project_permissions_path
from agentclip.executor.hosts import FakeHost
from agentclip.executor.mcp.types import McpLocalServer, McpRemoteServer

# The remote user's home, and where AgentClip keeps its ruleset under it -
# which in a remote session is composed from `home`, never from this PC's ~.
REMOTE_HOME = Path("/home/dev")
REMOTE_RULES = "/home/dev/.config/agentclip/permissions.json"
REMOTE_ROOT = "/srv/app"
REMOTE_PROJECT_RULES = f"{REMOTE_ROOT}/.agentclip/permissions.json"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def global_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _write_rules(path: Path, mcp: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcp": mcp}), encoding="utf-8")


def _rules_json(mcp: dict) -> str:
    return json.dumps({"mcp": mcp})


def _remote(global_path: Path, host: FakeHost, **kwargs: object):
    """A session on ``host``, loaded the way cli.remote_launch loads one: the
    project root, the home directory and the environment are the target's."""
    return load_config(
        Path(REMOTE_ROOT),
        global_config_path=global_path,
        host=host,
        home=REMOTE_HOME,
        **kwargs,  # type: ignore[arg-type]
    )


# -- the [mcp] table and its defaults -----------------------------------------


def test_mcp_defaults_and_no_real_file_is_read(project: Path, global_path: Path, tmp_path: Path) -> None:
    """With no [mcp] table and no override, the servers come from
    default_permissions_config_path() - which the suite-wide
    _no_real_permissions_config fixture points at a nonexistent tmp file. This
    test is the proof that the fixture isolates the MCP reader exactly as it
    isolates the permission reader (docs/design/mcp.md section 7): the
    developer's real permissions.json is never opened, because load_config
    resolves the path through that one function."""
    # The attribute lookup must go through the module (the fixture patches
    # agentclip.config.default_permissions_config_path, not a from-import binding).
    default = agentclip.config.default_permissions_config_path()
    assert default.is_relative_to(tmp_path)  # the fixture's tmp path, not ~/.config
    assert not default.exists()

    config = load_config(project, global_config_path=global_path)

    assert config.mcp.enabled is True
    assert config.mcp.permissions_config == ""
    assert config.mcp_servers.servers == ()
    assert config.mcp_servers.source == ""
    assert config.warnings == ()  # absent file is silent, like the permission reader


def test_default_path_function_is_shared_not_copied(
    project: Path, global_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repointing default_permissions_config_path (the same seam the permission
    reader uses) repoints the MCP reader too - i.e. the loader reuses that one
    function rather than carrying a private copy of the default path."""
    rules = tmp_path / "shared-default.json"
    _write_rules(rules, {"echo": {"type": "local", "command": ["echo", "hi"]}})
    monkeypatch.setattr("agentclip.config.default_permissions_config_path", lambda: rules)

    config = load_config(project, global_config_path=global_path)

    assert len(config.mcp_servers.servers) == 1
    server = config.mcp_servers.servers[0]
    assert isinstance(server, McpLocalServer)
    assert server.command == ("echo", "hi")
    assert config.mcp_servers.source == str(rules)


def test_permissions_config_override_wins_over_default(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    ocjson = tmp_path / "elsewhere.json"
    _write_rules(
        ocjson,
        {"github": {"type": "local", "command": ["npx", "gh-mcp"], "timeout": 5000}},
    )
    # TOML literal string (single quotes): a Windows path's backslashes must not
    # be read as escapes.
    global_path.write_text(f"[mcp]\npermissions_config = '{ocjson}'\n", encoding="utf-8")

    config = load_config(project, global_config_path=global_path)

    assert config.mcp.permissions_config == str(ocjson)
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
        f"[mcp]\nenabled = false\npermissions_config = '{ocjson}'\n", encoding="utf-8"
    )

    config = load_config(project, global_config_path=global_path)

    assert config.mcp.enabled is False
    assert config.mcp_servers.servers == ()
    assert config.mcp_servers.source == ""
    assert not any(str(ocjson) in w for w in config.warnings)


# -- the project layer, in both kinds of session -------------------------------


def test_local_session_reads_the_projects_own_ruleset(project: Path, global_path: Path) -> None:
    local = project_permissions_path(project)
    _write_rules(local, {"fs": {"type": "local", "command": ["fs-mcp"]}})

    config = load_config(project, global_config_path=global_path)

    assert [s.name for s in config.mcp_servers.servers] == ["fs"]
    assert config.mcp_servers.source == str(local)


def test_project_layer_merges_per_field_over_global(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    """Global declares the server; the project layer retunes ONE field and the
    rest survives - OpenCode's deep merge, project over global."""
    ocjson = tmp_path / "global-permissions.json"
    _write_rules(
        ocjson,
        {"api": {"type": "remote", "url": "https://api.example/mcp", "headers": {"a": "1"}}},
    )
    _write_rules(project_permissions_path(project), {"api": {"timeout": 1234}})
    global_path.write_text(f"[mcp]\npermissions_config = '{ocjson}'\n", encoding="utf-8")

    config = load_config(project, global_config_path=global_path)

    assert [s.name for s in config.mcp_servers.servers] == ["api"]
    server = config.mcp_servers.servers[0]
    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://api.example/mcp"  # global field survives
    assert server.headers == (("a", "1"),)
    assert server.timeout_ms == 1234  # the project's one overridden field
    assert config.mcp_servers.source == f"{ocjson}, {project_permissions_path(project)}"


# -- a remote session: both layers, off the target -----------------------------


def test_both_layers_are_read_off_the_target(global_path: Path, tmp_path: Path) -> None:
    """The tripwire, inverted: the servers now come off the machine the project
    is on, project layer included.

    The entries describe that machine - the URL its author could reach, the
    token beside the file that names it - so the file over there is the one
    that answers, and a file at the same path on this PC must not be.
    """
    (tmp_path / "permissions.json").write_text(
        _rules_json({"api": {"type": "remote", "url": "https://this-pc.example"}}),
        encoding="utf-8",
    )
    host = FakeHost(REMOTE_ROOT)
    host.add_file(REMOTE_RULES, _rules_json({"api": {"type": "remote", "url": "https://box"}}))
    host.add_file(REMOTE_PROJECT_RULES, _rules_json({"api": {"headers": {"a": "1"}}}))

    config = _remote(global_path, host)

    (server,) = config.mcp_servers.servers
    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://box"  # the remote global layer
    assert server.headers == (("a", "1"),)  # ...retuned by the remote project layer
    # str(Path), not the posix spelling: McpServers.source is not shown anywhere
    # yet, so it is still the plain path join every local test asserts.
    assert config.mcp_servers.source == (
        f"{Path(REMOTE_RULES)}, {Path(REMOTE_PROJECT_RULES)}"
    )


def test_the_remote_global_layer_comes_from_the_remote_home(global_path: Path) -> None:
    """With no [mcp] permissions_config, ~ is the TARGET user's home - the same
    resolution the permission ruleset in that very file gets."""
    host = FakeHost(REMOTE_ROOT)
    host.add_file(REMOTE_RULES, _rules_json({"fs": {"type": "local", "command": ["fs"]}}))

    config = _remote(global_path, host)

    assert [s.name for s in config.mcp_servers.servers] == ["fs"]
    assert config.mcp_servers.source == str(Path(REMOTE_RULES))


def test_a_tilde_in_mcp_permissions_config_expands_on_the_target(global_path: Path) -> None:
    global_path.write_text('[mcp]\npermissions_config = "~/servers.json"\n', encoding="utf-8")
    host = FakeHost(REMOTE_ROOT)
    host.add_file(
        "/home/dev/servers.json", _rules_json({"fs": {"type": "local", "command": ["fs"]}})
    )

    config = _remote(global_path, host)

    assert [s.name for s in config.mcp_servers.servers] == ["fs"]
    assert config.mcp_servers.source == str(Path("/home/dev/servers.json"))


def test_a_stdio_entry_on_the_target_is_read_not_dropped(global_path: Path) -> None:
    """Reading it is what lets McpManager REFUSE it by name (mcp/client.py):
    dropping it here is how a server the user configured disappears without a
    word, which is the failure this replaced."""
    host = FakeHost(REMOTE_ROOT)
    host.add_file(
        REMOTE_PROJECT_RULES,
        _rules_json({"fs": {"type": "local", "command": ["fs-mcp"]}}),
    )

    config = _remote(global_path, host)

    (server,) = config.mcp_servers.servers
    assert isinstance(server, McpLocalServer)
    assert server.command == ("fs-mcp",)


def test_a_file_placeholder_is_read_through_the_host(global_path: Path, tmp_path: Path) -> None:
    """`{file:token}` is a path on the TARGET, anchored to the directory of the
    config that named it - the token sits next to the file that names it, and
    that file is over there."""
    (tmp_path / "token").write_text("this-pcs-secret", encoding="utf-8")
    host = FakeHost(REMOTE_ROOT)
    host.add_file(REMOTE_RULES, _rules_json({
        "api": {
            "type": "remote",
            "url": "https://box",
            "headers": {"Authorization": "Bearer {file:token}"},
        }
    }))
    host.add_file("/home/dev/.config/agentclip/token", "the-boxs-secret\n")

    config = _remote(global_path, host)

    (server,) = config.mcp_servers.servers
    assert isinstance(server, McpRemoteServer)
    assert server.headers == (("Authorization", "Bearer the-boxs-secret"),)


def test_a_file_placeholder_with_a_tilde_expands_on_the_target(global_path: Path) -> None:
    host = FakeHost(REMOTE_ROOT)
    host.add_file(REMOTE_RULES, _rules_json({
        "api": {"type": "remote", "url": "https://box", "headers": {"k": "{file:~/token}"}}
    }))
    host.add_file("/home/dev/token", "from-the-remote-home")

    config = _remote(global_path, host)

    (server,) = config.mcp_servers.servers
    assert isinstance(server, McpRemoteServer)
    assert server.headers == (("k", "from-the-remote-home"),)


def test_env_placeholders_come_from_the_supplied_environment(
    global_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is the target's, passed in by cli.remote_launch. This
    PC's variable of the same name is a different machine's secret."""
    monkeypatch.setenv("AGENTCLIP_TEST_TOKEN", "this-pcs-secret")
    host = FakeHost(REMOTE_ROOT)
    host.add_file(REMOTE_RULES, _rules_json({
        "api": {
            "type": "remote",
            "url": "https://box",
            "headers": {"Authorization": "Bearer {env:AGENTCLIP_TEST_TOKEN}"},
        }
    }))

    config = _remote(global_path, host, environ={"AGENTCLIP_TEST_TOKEN": "the-boxs-secret"})

    (server,) = config.mcp_servers.servers
    assert isinstance(server, McpRemoteServer)
    assert server.headers == (("Authorization", "Bearer the-boxs-secret"),)


def test_without_a_remote_environment_env_placeholders_are_empty(
    global_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable printenv (cli warns and passes nothing) substitutes empty -
    exactly what an unset variable does - and never this PC's value."""
    monkeypatch.setenv("AGENTCLIP_TEST_TOKEN", "this-pcs-secret")
    host = FakeHost(REMOTE_ROOT)
    host.add_file(REMOTE_RULES, _rules_json({
        "api": {"type": "remote", "url": "https://box/{env:AGENTCLIP_TEST_TOKEN}"}
    }))

    config = _remote(global_path, host)

    (server,) = config.mcp_servers.servers
    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://box/"


def test_a_target_with_no_ruleset_is_silent(global_path: Path) -> None:
    config = _remote(global_path, FakeHost(REMOTE_ROOT))
    assert config.mcp_servers.servers == ()
    assert config.mcp_servers.source == ""
    assert config.warnings == ()
