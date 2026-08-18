"""The SSH link channel: framing over a channel, and a launch that failed.

``SshHost.open_link_channel`` is the transport increment 3 adds
(docs/design/remote-executor.md §2.12), and what is worth pinning here is
everything the wire protocol above it ASSUMES and cannot check for itself: that
one ``"\\n"``-terminated line is one frame no matter how the transport chops the
bytes up, that a multibyte character surviving a chunk boundary is still one
character, that EOF is an empty string rather than a hang, that stderr is drained
(an unread one wedges the remote writer) and bounded, and that a launch which
never produced a handshake can still say why.

The real thing is exercised by tests/executor/hosts/test_link_real.py, which
needs a machine with ``agentclip-engine`` installed on it and is skipped without
one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import paramiko
import pytest

from agentclip import cli
from agentclip.config import RemoteTarget, load_config
from agentclip.engine.link.wire import EngineLinkError
from agentclip.executor.hosts.connect import ConnectedRemote
from agentclip.executor.hosts.ssh import LinkChannel, SshHost

from .fake_paramiko import FakeChannel, FakeCommandScript, FakeSSHClient

ROOT = "/home/dev/project"


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch: pytest.MonkeyPatch) -> type[FakeSSHClient]:
    """Every SSHClient in this module is the fake one; no test dials anything."""
    FakeSSHClient.reset()
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    return FakeSSHClient


def make_channel(
    *,
    chunks: list[bytes] | None = None,
    stderr: list[bytes] | None = None,
    hangs: bool = True,
    exit_code: int = 0,
) -> tuple[LinkChannel, FakeChannel]:
    """A LinkChannel over a scripted channel, with no SshHost in the way."""
    script = FakeCommandScript(
        exit_code=exit_code,
        hangs=hangs,
        chunks=list(chunks or []),
        stderr_chunks=list(stderr or []),
    )
    chan = FakeChannel(script, FakeSSHClient())
    return LinkChannel(chan), chan  # type: ignore[arg-type]


def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Wait for the stderr drain THREAD to have got there. Never a timing claim."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


# -- the reader: one line is one frame, whatever the transport did -------------


def test_a_line_split_over_two_recvs_arrives_whole() -> None:
    link, _ = make_channel(chunks=[b'{"type":"hel', b'lo"}\n'])
    assert link.reader.readline() == '{"type":"hello"}\n'
    link.close()


def test_two_lines_in_one_recv_are_two_readlines() -> None:
    link, _ = make_channel(chunks=[b'{"a":1}\n{"b":2}\n'])
    assert link.reader.readline() == '{"a":1}\n'
    assert link.reader.readline() == '{"b":2}\n'
    assert link.reader.readline() == ""
    link.close()


def test_a_multibyte_character_split_across_a_chunk_survives() -> None:
    """Half a UTF-8 sequence is not a replacement character; it is a wait."""
    payload = '{"chat":"café ✓"}\n'.encode()
    link, _ = make_channel(chunks=[payload[:12], payload[12:]])
    assert link.reader.readline() == '{"chat":"café ✓"}\n'
    link.close()


def test_eof_answers_an_empty_string_rather_than_hanging() -> None:
    link, _ = make_channel(chunks=[])
    assert link.reader.readline() == ""
    assert link.reader.readline() == ""  # and stays EOF
    link.close()


def test_a_final_line_without_a_terminator_is_handed_over_once() -> None:
    link, _ = make_channel(chunks=[b"half a frame"])
    assert link.reader.readline() == "half a frame"
    assert link.reader.readline() == ""
    link.close()


def test_a_dead_transport_reads_as_eof() -> None:
    """The client turns EOF into one failed call; an exception type would not."""
    script = FakeCommandScript(breaks=True)
    chan = FakeChannel(script, FakeSSHClient())
    link = LinkChannel(chan)  # type: ignore[arg-type]
    assert link.reader.readline() == ""
    link.close()


# -- the writer ----------------------------------------------------------------


def test_the_writer_sends_the_utf8_of_what_was_written() -> None:
    link, chan = make_channel()
    link.writer.write('{"type":"hello","chat":"café"}\n')
    link.writer.flush()  # a no-op by contract: sendall already blocked
    assert bytes(chan.sent) == '{"type":"hello","chat":"café"}\n'.encode()
    link.close()


def test_writing_to_a_dead_channel_raises_an_oserror() -> None:
    """Which is what RemoteLinkClient already reads as "the link closed"."""
    script = FakeCommandScript(breaks=True)
    chan = FakeChannel(script, FakeSSHClient())
    link = LinkChannel(chan)  # type: ignore[arg-type]
    with pytest.raises(OSError):
        link.writer.write("x\n")
    link.close()


# -- stderr: drained, bounded, and the only evidence a failed launch leaves ----


def test_stderr_is_captured_off_the_protocol_stream() -> None:
    link, _ = make_channel(stderr=[b"bash: agentclip-engine: ", b"command not found\n"])
    assert until(lambda: "command not found" in link.stderr_tail())
    assert link.stderr_tail() == "bash: agentclip-engine: command not found\n"
    link.close()


def test_stderr_keeps_the_tail_and_not_the_flood() -> None:
    link, _ = make_channel(stderr=[b"a" * 6000, b"b" * 6000, b"END\n"])
    assert until(lambda: link.stderr_tail().endswith("END\n"))
    tail = link.stderr_tail()
    assert len(tail) == 8192  # the cap, not the 12004 bytes that were written
    assert tail.endswith("b" * 100 + "END\n")
    assert "a" not in tail[-6000:]  # the flood's head is what got dropped
    link.close()


# -- exit status and close -----------------------------------------------------


def test_exit_status_is_none_while_the_engine_runs_and_a_number_after() -> None:
    script = FakeCommandScript(hangs=True, exit_code=127)
    link = LinkChannel(FakeChannel(script, FakeSSHClient()))  # type: ignore[arg-type]
    assert link.exit_status() is None
    script.hangs = False  # the remote process exited
    assert link.exit_status() == 127
    link.close()


def test_close_shuts_the_channel_and_joins_the_drain_thread() -> None:
    link, chan = make_channel(stderr=[b"log line\n"])
    link.close()
    assert chan.closed is True
    assert link._drain.is_alive() is False
    link.close()  # idempotent: closing a dead channel must never raise


# -- opening one off a host ----------------------------------------------------


def make_host(tmp_path: Path) -> SshHost:
    host = SshHost(
        "box",
        user="dev",
        ssh_config_path=tmp_path / "no-ssh-config",
        known_hosts_path=tmp_path / "no-known-hosts",
    )
    host.connect()
    return host


def test_open_link_channel_runs_the_command_bare(tmp_path: Path) -> None:
    """No wrapper, no setsid: the engine must die with the channel (§2.3)."""
    host = make_host(tmp_path)
    link = host.open_link_channel("agentclip-engine --project /srv/app")
    sent = FakeSSHClient.instances[0].commands[0]
    assert sent == "agentclip-engine --project /srv/app"
    assert "setsid" not in sent
    assert "trap" not in sent  # nor the pidfile bookkeeping a tool call gets
    link.close()


def test_open_link_channel_keeps_stderr_off_the_protocol_stream(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    link = host.open_link_channel("agentclip-engine --project /srv/app")
    assert FakeSSHClient.instances[0].channels[0].combined is False
    link.close()


# -- the factory: a target with no engine on it --------------------------------


def connected(host: SshHost, tmp_path: Path) -> ConnectedRemote:
    """What a connect flow hands back, with the parts this path does not read
    filled in from a config that reads nothing of the developer's own."""
    return ConnectedRemote(
        host=host,
        target=RemoteTarget(name="box", host="box", user="dev", root=ROOT),
        os_name="Linux (ssh)",
        project_root=Path(ROOT),
        home=Path("/home/dev"),
        data_root=tmp_path / "data",
        config=load_config(tmp_path, global_config_path=tmp_path / "no-global.toml"),
    )


def test_a_target_without_the_engine_says_how_to_install_it(tmp_path: Path) -> None:
    """The whole point of §2.12's classification, end to end over a fake link."""
    FakeSSHClient.scripts = [
        FakeCommandScript(
            exit_code=127,
            hangs=False,
            stderr_chunks=[b"bash: agentclip-engine: command not found\n"],
        )
    ]
    host = make_host(tmp_path)
    with pytest.raises(EngineLinkError) as caught:
        cli.make_remote_link_factory(connected(host, tmp_path))
    message = str(caught.value)
    assert "agentclip-engine is not installed on dev@box" in message
    assert "uv tool install agentclip" in message
    assert FakeSSHClient.instances[0].channels[0].closed is True


def test_the_engine_command_carries_the_remote_root(tmp_path: Path) -> None:
    """The launch line is built from the CONNECTED root, not a local path."""
    FakeSSHClient.scripts = [FakeCommandScript(exit_code=127, hangs=False)]
    host = make_host(tmp_path)
    with pytest.raises(EngineLinkError):
        cli.make_remote_link_factory(connected(host, tmp_path), service="chatgpt")
    sent = FakeSSHClient.instances[0].commands[0]
    assert sent == f"agentclip-engine --project {ROOT} --service chatgpt"
