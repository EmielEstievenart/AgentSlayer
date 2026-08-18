"""The ask_user park: what the composer's text means while it is open, and the
one way out that is not an answer.

Two rules meet here and only one of them can win a keystroke:

* **an answer is verbatim** - while the flow is parked on the answer future the
  composer's text IS the answer, slash and all (``submit_message`` checks
  ``_awaiting_answer`` before it checks for a leading ``/``;
  ``modals-keys-esc.md`` §6.1). A legitimate answer like ``/etc/hosts`` must
  reach the model, so there is no room left for a command.
* **...which is why cancelling needs a door of its own** -
  ``cancel_pending_question``, reached from a key rather than from the box. It
  RESOLVES the future with ``_CANCELLED_ANSWER`` rather than poisoning it: the
  engine's only exit from AWAITING_USER is ``answer_user``, so an exception
  raised into the park would strand a live engine on a question nobody can
  answer again. ``/new``'s poison is safe only because a full session reset
  always follows it; this has none.
"""

from __future__ import annotations

import asyncio

from agentclip.shell.app.controller import SessionController

from .conftest import (
    MASTER_CHAT,
    FakeChatView,
    ask_user_reply,
    settle,
    start_session,
    task_done_reply,
    wait_for,
)


async def _park_at_a_question(
    controller: SessionController, view: FakeChatView, question: str = "Which file?"
) -> None:
    await start_session(controller, view)
    controller.submit_clipboard(ask_user_reply(question, chat=MASTER_CHAT))
    await wait_for(lambda: controller._awaiting_answer, "the model's question")


async def test_the_question_is_announced_before_the_park(
    controller: SessionController, view: FakeChatView
) -> None:
    """The "? ..." note is the ONE place the question text reaches a view - the
    GUI's banner is built from it - so its shape is a contract, not a flourish."""
    await _park_at_a_question(controller, view, "src/x.py or src/y.py?")
    assert ("note", "? src/x.py or src/y.py?") in view.events


async def test_a_slash_line_is_the_answer_and_never_a_command(
    controller: SessionController, view: FakeChatView
) -> None:
    """§6.1's invariant, in the master's own turn: "/etc/hosts" is a perfectly
    good answer and the model must receive it, not a help sheet."""
    await _park_at_a_question(controller, view, "Which file?")
    notes_before = len(view.notes())

    controller.submit_message("/etc/hosts")
    await wait_for(lambda: not controller._awaiting_answer, "the answer to land")
    await settle(view)

    assert ("user", "/etc/hosts") in view.events
    assert any("/etc/hosts" in payload for payload in view.copied)
    # No command ran: /help and friends all write a note, and none was written.
    assert len(view.notes()) == notes_before


async def test_cancelling_answers_the_model_instead_of_abandoning_it(
    controller: SessionController, view: FakeChatView
) -> None:
    """The whole decision in one test: the park is RESOLVED, so the turn runs
    on. The engine leaves AWAITING_USER the only way it can, the transcript
    shows what was sent, and the next payload carries it to the model."""
    await _park_at_a_question(controller, view)

    controller.cancel_pending_question()
    await wait_for(lambda: not controller._awaiting_answer, "the cancel to land")
    await settle(view)

    assert controller._answer_future is None
    assert ("user", "[cancelled by user]") in view.events
    assert any("[cancelled by user]" in payload for payload in view.copied)
    assert controller._session_active  # cancelled the QUESTION, not the session
    assert any("question cancelled" in text for text in view.toasts())


async def test_a_command_routes_again_once_the_question_is_gone(
    controller: SessionController, view: FakeChatView
) -> None:
    """The other half of the invariant: verbatim is a property of the PARK, and
    the park is over. A slash line is a command again the moment it ends."""
    await _park_at_a_question(controller, view)
    controller.cancel_pending_question()
    await wait_for(lambda: not controller._awaiting_answer, "the cancel to land")
    await settle(view)

    controller.submit_message("/help")
    await asyncio.sleep(0.05)

    assert any("/abort" in text for text in view.notes())
    assert ("user", "/help") not in view.events  # not delivered as an answer


async def test_cancelling_with_nothing_pending_is_a_no_op(
    controller: SessionController, view: FakeChatView
) -> None:
    """The page presses it without knowing whether a question is up (the state
    push it reads is one event behind), so "nothing to cancel" must be silent
    rather than a toast about a question that was already answered."""
    controller.cancel_pending_question()  # no session at all
    await start_session(controller, view)
    before = view.toasts()  # the bootstrap's own "copied ..." and nothing else
    controller.cancel_pending_question()  # a session, but no question

    assert view.toasts() == before
    assert controller._session_active


async def test_a_send_that_beat_the_cancel_wins(
    controller: SessionController, view: FakeChatView
) -> None:
    """Enter and Esc a frame apart: the future is already resolved, so the
    cancel is dropped rather than raising into a settled park - and the model
    hears the answer that was actually typed."""
    await _park_at_a_question(controller, view)

    controller.submit_message("src/x.py")
    controller.cancel_pending_question()
    await settle(view)

    assert ("user", "src/x.py") in view.events
    assert ("user", "[cancelled by user]") not in view.events


async def test_the_turn_finishes_normally_after_a_cancel(
    controller: SessionController, view: FakeChatView
) -> None:
    """A cancelled question leaves an ordinary AWAITING_REPLY session behind -
    the next paste is ingested like any other, which is what "not an abort"
    means in practice."""
    await _park_at_a_question(controller, view)
    controller.cancel_pending_question()
    await settle(view)

    controller.submit_clipboard(task_done_reply("all done", chat=MASTER_CHAT))
    await settle(view)

    assert any(text == "✓ task done" for text in view.notes())
