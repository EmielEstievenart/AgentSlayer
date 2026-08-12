"""A reply whose line breaks died in transport, from the controller's side.

The engine's half of this lives in ``tests/engine/test_state_machine.py``: it
composes an id=0 ``reply_flattened`` payload for the first two such pastes and
gives up on the third. What matters *here* is that the payload actually reaches
the clipboard. A bounce nobody copies out is the very stall this feature exists
to end - the model sits waiting for results while AgentClip quietly holds the
answer - so the copy is the assertion, not the transcript line.
"""

from __future__ import annotations

from agentclip.app.controller import SessionController

from .conftest import MASTER_CHAT, FakeChatView, settle, start_session

# CLIP blocks the chat rendered as markdown prose: the copy button handed back
# one long line, with a whole CALL block riding behind the EOM marker.
FLATTENED = (
    "===CLIP:CALL id=1 tool=read_file===\n"
    "path: README.md\n"
    "===CLIP:END===\n"
    f"===CLIP:EOM calls=1 chat={MASTER_CHAT}===~~~~ ===CLIP:CALL id=1 tool=run_command==="
    " command: echo hello ===CLIP:END===\n"
    f"===CLIP:EOM calls=1 chat={MASTER_CHAT}===\n"
)


async def test_a_flattened_reply_copies_a_resend_request_out(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    before = len(view.copied)

    controller.submit_clipboard(FLATTENED)
    await settle(view)

    assert not any(kind == "call" for kind, _ in view.events)  # the read never ran
    assert len(view.copied) == before + 1
    payload = view.copied[-1]
    assert "code=reply_flattened" in payload
    assert "ENTIRE reply" in payload and "~~~~ fence" in payload
    assert any("line breaks" in text for text in view.errors())


async def test_the_third_flattened_reply_in_a_row_is_the_user_s_problem(
    controller: SessionController, view: FakeChatView
) -> None:
    """Two bounces, then silence towards the model: a host that flattens every
    copy would otherwise ping-pong forever. The user is told instead, because
    the host is the thing that has to change."""
    await start_session(controller, view)
    before = len(view.copied)

    for _ in range(3):
        controller.submit_clipboard(FLATTENED)
        await settle(view)

    assert len(view.copied) == before + 2  # the third composed nothing
    assert any("stopped asking" in text for text in view.errors())
