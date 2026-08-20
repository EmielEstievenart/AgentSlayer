"""The slash-command registry, and the two things that must derive from it.

The point of ``agentclip.shell.app.commands`` is that there is exactly one list. So
these tests are less about the strings than about the joins: the controller's
dispatch table covers the registry and nothing else, `/help` and the "unknown
command" hint are rendered from the same entries, and the autocomplete trigger
agrees with the dispatcher about what counts as a command in progress.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import agentclip.config
from agentclip.config import Config, project_permissions_path
from agentclip.executor.permissions import DEFAULT_CONFIG
from agentclip.shell.app.commands import (
    COMMANDS,
    ChatCommand,
    command_list,
    help_text,
    lookup,
    match_prefix,
)
from agentclip.shell.app.controller import SessionController
from agentclip.shell.app.types import SessionSpec

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
        "skills",
        "armed",
        "mode",
        "theme",
        "config",
        "unattended",
        "yolo",
    ]
    assert lookup("yolo") is not None and lookup("yolo").arg == "[on|off]"  # type: ignore[union-attr]
    assert lookup("mode") is not None and lookup("mode").arg == "[plan|build]"  # type: ignore[union-attr]
    assert lookup("unattended") is not None and lookup("unattended").arg == "[on|off]"  # type: ignore[union-attr]
    assert all(command.summary for command in COMMANDS)


def test_yolo_is_never_the_first_row() -> None:
    """Regression (tui.md §3.3a): the popup renders this tuple as typed, so the
    top row is whatever a stray Enter is nearest to - and `/yolo` turns EVERY
    approval gate off. It goes last, behind the reversible ones."""
    assert COMMANDS[-1].name == "yolo"
    assert COMMANDS[0].name == "help"  # ...and the top row is the harmless one
    # The other gate-answering command sits with it, one row above: it only ever
    # refuses, so it goes above the one that approves.
    assert COMMANDS[-2].name == "unattended"


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
    assert hint == (
        "/help, /new, /abort, /identify, /log, /mcp, /skills, /armed, /mode, /theme, "
        "/config, /unattended, or /yolo"
    )


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


@dataclass(frozen=True)
class McpRow:
    """Duck-typed ``McpStatusLine``: the app layer never imports agentclip.executor.mcp,
    so a row is whatever answers to these four names (``shell/app/link.py``)."""

    name: str
    state: str
    detail: str = ""
    tool_count: int = 0


def test_mcp_without_a_source_says_mcp_is_not_configured(
    controller: SessionController, view: FakeChatView
) -> None:
    """The fixture controller is built without ``mcp_statuses`` - the app shape
    when permissions.json has no mcp block - so the command answers with a toast,
    not a transcript listing, and needs no session to do it."""
    controller.submit_message("/mcp")
    assert view.events == []
    assert any("MCP is not configured" in message for message in view.toasts())


def test_mcp_switched_off_in_config_says_so_rather_than_not_configured(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """`[mcp] enabled = false` never opens permissions.json, so the listing is
    empty for a reason that has nothing to do with the servers in it. Telling
    that user to go and add servers sends them to edit a file that already has
    the ones they want; the dial they actually turned off is named instead.

    The only case this layer can tell apart, and it needs no wire op to do it:
    the config a controller holds is the SESSION's, which ``rebind`` replaces
    with the target's on a remote connect.
    """
    off = replace(app_config, mcp=replace(app_config.mcp, enabled=False))
    controller = SessionController(off, make_factory(project), project, view=view)
    view.controller = controller

    controller.submit_message("/mcp")

    assert view.events == []
    (toast,) = view.toasts()
    assert "MCP is disabled" in toast
    assert "enabled = true" in toast


async def test_mcp_lists_every_server_with_state_tools_and_detail(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """`/mcp` renders the supplier's rows into one transcript note - state
    always, tool count when connected, the detail line whenever there is one -
    and needs no session (`/log`'s rule: "where are my server's tools?" is
    asked before a session as readily as during one).

    This is also the FALLBACK path in full: with no session there is no link to
    ask, so the constructor's callable is the whole answer and it is read
    synchronously.
    """
    rows = (
        McpRow("github", "connected", tool_count=12),
        McpRow("linear", "needs_auth", detail="server rejected the request (401/403)"),
        McpRow("scratch", "disabled"),
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


async def test_mcp_reads_the_live_link_rather_than_the_constructor_source(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """With a session live, `/mcp` asks the LINK - the only thing that knows
    which machine the servers are on.

    MCP servers spawn where the engine runs (docs/design/remote-executor.md
    §2.7), so a remote session's listing must be the target's. The two sources
    are scripted to disagree, which is the only way to tell which one answered:
    the constructor's is this PC's, the link's is the machine the session was
    built on.
    """
    on_the_box = (McpRow("github", "connected", tool_count=12),)
    this_pc = (McpRow("stale-local", "connected", tool_count=1),)
    controller = SessionController(
        app_config,
        make_factory(project, mcp_statuses=lambda: on_the_box),
        project,
        view=view,
        mcp_statuses=lambda: this_pc,
    )
    view.controller = controller
    await start_session(controller, view)

    controller.submit_message("/mcp")
    await settle(view)

    note = next(text for text in view.notes() if text.startswith("MCP servers:"))
    assert "github · connected · 12 tools" in note
    assert "stale-local" not in note


async def test_rebind_points_the_pre_session_mcp_source_at_the_new_machine(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """A reconnect changes which machine the NEXT session's servers are on.

    ``rebind`` takes the four things a session is built from, and MCP is one of
    them: without it, a GUI that had connected to a box went on answering a
    pre-session `/mcp` out of the previous target's source. Passing nothing
    keeps whatever is current - the local callers have nothing new to say.
    """
    controller = SessionController(
        app_config,
        make_factory(project),
        project,
        view=view,
        mcp_statuses=lambda: (McpRow("this-pc", "connected", tool_count=3),),
    )
    view.controller = controller

    assert controller.rebind(
        app_config,
        make_factory(project),
        project,
        mcp_statuses=lambda: (McpRow("on-the-box", "connected", tool_count=9),),
    )
    controller.submit_message("/mcp")
    await settle(view)
    note = next(text for text in view.notes() if text.startswith("MCP servers:"))
    assert "on-the-box · connected · 9 tools" in note
    assert "this-pc" not in note

    view.events.clear()
    assert controller.rebind(app_config, make_factory(project), project)
    controller.submit_message("/mcp")
    await settle(view)
    assert "on-the-box" in next(text for text in view.notes() if text.startswith("MCP servers:"))


@dataclass(frozen=True)
class SkillRow:
    """Duck-typed ``SkillLine``: the app layer never imports agentclip.executor.tools,
    so a row is whatever answers to these four names (``shell/app/link.py``)."""

    name: str
    description: str = ""
    folder: str = ""
    model_invocable: bool = True


@dataclass(frozen=True)
class SkillsFound:
    """Duck-typed ``SkillReport`` - the rows, and where they were looked for."""

    skills: tuple[SkillRow, ...] = ()
    searched: tuple[str, ...] = ()


async def test_skills_lists_every_skill_with_its_description_and_folder(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """`/skills` answers the question the bootstrap's one-line listing cannot:
    WHICH copy of a skill is loaded, and out of which of the six folders.

    The fallback path in full - no session, so the constructor's callable is the
    whole answer and it is read synchronously (`/mcp`'s arrangement).
    """
    report = SkillsFound(
        skills=(
            SkillRow("tdd", "test-first development", "/proj/.claude/skills/tdd"),
            SkillRow("release", "cut a release", "/home/dev/.agents/skills/release", False),
        ),
        searched=("/proj/.claude/skills",),
    )
    controller = SessionController(
        app_config, make_factory(project), project, view=view, skills=lambda: report
    )
    view.controller = controller

    controller.submit_message("/skills")
    await settle(view)

    note = next(text for text in view.notes() if text.startswith("Skills:"))
    assert "tdd - test-first development  (/proj/.claude/skills/tdd)" in note
    # The ones the model may not call are listed too, and marked: a skill that
    # sets disable-model-invocation is loaded and simply unreachable, which is
    # exactly what somebody typing /skills is trying to find out.
    assert "release - cut a release  (/home/dev/.agents/skills/release)  [hidden from the model]" in (
        note
    )
    assert "[hidden from the model]" not in note.splitlines()[1]  # ...and only on that one


async def test_skills_with_nothing_found_names_the_folders_it_searched(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """The empty listing is the useful one: "no skills" is only actionable
    beside the folders that were scanned, so those are the answer."""
    controller = SessionController(
        app_config,
        make_factory(project),
        project,
        view=view,
        skills=lambda: SkillsFound(searched=("/proj/.claude/skills", "/home/dev/.claude/skills")),
    )
    view.controller = controller

    controller.submit_message("/skills")
    await settle(view)

    note = next(text for text in view.notes() if text.startswith("No skills found"))
    assert "/proj/.claude/skills" in note
    assert "/home/dev/.claude/skills" in note


async def test_skills_reads_the_live_link_rather_than_the_constructor_source(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """With a session live, `/skills` asks the LINK.

    Skills are discovered where the engine runs (docs/design/remote-ssh.md
    decision 6), so a remote session's listing must name the target's folders.
    The two sources are scripted to disagree, which is the only way to tell which
    one answered.
    """
    on_the_box = SkillsFound(skills=(SkillRow("deploy", "ship it", "/srv/app/.claude/skills/deploy"),))
    this_pc = SkillsFound(skills=(SkillRow("stale-local", "", "C:/dev/.claude/skills/x"),))
    controller = SessionController(
        app_config,
        make_factory(project, skills=lambda: on_the_box),
        project,
        view=view,
        skills=lambda: this_pc,
    )
    view.controller = controller
    await start_session(controller, view)

    controller.submit_message("/skills")
    await settle(view)

    note = next(text for text in view.notes() if text.startswith("Skills:"))
    assert "deploy - ship it  (/srv/app/.claude/skills/deploy)" in note
    assert "stale-local" not in note


async def test_skills_without_a_source_says_nothing_was_found_or_searched(
    controller: SessionController, view: FakeChatView
) -> None:
    """The fixture controller is built without a skills source. It must still
    answer - and needs no session to do it."""
    controller.submit_message("/skills")
    await settle(view)
    assert any(text.startswith("No skills found") for text in view.notes())


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
    assert controller._link is None
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

    controller.submit_message("/mode PLAN")
    await settle(view)

    assert controller.permission_mode == "plan"
    assert any("exploration only" in note for note in view.notes())


async def test_switching_unattended_on_under_yolo_says_which_one_wins(
    controller: SessionController, view: FakeChatView
) -> None:
    """The two settings pull opposite ways - YOLO still auto-APPROVES the calls
    unattended would have denied - and a user who is walking away has to know."""
    await start_session(controller, view)
    controller.submit_message("/yolo on")
    await settle(view)

    controller.set_unattended(True)
    await settle(view)

    assert any("CAUTION: YOLO is ON" in note for note in view.notes())


async def test_bare_mode_reports_rather_than_cycles(
    controller: SessionController, view: FakeChatView
) -> None:
    """A named state is not a thing a user can aim at blind by cycling."""
    await start_session(controller, view)
    controller.submit_message("/mode plan")
    await settle(view)
    before = len(view.notes())

    controller.submit_message("/mode")
    await settle(view)

    assert controller.permission_mode == "plan"  # unchanged
    assert len(view.notes()) == before  # nothing announced, nothing audited
    assert any("permission mode: plan" in message for message in view.toasts())


async def test_cycle_walks_build_plan_and_back(
    controller: SessionController, view: FakeChatView
) -> None:
    """What the TUI's mode key calls. The order is the registry's, so the two
    front-ends cannot disagree about what "next" means."""
    await start_session(controller, view)
    seen = [controller.permission_mode]
    for _ in range(2):
        controller.cycle_permission_mode()
        await settle(view)
        seen.append(controller.permission_mode)

    assert seen == ["build", "plan", "build"]


async def test_an_unparseable_mode_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)

    controller.submit_message("/mode planning")
    await settle(view)

    assert controller.permission_mode == "build"
    assert any("usage: /mode [build|plan]" in message for message in view.toasts())


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
    assert controller._link is None
    assert any("exploration only" in note for note in view.notes())
    assert len(view.states) > pushes  # repainted, so the segment can follow
    assert view.states[-1].snapshot is None  # ...with no session behind it


async def test_cycling_works_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    """What shift+tab at the start prompt does. Not a no-op: the cycle is how a
    user arms plan mode for the session they are about to describe."""
    seen = [controller.permission_mode]
    for _ in range(2):
        controller.cycle_permission_mode()
        await settle(view)
        seen.append(controller.permission_mode)

    assert seen == ["build", "plan", "build"]


async def test_mode_command_works_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.submit_message("/mode plan")
    await settle(view)

    assert controller.permission_mode == "plan"
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


# -- the unattended switch -------------------------------------------------------
# The mode's twin, and scoped like it rather than like YOLO: "I have stepped
# away" is a statement about the USER, so it can be thrown before a session
# exists, it survives /new, and it reaches every engine the app builds.


async def test_unattended_can_be_armed_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    pushes = len(view.states)

    controller.set_unattended(True)
    await settle(view)

    assert controller.unattended is True
    assert controller._link is None
    assert any("UNATTENDED ON" in note for note in view.notes())
    assert len(view.states) > pushes


async def test_unattended_reaches_the_engine_and_survives_a_new_session(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    controller.set_unattended(True)
    await wait_for(lambda: controller.unattended, "unattended armed")
    await settle(view)
    assert controller._snap is not None and controller._snap.unattended is True

    view.specs.append(SessionSpec(task="A second task.", service="claude"))
    assert controller.request_new_session() is True
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert controller.unattended is True
    assert controller._snap is not None and controller._snap.unattended is True


async def test_turning_unattended_off_says_the_gates_are_back(
    controller: SessionController, view: FakeChatView
) -> None:
    await start_session(controller, view)
    controller.set_unattended(True)
    await wait_for(lambda: controller.unattended, "unattended armed")

    controller.set_unattended(False)
    await wait_for(lambda: not controller.unattended, "unattended cleared")
    await settle(view)

    assert any("UNATTENDED OFF" in note for note in view.notes())
    assert controller._snap is not None and controller._snap.unattended is False


async def test_the_unattended_command_sets_it_explicitly(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.submit_message("/unattended on")
    await settle(view)
    assert controller.unattended is True

    controller.submit_message("/unattended off")
    await settle(view)
    assert controller.unattended is False


async def test_a_bare_unattended_toggles_against_the_mirror(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/yolo`'s bare form, for its reason: "is anyone watching" is one bit the
    user is either setting or clearing, so the command toggles rather than
    reporting the way `/mode` does."""
    controller.submit_message("/unattended")
    await settle(view)
    assert controller.unattended is True

    controller.submit_message("/unattended")
    await settle(view)
    assert controller.unattended is False


async def test_an_unparseable_unattended_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.submit_message("/unattended maybe")
    await settle(view)

    assert controller.unattended is False
    assert view.notes() == []  # nothing was applied, so nothing was announced
    assert any("usage: /unattended [on|off]" in message for message in view.toasts())


# -- /yolo -----------------------------------------------------------------------
# YOLO stays one session's policy - audited into that session's log, back to the
# configured default when it ends - but "approve everything I am about to ask for"
# is a decision made about the task one is ABOUT to describe. So the command is
# reachable at the start prompt, where only the mirror moves; the engine the next
# session builds is armed with it before its first payload.


async def test_yolo_can_be_armed_before_any_session_exists(
    controller: SessionController, view: FakeChatView
) -> None:
    """No engine to tell and no conversation to announce it to, so the engine half
    is skipped entirely: the mirror moves, the status bar is repainted (its edits
    segment falls back to ``yolo`` when there is no snapshot), and the toast says
    the state is armed rather than in force."""
    pushes = len(view.states)

    controller.submit_message("/yolo on")
    await settle(view)

    assert controller.yolo is True
    assert controller._link is None
    assert view.events == []  # nothing to audit into, nothing to announce
    assert len(view.states) > pushes
    assert view.states[-1].snapshot is None
    assert any("YOLO will be ON when the next session starts" in m for m in view.toasts())


async def test_yolo_before_a_session_toggles_against_the_mirror(
    controller: SessionController, view: FakeChatView
) -> None:
    """Bare `/yolo` still means "the other one" with no engine to read it off."""
    controller.submit_message("/yolo")
    await settle(view)
    assert controller.yolo is True

    controller.submit_message("/yolo")
    await settle(view)
    assert controller.yolo is False
    assert any("YOLO will be OFF when the next session starts" in m for m in view.toasts())


async def test_an_unparseable_yolo_before_a_session_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    """A typo must never be read as either half of the approval switch - the same
    rule as `/armed`, and it applies before the session as much as during it."""
    controller.submit_message("/yolo maybe")
    await settle(view)

    assert controller.yolo is False
    assert any("usage: /yolo [on|off]" in message for message in view.toasts())


async def test_a_yolo_armed_before_the_session_governs_its_first_turn(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The point of the whole pre-session half: the engine is armed with the
    dialled-in state BEFORE the bootstrap goes out, so the very first edit of the
    very first turn auto-approves rather than opening a gate."""
    controller.submit_message("/yolo on")
    await settle(view)

    await start_session(controller, view)
    assert controller._snap is not None and controller._snap.yolo is True

    controller.submit_clipboard(
        edit_reply("src/utils.py", "return 1", "return 2", chat=MASTER_CHAT)
    )
    await settle(view)

    assert view.gates == []  # YOLO approves; it does not ask
    assert (project / "src" / "utils.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"


async def test_an_untouched_yolo_mirror_tells_the_new_engine_nothing(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The engine builds its policy from the same configured default the mirror
    started at, so a session begun without a pre-arm must write no yolo audit line
    - an event about a change nobody made is a lie in the log."""
    await start_session(controller, view)

    assert controller._snap is not None and controller._snap.yolo is False
    log = next(project.glob(".agentclip/sessions/*/transcript.jsonl"))
    assert '"t": "yolo"' not in log.read_text(encoding="utf-8")


async def test_yolo_still_dies_with_the_session(
    controller: SessionController, view: FakeChatView
) -> None:
    """Unlike the permission mode (the user's dial, carried over), YOLO is a
    property of one conversation: /new puts the configured default back, and the
    next session's engine is left alone because the mirror agrees with it again."""
    await start_session(controller, view)
    controller.submit_message("/yolo on")
    await settle(view)
    view.specs.append(SessionSpec(task="A second task.", service="claude"))

    assert controller.request_new_session() is True
    await wait_for(lambda: view.cleared > 0, "the session was reset")
    await settle(view)

    assert controller.yolo is False
    assert controller._snap is not None and controller._snap.yolo is False


# -- /theme ---------------------------------------------------------------------
# The one command whose vocabulary the controller does not know: a theme name
# means something in a shell, not in the app layer (Textual themes in the TUI,
# CSS palettes in the GUI - overlapping by the Claude pair and by nothing else),
# so every one of these goes through the port - the choices are read back before
# anything is applied.


async def test_bare_theme_lists_the_choices_and_marks_the_current_one(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/help`'s shape rather than `/mode`'s: the names are not guessable and
    differ per shell, so the answer is a transcript note worth keeping rather
    than a toast that is gone in eight seconds."""
    view.theme = "claude-warm"

    controller.submit_message("/theme")
    await settle(view)

    note = next(text for text in view.notes() if text.startswith("themes:"))
    for name in view.themes:
        assert name in note
    assert "claude-warm (current)" in note
    assert "/theme <name> to switch" in note
    assert view.themes_applied == []  # a listing changes nothing


async def test_bare_theme_needs_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/armed`'s rule, minus the urgency: appearance belongs to the machine the
    user is sitting at, not to a conversation."""
    controller.submit_message("/theme")
    await settle(view)

    assert view.toasts() == []  # no refusal, no "start a session first"


def test_theme_with_a_valid_name_applies_and_persists_it(
    controller: SessionController, view: FakeChatView
) -> None:
    """One port call, which is the whole change - the view wears it AND
    remembers it, because the two shells save into two different config
    tables and only they know which."""
    controller.submit_message("/theme claude-dark")

    assert view.themes_applied == ["claude-dark"]
    assert view.current_theme() == "claude-dark"
    assert view.toasts() == ["theme: claude-dark"]  # said once


def test_theme_reads_the_name_the_user_typed(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.submit_message("/theme  CLAUDE-WARM ")

    assert view.themes_applied == ["claude-warm"]


def test_setting_the_same_theme_again_is_idempotent(
    controller: SessionController, view: FakeChatView
) -> None:
    controller.submit_message("/theme claude-dark")
    controller.submit_message("/theme claude-dark")

    assert view.themes_applied == ["claude-dark", "claude-dark"]
    assert view.current_theme() == "claude-dark"


def test_an_unknown_theme_changes_nothing_and_says_so(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/armed`'s rule that a typo is never read as an instruction - and the
    refusal names the ones that would have worked, since the user has no other
    way to find them out."""
    controller.submit_message("/theme solarized")

    assert view.themes_applied == []
    assert view.current_theme() == "textual-dark"
    message, severity = view.notifications[-1]
    assert "unknown theme: solarized" in message
    assert severity == "warning"
    for name in view.themes:
        assert name in message


async def test_the_theme_names_are_the_views_not_the_controllers(
    controller: SessionController, view: FakeChatView
) -> None:
    """A list shaped like the GUI's through the same controller. The two shells
    overlap - ``claude-dark`` is a real name in both - but neither list is the
    other's, and ``textual-dark`` is the TUI's alone. Nothing in the app layer
    may hold a theme name it did not read back from the port, which is why the
    refusal below has to come from the VIEW's list and not from a set of names
    the controller knows."""
    view.themes = ("dark", "light", "claude-warm", "claude-dark")
    view.theme = "dark"

    controller.submit_message("/theme light")
    assert view.themes_applied == ["light"]

    controller.submit_message("/theme textual-dark")
    await settle(view)
    assert view.themes_applied == ["light"]  # refused, and by the view's list
    assert any("unknown theme: textual-dark" in message for message in view.toasts())


# -- /config ---------------------------------------------------------------------
# The permission + MCP ruleset is a FILE, read once at launch, and the app has no
# editor - so the command's whole job is to make sure the file exists and to hand
# the user its path. It needs no session for `/theme`'s reason (a ruleset is a
# property of a machine and a project, not of a conversation), and the path
# leaves through ``park_outbound`` because that is the one clipboard write the
# watcher is told about before it lands.


async def test_bare_config_reports_both_layers_without_creating_anything(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """`/mode`'s rule: a command naming two things must say where both are
    before it is asked to act on either."""
    controller.submit_message("/config")
    await settle(view)

    note = next(text for text in view.notes() if "permission + MCP ruleset" in text)
    assert str(agentclip.config.default_permissions_config_path()) in note
    assert str(project_permissions_path(project)) in note
    assert "not created yet" in note
    assert "restart AgentClip" in note
    assert not project_permissions_path(project).exists()  # a report writes nothing
    assert view.parked == []


async def test_config_local_creates_the_project_file_and_parks_its_path(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The template is the rules that were already in force plus the `mcp` block,
    so the editor the user opens next shows a working example of every key rather
    than a blank page - and creating the file changes nothing about the session."""
    controller.submit_message("/config local")
    await settle(view)

    path = project_permissions_path(project)
    assert json.loads(path.read_text(encoding="utf-8")) == {**DEFAULT_CONFIG, "mcp": {}}
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert view.parked == [str(path)]
    assert view.copied == []  # parked, never delivered: nothing is pasted anywhere
    assert any("created" in message for message in view.toasts())


async def test_config_global_creates_the_file_beside_the_config_toml(
    controller: SessionController, view: FakeChatView
) -> None:
    """The suite-wide fixture points the default path at a tmp file that does not
    exist, which is exactly the state a fresh install is in."""
    # Read through the module: the suite-wide fixture patches the ATTRIBUTE, and
    # a from-import here would hold the developer's real path instead.
    path = agentclip.config.default_permissions_config_path()
    assert not path.exists()

    controller.submit_message("/config global")
    await settle(view)

    assert json.loads(path.read_text(encoding="utf-8")) == {**DEFAULT_CONFIG, "mcp": {}}
    assert view.parked == [str(path)]


async def test_config_never_overwrites_a_ruleset_that_exists(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The user's rules are the whole point of the file: a second /config hands
    back the path it already had and leaves every byte alone."""
    path = project_permissions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"permission": {"bash": "deny"}}', encoding="utf-8")

    controller.submit_message("/config local")
    await settle(view)

    assert path.read_text(encoding="utf-8") == '{"permission": {"bash": "deny"}}'
    assert view.parked == [str(path)]
    assert any("found" in message for message in view.toasts())


async def test_a_typo_writes_nothing_and_says_what_would_have_worked(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """`/armed`'s rule that a typo is never read as an instruction, and it bites
    harder here because the acting branch writes a file."""
    controller.submit_message("/config globl")
    await settle(view)

    assert not project_permissions_path(project).exists()
    assert view.parked == []
    assert any("usage: /config [global|local|global reset" in message for message in view.toasts())


async def test_config_local_refuses_in_a_remote_session(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """The project is on the target and this shell writes files on THIS PC, so
    creating a same-named file here would put a ruleset nobody reads beside a
    project that is not here. Said, not guessed at."""
    remote = replace(app_config, remote=replace(app_config.remote, target="dev@box"))
    controller = SessionController(remote, make_factory(project), project, view=view)
    view.controller = controller

    controller.submit_message("/config local")
    await settle(view)

    assert not project_permissions_path(project).exists()
    assert view.parked == []
    assert any("not supported in a remote session" in m for m in view.toasts())
    assert any("dev@box" in m for m in view.toasts())

    # ...and the report says which machine's rules are actually in force, since
    # neither local file governs a remote session ("the target owns its policy").
    controller.submit_message("/config")
    await settle(view)
    note = next(text for text in view.notes() if "permission + MCP ruleset" in text)
    assert "this session's rules come from dev@box" in note


async def test_config_reset_writes_the_shipped_defaults_over_the_file(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The one branch that overwrites: the way out of a ruleset edited until
    nothing runs any more. It parks nothing - the answer is what the file now
    says, not where it is - and it repeats the restart note, which bites hardest
    here because the session is still running the rules read at launch."""
    path = project_permissions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"permission": {"bash": "deny"}}', encoding="utf-8")

    controller.submit_message("/config local reset")
    await settle(view)

    assert json.loads(path.read_text(encoding="utf-8")) == {**DEFAULT_CONFIG, "mcp": {}}
    assert view.parked == []
    assert any("reset" in note and "restart AgentClip" in note for note in view.notes())
    # ...and it is truthful about which restart: /new does not re-read the file.
    assert any("not even /new re-reads them" in note for note in view.notes())


async def test_config_reset_keeps_the_mcp_block_it_found(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """`mcp` is the one top-level key that has nothing to do with permissions, so
    losing a configured server to a permissions reset would be a surprise."""
    path = project_permissions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    servers = {"fs": {"command": "npx", "args": ["-y", "server-filesystem"]}}
    path.write_text(json.dumps({"permission": {"bash": "deny"}, "mcp": servers}), encoding="utf-8")

    controller.submit_message("/config local reset")
    await settle(view)

    assert json.loads(path.read_text(encoding="utf-8")) == {**DEFAULT_CONFIG, "mcp": servers}
    assert any("mcp block was kept" in note for note in view.notes())


async def test_config_reset_replaces_a_file_that_does_not_parse(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """Exactly the file this branch exists to get out of: nothing to carry
    across, so it is replaced outright rather than repaired."""
    path = project_permissions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"permission": {,,,', encoding="utf-8")

    controller.submit_message("/config local reset")
    await settle(view)

    assert json.loads(path.read_text(encoding="utf-8")) == {**DEFAULT_CONFIG, "mcp": {}}


async def test_config_reset_global_creates_the_directories_it_needs(
    controller: SessionController, view: FakeChatView
) -> None:
    """A reset on an install that never created the file is still a reset: it
    writes the defaults, parent directories and all."""
    path = agentclip.config.default_permissions_config_path()
    assert not path.exists()

    controller.submit_message("/config global reset")
    await settle(view)

    assert json.loads(path.read_text(encoding="utf-8")) == {**DEFAULT_CONFIG, "mcp": {}}


async def test_config_reset_local_refuses_in_a_remote_session(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """`/config local`'s refusal, and more sharply: the file it would overwrite
    governs nothing, because the project is on the target."""
    remote = replace(app_config, remote=replace(app_config.remote, target="dev@box"))
    controller = SessionController(remote, make_factory(project), project, view=view)
    view.controller = controller

    controller.submit_message("/config local reset")
    await settle(view)

    assert not project_permissions_path(project).exists()
    assert any("not supported in a remote session" in m for m in view.toasts())


async def test_a_reset_typo_writes_nothing_either(
    controller: SessionController, view: FakeChatView, project: Path
) -> None:
    """The layer comes first and the trailing word has to BE "reset": the branch
    that overwrites a file is the last one that may be reached by a near miss.
    The old `reset <layer>` order is a near miss like any other now - it is not
    kept alive as an alias, so a user who learned it is told the shape once."""
    for typo in (
        "/config localreset",
        "/config reset",
        "/config local resett",
        "/config reset local",
        "/config reset global",
    ):
        controller.submit_message(typo)
        await settle(view)

    assert not project_permissions_path(project).exists()
    assert all("usage: /config" in message for message in view.toasts())


async def test_config_works_with_no_session_of_any_kind(
    controller: SessionController, view: FakeChatView
) -> None:
    """`/theme`'s gate, or rather its absence: the file governs the session the
    user is about to start, which is precisely when they reach for it."""
    controller.submit_message("/config")
    await settle(view)

    assert controller._link is None
    assert not any("start a session" in message for message in view.toasts())


def test_match_prefix_narrows_as_the_user_types() -> None:
    assert match_prefix("/") == COMMANDS  # a bare slash offers everything
    assert [c.name for c in match_prefix("/y")] == ["yolo"]
    assert [c.name for c in match_prefix("/m")] == ["mcp", "mode"]  # registry order
    assert [c.name for c in match_prefix("/mo")] == ["mode"]
    assert [c.name for c in match_prefix("/mc")] == ["mcp"]
    assert [c.name for c in match_prefix("/i")] == ["identify"]
    assert [c.name for c in match_prefix("/c")] == ["config"]
    assert [c.name for c in match_prefix("/n")] == ["new"]
    assert [c.name for c in match_prefix("/t")] == ["theme"]
    assert [c.name for c in match_prefix("/u")] == ["unattended"]
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
