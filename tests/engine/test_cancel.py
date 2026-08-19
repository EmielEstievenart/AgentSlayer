"""Engine-level cooperative cancellation (Engine.request_cancel).

The user pressing cancel mid-batch must produce a *complete story* for the
model rather than silence: the interrupted call reports code=cancelled with
whatever it had, every later call says explicitly that it never ran, and the
turn still ends through the normal Send path so the results get pasted back.

A fake slow tool stands in for run_command here (the real subprocess kill is
covered in tests/executor/tools/test_shell.py); it blocks exactly like a long command,
polling the same ctx.cancelled() flag.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentclip.engine.engine import Done, Engine, NewTurn, Phase, Send
from agentclip.executor.tools.registry import (
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
    default_registry,
    tool_handler,
)
from agentclip.protocol.types import ToolCall

from ..conftest import write_permissions

# The conftest make_engine fixture (its EngineFactory alias lives there, but
# `tests` is not an importable package - so spell the callable out).
EngineFactory = Callable[..., Engine]

SLOW_DOC = """\
slow_tool()
  Blocks until released or cancelled (test double).
===CLIP:CALL id=1 tool=slow_tool===
===CLIP:END==="""

REPLY_SLOW_BATCH = """Running the slow thing, then reading and finishing.

===CLIP:CALL id=1 tool=slow_tool===
===CLIP:END===
===CLIP:CALL id=2 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=3 tool=task_done===
summary: all done
===CLIP:END===
===CLIP:EOM calls=3 chat=amber-falcon===
"""

REPLY_AFTER_CANCEL = """Fine, trying the slow thing once more.

===CLIP:CALL id=1 tool=slow_tool===
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def _slow_tool(hold: threading.Event) -> tuple[ToolSpec, threading.Event]:
    """A handler that blocks like a long command: it finishes when ``hold`` is
    set, and bails out with code=cancelled the moment the engine's cancel flag
    fires. Returns the spec and the event it sets once it is actually running."""
    started = threading.Event()

    @tool_handler
    def handler(ctx: ToolContext, call: ToolCall) -> str:
        started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ctx.cancelled():
                raise ToolError(
                    "cancelled",
                    "cancelled by the user before completion (killed after 0.3s)\n"
                    "partial output (tail):\nhalf-done",
                    "the user stopped this deliberately - do not re-run it unchanged.",
                )
            if hold.wait(0.02):
                return "finished on its own"
        raise ToolError("exec_timeout", "the fake slow tool was never released", "fix the test.")

    return ToolSpec("slow_tool", "auto", handler, None, SLOW_DOC), started


def _registry_with(extra: ToolSpec) -> ToolRegistry:
    base = default_registry()
    specs = [s for s in (base.get(name) for name in base.names()) if s is not None]
    return ToolRegistry([*specs, extra])


def _armed_engine(project: Path, make_engine: EngineFactory, extra: ToolSpec) -> Engine:
    # The stand-in tool is in no permission table, so it answers to its own name
    # and would otherwise gate like anything nobody has decided about.
    write_permissions(project, {"permission": {extra.name: "allow"}})
    engine = make_engine(tools=_registry_with(extra))
    engine.start_task("Do the slow thing.")
    return engine


def test_cancel_interrupts_the_running_call_and_skips_the_rest(
    project: Path,
    make_engine: EngineFactory,
) -> None:
    hold = threading.Event()
    spec, started = _slow_tool(hold)
    engine = _armed_engine(project, make_engine, spec)
    assert isinstance(engine.ingest(REPLY_SLOW_BATCH), NewTurn)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(engine.execute)  # the host's worker thread
        assert started.wait(10), "the slow tool never started"
        engine.request_cancel()  # ...cancelled from another thread, mid-execute
        step = running.result(timeout=20)

    # The turn finishes NORMALLY - not as Done (task_done was skipped, so the
    # cancelled batch cannot declare the task complete) and not as an abort.
    assert isinstance(step, Send)
    assert not isinstance(step, Done)
    payload = step.outbound.chunks[0]

    # The interrupted call: its own error result, with what it managed to do.
    assert "===CLIP:RESULT id=1 status=error code=cancelled===" in payload
    assert "cancelled by the user before completion" in payload
    assert "half-done" in payload
    # The calls that never ran say so explicitly, with the same code.
    assert "===CLIP:RESULT id=2 status=error code=cancelled===" in payload
    assert "===CLIP:RESULT id=3 status=error code=cancelled===" in payload
    assert "skipped: the user cancelled this batch" in payload
    assert "demo project for engine tests" not in payload  # README was never read

    status = engine.status()
    assert status.phase is Phase.AWAITING_REPLY  # armed for the model's answer


def test_a_normal_run_after_a_cancelled_one(project: Path, make_engine: EngineFactory) -> None:
    """The cancel flag is per-run: the next turn must execute untouched."""
    hold = threading.Event()
    spec, started = _slow_tool(hold)
    engine = _armed_engine(project, make_engine, spec)
    assert isinstance(engine.ingest(REPLY_SLOW_BATCH), NewTurn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(engine.execute)
        assert started.wait(10)
        engine.request_cancel()
        assert isinstance(running.result(timeout=20), Send)

    hold.set()  # the next run's slow tool completes on its own
    assert isinstance(engine.ingest(REPLY_AFTER_CANCEL), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=ok===" in payload
    assert "finished on its own" in payload
    assert "cancelled" not in payload


def test_cancel_while_nothing_executes_is_a_no_op(project: Path, make_engine: EngineFactory) -> None:
    hold = threading.Event()
    hold.set()
    spec, _ = _slow_tool(hold)
    engine = _armed_engine(project, make_engine, spec)
    engine.request_cancel()  # nothing is running - must not poison the next run
    assert isinstance(engine.ingest(REPLY_AFTER_CANCEL), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert "===CLIP:RESULT id=1 status=ok===" in step.outbound.chunks[0]
