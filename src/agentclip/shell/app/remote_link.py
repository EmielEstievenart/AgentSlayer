"""The Shell's end of the wire: one connection, and the links it hosts.

The mirror image of :mod:`agentclip.engine.link.server`, and the other half of
the seam :mod:`agentclip.shell.app.link` defines (docs/design/remote-executor.md
sections 2.2, 2.9 and 2.11). :class:`RemoteLink` is a ``Link`` like
:class:`~agentclip.shell.app.link.LocalLink` is - the controller cannot tell them
apart - except that every call is a frame written to a stream and an answer read
back off another one.

Transport-agnostic on purpose
-----------------------------
:class:`RemoteLinkClient` is constructed over a pair of LINE STREAMS and spawns
nothing. In increment 2 the tests hand it a localhost subprocess's pipes
(``python -m agentclip.engine.link``); in increment 3 the same object gets an SSH
exec channel's streams (``executor.hosts.ssh.LinkChannel``). That is why there is
no ``Popen`` here and no ``paramiko``: what a link needs is a reader and a
writer, and deciding WHERE they come from is the launcher's business, not the
protocol's - which is also why the two parameters are the
:class:`LineReader`/:class:`LineWriter` Protocols below rather than ``TextIO``:
one of the two transports is a file object and the other deliberately is not.

One reader, inside the call
---------------------------
There is no background reader thread. ``roundtrip`` writes its call frame and
then reads frames itself until that call's answer arrives, dispatching the
progress/output events it meets on the way. That is sound only while **one call
is in flight per connection**, and since link-scoped calls exist
(``mcp_statuses``, ``build_session``) the per-link ``asyncio.Lock`` cannot
promise it on its own: a link-scoped call belongs to no link, so no per-link
lock stands between it and a session call's reads, and two readers on one stream
would each consume frames the other was waiting for. So the serialization lives
HERE, in a ``threading.Lock`` held across the whole of ``roundtrip`` - write and
read-until-answer together. The per-link locks stay on top of it (they are the
``Link`` contract, and they are what the server's per-session busy rule
mirrors); the double-serialization costs nothing, because a connection that
allowed two calls at once would have nowhere to put the second one's answer.
A reader thread would buy the same guarantee and cost a queue, a shutdown
protocol and a second place for a frame to be lost.

Events reach the link they belong to, not the caller
----------------------------------------------------
Progress and output frames are dispatched BY SESSION ID through the client's
registry, so a parked link's events still reach ITS hooks even though somebody
else's call is the one doing the reading. The engine's hook contract is
reimplemented here rather than inherited: hooks fire from the worker thread
(``asyncio.to_thread``'s, the same shape as the engine's), must not block, and
one that raises is dropped for good - a progress watcher may never fail a turn,
and a wire is no reason to change that.

Cancel, and death
-----------------
:meth:`RemoteLink.request_cancel` writes a ``cancel`` frame under the client's
send lock while the roundtrip it interrupts is blocked in a read. That is the
whole point of the lock being around the WRITE only. And a link that has gone
away (EOF on the reader, a broken writer) raises
:class:`~agentclip.engine.link.wire.EngineLinkError` rather than hanging: a dead
server must cost one failed call, not a wedged UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal, Protocol

from agentclip import __version__
from agentclip.engine.approval import PermissionMode
from agentclip.engine.engine import (
    ArmResult,
    CallProgress,
    IngestResult,
    PendingAction,
    ProgressHook,
    StatusSnapshot,
    StepResult,
)
from agentclip.engine.link import wire
from agentclip.engine.link.factory import EngineRequest
from agentclip.engine.link.wire import (
    BUILD_SESSION,
    MCP_STATUSES,
    EngineLinkError,
    SessionInfo,
    WireError,
    WireVersionError,
)
from agentclip.engine.states import Decision
from agentclip.engine.store.backups import UndoReport
from agentclip.protocol.types import Outbound, ResultStatus
from agentclip.shell.app.link import Link, McpStatusLine

# The two ``EngineLinkError`` kinds this side raises on its own account. The
# wire's four kinds (``bad_request``, ``internal``, and the two that rebuild as
# real exceptions) all describe what the FAR side answered; these two describe
# what happened to the link itself, which no error frame can tell us because by
# then there are no error frames.
LINK_CLOSED = "link_closed"
LINK_PROTOCOL = "protocol"
# The third: the two halves are installed separately (design section 2.6), so a
# target running another wire version is a CONFIGURATION problem the user can
# fix, not a bug - and it deserves a message that says which install to touch.
LINK_VERSION = "version_mismatch"

# What the user is told to do about it. Named here rather than spelled inline so
# the two paths that raise it (a wrong-version ack, and an error frame in place
# of one) cannot end up advising two different things.
UPDATE_HINT = "update the target's install (e.g. `uv tool install --upgrade agentclip`)"


class LineReader(Protocol):
    """The half of a transport this client reads frames off.

    Two Protocols rather than ``TextIO`` because a transport is not always a
    file: increment 2's tests hand over a subprocess's pipes (which ARE
    ``TextIO``), and increment 3 hands over an SSH exec channel wrapped in a
    small duplex adapter (which is not, and must not be - see
    ``executor.hosts.ssh.LinkChannel``). Structural typing is exactly the tool
    for "whatever can do these two things", and stating the two things is
    cheaper than requiring every transport to impersonate a file object.
    """

    def readline(self) -> str:
        """The next ``\\n``-terminated frame, or ``""`` at EOF. Blocks."""
        ...


class LineWriter(Protocol):
    """The half this client writes frames onto. See :class:`LineReader`."""

    def write(self, s: str, /) -> int: ...

    def flush(self) -> None: ...


def version_refusal(peer: wire.Versions) -> str:
    """The sentence a user reads when the target speaks another wire version.

    Both installs by name, because that is the only half a human can act on: the
    wire number is ours to reason about, the ``agentclip`` version is what they
    chose on each machine and what they will change.
    """
    return (
        f"the engine on the target speaks wire v{peer.wire} (agentclip {peer.package}); "
        f"this AgentClip speaks wire v{wire.WIRE_VERSION} (agentclip {__version__}) "
        f"- {UPDATE_HINT}"
    )


class RemoteLinkClient:
    """One connection to one server process, and the sessions it hosts.

    Built over the two halves of an already-open transport - a subprocess's
    stdin/stdout in the localhost tests, an SSH exec channel's streams in a
    remote session. It never opens or closes them: whoever made the transport
    owns its lifetime, and this object only speaks protocol v1 across it.

    THREAD CONTRACT, and the reason the design is this simple:

    * :meth:`roundtrip` runs on a worker thread (``asyncio.to_thread``, from
      :class:`RemoteLink`) and is the ONLY reader. It holds :attr:`_call` for
      its whole length, so "one call in flight per connection" is enforced here
      rather than merely relied on: the per-link locks above cannot serialize a
      link-scoped call against a session one, because a link-scoped call belongs
      to no link.
    * :meth:`send_cancel` runs on the EVENT LOOP thread, in the middle of
      somebody else's roundtrip. It takes the SEND lock (which the roundtrip
      released the moment its call frame was flushed) and never the call lock or
      the reader - taking the call lock is exactly the wait a cancel must not do,
      since the call it is cancelling is the one holding it.
    * :meth:`hello` and :meth:`build_session` are ordinary blocking calls made
      before/between links, and count as roundtrips for the rule above.
    """

    def __init__(self, reader: LineReader, writer: LineWriter) -> None:
        self._reader = reader
        self._writer = writer
        # Guards every write+flush, and nothing else. Deliberately NOT held
        # across the read that follows: a cancel has to get out while the call
        # it cancels is still outstanding.
        self._send = threading.Lock()
        # Guards a WHOLE call - the write and the read-until-answer. See the
        # module docstring: with link-scoped calls in the vocabulary, this is
        # the only lock that can promise one reader on this stream.
        self._call = threading.Lock()
        self._next_id = 0
        # Session id -> the link that owns it. Written by build_session, read by
        # whichever roundtrip meets an event frame; both happen on a worker
        # thread under the one-call-in-flight contract, so the plain dict needs
        # no lock of its own.
        self._links: dict[str, RemoteLink] = {}
        self.server_id: str = ""
        # The target's ``agentclip`` version, learned in the handshake. Nothing
        # branches on it - it is here so a status line or a link indicator can
        # show WHICH install is answering, and so a support question about a
        # remote session has an answer that does not need another round trip.
        self.server_package: str = ""
        # The MCP settle the most recent ``build_session`` carried home. ()
        # until the first session is built, and a SNAPSHOT rather than a live
        # reading - :meth:`mcp_statuses` is how a Shell asks again.
        self.build_mcp_statuses: tuple[McpStatusLine, ...] = ()

    # -- the handshake ---------------------------------------------------------

    def hello(self) -> str:
        """Say hello, check the versions, remember who answered.

        Anything but a v1 ``hello_ack`` - a frame out of order, a process that
        died on startup - is a link that must not be used, so it raises rather
        than being nursed along. The ``server_id`` names the PROCESS (wire.py);
        v1 keeps it for the detach/reattach mode design section 2.3 leaves room
        for, and checks only that it is there.

        The WIRE version is the gate and the only thing refused here. The
        PACKAGE version is remembered and never compared: the engine half is
        installed on the target by the user (design section 2.6), so the two
        halves being different releases of ``agentclip`` is the normal case, not
        a fault. When the gate does close, both installs are named - see
        :func:`version_refusal` - because "wire v2 is not v1" is not something a
        user can do anything with.
        """
        self._write(wire.hello_frame())
        frame = self._read_frame()
        self._refuse_error_frame(frame)
        try:
            ack = wire.read_hello_ack(frame)
        except WireVersionError as exc:
            raise EngineLinkError(LINK_VERSION, version_refusal(exc.peer)) from exc
        except WireError as exc:
            raise EngineLinkError(LINK_PROTOCOL, f"bad handshake: {exc}") from exc
        self.server_id = ack.server_id
        self.server_package = ack.versions.package
        return self.server_id

    def _refuse_error_frame(self, frame: dict[str, Any]) -> None:
        """The other shape a refused handshake takes: an ``error`` instead of an ack.

        A server that does not speak our wire version answers the hello with a
        ``bad_request`` whose detail already names both wire versions and both
        packages (``WireVersionError``), and then exits. Passing that detail
        through - rather than letting ``read_hello_ack`` report "expected a
        'hello_ack' frame, got 'error'" - is the difference between a user who
        knows which machine to upgrade and one who does not.
        """
        if frame.get("type") != "error":
            return
        try:
            error = wire.read_error(frame)
        except WireError:  # not a readable error frame; let the ack reader talk
            return
        raise EngineLinkError(
            LINK_VERSION,
            f"the engine on the target refused the handshake: {error.detail} - {UPDATE_HINT}",
        )

    # -- sessions --------------------------------------------------------------

    def build_session(self, request: EngineRequest) -> RemoteLink:
        """Mint one remote session and wrap it in a :class:`RemoteLink`.

        Called more than once per connection on purpose: one server process
        hosts every session of one link, exactly as one local engine factory
        builds every engine of one run (the controller builds a sub-agent engine
        mid-session from the same factory). Only one of the links is live at a
        time - that is the controller's contract - but a parked one keeps its
        session, and keeps receiving its own events.
        """
        info = self.roundtrip(
            None, BUILD_SESSION, wire.encode_params(BUILD_SESSION, **asdict(request))
        )
        if not isinstance(info, SessionInfo):  # pragma: no cover - wire's codec guarantees it
            raise EngineLinkError(LINK_PROTOCOL, f"build_session answered with {type(info).__name__}")
        # Kept where a Shell can paint it without a second round trip: the far
        # side already waited on the catalog before it answered (wire's
        # SessionInfo), so this is the settle as it stood when the session was
        # built. On the client because it is the CONNECTION's runtime, and on
        # the link because that is what a session-shaped caller holds.
        self.build_mcp_statuses = info.mcp_statuses
        link = RemoteLink(self, info)
        self._links[info.session] = link
        return link

    # -- the connection's MCP runtime ------------------------------------------

    def mcp_statuses(self) -> tuple[McpStatusLine, ...]:
        """The target's MCP rows, right now - a link-scoped roundtrip.

        Sync and session-free on purpose: it is callable from a worker thread
        BEFORE any session exists, which is exactly when a Shell wants to paint
        the block (the local mode's ``cli.LinkFactory`` answers the same
        question at launch, off the builder).
        """
        statuses = self.roundtrip(None, MCP_STATUSES, wire.encode_params(MCP_STATUSES))
        return tuple(statuses)

    # -- one call --------------------------------------------------------------

    def roundtrip(self, sid: str | None, method: str, params: dict[str, Any]) -> Any:
        """Write one call and read until its answer, dispatching events on the way.

        ``sid`` is the session the call belongs to, or None for the two
        link-scoped methods - ``build_session``, which has no session yet
        because it is what mints one, and ``mcp_statuses``, which has none ever
        because the MCP runtime is the process's rather than any session's.

        THE LOCK IS THE CONTRACT. It is held from the first byte written to the
        answer read, so this connection has exactly one reader at a time. The
        per-link ``asyncio.Lock`` above cannot do that job once link-scoped
        calls exist: a ``mcp_statuses`` fired while a session's ``execute`` is
        outstanding would otherwise start reading the frames that turn is
        waiting for. ``send_cancel`` stays outside it, and must - it is the one
        frame written while somebody else's call is blocked in a read.

        Everything that is not this call's answer is either an event (dispatched
        to whichever link owns it and read past) or a protocol violation. A
        result or error carrying somebody ELSE's id is the second kind: with one
        call in flight there is no other call it could belong to, so the two ends
        disagree about what is outstanding and going on would be guessing.
        """
        with self._call:
            req_id = self._write_call(sid, method, params)
            while True:
                frame = self._read_frame()
                try:
                    kind = wire.frame_type(frame)
                except WireError as exc:
                    raise EngineLinkError(LINK_PROTOCOL, str(exc)) from exc
                if kind == "progress":
                    self._dispatch_progress(frame)
                    continue
                if kind == "output":
                    self._dispatch_output(frame)
                    continue
                if kind == "result":
                    answer_id, value = self._read(wire.read_result, frame)
                    self._check_id(answer_id, req_id, method)
                    try:
                        return wire.decode_result(method, value)
                    except WireError as exc:
                        raise EngineLinkError(LINK_PROTOCOL, f"{method}: {exc}") from exc
                if kind == "error":
                    error = self._read(wire.read_error, frame)
                    self._check_id(error.id, req_id, method)
                    # BudgetExceeded and EngineStateError come back AS THEMSELVES
                    # here (wire.error_exception): the controller catches exactly
                    # those two by type, and a link that wrapped them would
                    # silently break both branches.
                    raise wire.error_exception(error)
                raise EngineLinkError(LINK_PROTOCOL, f"a client does not receive a {kind!r} frame")

    def send_cancel(self, sid: str) -> None:
        """The out-of-band frame, written from the event loop mid-roundtrip.

        Never answered, and never allowed to raise: this is the wire's
        ``Link.request_cancel``, a best-effort ask that is a no-op for an idle
        session. A link that has already died will fail the call it was
        interrupting on its own - reporting the same death twice, out of a sync
        method the UI calls on a keypress, buys nothing.
        """
        with contextlib.suppress(EngineLinkError):
            self._write(wire.cancel_frame(sid))

    # -- frames in -------------------------------------------------------------

    def _read_frame(self) -> dict[str, Any]:
        try:
            line = self._reader.readline()
        except (OSError, ValueError) as exc:
            raise EngineLinkError(LINK_CLOSED, f"the link closed mid-read: {exc}") from exc
        if not line:
            # EOF: the server exited, the channel dropped, the process was
            # killed. The caller is told NOW - a link that waited here would
            # hang a turn on a machine that no longer has an engine on it.
            raise EngineLinkError(LINK_CLOSED, "the link closed before the answer arrived")
        try:
            return wire.decode_line(line)
        except WireError as exc:
            raise EngineLinkError(LINK_PROTOCOL, str(exc)) from exc

    def _read(self, reader: Callable[[dict[str, Any]], Any], frame: dict[str, Any]) -> Any:
        try:
            return reader(frame)
        except WireError as exc:
            raise EngineLinkError(LINK_PROTOCOL, str(exc)) from exc

    def _check_id(self, answer_id: int, req_id: int, method: str) -> None:
        if answer_id != req_id:
            raise EngineLinkError(
                LINK_PROTOCOL,
                f"{method}: answer for call {answer_id} while call {req_id} is outstanding",
            )

    def _dispatch_progress(self, frame: dict[str, Any]) -> None:
        event = self._read(wire.read_progress, frame)
        link = self._links.get(event.session)
        if link is not None:  # an event for a session this client never made
            link.fire_progress(event.progress)

    def _dispatch_output(self, frame: dict[str, Any]) -> None:
        event = self._read(wire.read_output, frame)
        link = self._links.get(event.session)
        if link is not None:
            link.fire_output(event.call_id, event.delta)

    # -- frames out ------------------------------------------------------------

    def _write_call(self, sid: str | None, method: str, params: dict[str, Any]) -> int:
        """Allocate this call's id and put its frame on the wire.

        The id is allocated under the same lock that writes, so ids stay
        strictly increasing in the order the frames were actually written.
        """
        with self._send:
            self._next_id += 1
            req_id = self._next_id
            try:
                frame = wire.call_frame(req_id, method, params, session=sid)
            except WireError as exc:  # a method or session this end got wrong
                raise EngineLinkError(LINK_PROTOCOL, str(exc)) from exc
            self._write_locked(frame)
        return req_id

    def _write(self, frame: dict[str, Any]) -> None:
        with self._send:
            self._write_locked(frame)

    def _write_locked(self, frame: dict[str, Any]) -> None:
        try:
            self._writer.write(wire.encode_line(frame))
            self._writer.flush()
        except (OSError, ValueError) as exc:
            raise EngineLinkError(LINK_CLOSED, f"the link closed mid-write: {exc}") from exc


class RemoteLink:
    """A ``Link`` whose engine is in another process, on another machine.

    Structurally identical to :class:`~agentclip.shell.app.link.LocalLink` -
    which is the point: the same ``asyncio.Lock`` serializing the same thirteen
    calls, the same ``asyncio.to_thread`` hop so the event loop never blocks, the
    same three facts read synchronously off the object. Only the body of the hop
    differs: a write and a read instead of an engine method.

    The three facts come home in ``build_session``'s answer (wire's
    ``SessionInfo``) exactly as design section 2.2 said a handshake would, so the
    controller reads ``chat_name`` from the event loop with nothing to await,
    remote session or not.

    Never constructed directly: :meth:`RemoteLinkClient.build_session` makes it
    and registers it, and the registration is what routes this session's
    progress/output frames to the hooks below.
    """

    def __init__(self, client: RemoteLinkClient, info: SessionInfo) -> None:
        self._client = client
        self._sid = info.session
        # One in flight per link, for the same reason LocalLink has one - and
        # here it is load-bearing twice over: the server refuses a second call on
        # a busy session, and the client has exactly one reader.
        self._lock = asyncio.Lock()
        self.chat_name: str = info.chat_name
        self.role: Literal["master", "subagent"] = info.role
        self.build_warnings: tuple[str, ...] = info.build_warnings
        # The MCP rows as they stood when this session was built, off the same
        # answer. Not one of the immutable facts - the runtime keeps settling -
        # but the reading a Shell wants at the top of a session, and it cost no
        # round trip of its own. :meth:`mcp_statuses` takes a fresh one.
        self.build_mcp_statuses: tuple[McpStatusLine, ...] = info.mcp_statuses
        # Fired from the worker thread, inside a roundtrip. Plain attributes, no
        # lock: they are set once before the first call (the seam's "sync
        # registration") and cleared only by the thread that fires them.
        self._progress_hook: ProgressHook | None = None
        self._output_hook: Callable[[int, str], None] | None = None

    @property
    def session_id(self) -> str:
        """The server's name for this session. Diagnostics only - the Shell
        never has to compose one, because the frames carry it."""
        return self._sid

    async def _call(self, method: str, **params: Any) -> Any:
        """Serialize one call and run its roundtrip off the event loop.

        ``wire.encode_params`` is what turns keywords into a params object, so
        the per-method table stays the single source of truth for both ends: a
        parameter this side spelled wrong is a ``WireError`` here, not a
        surprise on the target.
        """
        try:
            payload = wire.encode_params(method, **params)
        except WireError as exc:
            raise EngineLinkError(LINK_PROTOCOL, str(exc)) from exc
        async with self._lock:
            return await asyncio.to_thread(self._client.roundtrip, self._sid, method, payload)

    # -- state-changing calls --------------------------------------------------

    async def start_task(self, task: str) -> Outbound:
        return await self._call("start_task", task=task)

    async def follow_up(self, text: str) -> Outbound:
        return await self._call("follow_up", text=text)

    async def ingest(self, text: str) -> IngestResult:
        return await self._call("ingest", text=text)

    async def pending(self) -> tuple[PendingAction, ...]:
        return await self._call("pending")

    async def decide(self, call_id: int, decision: Decision, note: str | None = None) -> None:
        await self._call("decide", call_id=call_id, decision=decision, note=note)

    async def execute(self) -> StepResult:
        return await self._call("execute")

    async def answer_user(self, text: str) -> StepResult:
        return await self._call("answer_user", text=text)

    async def deliver_delegate_result(
        self,
        text: str,
        *,
        status: ResultStatus = "ok",
        code: str | None = None,
    ) -> StepResult:
        return await self._call("deliver_delegate_result", text=text, status=status, code=code)

    async def undo_last_turn(
        self, *, compose_notice: bool = True
    ) -> tuple[UndoReport, Outbound | None]:
        return await self._call("undo_last_turn", compose_notice=compose_notice)

    async def status(self) -> StatusSnapshot:
        return await self._call("status")

    async def set_yolo(self, enabled: bool) -> bool:
        return await self._call("set_yolo", enabled=enabled)

    async def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        return await self._call("set_permission_mode", mode=mode)

    async def arm_extra_instructions(self) -> ArmResult:
        return await self._call("arm_extra_instructions")

    # -- the target's MCP runtime ----------------------------------------------

    async def mcp_statuses(self) -> tuple[McpStatusLine, ...]:
        """One link-scoped roundtrip, off the event loop.

        The link's lock is taken even though the call carries no session: the
        Link contract is one call in flight per link, and honouring it here
        keeps this method indistinguishable from every other await the
        controller makes. The connection's own serialization (the client's call
        lock) is what actually protects the stream.
        """
        async with self._lock:
            return await asyncio.to_thread(self._client.mcp_statuses)

    # -- out-of-band -----------------------------------------------------------

    def request_cancel(self) -> None:
        """One frame, written from the event loop while the turn is out.

        The local link's version sets a ``threading.Event`` the worker will
        notice; this one hands the same job to the server's reader thread, which
        never runs an engine method precisely so it is free to take this the
        moment it lands.
        """
        self._client.send_cancel(self._sid)

    # -- hook registration -----------------------------------------------------

    def set_progress_hook(self, hook: ProgressHook | None) -> None:
        self._progress_hook = hook

    def set_output_hook(self, hook: Callable[[int, str], None] | None) -> None:
        self._output_hook = hook

    # -- what the client dispatches to (worker thread) -------------------------

    def fire_progress(self, progress: CallProgress) -> None:
        """One ``progress`` frame, handed on under the engine's own contract.

        A hook that raises is dropped, hook and all, for the rest of the session
        - the engine does exactly this (``Engine._progress``), and the reason
        does not change with a wire in the way: a progress report is a courtesy,
        and a turn must not fail because nobody was left to watch it.
        """
        hook = self._progress_hook
        if hook is None:
            return
        try:
            hook(progress)
        except Exception:  # noqa: BLE001 - a watcher is not the turn
            self._progress_hook = None

    def fire_output(self, call_id: int, delta: str) -> None:
        hook = self._output_hook
        if hook is None:
            return
        try:
            hook(call_id, delta)
        except Exception:  # noqa: BLE001 - same contract as the progress hook
            self._output_hook = None


def _conforms(link: RemoteLink) -> Link:
    """Structural pin: mypy fails HERE if this class drifts from the Protocol.

    The tests are not type-checked, and a Protocol nothing declares is a Protocol
    nothing enforces - so the check lives in the module it is about. Costs one
    function that is never called; buys a red type-check the day a signature on
    either side of the seam changes without the other.
    """
    return link
