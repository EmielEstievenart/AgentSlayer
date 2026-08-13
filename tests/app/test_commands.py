"""The slash-command registry, and the two things that must derive from it.

The point of ``agentclip.app.commands`` is that there is exactly one list. So
these tests are less about the strings than about the joins: the controller's
dispatch table covers the registry and nothing else, `/help` and the "unknown
command" hint are rendered from the same entries, and the autocomplete trigger
agrees with the dispatcher about what counts as a command in progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentclip.app.commands import (
    COMMANDS,
    ChatCommand,
    command_list,
    help_text,
    lookup,
    match_prefix,
)
from agentclip.app.controller import SessionController
from agentclip.app.types import SessionSpec
from agentclip.config import Config

from .conftest import (
    MASTER_CHAT,
    FakeChatView,
    edit_reply,
    make_factory,
    read_file_reply,
    settle,
    start_session,
    wait_for,
)


def test_the_registry_is_the_documented_commands() -> None:
    """tui.md §3.3a's list, in the order the user meets them - which is also the
    order the popup lists them in, so the most destructive one is last."""
    assert [command.name for command in COMMANDS] == [
        "help",
        "new",
        "abort",
        "identify",
        "log",
        "mcp",
        "armed",
        "mode",
        "yolo",
    ]
    assert lookup("yolo") is not None and lookup("yolo").arg == "[on|off]"  # type: ignore[union-attr]
    assert lookup("mode") is not None and lookup("mode").arg == "[plan|ask|unattended]"  # type: ignore[union-attr]
    assert all(command.summary for command in COMMANDS)


def test_yolo_is_never_the_first_row() -> None:
    """Regression (tui.md §3.3a): the popup renders this tuple as typed, so the
    top row is whatever a stray Enter is nearest to - and `/yolo` turns EVERY
    approval gate off. It goes last, behind the reversible ones."""
    assert COMMANDS[-1].name == "yolo"
    assert COMMANDS[0].name == "help"  # ...and the top row is the harmless one


def test_lookup_resolves_aliases_and_case() -> None:
    help_command = lookup("help")
    assert help_command is not None
    assert lookup("commands") is help_command
    assert lookup("?") is help_command
    assert lookup("HELP") is help_command
    assert lookup("Yolo") is lookup("yolo")
    assert lookup("nope") is None
    assert lookup("") is None
    assert lookup("  ") is None


def test_dispatch_covers_the_registry_exactly(controller: SessionController) -> None:
    """A command in the table with no handler would be an undiscoverable dead
    row in the popup; a handler with no table entry would be a hidden feature."""
    handlers = controller._command_handlers()
    assert set(handlers) == {command.name for command in COMMANDS}


def test_help_text_lists_every_command_with_its_hint() -> None:
    text = help_text()
    assert text.startswith("commands:  ")
    for command in COMMANDS:
        assert command.label in text
        assert command.summary in text
    # The two escape hatches that are easy to confuse stay distinguished in it.
    assert "/abort" in text and "ctrl+x" in text
    assert "/yolo [on|off]" in text  # the argument hint travels with the name


def test_unknown_command_hint_lists_every_command() -> None:
    hint = command_list()
    for command in COMMANDS:
        assert command.slash in hint
    # An English list, not a dump.
    assert hint == "/help, /new, /abort, /identify, /log, /mcp, /armed, /mode, or /yolo"


def test_identify_dispatches_to_the_view_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """The one command with no session gate. It is a calibration aid, and the
    states it is most needed in - nothing armed, or a run wedged against a chat
    window the tool cannot read - are exactly the states a gate would lock it out
    of. It cannot touch a session either: the view captures one frame and draws
    on top of it."""
    controller.submit_message("/identify")
    assert view.identify_overlays == 1
    assert view.toasts() == []  # no refusal, no "start a session first"


def test_log_dispatches_to_the_view_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/identify`'s rule again, and for the same reason: the harness log is
    read when the automation has stopped making sense, which is routinely after
    a run wedged or ended. Gating it behind a live session would lock it away
    exactly when it is wanted, and the controller has nothing to add - every
    decision in the log was taken on the view's side of the port."""
    controller.submit_message("/log")
    assert view.harness_log_toggles == 1
    assert view.toasts() == []  # no refusal, no "start a session first"
    assert view.events == []  # and nothing lands in the transcript
    # ...and it is a toggle on the far side, so the second one puts it away
    # rather than stacking a second view of the same log.
    controller.submit_message("/log")
    assert view.harness_log_toggles == 2


