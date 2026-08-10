"""Starting a session over: who asks, and who gets a fresh BROWSER chat out of it.

Three doors lead to ``_reset_session`` and they do not mean the same thing:

* ``/new`` - the user is abandoning the conversation on screen, so the next
  session belongs in a fresh browser chat. The controller says so through the
  port (``open_new_chat_now``, tui.md section 3.3a) and the view does all of it
  right then: nothing is clicked here, because the controller must never learn
  what a new-chat button is. Notably it does not reset the session either - the
  view calls ``request_new_session`` back, one beat later and whether or not the
  click landed, since the tool side is the half that can always be delivered.
* the summary screen's *new session* - the user has just read the wrap-up of a
  session they ended deliberately. Nothing says the chat behind it is stale, and
  emptying it would take the transcript they may still want to read with it.
* ``request_new_session()`` - the tool-side half on its own, for a caller that
  has ALREADY opened the fresh chat (both the sidebar's "New browser chat"
  button on the master tab and, one beat later, ``/new`` itself).

The scope is the whole point of these tests: a browser click made on every reset
would open a blank chat after the summary screen and at the budget-exceeded
retry, neither of which the user asked for.

The second half of the file is the other rule the door enforces: **a new chat is
always available**. It used to be refused whenever a turn was in flight, which
put the one escape from a conversation that had gone wrong behind the very turn
the user wanted out of. Now ``request_new_session`` aborts that turn instead -
poisoning whichever park it sits on, cancelling an executing step through the
engine, ending a delegated run - waits for the flow to unwind, and only then
resets. What those tests pin is that the abort is *clean*: no outbound is copied
for a turn nobody will read, no gate or answer park is left armed, the master's
context is restored after a sub-run, and nothing slips into the flow slot in the
gap. The one refusal left is "no active session to replace".

Typing ``/new`` at an ask_user park is deliberately NOT a new chat - the
composer's text is the answer, verbatim (``submit_message``'s standing
invariant, pinned in tests/tui/test_chat_ui.py) - so the sidebar button's door,
``request_new_session`` itself, is what a test drives for that state.
"""

from __future__ import annotations

from agentclip.app.controller import SessionController
from agentclip.app.types import SessionSpec

from .conftest import (
    MARKER,
    MASTER_CHAT,
    SUB_CHATS,
    FakeChatView,
    ask_user_reply,
    delegate_reply,
    edit_reply,
    read_file_reply,
    settle,
    slow_command_reply,
    start_session,
    wait_for,
)

ABORT_TOAST = "aborting the current step - starting a fresh session"


async def test_new_opens_a_fresh_browser_chat_immediately(
    controller: SessionController, view: FakeChatView
) -> None:
    """The command's whole browser side happens at command time, not at the next
    paste - and the reset arrives back through the port one beat later."""
    await start_session(controller, view)

    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert view.new_chats_opened == 1  # asked for once, by the command that means it
    assert view.trace.count("open-new-chat") == 1


async def test_new_resets_even_when_the_chat_could_not_be_opened(
    controller: SessionController, view: FakeChatView
) -> None:
    """The browser half of a fresh chat can fail for half a dozen reasons the
    controller cannot see and the user can fix in one click; the tool half never
    fails. Tying the second to the first left /new doing nothing at all in the
    states it is reached for, so the reset comes back either way - the view's
    toast is what tells the user the old chat is still on screen."""
    await start_session(controller, view)
    view.new_chat_lands = False  # not calibrated / not on screen / the OS said no

    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset anyway")
    await settle(view)

    assert view.new_chats_opened == 1  # it was asked for, and failed on the far side
    assert view.trace.count("open-new-chat-refused") == 1


async def test_the_summary_screens_new_session_opens_no_browser_chat(
    controller: SessionController, view: FakeChatView
) -> None:
    """The user ended this session and read its summary - the chat behind it is
    the one they just finished with, not a stale one to be swept away."""
    await start_session(controller, view)
    view.summary_action = "new"

    controller.end_session()
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert view.new_chats_opened == 0


async def test_request_new_session_resets_without_touching_the_browser(
    controller: SessionController, view: FakeChatView
) -> None:
    """The caller has opened the browser chat itself; opening another would
    click new-chat a second time, on the chat that was just opened."""
    await start_session(controller, view)

    assert controller.request_new_session() is True
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert view.new_chats_opened == 0


