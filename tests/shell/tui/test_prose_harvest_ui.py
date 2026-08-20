"""Pilot tests for the no-CLIP harvest (``_ingest_prose_harvest``).

A model reply that carries no CLIP blocks at all used to vanish: the watcher's
protocol pre-filter dropped it (protocol.md 1.4 tolerance #11) and the only way
to see it was the manual `i` ingest. Sometimes the model does everything right
and simply answers in prose, so a copy click AgentClip made ITSELF now always
ingests what it harvested - the flow just watched the copy button write THIS
text, so unlike the watcher it knows the text is the reply - and shows it in the
transcript as prose, never executed.

What is at stake: the flow's own verified click shows prose with no
configuration at all, the loosening lasts exactly that one act
(``AutomationController.prose_window``) and nothing outside it, and a
protocol-shaped harvest is left strictly to the watcher (the harvest path
ingesting it too would be a double ingest of every normal reply).

OS touches are monkeypatched at the use site, as everywhere in this suite; the
"copy click" writes straight into the FakeClipboard, which is exactly what the
real click does to the real one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import RegionMatch
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.main import MainScreen

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
MATCH = RegionMatch(x=120, y=300, diff=0.03)

PROSE_REPLY = "Here is my plain answer - no tool calls, just words."

PROTOCOL_REPLY = """~~~~
===CLIP:CALL id=1 tool=task_done===
summary: all done
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""


def _frame(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, profile_root: Path) -> tuple[AgentClipApp, FakeClipboard]:
    """A stock app: the harvest takes no configuration any more, so the config
    file this builds on is the empty one every user starts with."""
    project = tmp_path / "project"
    project.mkdir()
    global_path = tmp_path / "config.toml"
    global_path.write_text("", encoding="utf-8")
    config = load_config(project, global_config_path=global_path)
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
        profile_root=profile_root,
    )
    return app, fake


async def _session_with_region(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    """A running session (so the controller has an engine to hand prose to)
    with the chat region drawn - the state a real harvest fires from."""
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main._chat_region = CHAT_REGION
    main.composer.load_text("Say hello.")
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


def _prose_entries(main: MainScreen) -> list[str]:
    return [e for e in main.transcript.entries if e.startswith("llm:")]


def _patch_flow(monkeypatch: pytest.MonkeyPatch, fake: FakeClipboard) -> None:
    """The whole OS side of a successful harvest: every click 'copies' a fresh
    variant of the prose reply, so the verification read sees a change."""
    clicks = [0]

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        clicks[0] += 1
        fake.set_text(f"{PROSE_REPLY} ({clicks[0]})")
        return True

    monkeypatch.setattr(main_mod, "capture_region", _frame)
    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda handle: True)
    monkeypatch.setattr(
        main_mod, "find_lowest_with_best_miss", lambda template, scene, **kw: (MATCH, None)
    )
    monkeypatch.setattr(main_mod, "send_paste", lambda: True)


async def test_our_own_copy_click_shows_a_no_clip_reply_as_prose(
    tmp_path: Path,
    profile_root: Path,
    seed_templates: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default, with nothing configured: the flow found the copy button,
    clicked it, watched the clipboard change - so whatever came back is the
    reply, prose or not, and the user gets to read it."""
    app, fake = _make_app(tmp_path, profile_root)
    seed_templates("chatgpt-attach", TemplateKind.COPY, size=(24, 24))
    _patch_flow(monkeypatch, fake)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _session_with_region(app, pilot)

        await main._auto_copy_flow()
        await _wait_for(
            pilot,
            lambda: any(PROSE_REPLY in e for e in _prose_entries(main)),
            "the no-CLIP reply shown in the transcript",
        )
        # ...and the loosening did not outlive the act that earned it.
        assert main._automation.prose_window is False


async def test_a_harvest_outside_the_window_ingests_nothing(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window is the whole permission. Reached any other way - a stale call,
    a click that never verified - the harvest is not entitled to decide that the
    clipboard holds the model's reply, and does nothing."""
    app, fake = _make_app(tmp_path, profile_root)
    submissions: list[tuple[str, bool]] = []
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        monkeypatch.setattr(
            main._controller,
            "submit_clipboard",
            lambda text, accept_prose=False: submissions.append((text, accept_prose)),
        )
        fake.set_text(PROSE_REPLY)
        assert main._automation.prose_window is False

        await main._ingest_prose_harvest()
        await pilot.pause()

    assert submissions == []


async def test_a_protocol_shaped_harvest_is_left_to_the_watcher(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvest hook must not race the watcher over normal replies: a text
    with CLIP blocks is ingested once, by the watcher, exactly as before - even
    with the window wide open."""
    app, fake = _make_app(tmp_path, profile_root)
    submissions: list[tuple[str, bool]] = []
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        monkeypatch.setattr(
            main._controller,
            "submit_clipboard",
            lambda text, accept_prose=False: submissions.append((text, accept_prose)),
        )
        main._automation._prose_window = True
        fake.set_text(PROTOCOL_REPLY)

        await main._ingest_prose_harvest()
        await pilot.pause()

    assert submissions == []


async def test_an_empty_clipboard_harvests_nothing(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, fake = _make_app(tmp_path, profile_root)
    submissions: list[tuple[str, bool]] = []
    async with app.run_test(size=(110, 55)) as pilot:
        main = app.main_screen
        assert main is not None
        await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
        monkeypatch.setattr(
            main._controller,
            "submit_clipboard",
            lambda text, accept_prose=False: submissions.append((text, accept_prose)),
        )
        main._automation._prose_window = True
        fake.set_text("")

        await main._ingest_prose_harvest()
        await pilot.pause()

    assert submissions == []
