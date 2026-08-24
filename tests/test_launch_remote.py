"""The remote launch flow: one host, wired once, before the TUI exists.

cli.remote_launch is the single construction point the design gives phase 2 -
connect, probe, read the REMOTE project config, and hand ONE host to whatever a
session is then built from. What these tests pin is that order, plus every way
the launch is supposed to fail fast (docs/design/remote-ssh.md 6, and the
implementation notes).

What a launch is built INTO changed with increment 4's flip
(docs/design/remote-executor.md §2.12): ``--ssh`` now starts ``agentclip-engine``
ON the target and drives it over the wire, so the section "the engine runs on the
target" below takes ``cli.main`` all the way through a scripted handshake. The
legacy per-call ``SshHost`` assembly - the engine here, the target reached one
round trip at a time - is still constructable and still tested (the last
section); it is simply no longer what any shell does, and §2.8's deletion of it
is increment 5.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

import pytest

from agentclip import cli
from agentclip.config import Config, load_config
from agentclip.engine.link import wire
from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.hosts import FakeHost
from agentclip.executor.hosts.ssh import LinkChannel, SshError
from tests.executor.hosts.fake_paramiko import FakeChannel, FakeCommandScript, FakeSSHClient

REMOTE_ROOT = "/home/dev/app"


class RecordingChannel(FakeChannel):
    """A fake exec channel that says WHEN it was closed, relative to the host.

    The teardown order is the one thing about a remote run's exit that a test
    can get wrong silently: the engine dies with its channel (§2.3), so the
    channel has to go before the transport it rides on - a host closed first
    would turn an orderly shutdown into a dropped link.
    """

    def __init__(self, script: FakeCommandScript, order: list[str]) -> None:
        super().__init__(script, FakeSSHClient())
        self._order = order

    def close(self) -> None:
        self._order.append("channel")
        super().close()


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
        # The link half: what `agentclip-engine` was asked to be, what it says
        # back on stdout, and what it wrote to stderr before dying. Defaults to a
        # box with the engine installed and answering the handshake.
        self.link_commands: list[str] = []
        self.link_chunks: list[bytes] = [_line(wire.hello_ack_frame("server-1"))]
        self.link_stderr: list[bytes] = []
        self.link_exit: int | None = None  # None = still running
        self.channels: list[RecordingChannel] = []
        self.order: list[str] = []
        # Which spellings of the engine can actually be RUN over there. None
        # means every one can (a box with it on PATH); a set is how a target
        # whose non-interactive PATH hides ~/.local/bin looks from here - the
        # plain name exits 127, the explicit path does not.
        self.runnable: set[str] | None = None

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

    def open_link_channel(self, command: str) -> LinkChannel:
        """``SshHost.open_link_channel``, over a scripted channel."""
        self.link_commands.append(command)
        executable = shlex.split(command)[0]
        absent = self.runnable is not None and executable not in self.runnable
        chan = RecordingChannel(
            FakeCommandScript(
                hangs=self.link_exit is None and not absent,
                exit_code=127 if absent else (self.link_exit or 0),
                chunks=[] if absent else list(self.link_chunks),
                stderr_chunks=(
                    [f"bash: {executable}: command not found\n".encode()]
                    if absent
                    else list(self.link_stderr)
                ),
            ),
            self.order,
        )
        self.channels.append(chan)
        return LinkChannel(chan)  # type: ignore[arg-type]

    def close(self) -> None:
        self.order.append("host")
        self.closed = True


def _line(frame: dict[str, Any]) -> bytes:
    return wire.encode_line(frame).encode("utf-8")


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
    """The `on {os}` slot must not claim the operator's PC (design note).

    A legacy-assembly field now: the engine on the target derives its own
    ``os_name`` from the platform it is running on (``make_engine_builder``'s
    ``os_name=None`` default), so nothing on the default path reads this. It is
    still what the probe found, and still what the per-call path would use.
    """
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.os_name == "Linux (ssh)"


def test_the_local_state_dir_is_still_resolved_for_the_legacy_assembly(
    args, host: FakeSshHost, tmp_path: Path
) -> None:
    """Where a remote session's own state WOULD go if the engine ran here.

    It does not any more: the store follows the engine, so a remote session's
    transcripts and backups land in ``<project>/.agentclip/`` on the target
    (docs/design/remote-executor.md §2.4). ``main`` no longer creates or prunes
    this directory - see the flip's own test - but ``Launch`` still resolves it,
    because the per-call path it belongs to is still constructable until
    increment 5.
    """
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.data_root == tmp_path / "state"


def test_the_launch_carries_the_dialled_machine_itself(args, host: FakeSshHost) -> None:
    """What the engine is launched over: the ConnectedRemote, whole.

    ``make_remote_link_factory`` takes the dialled machine (its host and its
    project root), not a launch's summary of it - so the launch carries the
    thing rather than flattening it (§2.12).
    """
    launch = cli.remote_launch(args)
    assert not isinstance(launch, int)
    assert launch.remote is not None
    assert launch.remote.host is host
    assert launch.remote.project_root.as_posix() == REMOTE_ROOT


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


# == the engine runs on the target (the default since increment 4) =============
# docs/design/remote-executor.md §2.12. These drive ``cli.main`` all the way
# through: the real connect sequence over the fake host, the real
# ``make_remote_link_factory``, the real ``RemoteLinkClient`` handshake - over a
# scripted exec channel instead of a network. What is NOT exercised is a session
# (nothing calls the factory without a shell), which is what the localhost
# subprocess suite is for (tests/shell/app/test_remote_link.py).


class FakeApp:
    """AgentClipApp, cut to what a launch hands it and what ``main`` calls."""

    last: FakeApp | None = None

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.app_config = kwargs["config"]
        self.ran = 0
        FakeApp.last = self

    def run(self) -> None:
        self.ran += 1


@pytest.fixture
def tui(monkeypatch: pytest.MonkeyPatch) -> type[FakeApp]:
    """``main`` without a terminal under it: no sixel probe, no Textual."""
    FakeApp.last = None
    monkeypatch.setattr(cli, "probe_terminal", lambda: None)
    monkeypatch.setattr(cli, "AgentClipApp", FakeApp)
    return FakeApp


def argv(args: argparse.Namespace, *extra: str) -> list[str]:
    # ``--tui`` because it is the TUI's launch that is under test here: the GUI
    # defers an ``--ssh`` dial into its own connect dialog (cli.py's
    # ``pending_connect``), so a bare launch would never reach ``remote_launch``
    # at all. The GUI's side of that is tests/shell/gui/test_connect.py.
    return [
        "--tui",
        "--project",
        args.project,
        "--ssh",
        "box",
        "--remote-root",
        REMOTE_ROOT,
        *extra,
    ]


def launched(app: type[FakeApp]) -> FakeApp:
    assert app.last is not None
    return app.last


def test_ssh_launches_the_engine_on_the_target_and_drives_it_over_the_wire(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp]
) -> None:
    """The flip itself: no engine is assembled here, one is started over there.

    The command is ``engine_command``'s - the pre-installed console script, the
    CONNECTED root, and deliberately no ``--global-config``/``--home``/
    ``--data-root``, because on the target the engine reads the target's own
    (§2.5, §2.12).
    """
    assert cli.main(argv(args)) == 0

    assert host.link_commands == [f"agentclip-engine --project {REMOTE_ROOT}"]
    assert b'"hello"' in bytes(host.channels[0].sent)  # ...and it really shook hands
    app = launched(tui)
    assert not isinstance(app.kwargs["engine_factory"], cli.LinkFactory)
    assert callable(app.kwargs["engine_factory"])


def test_the_shells_mcp_source_is_the_engine_on_the_target(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp]
) -> None:
    """MCP servers spawn where the engine runs (§2.7), so the status source has
    to be the link - and unconditionally, unlike the local builder's: it has
    nothing to report until the first session build carries the settle home, and
    gating it on a non-empty reading would drop it before it could answer."""
    assert cli.main(argv(args)) == 0
    source = launched(tui).kwargs["mcp_manager"]
    assert isinstance(source, cli.RemoteEngine)
    assert source.statuses() == ()  # no session built yet, and that is honest


def test_the_service_flag_reaches_the_targets_engine(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp]
) -> None:
    """The one part of the target's config a local flag legitimately overrides."""
    assert cli.main(argv(args, "--service", "claude")) == 0
    assert host.link_commands == [f"agentclip-engine --project {REMOTE_ROOT} --service claude"]


