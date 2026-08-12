"""What the controller tells the view while a turn executes (tui.md §8a).

The engine reports progress from its worker thread; the controller's job is to
turn that into rows a view can draw - the plan up front (so queued calls are
visible before they run), then one started/finished per call, with the detail
each row is labelled by. This is where the ✓ of "approved at the gate" and the
✓ of "ran and worked" have to stay apart, since both live in this class.
"""

from __future__ import annotations

from agentclip.app.controller import SessionController

from .conftest import (
    MASTER_CHAT,
    FakeChatView,
    read_file_reply,
    settle,
    start_session,
)


def _two_reads(chat: str = MASTER_CHAT) -> str:
    return (
        "Reading both.\n\n~~~~\n"
        "===CLIP:CALL id=1 tool=read_file===\npath: README.md\n===CLIP:END===\n"
        "===CLIP:CALL id=2 tool=read_file===\npath: src/utils.py\n===CLIP:END===\n"
        f"===CLIP:EOM calls=2 chat={chat}===\n~~~~\n"
    )


async def test_the_whole_plan_is_handed_over_before_the_first_call_runs(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    view.working.clear()

    controller.submit_clipboard(_two_reads())
    await settle(view)

    label, rows = view.working[-1]
    assert "2 tool calls" in label
    assert [(r.call_id, r.tool, r.detail) for r in rows] == [
        (1, "read_file", "README.md"),
        (2, "read_file", "src/utils.py"),
    ]
    # Every row starts pending: approval is not execution.
    assert {r.glyph for r in rows} == {"•"}


async def test_each_call_reports_started_then_finished_in_order(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    view.call_events.clear()

    controller.submit_clipboard(_two_reads())
    await settle(view)

    assert view.call_events == [
        ("started", 1, "read_file"),
        ("finished", 1, "✓"),
        ("started", 2, "read_file"),
        ("finished", 2, "✓"),
    ]


async def test_a_failed_call_resolves_with_the_error_glyph(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    view.call_events.clear()

    controller.submit_clipboard(read_file_reply("no-such-file.txt", chat=MASTER_CHAT))
    await settle(view)

    assert ("finished", 1, "✗") in view.call_events


async def test_a_run_command_row_is_marked_as_one_that_streams(
    controller: SessionController, view: FakeChatView
) -> None:
    """Only the rows that can produce a live tail advertise one."""
    await start_session(controller, view)
    view.working.clear()

    controller.submit_clipboard(
        "Running it.\n\n~~~~\n"
        "===CLIP:CALL id=1 tool=run_command===\n"
        "command: python -c \"print('hi')\"\n"
        "reason: say hi\n"
        "===CLIP:END===\n"
        "===CLIP:CALL id=2 tool=read_file===\npath: README.md\n===CLIP:END===\n"
        f"===CLIP:EOM calls=2 chat={MASTER_CHAT}===\n~~~~\n"
    )
    await settle(view)

    rows = {r.call_id: r for r in view.working[-1][1]}
    assert rows[1].streams and not rows[2].streams
    assert rows[1].detail == "python -c \"print('hi')\""


async def test_a_commands_output_reaches_the_view_as_deltas(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    view.call_output_chunks.clear()

    controller.submit_clipboard(
        "Running it.\n\n~~~~\n"
        "===CLIP:CALL id=1 tool=run_command===\n"
        "command: python -c \"print('streamed line')\"\n"
        "reason: prove the tail arrives\n"
        "===CLIP:END===\n"
        f"===CLIP:EOM calls=1 chat={MASTER_CHAT}===\n~~~~\n"
    )
    await settle(view)

    assert view.call_output_chunks, "no live output reached the view"
    assert all(call_id == 1 for call_id, _ in view.call_output_chunks)
    assert "streamed line" in "".join(chunk for _, chunk in view.call_output_chunks)
