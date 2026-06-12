"""The single Textual Pilot smoke test (architecture.md section 8).

Boots the real app over a tmp project with a FakeClipboard, drives the
NewSessionScreen, injects an LLM reply by posting ClipboardCaptured directly
(the documented injectable path - deterministic, no watcher-thread timing),
approves the edit gate with "y", and asserts: the edit landed ON DISK, the
results payload was written to the fake clipboard, and the transcript shows
the result.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import TextArea

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured
from agentclip.tui.screens.new_session import NewSessionScreen

UTILS_PY = '''"""Utility helpers."""

from datetime import datetime


def parse_date(s):
    # NOTE: legacy format
    return datetime.strptime(s, "%d/%m/%Y")
'''

REPLY_WITH_EDIT = """I'll fix the date format.

~~~~
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace <<EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
~~~~
"""


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def test_smoke_full_loop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "utils.py").write_text(UTILS_PY, encoding="utf-8", newline="")
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(config, project),
        project_root=project,
    )

    async with app.run_test(size=(110, 40)) as pilot:
        # -- NewSessionScreen: type the task, ctrl+enter starts ------------------
        await _wait_for(pilot, lambda: isinstance(app.screen, NewSessionScreen), "session modal")
        app.screen.query_one("#task", TextArea).load_text(
            "Fix the date parsing bug in src/utils.py: use ISO dates."
        )
        await pilot.press("ctrl+enter")

        # -- the bootstrap payload was written to the (fake) clipboard ----------
        await _wait_for(pilot, lambda: bool(fake.written), "bootstrap on the clipboard")
        bootstrap = fake.written[0]
        assert "===CLIP:TASK===" in bootstrap
        assert "Fix the date parsing bug" in bootstrap
        assert "edit_file" in bootstrap  # tool catalog made it into the bootstrap

        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.session_active, "session armed")

        # -- inject the LLM reply (documented injectable path) -------------------
        main.post_message(ClipboardCaptured(REPLY_WITH_EDIT))
        await _wait_for(pilot, lambda: main.pending_approval, "approval gate")
        panel = main.action_panel
        assert panel.display, "ActionPanel must be visible at the gate"
        action = panel.current_action
        assert action is not None and action.kind == "edit"
        assert '"%Y-%m-%d"' in action.preview  # the unified diff carries the replacement

        # -- approve with a single keypress ---------------------------------------
        await pilot.press("y")
        await _wait_for(pilot, lambda: len(fake.written) >= 2, "results on the clipboard")
        results = fake.written[-1]
        assert "===CLIP:RESULTS turn=2===" in results
        assert "status=ok" in results
        assert "replaced 1 occurrence" in results

        # THE EDIT IS ON DISK.
        on_disk = (project / "src" / "utils.py").read_text(encoding="utf-8")
        assert '"%Y-%m-%d"' in on_disk
        assert "%d/%m/%Y" not in on_disk

        # -- transcript shows the result; engine is armed for the next reply ------
        await _wait_for(
            pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for the next reply"
        )
        assert not main.pending_approval
        assert any("replaced 1 occurrence" in entry for entry in main.transcript.entries)
        assert any("results copied" in entry for entry in main.transcript.entries)
        assert any("✓ approved edit_file" in entry for entry in main.transcript.entries)
