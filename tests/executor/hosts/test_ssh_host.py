"""SshHost against a paramiko that is a dictionary: no network, no server.

What an ``SshHost`` IS shrank with remote-executor.md §2.8 (increment 5): the
per-call tool path over SSH is deleted, so there is no ``spawn``, no
``ExecHandle``, no kill-tree wrapper and none of the write/traverse primitives
to test any more. What is left - and what this suite pins - is the CONNECTION:
the auth ladder's order, the reconnect state machine, and the handful of probes
and reads :func:`connect_remote` makes before a session exists, including the
POSIX-path discipline they need (this suite runs on Windows, where a Path would
otherwise spell a remote path with backslashes).

The link channel has its own suite (test_link_channel.py). The real thing is
exercised by tests/executor/hosts/test_ssh_real.py, which needs a machine to
talk to and is skipped without one.
"""

from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from agentclip.executor.hosts.connect import PASSWORD_ATTEMPTS
from agentclip.executor.hosts.ssh import CONNECTION_LOST_EXIT, SshError, SshHost

from .fake_paramiko import FakeCommandScript, FakeSSHClient

ROOT = "/home/dev/project"


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> type[FakeSSHClient]:
    """Every SSHClient in this module is the fake one; no test dials anything."""
    FakeSSHClient.reset()
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    return FakeSSHClient


def make_host(tmp_path: Path, **kwargs: object) -> SshHost:
    """A host whose ssh_config/known_hosts point at files that do not exist."""
    defaults: dict = {
        "user": "dev",
        "ssh_config_path": tmp_path / "no-ssh-config",
        "known_hosts_path": tmp_path / "no-known-hosts",
    }
    defaults.update(kwargs)
    return SshHost("box", **defaults)


@pytest.fixture
def host(tmp_path: Path) -> SshHost:
    h = make_host(tmp_path)
    h.connect()
    return h


# -- POSIX paths, from a Windows PC --------------------------------------------


def test_a_path_built_by_pathlib_reaches_the_wire_as_posix(host: SshHost) -> None:
    """The remote root joined with pathlib is still a POSIX path on the wire."""
    FakeSSHClient.fs.add_file(f"{ROOT}/src/utils.py", "x")
    host.read_bytes(Path(ROOT) / "src" / "utils.py")
    assert FakeSSHClient.fs.reads[-1][0] == f"{ROOT}/src/utils.py"


def test_realpath_answers_a_path_that_stays_posix(host: SshHost) -> None:
    FakeSSHClient.fs.add_dir(ROOT)
    assert host.realpath(Path(ROOT)).as_posix() == ROOT


# -- the file primitives -------------------------------------------------------


