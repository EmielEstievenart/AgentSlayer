"""The remote launch flow: one host, wired once, before the TUI exists.

cli.remote_launch is the single construction point the design gives phase 2 -
connect, probe, read the REMOTE project config, and hand ONE host to the
workspace jail, the tool context, the backup store and the engine. What these
tests pin is that order and that sharing, plus every way the launch is supposed
to fail fast (docs/design/remote-ssh.md 6, and the implementation notes).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentclip import cli
from agentclip.config import Config, load_config
from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.hosts import FakeHost
from agentclip.executor.hosts.ssh import SshError

REMOTE_ROOT = "/home/dev/app"


class FakeSshHost(FakeHost):
    """A FakeHost that answers the launch flow's SSH-shaped questions too."""

    def __init__(self, root: str = REMOTE_ROOT, *, fail: str = "") -> None:
        super().__init__(root)
        self.target = "dev@box"
        self.os_name = "remote"
        self.connected = False
        self.closed = False
        self._fail = fail  # "connect" | "probe" | ""
        # What `printenv` answers. A box with a plausible login environment by
        # default, so every launch test exercises the probe the way a real one
        # does; the tests about the probe itself replace it.
        self.blocking: dict[str, tuple[int, str]] = {"printenv": (0, "HOME=/home/dev\n")}

    def run_blocking(self, command: str, *, timeout: float = 60.0) -> tuple[int, str]:
        return self.blocking.get(command, (0, ""))

    def connect(self) -> None:
        if self._fail == "connect":
            raise SshError("authentication failed for dev@box: no")
        self.connected = True

    def probe_os(self) -> str:
        if self._fail == "probe":
            raise SshError("dev@box did not answer 'uname -s'")
        self.os_name = "Linux (ssh)"
        return self.os_name

    def home_dir(self) -> Path:
        return Path("/home/dev")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def args(tmp_path: Path) -> argparse.Namespace:
    """The parsed command line of `agentclip --ssh box --remote-root ...`."""
    local = tmp_path / "local"
    local.mkdir()
    return argparse.Namespace(
        project=str(local),
        service=None,
        ssh="box",
        remote_root=REMOTE_ROOT,
    )


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSshHost:
    """Whatever SshHost cli would build, it gets this one instead."""
    made = FakeSshHost()
    made.add_dir(REMOTE_ROOT)
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: made)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    monkeypatch.setattr(
        "agentclip.config.default_remote_state_dir",
        lambda target, root: tmp_path / "state",
    )
    monkeypatch.setattr(cli, "default_remote_state_dir", lambda target, root: tmp_path / "state")
    return made


# -- the happy path ------------------------------------------------------------


def test_the_remote_root_becomes_the_project_root(args, host: FakeSshHost) -> None:
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.project_root.as_posix() == REMOTE_ROOT
    assert launch.host is host
    assert host.connected


def test_the_global_skill_folders_are_the_remote_users(args, host: FakeSshHost) -> None:
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.home == Path("/home/dev")  # not this operator's ~


def test_the_bootstrap_os_is_the_remote_one(args, host: FakeSshHost) -> None:
    """The `on {os}` slot must not claim the operator's PC (design note)."""
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.os_name == "Linux (ssh)"


def test_the_session_tree_lands_on_this_pc(args, host: FakeSshHost, tmp_path: Path) -> None:
    """Backups and transcripts are AgentClip's own state: they stay local."""
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.data_root == tmp_path / "state"


def test_the_project_config_is_read_from_the_remote_machine(
    args, host: FakeSshHost
) -> None:
    host.add_file(f"{REMOTE_ROOT}/.agentclip.toml", '[general]\nservice = "claude"\n')
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.config.general.service == "claude"


def test_a_saved_target_supplies_the_root(args, host: FakeSshHost, tmp_path: Path) -> None:
    (tmp_path / "global.toml").write_text(
        f'[remote.box]\nhost = "10.0.0.5"\nuser = "dev"\nroot = "{REMOTE_ROOT}"\n',
        encoding="utf-8",
    )
    args.remote_root = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "agentclip.config.default_global_config_path", lambda: tmp_path / "global.toml"
        )
        launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.project_root.as_posix() == REMOTE_ROOT


# -- the target's environment ($env: in ITS mcp config) ------------------------


