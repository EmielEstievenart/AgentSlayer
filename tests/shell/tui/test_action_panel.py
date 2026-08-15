"""Unit tests for the approval drawer's preview renderable.

The command branch does not render the tool's preview string verbatim - it
rebuilds the block from the call's params - so the model's stated reason has to
be pulled in here too, or the one surface that must show it never would.
"""

from __future__ import annotations

from rich.text import Text

from agentclip.engine.engine import PendingAction
from agentclip.protocol.types import ToolCall
from agentclip.shell.tui.widgets.action_panel import preview_renderable


def command_action(**params: str) -> PendingAction:
    call = ToolCall(id=1, tool="run_command", params=dict(params), raw="")
    return PendingAction(call=call, kind="command", preview="", auto_reason=None)


def test_the_drawer_shows_the_reason_under_the_command() -> None:
    rendered = preview_renderable(
        command_action(command="rm -rf build", reason="clear the stale build tree")
    )
    assert isinstance(rendered, Text)
    lines = rendered.plain.splitlines()
    assert lines[0] == "$ rm -rf build"
    assert lines[1] == "reason: clear the stale build tree"


def test_a_multi_line_reason_stays_one_line_in_the_drawer() -> None:
    rendered = preview_renderable(
        command_action(command="pytest -q", reason="why\nI\nwant\nthis\n" * 40)
    )
    assert isinstance(rendered, Text)
    lines = rendered.plain.splitlines()
    assert lines[1].startswith("reason: why I want this")
    assert len(lines[1]) == len("reason: ") + 200


def test_a_call_without_a_reason_renders_as_before() -> None:
    rendered = preview_renderable(command_action(command="pytest -q"))
    assert isinstance(rendered, Text)
    lines = rendered.plain.splitlines()
    assert lines[0] == "$ pytest -q"
    assert not any(line.startswith("reason:") for line in lines)
