"""Pilot tests for the streamed outbound delivery (``ServicePreset.delivery``).

The paste mode is covered next door in test_paste_flash_ui.py; what is at stake
here is the second path ``copy_outbound`` grew: a service set to ``"stream"``
walks the payload into the chat box as a run of clipboard-write + Ctrl+V bursts
instead of one, so a very large message shows progress instead of stalling the
page.

Everything that touches the OS is monkeypatched at the *use site*
(``main_mod.click_region`` / ``main_mod.send_paste``, which main.py from-imports)
and the clipboard is a :class:`FakeClipboard` - a real ``send_paste`` here would
press Ctrl+V into whatever window is running the suite, once per chunk.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.chunking import split_for_stream
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.widgets.sidebar import ENTER_FLASH_TEXT, PASTE_FLASH_TEXT

# Small enough that a readable test payload is several chunks, big enough that a
# short one is still exactly one.
_LIMIT = 40


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, *, delivery: str) -> tuple[AgentClipApp, FakeClipboard]:
    """An app whose default service delivers outbounds the given way."""
    project = tmp_path / "project"
    project.mkdir()
    global_path = tmp_path / "config.toml"
    global_path.write_text(
        f'[services.chatgpt-attach]\ndelivery = "{delivery}"\n', encoding="utf-8"
    )
    config = load_config(project, global_config_path=global_path)
    assert config.preset().delivery == delivery
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
    )
    return app, fake


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    main._chat_region = ScreenRegion(0, 0, 100, 20)  # something to click, so the paste runs
    return main


def _flash_text(app: AgentClipApp) -> str:
    assert app.main_screen is not None
    return str(app.main_screen.query_one("#side-paste-flash", Static).render())


@pytest.fixture(autouse=True)
def _fast_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real click, no real Ctrl+V, and no real waiting between chunks."""
    monkeypatch.setattr(main_mod, "click_region", lambda region: True)
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)
    monkeypatch.setattr(main_mod, "_STREAM_CHUNK_SETTLE_S", 0.0)
    monkeypatch.setattr(main_mod, "STREAM_CHUNK_CHARS", _LIMIT)


def _payload(lines: int = 12) -> str:
    return "".join(f"line {n:02d} of the outbound payload\n" for n in range(lines))


# -- the stream itself ---------------------------------------------------------


async def test_a_long_payload_is_delivered_one_chunk_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One clipboard write and one Ctrl+V per chunk, in order, and the chunks
    rejoin into exactly the payload the controller handed over."""
    pastes: list[str] = []
    app, fake = _make_app(tmp_path, delivery="stream")
    monkeypatch.setattr(
        main_mod, "send_paste", lambda: pastes.append(fake.read_text() or "") or True
    )
    text = _payload()
    expected = split_for_stream(text, _LIMIT)
    assert len(expected) > 1  # otherwise this proves nothing

    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound(text)
        await pilot.pause()

        # The whole payload lands first (it is what every manual recovery
        # pastes), then the chunks in order.
        assert fake.written == [text, *expected]
        assert pastes == expected
        assert "".join(pastes) == text


async def test_every_chunk_is_registered_as_a_self_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher polls the same clipboard we are writing chunks to; a chunk
    that was not registered would come straight back in as a "reply"."""
    monkeypatch.setattr(main_mod, "send_paste", lambda: True)
    app, _fake = _make_app(tmp_path, delivery="stream")
    text = _payload()
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound(text)
        await pilot.pause()

        for chunk in split_for_stream(text, _LIMIT):
            assert main._self_writes.contains_text(chunk), chunk


