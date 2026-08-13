"""McpManager: the MCP client runtime - one thread, one loop, every server on it.

The shape is forced by two facts that do not negotiate (docs/design/mcp.md
sections 2 and 3).

**The SDK is optional.** `mcp` is an extra, exactly like `cv`: it drags in
httpx/anyio/pydantic, and an install without it is *healthy*, not broken. So
every `import mcp` in this module happens inside a function, never at module
scope, and an ImportError is a per-server **state** (`missing_sdk`, with the
fix named in the detail) rather than an exception anyone upstream has to
handle. With the SDK absent this manager still answers every call: no tools,
statuses that explain themselves, and `call()` raising the same typed error a
disconnected server produces.

**The SDK is async and everything above it here is not.** Tool handlers run on
the engine's worker thread (`controller._engine_call` / `asyncio.to_thread`)
and must never touch Textual's loop, so this owns a single daemon thread
running its own asyncio loop. Every SDK object - clients, transports, exit
stacks - lives there and is only ever reached through
`asyncio.run_coroutine_threadsafe`. The public surface below is entirely
synchronous and safe from any thread.

**One task per server, cradle to grave.** anyio (under the SDK) binds cancel
scopes to the task that entered them: enter a client in one task and exit it in
another and you get a RuntimeError instead of a clean close. So each server's
connect, its whole connected lifetime, and its teardown all happen in one
long-lived `_serve` task, which parks on a shutdown event in between. That also
means the connect timeout is `asyncio.timeout` (same task) and not
`asyncio.wait_for` (which would run the body in a child task).

**Timeouts are enforced here, twice.** The SDK's `read_timeout_seconds` is a
transport-level read timeout, and the in-process transport used by the tests
dispatches directly - no framing, no reads, no timeout. So `call()` wraps every
invocation in `asyncio.wait_for` on the loop *and* backstops it with a bounded
`Future.result()` on the calling thread, because a loop that wedged would
otherwise hang a tool handler forever.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentclip.mcp.types import (
    McpLocalServer,
    McpRemoteServer,
    McpServerConfig,
    McpServerState,
    McpServerStatus,
    McpToolInfo,
    sanitize,
)
from agentclip.mcp.types import (
    tool_id as composite_id,  # aliased: `tool_id` is a parameter name all over this module
)

# The one line every missing_sdk status carries. Named here so the config
# loader's warning and this can never drift (docs/design/mcp.md section 2).
MISSING_SDK_HINT = "the mcp extra is not installed - run: uv pip install 'agentclip[mcp]'"

# How long past a call's own timeout the calling thread waits before giving up
# on the loop itself. Only ever hit if the loop thread is wedged.
_CALL_SLACK_S = 2.0

# close() is called from cli.py's finally, often with a user watching a TUI tear
# down. A server that will not let go does not get to hold the process - but
# a CANCELLED server task must still get to finish killing its child: the SDK's
# stdio unwind (flush stdin, terminate, force-kill, reap) budgets ~6.5s worst
# case, and stopping the loop under it orphans the process tree on POSIX and
# leaves Proactor transports to die noisily at loop.close(). Hence two phases:
# a graceful wait, then cancel plus a second wait sized to the SDK's unwind.
_CLOSE_WAIT_S = 3.0
_CANCEL_WAIT_S = 5.0
_JOIN_S = 3.0

_DETAIL_MAX = 200

McpErrorCode = Literal["mcp_unavailable", "mcp_error", "unknown_tool"]


class McpCallError(Exception):
    """A failed `call()`, pre-classified into the codes docs/design/mcp.md
    section 8 puts on the wire. The tool layer maps `code` straight onto
    `error_result`; `message` is the one line that follows it."""

    def __init__(self, code: McpErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: McpErrorCode = code
        self.message = message


class _NeedsAuth(Exception):
    """Internal: a remote server answered 401/403. Carries the hint line."""


@dataclass
class _Record:
    """One configured server's mutable half. Guarded by McpManager._lock;
    `client` is only ever *used* on the loop thread."""

    entry: McpServerConfig
    state: McpServerState = "pending"
    detail: str = ""
    shadow_detail: str = ""  # recomputed from every server's tools, see _refresh_shadows
    tools: tuple[McpToolInfo, ...] = ()
    client: Any = None

    def status(self) -> McpServerStatus:
        detail = "; ".join(part for part in (self.detail, self.shadow_detail) if part)
        return McpServerStatus(
            name=self.entry.name,
            state=self.state,
            detail=detail,
            tool_count=len(self.tools),
        )


def _one_line(exc: BaseException) -> str:
    """An exception squashed to one human-facing status line."""
    text = " ".join(str(exc).split())
    if not text:
        text = type(exc).__name__
    else:
        text = f"{type(exc).__name__}: {text}"
    if len(text) > _DETAIL_MAX:
        text = text[: _DETAIL_MAX - 1] + "…"
    return text


async def _quiet_aclose(stack: AsyncExitStack) -> None:
    """Unwind a connection's exit stack, swallowing whatever it throws.

    Half-open clients are the failure mode this exists for: a connect that died
    after entering the transport but before the handshake finished still has a
    subprocess or a socket to release, and the exception that comes back out of
    that unwind (often a BaseExceptionGroup, sometimes a cancellation the SDK
    re-raises) tells nobody anything the connect error did not already say.
    """
    # BaseException, not Exception: an unwind that races a cancellation comes
    # back as a bare CancelledError or a BaseExceptionGroup wrapping one.
    with suppress(BaseException):
        await stack.aclose()


class McpManager:
    """The process-wide MCP runtime (docs/design/mcp.md section 3).

    Built once in `cli.py:main()`, handed to the app for status display and into
    the engine factory's closure for the tools, closed in the same `finally` as
    `host.close()`. Every method is synchronous and callable from any thread.
    """

    def __init__(
        self,
        servers: Sequence[McpServerConfig],
        project_root: Path,
        *,
        _inproc_targets: Mapping[str, object] | None = None,
    ) -> None:
        """`_inproc_targets` is a TEST SEAM and nothing else
        (docs/design/mcp.md section 7): it maps a server *name* to an object
        handed straight to `mcp.Client(...)` in place of a stdio or HTTP
        transport, which is how the suite connects to in-process `MCPServer`
        instances - real protocol, real client, no subprocess - and how it
        forces a connect failure without depending on a missing binary. No
        production caller passes it.
        """
        self._project_root = project_root
        self._inproc_targets: Mapping[str, object] = dict(_inproc_targets or {})
        self._lock = threading.Lock()
        self._records: tuple[_Record, ...] = tuple(_Record(entry=s) for s in servers)
        # A disabled entry never becomes a connection attempt, so it gets its
        # terminal state here rather than pending->...->disabled.
        for rec in self._records:
            if not rec.entry.enabled:
                rec.state = "disabled"

        self._status_hook: Callable[[McpServerStatus], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closing: asyncio.Event | None = None  # created on the loop
        # In-flight call() futures, so close() can fail them fast: a call queued
        # onto a loop that stops before running it would otherwise block its
        # worker thread for the full timeout+slack with nothing to wake it.
        self._call_futs: set[concurrent.futures.Future[str]] = set()
        self._started = False
        self._closed = False
        self._sdk_missing = False
        self._pending = 0  # enabled servers whose connect has not settled
        self._settled = threading.Event()
        self._settled.set()

    # ------------------------------------------------------------------ hooks

    def set_status_hook(self, cb: Callable[[McpServerStatus], None] | None) -> None:
        """Register the one listener told about every state change.

        Called from the loop thread for connect transitions, and from the
        *calling* thread for the two that never reach the loop (`missing_sdk`
        during ensure_started, `disabled` at construction - which precedes any
        hook and so is never delivered at all). The listener must be
        non-blocking and marshal onto its own loop, exactly as the TUI does with
        `call_from_thread` (docs/design/mcp.md section 6).
        """
        self._status_hook = cb

    def _fire(self, status: McpServerStatus) -> None:
        """Push one transition at the hook, if anyone is listening.

        Defensive by contract, mirroring ToolContext.emit_output: the hook
        crosses into the UI layer, and a view that raises (a screen torn down
        mid-connect, say) must not turn a working server into a failed one.
        """
        hook = self._status_hook
        if hook is None:
            return
        try:
            hook(status)
        except Exception:  # noqa: BLE001 - a broken listener is not the server's problem
            # ...and it is not asked again: a listener that failed once will
            # fail on every remaining transition of every remaining server.
            self._status_hook = None

    # ------------------------------------------------------------- lifecycle

    def ensure_started(self) -> None:
        """Import the SDK, start the loop thread, kick off every connect.

        Idempotent and non-blocking - it schedules the connects and returns, so
        the first session build never waits on a slow server (the design's
        "lazy but eager-on-arm"). A no-op after close().
        """
        # The flag transition rides the records lock so ensure_started and
        # close() cannot interleave: without it, a close() landing between the
        # loop being assigned and the thread starting would find nothing to
        # tear down, and the thread would then connect servers post-teardown.
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True

        try:
            # A real import statement, not importlib: the missing-extra path is
            # exactly `import mcp` failing, and that is what must be exercised.
            import mcp  # noqa: F401 - presence check; the real uses are on the loop
        except ImportError:
            self._sdk_missing = True
            for rec in self._records:
                if rec.entry.enabled:
                    self._set_state(rec, "missing_sdk", MISSING_SDK_HINT)
            return

        enabled = [rec for rec in self._records if rec.entry.enabled]
        if not enabled:
            return  # nothing to connect; _settled stays set

        with self._lock:
            # Re-checked under the lock: a close() that won the race since the
            # flag transition above must not have a loop appear behind it.
            if self._closed:
                return
            self._pending = len(enabled)
            self._settled.clear()
            # Windows: SelectorEventLoop cannot spawn subprocesses at all
            # (NotImplementedError on stdio servers), and while new_event_loop()
            # gives a Proactor loop by default on 3.11, that default is a *policy*
            # anything in-process could have replaced. A local MCP server failing
            # to spawn because some unrelated library set a policy is not a
            # failure anyone could diagnose from the status line, so we pin it.
            loop = (
                asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            )
            self._loop = loop
            self._thread = threading.Thread(
                target=self._run_loop, name="agentclip-mcp", daemon=True
            )
            self._thread.start()
        asyncio.run_coroutine_threadsafe(self._spawn_all(), loop)

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            with suppress(Exception):  # the loop is going away regardless
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _spawn_all(self) -> None:
        """On the loop: one long-lived task per enabled server, all at once."""
        self._closing = asyncio.Event()
        for rec in self._records:
            if rec.entry.enabled:
                self._tasks.append(asyncio.create_task(self._serve(rec)))

    def close(self) -> None:
        """Close every client, stop the loop, join the thread. Idempotent, and
        safe on a manager that was never started."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
            calls = list(self._call_futs)
        if loop is None or thread is None:
            return
        # Fail the in-flight (and, crucially, the queued-but-never-run) call
        # futures FIRST: a worker thread blocked in fut.result() has nothing
        # else that can wake it once the loop stops, and concurrent.futures'
        # atexit join would then hold the whole process for timeout+slack.
        for fut in calls:
            fut.cancel()
        try:
            done = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            done.result(timeout=_CLOSE_WAIT_S + _CANCEL_WAIT_S + 1.0)
        except Exception:  # noqa: BLE001 - a server that will not let go does not block exit
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_JOIN_S)

    async def _shutdown(self) -> None:
        """On the loop: release every `_serve` task from its park and wait,
        briefly, for the stacks to unwind in the tasks that entered them."""
        if self._closing is not None:
            self._closing.set()
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=_CLOSE_WAIT_S)
        for task in pending:
            task.cancel()
        if pending:
            # The cancellation has to LAND before the loop stops: the SDK's
            # stdio unwind (shielded against this very cancel) is what
            # terminates the child process tree, and returning here at
            # cancel-call time meant the loop stopped under it - orphaning the
            # tree on POSIX and leaving live Proactor transports to complain
            # all over stderr at loop.close().
            await asyncio.wait(pending, timeout=_CANCEL_WAIT_S)

    # ------------------------------------------------------------- per-server

    async def _serve(self, rec: _Record) -> None:
        """One server, start to finish, in a single task.

        Connect (under the entry's timeout), publish, then park on the shutdown
        event holding the exit stack open - because whatever entered that stack
        must also leave it from *this* task, or anyio refuses the exit.
        """
        stack = AsyncExitStack()
        timeout_s = rec.entry.timeout_ms / 1000
        self._set_state(rec, "connecting", "")
        connected = False
        try:
            try:
                # asyncio.timeout, not wait_for: wait_for would run the connect
                # in a child task, and the exit stack would then belong to a
                # task that is already gone by the time we close it.
                async with asyncio.timeout(timeout_s):
                    client, tools = await self._connect(rec, stack)
            except TimeoutError:
                self._set_state(rec, "failed", f"connect timed out after {rec.entry.timeout_ms} ms")
            except _NeedsAuth as exc:
                self._set_state(rec, "needs_auth", str(exc))
            except Exception as exc:  # noqa: BLE001 - any connect failure is one status line
                self._set_state(rec, "failed", _one_line(exc))
            else:
                connected = True
                self._set_connected(rec, client, tools)
        finally:
            # The unwind lives in the finally because CancelledError is a
            # BaseException: it sails past the except arms above, and an
            # unwind sitting after this block never runs for it - abandoning
            # a half-spawned child with no one left to terminate it. Ordinary
            # failures take the same path; `connected` guards the parked case.
            self._settle_one()
            if not connected:
                await _quiet_aclose(stack)

        if not connected:
            return
        try:
            assert self._closing is not None
            await self._closing.wait()
        finally:
            with self._lock:
                rec.client = None
            await _quiet_aclose(stack)

    async def _connect(
        self, rec: _Record, stack: AsyncExitStack
    ) -> tuple[Any, tuple[McpToolInfo, ...]]:
        """Open one client and cache its tool listing. Runs on the loop."""
        # From the defining submodule, not the package root that re-exports it:
        # `tests/mcp/` is a package on the lint config's src roots, so a bare
        # `from mcp import Client` gets sorted as if it were first-party.
        from mcp.client import Client

        entry = rec.entry
        timeout_s = entry.timeout_ms / 1000
        # Any, not object: the seam hands Client whatever a test built, and the
        # SDK's own union of accepted targets is not ours to re-declare.
        target: Any = self._inproc_targets.get(entry.name)
        if target is not None:  # test seam, see __init__
            client = await stack.enter_async_context(
                Client(target, read_timeout_seconds=timeout_s)
            )
        elif isinstance(entry, McpLocalServer):
            client = await self._open_local(entry, stack, timeout_s)
        else:
            client = await self._open_remote(entry, stack, timeout_s)

        listing = await client.list_tools()
        tools = tuple(
            McpToolInfo(
                id=composite_id(entry.name, tool.name),
                server=entry.name,
                name=tool.name,
                description=tool.description or "",
                # Compact on purpose: this text is re-emitted into a paste
                # budget measured in characters (docs/design/mcp.md section 5).
                input_schema_json=json.dumps(tool.input_schema, separators=(",", ":")),
            )
            for tool in listing.tools
        )
        return client, tools

    async def _open_local(
        self, entry: McpLocalServer, stack: AsyncExitStack, timeout_s: float
    ) -> Any:
        """A stdio server THIS PC spawns (docs/design/mcp.md section 1)."""
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters, stdio_client

        argv = list(entry.command)
        command, args = argv[0], argv[1:]

        # The SDK merges `env` over its own *filtered* default environment, not
        # over os.environ - so a server that needs PATH-adjacent variables the
        # filter drops would silently misbehave. Pass the real environment
        # explicitly, with the entry's overlay on top.
        env = {**os.environ, **dict(entry.environment)}

        cwd = entry.cwd
        if not cwd:
            work = self._project_root
        else:
            candidate = Path(cwd)
            work = candidate if candidate.is_absolute() else self._project_root / candidate

        params = StdioServerParameters(command=command, args=args, env=env, cwd=str(work))
        # StdioServerParameters is not itself a transport - Client takes the
        # context manager stdio_client() returns.
        return await stack.enter_async_context(
            Client(stdio_client(params), read_timeout_seconds=timeout_s)
        )

    async def _open_remote(
        self, entry: McpRemoteServer, stack: AsyncExitStack, timeout_s: float
    ) -> Any:
        """Streamable HTTP first, one SSE attempt after (design section 1).

        SDK 2.0 dropped the automatic SSE fallback its 1.x line had, so
        OpenCode's transport probing is reimplemented here: try the modern
        transport, and on a non-auth failure try the legacy one exactly once.
        A 401/403 short-circuits both - retrying a rejected credential on
        another transport only produces a second rejection and a worse status
        line.
        """
        # There is no typed auth failure anywhere in this SDK: a 401 surfaces as
        # a generic MCPError("Server returned an error response"), which is
        # indistinguishable from a dozen other server errors. So the status code
        # is observed where it actually exists - on the HTTP response - and
        # parked in this cell for the failure path to consult.
        auth_rejected = [False]

        # BaseException, not Exception, on both unwinds: until the last line
        # hands `attempt` to the caller's stack, nobody else can close what it
        # holds - and a cancellation (the timeout in _serve, or shutdown) is
        # exactly when an entered httpx client would otherwise be abandoned.
        attempt = AsyncExitStack()
        try:
            client = await self._open_streamable(entry, attempt, timeout_s, auth_rejected)
        except BaseException as exc:
            await _quiet_aclose(attempt)
            if not isinstance(exc, Exception):
                raise  # cancellation: unwound, now get out of the way
            if auth_rejected[0]:
                raise self._needs_auth(entry) from exc
            attempt = AsyncExitStack()
            try:
                client = await self._open_sse(entry, attempt, timeout_s)
            except BaseException:
                await _quiet_aclose(attempt)
                raise  # the SSE error is the more recent one; both were the same server
        stack.push_async_callback(attempt.aclose)
        return client

    async def _open_streamable(
        self,
        entry: McpRemoteServer,
        stack: AsyncExitStack,
        timeout_s: float,
        auth_rejected: list[bool],
    ) -> Any:
        import httpx2
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client

        # Private module, deliberately: it is the only place the SDK's own
        # redirect/timeout defaults for MCP live, and duplicating them here
        # would silently drift from the transport they were chosen for.
        from mcp.shared._httpx_utils import create_mcp_http_client

        async def _note_status(response: Any) -> None:
            if response.status_code in (401, 403):
                auth_rejected[0] = True

        http = create_mcp_http_client(
            headers=dict(entry.headers),
            timeout=httpx2.Timeout(timeout_s),
        )
        http.event_hooks = {"response": [_note_status]}
        # Headers and timeouts ride the httpx client, not the transport; and a
        # caller-provided client is one the transport will not close, so its
        # lifetime is ours.
        await stack.enter_async_context(http)
        client = await stack.enter_async_context(
            Client(
                streamable_http_client(entry.url, http_client=http),
                read_timeout_seconds=timeout_s,
            )
        )
        if auth_rejected[0]:
            raise self._needs_auth(entry)
        return client

    async def _open_sse(
        self, entry: McpRemoteServer, stack: AsyncExitStack, timeout_s: float
    ) -> Any:
        from mcp.client import Client
        from mcp.client.sse import sse_client

        # sse_client builds its own httpx client, so the auth cell cannot see
        # this leg. That costs nothing: a server that rejects credentials does
        # so on the streamable attempt, which is classified before we get here.
        return await stack.enter_async_context(
            Client(
                sse_client(entry.url, headers=dict(entry.headers), timeout=timeout_s),
                read_timeout_seconds=timeout_s,
            )
        )

    def _needs_auth(self, entry: McpRemoteServer) -> _NeedsAuth:
        hint = "server rejected the request (401/403); add credentials to this server's headers"
        if entry.oauth:
            # OpenCode would have run an OAuth flow here; phase 1 has none
            # (docs/design/mcp.md section 9), so say who can.
            hint += " or authenticate it once in OpenCode"
        return _NeedsAuth(hint)

    # ------------------------------------------------------------ state edits

    def _set_state(self, rec: _Record, state: McpServerState, detail: str) -> None:
        with self._lock:
            rec.state = state
            rec.detail = detail
            status = rec.status()
        self._fire(status)

    def _set_connected(self, rec: _Record, client: Any, tools: tuple[McpToolInfo, ...]) -> None:
        with self._lock:
            rec.state = "connected"
            rec.detail = ""
            rec.client = client
            rec.tools = tools
            # This server's arrival can shadow - or be shadowed by - any other,
            # and connects finish in whatever order the servers answer in, so
            # the whole picture is recomputed rather than appended to.
            changed = self._refresh_shadows_locked()
            status = rec.status()
            others = [other.status() for other in changed if other is not rec]
        self._fire(status)
        for other_status in others:
            self._fire(other_status)

    def _settle_one(self) -> None:
        with self._lock:
            self._pending -= 1
            done = self._pending <= 0
        if done:
            self._settled.set()

    def _refresh_shadows_locked(self) -> list[_Record]:
        """Recompute every record's shadow warning; return those that changed.

        Duplicate composite ids are possible whenever two server names sanitize
        to the same thing (`"a b"` and `"a_b"` both become `a_b`), and the
        winner must be a property of the *config*, not of who answered first -
        so this walks records in config order, every time.
        """
        seen: dict[str, str] = {}
        losses: dict[str, list[str]] = {}
        for rec in self._records:
            for info in rec.tools:
                owner = seen.get(info.id)
                if owner is None:
                    seen[info.id] = rec.entry.name
                else:
                    losses.setdefault(rec.entry.name, []).append(f"{info.id} (from {owner!r})")
        changed = []
        for rec in self._records:
            lost = losses.get(rec.entry.name, [])
            detail = f"shadowed tool ids: {', '.join(lost)}" if lost else ""
            if detail != rec.shadow_detail:
                rec.shadow_detail = detail
                changed.append(rec)
        return changed

    # ---------------------------------------------------------------- queries

    def statuses(self) -> tuple[McpServerStatus, ...]:
        """One entry per configured server, in config order, including disabled
        ones. Never blocks and never waits on a connect."""
        with self._lock:
            self._refresh_shadows_locked()
            return tuple(rec.status() for rec in self._records)

    def tools(self) -> tuple[McpToolInfo, ...]:
        """Every connected server's cached tools, config order, deduped.

        Does not wait for pending connects: a tool that is not live yet simply
        is not listed, and the bootstrap that measured the listing is the same
        one the model gets.
        """
        with self._lock:
            return self._visible_locked()

    def schema(self, tool_id: str) -> McpToolInfo | None:
        with self._lock:
            for info in self._visible_locked():
                if info.id == tool_id:
                    return info
        return None

    def _likely_owner_locked(self, tool_id: str) -> _Record | None:
        """The configured server an unrecognized composite id most plausibly
        names: longest `sanitize(server) + "_"` prefix match, first server in
        config order on a tie (the same tie-break duplicate ids get). None when
        no configured server's name prefixes the id at all."""
        best: _Record | None = None
        best_len = -1
        for rec in self._records:
            prefix = sanitize(rec.entry.name) + "_"
            if tool_id.startswith(prefix) and len(prefix) > best_len:
                best, best_len = rec, len(prefix)
        return best

    def _visible_locked(self) -> tuple[McpToolInfo, ...]:
        seen: set[str] = set()
        out: list[McpToolInfo] = []
        for rec in self._records:
            for info in rec.tools:
                if info.id in seen:
                    continue  # first server in CONFIG order wins, always
                seen.add(info.id)
                out.append(info)
        return tuple(out)

    def wait_ready(self, timeout_s: float) -> bool:
        """Block until every scheduled connect has settled (succeeded or not).

        A convenience for tests and for a caller that genuinely wants tools
        before it renders; the normal path never calls it. Returns immediately
        when there is nothing to wait for.
        """
        if not self._started or self._sdk_missing or self._closed:
            return True
        return self._settled.wait(timeout_s)

    # ------------------------------------------------------------------- call

    def call(self, tool_id: str, args: dict[str, Any]) -> str:
        """Invoke one MCP tool and return its text content.

        Called from the engine's worker thread. Everything that can go wrong
        arrives as `McpCallError` with one of section 8's codes - a tool handler
        should never see an SDK exception, a timeout, or a `None`.
        """
        if self._sdk_missing:
            raise McpCallError("mcp_unavailable", MISSING_SDK_HINT)
        loop = self._loop
        if self._closed or not self._started or loop is None:
            raise McpCallError("mcp_unavailable", "the MCP runtime is not running")

        with self._lock:
            info = next((i for i in self._visible_locked() if i.id == tool_id), None)
            rec = None
            if info is not None:
                rec = next((r for r in self._records if r.entry.name == info.server), None)
            state = rec.state if rec is not None else ""
            client = rec.client if rec is not None else None
            timeout_ms = rec.entry.timeout_ms if rec is not None else 0
            owner = None if info is not None else self._likely_owner_locked(tool_id)
            owner_name = owner.entry.name if owner is not None else ""
            owner_state = owner.state if owner is not None else ""

        if info is None or rec is None:
            # An id no connected server exports can still *belong* to a server
            # everyone can see is down - its tools were never listed, so the
            # cache cannot know them. When the id's prefix names a configured
            # server that is not connected, the honest code is mcp_unavailable
            # naming its status, not unknown_tool (docs/design/mcp.md section 8).
            if owner is not None and owner_state != "connected":
                raise McpCallError(
                    "mcp_unavailable",
                    f"MCP server {owner_name!r} is {owner_state}, not connected",
                )
            raise McpCallError("unknown_tool", f"no connected MCP server exports {tool_id!r}")
        if state != "connected" or client is None:
            raise McpCallError(
                "mcp_unavailable",
                f"MCP server {info.server!r} is {state or 'unknown'}, not connected",
            )

        timeout_s = timeout_ms / 1000
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._call(client, info.name, args, timeout_s, timeout_ms), loop
            )
        except RuntimeError:
            # The loop closed between the _closed check above and here - the
            # gap is real (close() runs on another thread) and this is the
            # error shape it produces. A handler must see section 8's codes,
            # never an SDK/loop internal.
            raise McpCallError("mcp_unavailable", "the MCP runtime is shutting down") from None
        with self._lock:
            if self._closed:
                # close() may already have swept _call_futs; a future added
                # after the sweep would block its thread on a loop that will
                # never run it. Refuse instead.
                fut.cancel()
                raise McpCallError("mcp_unavailable", "the MCP runtime is shutting down")
            self._call_futs.add(fut)
        try:
            return fut.result(timeout=timeout_s + _CALL_SLACK_S)
        except concurrent.futures.CancelledError:
            # close() cancelled us to free this thread; same story as above.
            raise McpCallError("mcp_unavailable", "the MCP runtime is shutting down") from None
        except concurrent.futures.TimeoutError:
            # The loop itself did not answer - the in-loop wait_for should have
            # fired first. Drop the call rather than leak a running request.
            fut.cancel()
            raise McpCallError("mcp_error", f"tool call timed out after {timeout_ms} ms") from None
        finally:
            with self._lock:
                self._call_futs.discard(fut)

    async def _call(
        self, client: Any, name: str, args: dict[str, Any], timeout_s: float, timeout_ms: int
    ) -> str:
        try:
            # wait_for and not read_timeout_seconds: the in-process transport
            # dispatches without any read at all, so the SDK's own timeout can
            # never fire there (docs/design/mcp.md section 7).
            result = await asyncio.wait_for(client.call_tool(name, args), timeout_s)
        except TimeoutError:
            raise McpCallError("mcp_error", f"tool call timed out after {timeout_ms} ms") from None
        except Exception as exc:  # noqa: BLE001 - MCPError and transport faults read the same here
            raise McpCallError("mcp_error", _one_line(exc)) from None

        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        # call_tool does NOT raise on a tool-level failure; is_error is the only
        # signal, and the text content is the server's explanation of it.
        if result.is_error:
            raise McpCallError("mcp_error", text or "the server reported an error")
        return text
