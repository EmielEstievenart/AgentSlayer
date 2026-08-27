"""§5's shared secret: the file it lives in, and the door it guards.

docs/design/ui-monitor.md §5 left auth on the monitor port open - "the handshake
has room for a secret and does not use it". This is the suite for using it, and
it is deliberately in two halves.

The **store** half is about a credential on disk: minted once, reused forever,
readable only by its owner where the OS has modes, and replaced only when
somebody asks for that in so many words.

The **door** half is about the handshake, over a real socket, against a real
:class:`~agentclip.driver.monitor.server.MonitorServer`. The property that
matters most there cannot be asserted from the client at all - that the refusal
comes BEFORE the ``hello_ack``, so an unauthorised dialler never learns the
monitor's ``server_id`` or which clipboard the machine has - so those tests
speak the wire by hand and read the frames in order.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from agentclip.driver.monitor.auth import (
    TOKEN_CHARS,
    TOKEN_FILE,
    load_or_create_token,
    new_token,
    regenerate_token,
    token_path,
    tokens_match,
)
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.protocol import UIMonitor
from agentclip.driver.monitor.remote import MonitorRefused, RemoteUIMonitor
from agentclip.driver.monitor.server import BindRefused, MonitorServer, serve
from agentclip.driver.monitor.wire import (
    LINE_LIMIT,
    decode_line,
    encode_line,
    frame_type,
    hello_frame,
    read_error,
)

from .conftest import TIMEOUT_S, await_until

Listen = Callable[..., Awaitable[MonitorServer]]


@pytest.fixture
async def listen() -> AsyncIterator[Listen]:
    """Servers on ephemeral loopback ports, with a token if the test wants one."""
    started: list[MonitorServer] = []

    async def start(monitor: UIMonitor, *, token: str | None = None) -> MonitorServer:
        server = await serve(monitor, port=0, token=token)
        started.append(server)
        return server

    yield start
    for server in reversed(started):
        await server.close()


@pytest.fixture
def monitor() -> FakeUIMonitor:
    """A monitor made of lists: this file is about the handshake, not the screen."""
    return FakeUIMonitor()


# == the store =================================================================


def test_a_token_is_minted_once_and_then_reused(tmp_path: Path) -> None:
    """The whole point of storing it: a monitor that minted a new secret on
    every launch would make the brain's saved connection wrong after each
    reboot, which is the one thing an operator does without thinking."""
    first = load_or_create_token(tmp_path)
    assert len(first) == TOKEN_CHARS
    assert set(first) <= set("0123456789abcdef")
    assert load_or_create_token(tmp_path) == first
    assert token_path(tmp_path).name == TOKEN_FILE
    assert token_path(tmp_path).read_text(encoding="utf-8").strip() == first


def test_the_directory_is_created_on_first_use(tmp_path: Path) -> None:
    nested = tmp_path / "nowhere" / "monitor"
    token = load_or_create_token(nested)
    assert token_path(nested).read_text(encoding="utf-8").strip() == token


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX modes")
def test_the_token_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    load_or_create_token(tmp_path)
    mode = stat.S_IMODE(os.stat(token_path(tmp_path)).st_mode)
    assert mode & 0o077 == 0, f"the token file is {mode:o}, which lets others read it"


def test_an_empty_token_file_is_replaced_rather_than_trusted(tmp_path: Path) -> None:
    """An empty secret that compared equal to an empty ``"token": ""`` would be
    an open port that looks authenticated."""
    path = token_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n", encoding="utf-8")
    token = load_or_create_token(tmp_path)
    assert len(token) == TOKEN_CHARS
    assert load_or_create_token(tmp_path) == token


def test_regenerating_replaces_the_stored_token(tmp_path: Path) -> None:
    first = load_or_create_token(tmp_path)
    second = regenerate_token(tmp_path)
    assert second != first
    assert load_or_create_token(tmp_path) == second


def test_tokens_match_treats_no_token_as_no_gate() -> None:
    """A server with no token accepts anything, a client's included: whether the
    port is guarded is the SERVER's decision, not the dialler's."""
    assert tokens_match(None, None)
    assert tokens_match(None, "anything")
    secret = new_token()
    assert tokens_match(secret, secret)
    assert not tokens_match(secret, secret.upper())
    assert not tokens_match(secret, None)
    assert not tokens_match(secret, "")


# == the door ==================================================================


async def test_the_right_token_attaches(monitor: FakeUIMonitor, listen: Listen) -> None:
    secret = new_token()
    server = await listen(monitor, token=secret)
    client = await RemoteUIMonitor.connect("127.0.0.1", server.port, token=secret)
    try:
        assert client.connected
        assert client.server_id == server.server_id
        assert server.attached
    finally:
        await client.close()


async def test_a_wrong_token_is_refused_as_unauthorized(
    monitor: FakeUIMonitor, listen: Listen
) -> None:
    server = await listen(monitor, token=new_token())
    with pytest.raises(MonitorRefused) as caught:
        await RemoteUIMonitor.connect("127.0.0.1", server.port, token=new_token())
    assert caught.value.kind == "unauthorized"
    # And the slot comes back: the one-brain slot is claimed before the
    # handshake (two simultaneous dials must not both win it), so a refused peer
    # holds it for exactly as long as the refusal takes to send.
    await await_until(lambda: not server.attached, "the refused peer to be dropped")


async def test_no_token_is_refused_by_a_server_that_has_one(
    monitor: FakeUIMonitor, listen: Listen
) -> None:
    server = await listen(monitor, token=new_token())
    with pytest.raises(MonitorRefused) as caught:
        await RemoteUIMonitor.connect("127.0.0.1", server.port)
    assert caught.value.kind == "unauthorized"


async def test_the_refusal_comes_before_the_ack(monitor: FakeUIMonitor, listen: Listen) -> None:
    """The property the client cannot see, and the reason the check sits where
    it does: an unauthorised peer learns neither the monitor's ``server_id`` nor
    which clipboard backend the machine has, because the ack is never sent."""
    server = await listen(monitor, token=new_token())
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port, limit=LINE_LIMIT)
    try:
        writer.write(encode_line(hello_frame("wrong")).encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), TIMEOUT_S)
        frame = decode_line(raw.decode("utf-8"))
        assert frame_type(frame) == "error"
        error = read_error(frame)
        # No id: it answers no call, exactly like the busy refusal (§2.8).
        assert (error.id, error.kind) == (None, "unauthorized")
        assert server.server_id not in error.message
        # And then the door: nothing else is ever sent on this connection.
        assert await asyncio.wait_for(reader.readline(), TIMEOUT_S) == b""
    finally:
        writer.close()


async def test_a_tokenless_server_on_loopback_accepts_anyone(
    monitor: FakeUIMonitor, listen: Listen
) -> None:
    """The no-token mode. On one machine anything that can reach 127.0.0.1 can
    already drive the mouse, so this is a real deployment rather than a hole."""
    server = await listen(monitor)
    assert server.token is None
    client = await RemoteUIMonitor.connect("127.0.0.1", server.port)
    try:
        assert client.connected
    finally:
        await client.close()
    # A brain that still carries a token for a monitor that stopped requiring
    # one connects too: the server decides whether the port is guarded.
    carrying = await RemoteUIMonitor.connect("127.0.0.1", server.port, token=new_token())
    try:
        assert carrying.connected
    finally:
        await carrying.close()


def test_binding_off_loopback_without_a_token_is_refused(monitor: FakeUIMonitor) -> None:
    """§5's other half, and the one it is not the operator's to decide by
    omission: off loopback the port is reachable by everything on the network."""
    with pytest.raises(BindRefused) as caught:
        MonitorServer(monitor, host="0.0.0.0", port=0, allow_remote=True)
    assert "token" in str(caught.value)


def test_binding_off_loopback_with_a_token_is_allowed(monitor: FakeUIMonitor) -> None:
    """Constructed, not started: this asserts the refusal does NOT fire, and
    actually listening on 0.0.0.0 is not something a test suite should do."""
    server = MonitorServer(monitor, host="0.0.0.0", port=0, allow_remote=True, token=new_token())
    assert server.token is not None


def test_the_status_line_has_an_address_and_an_attached_flag(
    monitor: FakeUIMonitor,
) -> None:
    server = MonitorServer(monitor, port=4321)
    assert server.address == "127.0.0.1:4321"
    assert server.attached is False
    assert server.token is None
