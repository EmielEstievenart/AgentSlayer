"""Which session a pasted reply belongs to, and what the controller saves per session.

Two mechanisms, both invisible until a sub-agent exists:

* **chat-name routing** (``submit_clipboard``). Every reply carries the chat
  name it came from; while a sub-run is in flight the master's clipboard queue
  is the wrong place for a sub-agent reply (the master's flow is busy for the
  whole delegation, so anything queued there would never be looked at). Routing
  therefore runs BEFORE the busy check, and a paste from the wrong chat is
  dropped with an explanation rather than parked.
* **the session context** (``_snapshot_ctx`` / ``_restore_ctx``). A delegation
  swaps the engine, stats, glyph strip, outbound state and YOLO mirror for the
  sub-agent's and must put every one of them back, or the master silently
  continues with a sub-agent's numbers. The permission mode is the one field
  that is deliberately NOT swapped: it is an app-wide dial, not a property of
  one conversation.
"""

from __future__ import annotations

import pytest

from agentclip.shell.app.controller import SessionController
from agentclip.shell.app.types import SessionRef, SessionStats

from .conftest import (
    MASTER_CHAT,
    FakeChatView,
    read_file_reply,
    settle,
    start_session,
    task_done_reply,
    wait_for,
)


async def test_a_reply_from_this_chat_is_ingested(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)

    assert any(kind == "call" for kind, _ in view.events)
    assert len(view.copied) == 2  # bootstrap, then the results


async def test_a_reply_naming_another_chat_is_dropped_with_a_reason(
    controller: SessionController, view: FakeChatView
) -> None:
    """The user pasted the wrong window's reply. Never ingested (the engine
    would drop it as noise anyway) and never queued - just explained."""
    await start_session(controller, view)
    before = len(view.copied)

    controller.submit_clipboard(read_file_reply("README.md", chat="teal-moth"))
    await settle(view)

    assert len(view.copied) == before  # nothing ran
    assert any("teal-moth" in text and MASTER_CHAT in text for text in view.toasts())


async def test_an_unnamed_paste_still_reaches_the_active_session(
    controller: SessionController, view: FakeChatView
) -> None:
    """No ``chat=`` attribute at all (an older model, an ACK): the router has
    nothing to decide on, so it hands it over and lets the engine's own chat
    gate be the backstop."""
    await start_session(controller, view)
    unnamed = read_file_reply("README.md", chat=MASTER_CHAT).replace(
        f" chat={MASTER_CHAT}", ""
    )

    controller.submit_clipboard(unnamed)
    await settle(view)

    # The engine saw it and rejected it itself (missing-chat), which is the
    # backstop doing its job - the router did not silently eat it.
    assert any("without this chat's name" in text for text in view.toasts())


async def test_a_reply_arriving_mid_turn_is_queued_depth_one(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    controller._busy = True

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))

    assert controller._queued_capture is not None
    assert any("queued" in text for text in view.toasts())


async def test_a_paste_for_the_sub_agent_resolves_its_reply_future(
    controller: SessionController, view: FakeChatView
) -> None:
    """The routing that only exists because of delegation: with ``_sub`` set,
    a matching paste feeds the parked sub-run instead of the ingest flow."""
    await start_session(controller, view)
    sub = SessionRef(id="sub-1", role="subagent", title="read it", chat_name="jade-otter")
    controller._sub = sub
    controller._busy = True  # the master's flow is busy for the whole sub-run
    future = controller._await_reply()
    task = __import__("asyncio").ensure_future(future)
    await wait_for(lambda: controller._reply_future is not None, "the sub-run parked")

    controller.submit_clipboard(task_done_reply("done", result="the answer", chat="jade-otter"))

    assert await task == task_done_reply("done", result="the answer", chat="jade-otter")
    assert controller._queued_capture is None  # never touched the master's queue


async def test_a_master_reply_during_a_sub_run_is_refused_not_queued(
    controller: SessionController, view: FakeChatView
) -> None:
    """5.9: the master's next payload is composed fresh after the delegation
    returns, so a master-chat paste arriving mid-sub-run is stale by definition."""
    await start_session(controller, view)
    controller._sub = SessionRef(
        id="sub-1", role="subagent", title="read it", chat_name="jade-otter"
    )
    controller._busy = True

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))

    assert controller._queued_capture is None
    assert any("/abort" in text for text in view.toasts())


