"""``/abort``: the only way out of a sub-agent run, and where it lands.

A sub-run has no wall-clock timeout - the transport is a human alt-tabbing
between two browser windows, and a bounded sub-task can honestly take twenty
minutes. So the escape hatch is explicit, and it has to work from wherever the
run happens to be at the moment the user types it. There are three such places,
and the interleaving is the whole test file:

* **parked on a reply** - the common case. The reply future raises
  ``_SubagentAborted`` and the run unwinds through the same ``finally`` every
  other failure uses.
* **at an approval gate** - resolving the gate is what unblocks the flow, so the
  gate is rejected (which aborts the sub-agent's turn anyway) and the latched
  flag ends the run when it comes back for the next reply.
* **executing tool calls** - nothing is awaiting anything the UI can resolve, so
  the SUB-AGENT's engine is cancelled (``request_cancel``, thread-safe by
  design); that turn finishes normally, and the latch ends the run at the next
  park.

The distinction from ctrl+x is deliberate and documented in /help: ctrl+x
cancels the calls running right now and the turn still reports back into the
sub-agent's chat; /abort ends the delegation.
"""

from __future__ import annotations

import asyncio

from agentclip.app.controller import SessionController
from agentclip.engine.engine import Decision

from .conftest import (
    MARKER,
    SUB_CHATS,
    FakeChatView,
    delegate_reply,
    edit_reply,
    settle,
    slow_command_reply,
    start_session,
    task_done_reply,
    wait_for,
)

SUB = SUB_CHATS[0]


async def _park_a_sub_run(controller: SessionController, view: FakeChatView) -> None:
    view.delegation = True
    await start_session(controller, view)
    controller.submit_clipboard(delegate_reply("Read every file under src/."))
    await wait_for(
        lambda: controller._reply_future is not None, "the sub-run to park on a reply"
    )


async def test_abort_while_waiting_for_a_reply(
    controller: SessionController, view: FakeChatView
) -> None:
    await _park_a_sub_run(controller, view)

    controller.submit_message("/abort")
    await settle(view)

    payload = view.copied[-1]
    assert "the user aborted the sub-agent run" in payload
    assert "status=error" in payload
    assert controller._sub is None
    assert controller._sub_aborting is False
    assert view.chats_ended  # the live browser chat went back to the master
    assert view.focused[-1] == "master"
    assert any("✗ sub-agent run aborted" in note for note in view.notes())


async def test_abort_at_a_gate_rejects_it_and_then_ends_the_run(
    controller: SessionController, view: FakeChatView
) -> None:
    """The gate is what the flow is blocked on, so it must be resolved first;
    the abort itself lands when the turn comes back for the next reply."""
    await _park_a_sub_run(controller, view)
    # Hold the gate open so /abort is what resolves it.
    view.decision = (Decision.APPROVE, None)
    gated: list[None] = []

    def hold(action: object, position: str, queue: str) -> None:
        gated.append(None)  # deliberately does NOT schedule a decision

    view.show_gate = hold  # type: ignore[method-assign]
    controller.submit_clipboard(edit_reply("src/utils.py", "return 1", "return 2", chat=SUB))
    await wait_for(lambda: bool(gated), "the sub-agent's edit to reach the gate")

    controller.submit_message("/abort")
    await settle(view)

    payload = view.copied[-1]
    assert "the user aborted the sub-agent run" in payload
    assert controller._sub is None
    # The edit was never applied: rejecting the gate aborted the sub-agent's turn.
    assert "return 1" in (controller._project_root / "src" / "utils.py").read_text()


async def test_abort_while_the_sub_agent_executes_cancels_then_ends_the_run(
    controller: SessionController, view: FakeChatView
) -> None:
    """Nothing is awaiting a UI decision here, so the abort has to reach the
    engine: request_cancel unblocks the worker, that turn ends normally (its
    cancelled results are copied into the sub-agent's chat), and the latched
    flag ends the delegation when the loop comes back for the next reply.

    A real subprocess, and the abort only fires once its marker file proves it
    is running - every plan run clears the cancel flag before its first call, so
    cancelling earlier is a race the engine loses by design."""
    await _park_a_sub_run(controller, view)
    marker = controller._project_root / MARKER
    controller.submit_clipboard(slow_command_reply(20, chat=SUB))
    await wait_for(marker.exists, "the sub-agent's command to be running", timeout=30)

    controller.submit_message("/abort")
    await settle(view)

    payload = view.copied[-1]
    assert "the user aborted the sub-agent run" in payload
    assert controller._sub is None
    assert controller._active is not None and controller._active.role == "master"
    # The killed call was reported into the sub-agent's chat first: ctrl+x-style
    # cancellation ends the turn, /abort ends the run, and both happened here.
    assert any("cancelled" in text for text in view.copied[2:-1])


async def test_abort_outside_a_sub_run_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    view.delegation = True
    await start_session(controller, view)

    controller.submit_message("/abort")
    await settle(view)

    assert ("no sub-agent run to abort", "warning") in view.notifications


async def test_an_ask_user_answer_still_wins_over_the_command(
    controller: SessionController, view: FakeChatView
) -> None:
    """The standing invariant: while the composer is in answer mode its text IS
    the answer, verbatim. A sub-agent asking "which flag?" and the user typing
    "/abort" must deliver that string, not end the run - the alternative is an
    answer the user cannot type."""
    await _park_a_sub_run(controller, view)
    controller.submit_clipboard(
        "===CLIP:CALL id=1 tool=ask_user===\n"
        "question <<EOT\nWhich flag?\nEOT\n"
        "===CLIP:END===\n"
        f"===CLIP:EOM calls=1 chat={SUB}===\n"
    )
    await wait_for(lambda: controller._awaiting_answer, "the sub-agent's question")

    controller.submit_message("/abort")
    await wait_for(lambda: not controller._awaiting_answer, "the answer to land")

    assert controller._sub is not None  # still running
    assert ("user", "/abort") in view.events
    await _finish(controller, view)


async def test_help_names_both_escape_hatches(
    controller: SessionController, view: FakeChatView
) -> None:
    view.delegation = True
    await start_session(controller, view)

    controller.submit_message("/help")
    await asyncio.sleep(0.05)

    text = " ".join(view.notes())
    assert "/abort" in text
    assert "ctrl+x" in text


async def _finish(controller: SessionController, view: FakeChatView) -> None:
    await wait_for(lambda: controller._reply_future is not None, "the sub-run to park again")
    controller.submit_clipboard(task_done_reply("done", result="the answer", chat=SUB))
    await settle(view)