async def test_request_new_session_refuses_when_there_is_nothing_to_replace(
    controller: SessionController, view: FakeChatView
) -> None:
    """The only refusal left - the caller is told, and finds out, so it can
    leave its own state alone."""
    assert controller.request_new_session() is False
    assert view.cleared == 0
    assert any("no active session to replace" in text for text in view.toasts())


# == a turn in flight is aborted, not a reason to refuse ======================
#
# Every state below is one the old busy guard refused from, which meant the user
# could not leave a conversation until the conversation let them.


async def _park_at_a_gate(controller: SessionController, view: FakeChatView) -> list[None]:
    """Park the master's turn on an approval gate that nobody will answer."""
    await start_session(controller, view)
    gated: list[None] = []

    def hold(action: object, position: str, queue: str) -> None:
        gated.append(None)  # deliberately does NOT schedule a decision

    view.show_gate = hold  # type: ignore[method-assign]
    controller.submit_clipboard(
        edit_reply("src/utils.py", "return 1", "return 2", chat=MASTER_CHAT)
    )
    await wait_for(lambda: bool(gated), "the edit to reach the approval gate")
    return gated


async def test_new_at_an_approval_gate_aborts_the_turn_and_resets(
    controller: SessionController, view: FakeChatView
) -> None:
    """The gate is what the flow is blocked on, so the abort has to go INTO it.

    Poisoning the future unwinds the turn through ``_gate``'s own ``finally``,
    which is what leaves the composer and the gate panel in a usable state for
    the session that replaces it - and the pending edit is never applied,
    because the decision it was waiting for never came.
    """
    await _park_at_a_gate(controller, view)
    # No second spec queued: the fresh session stops at the task prompt, which
    # keeps every count below about the turn that was aborted.

    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert ABORT_TOAST in view.toasts()
    assert controller._pending_approval is False
    assert controller._awaiting_answer is False
    assert controller._gate_future is None
    assert controller._turn_aborting is False
    # The turn never got its decision, so the engine never ran the edit.
    assert "return 1" in (controller._project_root / "src" / "utils.py").read_text()


async def test_new_mid_turn_starts_the_session_that_replaces_it(
    controller: SessionController, view: FakeChatView
) -> None:
    """The abort is not the end of it: the whole point is the fresh session on
    the other side, armed and waiting for its own first reply."""
    await _park_at_a_gate(controller, view)
    view.specs.append(SessionSpec(task="Something else entirely.", service="claude"))

    controller.submit_message("/new")
    await wait_for(lambda: view.input_started > 1, "a fresh session armed behind the new chat")
    await settle(view)

    assert view.cleared == 1
    assert controller._session_active is True
    assert ("user", "Something else entirely.") in view.events


async def test_new_while_executing_cancels_the_engine_and_copies_nothing(
    controller: SessionController, view: FakeChatView
) -> None:
    """Nothing is awaiting a UI decision here, so the abort has to reach the
    engine: ``request_cancel`` unblocks the worker thread (thread-safe by
    design), that turn's execute returns, and the checkpoint at the top of
    ``_handle_step`` is what stops its ``Send`` outbound from ever being copied
    - a payload for a chat that no longer exists.

    A real subprocess, and the abort only fires once its marker file proves the
    command is running: every plan run clears the cancel flag before its first
    call, so cancelling earlier is a race the engine loses by design.
    """
    await start_session(controller, view)
    engine = controller._engine
    assert engine is not None
    cancels: list[None] = []
    real_cancel = engine.request_cancel

    def spy() -> None:
        cancels.append(None)
        real_cancel()

    engine.request_cancel = spy  # type: ignore[method-assign]
    marker = controller._project_root / MARKER
    controller.submit_clipboard(slow_command_reply(20, chat=MASTER_CHAT))
    await wait_for(marker.exists, "the command to be running", timeout=30)
    copied_before = len(view.copied)

    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert cancels, "the abort never reached the engine"
    assert len(view.copied) == copied_before  # the aborted turn copied nothing out
    assert controller._executing is False