def test_mcp_without_a_source_says_mcp_is_not_configured(
    controller: SessionController, view: FakeChatView
) -> None:
    """The fixture controller is built without ``mcp_statuses`` - the app shape
    when opencode.json has no mcp block - so the command answers with a toast,
    not a transcript listing, and needs no session to do it."""
    controller.submit_message("/mcp")
    assert view.events == []
    assert any("MCP is not configured" in message for message in view.toasts())


async def test_mcp_lists_every_server_with_state_tools_and_detail(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """`/mcp` renders the supplier's rows into one transcript note - state
    always, tool count when connected, the detail line whenever there is one -
    and needs no session (`/log`'s rule: "where are my server's tools?" is
    asked before a session as readily as during one)."""

    @dataclass(frozen=True)
    class Row:  # duck-typed McpStatusLine - the app layer never imports agentclip.mcp
        name: str
        state: str
        detail: str = ""
        tool_count: int = 0

    rows = (
        Row("github", "connected", tool_count=12),
        Row("linear", "needs_auth", detail="server rejected the request (401/403)"),
        Row("scratch", "disabled"),
    )
    controller = SessionController(
        app_config, make_factory(project), project, view=view, mcp_statuses=lambda: rows
    )
    view.controller = controller

    controller.submit_message("/mcp")
    await settle(view)

    note = next(text for text in view.notes() if text.startswith("MCP servers:"))
    assert "github · connected · 12 tools" in note
    assert "linear · needs_auth · server rejected the request (401/403)" in note
    assert "scratch · disabled" in note
    assert "disabled ·" not in note  # no tool count, no detail: the state says it all


def test_armed_dispatches_to_the_view_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/identify`'s reasoning, one step sharper. The armed switch is what a user
    reaches for when the tool is doing something to their screen they want
    stopped, so a session gate would lock it away at the exact moment it is
    wanted. Bare = toggle, and the target travels as ``None`` because the flag
    lives in the view - the controller has nothing to toggle against."""
    controller.submit_message("/armed")
    assert view.armed_targets == [None]
    assert view.os_armed is False
    assert view.toasts() == []  # no refusal, no "start a session first"

    controller.submit_message("/armed")
    assert view.armed_targets == [None, None]
    assert view.os_armed is True


def test_armed_takes_an_explicit_on_or_off(
    controller: SessionController, view: FakeChatView
) -> None:
    """Explicit beats toggle for the case that matters: a user who is not sure
    what state they are in can say which one they want and get it."""
    controller.submit_message("/armed off")
    controller.submit_message("/armed off")  # ...and saying it twice is idempotent
    assert view.armed_targets == [False, False]
    assert view.os_armed is False

    controller.submit_message("/armed ON")
    assert view.armed_targets == [False, False, True]
    assert view.os_armed is True


def test_an_unparseable_armed_argument_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    """A typo must never be read as either half of a safety switch."""
    controller.submit_message("/armed maybe")
    assert view.armed_targets == []
    assert view.os_armed is True
    assert any("usage: /armed [on|off]" in message for message, _ in view.notifications)


def test_the_engine_never_hears_about_the_armed_switch(
    controller: SessionController, view: FakeChatView
) -> None:
    """The line between this and `/yolo`: YOLO is one session's policy (audited
    into that session's log, dead when it ends), while ARMED is a property of the
    machine the user is sitting at. So this one starts no engine call and needs
    no session - it never even reaches for one."""
    controller.submit_message("/armed off")
    assert view.armed_targets == [False]
    assert controller._engine is None
    assert view.events == []  # no transcript note, no audit trail


# -- /mode ---------------------------------------------------------------------
# The permission mode is engine state (audited into the session log, dead when
# the session ends), so unlike `/armed` every one of these goes through a live
# session - and the mirror the controller keeps has to agree with it afterwards,
# because a bare `/mode` and the status bar both read the mirror.


async def test_mode_switches_the_session_and_says_what_changed(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)

    controller.submit_message("/mode plan")
    await settle(view)

    assert controller.permission_mode == "plan"
    assert controller._snap is not None and controller._snap.mode == "plan"
    assert any("exploration only" in note for note in view.notes())
    assert any("mode: PLAN" in message for message, _ in view.alerts)


async def test_mode_reads_the_word_the_user_typed(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)

    controller.submit_message("/mode UNATTENDED")
    await settle(view)

    assert controller.permission_mode == "unattended"
    assert any("nothing will ask you" in note for note in view.notes())


async def test_switching_to_unattended_under_yolo_says_which_one_wins(
    controller: SessionController, view: FakeChatView
) -> None:
    """The two settings pull opposite ways - YOLO still auto-APPROVES the calls
    unattended would have denied - and a user who is walking away has to know."""
    await start_session(controller, view)
    controller.submit_message("/yolo on")
    await settle(view)

    controller.submit_message("/mode unattended")
    await settle(view)

    assert any("CAUTION: YOLO is ON" in note for note in view.notes())


async def test_bare_mode_reports_rather_than_cycles(
    controller: SessionController, view: FakeChatView
) -> None:
    """Three states, so "the next one" is not a thing a user can aim at blind."""
    await start_session(controller, view)
    controller.submit_message("/mode plan")
    await settle(view)
    before = len(view.notes())

    controller.submit_message("/mode")
    await settle(view)

    assert controller.permission_mode == "plan"  # unchanged
    assert len(view.notes()) == before  # nothing announced, nothing audited
    assert any("permission mode: plan" in message for message in view.toasts())


async def test_cycle_walks_ask_plan_unattended_and_back(
    controller: SessionController, view: FakeChatView
) -> None:
    """What the TUI's mode key calls. The order is the registry's, so the two
    front-ends cannot disagree about what "next" means."""
    await start_session(controller, view)
    seen = [controller.permission_mode]
    for _ in range(3):
        controller.cycle_permission_mode()
        await settle(view)
        seen.append(controller.permission_mode)

    assert seen == ["ask", "plan", "unattended", "ask"]


async def test_an_unparseable_mode_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)

    controller.submit_message("/mode planning")
    await settle(view)

    assert controller.permission_mode == "ask"
    assert any("usage: /mode [ask|plan|unattended]" in message for message in view.toasts())


# -- the mode before, between and across sessions --------------------------------
# The dial is the user's, not a session's. "Only explore, do not change anything"
# is a decision made about the task one is ABOUT to describe, so it has to be
# reachable at the start prompt - and it has to still be true after /new, or the
# next session's first edit lands on somebody who thought they had turned changes
# off.


async def test_the_mode_can_be_set_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    """No engine to tell, so only the engine half is skipped: the mirror moves,
    the transcript says why, and the status bar is repainted (its MODE segment
    falls back to ``permission_mode`` when there is no snapshot)."""
    pushes = len(view.states)

    controller.set_permission_mode("plan")
    await settle(view)

    assert controller.permission_mode == "plan"
    assert controller._engine is None
    assert any("exploration only" in note for note in view.notes())
    assert len(view.states) > pushes  # repainted, so the segment can follow
    assert view.states[-1].snapshot is None  # ...with no session behind it


async def test_cycling_works_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    """What shift+tab at the start prompt does. Not a no-op: the cycle is how a
    user arms plan mode for the session they are about to describe."""
    seen = [controller.permission_mode]
    for _ in range(3):
        controller.cycle_permission_mode()
        await settle(view)
        seen.append(controller.permission_mode)

    assert seen == ["ask", "plan", "unattended", "ask"]


async def test_mode_command_works_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.submit_message("/mode unattended")
    await settle(view)

    assert controller.permission_mode == "unattended"
    assert not any("start a session" in message for message in view.toasts())


async def test_a_mode_chosen_before_the_session_governs_its_first_turn(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The point of the whole pre-session half: the engine is armed with the
    dialled-in mode BEFORE the bootstrap goes out, so the very first edit of the
    very first turn is refused rather than gated."""
    controller.set_permission_mode("plan")
    await settle(view)

    await start_session(controller, view)
    assert controller._snap is not None and controller._snap.mode == "plan"

    before = (project / "src" / "utils.py").read_text(encoding="utf-8")
    controller.submit_clipboard(
        edit_reply("src/utils.py", "return 1", "return 2", chat=MASTER_CHAT)
    )
    await settle(view)

    assert view.gates == []  # plan denies; it does not ask
    assert (project / "src" / "utils.py").read_text(encoding="utf-8") == before
    assert "plan mode is active" in view.copied[-1]


async def test_a_pre_session_mode_is_never_announced_as_a_change(
    controller: SessionController, view: FakeChatView
) -> None:
    """There was no conversation to change it under. The mode is in force from
    turn one and every denial body says so, so a "the mode is now plan" note in
    the first results payload would be announcing something that never happened."""
    controller.set_permission_mode("plan")
    await settle(view)
    await start_session(controller, view)

    controller.submit_clipboard(read_file_reply("README.md", chat=MASTER_CHAT))
    await settle(view)

    assert all("permission mode is now" not in copied for copied in view.copied)


async def test_the_mode_survives_a_new_session(
    controller: SessionController, view: FakeChatView
) -> None:
    """/new replaces the conversation, not the user. Unlike YOLO (which goes back
    to its configured default) the mode is carried over, and the next session's
    engine is armed with it before its first payload."""
    await start_session(controller, view)
    controller.submit_message("/mode plan")
    await settle(view)
    view.specs.append(SessionSpec(task="A second task.", service="claude"))

    assert controller.request_new_session() is True
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert controller.permission_mode == "plan"
    assert controller._snap is not None and controller._snap.mode == "plan"


def test_match_prefix_narrows_as_the_user_types() -> None:
    assert match_prefix("/") == COMMANDS  # a bare slash offers everything
    assert [c.name for c in match_prefix("/y")] == ["yolo"]
    assert [c.name for c in match_prefix("/m")] == ["mcp", "mode"]  # registry order
    assert [c.name for c in match_prefix("/mo")] == ["mode"]
    assert [c.name for c in match_prefix("/mc")] == ["mcp"]
    assert [c.name for c in match_prefix("/i")] == ["identify"]
    assert [c.name for c in match_prefix("/n")] == ["new"]
    assert [c.name for c in match_prefix("/yolo")] == ["yolo"]
    assert [c.name for c in match_prefix("/YO")] == ["yolo"]


def test_match_prefix_is_empty_for_everything_that_is_not_a_command_in_progress() -> None:
    assert match_prefix("") == ()
    assert match_prefix("hello") == ()
    assert match_prefix(" /y") == ()  # not a leading slash
    assert match_prefix("//escaped") == ()  # the literal-slash escape hatch
    assert match_prefix("/xyz") == ()  # nothing matches
    assert match_prefix("/yolo ") == ()  # committed: typing the argument now
    assert match_prefix("/yolo on") == ()
    assert match_prefix("/etc/hosts") == ()  # a path is not a prefix of anything
    assert match_prefix("/\n") == ()


def test_aliases_are_dispatch_only_and_never_offered() -> None:
    """`/commands` runs, but the popup teaches one spelling per command."""
    assert lookup("commands") is not None
    assert match_prefix("/comm") == ()
    assert all(command.name != "commands" for command in COMMANDS)


def test_label_falls_back_to_the_bare_name_without_an_argument() -> None:
    assert ChatCommand(name="new", summary="x").label == "/new"
    assert ChatCommand(name="yolo", summary="x", arg="[on|off]").label == "/yolo [on|off]"
