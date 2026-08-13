"""Pilot tests for the MCP TUI surface (docs/design/mcp.md section 6).

The manager itself is covered in tests/mcp; what is pinned here is the screen's
half of the contract, against a stub that is nothing but the ``McpStatusSource``
shape (``statuses()`` + ``set_status_hook`` - no SDK, no loop thread):

* an app built WITHOUT a manager grows no MCP chrome at all - no sidebar block,
  a hidden statusbar segment;
* an app built WITH one paints the configured servers at mount, so the
  pending/disabled states show before any transition fires (connects are lazy),
  which is also the ONLY surface a stdio server refused in a remote session
  gets - it is decided before any hook exists;
* a status-hook transition fired from a FOREIGN thread - the manager's loop
  thread in production - repaints both surfaces, which proves the post_message
  marshal rather than the painting;
* failed/needs_auth transitions land in the transcript ONCE per server per
  state (reconnect churn spams nothing), connected transitions never note;
* `/mcp` lists every server into the transcript.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.mcp.types import McpServerStatus
from agentclip.tui.app import AgentClipApp
from agentclip.tui.screens.main import MainScreen
from agentclip.tui.widgets.sidebar import mcp_row_id

from .conftest import send_composer


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class FakeMcpManager:
    """Exactly the ``McpStatusSource`` surface MainScreen consumes.

    ``transition`` is the test's stand-in for a connect settling on the
    manager's loop thread: replace the named server's row, then fire the hook
    with the transition - which is the real manager's order too (`_set_state`
    updates the record under its lock before `_fire`).
    """

    def __init__(self, *statuses: McpServerStatus) -> None:
        self._statuses = list(statuses)
        self.hook: Callable[[McpServerStatus], None] | None = None

    def statuses(self) -> tuple[McpServerStatus, ...]:
        return tuple(self._statuses)

    def set_status_hook(self, cb: Callable[[McpServerStatus], None] | None) -> None:
        self.hook = cb

    def transition(self, status: McpServerStatus) -> None:
        self._statuses = [status if s.name == status.name else s for s in self._statuses]
        assert self.hook is not None, "the app never wired the status hook"
        self.hook(status)


def _make_app(tmp_path: Path, manager: FakeMcpManager | None = None) -> AgentClipApp:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    app = AgentClipApp(
        config=config,
        provider=FakeClipboard(),
        engine_factory=make_engine_factory(lambda: app.app_config, project),
        project_root=project,
        mcp_manager=manager,  # type: ignore[arg-type]  # the McpStatusSource shape is all the TUI reads
    )
    return app


async def _ready(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


def _seg(main: MainScreen) -> Static:
    return main.status_bar.query_one("#seg-mcp", Static)


def _row_text(main: MainScreen, index: int) -> str:
    return str(main.sidebar.query_one(f"#{mcp_row_id(index)}", Static).render())


async def test_no_manager_means_no_mcp_chrome_at_all(tmp_path: Path) -> None:
    """The default install has no opencode.json mcp block, so it must get
    exactly the screen it always had: no MCP heading in the sidebar, no rows,
    and the statusbar segment hidden rather than reading 'mcp 0/0'."""
    app = _make_app(tmp_path)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        assert not _seg(main).display
        assert not main.sidebar.query("#side-mcp-title")
        assert not main.sidebar.query(f"#{mcp_row_id(0)}")


async def test_mount_paints_the_configured_servers_before_any_transition(
    tmp_path: Path,
) -> None:
    """Connects are lazy (nothing dials until the first session build), so the
    states a freshly launched app shows are pending/disabled - and they must
    show from the first frame, not after some transition fires."""
    manager = FakeMcpManager(
        McpServerStatus("alpha", "pending"),
        McpServerStatus("beta", "disabled"),
    )
    app = _make_app(tmp_path, manager)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        assert manager.hook is not None  # wired at mount
        seg = _seg(main)
        assert seg.display
        # 0 connected; the disabled entry is a config statement, not a runtime
        # hope, so it is out of the denominator too.
        assert str(seg.render()) == "mcp 0/1"
        assert _row_text(main, 0) == "alpha · pending"
        assert _row_text(main, 1) == "beta · disabled"


async def test_a_refused_stdio_server_is_visible_without_any_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stdio server refused in a remote session (mcp/client.py) reaches the
    screen through the mount paint, exactly as `disabled` does: it is decided
    in the manager's __init__, so no hook exists yet to announce it.

    That is the whole of its reporting, and this test is where that is written
    down: the sidebar row and the statusbar denominator carry it (and `/mcp`
    reads the same statuses()), while the transcript note and the warning toast
    - which only `_on_mcp_status_changed` writes - never fire for it.
    """
    toasts: list[str] = []
    monkeypatch.setattr(
        MainScreen,
        "notify",
        lambda self, message, *args, **kwargs: toasts.append(message),
    )
    detail = (
        "stdio servers are not supported in a remote session: this entry's command "
        "and cwd describe ssh:box, and AgentClip spawns processes on this PC only"
    )
    manager = FakeMcpManager(McpServerStatus("fs", "failed", detail=detail))
    app = _make_app(tmp_path, manager)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        assert _row_text(main, 0).startswith("fs · failed · stdio servers are not supported")
        assert str(_seg(main).render()) == "mcp 0/1"  # counted, not hidden
        await pilot.pause(0.2)
        assert not any("fs" in entry for entry in main.transcript.entries)
        assert toasts == []