def test_the_environment_is_the_targets_and_reaches_the_mcp_config(
    args, host: FakeSshHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{env:...}` in the target's permissions.json means the TARGET's variable.

    The operator's own environment holds a different secret under the same
    name, and reading it here would send it to the wrong server.
    """
    monkeypatch.setenv("AGENTCLIP_LAUNCH_TOKEN", "this-pcs-secret")
    host.blocking["printenv"] = (0, "AGENTCLIP_LAUNCH_TOKEN=the-boxs-secret\n")
    host.add_file(
        f"{REMOTE_ROOT}/.agentclip/permissions.json",
        '{"mcp": {"api": {"type": "remote", "url": "https://x",'
        ' "headers": {"Authorization": "Bearer {env:AGENTCLIP_LAUNCH_TOKEN}"}}}}',
    )

    launch = cli.remote_launch(args)

    assert not isinstance(launch, int)
    (server,) = launch.config.mcp_servers.servers
    assert server.headers == (("Authorization", "Bearer the-boxs-secret"),)


def test_printenv_output_becomes_a_mapping() -> None:
    assert cli._parse_environment("HOME=/home/dev\nPATH=/usr/bin:/bin\n") == {
        "HOME": "/home/dev",
        "PATH": "/usr/bin:/bin",
    }


def test_a_line_that_is_not_a_variable_is_dropped() -> None:
    """A continuation line of a multi-line value looks exactly like a login
    banner from here, so neither is guessed at: only real pairs survive."""
    parsed = cli._parse_environment(
        "Welcome to box!\n"
        "GREETING=hello\n"
        "  and the rest of a multi-line value\n"
        "2BAD=no\n"  # not a POSIX name
        "PATH=/usr/bin\n"
    )
    assert parsed == {"GREETING": "hello", "PATH": "/usr/bin"}


def test_an_empty_value_is_still_a_variable() -> None:
    assert cli._parse_environment("EMPTY=\n") == {"EMPTY": ""}


def test_a_failed_printenv_is_a_warning_and_an_empty_environment(
    host: FakeSshHost, capsys
) -> None:
    """Not fatal: an unset `{env:...}` already substitutes empty, so the worst
    case is the state the user's own config file describes as acceptable."""
    host.blocking["printenv"] = (127, "bash: printenv: command not found\n")

    assert cli._remote_environment(host) == {}
    assert "did not answer 'printenv'" in capsys.readouterr().err


def test_a_remote_session_never_falls_back_to_this_pcs_environment(
    args, host: FakeSshHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTCLIP_LAUNCH_TOKEN", "this-pcs-secret")
    host.blocking["printenv"] = (1, "")
    host.add_file(
        f"{REMOTE_ROOT}/.agentclip/permissions.json",
        '{"mcp": {"api": {"type": "remote", "url": "https://x/{env:AGENTCLIP_LAUNCH_TOKEN}"}}}',
    )

    launch = cli.remote_launch(args)

    assert not isinstance(launch, int)
    (server,) = launch.config.mcp_servers.servers
    assert server.url == "https://x/"  # empty, not the operator's secret


# -- failing fast, before the TUI ----------------------------------------------


def test_no_root_anywhere_is_refused_before_connecting(
    args, host: FakeSshHost, capsys
) -> None:
    args.remote_root = None
    assert cli.remote_launch(args) == 2
    assert "needs a project root" in capsys.readouterr().err
    assert not host.connected


def test_a_failed_connection_is_reported_and_fatal(args, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        "agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: FakeSshHost(fail="connect")
    )
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    assert cli.remote_launch(args) == 2
    assert "authentication failed" in capsys.readouterr().err


def test_a_box_that_cannot_be_probed_is_fatal(args, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        "agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: FakeSshHost(fail="probe")
    )
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    assert cli.remote_launch(args) == 2
    assert "uname -s" in capsys.readouterr().err


def test_a_missing_remote_root_is_fatal_and_closes_the_link(
    args, monkeypatch, tmp_path, capsys
) -> None:
    empty = FakeSshHost("/elsewhere")
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: empty)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    assert cli.remote_launch(args) == 2
    assert "cannot use" in capsys.readouterr().err
    assert empty.closed


def test_a_remote_root_that_is_a_file_is_fatal(args, host: FakeSshHost, capsys) -> None:
    host.add_file(f"{REMOTE_ROOT}/../file.txt", "x")
    args.remote_root = "/home/dev/file.txt"
    host.add_file("/home/dev/file.txt", "x")
    assert cli.remote_launch(args) == 2
    assert "not a directory" in capsys.readouterr().err
    assert host.closed


# -- one host, everywhere ------------------------------------------------------


def test_every_part_of_a_session_gets_the_same_host(tmp_path: Path) -> None:
    """The single-construction-point rule: no session is half local."""
    host = FakeSshHost()
    host.add_file(f"{REMOTE_ROOT}/README.md", "hi\n")
    host.add_file(f"{REMOTE_ROOT}/.claude/skills/deploy/SKILL.md", "---\nname: deploy\n---\ngo")
    # A global skill folder under the REMOTE user's home, not the operator's.
    host.add_file("/home/dev/.claude/skills/release/SKILL.md", "---\nname: release\n---\nship")

    def get_config() -> Config:
        return load_config(
            Path(REMOTE_ROOT), global_config_path=tmp_path / "none.toml", host=host
        )

    build = cli.make_engine_factory(
        get_config,
        Path(REMOTE_ROOT),
        host=host,
        os_name="Linux (ssh)",
        data_root=tmp_path / "state",
        home=host.home_dir(),
    )
    # The factory returns a Link; this test is about what it BUILT, so unwrap.
    engine = build(EngineRequest(service="claude")).engine

    payload = engine.start_task("do it").chunks[0]
    assert "on Linux (ssh)" in payload  # the bootstrap tells the truth
    assert "deploy" in payload  # ...and the skills came off the remote machine
    assert "release" in payload  # ...including the remote user's own
    # The session tree is on THIS PC, next to nothing the remote host holds.
    assert (tmp_path / "state" / ".agentclip" / "sessions").is_dir()
    assert host.stat(Path(f"{REMOTE_ROOT}/.agentclip")) is None


def test_a_local_launch_is_unchanged(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    launch = cli.local_launch(
        argparse.Namespace(project=str(project), service=None, ssh=None, remote_root=None)
    )
    assert not isinstance(launch, int)
    assert launch.project_root == project.resolve()
    assert launch.data_root == launch.project_root
    assert launch.host.name == "local"