async def test_new_during_a_sub_agent_run_ends_the_run_then_resets(
    controller: SessionController, view: FakeChatView
) -> None:
    """A delegation is the deepest a turn can be parked, and it unwinds in the
    right order: the sub-run ends the way ``/abort`` ends it (its tab finalized,
    the live browser chat handed back, the master's context restored), and only
    then does the master's own turn fall - without ever delivering the
    delegate result it no longer has anyone to give."""
    view.delegation = True
    await start_session(controller, view)
    controller.submit_clipboard(delegate_reply("Read every file under src/."))
    await wait_for(lambda: controller._reply_future is not None, "the sub-run to park")
    copied_before = len(view.copied)

    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert controller._sub is None
    assert controller._sub_aborting is False
    assert view.chats_ended  # the live browser chat went back to the master
    assert view.finished and view.finished[-1][2] is False  # the tab says "no result"
    assert "master" in view.focused
    # No delegate result was composed for the master: that payload would be the
    # old conversation's next turn, and there is no old conversation now.
    assert len(view.copied) == copied_before


async def test_new_at_a_sub_agents_gate_does_not_become_a_delegate_result(
    controller: SessionController, view: FakeChatView
) -> None:
    """The one path where the turn-abort travels THROUGH ``_run_subagent``.

    A sub-run parked at an approval gate has no reply future to poison, so the
    gate is what raises - inside the sub-run, under the ``except Exception`` that
    turns every sub-run crash into a delegate result. It has to be re-raised
    there or the session-ending abort would be swallowed and handed to the model
    as "the sub-agent run failed", leaving the master's turn running.
    """
    view.delegation = True
    await start_session(controller, view)
    controller.submit_clipboard(delegate_reply("Edit src/utils.py."))
    await wait_for(lambda: controller._reply_future is not None, "the sub-run to park")
    gated: list[None] = []

    def hold(action: object, position: str, queue: str) -> None:
        gated.append(None)

    view.show_gate = hold  # type: ignore[method-assign]
    controller.submit_clipboard(
        edit_reply("src/utils.py", "return 1", "return 2", chat=SUB_CHATS[0])
    )
    await wait_for(lambda: bool(gated), "the sub-agent's edit to reach the gate")
    copied_before = len(view.copied)

    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert controller._sub is None
    assert len(view.copied) == copied_before  # no delegate result was ever composed
    assert "return 1" in (controller._project_root / "src" / "utils.py").read_text()


async def test_the_button_is_the_escape_hatch_from_an_ask_user_park(
    controller: SessionController, view: FakeChatView
) -> None:
    """Typing "/new" at an ask_user park is an ANSWER, verbatim - the standing
    invariant, and the reason the sidebar's "New browser chat" button exists as
    a second door. It comes in here rather than through ``submit_message``, so
    it is not competing with the answer for the same keystrokes."""
    await start_session(controller, view)
    controller.submit_clipboard(ask_user_reply("Which file?", chat=MASTER_CHAT))
    await wait_for(lambda: controller._awaiting_answer, "the model's question")

    assert controller.request_new_session() is True
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert controller._awaiting_answer is False
    assert controller._answer_future is None
    assert view.new_chats_opened == 0  # the browser half already happened, in the view


async def test_a_second_new_mid_turn_aborts_nothing_twice(
    controller: SessionController, view: FakeChatView
) -> None:
    """Two presses while the turn is unwinding are one abort and one reset.

    The second is not a refusal either - it is the same request, already in
    progress, so it answers True and says nothing more. A second abort would
    poison futures that a third flow (the reset itself) had by then created."""
    await _park_at_a_gate(controller, view)

    controller.submit_message("/new")
    controller.submit_message("/new")
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert view.toasts().count(ABORT_TOAST) == 1
    assert view.cleared == 1


async def test_a_reply_arriving_during_the_abort_is_dropped(
    controller: SessionController, view: FakeChatView
) -> None:
    """The gap between the poisoned park and the reset is the one moment a
    capture could hand the dying session a new turn. It belongs to a chat the
    user has just walked away from, so it is dropped outright - not queued,
    which would resurrect it the instant the flow slot fell free."""
    await _park_at_a_gate(controller, view)

    controller.submit_message("/new")
    await wait_for(lambda: controller._turn_aborting, "the abort to be under way")
    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    assert controller._queued_capture is None
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert not any("queued (newest wins)" in text for text in view.toasts())
    assert view.cleared == 1
