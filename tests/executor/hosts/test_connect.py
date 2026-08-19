"""The remote connect sequence, driven with scripted callbacks and no network.

These tests protect BOTH shells: ``cli.remote_launch`` is a wrapper over
``connect_remote`` now (its own stderr/exit-code tests live in
``tests/test_launch_remote.py``) and the GUI's connect dialog drives the same
function with modals instead of ``getpass``. What is pinned here is the part
neither shell may re-decide - the ORDER of the six steps, which of them are
fatal, what each failure says, and that a failed attempt leaves no socket open
(docs/design/ui-briefs/ssh-connect.md §2).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentclip.executor.hosts import FakeHost
from agentclip.executor.hosts.connect import (
    CONNECT_STEPS,
    PASSWORD_ATTEMPTS,
    STEP_CONFIG,
    STEP_CONNECT,
    STEP_ENV,
    STEP_PROBE,
    STEP_RESOLVE,
    STEP_ROOT,
    ConnectError,
    ConnectPrompts,
    StepEvent,
    connect_remote,
    describe_target,
    parse_environment,
    remote_environment,
    resolve_target,
    ssh_config_aliases,
)
from agentclip.executor.hosts.ssh import SshError

REMOTE_ROOT = "/home/dev/app"


class ScriptedHost(FakeHost):
    """An SshHost stand-in: an in-memory filesystem plus the launch probes."""

    def __init__(self, root: str = REMOTE_ROOT, *, fail: str = "") -> None:
        super().__init__(root)
        self.target = "dev@box"
        self.name = "ssh:box"
        self.os_name = "remote"
        self.connected = False
        self.closed = 0
        self.kwargs: dict[str, object] = {}
        self._fail = fail  # "connect" | "probe" | ""
        self.blocking: dict[str, tuple[int, str]] = {"printenv": (0, "HOME=/home/dev\n")}

    def run_blocking(self, command: str, *, timeout: float = 60.0) -> tuple[int, str]:
        return self.blocking.get(command, (0, ""))

    def connect(self) -> None:
        if self._fail == "connect":
            raise SshError("authentication failed for dev@box: no")
        self.connected = True

    def probe_os(self) -> str:
        if self._fail == "probe":
            raise SshError("dev@box did not answer 'uname -s' (exit 127)")
        self.os_name = "Linux (ssh)"
        return self.os_name

    def home_dir(self) -> Path:
        return Path("/home/dev")

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def local(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    root.mkdir()
    return root


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ScriptedHost:
    """Whatever SshHost connect_remote builds, it gets this one instead."""
    made = ScriptedHost()
    made.add_dir(REMOTE_ROOT)

    def build(*args: object, **kwargs: object) -> ScriptedHost:
        made.kwargs = dict(kwargs)
        return made

    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", build)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    monkeypatch.setattr(
        "agentclip.executor.hosts.connect.default_remote_state_dir",
        lambda target, root: tmp_path / "state",
    )
    return made


def run(local: Path, host: ScriptedHost, **kwargs: object) -> tuple[object, list[StepEvent]]:
    """Drive the sequence, collecting every step event it reports."""
    seen: list[StepEvent] = []
    spec = str(kwargs.pop("target_spec", "box"))
    root = kwargs.pop("remote_root", REMOTE_ROOT)
    try:
        result: object = connect_remote(
            spec,
            root if root is None else str(root),
            local_root=local,
            on_step=seen.append,
            **kwargs,  # type: ignore[arg-type]
        )
    except ConnectError as err:
        result = err
    return result, seen


def beats(seen: list[StepEvent]) -> list[tuple[str, str]]:
    return [(e.step, e.state) for e in seen]


# -- the order, which is the design's ------------------------------------------


def test_the_six_steps_run_in_the_declared_order(local: Path, host: ScriptedHost) -> None:
    """cli.py's launch order, and the reason 'the target owns its policy' is
    safe: nothing reads the remote ruleset before the box has been dialled."""
    result, seen = run(local, host)
    assert not isinstance(result, ConnectError)
    assert beats(seen) == [
        (step, state) for step in CONNECT_STEPS for state in ("running", "ok")
    ]
    assert [e.step for e in seen if e.state == "running"] == list(CONNECT_STEPS)


def test_the_connected_remote_carries_what_a_launch_needs(
    local: Path, host: ScriptedHost, tmp_path: Path
) -> None:
    result, _ = run(local, host)
    assert not isinstance(result, ConnectError)
    assert result.project_root.as_posix() == REMOTE_ROOT
    assert result.os_name == "Linux (ssh)"
    assert result.home == Path("/home/dev")  # not this operator's ~
    assert result.data_root == tmp_path / "state"  # AgentClip's own state stays local
    assert result.host is host
    assert result.target.host == "box"


def test_the_project_config_comes_off_the_target(local: Path, host: ScriptedHost) -> None:
    host.add_file(f"{REMOTE_ROOT}/.agentclip.toml", '[general]\nservice = "claude"\n')
    result, _ = run(local, host)
    assert not isinstance(result, ConnectError)
    assert result.config.general.service == "claude"


def test_the_prompts_are_handed_to_the_host(local: Path, host: ScriptedHost) -> None:
    """All three of them, including the one paramiko does not call yet: the
    seam has to be wired before the day it is used (ssh-connect.md §3.7)."""
    prompts = ConnectPrompts(
        password=lambda text: "pw",
        host_key=lambda h, k, f: True,
        keyboard_interactive=lambda t, i, p: ["123456"],
    )
    run(local, host, prompts=prompts)
    assert host.kwargs["password_prompt"] is prompts.password
    assert host.kwargs["host_key_prompt"] is prompts.host_key
    assert host.kwargs["keyboard_prompt"] is prompts.keyboard_interactive


# -- each failure lands on its own step ----------------------------------------


def test_no_root_anywhere_fails_the_resolve_step_before_dialling(
    local: Path, host: ScriptedHost
) -> None:
    result, seen = run(local, host, remote_root=None)
    assert isinstance(result, ConnectError)
    assert result.step == STEP_RESOLVE
    assert "needs a project root" in result.message
    assert beats(seen) == [(STEP_RESOLVE, "running"), (STEP_RESOLVE, "failed")]
    assert not host.connected  # nothing was dialled


def test_a_refused_dial_fails_the_connect_step(
    local: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dead = ScriptedHost(fail="connect")
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: dead)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    result, seen = run(tmp_path, dead)
    assert isinstance(result, ConnectError)
    assert result.step == STEP_CONNECT
    assert "authentication failed" in result.message
    assert beats(seen)[-1] == (STEP_CONNECT, "failed")
    assert STEP_PROBE not in [e.step for e in seen]  # later steps stay pending


def test_a_box_that_cannot_be_probed_fails_the_probe_step(
    local: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dead = ScriptedHost(fail="probe")
    dead.add_dir(REMOTE_ROOT)
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: dead)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    result, seen = run(tmp_path, dead)
    assert isinstance(result, ConnectError)
    assert result.step == STEP_PROBE
    assert "uname -s" in result.message
    assert beats(seen)[-1] == (STEP_PROBE, "failed")


def test_a_missing_remote_root_fails_the_root_step(
    local: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = ScriptedHost("/elsewhere")
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: empty)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    result, seen = run(tmp_path, empty)
    assert isinstance(result, ConnectError)
    assert result.step == STEP_ROOT
    assert "cannot use" in result.message
    assert beats(seen)[-1] == (STEP_ROOT, "failed")


def test_a_remote_root_that_is_a_file_fails_the_root_step(
    local: Path, host: ScriptedHost
) -> None:
    host.add_file("/home/dev/file.txt", "x")
    result, _ = run(local, host, remote_root="/home/dev/file.txt")
    assert isinstance(result, ConnectError)
    assert result.step == STEP_ROOT
    assert "not a directory" in result.message


@pytest.mark.parametrize("fail", ["connect", "probe"])
def test_a_failed_attempt_closes_the_host_it_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: str
) -> None:
    """No socket outlives its attempt: the GUI retries in place, so a leak
    would be one per press rather than one per process."""
    dead = ScriptedHost(fail=fail)
    dead.add_dir(REMOTE_ROOT)
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: dead)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    run(tmp_path, dead)
    assert dead.closed == 1


def test_a_host_key_decline_aborts_the_attempt(
    local: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_AskPolicy raises SshError from inside connect() when the fingerprint is
    refused, so a decline lands as a failed CONNECT step - never as a session
    that quietly went on without checking the key."""
    refused = ScriptedHost()
    refused.add_dir(REMOTE_ROOT)

    def connect() -> None:
        raise SshError("the host key for box was not accepted")

    refused.connect = connect  # type: ignore[method-assign]
    monkeypatch.setattr("agentclip.executor.hosts.ssh.SshHost", lambda *a, **kw: refused)
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "no-global.toml"
    )
    result, seen = run(tmp_path, refused)
    assert isinstance(result, ConnectError)
    assert result.step == STEP_CONNECT
    assert "was not accepted" in result.message
    assert refused.closed == 1


