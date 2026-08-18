"""The Shell<->Engine link: the seam a remote engine will be spoken to across.

The Shell (clipboard watcher, driver, GUI/TUI) must stay on the machine the
browser runs on; the Engine and its Executor may not (docs/design/remote-executor.md
section 2.1). This module is where that line is drawn - the ONE surface the
Shell uses to drive a session - so that a later increment can put a wire
underneath it without the controller learning a new vocabulary:

* :class:`LocalLink` - today's mode, the engine in this process (section 2.2);
* ``RemoteLink`` - later, the same calls as messages over an SSH exec channel;
* ``FakeLink`` - a test double for shell tests that want no engine at all.

Only the Shell's own surface is here. Everything below the engine (tool
handlers, ``ToolContext``, the Host seam) stays where it is: the investigation
behind the design doc found the engine welded to its executor by in-process
calls, so the cut is ABOVE the engine, never between it and its tools.

Threading contract - unchanged from what ``SessionController._engine_call``
used to guarantee, just moved down here where every implementation inherits it:

* the **async** methods are the state-changing ones. They are called from the
  event loop, at most one is in flight at a time (the link serializes them), and
  a link is free to take minutes over one - ``execute()`` runs a whole plan of
  tool calls. :class:`LocalLink` honours "never block the loop" with
  ``asyncio.to_thread``; a remote link will be waiting on a socket instead.
* :meth:`Link.request_cancel` is **sync and out-of-band**: it must stay callable
  while an async method is in flight, so it is never gated by the serializing
  lock. That is the whole reason it is not async - the call it interrupts is the
  one holding the lock.
* the two hook setters are **sync registration**, wired once right after the
  link is built and before the first async call. The hooks themselves keep
  today's contract, which is the engine's: they fire FROM THE WORKER THREAD in
  the middle of ``execute()``, so a hook may not block and may not touch the
  event loop, and one that raises is silently dropped for the rest of the
  session (a progress watcher must never fail a turn).

Exceptions cross the seam unchanged - ``BudgetExceeded`` out of
:meth:`Link.start_task`, ``EngineStateError`` out of
:meth:`Link.undo_last_turn` - because the controller catches exactly those and
turns them into transcript errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, Protocol, TypeVar

from agentclip.engine.approval import PermissionMode
from agentclip.engine.engine import (
    ArmResult,
    Engine,
    IngestResult,
    PendingAction,
    ProgressHook,
    StatusSnapshot,
    StepResult,
)
from agentclip.engine.states import Decision
from agentclip.engine.store.backups import UndoReport
from agentclip.protocol.types import Outbound, ResultStatus

_T = TypeVar("_T")


class Link(Protocol):
    """What a Shell may ask of an engine, wherever that engine is running."""

    # -- immutable per-session facts, snapshotted once -------------------------
    # Read straight off the link from the event loop (the transcript notes and
    # the noise toasts need them the moment a session arms), which is only safe
    # because they cannot change for the life of a session - so a remote link
    # can carry them home in the handshake instead of paying a round trip.

    chat_name: str
    role: Literal["master", "subagent"]
    build_warnings: tuple[str, ...]

    # -- state-changing calls (serialized, one in flight) ----------------------

    async def start_task(self, task: str) -> Outbound: ...

    async def follow_up(self, text: str) -> Outbound: ...

    async def ingest(self, text: str) -> IngestResult: ...

    async def pending(self) -> tuple[PendingAction, ...]: ...

    async def decide(self, call_id: int, decision: Decision, note: str | None = None) -> None: ...

    async def execute(self) -> StepResult: ...

    async def answer_user(self, text: str) -> StepResult: ...

    async def deliver_delegate_result(
        self,
        text: str,
        *,
        status: ResultStatus = "ok",
        code: str | None = None,
    ) -> StepResult: ...

    async def undo_last_turn(
        self, *, compose_notice: bool = True
    ) -> tuple[UndoReport, Outbound | None]: ...

    async def status(self) -> StatusSnapshot: ...

    async def set_yolo(self, enabled: bool) -> bool: ...

    async def set_permission_mode(self, mode: PermissionMode) -> PermissionMode: ...

    async def arm_extra_instructions(self) -> ArmResult: ...

    # -- out-of-band, from the event loop, mid-call ---------------------------

    def request_cancel(self) -> None:
        """Ask the turn running right now to stop. NEVER serialized: the call it
        interrupts is the one holding the link's lock (see the module
        docstring). A no-op when nothing is executing."""
        ...

    # -- hook registration (sync, wired once after construction) ---------------

    def set_progress_hook(self, hook: ProgressHook | None) -> None: ...

    def set_output_hook(self, hook: Callable[[int, str], None] | None) -> None: ...


class LocalLink:
    """The link when the engine is in this process: a lock and a thread hop.

    Both halves used to live in ``SessionController._engine_call`` and are here
    for one reason - they are the local ANSWER to a question the seam asks of
    every implementation ("one call at a time, and do not block the loop"), not
    a fact about the controller. A remote link answers the same question with a
    request id and a socket read.

    The engine is deliberately reachable as :attr:`engine`: in local mode there
    is no wire to hide, and tests that want the real object (the factory's own
    tests, which assert on what was BUILT) should not have to fake one.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        # One in flight per link. Per-link rather than per-controller because
        # the link IS the resource being serialized; the controller only ever
        # calls the session that is currently live, so this is exactly the
        # single-flight guarantee it used to enforce itself.
        self._lock = asyncio.Lock()
        # Snapshotted, not proxied: immutable for the engine's lifetime, and the
        # controller reads them from the event loop with no await to spare.
        self.chat_name: str = engine.chat_name
        self.role: Literal["master", "subagent"] = engine.role
        self.build_warnings: tuple[str, ...] = engine.build_warnings

    async def _call(self, fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        """Serialize one engine call and run it off the event loop."""
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # -- state-changing calls --------------------------------------------------

    async def start_task(self, task: str) -> Outbound:
        return await self._call(self.engine.start_task, task)

    async def follow_up(self, text: str) -> Outbound:
        return await self._call(self.engine.follow_up, text)

    async def ingest(self, text: str) -> IngestResult:
        return await self._call(self.engine.ingest, text)

    async def pending(self) -> tuple[PendingAction, ...]:
        return await self._call(self.engine.pending)

    async def decide(self, call_id: int, decision: Decision, note: str | None = None) -> None:
        await self._call(self.engine.decide, call_id, decision, note)

    async def execute(self) -> StepResult:
        return await self._call(self.engine.execute)

    async def answer_user(self, text: str) -> StepResult:
        return await self._call(self.engine.answer_user, text)

    async def deliver_delegate_result(
        self,
        text: str,
        *,
        status: ResultStatus = "ok",
        code: str | None = None,
    ) -> StepResult:
        return await self._call(
            self.engine.deliver_delegate_result, text, status=status, code=code
        )

    async def undo_last_turn(
        self, *, compose_notice: bool = True
    ) -> tuple[UndoReport, Outbound | None]:
        return await self._call(self.engine.undo_last_turn, compose_notice=compose_notice)

    async def status(self) -> StatusSnapshot:
        return await self._call(self.engine.status)

    async def set_yolo(self, enabled: bool) -> bool:
        return await self._call(self.engine.set_yolo, enabled)

    async def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        return await self._call(self.engine.set_permission_mode, mode)

    async def arm_extra_instructions(self) -> ArmResult:
        return await self._call(self.engine.arm_extra_instructions)

    # -- out-of-band -----------------------------------------------------------

    def request_cancel(self) -> None:
        """Straight through, no lock: ``Engine.request_cancel`` is thread-safe by
        construction (it sets a ``threading.Event``) precisely so the event loop
        can call it while the worker thread is inside ``execute()``."""
        self.engine.request_cancel()

    # -- hook registration -----------------------------------------------------

    def set_progress_hook(self, hook: ProgressHook | None) -> None:
        self.engine.set_progress_hook(hook)

    def set_output_hook(self, hook: Callable[[int, str], None] | None) -> None:
        self.engine.set_output_hook(hook)
