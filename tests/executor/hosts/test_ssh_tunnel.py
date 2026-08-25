"""``SshHost.open_tunnel``: a forwarded port, with real sockets and no network.

The channel is fake (``FakeTcpChannel``, a blocking byte pipe standing in for
``direct-tcpip``), but the LOCAL half is the real thing: a real listener on
127.0.0.1, a real client socket, real pump threads carrying bytes between them.
That split is deliberate - what can go wrong here is threading and socket
lifetime, and a fake socket would test none of it. Nothing in this module
reaches beyond loopback, so it is not a ``real_ssh`` test.

What is pinned: the tunnel listens somewhere a monitor client can dial, bytes
cross in both directions, EOF on the channel ends the local connection, exactly
ONE local connection is ever served, ``close`` is idempotent and leaves no
thread behind, and the two open failures are told apart - a destination that
refuses keeps the SSH connection, a dead transport does not.

The two tests that assert a REFUSED connect take ~2s each on Windows and there
is nothing to tune: Winsock retries the SYN before reporting ECONNREFUSED, even
on loopback. It is the platform, not the tunnel.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import paramiko
import pytest

from agentclip.executor.hosts.ssh import SshHost, Tunnel

from .fake_paramiko import FakeSSHClient, FakeTcpChannel

DEST_HOST = "10.0.0.9"
DEST_PORT = 4771
HELLO = b'{"type":"hello"}\n'
ACK = b'{"type":"hello_ack"}\n'


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch: pytest.MonkeyPatch) -> type[FakeSSHClient]:
    """Every SSHClient in this module is the fake one; no test dials anything."""
    FakeSSHClient.reset()
    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    return FakeSSHClient


@pytest.fixture
def host(tmp_path: Path) -> SshHost:
    h = SshHost(
        "box",
        user="dev",
        ssh_config_path=tmp_path / "no-ssh-config",
        known_hosts_path=tmp_path / "no-known-hosts",
    )
    h.connect()
    return h


@pytest.fixture
def cleanup() -> Iterator[list]:
    """Everything a test opened, closed even when the test failed mid-way."""
    opened: list = []
    yield opened
    for item in reversed(opened):
        with suppress(OSError):
            item.close()


def open_tunnel(host: SshHost, cleanup: list) -> tuple[Tunnel, FakeTcpChannel]:
    tunnel = host.open_tunnel(DEST_HOST, DEST_PORT)
    cleanup.append(tunnel)
    return tunnel, FakeSSHClient.instances[0].tcp_channels[0]


def dial(tunnel: Tunnel, cleanup: list) -> socket.socket:
    sock = socket.create_connection((tunnel.local_host, tunnel.local_port), timeout=5)
    sock.settimeout(5)
    cleanup.append(sock)
    return sock


def live_threads(tunnel: Tunnel) -> list[threading.Thread]:
    prefix = f"agentclip-tunnel-{tunnel.local_port}"
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def read_eof(sock: socket.socket) -> bool:
    """True when the far end hung up, however this platform spells it."""
    try:
        return sock.recv(4096) == b""
    except ConnectionResetError:
        return True


# -- opening -------------------------------------------------------------------


def test_a_tunnel_listens_on_a_loopback_port_of_its_own(host: SshHost, cleanup: list) -> None:
    tunnel, _chan = open_tunnel(host, cleanup)
    assert tunnel.local_host == "127.0.0.1"
    assert tunnel.local_port > 0
    dial(tunnel, cleanup)  # a monitor client's asyncio.open_connection, in miniature


def test_the_channel_is_opened_eagerly_at_the_destination_asked_for(
    host: SshHost, cleanup: list
) -> None:
    """Eager, so a dest that is not listening fails HERE - while the dialog is up."""
    tunnel, chan = open_tunnel(host, cleanup)
    assert chan.kind == "direct-tcpip"
    assert chan.dest == (DEST_HOST, DEST_PORT)
    assert chan.origin == ("127.0.0.1", 0)
    assert tunnel.dest == f"{DEST_HOST}:{DEST_PORT}"


# -- the pump ------------------------------------------------------------------


def test_bytes_from_the_local_client_reach_the_channel(host: SshHost, cleanup: list) -> None:
    tunnel, chan = open_tunnel(host, cleanup)
    sock = dial(tunnel, cleanup)
    sock.sendall(HELLO)
    assert chan.wait_for_sent(len(HELLO)) == HELLO


def test_bytes_from_the_channel_reach_the_local_client(host: SshHost, cleanup: list) -> None:
    tunnel, chan = open_tunnel(host, cleanup)
    sock = dial(tunnel, cleanup)
    chan.feed(ACK)
    assert sock.recv(4096) == ACK


def test_a_reply_split_across_two_chunks_arrives_whole(host: SshHost, cleanup: list) -> None:
    """The pump moves bytes, not frames: chunking stays the transport's business."""
    tunnel, chan = open_tunnel(host, cleanup)
    sock = dial(tunnel, cleanup)
    chan.feed(b"half-")
    chan.feed(b"a-line\n")
    received = b""
    while not received.endswith(b"\n"):
        received += sock.recv(4096)
    assert received == b"half-a-line\n"