# -- the two steps that never fail a launch ------------------------------------


def test_an_unusable_printenv_is_a_note_on_a_step_that_still_passes(
    local: Path, host: ScriptedHost
) -> None:
    host.blocking["printenv"] = (127, "bash: printenv: command not found\n")
    result, seen = run(local, host)
    assert not isinstance(result, ConnectError)  # not fatal
    assert result.environ == {}
    (env_ok,) = [e for e in seen if e.step == STEP_ENV and e.state == "ok"]
    assert "did not answer 'printenv'" in env_ok.note
    assert "{env:...}" in env_ok.note


def test_a_remote_session_never_falls_back_to_this_pcs_environment(
    local: Path, host: ScriptedHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTCLIP_CONNECT_TOKEN", "this-pcs-secret")
    host.blocking["printenv"] = (1, "")
    host.add_file(
        f"{REMOTE_ROOT}/.agentclip/permissions.json",
        '{"mcp": {"api": {"type": "remote", "url": "https://x/{env:AGENTCLIP_CONNECT_TOKEN}"}}}',
    )
    result, _ = run(local, host)
    assert not isinstance(result, ConnectError)
    (server,) = result.config.mcp_servers.servers
    assert server.url == "https://x/"  # empty, not the operator's secret


def test_a_bad_remote_config_is_a_warning_not_a_failed_step(
    local: Path, host: ScriptedHost
) -> None:
    """load_config never raises; its complaints ride on Config.warnings, so the
    CONFIG step always ticks green and the notices go beside it.

    The sample used to be a remote `[approval]` table, which earned a warning of
    its own back when it was ignored. It is not ignored any more - the engine
    owns policy wholesale (docs/design/remote-executor.md section 2.5) - so the
    warning here is an unreadable VALUE, and the same table's readable key is
    asserted to have taken effect through the real connect sequence.
    """
    host.add_file(
        f"{REMOTE_ROOT}/.agentclip.toml", '[approval]\nmode = "wobble"\nyolo = true\n'
    )
    result, seen = run(local, host)
    assert not isinstance(result, ConnectError)
    assert beats(seen)[-1] == (STEP_CONFIG, "ok")
    assert any("unknown approval mode" in w for w in result.config.warnings)
    assert result.config.approval.yolo is True  # the TARGET's table, honoured


def test_the_step_notes_are_the_sentences_the_terminal_prints(
    local: Path, host: ScriptedHost
) -> None:
    """cli.remote_launch's stderr/stdout wording IS these notes - the wrapper
    only chooses a stream and a prefix (tests/test_launch_remote.py holds it)."""
    _, seen = run(local, host)
    notes = {(e.step, e.state): e.note for e in seen}
    assert notes[(STEP_CONNECT, "running")] == "connecting to box..."
    assert notes[(STEP_ROOT, "ok")] == f"dev@box is Linux (ssh), working in {REMOTE_ROOT}"


# -- the pieces the dialog reads on its own ------------------------------------


def test_resolve_reads_a_saved_target_out_of_the_global_config(
    local: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "global.toml").write_text(
        f'[remote.box]\nhost = "10.0.0.5"\nuser = "dev"\nport = 2222\nroot = "{REMOTE_ROOT}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "global.toml"
    )
    target, boot = resolve_target("box", None, local_root=local)
    assert (target.host, target.user, target.port, target.root) == (
        "10.0.0.5",
        "dev",
        2222,
        REMOTE_ROOT,
    )
    assert "box" in boot.remote.targets


def test_a_spelled_out_destination_needs_no_saved_target(
    local: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", lambda: tmp_path / "none.toml"
    )
    target, _ = resolve_target("pi@10.0.0.9:2200", REMOTE_ROOT, local_root=local)
    assert (target.user, target.host, target.port) == ("pi", "10.0.0.9", 2200)
    assert describe_target(target) == "pi@10.0.0.9:2200"


def test_the_preview_omits_a_default_port_the_way_ssh_host_does() -> None:
    from agentclip.config import RemoteTarget

    assert describe_target(RemoteTarget(host="box")) == "box"
    assert describe_target(RemoteTarget(host="box", user="dev")) == "dev@box"
    assert describe_target(RemoteTarget(host="box", user="dev", port=22)) == "dev@box"


def test_ssh_config_aliases_hide_wildcards_and_match_blocks(tmp_path: Path) -> None:
    """gui.md §4 ruling 3. Note the Match block: paramiko's own
    ``get_hostnames()`` raises KeyError on a file that has one, which is why
    this is read off the parse rather than off that accessor."""
    config = tmp_path / "config"
    config.write_text(
        "Host alpha\n  HostName 10.0.0.1\n"
        "Host *.example.com\n  User dev\n"
        "Match host beta\n  User x\n"
        "Host gamma delta\n  Port 2222\n"
        "Host !nope\n  User y\n",
        encoding="utf-8",
    )
    assert ssh_config_aliases(config) == ["alpha", "gamma", "delta"]


def test_a_missing_ssh_config_lists_nothing(tmp_path: Path) -> None:
    assert ssh_config_aliases(tmp_path / "absent") == []


def test_the_advertised_attempt_count_is_the_one_ssh_host_enforces() -> None:
    """``PASSWORD_ATTEMPTS`` is spelled here so a UI can SAY "attempt 2 of 3"
    without importing the module paramiko rides in on. Two spellings of a number
    is one drift away from a dialog that promises a fourth try (gui.md §4)."""
    from agentclip.executor.hosts import ssh

    assert PASSWORD_ATTEMPTS == ssh._PASSWORD_ATTEMPTS


# -- printenv parsing, which the sequence inherits -----------------------------


def test_printenv_output_becomes_a_mapping() -> None:
    assert parse_environment("HOME=/home/dev\nPATH=/usr/bin:/bin\n") == {
        "HOME": "/home/dev",
        "PATH": "/usr/bin:/bin",
    }


def test_a_line_that_is_not_a_variable_is_dropped() -> None:
    parsed = parse_environment(
        "Welcome to box!\nGREETING=hello\n  and the rest\n2BAD=no\nPATH=/usr/bin\n"
    )
    assert parsed == {"GREETING": "hello", "PATH": "/usr/bin"}


def test_remote_environment_hands_back_its_complaint_rather_than_printing_it(
    host: ScriptedHost,
) -> None:
    host.blocking["printenv"] = (127, "")
    environment, complaint = remote_environment(host)
    assert environment == {}
    assert complaint.startswith("dev@box did not answer 'printenv'")


def test_a_usable_printenv_complains_about_nothing(host: ScriptedHost) -> None:
    assert remote_environment(host) == ({"HOME": "/home/dev"}, "")


# -- and the wrapper the TUI keeps ---------------------------------------------


def test_cli_remote_launch_still_returns_a_launch(
    local: Path, host: ScriptedHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper's own contract, one level up from the sequence's."""
    from agentclip import cli

    monkeypatch.setattr(cli, "default_remote_state_dir", lambda target, root: tmp_path / "state")
    launch = cli.remote_launch(
        argparse.Namespace(
            project=str(local), service=None, ssh="box", remote_root=REMOTE_ROOT
        )
    )
    assert not isinstance(launch, int)
    assert launch.host is host
    assert launch.data_root == tmp_path / "state"
