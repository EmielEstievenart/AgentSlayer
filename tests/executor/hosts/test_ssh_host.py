"""SshHost against a paramiko that is a dictionary: no network, no server.

What is worth pinning here is everything that is NOT "paramiko does the right
thing": the POSIX-path discipline (this suite runs on Windows, where a Path
would otherwise spell a remote path with backslashes), the reconnect state
machine, the unknown-outcome answer a command in flight gets when the link
dies, the auth ladder's order, and the SFTP primitives' contract with the seam.
The real thing is exercised by tests/executor/hosts/test_ssh_real.py, which needs a
machine to talk to and is skipped without one.
"""

from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from agentclip.executor.hosts.connect import PASSWORD_ATTEMPTS
from agentclip.executor.hosts.ssh import (
    CONNECTION_LOST_EXIT,
    SshError,
    SshHost,
    wrap_command,
)

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


# -- the command wrapper -------------------------------------------------------


def test_wrapper_cds_records_the_pid_and_keeps_the_command_whole() -> None:
    line = wrap_command("pytest -q && ruff check", "/srv/app", "/tmp/p.pid")
    assert "cd /srv/app" in line
    assert "echo $$ >/tmp/p.pid" in line
    assert "pytest -q && ruff check" in line
    # A compound command must not be exec'd - exec would run only its head.
    assert "exec pytest" not in line
    assert line.startswith("setsid --wait true")  # probed, not assumed
    assert "exec bash -lc" in line  # ...with a fallback for a box without it


def test_wrapper_quotes_a_directory_with_spaces() -> None:
    line = wrap_command("ls", "/srv/my app", "/tmp/p.pid")
    assert "/srv/my app" in line
    assert "cd /srv/my app" not in line  # unquoted, that is a cd with two arguments


def test_spawn_sends_the_wrapped_command_from_the_workspace_root(host: SshHost) -> None:
    host.spawn("ls -la", Path(ROOT))
    sent = FakeSSHClient.instances[0].commands[0]
    assert f"cd {ROOT}" in sent
    assert "ls -la" in sent


def test_spawn_merges_stderr_into_the_one_stream(host: SshHost) -> None:
    host.spawn("ls", Path(ROOT))
    assert FakeSSHClient.instances[0].channels[0].combined is True


# -- POSIX paths, from a Windows PC --------------------------------------------


def test_a_path_built_by_pathlib_reaches_the_wire_as_posix(host: SshHost) -> None:
    """The remote root joined with pathlib is still a POSIX path on the wire."""
    FakeSSHClient.fs.add_file(f"{ROOT}/src/utils.py", "x")
    host.read_bytes(Path(ROOT) / "src" / "utils.py")
    assert FakeSSHClient.fs.reads[-1][0] == f"{ROOT}/src/utils.py"


def test_the_cwd_of_a_command_is_posix_too(host: SshHost) -> None:
    host.spawn("ls", Path(ROOT) / "src")
    assert f"cd {ROOT}/src" in FakeSSHClient.instances[0].commands[0]


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


def test_write_bytes_creates_the_missing_parents(host: SshHost) -> None:
    FakeSSHClient.fs.add_dir(ROOT)
    host.write_bytes(Path(f"{ROOT}/a/b/c.txt"), b"hi")
    assert FakeSSHClient.fs.made == [f"{ROOT}/a", f"{ROOT}/a/b"]  # shallowest first
    assert FakeSSHClient.fs.files[f"{ROOT}/a/b/c.txt"] == b"hi"


