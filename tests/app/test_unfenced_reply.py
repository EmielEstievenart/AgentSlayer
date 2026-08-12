"""A reply that arrived outside a fence, from the controller's side.

The engine's half of this lives in ``tests/engine/test_state_machine.py``: on a
service marked ``require_fenced_reply`` it composes an id=0 ``reply_unfenced``
payload for the first two such pastes and hands the third to the user. What
matters *here* is the same thing that matters for the flattened bounce - that
the payload actually reaches the clipboard. A refusal nobody copies out leaves
the model waiting for results that will never come, which is worse than the
corruption the gate exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from agentclip.app.controller import SessionController

from .conftest import MASTER_CHAT, FakeChatView, settle, start_session

# Perfectly well-formed - that is the whole problem. On a host that renders
# unfenced text as prose before the copy, this reply's code has already been
# rewritten (link-stripped bracket-paren shapes) and nothing downstream can
# tell. So it must not run.
UNFENCED = (
    "===CLIP:CALL id=1 tool=read_file===\n"
    "path: README.md\n"
    "===CLIP:END===\n"
    f"===CLIP:EOM calls=1 chat={MASTER_CHAT}===\n"
)

FENCED = f"~~~~\n{UNFENCED}~~~~\n"


def _require_fences(project: Path) -> None:
    """Mark the session's service as one whose copy path mangles unfenced text.

    Written into the PROJECT config because the engine factory re-reads config
    on every session start - which is also how a user would turn this on for
    one repo without touching the global file.
    """
    (project / ".agentclip.toml").write_text(
        "[services.claude]\nrequire_fenced_reply = true\n", encoding="utf-8"
    )


async def test_an_unfenced_reply_copies_a_resend_request_out(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    _require_fences(project)
    await start_session(controller, view)
    before = len(view.copied)

    controller.submit_clipboard(UNFENCED)
    await settle(view)

    assert not any(kind == "call" for kind, _ in view.events)  # the read never ran
    assert len(view.copied) == before + 1
    payload = view.copied[-1]
    assert "code=reply_unfenced" in payload
    assert "ENTIRE reply" in payload and "~~~~ fence" in payload
    assert any("outside a code fence" in text for text in view.errors())


async def test_the_same_reply_fenced_runs_normally(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The gate refuses the transport, not the reply: fence the identical text
    and it is an ordinary turn."""
    _require_fences(project)
    await start_session(controller, view)

    controller.submit_clipboard(FENCED)
    await settle(view)

    assert any(kind == "call" for kind, _ in view.events)


async def test_the_third_transport_bounce_in_a_row_is_the_user_s_problem(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """Two bounces, then silence towards the model. A host that strips the fence
    from every copy would otherwise ping-pong forever, and the host is the thing
    that has to change."""
    _require_fences(project)
    await start_session(controller, view)
    before = len(view.copied)

    for _ in range(3):
        controller.submit_clipboard(UNFENCED)
        await settle(view)

    assert len(view.copied) == before + 2  # the third composed nothing
    assert any("stopped asking" in text for text in view.errors())
