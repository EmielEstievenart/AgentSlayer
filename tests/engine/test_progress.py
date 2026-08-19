"""Engine.set_progress_hook: the turn narrating itself as it executes.

The hook is the only thing that says anything at all between "the user approved
the batch" and "here are the results" - which, for a turn holding a five-minute
command, is most of the turn. So what matters here is not that it fires but
that it fires for EVERY call and in the order they run: a row on screen that
never resolves is worse than no row at all.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agentclip.engine.engine import CallProgress, Decision, Engine, NewTurn

EngineFactory = Callable[..., Engine]

REPLY_THREE_CALLS = """Reading, editing and finishing.

===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: notes.txt
content: hello
===CLIP:END===
===CLIP:CALL id=3 tool=task_done===
summary: done
===CLIP:END===
===CLIP:EOM calls=3 chat=amber-falcon===
"""

REPLY_READ_THEN_EDIT = """Reading, then editing.

===CLIP:CALL id=1 tool=write_file===
path: a.txt
content: one
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: b.txt
content: two
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

REPLY_PLAN_MODE = """A read and a write.

===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: notes.txt
content: hello
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
"""

REPLY_UNKNOWN_TOOL = """Trying something that does not exist.

===CLIP:CALL id=1 tool=teleport===
where: mars
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def _record(engine: Engine) -> list[CallProgress]:
    seen: list[CallProgress] = []
    engine.set_progress_hook(seen.append)
    return seen


def _steps(seen: list[CallProgress]) -> list[tuple[int, str, str]]:
    return [(p.call_id, p.phase, p.status) for p in seen]


def test_every_call_reports_running_then_done_in_plan_order(
    project: Path, make_engine: EngineFactory
) -> None:
    engine = make_engine()
    seen = _record(engine)
    engine.start_task("do the thing")
    assert isinstance(engine.ingest(REPLY_THREE_CALLS), NewTurn)
    for action in engine.pending():
        engine.decide(action.call.id, Decision.APPROVE)
    engine.execute()

    assert _steps(seen) == [
        (1, "running", ""),
        (1, "done", "ok"),
        (2, "running", ""),
        (2, "done", "ok"),
        # task_done never enters a handler, so it only ever resolves.
        (3, "done", "ok"),
    ]


def test_a_rejected_call_and_the_ones_skipped_behind_it_all_resolve(
    project: Path, make_engine: EngineFactory
) -> None:
    """The queue must empty on screen even when nothing after the gate runs."""
    engine = make_engine()
    seen = _record(engine)
    engine.start_task("write two files")
    assert isinstance(engine.ingest(REPLY_READ_THEN_EDIT), NewTurn)
    engine.decide(1, Decision.REJECT, "no thanks")
    engine.execute()

    assert _steps(seen) == [(1, "done", "denied"), (2, "done", "skipped")]


def test_a_call_denied_by_plan_mode_resolves_without_ever_running(
    project: Path, make_engine: EngineFactory
) -> None:
    engine = make_engine()
    engine.set_permission_mode("plan")
    seen = _record(engine)
    engine.start_task("read and write")
    assert isinstance(engine.ingest(REPLY_PLAN_MODE), NewTurn)
    engine.execute()

    assert _steps(seen) == [(1, "running", ""), (1, "done", "ok"), (2, "done", "denied")]


def test_a_pre_resolved_unknown_tool_resolves_too(
    project: Path, make_engine: EngineFactory
) -> None:
    engine = make_engine()
    seen = _record(engine)
    engine.start_task("teleport")
    assert isinstance(engine.ingest(REPLY_UNKNOWN_TOOL), NewTurn)
    engine.execute()

    assert _steps(seen) == [(1, "done", "error")]


def test_the_hook_carries_the_tool_name_each_row_is_labelled_with(
    project: Path, make_engine: EngineFactory
) -> None:
    engine = make_engine()
    seen = _record(engine)
    engine.start_task("do the thing")
    assert isinstance(engine.ingest(REPLY_THREE_CALLS), NewTurn)
    for action in engine.pending():
        engine.decide(action.call.id, Decision.APPROVE)
    engine.execute()

    assert {(p.call_id, p.tool) for p in seen} == {
        (1, "read_file"),
        (2, "write_file"),
        (3, "task_done"),
    }


def test_a_watcher_that_raises_is_dropped_and_the_turn_finishes(
    project: Path, make_engine: EngineFactory
) -> None:
    """A dead UI must not be able to fail a turn that is doing real work."""
    engine = make_engine()

    def explode(_progress: CallProgress) -> None:
        raise RuntimeError("the screen went away")

    engine.set_progress_hook(explode)
    engine.start_task("do the thing")
    assert isinstance(engine.ingest(REPLY_THREE_CALLS), NewTurn)
    for action in engine.pending():
        engine.decide(action.call.id, Decision.APPROVE)
    step = engine.execute()

    assert getattr(step, "summary", "") == "done"
    assert (project / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_no_hook_is_the_default_and_costs_nothing(
    project: Path, make_engine: EngineFactory
) -> None:
    engine = make_engine()
    engine.start_task("do the thing")
    assert isinstance(engine.ingest(REPLY_THREE_CALLS), NewTurn)
    for action in engine.pending():
        engine.decide(action.call.id, Decision.APPROVE)
    assert engine.execute() is not None


def test_the_output_hook_reaches_the_tool_context(
    project: Path, make_engine: EngineFactory
) -> None:
    """set_output_hook is the second half of the wiring: it lands on ToolContext."""
    engine = make_engine()
    seen: list[tuple[int, str]] = []
    engine.set_output_hook(lambda cid, chunk: seen.append((cid, chunk)))
    engine._ctx.emit_output(7, "compiling\n")
    assert seen == [(7, "compiling\n")]
    engine.set_output_hook(None)
    engine._ctx.emit_output(7, "more")
    assert seen == [(7, "compiling\n")]