async def test_a_finished_stream_ends_in_wait_send_and_asks_for_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming is one auto-insert that takes a while, not a state of its own:
    the loop comes out of it exactly where a single paste would."""
    monkeypatch.setattr(main_mod, "send_paste", lambda: True)
    app, _fake = _make_app(tmp_path, delivery="stream")
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound(_payload())
        await pilot.pause()

        assert main._loop_state is LoopState.WAIT_SEND
        assert ENTER_FLASH_TEXT.splitlines()[0] in _flash_text(app)


async def test_the_banner_counts_the_chunks_while_they_go_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user is looking at the browser, so the count is the only thing that
    says a big payload is still going in rather than stuck."""
    seen: list[str] = []
    app, _fake = _make_app(tmp_path, delivery="stream")
    text = _payload()
    total = len(split_for_stream(text, _LIMIT))

    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        # Sampled from inside send_paste: by the time copy_outbound returns the
        # banner has already moved on to ">>> PRESS ENTER <<<".
        monkeypatch.setattr(
            main_mod, "send_paste", lambda: seen.append(_flash_text(app)) or True
        )
        await main.copy_outbound(text)
        await pilot.pause()

    assert "STREAMING" in seen[0]
    assert f"1/{total}" in seen[0]
    assert f"{total}/{total}" in seen[-1]


async def test_a_short_payload_in_stream_mode_is_a_single_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to show progress about, so the stream costs one extra clipboard
    write and nothing else."""
    pastes: list[None] = []
    monkeypatch.setattr(main_mod, "send_paste", lambda: pastes.append(None) or True)
    app, fake = _make_app(tmp_path, delivery="stream")
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound("short")
        await pilot.pause()

        assert pastes == [None]
        assert fake.written == ["short", "short"]


# -- when a chunk does not land ------------------------------------------------


async def test_a_failed_chunk_stops_the_stream_and_restores_the_full_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box now holds a fragment the user has to clear, so the clipboard has
    to hold the WHOLE message for the manual Ctrl+V that replaces it."""
    attempts: list[None] = []

    def flaky() -> bool:
        attempts.append(None)
        return len(attempts) < 3  # the third chunk never lands

    monkeypatch.setattr(main_mod, "send_paste", flaky)
    app, fake = _make_app(tmp_path, delivery="stream")
    text = _payload()
    chunks = split_for_stream(text, _LIMIT)
    assert len(chunks) > 3

    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound(text)
        await pilot.pause()

        assert len(attempts) == 3  # stopped, rather than ploughing on
        assert fake.written == [text, *chunks[:3], text]
        assert fake.read_text() == text
        assert main._self_writes.contains_text(text)
        assert main._loop_state is LoopState.MANUAL_INSERT
        assert PASTE_FLASH_TEXT.splitlines()[0] in _flash_text(app)


async def test_a_failed_chunk_warns_that_the_box_holds_a_partial_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes: list[str] = []
    monkeypatch.setattr(main_mod, "send_paste", lambda: False)
    app, _fake = _make_app(tmp_path, delivery="stream")
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        monkeypatch.setattr(
            type(main), "notify", lambda self, msg, **kw: notes.append(str(msg))
        )
        await main.copy_outbound(_payload())
        await pilot.pause()

    assert any("partial" in note and "Ctrl+V" in note for note in notes)


async def test_a_click_that_never_landed_streams_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged from the paste mode: focus could be on any window, so nothing
    is typed into it - and the full payload is already on the clipboard."""
    pastes: list[None] = []
    monkeypatch.setattr(main_mod, "click_region", lambda region: False)
    monkeypatch.setattr(main_mod, "send_paste", lambda: pastes.append(None) or True)
    app, fake = _make_app(tmp_path, delivery="stream")
    text = _payload()
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound(text)
        await pilot.pause()

        assert pastes == []
        assert fake.written == [text]
        assert main._loop_state is LoopState.MANUAL_INSERT


# -- the default mode is untouched ---------------------------------------------


async def test_paste_mode_is_still_one_write_and_one_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload far past the chunk size delivered by a service that did not ask
    for streaming must behave exactly as it did before this existed."""
    pastes: list[None] = []
    monkeypatch.setattr(main_mod, "send_paste", lambda: pastes.append(None) or True)
    app, fake = _make_app(tmp_path, delivery="paste")
    text = _payload()
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)
        await main.copy_outbound(text)
        await pilot.pause()

        assert fake.written == [text]
        assert pastes == [None]
        assert main._loop_state is LoopState.WAIT_SEND
