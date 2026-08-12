"""`r` at the controller: arming the service's extra instructions for one send.

The engine half is pinned in tests/engine/test_extra_instructions.py. What
matters here is the front-end contract: the flag reaches the engine off-loop
like every other dial, the reminder shows up in the payload the user's next
send actually puts on the clipboard, it is gone from the one after, and the two
refusals arrive as two different sentences rather than one silent no-op.
"""

from __future__ import annotations

from pathlib import Path

from agentclip.app.controller import SessionController

from .conftest import (
    MASTER_CHAT,
    FakeChatView,
    read_file_reply,
    settle,
    start_session,
)

LINE = "always put a space between ] and ( in code you send"
REMINDER = f"user instructions reminder: {LINE}"


def instruct(project: Path, text: str = LINE) -> None:
    """Give the session's service a line of extra instructions.

    Written into the project's own config rather than passed in: the engine
    factory reloads config per build (conftest.make_factory), so this is what a
    user who saved the field in the service editor would have.
    """
    (project / ".agentclip.toml").write_text(
        f'[services.claude]\nextra_instructions = "{text}"\n', encoding="utf-8"
    )


async def test_arming_puts_the_reminder_in_the_next_payload_and_only_that_one(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    instruct(project)
    await start_session(controller, view)
    assert REMINDER not in view.copied[-1]  # the bootstrap carries them unquoted
    assert LINE in view.copied[-1]

    controller.reinstruct()
    await settle(view)
    assert any("armed" in toast for toast in view.toasts())

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)
    assert REMINDER in view.copied[-1]

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)
    assert REMINDER not in view.copied[-1]


async def test_a_second_press_disarms_before_anything_is_sent(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    instruct(project)
    await start_session(controller, view)

    controller.reinstruct()
    await settle(view)
    controller.reinstruct()
    await settle(view)
    assert any("disarmed" in toast for toast in view.toasts())

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)
    assert REMINDER not in view.copied[-1]


async def test_the_reminder_rides_a_typed_follow_up(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """"The next thing we send" is not "the next results payload"."""
    instruct(project)
    await start_session(controller, view)

    controller.reinstruct()
    await settle(view)
    controller.submit_message("actually, start with README.md")
    await settle(view)

    assert REMINDER in view.copied[-1]


async def test_without_a_session_it_says_so_and_touches_nothing(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.reinstruct()
    await settle(view)

    assert any("no session" in toast for toast in view.toasts())
    assert controller._engine is None


async def test_a_service_with_no_instructions_is_pointed_at_the_editor(
    controller: SessionController, view: FakeChatView
) -> None:
    """A different sentence from "no session": the fix is in a different place."""
    await start_session(controller, view)

    controller.reinstruct()
    await settle(view)

    assert any("service editor" in toast for toast in view.toasts())
    assert not any("armed" in toast for toast in view.toasts())


async def test_the_armed_flag_is_visible_in_the_pushed_state(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """What lights the status bar's INSTR segment - read off the engine's own
    snapshot, so it cannot disagree with what the next payload will carry."""
    instruct(project)
    await start_session(controller, view)
    assert view.states[-1].snapshot is not None
    assert view.states[-1].snapshot.has_extra_instructions is True
    assert view.states[-1].snapshot.instructions_armed is False

    controller.reinstruct()
    await settle(view)
    assert view.states[-1].snapshot is not None
    assert view.states[-1].snapshot.instructions_armed is True

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)
    assert view.states[-1].snapshot is not None
    assert view.states[-1].snapshot.instructions_armed is False