def test_eof_from_the_channel_closes_the_local_client(host: SshHost, cleanup: list) -> None:
    tunnel, chan = open_tunnel(host, cleanup)
    sock = dial(tunnel, cleanup)
    chan.feed_eof()
    assert read_eof(sock)
    assert tunnel.closed


def test_the_local_client_hanging_up_closes_the_channel(host: SshHost, cleanup: list) -> None:
    tunnel, chan = open_tunnel(host, cleanup)
    sock = dial(tunnel, cleanup)
    sock.sendall(b"x")
    assert chan.wait_for_sent(1) == b"x"  # the pumps are up
    sock.close()
    tunnel.close()  # joins the pumps rather than polling for one to notice
    assert chan.closed


# -- one brain -----------------------------------------------------------------


def test_only_one_local_connection_is_ever_served(host: SshHost, cleanup: list) -> None:
    """A tunnel carries ONE monitor client; the listener is gone after it."""
    tunnel, chan = open_tunnel(host, cleanup)
    first = dial(tunnel, cleanup)
    first.sendall(b"x")
    assert chan.wait_for_sent(1) == b"x"  # the first connection is being served

    with pytest.raises(OSError):  # refused, rather than accepted and dropped
        socket.create_connection((tunnel.local_host, tunnel.local_port), timeout=5)


# -- closing -------------------------------------------------------------------


def test_close_is_idempotent_and_leaves_no_thread_behind(host: SshHost, cleanup: list) -> None:
    tunnel, chan = open_tunnel(host, cleanup)
    sock = dial(tunnel, cleanup)
    sock.sendall(b"x")
    assert chan.wait_for_sent(1) == b"x"

    tunnel.close()
    tunnel.close()  # twice: the second must neither raise nor un-close anything

    assert tunnel.closed
    assert chan.closed
    assert live_threads(tunnel) == []
    assert read_eof(sock)


def test_closing_a_tunnel_nobody_dialled_stops_its_accept_thread(
    host: SshHost, cleanup: list
) -> None:
    tunnel, chan = open_tunnel(host, cleanup)
    tunnel.close()
    assert live_threads(tunnel) == []
    assert chan.closed
    with pytest.raises(OSError):
        socket.create_connection((tunnel.local_host, tunnel.local_port), timeout=5)


def test_the_tunnel_is_a_context_manager(host: SshHost) -> None:
    with host.open_tunnel(DEST_HOST, DEST_PORT) as tunnel:
        assert tunnel.local_port > 0
    assert tunnel.closed
    assert live_threads(tunnel) == []


# -- the two open failures -----------------------------------------------------


def test_a_destination_that_refuses_is_an_error_that_keeps_the_link(host: SshHost) -> None:
    """A typo in a port field must not cost a re-dial and a fresh authentication."""
    FakeSSHClient.open_channel_error = paramiko.ChannelException(2, "Connect failed")
    with pytest.raises(OSError) as caught:
        host.open_tunnel(DEST_HOST, DEST_PORT)
    assert f"{DEST_HOST}:{DEST_PORT}" in str(caught.value)
    assert isinstance(caught.value, ConnectionRefusedError)
    assert host.connected  # the SSH connection is fine; the far side said no


def test_a_dead_transport_marks_the_host_dead(host: SshHost) -> None:
    FakeSSHClient.instances[0].broken = True
    with pytest.raises(OSError) as caught:
        host.open_tunnel(DEST_HOST, DEST_PORT)
    assert "connection lost to dev@box" in str(caught.value)
    assert not host.connected  # marked dead: the next operation re-dials
    assert host.last_error is not None