def test_read_bytes_max_bytes_asks_for_a_prefix_only(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/big.bin", b"x" * 100)
    assert host.read_bytes(Path(f"{ROOT}/big.bin"), max_bytes=8) == b"x" * 8
    assert FakeSSHClient.fs.reads[-1] == (f"{ROOT}/big.bin", 8)
    assert FakeSSHClient.fs.prefetched == []  # never pulls the whole file for a sniff


def test_read_bytes_without_a_cap_reads_it_all(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/a.txt", "hello")
    assert host.read_bytes(Path(f"{ROOT}/a.txt")) == b"hello"


def test_read_bytes_missing_raises_filenotfounderror(host: SshHost) -> None:
    """Paramiko answers a plain IOError; handlers above the seam catch subclasses."""
    with pytest.raises(FileNotFoundError):
        host.read_bytes(Path(f"{ROOT}/nope.txt"))


# -- metadata ------------------------------------------------------------------


def test_stat_of_a_file_reports_size(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/f.txt", "12345")
    st = host.stat(Path(f"{ROOT}/f.txt"))
    assert st is not None and st.is_file and not st.is_dir and st.size == 5


def test_stat_of_a_missing_path_is_none_not_an_error(host: SshHost) -> None:
    assert host.stat(Path(f"{ROOT}/nope")) is None


def test_stat_of_a_broken_symlink_is_none(host: SshHost) -> None:
    """stat follows the link, and there is nothing at the end of this one."""
    FakeSSHClient.fs.links[f"{ROOT}/dangling"] = f"{ROOT}/not-there"
    assert host.stat(Path(f"{ROOT}/dangling")) is None


def test_realpath_strict_needs_the_path_to_exist(host: SshHost) -> None:
    with pytest.raises(OSError):
        host.realpath(Path(f"{ROOT}/nope"), strict=True)


def test_realpath_tolerates_a_tail_that_is_not_there_yet(host: SshHost) -> None:
    """Path.resolve(strict=False)'s answer, which resolve_write depends on."""
    FakeSSHClient.fs.add_dir(ROOT)
    resolved = host.realpath(Path(f"{ROOT}/new/deep/file.txt"))
    assert resolved.as_posix() == f"{ROOT}/new/deep/file.txt"


def test_realpath_resolves_a_symlinked_ancestor(host: SshHost) -> None:
    FakeSSHClient.fs.add_dir("/srv/real")
    FakeSSHClient.fs.links[f"{ROOT}/alias"] = "/srv/real"
    assert host.realpath(Path(f"{ROOT}/alias/new.txt")).as_posix() == "/srv/real/new.txt"


def test_probe_os_names_the_remote_kernel_for_the_bootstrap(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(output="Linux\n")]
    assert host.probe_os() == "Linux (ssh)"
    assert host.os_name == "Linux (ssh)"


def test_probe_os_fails_loudly_when_the_box_cannot_answer(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(exit_code=127, output="")]
    with pytest.raises(SshError):
        host.probe_os()


def test_a_probe_runs_through_a_login_shell(host: SshHost) -> None:
    """``printenv`` must report the LOGIN shell's view; a bare exec would not."""
    FakeSSHClient.scripts = [FakeCommandScript(output="HOME=/home/dev\n")]
    code, out = host.probe_command("printenv")
    assert (code, out) == (0, "HOME=/home/dev\n")
    assert FakeSSHClient.instances[0].commands[0] == "bash -lc printenv"
    assert FakeSSHClient.instances[0].channels[0].combined is True


def test_a_probe_over_a_dead_link_answers_rather_than_raising(host: SshHost) -> None:
    """A non-fatal connect step carries on with an empty answer (ssh-connect.md 2)."""
    FakeSSHClient.instances[0].broken = True
    code, out = host.probe_command("printenv")
    assert code == CONNECTION_LOST_EXIT
    assert "connection lost to dev@box" in out
    assert not host.connected


def test_a_probes_channel_is_closed_when_it_is_done(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(output="Linux\n")]
    host.probe_command("uname -s")
    assert FakeSSHClient.instances[0].channels[0].closed


# -- the reconnect state machine ----------------------------------------------


def test_the_host_marks_itself_dead_and_redials_on_the_next_operation(
    host: SshHost, tmp_path: Path
) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(breaks=True)]
    host.probe_command("uname -s")
    assert not host.connected  # DEAD
    assert host.reconnects == 0  # ...but nothing has been re-dialed yet

    FakeSSHClient.scripts = []
    FakeSSHClient.fs.add_file(f"{ROOT}/a.txt", "back")
    assert host.read_bytes(Path(f"{ROOT}/a.txt")) == b"back"  # LIVE again
    assert host.reconnects == 1
    assert len(FakeSSHClient.instances) == 2  # a second, freshly authenticated client


def test_a_dead_link_under_an_sftp_call_becomes_a_connection_error(host: SshHost) -> None:
    FakeSSHClient.instances[0].broken = True
    with pytest.raises(OSError) as caught:
        host.read_bytes(Path(f"{ROOT}/a.txt"))
    assert "connection lost to dev@box" in str(caught.value)
    assert not host.connected


# -- authentication ------------------------------------------------------------


def test_agent_and_keys_are_tried_before_anything_is_asked(tmp_path: Path) -> None:
    asked: list[str] = []
    host = make_host(tmp_path, password_prompt=lambda prompt: asked.append(prompt) or "pw")
    host.connect()
    assert asked == []
    first = FakeSSHClient.connects[0]
    assert first["allow_agent"] is True and first["look_for_keys"] is True
    assert "password" not in first


def test_a_password_is_asked_for_only_after_keys_are_refused(tmp_path: Path) -> None:
    FakeSSHClient.connect_errors = [paramiko.AuthenticationException("no"), None]
    asked: list[str] = []

    def prompt(text: str) -> str:
        asked.append(text)
        return "hunter2"

    host = make_host(tmp_path, password_prompt=prompt)
    host.connect()

    assert asked == ["password for dev@box: "]
    second = FakeSSHClient.connects[1]
    assert second["password"] == "hunter2"
    assert second["allow_agent"] is False and second["look_for_keys"] is False


def test_the_password_that_worked_is_reused_on_a_redial(tmp_path: Path) -> None:
    """A reconnect happens under the TUI, where no prompt could be shown."""
    FakeSSHClient.connect_errors = [paramiko.AuthenticationException("no"), None]
    asked: list[str] = []
    host = make_host(tmp_path, password_prompt=lambda text: asked.append(text) or "hunter2")
    host.connect()
    host.mark_dead()

    FakeSSHClient.connect_errors = [paramiko.AuthenticationException("no"), None]
    FakeSSHClient.fs.add_file(f"{ROOT}/a.txt", "x")
    host.read_bytes(Path(f"{ROOT}/a.txt"))

    assert len(asked) == 1  # asked once, at launch; never again
    assert FakeSSHClient.connects[-1]["password"] == "hunter2"


def test_authentication_failure_is_an_ssherror_naming_the_target(tmp_path: Path) -> None:
    FakeSSHClient.connect_errors = [paramiko.AuthenticationException("nope")] * 4
    host = make_host(tmp_path, password_prompt=lambda text: "wrong")
    with pytest.raises(SshError) as caught:
        host.connect()
    assert "authentication failed for dev@box" in str(caught.value)


def test_a_wrong_password_is_asked_for_exactly_three_times(tmp_path: Path) -> None:
    """``_PASSWORD_ATTEMPTS``, pinned where it is enforced. No caller may extend
    it: a UI-level retry loop would call connect() from scratch and double-count
    ``reconnects`` (docs/design/ui-briefs/ssh-connect.md §2), and the GUI's
    dialog says "attempt n of 3" from the copy in hosts/connect.py."""
    FakeSSHClient.connect_errors = [paramiko.AuthenticationException("no")] * 8
    asked: list[str] = []
    host = make_host(tmp_path, password_prompt=lambda text: asked.append(text) or "wrong")
    with pytest.raises(SshError):
        host.connect()
    assert len(asked) == PASSWORD_ATTEMPTS == 3


def test_a_declined_password_prompt_stops_asking(tmp_path: Path) -> None:
    FakeSSHClient.connect_errors = [paramiko.AuthenticationException("no")]
    asked: list[str] = []
    host = make_host(tmp_path, password_prompt=lambda text: asked.append(text) or None)
    with pytest.raises(SshError):
        host.connect()
    assert len(asked) == 1  # one refusal ends it; no three-strikes ritual


def test_an_unreachable_box_is_an_ssherror_not_a_socket_error(tmp_path: Path) -> None:
    FakeSSHClient.connect_errors = [OSError("no route to host")]
    host = make_host(tmp_path)
    with pytest.raises(SshError) as caught:
        host.connect()
    assert "cannot reach dev@box" in str(caught.value)


# -- ssh config and host keys --------------------------------------------------


def test_an_ssh_config_alias_supplies_host_user_and_port(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("Host box\n  HostName 10.0.0.7\n  User pi\n  Port 2222\n", encoding="utf-8")
    host = SshHost("box", ssh_config_path=config, known_hosts_path=tmp_path / "kh")
    host.connect()
    assert host.target == "pi@10.0.0.7:2222"
    assert FakeSSHClient.connects[0]["hostname"] == "10.0.0.7"
    assert FakeSSHClient.connects[0]["port"] == 2222
    assert FakeSSHClient.connects[0]["username"] == "pi"


def test_explicit_user_and_port_beat_the_ssh_config(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("Host box\n  HostName 10.0.0.7\n  User pi\n  Port 2222\n", encoding="utf-8")
    host = SshHost(
        "box", user="dev", port=22, ssh_config_path=config, known_hosts_path=tmp_path / "kh"
    )
    host.connect()
    assert FakeSSHClient.connects[0]["username"] == "dev"
    assert FakeSSHClient.connects[0]["port"] == 22


def test_known_hosts_is_read_and_an_unknown_key_is_refused_without_a_prompt(
    tmp_path: Path,
) -> None:
    host = make_host(tmp_path)
    host.connect()
    assert str(tmp_path / "no-known-hosts") in FakeSSHClient.host_keys_seen
    policy = FakeSSHClient.instances[0].policy
    key = paramiko.RSAKey.generate(2048)
    with pytest.raises(SshError):  # no callback = no trust: never auto-add
        policy.missing_host_key(FakeSSHClient.instances[0], "box", key)


def test_an_unknown_host_key_is_offered_with_its_fingerprint(tmp_path: Path) -> None:
    seen: list[tuple[str, str, str]] = []
    host = make_host(tmp_path, host_key_prompt=lambda *args: bool(seen.append(args)) or True)
    host.connect()
    key = paramiko.RSAKey.generate(2048)

    class Recorder:
        def __init__(self) -> None:
            self.added: list[tuple] = []

        def get_host_keys(self) -> Recorder:
            return self

        def add(self, hostname: str, keytype: str, key: object) -> None:
            self.added.append((hostname, keytype, key))

    recorder = Recorder()
    FakeSSHClient.instances[0].policy.missing_host_key(recorder, "box", key)

    assert seen[0][0] == "box"
    assert seen[0][2].startswith("SHA256:")
    assert recorder.added and recorder.added[0][0] == "box"


def test_a_rejected_host_key_stops_the_connection(tmp_path: Path) -> None:
    host = make_host(tmp_path, host_key_prompt=lambda *args: False)
    host.connect()
    key = paramiko.RSAKey.generate(2048)
    with pytest.raises(SshError):
        FakeSSHClient.instances[0].policy.missing_host_key(FakeSSHClient.instances[0], "box", key)


def test_the_transport_gets_a_keepalive(host: SshHost) -> None:
    assert FakeSSHClient.instances[0].get_transport().keepalive == 30


def test_home_dir_is_where_sftp_starts(host: SshHost) -> None:
    """The remote user's skill folders hang off this, not the operator's ~."""
    FakeSSHClient.fs.add_dir("/home/dev")
    FakeSSHClient.fs.links["."] = "/home/dev"
    assert host.home_dir().as_posix() == "/home/dev"


def test_home_dir_falls_back_instead_of_failing_the_launch(host: SshHost) -> None:
    FakeSSHClient.instances[0].broken = True
    assert host.home_dir().as_posix() == "/home/dev"
