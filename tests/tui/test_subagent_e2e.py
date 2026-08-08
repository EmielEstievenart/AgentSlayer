"""End-to-end delegation through the real app: two chat windows, one keypress.

Everything below the composer is real - the Textual screen, the slot
calibrations, the tabbed transcript, both Engines, the protocol round trip. Only
the OS itself is faked (the region picker, screen capture, element probes,
clicks, the synthetic paste), exactly as the other screen tests do it.

What this pins down that the controller tests cannot:

* the sub-agent gets a second **tab**, live (``▶``) while it runs and ticked
  (``✓``) once it is done, with its own transcript;
* the sub-agent's chat window is **opened before anything is pasted** - asserted
  on one interleaved trace of clicks and clipboard writes, because a sub-agent
  bootstrap landing in the master's chat is the unrecoverable failure of this
  feature;
* the automation follows the live slot: the sub-agent's payload is pasted into
  the SUB-AGENT's chat box and the master's into the master's;
* the sub-agent's ``result`` comes back inside the master's results payload.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button

import agentclip.tui.screens.main as main_mod
from agentclip.app.types import EngineRequest
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.engine.engine import Engine
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import RegionImage
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot
from agentclip.tui.app import AgentClipApp
from agentclip.tui.messages import ClipboardCaptured
from agentclip.tui.screens.main import MainScreen

MASTER_CHAT = "amber-falcon"
SUB_CHAT = "jade-otter"

MASTER_BOX = ScreenRegion(10, 400, 300, 40)
MASTER_BUSY = ScreenRegion(10, 10, 40, 20)
MASTER_COPY = ScreenRegion(10, 300, 24, 24)
MASTER_NEWCHAT = ScreenRegion(10, 60, 80, 24)
SUB_BOX = ScreenRegion(900, 400, 300, 40)
SUB_BUSY = ScreenRegion(900, 10, 40, 20)
SUB_COPY = ScreenRegion(900, 300, 24, 24)
SUB_NEWCHAT = ScreenRegion(900, 60, 80, 24)

SIZE = (110, 100)

DELEGATE_REPLY = f"""I'll hand the survey off.

===CLIP:CALL id=1 tool=delegate===
task <<EOT
Read every file under src/ and report how many there are.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat={MASTER_CHAT}===
"""

SUB_DONE_REPLY = f"""Counted them.

