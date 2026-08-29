"""SAY blocks reaching the transcript, from the controller's side.

The parser's half of this lives in ``tests/protocol/test_parser_golden.py``.
What matters *here* is that a reply arrives in the transcript in the order the
model wrote it - what it said before the calls above them, what it said after
below them - and that the two kinds stay apart: ``say`` is the model addressing
the user in markdown, ``prose`` is whatever it left outside its blocks.
"""

from __future__ import annotations

from agentclip.shell.app.controller import SessionController

from .conftest import MASTER_CHAT, FakeChatView, settle, start_session

REPLY = (
    "a stray sentence outside every block\n"
    "\n"
    "~~~~\n"
    "===CLIP:SAY===\n"
    "**Reading** `README.md` first.\n"
    "===CLIP:END===\n"
    "===CLIP:CALL id=1 tool=read_file===\n"
    "path: README.md\n"
    "===CLIP:END===\n"
    "===CLIP:SAY===\n"
    "Then I will run the tests.\n"
    "===CLIP:END===\n"
    f"===CLIP:EOM calls=1 chat={MASTER_CHAT}===\n"
    "~~~~\n"
)


async def test_says_reach_the_transcript_in_reply_order(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    view.events.clear()

    controller.submit_clipboard(REPLY)
    await settle(view)

    kinds = [kind for kind, _ in view.events]
    assert kinds[:4] == ["prose", "say", "call", "say"]
    said = [text for kind, text in view.events if kind == "say"]
    assert said == ["**Reading** `README.md` first.", "Then I will run the tests."]
    # Loose text is still tolerated and still shown - just not as a message.
    assert ("prose", "a stray sentence outside every block") in view.events


async def test_a_say_only_reply_is_a_turn_like_any_other(
    controller: SessionController, view: FakeChatView
) -> None:
    """The bootstrap's escape hatch: a greeting needing nothing touched is one
    SAY block and the EOM line. It carries no calls, so the engine answers it
    with the same "every reply must contain at least one call" note a prose-only
    reply always got - and the message itself still reaches the user."""
    await start_session(controller, view)
    view.events.clear()
    before = len(view.copied)

    controller.submit_clipboard(
        "~~~~\n"
        "===CLIP:SAY===\n"
        "Hello - what would you like me to work on?\n"
        "===CLIP:END===\n"
        f"===CLIP:EOM calls=0 chat={MASTER_CHAT}===\n"
        "~~~~\n"
    )
    await settle(view)

    assert ("say", "Hello - what would you like me to work on?") in view.events
    assert len(view.copied) == before + 1  # the nag went out; the turn completed
