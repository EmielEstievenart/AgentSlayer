"""Unit tests for the opencode.json ``mcp`` reader (agentclip.mcp.config).

Loader-level only: files in, :class:`McpServers` out, warnings collected. The
wiring into ``load_config`` (the ``[mcp]`` table, which paths get read in a
local vs a remote session) is tested in tests/mcp/test_load_config.py - nothing
here imports agentclip.config, both to keep these tests about one thing and
because the loader itself must never depend on it (circular import).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentclip.mcp.config import load_mcp_servers
from agentclip.mcp.types import DEFAULT_TIMEOUT_MS, McpLocalServer, McpRemoteServer


def write(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- local entries -------------------------------------------------------------


def test_local_entry_reads_every_field(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem"],
                    "cwd": "/srv/project",
                    "environment": {"TOKEN": "abc", "MODE": "fast"},
                    "enabled": False,
                    "timeout": 5000,
                }
            }
        },
    )
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert warnings == []
    (server,) = servers.servers
    assert isinstance(server, McpLocalServer)
    assert server.name == "fs"
    assert server.command == ("npx", "-y", "@modelcontextprotocol/server-filesystem")
    assert server.cwd == "/srv/project"
    assert server.environment == (("TOKEN", "abc"), ("MODE", "fast"))
    assert server.enabled is False
    # Milliseconds in, milliseconds out: no unit conversion anywhere.
    assert server.timeout_ms == 5000
    assert servers.enabled_servers() == ()


def test_local_defaults_when_optional_keys_are_absent(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"mcp": {"fs": {"type": "local", "command": ["fs"]}}})
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert warnings == []
    assert isinstance(server, McpLocalServer)
    assert server.cwd == ""
    assert server.environment == ()
    assert server.enabled is True
    assert server.timeout_ms == DEFAULT_TIMEOUT_MS == 30_000


def test_unknown_keys_inside_an_entry_are_ignored_silently(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {"mcp": {"fs": {"type": "local", "command": ["fs"], "future_key": {"x": 1}}}},
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert warnings == []
    assert isinstance(server, McpLocalServer)


@pytest.mark.parametrize("timeout", [0, -1, "5000", True, 1.5])
def test_invalid_timeout_warns_and_defaults(tmp_path: Path, timeout: object) -> None:
    path = write(
        tmp_path / "opencode.json",
        {"mcp": {"fs": {"type": "local", "command": ["fs"], "timeout": timeout}}},
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert server.timeout_ms == DEFAULT_TIMEOUT_MS
    assert len(warnings) == 1
    assert "mcp.fs: timeout" in warnings[0]


def test_invalid_scalars_warn_and_fall_back_without_losing_the_server(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["fs"],
                    "cwd": 7,
                    "enabled": "yes",
                    "environment": ["TOKEN=abc"],
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert isinstance(server, McpLocalServer)
    assert server.cwd == ""
    assert server.enabled is True
    assert server.environment == ()
    assert len(warnings) == 3


def test_non_string_environment_value_drops_only_that_pair(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["fs"],
                    "environment": {"GOOD": "1", "BAD": 2, "ALSO_GOOD": "3"},
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert server.environment == (("GOOD", "1"), ("ALSO_GOOD", "3"))
    assert warnings == [f"config: {path}: mcp.fs: environment.BAD must be a string; dropped"]


# -- remote entries ------------------------------------------------------------


def test_remote_entry_reads_every_field(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "gh": {
                    "type": "remote",
                    "url": "https://mcp.example.com/v1",
                    "headers": {"Authorization": "Bearer xyz"},
                    "enabled": True,
                    "timeout": 12_000,
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert warnings == []
    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://mcp.example.com/v1"
    assert server.headers == (("Authorization", "Bearer xyz"),)
    assert server.enabled is True
    assert server.timeout_ms == 12_000


def test_oauth_absent_means_true(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {"mcp": {"gh": {"type": "remote", "url": "https://x"}}},
    )
    (server,) = load_mcp_servers([path], []).servers
    assert isinstance(server, McpRemoteServer)
    assert server.oauth is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(False, False), (True, True), ("dcr", True), (0, False), ({}, False)],
)
def test_oauth_is_read_for_truthiness(tmp_path: Path, value: object, expected: bool) -> None:
    path = write(
        tmp_path / "opencode.json",
        {"mcp": {"gh": {"type": "remote", "url": "https://x", "oauth": value}}},
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert isinstance(server, McpRemoteServer)
    assert server.oauth is expected
    assert warnings == []


# -- entries that are skipped --------------------------------------------------


def test_local_without_a_usable_command_is_skipped_with_a_warning(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "no_cmd": {"type": "local"},
                "empty": {"type": "local", "command": []},
                "not_strings": {"type": "local", "command": ["ok", 3]},
                "not_a_list": {"type": "local", "command": "npx server"},
            }
        },
    )
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert len(warnings) == 4
    assert all("command must be a non-empty list of strings" in w for w in warnings)


@pytest.mark.parametrize("url", [None, "", "   ", 5])
def test_remote_without_a_usable_url_is_skipped_with_a_warning(
    tmp_path: Path, url: object
) -> None:
    entry: dict = {"type": "remote"}
    if url is not None:
        entry["url"] = url
    path = write(tmp_path / "opencode.json", {"mcp": {"gh": entry}})
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert warnings == [f"config: {path}: mcp.gh: url must be a non-empty string; server ignored"]


def test_typeless_entry_that_is_not_a_bare_disable_warns(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"mcp": {"mystery": {"command": ["x"]}}})
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert warnings == [f'config: {path}: mcp.mystery: type must be "local" or "remote"; ignored']


def test_unknown_type_warns_and_is_skipped(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"mcp": {"weird": {"type": "sse", "url": "https://x"}}})
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert len(warnings) == 1
    assert "unknown type 'sse'" in warnings[0]


def test_entry_that_is_not_a_table_warns_and_is_skipped(tmp_path: Path) -> None:
    path = write(
        tmp_path / "opencode.json",
        {"mcp": {"bad": "npx server", "fs": {"type": "local", "command": ["fs"]}}},
    )
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert [s.name for s in servers.servers] == ["fs"]
    assert warnings == [f"config: {path}: mcp.bad must be a table; ignored"]


# -- merging across layers -----------------------------------------------------


def test_later_layer_overrides_one_field_and_the_rest_survives(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["npx", "fs"],
                    "environment": {"TOKEN": "abc"},
                    "timeout": 5000,
                }
            }
        },
    )
    project_path = write(tmp_path / "project.json", {"mcp": {"fs": {"timeout": 9000}}})
    warnings: list[str] = []

    (server,) = load_mcp_servers([global_path, project_path], warnings).servers

    assert warnings == []
    assert isinstance(server, McpLocalServer)
    assert server.command == ("npx", "fs")
    assert server.environment == (("TOKEN", "abc"),)
    assert server.timeout_ms == 9000


def test_nested_tables_merge_per_key(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["fs"],
                    "environment": {"TOKEN": "old", "KEEP": "1"},
                }
            }
        },
    )
    project_path = write(
        tmp_path / "project.json",
        {"mcp": {"fs": {"environment": {"TOKEN": "new", "EXTRA": "2"}}}},
    )

    (server,) = load_mcp_servers([global_path, project_path], []).servers

    assert isinstance(server, McpLocalServer)
    assert dict(server.environment) == {"TOKEN": "new", "KEEP": "1", "EXTRA": "2"}


def test_a_list_replaces_rather_than_merges(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {"mcp": {"fs": {"type": "local", "command": ["npx", "-y", "fs"]}}},
    )
    project_path = write(tmp_path / "project.json", {"mcp": {"fs": {"command": ["bun", "fs"]}}})

    (server,) = load_mcp_servers([global_path, project_path], []).servers

    assert isinstance(server, McpLocalServer)
    assert server.command == ("bun", "fs")


def test_first_seen_name_order_is_preserved(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {
            "mcp": {
                "b": {"type": "local", "command": ["b"]},
                "a": {"type": "local", "command": ["a"]},
            }
        },
    )
    project_path = write(
        tmp_path / "project.json",
        {
            "mcp": {
                "c": {"type": "local", "command": ["c"]},
                "b": {"timeout": 1000},
            }
        },
    )

    servers = load_mcp_servers([global_path, project_path], []).servers

    assert [s.name for s in servers] == ["b", "a", "c"]


def test_bare_disable_in_a_later_layer_switches_an_earlier_server_off(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {"mcp": {"fs": {"type": "local", "command": ["fs"]}}},
    )
    project_path = write(tmp_path / "project.json", {"mcp": {"fs": {"enabled": False}}})
    warnings: list[str] = []

    servers = load_mcp_servers([global_path, project_path], warnings)

    assert warnings == []
    (server,) = servers.servers  # still known, so the TUI can say "disabled"
    assert server.enabled is False
    assert servers.enabled_servers() == ()


def test_bare_disable_with_nothing_to_disable_is_skipped_silently(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"mcp": {"ghost": {"enabled": False}}})
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert warnings == []


def test_a_parse_warning_blames_the_file_that_last_touched_the_entry(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {"mcp": {"fs": {"type": "local", "command": ["fs"]}}},
    )
    project_path = write(tmp_path / "project.json", {"mcp": {"fs": {"timeout": "soon"}}})
    warnings: list[str] = []

    load_mcp_servers([global_path, project_path], warnings)

    assert len(warnings) == 1
    assert warnings[0].startswith(f"config: {project_path}: mcp.fs: timeout")


# -- placeholder substitution --------------------------------------------------


def test_env_placeholder_uses_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTCLIP_TEST_TOKEN", "s3cret")
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "gh": {
                    "type": "remote",
                    "url": "https://x",
                    "headers": {"Authorization": "Bearer {env:AGENTCLIP_TEST_TOKEN}"},
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert isinstance(server, McpRemoteServer)
    # Embedded, not standalone: the surrounding text survives.
    assert server.headers == (("Authorization", "Bearer s3cret"),)
    assert warnings == []


def test_unset_env_placeholder_substitutes_empty_without_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTCLIP_TEST_TOKEN", raising=False)
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "gh": {
                    "type": "remote",
                    "url": "https://x/{env:AGENTCLIP_TEST_TOKEN}",
                    "headers": {"Authorization": "{env:AGENTCLIP_TEST_TOKEN}"},
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://x/"
    assert server.headers == (("Authorization", ""),)
    assert warnings == []  # matches OpenCode: unset is empty, not an error


def test_substitution_reaches_command_elements_and_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTCLIP_TEST_HOME", "/opt/tools")
    monkeypatch.setenv("AGENTCLIP_TEST_TOKEN", "s3cret")
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "fs": {
                    "type": "local",
                    "command": ["{env:AGENTCLIP_TEST_HOME}/fs", "--root", "{env:AGENTCLIP_TEST_HOME}"],
                    "cwd": "{env:AGENTCLIP_TEST_HOME}/work",
                    "environment": {"TOKEN": "tok-{env:AGENTCLIP_TEST_TOKEN}"},
                }
            }
        },
    )

    (server,) = load_mcp_servers([path], []).servers

    assert isinstance(server, McpLocalServer)
    assert server.command == ("/opt/tools/fs", "--root", "/opt/tools")
    assert server.cwd == "/opt/tools/work"
    assert server.environment == (("TOKEN", "tok-s3cret"),)


def test_substitution_never_touches_names_or_table_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTCLIP_TEST_NAME", "renamed")
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "{env:AGENTCLIP_TEST_NAME}": {
                    "type": "local",
                    "command": ["fs"],
                    "environment": {"{env:AGENTCLIP_TEST_NAME}": "v"},
                }
            }
        },
    )

    (server,) = load_mcp_servers([path], []).servers

    assert server.name == "{env:AGENTCLIP_TEST_NAME}"
    assert isinstance(server, McpLocalServer)
    assert server.environment == (("{env:AGENTCLIP_TEST_NAME}", "v"),)


def test_file_placeholder_reads_and_strips_the_file(tmp_path: Path) -> None:
    secret = tmp_path / "token.txt"
    secret.write_text("s3cret\n", encoding="utf-8")
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "gh": {
                    "type": "remote",
                    "url": "https://x",
                    "headers": {"Authorization": "Bearer {file:" + str(secret) + "}"},
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert isinstance(server, McpRemoteServer)
    # Stripped: a trailing newline would corrupt the header it rides in.
    assert server.headers == (("Authorization", "Bearer s3cret"),)
    assert warnings == []


def test_missing_file_placeholder_substitutes_empty_with_a_warning(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "gh": {
                    "type": "remote",
                    "url": "https://x",
                    "headers": {"Authorization": "{file:" + str(missing) + "}"},
                }
            }
        },
    )
    warnings: list[str] = []

    (server,) = load_mcp_servers([path], warnings).servers

    assert isinstance(server, McpRemoteServer)
    assert server.headers == (("Authorization", ""),)
    assert len(warnings) == 1
    assert warnings[0].startswith(f"config: {path}: mcp.gh: could not read {missing}")


def test_repeated_placeholders_all_expand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCLIP_TEST_A", "1")
    monkeypatch.setenv("AGENTCLIP_TEST_B", "2")
    path = write(
        tmp_path / "opencode.json",
        {
            "mcp": {
                "gh": {
                    "type": "remote",
                    "url": "https://x/{env:AGENTCLIP_TEST_A}/{env:AGENTCLIP_TEST_B}/{env:AGENTCLIP_TEST_A}",
                }
            }
        },
    )

    (server,) = load_mcp_servers([path], []).servers

    assert isinstance(server, McpRemoteServer)
    assert server.url == "https://x/1/2/1"


# -- file-level triage ---------------------------------------------------------


def test_absent_file_is_silent(tmp_path: Path) -> None:
    warnings: list[str] = []

    servers = load_mcp_servers([tmp_path / "nope.json"], warnings)

    assert servers.servers == ()
    assert servers.source == ""
    assert warnings == []


def test_no_paths_at_all_is_silent(tmp_path: Path) -> None:
    warnings: list[str] = []

    servers = load_mcp_servers([], warnings)

    assert servers.servers == ()
    assert servers.source == ""
    assert warnings == []


def test_invalid_json_warns_once_and_loads_nothing(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    path.write_text('{"mcp": {', encoding="utf-8")
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert servers.source == ""
    assert len(warnings) == 1
    assert warnings[0].startswith(f"config: {path} is not valid JSON:")


def test_file_without_an_mcp_key_is_silent(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"permission": {"bash": "ask"}})
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert servers.source == ""
    assert warnings == []


def test_json_that_is_not_an_object_is_silent(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", ["mcp"])
    warnings: list[str] = []

    assert load_mcp_servers([path], warnings).servers == ()
    assert warnings == []


def test_mcp_that_is_not_a_table_warns(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"mcp": ["fs"]})
    warnings: list[str] = []

    servers = load_mcp_servers([path], warnings)

    assert servers.servers == ()
    assert servers.source == ""
    assert warnings == [f"config: {path}: mcp must be a table of servers; ignored"]


def test_one_bad_file_does_not_stop_the_other(tmp_path: Path) -> None:
    bad = tmp_path / "global.json"
    bad.write_text("not json", encoding="utf-8")
    good = write(tmp_path / "project.json", {"mcp": {"fs": {"type": "local", "command": ["fs"]}}})
    warnings: list[str] = []

    servers = load_mcp_servers([bad, good], warnings)

    assert [s.name for s in servers.servers] == ["fs"]
    assert servers.source == str(good)
    assert len(warnings) == 1


# -- source --------------------------------------------------------------------


def test_source_names_the_single_contributing_file(tmp_path: Path) -> None:
    path = write(tmp_path / "opencode.json", {"mcp": {"fs": {"type": "local", "command": ["fs"]}}})

    assert load_mcp_servers([path], []).source == str(path)


def test_source_joins_contributing_files_in_precedence_order(tmp_path: Path) -> None:
    global_path = write(
        tmp_path / "global.json",
        {"mcp": {"fs": {"type": "local", "command": ["fs"]}}},
    )
    project_path = write(tmp_path / "project.json", {"mcp": {"gh": {"type": "remote", "url": "u"}}})

    servers = load_mcp_servers([global_path, project_path], [])

    assert servers.source == f"{global_path}, {project_path}"


def test_a_file_with_an_empty_mcp_table_contributes_no_source(tmp_path: Path) -> None:
    empty = write(tmp_path / "global.json", {"mcp": {}})
    real = write(tmp_path / "project.json", {"mcp": {"fs": {"type": "local", "command": ["fs"]}}})
    warnings: list[str] = []

    servers = load_mcp_servers([empty, real], warnings)

    assert servers.source == str(real)
    assert warnings == []
