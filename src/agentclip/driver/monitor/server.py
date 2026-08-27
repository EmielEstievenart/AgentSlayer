"""The monitor half of the RPC: a TCP listener in front of a ``LocalUIMonitor``.

docs/design/ui-monitor.md §6.5. This process runs ON the machine whose screen
shows the chat - a VM, or this PC in split mode - and it is a **standing**
process (§2.8): it keeps polling whether or not a brain is attached, it hosts
the calibration window, and it accepts a redial. That is the opposite of the
engine link (remote-executor.md §2.3, unchanged there), and it is why nothing
here shuts the monitor down when a connection ends.

Three rules the shape below exists to keep:

**One brain at a time.** A second connection is not queued and not multiplexed -
it is refused with an ``error`` frame naming the first peer and then closed.
Two brains would be two hands on one mouse.

**Never interleave a frame.** Every write goes through one lock and one
``_send``: a tick pushed from the poller thread must not land in the middle of a
``result`` line, or the reader on the far side gets two half-frames and neither.

**A slow verb blocks nothing.** Each ``call`` runs as its own task, so a
``hover_scan`` walking the cursor up the screen for two seconds does not hold up
the ``read_clipboard`` that came after it. The far side matches results by id,
which is what makes that safe.

The two hooks (``subscribe``, ``on_clip``) fire on the MONITOR'S OWN THREADS -
the poller and the clipboard watcher - so neither may touch a stream directly.
Both marshal onto the server's event loop with ``call_soon_threadsafe`` and drop
their payload into a slot the pusher task drains. Ticks use a single slot
(drop-to-latest, §8: ``observe()`` only ever wants the newest, so a backlog of
stale ticks is a backlog of things nobody will read); clips use a list, because
a clipboard capture is a fact the user created and losing one loses a reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import uuid
from typing import Any

from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.monitor.auth import tokens_match
from agentclip.driver.monitor.protocol import ClipHook, Tick, TickHook, UIMonitor
from agentclip.driver.monitor.wire import (
    CLOSE,
    LINE_LIMIT,
    CallFrame,
    WireError,
    clip_frame,
    decode_line,
    decode_params,
    encode_line,
    encode_result,
    error_frame,
    frame_type,
    hello_ack_frame,
    read_call,
    read_hello,
    result_frame,
    tick_frame,
)

_log = logging.getLogger(__name__)

#: The addresses that need no opt-in. §5: the monitor port is a channel to a
#: machine's mouse, keyboard and clipboard, so the default bind is loopback and
#: anything else is a decision somebody has to make out loud - twice, now: the
#: address by name (``allow_remote``) and a ``token`` to guard it with.
LOOPBACK: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")


class BindRefused(ValueError):
    """A bind this server will not make: off loopback without the opt-in, or
    off loopback without a token.

    Its own type so a launcher can turn it into the one sentence a user can act
    on, rather than pattern-matching a ValueError's text.
    """


def _peer_name(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return "an unknown address" if peer is None else str(peer)


class _Session:
    """One attached brain: its streams, its pushes, and its calls in flight."""

    def __init__(
        self,
        monitor: UIMonitor,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        peer: str,
        server_id: str,
        token: str | None = None,
    ) -> None:
        self._monitor = monitor
        self._reader = reader
        self._writer = writer
        self.peer = peer
        self._server_id = server_id
        # The secret this session's hello has to carry, or None for the
        # no-token mode. Held per session rather than read off the server on
        # the fly, so a token regenerated mid-connection cannot retroactively
        # unauthorise a brain that is already clicking.
        self._token = token
        self._loop = asyncio.get_running_loop()
        # One frame per write, never interleaved. Held across the drain as well
        # as the write, because a partially-flushed line is the same corruption
        # as an interleaved one.
        self._write_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._pending_tick: Tick | None = None
        self._pending_clips: list[str] = []
        self._pusher: asyncio.Task[None] | None = None
        self._calls: set[asyncio.Task[None]] = set()
        self._unsubscribe: list[Any] = []
        self._done = False

    # -- the connection's life -------------------------------------------------

    async def run(self) -> None:
        """Handshake, then read until the far side stops. Never raises out.

        Every exit runs the teardown, the refused handshake included: a peer
        told its version is wrong is owed the CLOSE as much as the sentence, or
        it sits on a readline that will never return.
        """
        try:
            if not await self._handshake():
                return
            self._attach()
            self._pusher = self._loop.create_task(self._push_loop())
            await self._read_loop()
        finally:
            await self.teardown()

    async def _handshake(self) -> bool:
        """Read ``hello``, check the token, answer ``hello_ack``. False if the
        peer is refused.

        Three gates, in this order, and the order is the design. The version
        gate is hard and comes first: every frame after this one is decoded as
        v2, and reading a v3 call as v2 is exactly the guess a protocol
        boundary must not make - and a peer that cannot even be parsed is told
        to upgrade rather than told its token is wrong. The TOKEN gate comes
        next and **before the ack**: an unauthorised peer must not learn the
        monitor's ``server_id`` or which clipboard backend this machine has,
        which is exactly what the ack would tell it.
        """
        line = await self._readline()
        if line is None:
            return False
        try:
            hello = read_hello(decode_line(line))
        except WireError as exc:
            # WireVersionError's message already names BOTH installs, which is
            # the half a human can act on; it is sent verbatim.
            await self._send(error_frame(None, "bad_request", str(exc)))
            return False
        if not tokens_match(self._token, hello.token):
            # One sentence for "no token" and for "wrong token", deliberately:
            # they are the same refusal, and a message that told them apart
            # would tell a dialler which half of the guess to fix.
            await self._send(
                error_frame(
                    None,
                    "unauthorized",
                    "this monitor requires a token and the hello did not carry"
                    " a matching one - the monitor prints its token on startup"
                    " (--token / --token-file on that machine)",
                )
            )
            return False
        await self._send(hello_ack_frame(self._server_id, self._monitor.clipboard_kind))
        return True

    def _attach(self) -> None:
        on_tick: TickHook = self._tick_arrived
        on_clip: ClipHook = self._clip_arrived
        self._unsubscribe.append(self._monitor.subscribe(on_tick))
        self._unsubscribe.append(self._monitor.on_clip(on_clip))

    async def teardown(self) -> None:
        """Detach from the monitor and drop the socket - and NOTHING else.

        The monitor keeps polling (§2.8). What goes is what belonged to this
        brain: the two subscriptions, and the clipboard watcher, which is a
        session-scoped thing the brain turned on and would otherwise keep
        capturing into a fan-out with nobody on the far end.
        """
        if self._done:
            return
        self._done = True
        for drop in self._unsubscribe:
            drop()
        self._unsubscribe.clear()
        try:
            self._monitor.watch_clipboard(False)
        except Exception:  # noqa: BLE001 - a teardown never fails a teardown
            _log.exception("stopping the clipboard watcher raised")
        if self._pusher is not None:
            self._pusher.cancel()
        for task in tuple(self._calls):
            task.cancel()
        self._writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await self._writer.wait_closed()

    # -- reading ---------------------------------------------------------------

    async def _readline(self) -> str | None:
        try:
            raw = await self._reader.readline()
        except (OSError, ConnectionError, asyncio.LimitOverrunError, ValueError):
            return None
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace")

    async def _read_loop(self) -> None:
        while True:
            line = await self._readline()
            if line is None:
                return
            if not line.strip():
                continue
            try:
                frame = decode_line(line)
                kind = frame_type(frame)
                if kind != "call":
                    raise WireError(f"a server reads only 'call' frames, got {kind!r}")
                call = read_call(frame)
            except WireError as exc:
                await self._send(error_frame(None, "bad_request", str(exc)))
                continue
            # Its own task: the id on the frame is what pairs the answer back
            # up, so nothing is owed to arrival order and a slow verb owes the
            # next call nothing at all.
            task = self._loop.create_task(self._run_call(call))
            self._calls.add(task)
            task.add_done_callback(self._calls.discard)

    async def _run_call(self, call: CallFrame) -> None:
        try:
            kwargs = decode_params(call.verb, call.params)
        except WireError as exc:
            await self._send(error_frame(call.id, "bad_request", str(exc)))
            return
        if call.verb == CLOSE:
            # An orderly goodbye, NOT a shutdown. ``UIMonitor.close`` means "stop
            # every thread for good" to the object that owns the threads, and the
            # monitor here is owned by this process rather than by the brain
            # (§2.8) - so what closes is the link. The read loop sees EOF next.
            await self._send(result_frame(call.id, None))
            self._writer.close()
            return
        try:
            value = getattr(self._monitor, call.verb)(**kwargs)
            if inspect.isawaitable(value):
                value = await value
            payload = encode_result(call.verb, value)
        except asyncio.CancelledError:
            raise
        except ClipboardUnavailable as exc:
            await self._send(error_frame(call.id, "clipboard_unavailable", str(exc)))
            return
        except WireError as exc:
            await self._send(error_frame(call.id, "bad_request", str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - a bad verb is not a dead link
            _log.exception("monitor verb %s raised", call.verb)
            await self._send(error_frame(call.id, "internal", f"{type(exc).__name__}: {exc}"))
            return
        await self._send(result_frame(call.id, payload))

    # -- pushing ---------------------------------------------------------------

    def _tick_arrived(self, tick: Tick) -> None:
        """On the POLLER thread. Hand it to the loop and return immediately."""
        self._marshal(self._queue_tick, tick)

    def _clip_arrived(self, text: str) -> None:
        """On the WATCHER thread. Same contract."""
        self._marshal(self._queue_clip, text)

    def _marshal(self, fn: Any, payload: Any) -> None:
        # A RuntimeError here is the loop already gone (the server is shutting
        # down and a poll thread got one more tick out). Dropping it is the
        # whole answer: nothing is buffered and nothing is replayed (§2.9).
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(fn, payload)

    def _queue_tick(self, tick: Tick) -> None:
        # Drop-to-latest, and the drop IS the policy: an observe() waiting on
        # the far side wants the newest tick, and a queue would hand it a stale
        # one first and call that an observation.
        self._pending_tick = tick
        self._wake.set()

    def _queue_clip(self, text: str) -> None:
        self._pending_clips.append(text)
        self._wake.set()

    async def _push_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            clips, self._pending_clips = self._pending_clips, []
            tick, self._pending_tick = self._pending_tick, None
            for text in clips:
                await self._send(clip_frame(text))
            if tick is not None:
                await self._send(tick_frame(tick))

    # -- writing ---------------------------------------------------------------

    async def _send(self, frame: dict[str, Any]) -> None:
        try:
            line = encode_line(frame)
        except WireError:
            _log.exception("a frame this server built is not encodable")
            return
        async with self._write_lock:
            if self._writer.is_closing():
                return
            try:
                self._writer.write(line.encode("utf-8"))
                await self._writer.drain()
            except (OSError, ConnectionError):
                # The brain went away mid-frame. The read loop is about to see
                # EOF and run the teardown; there is nothing else to say.
                self._writer.close()


class MonitorServer:
    """A standing listener in front of one ``UIMonitor``.

    Long-lived by construction (§2.8): the monitor is built and configured
    before this exists and outlives every connection. ``close()`` is the only
    thing that ends the listener, and it still does not close the monitor - the
    process that built one closes it.
    """

    def __init__(
        self,
        monitor: UIMonitor,
        *,
        host: str = LOOPBACK[0],
        port: int,
        allow_remote: bool = False,
        token: str | None = None,
    ) -> None:
        if host not in LOOPBACK and not allow_remote:
            raise BindRefused(
                f"refusing to listen on {host!r}: the monitor port is a channel"
                " to this machine's mouse, keyboard and clipboard, so a"
                " non-loopback bind has to be asked for by name"
                " (--bind, which sets allow_remote)"
            )
        if host not in LOOPBACK and token is None:
            # §5's other half. Loopback with no token is a decision about ONE
            # machine - anything that can reach 127.0.0.1 can already drive the
            # mouse. Off loopback it is a decision about everything else on the
            # network, and that one is not the operator's to make by omission.
            raise BindRefused(
                f"refusing to listen on {host!r} without a token: off loopback"
                " the monitor port is reachable by anything on that network,"
                " and it is a channel to this machine's mouse, keyboard and"
                " clipboard - a token is required (--no-token is loopback only)"
            )
        self._monitor = monitor
        self._host = host
        self._port = port
        self._token = token
        self._server: asyncio.Server | None = None
        self._session: _Session | None = None
        # The PROCESS's id, not a session's: a redial that comes back with a
        # different one reached a monitor that has been restarted, and every
        # generation the brain remembers is meaningless.
        self.server_id = str(uuid.uuid4())

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Bind and begin accepting. Idempotent."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._on_connect, self._host, self._port, limit=LINE_LIMIT
        )

    async def close(self) -> None:
        """Stop listening and drop the attached brain. Idempotent.

        Does NOT close the monitor: this object borrowed one.
        """
        session, self._session = self._session, None
        if session is not None:
            await session.teardown()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(OSError, ConnectionError):
                await server.wait_closed()

    # -- what a caller can ask -------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def token(self) -> str | None:
        """The secret the NEXT hello must carry, or None for the no-token mode.

        Settable, and only because there is now a surface that can keep the
        sentence on the operator's screen true: the Monitor UI's Serve panel
        shows the token and regenerates it in place (ui-monitor.md 9.1), so
        the value here and the value the panel prints are written together.
        A terminal-only monitor never assigns it - it prints what
        :func:`~agentclip.driver.monitor.auth.load_or_create_token` gave it,
        once, and that string stays true for the process's life.

        Assigning it does NOT drop the attached brain, and that is the design's
        word rather than an accident of the implementation: a connection that
        already shook hands was already authorised, and :class:`_Session` holds
        its own copy of the secret it was admitted under. What changes is what
        the next ``hello`` has to carry.
        """
        return self._token

    @token.setter
    def token(self, token: str | None) -> None:
        self._token = token

    @property
    def port(self) -> int:
        """The port actually bound - which is the point of asking for 0."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def sockets(self) -> tuple[Any, ...]:
        """The listening sockets, for a caller that wants the address family too."""
        if self._server is None:
            return ()
        return tuple(self._server.sockets)

    @property
    def peer(self) -> str | None:
        """The attached brain's address, or None. What the refusal names."""
        return None if self._session is None else self._session.peer

    @property
    def attached(self) -> bool:
        """Is a brain on the line right now? The status line's first word."""
        return self._session is not None

    @property
    def address(self) -> str:
        """``host:port``, with the port actually bound.

        The one string an operator has to carry to the other machine, so it is
        built here rather than in each caller that wants to print it - and it
        reads the LIVE port, which is the whole point of being allowed to ask
        for 0.
        """
        return f"{self._host}:{self.port}"

    async def disconnect(self) -> bool:
        """Drop the attached brain, keep listening. True if there was one.

        The kick, and the only way one brain's grip on a monitor is broken from
        this side: a session that went away without closing its socket holds the
        one slot until TCP notices, and the operator watching the monitor is the
        one who can see that. The far side reads it as any other link loss
        (§2.9) - which is the point, because there is no other kind.
        """
        session, self._session = self._session, None
        if session is None:
            return False
        await session.teardown()
        return True

    # -- accepting -------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._session is not None:
            # Checked and claimed without an await in between, so two
            # simultaneous dials cannot both win the slot.
            await self._refuse(writer, self._session.peer)
            return
        session = _Session(
            self._monitor,
            reader,
            writer,
            peer=_peer_name(writer),
            server_id=self.server_id,
            token=self._token,
        )
        self._session = session
        try:
            await session.run()
        finally:
            if self._session is session:
                self._session = None

    async def _refuse(self, writer: asyncio.StreamWriter, held_by: str) -> None:
        """The second brain's answer: who has the monitor, then the door.

        Named rather than a bare "busy", because the operator's next question is
        always "which of my two windows is holding it" and the address is the
        only thing that answers it.
        """
        frame = error_frame(
            None,
            "busy",
            f"this monitor already has a brain attached from {held_by} - one brain at a time",
        )
        with contextlib.suppress(OSError, ConnectionError):
            writer.write(encode_line(frame).encode("utf-8"))
            await writer.drain()
        writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await writer.wait_closed()


async def serve(
    monitor: UIMonitor,
    *,
    host: str = LOOPBACK[0],
    port: int,
    allow_remote: bool = False,
    token: str | None = None,
) -> MonitorServer:
    """Build a :class:`MonitorServer` over ``monitor`` and start it.

    ``port=0`` binds an ephemeral one; read it back off :attr:`MonitorServer.port`.
    ``allow_remote`` is §5's opt-in and the only way past loopback; ``token``
    is the secret every hello must carry, and is REQUIRED once the bind leaves
    loopback.
    """
    server = MonitorServer(monitor, host=host, port=port, allow_remote=allow_remote, token=token)
    await server.start()
    return server
