"""The delegation orchestration: one nested session inside the master's turn.

The happy path is a small part of this file. The rest is the failure table from
the plan (section 5), because a delegation is the one place where the controller
drives *someone else's* browser window on the model's say-so, and every way that
can go wrong has to end the same way: an honest error result on the `delegate`
call, the master's context restored, and the live chat back on the master's
window. In particular:

* **5.2** uncalibrated when `delegate` is actually called -> refuse before any
  engine is built, and never open a tab;
* **5.3** the new-chat click did not verify -> abort with **zero** paste calls.
  This is the most damaging failure mode in the feature (a sub-agent's bootstrap
  pasted into the master's chat corrupts that conversation irrecoverably), so it
  is asserted on the ordered call trace, not on a flag;
* **5.8** a sub-agent that finished without a `result` still hands back something;
* **5.11** an exception anywhere restores the master and does not kill its turn;
* **5.12** a sub-task too large for one paste is an error result, not a crash.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.engine.link.factory import EngineRequest
from agentclip.protocol.composer import BudgetExceeded
from agentclip.shell.app.controller import SessionController
from agentclip.shell.app.link import Link
from agentclip.shell.app.types import SessionRef

from ...conftest import write_permissions
from .conftest import (
    MASTER_CHAT,
    SUB_CHATS,
    FakeChatView,
    ask_user_reply,
    delegate_reply,
    edit_reply,
    read_then_delegate_reply,
    settle,
    start_session,
    task_done_reply,
    wait_for,
)

SUB = SUB_CHATS[0]


async def _feed_sub(controller: SessionController, *replies: str) -> None:
    """Answer the parked sub-run, one paste at a time."""
    for reply in replies:
        await wait_for(
            lambda: controller._reply_future is not None and not controller._reply_future.done(),
            "the sub-run to park on a reply",
        )
        controller.submit_clipboard(reply)
        await wait_for(
            lambda: controller._reply_future is None or controller._reply_future.done(),
            "the paste to be taken",
        )


async def _delegating_session(
    controller: SessionController, view: FakeChatView, *, subagent_service: str = ""
) -> None:
    view.delegation = True
    await start_session(controller, view, subagent_service=subagent_service)


@pytest.fixture(autouse=True)
def _allow_delegating(project: Path) -> None:
    """`delegate` answers to the `task` permission, which the shipped defaults
    ask about. These tests are about the delegation MACHINERY, so the project
    allows it outright - the one test that is about plan mode denying a
    delegation says so itself."""
    write_permissions(project, {"permission": {"task": "allow"}})


def _record_engine_requests(controller: SessionController) -> list[EngineRequest]:
    """Tap the engine factory, keeping the real one behind it - what the
    controller ASKS for is the assertion, and a real Engine still has to come
    back or the session it arms is a stub."""
    seen: list[EngineRequest] = []
    inner = controller._engine_factory

    def record(request: EngineRequest) -> Link:
        seen.append(request)
        return inner(request)

    controller._engine_factory = record
    return seen


# -- the happy path -----------------------------------------------------------


async def test_a_delegated_result_reaches_the_master_payload(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Read every file under src/ and list them."))
    await _feed_sub(
        controller,
        task_done_reply("listed them", result="src/utils.py is the only file.", chat=SUB),
    )
    await settle(view)

    master_payload = view.copied[-1]
    # Verbatim, and delivered as the delegate call's own ok result.
    assert "===CLIP:RESULT id=1 status=ok===" in master_payload
    assert "src/utils.py is the only file." in master_payload


async def test_the_sub_agent_engine_is_built_from_the_sub_windows_service(
    controller: SessionController, view: FakeChatView
) -> None:
    """Two windows, two services (tui.md 1.6). The delegation's Engine has to
    come from the SUB-AGENT tab's preset: its paste budget is the budget of the
    chat the sub-run will actually be pasted into, and composing a bootstrap
    against the master's would overrun a smaller one silently.

    Frozen at bootstrap, like the master's - the spec carries both, because both
    pickers are locked for the session's life.
    """
    requests = _record_engine_requests(controller)
    await _delegating_session(controller, view, subagent_service="gemini")
    controller.submit_clipboard(delegate_reply("Read every file under src/."))
    await _feed_sub(controller, task_done_reply("read them", result="one file.", chat=SUB))
    await settle(view)

    assert [(req.role, req.service) for req in requests] == [
        ("master", "claude"),
        ("subagent", "gemini"),
    ]


async def test_a_blank_sub_agent_service_follows_the_master(
    controller: SessionController, view: FakeChatView
) -> None:
    """One picker's worth of information is a legal spec: a front-end that never
    offers a second service leaves the field blank and both windows run the
    same one."""
    requests = _record_engine_requests(controller)
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Read every file under src/."))
    await _feed_sub(controller, task_done_reply("read them", result="one file.", chat=SUB))
    await settle(view)

    assert [req.service for req in requests] == ["claude", "claude"]


async def test_the_sub_agent_gets_its_own_chat_and_tab(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Survey the tests."))
    await _feed_sub(controller, task_done_reply("surveyed", result="12 test files.", chat=SUB))
    await settle(view)

    assert [ref.id for ref in view.opened] == ["sub-1"]
    assert view.opened[0].role == "subagent"
    assert view.opened[0].title == "Survey the tests."
    assert view.opened[0].chat_name == SUB
    assert view.finished[0][0] == "sub-1"
    # Opened, driven, closed, and the master refocused - in that order.
    assert view.trace.index("open:sub-1") < view.trace.index("start_chat")
    assert view.trace.index("start_chat") < view.trace.index("end_chat")
    assert view.focused[-1] == "master"


async def test_the_context_param_travels_under_its_documented_heading(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    controller.submit_clipboard(
        delegate_reply("Summarise the parser.", context="It lives in protocol/parser.py.")
    )
    await _feed_sub(controller, task_done_reply("done", result="a summary", chat=SUB))
    await settle(view)

    bootstrap = view.copied[1]  # [0] is the master's
    assert "Context from the delegating agent:" in bootstrap
    assert "It lives in protocol/parser.py." in bootstrap


async def test_the_sub_agent_has_no_delegate_tool(
    controller: SessionController, view: FakeChatView
) -> None:
    """5.7: nesting is excluded by construction, not by a special case."""
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Do a bounded thing."))
    await wait_for(lambda: len(view.copied) > 1, "the sub-agent bootstrap")
    bootstrap = view.copied[1]

    assert "You are a sub-agent." in bootstrap
    assert "tool=delegate" not in bootstrap
    assert f"chat={SUB}" in bootstrap

    await _feed_sub(controller, task_done_reply("done", result="r", chat=SUB))
    await settle(view)


async def test_the_master_turn_resumes_after_the_delegation(
    controller: SessionController, view: FakeChatView
) -> None:
    """The delegate call parks the turn; it does not end it. The call after it
    must still run, and its result must ride in the same payload."""
    await _delegating_session(controller, view)
    controller.submit_clipboard(read_then_delegate_reply("README.md", "Check the tests."))
    await _feed_sub(controller, task_done_reply("checked", result="all green", chat=SUB))
    await settle(view)

    payload = view.copied[-1]
    assert "id=1" in payload and "id=2" in payload and "id=3" in payload
    assert "all green" in payload  # the delegate result
    assert "utils.py" in payload  # the list_dir AFTER the delegate ran


async def test_the_master_context_is_restored(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    master_link = controller._link
    master_stats = controller._stats

    controller.submit_clipboard(delegate_reply("Read the docs."))
    await _feed_sub(controller, task_done_reply("read", result="notes", chat=SUB))
    await settle(view)

    assert controller._link is master_link
    assert controller._stats is master_stats
    assert controller._chat_name == MASTER_CHAT
    assert controller._active == SessionRef(
        id="master", role="master", title="Tidy up src/utils.py.", chat_name=MASTER_CHAT
    )
    assert controller._sub is None
    assert controller._reply_future is None
    # The last outbound is the MASTER's results payload, so `c` re-copies the
    # right thing - not whatever the sub-agent last saw.
    assert controller._last_outbound == view.copied[-1]
    assert controller._stats.subagents == 1
    # The sub-agent's replies were counted against the sub-agent, not the master.
    assert controller._stats.replies == 1


async def test_the_sub_agents_own_turns_run_the_normal_loop(
    controller: SessionController, view: FakeChatView
) -> None:
    """A sub-agent is an ordinary session: multi-turn, gated, transcribed."""
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Fix the helper."))
    await _feed_sub(
        controller,
        edit_reply("src/utils.py", "return 1", "return 2", chat=SUB),
        task_done_reply("fixed it", result="src/utils.py now returns 2", chat=SUB),
    )
    await settle(view)

    assert len(view.gates) == 1  # the edit was gated, on the sub-agent's behalf
    assert any("sub-agent: approval needed: edit_file" in text for text, _ in view.alerts)
    assert "src/utils.py now returns 2" in view.copied[-1]


async def test_a_sub_agent_question_is_answered_on_its_own_tab(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    view.answers.append("use src/utils.py")
    controller.submit_clipboard(delegate_reply("Ask me where to look."))
    await _feed_sub(controller, ask_user_reply("Which file?", chat=SUB))
    await _feed_sub(controller, task_done_reply("asked", result="looked in utils", chat=SUB))
    await settle(view)

    assert any("sub-agent: the model asks you a question" in text for text, _ in view.alerts)
    assert ("user", "use src/utils.py") in view.events
    # The answer went into the SUB-agent's payload, not the master's.
    assert any("use src/utils.py" in text for text in view.copied[2:-1])


# -- failure paths ------------------------------------------------------------


async def test_an_uncalibrated_host_refuses_before_building_anything(
    controller: SessionController, view: FakeChatView
) -> None:
    """5.2. The catalog was gated at bootstrap, so this is the de-calibrated
    case: available at session start, gone by the time delegate ran."""
    await _delegating_session(controller, view)
    view.delegation = False
    view.missing = ("copy button", "new-chat button")

    controller.submit_clipboard(delegate_reply("Read every file."))
    await settle(view)

    payload = view.copied[-1]
    assert "delegation is unavailable" in payload
    assert "copy button, new-chat button" in payload
    assert "status=error" in payload
    assert view.opened == []  # no tab, no engine, nothing started
    assert view.chats_started == []
    assert len(view.copied) == 2  # the master's bootstrap and its results


async def test_a_failed_new_chat_click_pastes_nothing(
    controller: SessionController, view: FakeChatView
) -> None:
    """5.3, the safety rule of the whole feature: start_chat returning False
    must abort the delegation BEFORE any paste. Asserted on the ordered trace -
    a sub-agent bootstrap in the master's chat is unrecoverable."""
    await _delegating_session(controller, view)
    view.start_chat_ok = False

    controller.submit_clipboard(delegate_reply("Read every file."))
    await settle(view)

    assert view.trace == [
        "copy",  # the master's bootstrap, before any of this
        "open:sub-1",
        "start_chat",  # refused
        "end_chat",
        "finish-failed:sub-1",  # ...and the view is TOLD it failed
        "copy",  # the master's results, carrying the error
    ]
    payload = view.copied[-1]
    assert "could not open a fresh chat" in payload
    assert "status=error" in payload
    assert controller._sub is None