async def test_a_transition_fired_from_a_foreign_thread_repaints_both_surfaces(
    tmp_path: Path,
) -> None:
    """The hook fires on the manager's loop thread and must only hand off; this
    drives it from a real non-UI thread (asyncio.to_thread), so a marshal that
    touched widgets directly - or used call_from_thread's same-thread refusal -
    would fail here, not in production."""
    manager = FakeMcpManager(
        McpServerStatus("alpha", "pending"),
        McpServerStatus("beta", "pending"),
    )
    app = _make_app(tmp_path, manager)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        await asyncio.to_thread(
            manager.transition, McpServerStatus("alpha", "connected", tool_count=3)
        )
        await _wait_for(
            pilot,
            lambda: str(_seg(main).render()) == "mcp 1/2",
            "the statusbar segment to follow the connect",
        )
        assert _row_text(main, 0) == "alpha · connected · 3 tools"
        assert _row_text(main, 1) == "beta · pending"


async def test_failed_lands_in_the_transcript_once_per_server_per_state(
    tmp_path: Path,
) -> None:
    """Reconnect churn must not spam: the same server failing the same way
    again notes nothing, a DIFFERENT terminal state (needs_auth) is new
    information and notes once more."""
    manager = FakeMcpManager(McpServerStatus("alpha", "connecting"))
    app = _make_app(tmp_path, manager)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        failed = McpServerStatus("alpha", "failed", detail="spawn failed: no such file")
        await asyncio.to_thread(manager.transition, failed)
        await _wait_for(
            pilot,
            lambda: any("failed" in entry for entry in main.transcript.entries),
            "the failure note",
        )
        note = next(entry for entry in main.transcript.entries if "failed" in entry)
        assert "alpha" in note
        assert "spawn failed: no such file" in note  # the detail rides the note

        # Churn: connecting again, failing again - no second note.
        await asyncio.to_thread(manager.transition, McpServerStatus("alpha", "connecting"))
        await asyncio.to_thread(manager.transition, failed)
        await _wait_for(
            pilot,
            lambda: _row_text(main, 0).startswith("alpha · failed"),
            "the sidebar to show the re-failure",
        )
        assert sum("failed" in entry for entry in main.transcript.entries) == 1

        # A different state for the same server IS news, and notes once.
        await asyncio.to_thread(
            manager.transition,
            McpServerStatus("alpha", "needs_auth", detail="server rejected the request"),
        )
        await _wait_for(
            pilot,
            lambda: any("needs auth" in entry for entry in main.transcript.entries),
            "the needs-auth note",
        )
        assert sum("needs auth" in entry for entry in main.transcript.entries) == 1


async def test_connected_toasts_quietly_and_never_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working server is not worth a permanent transcript line - the sidebar
    block already says it - but the toast confirms the arrival, once, as
    information rather than warning."""
    toasts: list[tuple[str, str]] = []

    def fake_notify(self: MainScreen, message: str, *args: Any, **kwargs: Any) -> None:
        toasts.append((message, kwargs.get("severity", "information")))

    monkeypatch.setattr(MainScreen, "notify", fake_notify)
    manager = FakeMcpManager(McpServerStatus("alpha", "connecting"))
    app = _make_app(tmp_path, manager)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        connected = McpServerStatus("alpha", "connected", tool_count=2)
        await asyncio.to_thread(manager.transition, connected)
        await _wait_for(
            pilot,
            lambda: any("connected" in message for message, _ in toasts),
            "the connected toast",
        )
        message, severity = next(item for item in toasts if "connected" in item[0])
        assert "alpha" in message and "2 tools" in message
        assert severity == "information"
        assert not any("connected" in entry for entry in main.transcript.entries)

        # ...and only once, however often the hook re-reports the same state
        # (the real manager re-fires a connected server's status whenever a
        # neighbour's arrival recomputes the shadow warnings).
        await asyncio.to_thread(manager.transition, connected)
        await pilot.pause(0.2)
        assert sum("connected" in message for message, _ in toasts) == 1


async def test_the_mcp_command_lists_every_server_in_the_transcript(
    tmp_path: Path,
) -> None:
    """`/mcp` works at the start prompt (no session gate) and prints the whole
    listing - state, tool count, detail - where the sidebar's 30-cell lines
    cannot."""
    manager = FakeMcpManager(
        McpServerStatus("alpha", "connected", tool_count=12),
        McpServerStatus("beta", "failed", detail="connect timed out after 30000 ms"),
    )
    app = _make_app(tmp_path, manager)
    async with app.run_test(size=(110, 55)) as pilot:
        main = await _ready(app, pilot)

        await send_composer(app, pilot, "/mcp")
        await _wait_for(
            pilot,
            lambda: any("MCP servers:" in entry for entry in main.transcript.entries),
            "the /mcp listing",
        )
        note = next(entry for entry in main.transcript.entries if "MCP servers:" in entry)
        assert "alpha · connected · 12 tools" in note
        assert "beta · failed · connect timed out after 30000 ms" in note
