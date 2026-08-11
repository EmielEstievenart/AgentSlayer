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
from agentclip.app.types import EngineRequest
from agentclip.config import Config, load_config
from agentclip.hosts import FakeHost
from agentclip.hosts.ssh import SshError

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

    def connect(self) -> None:
        if self._fail == "connect":
            raise SshError("authentication failed for dev@box: no")
        self.connected = True

    def probe_os(self) -> str:
        if self._fail == "probe":
            raise SshError("dev@box did not answer 'uname -s'")
        self.os_name = "Linux (ssh)"
        return self.os_name

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
    monkeypatch.setattr("agentclip.hosts.ssh.SshHost", lambda *a, **kw: made)
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
        "agentclip.hosts.ssh.SshHost", lambda *a, **kw: FakeSshHost(fail="connect")
    )
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    assert cli.remote_launch(args) == 2
    assert "authentication failed" in capsys.readouterr().err


def test_a_box_that_cannot_be_probed_is_fatal(args, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        "agentclip.hosts.ssh.SshHost", lambda *a, **kw: FakeSshHost(fail="probe")
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
    monkeypatch.setattr("agentclip.hosts.ssh.SshHost", lambda *a, **kw: empty)
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
    )
    engine = build(EngineRequest(service="claude"))

    payload = engine.start_task("do it").chunks[0]
    assert "on Linux (ssh)" in payload  # the bootstrap tells the truth
    assert "deploy" in payload  # ...and the skills came off the remote machine
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