===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Surveyed src/.
EOT
result <<EOT
src/ holds exactly one file: src/utils.py (2 lines).
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat={SUB_CHAT}===
"""


class TracingClipboard(FakeClipboard):
    """A FakeClipboard that writes into a shared trace, so a clipboard write and
    a screen click can be ordered against each other."""

    def __init__(self, trace: list[tuple[str, object]]) -> None:
        super().__init__()
        self._trace = trace

    def write_text(self, text: str) -> None:
        self._trace.append(("write", text))
        super().write_text(text)


def _frame(region: ScreenRegion) -> RegionImage:
    fill = bytes([region.left % 251])
    return RegionImage(region.width, region.height, fill * (region.width * region.height * 4))


class _Picker:
    def __init__(self) -> None:
        self.region: ScreenRegion | None = None
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> ScreenRegion | None:
        self.prompts.append(prompt)
        return self.region


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def trace() -> list[tuple[str, object]]:
    return []


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, trace: list[tuple[str, object]]) -> _Picker:
    picker = _Picker()
    monkeypatch.setattr(main_mod, "pick_region", picker)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "_NEW_CHAT_SETTLE_S", 0.01)
    monkeypatch.setattr(main_mod, "_BUSY_POLL_S", 0.05)
    monkeypatch.setattr(main_mod, "probe_element", lambda element: True)
    monkeypatch.setattr(main_mod, "probe_busy", lambda *a: BusyProbe(BusyState.MATCH, 0.0))
    monkeypatch.setattr(main_mod, "send_paste", lambda: True)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)
    monkeypatch.setattr(main_mod, "foreground_window", lambda: None)
    monkeypatch.setattr(
        main_mod,
        "click_region",
        lambda region, **kw: bool(trace.append(("click", region))) or True,
    )
    return picker


def _make_app(tmp_path: Path, provider: FakeClipboard) -> AgentClipApp:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "utils.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def get_config():
        # A 24k preset: the bootstrap grows by the delegate catalog entry, and a
        # 12k service has no room left for it (the composer refuses, and the
        # session simply never arms - which is the honest existing behaviour).
        return load_config(
            project,
            global_config_path=project / "no-such-global.toml",
            service_override="claude",
        )

    base = make_engine_factory(get_config, project)

    def factory(request: EngineRequest | str) -> Engine:
        req = EngineRequest(service=request) if isinstance(request, str) else request
        if req.chat_name is None:  # pinned per role so the canned replies can name a chat
            req = replace(req, chat_name=MASTER_CHAT if req.role == "master" else SUB_CHAT)
        return base(req)

    return AgentClipApp(
        config=get_config(), provider=provider, engine_factory=factory, project_root=project
    )


async def _press(app: AgentClipApp, pilot: Pilot, button_id: str) -> None:
    assert app.main_screen is not None
    button = app.main_screen.query_one(button_id, Button)
    await _wait_for(pilot, lambda: button.region.width > 0, "sidebar button laid out")
    await pilot.click(button_id)


async def _calibrate(
    app: AgentClipApp, pilot: Pilot, picker: _Picker, button_id: str, region: ScreenRegion
) -> None:
    picker.region = region
    before = len(picker.prompts)
    await _press(app, pilot, button_id)
    await _wait_for(pilot, lambda: len(picker.prompts) > before, f"{button_id} picker ran")
    await pilot.pause(0.05)


async def _select_slot(app: AgentClipApp, pilot: Pilot, slot: AgentSlot) -> None:
    main = app.main_screen
    assert main is not None
    main.sidebar.slot_select.value = str(slot)
    await _wait_for(pilot, lambda: main._calibrating is slot, f"{slot} selected")


async def _calibrate_both_slots(app: AgentClipApp, pilot: Pilot, picker: _Picker) -> None:
    await _calibrate(app, pilot, picker, "#set-region-btn", MASTER_BOX)
    await _calibrate(app, pilot, picker, "#set-busy-btn", MASTER_BUSY)
    await _calibrate(app, pilot, picker, "#set-copy-btn", MASTER_COPY)
    await _calibrate(app, pilot, picker, "#set-newchat-btn", MASTER_NEWCHAT)
    await _select_slot(app, pilot, AgentSlot.SUBAGENT)
    await _calibrate(app, pilot, picker, "#set-region-btn", SUB_BOX)
    await _calibrate(app, pilot, picker, "#set-busy-btn", SUB_BUSY)
    await _calibrate(app, pilot, picker, "#set-copy-btn", SUB_COPY)
    await _calibrate(app, pilot, picker, "#set-newchat-btn", SUB_NEWCHAT)
    await _select_slot(app, pilot, AgentSlot.MASTER)


async def _armed(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


async def test_a_delegation_runs_end_to_end(
    tmp_path: Path, patched: _Picker, trace: list[tuple[str, object]]
) -> None:
    provider = TracingClipboard(trace)
    app = _make_app(tmp_path, provider)
    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        await _calibrate_both_slots(app, pilot, patched)
        assert main.delegation_available()

        main.composer.load_text("Survey the project.")
        main.composer.focus()  # the sidebar buttons took focus while calibrating
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "bootstrap copied")
        assert "tool=delegate" in provider.written[0]  # the tool was offered

        # -- the master delegates -------------------------------------------
        trace.clear()
        main.post_message(ClipboardCaptured(DELEGATE_REPLY))
        await _wait_for(pilot, lambda: main.chat_tabs.tab_count == 2, "the sub-agent's tab")
        await _wait_for(
            pilot,
            lambda: main._controller._reply_future is not None,
            "the sub-run waiting for its chat",
        )

        assert main.chat_tabs.get_tab("tab-sub-1").label_text.startswith("▶ ")
        assert main._focused_panel == "sub-1"
        assert main._live is AgentSlot.SUBAGENT  # the automation moved windows

        # The new-chat click came BEFORE the first byte was ever written out.
        clicks = [region for kind, region in trace if kind == "click"]
        writes = [text for kind, text in trace if kind == "write"]
        assert clicks[0] == SUB_NEWCHAT
        assert len(writes) == 1
        assert "You are a sub-agent." in writes[0]
        assert f"chat={SUB_CHAT}" in writes[0]
        assert trace.index(("click", SUB_NEWCHAT)) < trace.index(("write", writes[0]))
        # ...and the paste went into the SUB-AGENT's chat box, not the master's.
        assert SUB_BOX in clicks
        assert MASTER_BOX not in clicks

        # -- the sub-agent delivers ------------------------------------------
        main.post_message(ClipboardCaptured(SUB_DONE_REPLY))
        await _wait_for(pilot, lambda: main._live is AgentSlot.MASTER, "the master chat back")
        await _wait_for(pilot, lambda: not main.busy, "the master turn to finish")

        assert main.chat_tabs.get_tab("tab-sub-1").label_text == "✓ Read every file under src/ and…"
        assert main._focused_panel == "master"
        # The sub-agent's deliverable rode home inside the master's payload.
        assert "src/ holds exactly one file" in provider.written[-1]
        assert "===CLIP:RESULT id=1 status=ok===" in provider.written[-1]
        # Both transcripts survive, each with its own half of the story.
        sub_entries = " ".join(main._panels["sub-1"].entries)
        master_entries = " ".join(main._panels["master"].entries)
        assert "task done" in sub_entries
        assert "delegating to a sub-agent" in master_entries
        assert "sub-agent result" in master_entries
        assert "src/ holds exactly one file" in sub_entries  # the work stayed on its tab


async def test_the_status_bar_and_gate_say_it_is_a_sub_agent(
    tmp_path: Path, patched: _Picker, trace: list[tuple[str, object]]
) -> None:
    """A magenta status segment and a labelled approval box are the only things
    telling the user that the edit they are about to approve is a sub-agent's."""
    provider = TracingClipboard(trace)
    app = _make_app(tmp_path, provider)
    async with app.run_test(size=SIZE) as pilot:
        main = await _armed(app, pilot)
        await _calibrate_both_slots(app, pilot, patched)
        main.composer.load_text("Survey the project.")
        main.composer.focus()  # the sidebar buttons took focus while calibrating
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for(pilot, lambda: main.session_active, "session armed")
        await _wait_for(pilot, lambda: not main.busy, "bootstrap copied")

        main.post_message(ClipboardCaptured(DELEGATE_REPLY))
        await _wait_for(
            pilot,
            lambda: main._controller._reply_future is not None,
            "the sub-run waiting for its chat",
        )

        text, style = main._watch_segment()
        assert style == "st-sub"
        assert text.startswith("◆ SUB-AGENT · ")
        assert main._gate_prefix() == "SUB-AGENT ‹Read every file under src/ and…› · "
        # The composer stays usable so /abort is reachable mid-run.
        assert not main.composer.disabled
        assert "abort" in (main.composer.border_title or "")

        main.post_message(ClipboardCaptured(SUB_DONE_REPLY))
        await _wait_for(pilot, lambda: not main.busy, "the master turn to finish")
        assert main._watch_segment()[1] != "st-sub"