async def test_a_sub_agent_paste_with_nothing_parked_is_reported(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    controller._sub = SessionRef(
        id="sub-1", role="subagent", title="read it", chat_name="jade-otter"
    )

    controller.submit_clipboard(task_done_reply("done", chat="jade-otter"))

    assert any("still working" in text for text in view.toasts())


async def test_the_session_context_round_trips(
    controller: SessionController, view: FakeChatView
) -> None:
    """Adopt a sub-agent's context, then restore: every saved field comes back,
    and the stats object is the SAME object (counters kept accumulating on it)."""
    await start_session(controller, view)
    controller._stats.replies = 7
    controller._turn_glyphs = {1: ["✓", "read_file"]}
    controller._yolo = True
    controller._mode = "unattended"
    saved = controller._snapshot_ctx()
    master_link = controller._link
    master_stats = controller._stats

    sub_link = controller._engine_factory("claude")
    controller._adopt_ctx(
        SessionRef(id="sub-1", role="subagent", title="t", chat_name=sub_link.chat_name),
        sub_link,
    )
    assert controller._link is sub_link
    assert controller._stats is not master_stats
    assert controller._stats.replies == 0
    assert controller._turn_glyphs == {}
    assert controller._yolo is False  # YOLO deliberately does not inherit
    # ...but the permission mode does, and the divergence is deliberate: YOLO
    # answers a question about ONE conversation, while the mode is a statement
    # about the user that stays true of a sub-agent ("only exploring", "not at my
    # desk"). _sub_run arms the sub-agent's engine with it too.
    assert controller._mode == "unattended"
    assert controller._last_outbound is None

    # The dial survives the swap back untouched - it is not part of the saved
    # context at all, so a cycle made DURING a delegation is still in force here.
    controller._mode = "plan"
    controller._restore_ctx(saved)

    assert controller._link is master_link
    assert controller._stats is master_stats
    assert controller._stats.replies == 7
    assert controller._turn_glyphs == {1: ["✓", "read_file"]}
    assert controller._yolo is True
    assert controller._mode == "plan"  # the mirror won, not the snapshot
    assert saved.engine_mode == "unattended"  # ...which is what re-arms the engine
    assert controller._active is not None and controller._active.role == "master"


async def test_state_pushes_carry_whose_session_they_describe(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    assert view.states[-1].session_id == "master"
    assert view.states[-1].session_role == "master"

    sub_link = controller._engine_factory("claude")
    controller._adopt_ctx(
        SessionRef(id="sub-1", role="subagent", title="read the docs", chat_name="jade-otter"),
        sub_link,
    )

    pushed = view.states[-1]
    assert pushed.session_id == "sub-1"
    assert pushed.session_role == "subagent"
    assert pushed.session_title == "read the docs"


@pytest.mark.parametrize("delegation", [False, True])
async def test_the_catalog_is_gated_at_session_start(
    controller: SessionController, view: FakeChatView, delegation: bool
) -> None:
    """5.1: the sub-agent slot's calibration decides, once, whether the model is
    ever told about `delegate` - the bootstrap is the only place it could go."""
    view.delegation = delegation
    await start_session(controller, view)

    bootstrap = view.copied[0]
    assert ("tool=delegate" in bootstrap) is delegation
    assert any("delegate tool enabled" in note for note in view.notes()) is delegation


async def test_stats_rows_mention_sub_agent_runs_only_when_there_were_some(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    assert not any(label == "sub-agent runs" for label, _ in controller._stats_rows())

    controller._stats = SessionStats(service="claude", subagents=2)
    assert ("sub-agent runs", "2") in controller._stats_rows()


async def test_a_protocol_error_asks_for_the_user_out_loud_too(
    controller: SessionController, view: FakeChatView
) -> None:
    """The one re-sync the automation loop never hears about: nothing ran, no
    LoopState moved, and yet the turn only goes on once the user has gone back
    to the browser and re-copied. So the audible "your move" is asked for here
    rather than left to ``set_loop_state``; whether it makes a sound is the
    live service's business, not this controller's."""
    await start_session(controller, view)

    controller.submit_clipboard(f"===CLIP:NACK reason=truncated chat={MASTER_CHAT}===")
    await settle(view)

    assert any("press c to re-copy" in text for text in view.errors())
    assert view.attention_alerts == 1


async def test_an_ordinary_reply_never_asks_for_the_user_out_loud(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)

    assert view.attention_alerts == 0
