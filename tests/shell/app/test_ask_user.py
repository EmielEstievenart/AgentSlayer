"""The ask_user park: what the composer's text means while it is open, and the
one way out that is not an answer.

Two rules meet here and only one of them can win a keystroke:

* **an answer is verbatim** - while the flow is parked on the answer future the
  composer's text IS the answer, slash and all (``submit_message`` checks
  ``_awaiting_answer`` before it checks for a leading ``/``;
  ``modals-keys-esc.md`` §6.1). A legitimate answer like ``/etc/hosts`` must
  reach the model, so there is no room left for a command.
* **...which is why dismissing needs a door of its own** -
  ``dismiss_pending_question``, reached from a key rather than from the box.

What that door does NOT do is answer. Esc sends nothing: the user who pressed it
denied nothing, they simply have something else to say first, and a synthesized
"the user declined" would spend a whole turn on a reply to a message nobody
wrote. Nor may it POISON the park - the engine's one exit from AWAITING_USER is
``answer_user``, so an exception raised in there would strand a live engine on a
question nobody can answer again (``/new``'s poison is safe only because a full
session reset always follows it; this has none). So the park is left standing and
only this side changes: the composer goes back to normal, commands parse again,
and the next ordinary message resolves the question with ``_DECLINED_PREFIX`` in
front of what the user actually wanted.
"""

from __future__ import annotations

import asyncio

from agentclip.shell.app.controller import _DECLINED_PREFIX, SessionController

from .conftest import (
    MASTER_CHAT,
    SUB_CHATS,
    FakeChatView,
    ask_user_reply,
    delegate_reply,
    settle,
    start_session,
    task_done_reply,
    wait_for,
)

SUB = SUB_CHATS[0]


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


async def test_dismissing_answers_nothing_and_leaves_the_model_waiting(
    controller: SessionController, view: FakeChatView
) -> None:
    """The whole decision in one test: Esc is local. The park is still standing,
    the engine is still in AWAITING_USER, and not one character has been put in
    the user's mouth or sent to the model."""
    await _park_at_a_question(controller, view)
    copied_before = len(view.copied)
    events_before = len(view.events)

    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)

    assert controller._question_dismissed
    assert controller._awaiting_answer  # the flow has NOT moved on
    assert controller._answer_future is not None
    assert not controller._answer_future.done()
    assert len(view.copied) == copied_before  # nothing went to the model
    assert view.events[events_before:] == []  # and nothing was echoed as an answer
    assert controller._session_active
    assert any("question dismissed" in text for text in view.toasts())


async def test_dismissing_takes_the_composer_out_of_answer_mode(
    controller: SessionController, view: FakeChatView
) -> None:
    """The push is how both shells learn the box is theirs again: the verbatim
    flag drops, and the parked question rides on its own field so the composer
    can stay ENABLED through a flow that still reads busy."""
    await _park_at_a_question(controller, view)
    assert view.states[-1].awaiting_answer

    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)

    assert not view.states[-1].awaiting_answer
    assert view.states[-1].question_dismissed


async def test_a_command_routes_again_once_the_question_is_dismissed(
    controller: SessionController, view: FakeChatView
) -> None:
    """The other half of the invariant: verbatim is a property of the window in
    which the user is ANSWERING, and Esc closed it. A slash line is a command
    again from that press onwards, even though the park itself is still up."""
    await _park_at_a_question(controller, view)
    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)

    controller.submit_message("/help")
    await asyncio.sleep(0.05)

    assert any("/abort" in text for text in view.notes())
    assert ("user", "/help") not in view.events  # not delivered as an answer
    assert controller._awaiting_answer  # and the question is still waiting


async def test_the_next_message_answers_the_question_and_says_the_user_declined(
    controller: SessionController, view: FakeChatView
) -> None:
    """What the model actually receives. The wrapper is one string, prefixed
    verbatim to what the user typed, so the reply is not a non-sequitur - and the
    transcript echoes the whole thing, because the user should see exactly what
    the model will read."""
    await _park_at_a_question(controller, view, "Which file?")
    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)

    controller.submit_message("forget that, run the tests instead")
    await wait_for(lambda: not controller._awaiting_answer, "the park to resolve")
    await settle(view)

    wrapped = _DECLINED_PREFIX + "forget that, run the tests instead"
    assert ("user", wrapped) in view.events
    assert any(wrapped in payload for payload in view.copied)
    assert controller._question_dismissed is False  # the park it described is over


