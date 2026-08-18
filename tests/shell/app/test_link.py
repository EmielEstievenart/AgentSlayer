"""LocalLink: the Shell<->Engine seam, tested against a real Engine.

Five promises, one test each - they are what the controller stopped enforcing
itself when the lock and the thread hop moved down here
(docs/design/remote-executor.md section 2.2), and what ``RemoteLink`` will have
to make good on over a wire:

* the async methods answer with the engine's own values;
* they are serialized - one in flight, never interleaved;
* ``request_cancel`` is out-of-band and reaches the engine WHILE a call holds
  the lock, which is the whole reason it is not async;
* the immutable session facts are readable synchronously, with no await;
* exceptions cross unchanged, because the controller catches them by type.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agentclip.engine.states import EngineStateError
from agentclip.shell.app.link import LocalLink


async def test_async_calls_answer_with_the_engines_own_values(make_engine) -> None:
    engine = make_engine()
    link = LocalLink(engine)

    assert await link.set_yolo(True) is True
    snap = await link.status()
    assert snap.yolo is True
    assert snap.turn == 0
    assert snap.session_dir == engine.status().session_dir
    assert await link.set_permission_mode("plan") == "plan"
    assert (await link.status()).mode == "plan"


async def test_two_calls_serialize_instead_of_interleaving(make_engine) -> None:
    """One in flight, in call order: a second caller may not walk into the
    middle of a turn the engine is halfway through."""
    engine = make_engine()
    link = LocalLink(engine)
    trace: list[str] = []

    def slow(enabled: bool) -> bool:
        trace.append(f"enter {enabled}")
        # Long enough that an unserialized second call would land inside this
        # one on its own thread; short enough to stay a millisecond test.
        time.sleep(0.05)
        trace.append(f"exit {enabled}")
        return enabled

    engine.set_yolo = slow  # type: ignore[method-assign]
    assert await asyncio.gather(link.set_yolo(True), link.set_yolo(False)) == [True, False]
    assert trace == ["enter True", "exit True", "enter False", "exit False"]


async def test_request_cancel_lands_while_a_call_holds_the_lock(make_engine) -> None:
    """The out-of-band half of the seam. A ``request_cancel`` that had to wait
    for the lock would wait for the very call it exists to interrupt."""
    engine = make_engine()
    link = LocalLink(engine)
    entered = threading.Event()
    release = threading.Event()
    cancels: list[None] = []
    real_cancel = engine.request_cancel

    def spy() -> None:
        cancels.append(None)
        real_cancel()

    def blocking(enabled: bool) -> bool:
        entered.set()
        assert release.wait(5), "the cancel never came"
        return enabled

    engine.request_cancel = spy  # type: ignore[method-assign]
    engine.set_yolo = blocking  # type: ignore[method-assign]

    call = asyncio.create_task(link.set_yolo(True))
    assert await asyncio.to_thread(entered.wait, 5)  # the worker thread is inside the call
    link.request_cancel()  # from the event loop, mid-call: must not block
    assert cancels == [None]
    release.set()
    assert await asyncio.wait_for(call, 5) is True


async def test_the_session_facts_need_no_await(make_engine) -> None:
    """Immutable for the session, so they are snapshotted at construction and
    read straight off the link - the transcript notes that quote them run on the
    event loop with nothing to await."""
    engine = make_engine()
    link = LocalLink(engine)

    assert link.chat_name == engine.chat_name
    assert link.role == "master"
    assert link.build_warnings == engine.build_warnings

    sub = LocalLink(make_engine(role="subagent"))
    assert sub.role == "subagent"


async def test_engine_exceptions_cross_the_seam_unchanged(make_engine) -> None:
    """The controller catches ``EngineStateError`` by type and turns it into a
    toast; a link that wrapped it would silently break that branch."""
    link = LocalLink(make_engine())
    await link.start_task("t")  # IDLE -> AWAITING_REPLY, so undo is legal to ASK for

    with pytest.raises(EngineStateError, match="nothing to undo"):
        await link.undo_last_turn()


async def test_the_hooks_are_wired_straight_through(make_engine) -> None:
    """Sync registration, and the engine keeps its own hook contract behind it
    (worker-thread callbacks, a raising hook dropped) - the link only points
    them at the view."""
    engine = make_engine()
    link = LocalLink(engine)

    def on_progress(progress: object) -> None: ...

    def on_output(call_id: int, chunk: str) -> None: ...

    link.set_progress_hook(on_progress)  # type: ignore[arg-type]
    link.set_output_hook(on_output)
    assert engine._progress_hook is on_progress
    assert engine._ctx.on_output is on_output