def test_write_bytes_append_extends(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/log.txt", "one\n")
    host.write_bytes(Path(f"{ROOT}/log.txt"), b"two\n", append=True)
    assert FakeSSHClient.fs.files[f"{ROOT}/log.txt"] == b"one\ntwo\n"


def test_write_bytes_overwrites(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/log.txt", "one\n")
    host.write_bytes(Path(f"{ROOT}/log.txt"), b"two\n")
    assert FakeSSHClient.fs.files[f"{ROOT}/log.txt"] == b"two\n"


def test_delete_removes_one_file(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/gone.txt", "x")
    host.delete(Path(f"{ROOT}/gone.txt"))
    assert f"{ROOT}/gone.txt" not in FakeSSHClient.fs.files


def test_rmdir_refuses_a_directory_with_something_in_it(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/pkg/a.txt", "x")
    with pytest.raises(OSError):
        host.rmdir(Path(f"{ROOT}/pkg"))


# -- metadata ------------------------------------------------------------------


def test_stat_of_a_file_reports_size(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/f.txt", "12345")
    st = host.stat(Path(f"{ROOT}/f.txt"))
    assert st is not None and st.is_file and not st.is_dir and st.size == 5


def test_stat_of_a_missing_path_is_none_not_an_error(host: SshHost) -> None:
    assert host.stat(Path(f"{ROOT}/nope")) is None
    assert host.lstat(Path(f"{ROOT}/nope")) is None


def test_stat_follows_a_symlink_and_lstat_does_not(host: SshHost) -> None:
    FakeSSHClient.fs.add_dir(f"{ROOT}/real")
    FakeSSHClient.fs.links[f"{ROOT}/link"] = f"{ROOT}/real"
    followed = host.stat(Path(f"{ROOT}/link"))
    assert followed is not None and followed.is_dir and followed.is_symlink
    itself = host.lstat(Path(f"{ROOT}/link"))
    assert itself is not None and itself.is_symlink
    assert not itself.is_dir and not itself.is_file


def test_stat_of_a_broken_symlink_is_none(host: SshHost) -> None:
    FakeSSHClient.fs.links[f"{ROOT}/dangling"] = f"{ROOT}/not-there"
    assert host.stat(Path(f"{ROOT}/dangling")) is None
    assert host.lstat(Path(f"{ROOT}/dangling")) is not None


def test_listdir_reports_types_and_sizes(host: SshHost) -> None:
    FakeSSHClient.fs.add_file(f"{ROOT}/a.txt", "abc")
    FakeSSHClient.fs.add_dir(f"{ROOT}/sub")
    entries = {e.name: e for e in host.listdir(Path(ROOT))}
    assert entries["a.txt"].is_file and entries["a.txt"].size == 3
    assert entries["sub"].is_dir and entries["sub"].size == 0


def test_listdir_entry_flags_follow_symlinks(host: SshHost) -> None:
    """As os.scandir has it: a link to a directory is a directory AND a link."""
    FakeSSHClient.fs.add_dir(f"{ROOT}/real")
    FakeSSHClient.fs.links[f"{ROOT}/link"] = f"{ROOT}/real"
    entries = {e.name: e for e in host.listdir(Path(ROOT))}
    assert entries["link"].is_dir and entries["link"].is_symlink


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


# -- commands ------------------------------------------------------------------


def test_wait_returns_the_exit_code_and_merged_output(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(exit_code=3, output="boom\n")]
    result = host.spawn("false", Path(ROOT)).wait(1.0)
    assert result is not None and result.exit_code == 3 and result.output == "boom\n"


def test_wait_answers_none_while_the_command_runs(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(hangs=True, output="working\n")]
    handle = host.spawn("sleep 60", Path(ROOT))
    assert handle.wait(0.05) is None
    # Output buffered during that slice survives into the drain, as the
    # ExecHandle contract promises.
    assert "working" in handle.drain(0.05)


def test_peek_hands_over_what_the_poll_loop_has_already_pumped(host: SshHost) -> None:
    """A remote command is watchable too - at the polling loop's resolution."""
    FakeSSHClient.scripts = [FakeCommandScript(hangs=True, output="halfway\n")]
    handle = host.spawn("make", Path(ROOT))
    assert handle.peek() == ""  # nothing has been pumped yet
    handle.wait(0.05)
    assert handle.peek() == "halfway\n"
    assert handle.peek() == "halfway\n"  # a snapshot, and repeatable


def test_peek_matches_the_finished_result(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(exit_code=0, output="all of it\n")]
    handle = host.spawn("make", Path(ROOT))
    result = handle.wait(1.0)
    assert result is not None and handle.peek() == result.output


def test_kill_sends_a_kill_to_the_whole_process_group(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(hangs=True)]
    handle = host.spawn("sleep 60", Path(ROOT))
    pidfile = FakeSSHClient.instances[0].commands[0].split("echo $$ >")[1].split(" ")[0]
    FakeSSHClient.fs.add_file(pidfile, "4321\n")

    handle.wait(0.01)
    handle.kill()

    assert any("kill -9 -- -4321" in c for c in FakeSSHClient.instances[0].commands)
    assert FakeSSHClient.instances[0].channels[0].closed


def test_kill_without_a_pidfile_still_closes_the_channel(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(hangs=True)]
    handle = host.spawn("sleep 60", Path(ROOT))
    handle.kill()  # the pidfile was never written: must not raise
    assert FakeSSHClient.instances[0].channels[0].closed


def test_probe_os_names_the_remote_kernel_for_the_bootstrap(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(output="Linux\n")]
    assert host.probe_os() == "Linux (ssh)"
    assert host.os_name == "Linux (ssh)"


def test_probe_os_fails_loudly_when_the_box_cannot_answer(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(exit_code=127, output="")]
    with pytest.raises(SshError):
        host.probe_os()


# -- the reconnect state machine ----------------------------------------------


def test_a_command_in_flight_when_the_link_dies_reports_an_unknown_outcome(
    host: SshHost,
) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(breaks=True)]
    result = host.spawn("make", Path(ROOT)).wait(1.0)
    assert result is not None
    assert result.exit_code == CONNECTION_LOST_EXIT
    assert "connection lost to dev@box" in result.output
    assert "outcome unknown" in result.output
    assert "may have completed or still be running" in result.output


def test_the_host_marks_itself_dead_and_redials_on_the_next_operation(
    host: SshHost, tmp_path: Path
) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(breaks=True)]
    host.spawn("make", Path(ROOT)).wait(1.0)
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


def test_a_finished_result_survives_being_asked_again(host: SshHost) -> None:
    FakeSSHClient.scripts = [FakeCommandScript(exit_code=0, output="done\n")]
    handle = host.spawn("true", Path(ROOT))
    first = handle.wait(1.0)
    assert handle.wait(1.0) == first


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
    config.write_text(
        "Host box\n  HostName 10.0.0.7\n  User pi\n  Port 2222\n", encoding="utf-8"
    )
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