async def test_a_literal_slash_message_also_answers_the_dismissed_question(
    controller: SessionController, view: FakeChatView
) -> None:
    """`//text` means "a message that starts with a slash" everywhere else, and
    it has to mean it here too - one slash stripped, then the same declined
    wrapper as any other message."""
    await _park_at_a_question(controller, view)
    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)

    controller.submit_message("//usr/bin is where it lives")
    await wait_for(lambda: not controller._awaiting_answer, "the park to resolve")
    await settle(view)

    assert ("user", _DECLINED_PREFIX + "/usr/bin is where it lives") in view.events


async def test_dismissing_with_nothing_pending_is_a_no_op(
    controller: SessionController, view: FakeChatView
) -> None:
    """The page presses it without knowing whether a question is up (the state
    push it reads is one event behind), so "nothing to dismiss" must be silent
    rather than a toast about a question that was already answered."""
    controller.dismiss_pending_question()  # no session at all
    await start_session(controller, view)
    before = view.toasts()  # the bootstrap's own "copied ..." and nothing else
    controller.dismiss_pending_question()  # a session, but no question

    assert view.toasts() == before
    assert controller._session_active


async def test_dismissing_twice_is_a_no_op(
    controller: SessionController, view: FakeChatView
) -> None:
    """Esc is still bound after the first press - it just means "blur" now - and
    a second one that arrived here anyway must not re-toast a question the user
    has already put aside."""
    await _park_at_a_question(controller, view)
    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)
    before = len(view.toasts())

    controller.dismiss_pending_question()

    assert len(view.toasts()) == before
    assert controller._question_dismissed


async def test_a_send_that_beat_the_dismissal_wins(
    controller: SessionController, view: FakeChatView
) -> None:
    """Enter and Esc a frame apart: the future is already resolved, so the
    dismissal is dropped rather than latching a flag onto a park that is over -
    and the model hears the answer that was actually typed."""
    await _park_at_a_question(controller, view)

    controller.submit_message("src/x.py")
    controller.dismiss_pending_question()
    await settle(view)

    assert ("user", "src/x.py") in view.events
    assert not any(_DECLINED_PREFIX in text for _, text in view.events)


async def test_the_turn_finishes_normally_after_a_dismissed_question(
    controller: SessionController, view: FakeChatView
) -> None:
    """A dismissed-then-answered question leaves an ordinary AWAITING_REPLY
    session behind - the next paste is ingested like any other, which is what
    "nothing was aborted" means in practice."""
    await _park_at_a_question(controller, view)
    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)
    controller.submit_message("skip it")
    await settle(view)

    controller.submit_clipboard(task_done_reply("all done", chat=MASTER_CHAT))
    await settle(view)

    assert any(text == "✓ task done" for text in view.notes())


async def test_new_still_aborts_the_turn_after_a_dismissal(
    controller: SessionController, view: FakeChatView
) -> None:
    """The other door out, and the reason dismissing had to hand the slash back:
    `/new` is now typable at a question that has gone wrong. It takes the abort
    path as always - the park is poisoned, not answered - and the session is
    replaced rather than continued."""
    await _park_at_a_question(controller, view)
    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)
    view.specs.append(None)  # the fresh session's prompt is cancelled

    controller.submit_message("/new")
    await wait_for(lambda: not controller._awaiting_answer, "the turn to unwind")
    await settle(view)

    assert controller._answer_future is None
    assert controller._question_dismissed is False
    assert not any(_DECLINED_PREFIX in text for _, text in view.events)
    assert view.new_chats_opened == 1


async def test_a_sub_agents_question_dismisses_the_same_way(
    controller: SessionController, view: FakeChatView
) -> None:
    """A delegated run parks on the SAME future and the same flag - there is one
    ``_ask`` and one composer - so the sub-agent's question gets the door too,
    and its chat receives the declined wrapper like the master's would."""
    view.delegation = True
    await start_session(controller, view)
    controller.submit_clipboard(delegate_reply("Read every file under src/."))
    await wait_for(lambda: controller._reply_future is not None, "the sub-run to park")
    controller.submit_clipboard(ask_user_reply("Which flag?", chat=SUB))
    await wait_for(lambda: controller._awaiting_answer, "the sub-agent's question")

    controller.dismiss_pending_question()
    await asyncio.sleep(0.05)
    assert controller._awaiting_answer  # the sub-agent is still waiting

    controller.submit_message("just list them")
    await wait_for(lambda: not controller._awaiting_answer, "the park to resolve")
    await settle(view)

    assert ("user", _DECLINED_PREFIX + "just list them") in view.events
    assert any(_DECLINED_PREFIX in payload for payload in view.copied)