async def test_a_run_that_handed_nothing_back_is_annotated_as_a_failure(
    controller: SessionController, view: FakeChatView
) -> None:
    """The tab note and the tab glyph both come off this call, and every failure
    reaches it through the same ``finally`` as a success. Told nothing, the view
    printed "the result above was handed back" directly under the error saying
    no chat could be opened - and badged the tab ✓."""
    await _delegating_session(controller, view)
    view.start_chat_ok = False

    controller.submit_clipboard(delegate_reply("Read every file."))
    await settle(view)

    session_id, note, ok = view.finished[-1]
    assert (session_id, ok) == ("sub-1", False)
    assert "WITHOUT a result" in note
    assert "was handed back to the delegating agent" not in note


async def test_a_run_that_delivered_is_annotated_as_a_success(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Read the docs."))
    await _feed_sub(controller, task_done_reply("read", result="notes", chat=SUB))
    await settle(view)

    session_id, note, ok = view.finished[-1]
    assert (session_id, ok) == ("sub-1", True)
    assert "handed back to the delegating agent" in note


async def test_an_aborted_run_is_annotated_as_a_failure(
    controller: SessionController, view: FakeChatView
) -> None:
    """Aborting is a failure of the RUN even though nothing malfunctioned: the
    delegating agent got an error result, so the tab must not claim otherwise."""
    await _delegating_session(controller, view)
    view.on_start_chat = lambda: controller.submit_message("/abort")

    controller.submit_clipboard(delegate_reply("Read every file."))
    await settle(view)

    assert view.finished[-1][2] is False
    assert "aborted" in view.copied[-1]


async def test_a_deleted_sub_agent_service_refuses_the_delegation(
    controller: SessionController, view: FakeChatView
) -> None:
    """The sub window's service is frozen at bootstrap; the service editor is
    not. Deleting that preset mid-session used to fall through to the [general]
    fallback, so the sub-agent ran on neither the preset readiness advertised nor
    the one its window is pointed at - and its paste budget was a guess."""
    requests = _record_engine_requests(controller)
    await _delegating_session(controller, view, subagent_service="gemini")

    services = {k: v for k, v in controller._config.services.items() if k != "gemini"}
    controller.update_config(replace(controller._config, services=services))

    controller.submit_clipboard(delegate_reply("Read every file."))
    await settle(view)

    payload = view.copied[-1]
    assert "delegation is unavailable" in payload
    assert "'gemini'" in payload and "no longer exists" in payload
    assert "status=error" in payload
    assert view.opened == []  # no tab, no run
    assert view.chats_started == []
    assert [req.role for req in requests] == ["master"]  # ...and no sub engine
    assert any("deleted while this session was running" in text for text in view.errors())


async def test_a_sub_agent_that_states_no_result_still_hands_something_back(
    controller: SessionController, view: FakeChatView
) -> None:
    """5.8: fall back to the summary, then to a placeholder. The delegating
    agent's result body is never empty."""
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Do the thing."))
    await _feed_sub(controller, task_done_reply("I read all of src/ and it looks fine.", chat=SUB))
    await settle(view)

    assert "I read all of src/ and it looks fine." in view.copied[-1]


async def test_a_sub_agent_that_says_nothing_at_all_still_hands_something_back(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("Do the thing."))
    await _feed_sub(controller, task_done_reply("", chat=SUB))
    await settle(view)

    assert "finished without stating a result" in view.copied[-1]


async def test_an_exception_mid_run_restores_the_master_and_reports(
    controller: SessionController, view: FakeChatView
) -> None:
    """5.11: the finally is the whole safety net - it must fire for a failure
    nobody predicted, and the master's turn must still complete."""
    await _delegating_session(controller, view)
    master_link = controller._link

    async def boom(session: SessionRef) -> None:
        raise RuntimeError("the tab would not mount")

    view.open_session_view = boom  # type: ignore[method-assign]

    controller.submit_clipboard(delegate_reply("Read every file."))
    await settle(view)

    payload = view.copied[-1]
    assert "the sub-agent run failed" in payload
    assert "the tab would not mount" in payload
    assert controller._link is master_link
    assert controller._sub is None
    assert view.chats_ended  # the live chat went back to the master's window
    assert view.focused[-1] == "master"


async def test_a_sub_task_too_large_for_one_paste_is_an_error_result(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """5.12: BudgetExceeded inside the sub-run must not kill the master's turn."""
    await _delegating_session(controller, view)
    real = controller._engine_factory

    def build(request: object) -> object:
        link = real(request)  # type: ignore[arg-type]
        if link.role == "subagent":

            def refuse(task: str) -> None:
                raise BudgetExceeded(needed_chars=99_000, budget_chars=12_000)

            # Patched on the ENGINE, under the link: the link's own start_task is
            # a coroutine that hands the call to whatever the engine's is.
            link.engine.start_task = refuse  # type: ignore[method-assign]
        return link

    controller._engine_factory = build  # type: ignore[assignment]

    controller.submit_clipboard(delegate_reply("A gigantic sub-task."))
    await settle(view)

    payload = view.copied[-1]
    assert "did not fit in one paste" in payload
    assert "99,000" in payload
    assert view.chats_started == []  # the browser was never touched
    assert controller._active is not None and controller._active.role == "master"


async def test_two_delegations_get_their_own_tabs_and_chats(
    controller: SessionController, view: FakeChatView
) -> None:
    await _delegating_session(controller, view)
    controller.submit_clipboard(delegate_reply("First job."))
    await _feed_sub(controller, task_done_reply("one", result="first answer", chat=SUB_CHATS[0]))
    await settle(view)

    controller.submit_clipboard(delegate_reply("Second job."))
    await _feed_sub(controller, task_done_reply("two", result="second answer", chat=SUB_CHATS[1]))
    await settle(view)

    assert [ref.id for ref in view.opened] == ["sub-1", "sub-2"]
    assert [ref.chat_name for ref in view.opened] == [SUB_CHATS[0], SUB_CHATS[1]]
    assert "second answer" in view.copied[-1]
    assert controller._stats.subagents == 2


# == the permission mode reaches the sub-agent ================================
#
# The mode is the user's app-wide dial, not one conversation's setting (see
# architecture.md, and test_controller_routing's context round-trip). A
# delegation is where that has teeth: plan mode promises the model "no changes
# may be made", and a sub-agent editing files on the master's say-so would break
# that promise from inside the feature meant to keep it. Unattended promises
# nobody will be asked, and a sub-agent's gate would be a question asked of an
# empty chair.


def _command_reply(command: str, *, chat: str) -> str:
    """A sub-agent asking to run something no allow rule covers."""
    return (
        "===CLIP:CALL id=1 tool=run_command===\n"
        f"command: {command}\n"
        "reason: check the state of the tree\n"
        "===CLIP:END===\n"
        f"===CLIP:EOM calls=1 chat={chat}===\n"
    )


async def test_plan_mode_refuses_the_delegation_itself(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The gap this closes: a model in plan mode must not be able to make every
    change it likes simply by delegating them. Plan's overlay denies the `task`
    key, and it sits ABOVE this project's `task: allow`, so no sub-agent is even
    started - there is no window in which one could edit."""
    await _delegating_session(controller, view)
    controller.set_permission_mode("plan")
    await wait_for(lambda: controller.permission_mode == "plan", "plan mode armed")
    before = (project / "src" / "utils.py").read_text(encoding="utf-8")

    controller.submit_clipboard(delegate_reply("Tidy src/utils.py."))
    await settle(view)

    assert view.gates == []  # a denial is not a gate: there is nothing to ask
    assert view.opened == []  # ...and no sub-agent tab was ever opened
    assert (project / "src" / "utils.py").read_text(encoding="utf-8") == before
    assert any("plan mode is active" in copied for copied in view.copied)


async def test_a_sub_agent_started_unattended_never_asks(
    controller: SessionController, view: FakeChatView
) -> None:
    """The other half of the promise: the user is away, so a call no allow rule
    covers is refused rather than parked on a gate nobody will answer."""
    await _delegating_session(controller, view)
    controller.set_unattended(True)
    await wait_for(lambda: controller.unattended, "unattended armed")

    controller.submit_clipboard(delegate_reply("Find out what state the tree is in."))
    await _feed_sub(
        controller,
        _command_reply("definitely-not-covered-by-a-rule --flag", chat=SUB),
        task_done_reply("nothing ran", result="everything was auto-denied", chat=SUB),
    )
    await settle(view)

    assert view.gates == []
    assert any(
        "auto-denied: the user is away (unattended is on)" in copied for copied in view.copied
    )
    assert "everything was auto-denied" in view.copied[-1]


async def test_cycling_the_mode_during_a_delegation_hits_the_sub_agent_then_the_master(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """Only one engine is reachable at a time, so a shift+tab pressed while a
    sub-agent runs lands on the SUB-AGENT's policy - which is right, that is the
    conversation running - and the master picks the change up on the way back
    (``_rearm_master_mode``). The dial itself is never rolled back by the swap."""
    await _delegating_session(controller, view)
    assert controller.permission_mode == "build"

    controller.submit_clipboard(delegate_reply("Tidy src/utils.py."))
    await wait_for(lambda: controller._reply_future is not None, "the sub-run to park")

    controller.set_permission_mode("plan")
    # The live snapshot is the SUB-AGENT's engine (it holds the slot), and this
    # waits on the snapshot rather than the mirror precisely because the
    # snapshot is the engine's own policy read back - proof the change landed
    # on the running conversation and not just on the controller's field.
    await wait_for(
        lambda: controller._snap is not None and controller._snap.mode == "plan",
        "the mode to reach the sub-agent's engine",
    )

    before = (project / "src" / "utils.py").read_text(encoding="utf-8")
    await _feed_sub(
        controller,
        edit_reply("src/utils.py", "return 1", "return 2", chat=SUB),
        task_done_reply("nothing to hand back", result="plan mode blocked it", chat=SUB),
    )
    await settle(view)

    assert view.gates == []
    assert (project / "src" / "utils.py").read_text(encoding="utf-8") == before
    assert any("plan mode is active" in copied for copied in view.copied)
    # Back on the master: the dial survived the swap, and so did the engine
    # behind it - a master left on `build` would run the next turn's edits.
    assert controller.permission_mode == "plan"
    assert controller._active is not None and controller._active.role == "master"
    assert controller._snap is not None and controller._snap.mode == "plan"


async def test_the_unattended_switch_reaches_the_sub_agent_then_the_master(
    controller: SessionController, view: FakeChatView
) -> None:
    """The mode's twin, on the same rails: thrown mid-delegation it lands on the
    running conversation, and the master is reconciled on the way back."""
    await _delegating_session(controller, view)
    assert controller.unattended is False

    controller.submit_clipboard(delegate_reply("Survey src/."))
    await wait_for(lambda: controller._reply_future is not None, "the sub-run to park")

    controller.set_unattended(True)
    await wait_for(
        lambda: controller._snap is not None and controller._snap.unattended,
        "unattended to reach the sub-agent's engine",
    )

    await _feed_sub(
        controller,
        _command_reply("definitely-not-covered-by-a-rule --flag", chat=SUB),
        task_done_reply("nothing ran", result="all auto-denied", chat=SUB),
    )
    await settle(view)

    assert view.gates == []
    assert controller.unattended is True
    assert controller._snap is not None and controller._snap.unattended is True
