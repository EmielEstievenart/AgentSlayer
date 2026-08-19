"""The engine half's dispatch loop: JSON lines in, frames out.

This is the process that runs BESIDE the engine - on the target in a remote
session, in a subprocess on this PC in the localhost tests
(docs/design/remote-executor.md sections 2.9 and 4). It hosts bare
:class:`~agentclip.engine.engine.Engine` objects and nothing else: no
controller, no clipboard, no window. Every message it understands is defined in
:mod:`agentclip.engine.link.wire`, which the Shell's ``RemoteLink`` imports too,
so the two halves cannot drift.

Threading
---------
Synchronous by design, threads and not asyncio: the engine IS synchronous
(``execute()`` runs a whole plan of tool calls in a blocking loop) and the event
loop that a Shell needs lives on the OTHER side of the wire. So:

* **The reader thread only reads and routes.** It never runs an engine method,
  which is what keeps it free to notice the next line - a ``cancel`` frame in
  particular - while a turn is still running.
* **Every call runs on a thread of its own** (``threading.Thread`` per call).
  The client contract is one call in flight per session, enforced here by a
  per-session busy flag: a second call for a session whose previous one has not
  been answered earns ``bad_request`` rather than a second worker.
* **``cancel`` is handled on the reader thread**, by calling
  ``Engine.request_cancel()`` straight - the engine's ONE thread-safe method (it
  sets a ``threading.Event``), and thread-safe precisely so somebody who is not
  the worker can interrupt the worker.
* **One lock guards every frame written.** Workers, hooks and the reader thread
  all write through :class:`_Writer`, and each write is encode + write + flush
  under that lock, so a 200k-char output delta can never be spliced through the
  middle of somebody else's result frame. Nothing is left to the stream's
  buffering mode.

The interleaving guarantee wire.py promises - every ``progress``/``output``
frame of a call written and flushed BEFORE that call's answer - falls out of
this shape rather than being enforced: the hooks fire synchronously inside the
engine method, on the worker's own thread, so they have all been through the
write lock by the time ``encode_result`` is reached.

Failure
-------
Nothing a handler does kills the server. An exception out of an engine method
becomes an ``error`` frame (``budget_exceeded`` / ``engine_state_error`` keep
their identity, everything else is ``internal``); a frame that is not valid
protocol v1 becomes ``bad_request`` and the loop reads on. Two malformed lines
IN A ROW end the process, because at that point the stream is no longer a stream
of frames and answering it politely is a guess. ``stderr`` is the log, never
protocol data; EOF on the reader is a clean shutdown.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol, TextIO

from agentclip import __version__
from agentclip.engine.engine import CallProgress, Engine
from agentclip.engine.link.factory import EngineRequest
from agentclip.engine.link.wire import (
    BUILD_SESSION,
    MCP_STATUSES,
    SESSION_METHODS,
    CallFrame,
    SessionInfo,
    WireError,
    decode_line,
    decode_params,
    encode_line,
    encode_result,
    error_frame,
    error_frame_for,
    frame_type,
    hello_ack_frame,
    output_frame,
    progress_frame,
    read_call,
    read_cancel,
    read_hello,
    result_frame,
)
from agentclip.executor.mcp.types import McpServerStatus


class EngineBuilder(Protocol):
    """What ``serve`` needs of the object it builds engines from.

    A Protocol rather than the ``Callable`` alias this used to be, because the
    builder is no longer only a function: the MCP runtime is owned engine-side
    (docs/design/remote-executor.md section 2.7), so the server has to be able to
    ASK it what that runtime looks like - both to answer the link-scoped
    ``mcp_statuses`` call and to put the settle in ``build_session``'s answer.
    :class:`agentclip.engine.link.factory.EngineBuilder` satisfies it as it
    stands; nothing else in ``src`` builds engines for a link.
    """

    def __call__(self, request: EngineRequest | str, /) -> Engine: ...

    def mcp_statuses(self) -> tuple[McpServerStatus, ...]: ...

EXIT_OK = 0
EXIT_PROTOCOL = 1

# The ``id`` on an error frame that answers no call - a malformed line, a bad
# hello. wire's error frames carry an INT id (``read_error`` refuses null), so
# "unattributable" needs a value rather than an absence: 0, which no call can
# have, because call ids are client-chosen and strictly increasing from 1.
_NO_CALL_ID = 0

# How long shutdown waits for a turn that is still running. Long enough for a
# tool call to notice EOF is not its business and finish composing its results,
# short enough that a wedged handler cannot keep the process alive.
_DRAIN_TIMEOUT_S = 5.0

# Exactly the 13 state-changing `Link` methods, and the only strings that ever
# reach ``getattr`` on an engine.
_DISPATCHABLE: frozenset[str] = frozenset(SESSION_METHODS)


def _log(stream: TextIO, message: str) -> None:
    """One line to the process log.

    stderr is NEVER protocol data (wire.py, "Framing"): the one thing this
    process may put on stdout is a frame.
    """
    try:
        stream.write(f"agentclip-link: {message}\n")
        stream.flush()
    except (OSError, ValueError):  # the log went away; the link may not have
        pass


class _Writer:
    """Every frame that leaves this process, one at a time.

    The lock is the whole point: encode, write and flush are one atomic unit, so
    two threads - a worker answering a call and a progress hook firing inside
    another one - can never interleave halves of two lines. Flushing per frame
    rather than trusting the stream's buffering is equally deliberate; a Shell
    waiting on a result must not be waiting on a buffer to fill.
    """

    def __init__(self, stream: TextIO, log: TextIO) -> None:
        self._stream = stream
        self._log = log
        self._lock = threading.Lock()
        # Set once the far end stops listening. Frames after that are dropped
        # rather than raised: a progress hook is not allowed to fail a turn.
        self.broken = False

    def send(self, frame: dict[str, Any]) -> None:
        line = encode_line(frame)
        with self._lock:
            if self.broken:
                return
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError) as exc:
                self.broken = True
                _log(self._log, f"the link went away mid-write: {exc}")


class _Session:
    """One hosted engine, plus the flag that says a call is out on it."""

    __slots__ = ("sid", "engine", "busy")

    def __init__(self, sid: str, engine: Engine) -> None:
        self.sid = sid
        self.engine = engine
        self.busy = False


def serve(
    reader: TextIO,
    writer: TextIO,
    builder: EngineBuilder,
    *,
    log: TextIO = sys.stderr,
) -> int:
    """Speak protocol v1 on ``reader``/``writer`` until the client goes away.

    ``builder`` is :func:`agentclip.engine.link.factory.make_engine_builder`'s
    return value - and it is called MORE THAN ONCE over a process's life: one
    server hosts every session of one link, and the controller builds sub-agent
    engines mid-session from the same factory. It is also asked for its
    ``mcp_statuses`` (see :class:`EngineBuilder`), which is the whole of what
    this process tells the Shell about the MCP runtime it owns.

    Text streams rather than a socket or a subprocess so the loop can be driven
    in-process by tests over a pair of pipes; in production they are the
    process's own stdin/stdout (see ``python -m agentclip.engine.link``).

    Returns the process exit code: 0 for a clean shutdown (the client closed the
    link), nonzero for a wire that could not be trusted - a bad handshake, two
    malformed lines in a row, or a write end that went away.
    """
    out = _Writer(writer, log)
    if not _handshake(reader, out, log):
        return EXIT_PROTOCOL
    return _Server(out, builder, log).run(reader)


def _handshake(reader: TextIO, out: _Writer, log: TextIO) -> bool:
    """The first line must be a v1 ``hello``; nothing may precede it.

    A WIRE version this process does not speak is refused HERE, before an engine
    exists, because every frame after this point is read as v1 - and reading a
    v2 call frame as v1 is exactly the guess a protocol boundary must not make.
    The refusal names both wire versions AND both package versions
    (``WireVersionError``), because the two halves are installed separately
    (design section 2.6) and the human on the other end can only act on the
    latter.

    A PACKAGE version this process does not share is not refused at all - same
    wire, different release, which is the expected steady state of two
    independently-installed halves. It costs one line of stderr, so that a
    puzzling session has the two numbers in its log.
    """
    line = reader.readline()
    if not line:
        _log(log, "the client closed the link before saying hello")
        return False
    try:
        peer = read_hello(decode_line(line))
    except WireError as exc:
        # Answerable even when the line was gibberish: the client is owed the
        # reason it is about to see the process exit. Best effort - a client
        # that has already gone away simply never reads it.
        out.send(error_frame(_NO_CALL_ID, "bad_request", str(exc)))
        _log(log, f"bad hello: {exc}")
        return False
    if peer.package != __version__:
        _log(
            log,
            f"the client is agentclip {peer.package}, this engine is agentclip {__version__}"
            f" - same wire v{peer.wire}, carrying on",
        )
    out.send(hello_ack_frame(str(uuid.uuid4())))
    return True


class _Server:
    """The routing loop and the sessions it hosts."""

    def __init__(self, out: _Writer, builder: EngineBuilder, log: TextIO) -> None:
        self._out = out
        self._builder = builder
        self._log = log
        # Guards everything below it. Held only for dict/flag work - NEVER
        # across an engine call, which would serialize the sessions this server
        # exists to keep independent.
        self._state = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._next_id = 1
        self._workers: list[threading.Thread] = []

    # -- the reader thread -----------------------------------------------------

    def run(self, reader: TextIO) -> int:
        """Read frames until EOF. This thread never runs an engine method."""
        bad_streak = 0
        while True:
            line = reader.readline()
            if not line:
                break  # EOF: the client closed the link (or the channel died)
            try:
                frame = decode_line(line)
            except WireError as exc:
                bad_streak += 1
                self._refuse(_NO_CALL_ID, exc, bad_streak)
                if bad_streak >= 2:
                    return EXIT_PROTOCOL
                continue
            try:
                self._route(frame)
            except WireError as exc:
                bad_streak += 1
                self._refuse(_id_of(frame), exc, bad_streak)
                if bad_streak >= 2:
                    return EXIT_PROTOCOL
                continue
            bad_streak = 0
            if self._out.broken:
                return EXIT_PROTOCOL
        # Past EOF nothing new is read, so no call can start; what is already
        # running gets a moment to finish and write its answer through the lock.
        self._drain()
        return EXIT_PROTOCOL if self._out.broken else EXIT_OK

    def _refuse(self, req_id: int, exc: WireError, streak: int) -> None:
        """Answer one unreadable line, and say so in the log.

        A WireError is fatal for the FRAME it was raised on, not for the link
        (wire.WireError): the client is told which frame it got wrong and the
        loop reads on. Two in a row is the exception - by then the stream is not
        a stream of frames any more, and continuing would be guessing.
        """
        self._out.send(error_frame(req_id, "bad_request", str(exc)))
        _log(self._log, f"bad frame ({streak} in a row): {exc}")
        if streak >= 2:
            _log(self._log, "two bad frames in a row - the wire cannot be trusted, exiting")

    def _route(self, frame: dict[str, Any]) -> None:
        kind = frame_type(frame)
        if kind == "call":
            self._start_call(read_call(frame))
        elif kind == "cancel":
            self._cancel(read_cancel(frame))
        else:
            # hello (a second one), or an answer frame this side never receives.
            raise WireError(f"a server does not receive a {kind!r} frame")

    def _start_call(self, call: CallFrame) -> None:
        """Validate what only the reader can validate, then hand it to a worker."""
        if call.method == BUILD_SESSION:
            self._spawn(self._run_build, call)
            return
        if call.method == MCP_STATUSES:
            self._spawn(self._run_mcp_statuses, call)
            return
        if call.method not in _DISPATCHABLE:
            # read_call already refused a method the wire does not define; this
            # is the whitelist that stands between a string off the wire and
            # ``getattr(engine, ...)`` in the worker.
            self._out.send(error_frame(call.id, "bad_request", f"unknown method {call.method!r}"))
            return
        sid = call.session or ""
        with self._state:
            session = self._sessions.get(sid)
            busy = session is not None and session.busy
            if session is not None and not busy:
                session.busy = True
        if session is None:
            self._out.send(error_frame(call.id, "bad_request", f"unknown session {sid!r}"))
            return
        if busy:
            # One in flight per session is the client's contract (shell/app/link.py
            # serializes with a lock); a second one means the two ends disagree
            # about what is outstanding, which is worth saying out loud.
            self._out.send(
                error_frame(call.id, "bad_request", f"session {sid!r} already has a call in flight")
            )
            return
        self._spawn(self._run_call, session, call)

    def _cancel(self, sid: str) -> None:
        """The one frame answered on the READER thread, and the reason it can be.

        ``Engine.request_cancel`` only sets a ``threading.Event`` - it is the
        engine's single thread-safe method, made that way so somebody who is not
        the worker can interrupt the worker. Never answered (wire.py), and a
        cancel for a session that is doing nothing is a no-op, exactly like the
        local one.
        """
        with self._state:
            session = self._sessions.get(sid)
        if session is None:
            _log(self._log, f"cancel for unknown session {sid!r} - ignored")
            return
        session.engine.request_cancel()

    def _spawn(self, target: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, name="agentclip-link-call", daemon=True)
        with self._state:
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(thread)
        thread.start()

    def _drain(self) -> None:
        """Give the in-flight call a bounded moment to answer, then let go."""
        with self._state:
            workers = [t for t in self._workers if t.is_alive()]
        deadline = time.monotonic() + _DRAIN_TIMEOUT_S
        for thread in workers:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in workers):
            _log(self._log, f"a call was still running {_DRAIN_TIMEOUT_S}s after EOF - leaving it")

    # -- worker threads --------------------------------------------------------

    def _run_build(self, call: CallFrame) -> None:
        """Mint one session: build the engine, wire its hooks, answer with the id.

        Failures here (an unreadable config, a project that is not there) leave
        NO session behind and do not touch the ones already hosted - the client
        is told, and the server keeps running.
        """
        try:
            request = EngineRequest(**decode_params(BUILD_SESSION, call.params))
        except WireError as exc:
            self._out.send(error_frame(call.id, "bad_request", str(exc)))
            return
        try:
            engine = self._builder(request)
        except Exception as exc:  # noqa: BLE001 - a build failure is not a crash
            _log(self._log, f"build_session failed: {type(exc).__name__}: {exc}")
            self._out.send(error_frame_for(call.id, exc))
            return
        with self._state:
            sid = f"s{self._next_id}"
            self._next_id += 1
            self._sessions[sid] = _Session(sid, engine)
        # Wired BEFORE the answer goes out, because the client may send its
        # first call the instant it reads the SessionInfo, and the progress
        # frames of that turn must not find the hooks unset. Both fire from the
        # worker thread inside an engine method and must not block (the engine's
        # contract) - writing one line under the write lock is exactly that.
        engine.set_progress_hook(
            lambda progress: self._emit_progress(sid, progress),
        )
        engine.set_output_hook(
            lambda call_id, delta: self._out.send(output_frame(sid, call_id, delta)),
        )
        info = SessionInfo(
            session=sid,
            chat_name=engine.chat_name,
            role=engine.role,
            build_warnings=engine.build_warnings,
            # The settle rides home with the session it settled for: the build
            # above already waited on the catalog (factory._sized_registry gives
            # a pending/connecting runtime 0.5s), so by now these rows are the
            # ones the Shell wants to paint - and asking for them cost this
            # process nothing, where a second round trip would have cost the
            # Shell a whole one.
            mcp_statuses=self._builder.mcp_statuses(),
        )
        self._out.send(result_frame(call.id, encode_result(BUILD_SESSION, info)))

    def _run_mcp_statuses(self, call: CallFrame) -> None:
        """The link-scoped read: what the MCP runtime of THIS PROCESS looks like.

        On a worker thread like ``build_session``, and for the same reason - the
        first ask is what BUILDS the manager (the builder is lazy), so it is not
        work the reader thread may sit down to. No session is involved: one
        builder owns one manager however many sessions it hosts, so this answer
        belongs to the connection (wire.LINK_METHODS).
        """
        try:
            decode_params(MCP_STATUSES, call.params)
        except WireError as exc:
            self._out.send(error_frame(call.id, "bad_request", str(exc)))
            return
        try:
            statuses = self._builder.mcp_statuses()
        except Exception as exc:  # noqa: BLE001 - a status read is not a crash
            _log(self._log, f"mcp_statuses failed: {type(exc).__name__}: {exc}")
            self._out.send(error_frame_for(call.id, exc))
            return
        self._out.send(result_frame(call.id, encode_result(MCP_STATUSES, statuses)))

    def _emit_progress(self, sid: str, progress: CallProgress) -> None:
        self._out.send(progress_frame(sid, progress))

    def _run_call(self, session: _Session, call: CallFrame) -> None:
        """One engine method, on its own thread, answered exactly once."""
        method = call.method
        answer: dict[str, Any]
        try:
            kwargs = decode_params(method, call.params)
            # Safe because `method` came through _DISPATCHABLE: one of the 13
            # names wire.SESSION_METHODS lists, never an arbitrary string.
            value = getattr(session.engine, method)(**kwargs)
            # By the time this line runs, every progress/output frame this call
            # produced has already been written AND flushed - the hooks fired
            # synchronously inside the call above, on this thread. That is the
            # whole of wire.py's interleaving guarantee: the answer is the end of
            # the call's event stream, with no sequence numbers needed.
            answer = result_frame(call.id, encode_result(method, value))
        except WireError as exc:
            answer = error_frame(call.id, "bad_request", str(exc))
        except Exception as exc:  # noqa: BLE001 - the far side is told, not killed
            answer = error_frame_for(call.id, exc)
        finally:
            # Cleared BEFORE the answer is sent: a client sends its next call the
            # moment it reads this one's answer, and a flag still set would meet
            # it with a spurious "already has a call in flight".
            with self._state:
                session.busy = False
        self._out.send(answer)


def _id_of(frame: dict[str, Any]) -> int:
    """The call id an error for this frame should carry, best effort.

    A frame that decoded as JSON may still be unroutable (unknown method,
    missing session). If it named an id, the answer is attributed to it so the
    client can fail that one call; otherwise it is unattributable and gets 0.
    """
    value = frame.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        return _NO_CALL_ID
    return value
