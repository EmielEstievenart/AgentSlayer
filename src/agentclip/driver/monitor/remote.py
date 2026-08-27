"""The brain's half of the RPC: a ``UIMonitor`` that is a socket.

docs/design/ui-monitor.md §6.5. Everything a recipe asks the machine goes over
one TCP connection to :mod:`agentclip.driver.monitor.server`, and everything the
machine says back arrives on a **reader task this class owns from day one**.
That is the one structural difference from ``shell/app/remote_link.py`` and the
reason §2.7 says not to reuse it: the engine client reads frames only while a
call is in flight, so an unsolicited tick would have nowhere to land - and the
tick stream is what this link exists for.

The three shapes worth stating:

* **Queries stay local reads** (§2.1). :attr:`latest` and :attr:`generation` are
  fields updated by the reader task, not round trips, so the contract a recipe
  is written against does not change when the monitor moves to another machine.
  :meth:`observe` is a *wait*, not an ask: it parks until the next pushed tick
  whose ``seq`` is past the one current when it was called.
* **Every action is one round trip**, with a monotonically increasing id, and
  results are matched back by that id alone. Nothing is owed to arrival order,
  which is what lets the server run each call as its own task.
* **Link loss is loud and is not repaired here** (§2.9). EOF or a socket error
  fails every pending call with :class:`MonitorDisconnected`, raises it out of a
  parked :meth:`observe`, and fires the :meth:`on_disconnect` hooks once.
  Reconnecting is the caller's - a new :meth:`connect` - and re-deriving state
  from the screen after one is the brain's: nothing is buffered and nothing is
  replayed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.monitor.protocol import (
    ClipHook,
    ElementClick,
    Located,
    MonitorSpec,
    Tick,
    TickHook,
    UIMonitor,
)
from agentclip.driver.monitor.wire import (
    LINE_LIMIT,
    HelloAck,
    WireError,
    call_frame,
    decode_line,
    decode_result,
    encode_line,
    encode_params,
    frame_type,
    hello_frame,
    read_clip,
    read_error,
    read_hello_ack,
    read_result,
    read_tick,
)
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion

_log = logging.getLogger(__name__)


class MonitorLinkError(RuntimeError):
    """Anything that went wrong on the monitor link. The base of the two below."""


class MonitorDisconnected(MonitorLinkError):
    """The link is gone: EOF, a socket error, or a deliberate close.

    Raised by every call that was in flight, by a parked :meth:`observe`, and by
    every call made afterwards. Never retried here - a reconnect is a new
    :meth:`RemoteUIMonitor.connect`, and what the brain does with the gap is
    §2.9's business (park in ``DISCONNECTED``, re-derive from the screen).
    """


class MonitorRefused(MonitorLinkError):
    """The monitor answered the dial with a refusal instead of a ``hello_ack``.

    ``kind`` is the wire's error kind, and two of them mean this:
    ``"busy"`` - one brain at a time (§2.8), and the message names the address
    of the one that got there first - and ``"unauthorized"`` - the hello carried
    no token, or the wrong one (§5). Neither is worth a retry: one needs the
    other brain to let go, the other needs the operator to read the token off
    the monitor's terminal.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class MonitorCallError(MonitorLinkError):
    """One verb failed on the far side, and the link is fine.

    The catch-all for an ``error`` frame with an id on it: the call it names is
    over, nothing else is. ``ClipboardUnavailable`` is deliberately NOT one of
    these - it crosses as itself, because a delivery path catches it by type.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class _Waiter:
    """One parked :meth:`RemoteUIMonitor.observe`, and the tick it will take.

    ``armed`` is the newest ``seq`` this client had seen when the call was made,
    so only a LATER tick answers it. That is the same rule
    ``LocalUIMonitor.observe`` keeps and for the same reason: the bug it exists
    to stop is reading a tick from before the scroll that was just performed.
    """

    __slots__ = ("armed", "future")

    def __init__(self, armed: int, future: asyncio.Future[Tick]) -> None:
        self.armed = armed
        self.future = future


class RemoteUIMonitor:
    """A ``UIMonitor`` over TCP. Built by :meth:`connect`, never by hand."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ack: HelloAck,
        *,
        peer: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._ack = ack
        self._peer = peer
        self._loop = asyncio.get_running_loop()
        self._write_lock = asyncio.Lock()
        self._next_call_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._waiters: list[_Waiter] = []
        self._hooks: list[TickHook] = []
        self._clip_hooks: list[ClipHook] = []
        self._disconnect_hooks: list[Callable[[], None]] = []
        self._latest: Tick | None = None
        self._generation = 0
        self._connected = True
        self._closing = False
        self._notified = False
        self._task = self._loop.create_task(self._read_loop())

    # == connecting ============================================================

    @classmethod
    async def connect(
        cls, host: str, port: int, *, token: str | None = None
    ) -> RemoteUIMonitor:
        """Dial a monitor and complete the handshake.

        ``token`` is §5's shared secret, as the monitor printed it on the machine
        with the screen. ``None`` is a real value and the right one for a monitor
        started with ``--no-token``; a monitor that HAS a token refuses ``None``
        the same way it refuses a wrong one.

        Raises :class:`MonitorRefused` when the monitor already has a brain
        (``kind="busy"``) or the token did not authorise us
        (``kind="unauthorized"`` - the one a UI turns into "check the token on
        the other machine" rather than into a retry),
        :class:`~agentclip.driver.monitor.wire.WireVersionError` (naming BOTH
        installs) on a version mismatch, and :class:`MonitorDisconnected` when
        the far side hangs up without saying anything.
        """
        reader, writer = await asyncio.open_connection(host, port, limit=LINE_LIMIT)
        peer = f"{host}:{port}"
        try:
            writer.write(encode_line(hello_frame(token)).encode("utf-8"))
            await writer.drain()
            raw = await reader.readline()
            if not raw:
                raise MonitorDisconnected(f"{peer} closed the connection during the handshake")
            frame = decode_line(raw.decode("utf-8", errors="replace"))
            if frame_type(frame) == "error":
                refusal = read_error(frame)
                raise MonitorRefused(refusal.kind, refusal.message)
            ack = read_hello_ack(frame)
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer, ack, peer=peer)

    # == what a caller can ask about the link ==================================

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def peer(self) -> str:
        """``host:port`` as it was dialled - what a status line names."""
        return self._peer

    @property
    def server_id(self) -> str:
        """The monitor PROCESS's id from the handshake.

        A redial that comes back with a different one reached a monitor that has
        been restarted, so every generation the brain remembers is meaningless
        and the reconnect is a full retarget rather than a resume.
        """
        return self._ack.server_id

    def on_disconnect(self, hook: Callable[[], None]) -> Callable[[], None]:
        """Called once, when the link goes away for a reason nobody asked for.

        A hook registered AFTER the link has already dropped is called straight
        away, so a brain that races the failure cannot miss it. A deliberate
        :meth:`close` is not a disconnect and fires nothing.
        """
        if not self._connected and not self._closing:
            self._safely(hook)
            return lambda: None
        self._disconnect_hooks.append(hook)

        def unsubscribe() -> None:
            if hook in self._disconnect_hooks:
                self._disconnect_hooks.remove(hook)

        return unsubscribe

    # == lifecycle / configuration =============================================

    async def configure(self, spec: MonitorSpec) -> int:
        """Retarget the far monitor; returns ITS generation, which is now ours.

        The number is the monitor's own counter, not a local one: after a redial
        the brain must call this before it can tell a live tick from a ghost,
        because the monitor kept polling and kept counting while nobody was
        attached (§2.8).
        """
        generation = int(await self._call("configure", spec=spec))
        self._generation = generation
        return generation

    async def configured_region(self) -> ScreenRegion | None:
        answer = await self._call("configured_region")
        assert answer is None or isinstance(answer, ScreenRegion)
        return answer

    async def suspend(self) -> None:
        await self._call("suspend")

    async def resume(self) -> None:
        await self._call("resume")

    async def close(self) -> None:
        """Drop the link. Idempotent, and NOT a shutdown of the far monitor.

        ``UIMonitor.close`` means "stop every thread you own", and the threads
        this object owns are its reader task and its socket. The monitor on the
        other end is a standing process that outlives every brain (§2.8), so
        nothing is sent: closing the socket IS the goodbye.
        """
        if self._closing:
            return
        self._closing = True
        self._teardown(MonitorDisconnected(f"the link to {self._peer} was closed"))
        self._writer.close()
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - the link is over either way
                pass
        with contextlib.suppress(OSError, ConnectionError):
            await self._writer.wait_closed()

    # == observation (local reads; no round trip) ==============================

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def latest(self) -> Tick | None:
        return self._latest

    async def observe(self) -> Tick:
        """The next tick pushed AFTER this call - never the cached one."""
        if not self._connected:
            raise MonitorDisconnected(f"not attached to {self._peer}")
        future: asyncio.Future[Tick] = self._loop.create_future()
        armed = -1 if self._latest is None else self._latest.seq
        self._waiters.append(_Waiter(armed, future))
        return await future

    def subscribe(self, hook: TickHook) -> Callable[[], None]:
        """Every tick as it lands, on the READER TASK. Must not block."""
        self._hooks.append(hook)

        def unsubscribe() -> None:
            if hook in self._hooks:
                self._hooks.remove(hook)

        return unsubscribe

    def on_clip(self, hook: ClipHook) -> Callable[[], None]:
        """Every clipboard capture the far watcher accepted, on the reader task."""
        self._clip_hooks.append(hook)

        def unsubscribe() -> None:
            if hook in self._clip_hooks:
                self._clip_hooks.remove(hook)

        return unsubscribe

    # == actions ===============================================================

    async def focus_window(self, handle: int) -> bool:
        return bool(await self._call("focus_window", handle=handle))

    async def foreground_window(self) -> int | None:
        answer = await self._call("foreground_window")
        return None if answer is None else int(answer)

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        return bool(await self._call("click", region=region, settle_s=settle_s))

    async def move_cursor(self, x: int, y: int) -> bool:
        return bool(await self._call("move_cursor", x=x, y=y))

    async def scroll(self, region: ScreenRegion, detents: int) -> bool:
        return bool(await self._call("scroll", region=region, detents=detents))

    async def scroll_key(self, key: str, taps: int = 1) -> bool:
        return bool(await self._call("scroll_key", key=key, taps=taps))

    async def send_paste(self) -> bool:
        return bool(await self._call("send_paste"))

    async def send_enter(self) -> bool:
        return bool(await self._call("send_enter"))

    async def read_clipboard(self) -> str | None:
        answer = await self._call("read_clipboard")
        return None if answer is None else str(answer)

    async def write_clipboard(self, text: str) -> None:
        """Put ``text`` on the FAR machine's clipboard.

        Raises ``ClipboardUnavailable`` exactly as the local monitor does - the
        one monitor-side exception with an error kind of its own, because the
        manual-paste fallback catches it by type and a wire that flattened it
        into a generic failure would turn a fallback into a crash.
        """
        await self._call("write_clipboard", text=text)

    # -- the pixel verdicts ----------------------------------------------------

    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]:
        answer = await self._call("find_all", kind=kind)
        return tuple(answer)

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        answer = await self._call("locate", kind=kind, exclude_kinds=exclude_kinds)
        assert isinstance(answer, Located)
        return answer

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        answer = await self._call("click_element", kind=kind, settle_s=settle_s)
        assert isinstance(answer, ElementClick)
        return answer

    async def hover_scan(self, kind: TemplateKind) -> ScreenRegion | None:
        answer = await self._call("hover_scan", kind=kind)
        assert answer is None or isinstance(answer, ScreenRegion)
        return answer

    async def snap_to_bottom(self, action: str) -> None:
        await self._call("snap_to_bottom", action=action)

    # == the clipboard watcher =================================================

    @property
    def clipboard_kind(self) -> str | None:
        """The far machine's backend, learned in the handshake.

        Stated at connect time rather than asked for, because the Protocol makes
        this a PROPERTY and a property cannot await. It is fixed for the
        monitor's lifetime - the backend is chosen once, when that process
        starts - so there is nothing here that can go stale.
        """
        return self._ack.clipboard_kind

    def watch_clipboard(self, on: bool) -> bool:
        """Start or stop the far watcher; returns whether one is polling now.

        The Protocol's one synchronous verb, so it cannot await its own round
        trip. The call is sent and not waited on, and the answer is computed
        from :attr:`clipboard_kind` with ``LocalUIMonitor.watch_clipboard``'s own
        arithmetic: ``False`` for ``on=False``, and ``False`` for an ``on=True``
        there is nothing to honour (no backend at all, or the write-only
        "manual" sentinel). That is a prediction, but it is a prediction from
        the only input the far side's answer has.
        """
        if not self._connected:
            return False
        self._notify("watch_clipboard", on=on)
        if not on:
            return False
        kind = self._ack.clipboard_kind
        return kind is not None and kind != "manual"

    # == the wire ==============================================================

    def _take_id(self) -> int:
        self._next_call_id += 1
        return self._next_call_id

    async def _call(self, verb: str, **kwargs: Any) -> Any:
        """One round trip: encode, send, park on the id, decode the answer."""
        if not self._connected:
            raise MonitorDisconnected(f"not attached to {self._peer}")
        params = encode_params(verb, **kwargs)
        call_id = self._take_id()
        future: asyncio.Future[Any] = self._loop.create_future()
        self._pending[call_id] = future
        try:
            await self._write(call_frame(call_id, verb, params))
        except BaseException:
            self._pending.pop(call_id, None)
            raise
        payload = await future
        return decode_result(verb, payload)

    def _notify(self, verb: str, **kwargs: Any) -> None:
        """Send a call and never look at the answer. Only ``watch_clipboard``.

        The result frame still comes back and is dropped by the reader as an
        unknown id, which is deliberate: the far side answers every call the
        same way, so there is no second code path on the server for a verb
        nobody is waiting on.
        """
        frame = call_frame(self._take_id(), verb, encode_params(verb, **kwargs))
        self._loop.call_soon_threadsafe(self._spawn_write, frame)

    def _spawn_write(self, frame: dict[str, Any]) -> None:
        task = self._loop.create_task(self._write_quietly(frame))
        # Held only so the loop cannot garbage-collect a task mid-flight.
        task.add_done_callback(lambda _t: None)

    async def _write_quietly(self, frame: dict[str, Any]) -> None:
        with contextlib.suppress(MonitorDisconnected):
            await self._write(frame)

    async def _write(self, frame: dict[str, Any]) -> None:
        async with self._write_lock:
            if self._writer.is_closing() or not self._connected:
                raise MonitorDisconnected(f"the link to {self._peer} is gone")
            try:
                self._writer.write(encode_line(frame).encode("utf-8"))
                await self._writer.drain()
            except (OSError, ConnectionError) as exc:
                failure = MonitorDisconnected(f"writing to {self._peer} failed: {exc}")
                self._teardown(failure)
                raise failure from exc

    async def _read_loop(self) -> None:
        why: Exception = MonitorDisconnected(f"{self._peer} closed the connection")
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                self._dispatch(decode_line(raw.decode("utf-8", errors="replace")))
        except asyncio.CancelledError:
            raise
        except MonitorLinkError as exc:
            why = exc
        except WireError as exc:
            # A frame we cannot parse means the stream is no longer one we
            # understand. Nothing here tries to resynchronise - a
            # half-understood monitor is worse than no monitor.
            why = MonitorLinkError(f"{self._peer} sent a frame we cannot read: {exc}")
        except (OSError, ConnectionError) as exc:
            why = MonitorDisconnected(f"the link to {self._peer} failed: {exc}")
        finally:
            self._teardown(why)

    def _dispatch(self, frame: dict[str, Any]) -> None:
        kind = frame_type(frame)
        if kind == "tick":
            self._tick_arrived(read_tick(frame))
        elif kind == "clip":
            text = read_clip(frame)
            for hook in tuple(self._clip_hooks):
                self._safely(hook, text)
        elif kind == "result":
            call_id, value = read_result(frame)
            future = self._pending.pop(call_id, None)
            if future is not None and not future.done():
                future.set_result(value)
        elif kind == "error":
            self._error_arrived(frame)
        else:
            raise WireError(f"a client reads no {kind!r} frames after the handshake")

    def _error_arrived(self, frame: dict[str, Any]) -> None:
        error = read_error(frame)
        if error.id is None:
            # A failure that belongs to the CONNECTION rather than to a call.
            # Nothing to answer it with, so it ends the link.
            raise MonitorLinkError(f"{self._peer}: {error.kind}: {error.message}")
        future = self._pending.pop(error.id, None)
        if future is None or future.done():
            return
        if error.kind == "clipboard_unavailable":
            future.set_exception(ClipboardUnavailable(error.message))
        else:
            future.set_exception(MonitorCallError(error.kind, error.message))

    def _tick_arrived(self, tick: Tick) -> None:
        self._latest = tick
        # The far monitor's generation is the only one there is: a configure
        # bumped it there, and a tick is what tells us the bump landed.
        self._generation = tick.generation
        ready = [waiter for waiter in self._waiters if tick.seq > waiter.armed]
        for waiter in ready:
            self._waiters.remove(waiter)
            if not waiter.future.done():
                waiter.future.set_result(tick)
        for hook in tuple(self._hooks):
            self._safely(hook, tick)

    def _safely(self, hook: Callable[..., None], *args: Any) -> None:
        try:
            hook(*args)
        except Exception:  # noqa: BLE001 - one bad subscriber, not one dead link
            _log.exception("a monitor subscriber raised")

    def _teardown(self, why: Exception) -> None:
        """Fail everything in flight, once, and tell whoever asked to be told."""
        if not self._connected:
            return
        self._connected = False
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(why)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(why)
        if self._closing or self._notified:
            return
        self._notified = True
        for hook in tuple(self._disconnect_hooks):
            self._safely(hook)


def _conforms(monitor: RemoteUIMonitor) -> UIMonitor:
    """Structural pin: mypy fails HERE if this class drifts from the Protocol.

    The same pin ``local.py`` and ``fake.py`` carry, for the same reason: the
    tests are not type-checked, and a Protocol nothing declares is a Protocol
    nothing enforces.
    """
    return monitor