def test_a_remote_session_keeps_no_session_tree_on_this_pc(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp], tmp_path: Path
) -> None:
    """§2.4: the store follows the engine, and the engine is over there now.

    The local ``<user_data_dir>/agentclip/remote/...`` tree belongs to the legacy
    per-call path; creating and pruning one for a session whose transcripts land
    on the target would leave an empty directory pretending to hold a history.
    """
    assert cli.main(argv(args)) == 0
    assert not (tmp_path / "state").exists()


def test_the_link_channel_is_closed_before_the_transport_under_it(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp]
) -> None:
    """The engine dies with its channel (§2.3), so the channel goes first: a
    host closed first would make an orderly shutdown look like a dropped link."""
    assert cli.main(argv(args)) == 0
    assert host.order == ["channel", "host"]


def test_an_install_hidden_from_sshds_path_is_found_under_local_bin(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp]
) -> None:
    """The everyday target: `uv tool install agentclip`, and a dead launch.

    ``uv tool install`` writes ``~/.local/bin`` and sshd's non-interactive exec
    channel does not have it on PATH - the rule on Ubuntu, not the exception -
    so launching by name exits 127 on a box that HAS the engine. One retry at
    the well-known path, and the session proceeds exactly as if the plain name
    had worked, handshake and all.
    """
    host.runnable = {"/home/dev/.local/bin/agentclip-engine"}

    assert cli.main(argv(args)) == 0

    assert host.link_commands == [
        f"agentclip-engine --project {REMOTE_ROOT}",
        f"/home/dev/.local/bin/agentclip-engine --project {REMOTE_ROOT}",
    ]
    assert b'"hello"' in bytes(host.channels[1].sent)  # ...and it really shook hands
    assert host.channels[0].closed  # the dead attempt did not leak a channel
    assert launched(tui).ran == 1


