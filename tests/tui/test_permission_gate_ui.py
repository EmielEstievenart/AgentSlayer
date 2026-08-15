"""Pilot tests for the gate under a permission ruleset (an opencode.json).

Two things change on screen once rules govern a session, and only these two: the
middle button is offered for COMMANDS as well as edits, and it names the exact
pattern pressing it would remember. The rest of the drawer - title, preview,
y/n - is the same widget the legacy tests already cover.

The ruleset is a temp file the test writes; nothing here reads the developer's
real ~/.config/opencode/opencode.json (the root conftest blocks that path).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import Button

from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured

# Everything asks: the gate is the whole subject here.
OPENCODE_JSON = '{"permission": {"*": "ask", "bash": {"*": "ask"}}}'

REPLY_ECHO_ONE = """Let me check.

~~~~
===CLIP:CALL id=1 tool=run_command===
command: echo one
reason: say one
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

REPLY_ECHO_TWO = """And again.

~~~~
===CLIP:CALL id=1 tool=run_command===
command: echo two
reason: say two
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 20.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> tuple[AgentClipApp, FakeClipboard, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    rules = project / "opencode.json"
    rules.write_text(OPENCODE_JSON, encoding="utf-8")
    (project / ".agentclip.toml").write_text(
        f'[permission]\nopencode_config = "{rules.as_posix()}"\n', encoding="utf-8"
    )
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    assert config.permission_rules, "the session must start in ruleset mode"
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake, project


async def _start_session(app: AgentClipApp, pilot: Pilot) -> None:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main.composer.load_text("Echo something.")
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")


async def test_always_allow_is_offered_for_a_command_and_sticks(tmp_path: Path) -> None:
    app, fake, _ = _make_app(tmp_path)
    async with app.run_test(size=(110, 40)) as pilot:
        await _start_session(app, pilot)
        main = app.main_screen
        assert main is not None

        main.post_message(ClipboardCaptured(REPLY_ECHO_ONE))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate for the command")

        # A command gate, and the middle button is offered anyway - naming the
        # pattern it would remember, not just "always".
        assert main._gate_kind == "command"
        assert main._gate_always == "echo *"
        button = main.action_panel.query_one("#approve-edits-btn", Button)
        assert button.display
        assert str(button.label) == "Always: echo *  (a)"
        assert "until AgentClip restarts" in str(
            main.action_panel.query_one("#action-hints").render()
        )

        # `a` approves this call and remembers the rule.
        writes_before = len(fake.written)
        await pilot.press("a")
        await _wait_for(pilot, lambda: len(fake.written) > writes_before, "results copied")
        await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "re-armed")
        await _wait_for(pilot, lambda: not main.busy, "turn settled")
        assert "one" in fake.written[-1]  # the echo actually ran
        assert any("always allowing echo *" in e for e in main.transcript.entries)

        # The next command of the same shape never reaches a gate.
        main.post_message(ClipboardCaptured(REPLY_ECHO_TWO))
        await _wait_for(pilot, lambda: len(fake.written) > writes_before + 1, "second turn sent")
        await _wait_for(pilot, lambda: not main.busy, "second turn settled")
        assert not main.pending_approval
        assert "two" in fake.written[-1]
