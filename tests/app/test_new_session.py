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
"""

from __future__ import annotations

from agentclip.app.controller import SessionController

from .conftest import FakeChatView, settle, start_session, wait_for


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
    """Same refusal /new gives, from the same guard - the caller is told, and
    finds out, so it can leave its own state alone."""
    assert controller.request_new_session() is False
    assert view.cleared == 0
    assert any("no active session to replace" in text for text in view.toasts())