def test_the_fallback_is_only_reached_when_the_plain_name_is_missing(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp], capsys
) -> None:
    """A launch that DIED is a diagnosis, not a reason to try another path.

    A broken install, a traceback, a killed process: retrying somewhere else
    would replace the target's own words with a worse guess.
    """
    host.link_chunks = []
    host.link_exit = 3
    host.link_stderr = [b"ImportError: no agentclip\n"]

    assert cli.main(argv(args)) == 2

    assert host.link_commands == [f"agentclip-engine --project {REMOTE_ROOT}"]
    err = capsys.readouterr().err
    assert "exit 3" in err and "ImportError: no agentclip" in err


def test_a_target_without_the_engine_anywhere_is_fatal_and_says_what_was_tried(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp], capsys
) -> None:
    """The failure every first connect hits, on the stream every other fatal
    step of going remote uses, with the same exit code (§2.12).

    Both spellings are named, because both were tried: "not installed" to
    somebody who just installed it reads as a broken tool.
    """
    host.runnable = set()  # neither the name nor the path runs

    assert cli.main(argv(args)) == 2

    assert host.link_commands == [
        f"agentclip-engine --project {REMOTE_ROOT}",
        f"/home/dev/.local/bin/agentclip-engine --project {REMOTE_ROOT}",
    ]
    err = capsys.readouterr().err
    assert "agentclip-engine is not on the non-interactive PATH of dev@box" in err
    assert "'~/.local/bin/agentclip-engine'" in err
    assert "uv tool install agentclip" in err
    assert "link_closed" not in err  # the wire's own vocabulary is not a sentence
    assert tui.last is None  # ...and no TUI was opened on top of a dead link
    assert host.closed  # ...and the connection did not outlive the attempt


def test_a_wire_version_mismatch_names_both_installs(
    args: argparse.Namespace, host: FakeSshHost, tui: type[FakeApp], capsys
) -> None:
    """The far side ANSWERED, so the channel has nothing to add: ``hello()``'s
    sentence is the whole message (§2.9)."""
    host.link_chunks = [
        _line({"type": "hello_ack", "version": 2, "package": "0.1.0", "server_id": "s"})
    ]

    assert cli.main(argv(args)) == 2

    err = capsys.readouterr().err
    assert "wire v2 (agentclip 0.1.0)" in err
    assert "uv tool install --upgrade agentclip" in err
    assert host.closed


def test_a_local_launch_still_builds_its_engine_here(
    tmp_path: Path, tui: type[FakeApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twin of the flip: nothing about a local run changed, and the branch
    is what skips the wire rather than the wire being skipped by accident."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    assert cli.main(["--tui", "--project", str(project)]) == 0
    assert isinstance(launched(tui).kwargs["engine_factory"], cli.LinkFactory)


# == the legacy per-call SshHost assembly (increment 5 deletes it) =============
# Not reachable from ``--ssh`` or the GUI's connect any more - the flip above is
# what a remote session does. It stays constructable, and pinned, because §2.8's
# deletion is a whole increment of its own and a path nobody tests is a path
# nobody can delete safely.


def test_the_legacy_assembly_still_builds_a_whole_session_over_one_host(
    tmp_path: Path,
) -> None:
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
