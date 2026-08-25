"""Split mode: the GUI over a monitor on another machine (ui-monitor.md §6.5).

``--monitor host:port`` is the whole entry (there is deliberately no in-app
connect field in this phase), and everything it changes about this shell is
here: the controller is built over a ``SwitchableMonitor`` that is inert until
the first dial lands after first paint, a lost link parks the loop in
``DISCONNECTED`` and starts a backoff, a redial re-derives from the screen, and
the calibration door is closed because the pixels are somewhere else.

Nothing here opens a socket. The dial is an injected seam exactly as
``open_calibration`` is, so a "monitor" is a :class:`ScriptedLink` - a
``FakeUIMonitor`` with the three members only a LINK has (``peer``,
``server_id``, ``on_disconnect``) - and a "disconnect" is a method call. What
is under test is the SEQUENCE the view runs on each event, which is the half
that a real socket would only make slower to assert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentclip.cli import make_engine_factory
from agentclip.config import Config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.switchable import SwitchableMonitor
from agentclip.shell.chat import view as view_module
from agentclip.shell.chat.view import (
    CALIBRATION_REMOTE,
    MONITOR_RESTARTED,
    MONITOR_UP,
    GuiView,
)
from agentclip.shell.webview.bridge import Bridge

from .conftest import Recorder

TARGET = ("box", 7777)
PEER = "box:7777"


class ScriptedLink(FakeUIMonitor):
    """A dialled monitor: the fake, plus what only a LINK answers for.

    Subclassed rather than written fresh so every verb the view spends on it
    (``configure``, ``watch_clipboard``, ``close``) is the same recorded one the
    rest of the GUI suite asserts against.
    """

    def __init__(self, *, server_id: str = "monitor-1") -> None:
        super().__init__(clipboard=select_provider("manual"))
        self.server_id = server_id
        self.peer = PEER
        self.disconnect_hooks: list[Callable[[], None]] = []

    def on_disconnect(self, hook: Callable[[], None]) -> Callable[[], None]:
        self.disconnect_hooks.append(hook)
        return lambda: None

    def drop(self) -> None:
        """The far side went away for a reason nobody asked for."""
        for hook in list(self.disconnect_hooks):
            hook()


class Dialler:
    """The scripted ``--monitor`` dial: a queue of outcomes, in order.

    An entry is either a link to hand back or an exception to raise, so one
    script writes "refused, refused, then up" - which is what a monitor
    restarting looks like from here.
    """

    def __init__(self, *outcomes: ScriptedLink | Exception) -> None:
        self.outcomes = list(outcomes)
        self.dialled: list[tuple[str, int]] = []
        # Every token the view offered, in order. "" is a real value and the
        # right one for a monitor started with --no-token (ui-monitor.md §9.1).
        self.tokens: list[str] = []

    async def __call__(self, host: str, port: int, token: str = "") -> Any:
        self.dialled.append((host, port))
        self.tokens.append(token)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Split:
    """A split-mode view, its recorder and the dial script behind it."""

    def __init__(self, view: GuiView, recorder: Recorder, bridge: Bridge, dial: Dialler) -> None:
        self.view = view
        self.recorder = recorder
        self.bridge = bridge
        self.dial = dial
        self.calibrations = 0

    def flush(self) -> Recorder:
        self.bridge.drain_pending()
        return self.recorder

    def toasts(self) -> list[str]:
        return [event["message"] for event in self.flush().of_type("toast")]

    def watch_segment(self) -> dict[str, Any]:
        segments = {seg["id"]: seg for seg in self.flush().last("status")["segments"]}
        return segments["watch"]

    @property
    def link(self) -> Any:
        """Whichever monitor the switchable handle is pointed at right now."""
        assert isinstance(self.view.monitor, SwitchableMonitor)
        return self.view.monitor.inner


def build(project: Path, app_config: Config, tmp_path: Path, dial: Dialler) -> Split:
    recorder = Recorder()
    bridge = Bridge(recorder)
    holder: dict[str, Split] = {}
    view = GuiView(
        bridge,
        config=app_config,
        provider=select_provider("manual"),
        engine_factory=make_engine_factory(lambda: app_config, project),
        project_root=project,
        profile_root=tmp_path / "profiles",
        global_config_path=tmp_path / "no-such-global.toml",
        # The loop is real here, unlike the local harness's: the dial, the
        # configure and the backoff are all coroutines the view puts on it, and
        # a schedule that closed them would be a schedule that tested nothing.
        schedule=lambda coro: asyncio.ensure_future(coro),
        monitor_target=TARGET,
        dial=dial,
        open_calibration=lambda *_a: holder["s"].__setattr__(
            "calibrations", holder["s"].calibrations + 1
        ),
    )
    holder["s"] = Split(view, recorder, bridge, dial)
    return holder["s"]


async def settle(times: int = 40) -> None:
    """Give the loop enough turns for a dial, a configure and a backoff."""
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redial's wait, flattened. What the backoff is FOR (not hammering a
    machine that is restarting) is not a claim a suite can make faster, and the
    schedule itself is pinned by reading the constants, not by sleeping."""
    monkeypatch.setattr(view_module, "MONITOR_BACKOFF_START", 0.0)


# == the first dial ============================================================


async def test_the_link_is_dialled_after_first_paint_and_configured(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """gui.md §2's rule, kept for the slowest thing this shell does: the window
    is painted from the resting chrome and the TCP dial happens after it - and
    when it lands the monitor is configured, the far watcher is asked for and
    the user is told which machine they are now driving."""
    link = ScriptedLink()
    split = build(project, app_config, tmp_path, Dialler(link))

    split.view.start()
    painted = split.flush().types.index("rail")
    await settle()

    assert split.dial.dialled == [TARGET]
    assert split.link is link
    # The spec crossed, and it is the LIVE window's (§2.10) - the monitor is
    # told what to watch before anything else is asked of it.
    assert link.specs, "the link was never configured"
    assert link.watching is False  # a manual clipboard honours no watcher
    assert ("watch_clipboard", (True,)) in link.calls
    assert split.view.automation.loop_state is LoopState.IDLE
    assert MONITOR_UP.format(peer=PEER) in split.toasts()
    # ...and every one of those happened after the chrome was on screen.
    assert split.flush().types.index("toast") > painted


async def test_a_first_dial_that_is_refused_parks_and_keeps_trying(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The monitor is a standing process somebody starts (§2.8), so "it is not
    up yet" is a state to sit in rather than a launch error: the window stays,
    the loop says DISCONNECTED, and the backoff keeps dialling until it is."""
    link = ScriptedLink()
    dial = Dialler(ConnectionRefusedError("no listener"), link)
    split = build(project, app_config, tmp_path, dial)

    split.view.start()
    await settle()

    # It parked on the refusal, said which machine refused, and then got it.
    assert any("cannot reach the monitor at box:7777" in text for text in split.toasts())
    assert "DISCONNECTED" in [event["loop"] for event in split.flush().of_type("rail")]
    assert split.link is link
    assert split.view.automation.loop_state is LoopState.IDLE


# == losing the link ===========================================================


async def test_a_dropped_link_parks_the_loop_in_disconnected(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """§2.9: the loop parks, the rail draws it, the status bar's sentence
    changes and - because ``DISCONNECTED`` is an ``ATTENTION_STATES`` member -
    the segment is styled as one of the two "the next move is not this loop's"
    states, which is the same switch that arms the audible alarm."""
    link = ScriptedLink()
    # Never let the redial land, so the parked state is what is observed.
    split = build(project, app_config, tmp_path, Dialler(link, ConnectionResetError("still down")))
    split.view.start()
    await settle()
    split.flush().clear()

    link.drop()

    assert split.view.automation.loop_state is LoopState.DISCONNECTED
    assert split.flush().last("rail")["loop"] == "DISCONNECTED"
    rows = {row["state"]: row["label"] for row in split.flush().last("rail")["rows"]}
    assert rows["DISCONNECTED"] == "disconnected"
    segment = split.watch_segment()
    assert segment["text"] == "■ monitor link lost - reconnecting"
    assert segment["cls"] == "st-attn"
    assert any("monitor link lost" in text for text in split.toasts())


async def test_a_link_that_stays_down_is_announced_once_not_once_per_attempt(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """A monitor that is down for ten minutes must not stack one toast per
    backoff round on top of the one that already says the true thing."""
    link = ScriptedLink()
    split = build(project, app_config, tmp_path, Dialler(link, ConnectionResetError("down")))
    split.view.start()
    await settle()
    split.flush().clear()

    link.drop()
    await settle(60)

    assert len([t for t in split.toasts() if "monitor link lost" in t]) == 1
    assert len(split.dial.dialled) > 2  # ...and it really did keep trying


# == getting it back ===========================================================


async def test_a_redial_reconfigures_and_comes_back_to_idle(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The reconnect sequence, asserted in order: the new link is swapped in,
    the spec is re-sent (the monitor kept polling and counting while nobody was
    attached, so nothing it says is trustworthy until it has been retargeted),
    the far clipboard watcher is re-armed, and the loop lands on IDLE - from
    which the recipe re-runs off the screen rather than off anything replayed.
    """
    first, second = ScriptedLink(server_id="monitor-1"), ScriptedLink(server_id="monitor-1")
    split = build(project, app_config, tmp_path, Dialler(first, second))
    split.view.start()
    await settle()
    split.flush().clear()

    first.drop()
    await settle()

    assert split.link is second
    assert second.specs, "the reconnected monitor was never retargeted"
    assert ("watch_clipboard", (True,)) in second.calls
    assert split.view.automation.loop_state is LoopState.IDLE
    assert MONITOR_UP.format(peer=PEER) in split.toasts()
    # The dead link is closed on the way past: a redial per outage must not
    # leave a socket behind per outage.
    assert first.closed


async def test_a_monitor_that_restarted_is_named_as_a_different_process(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """A redial that comes back with a different ``server_id`` reached a monitor
    that has been restarted, so every generation we remember is meaningless and
    the reconnect is a full retarget. Said out loud, because a monitor that
    restarted is a monitor somebody restarted."""
    first, second = ScriptedLink(server_id="monitor-1"), ScriptedLink(server_id="monitor-2")
    split = build(project, app_config, tmp_path, Dialler(first, second))
    split.view.start()
    await settle()
    split.flush().clear()

    first.drop()
    await settle()

    assert MONITOR_RESTARTED.format(peer=PEER) in split.toasts()


async def test_the_redial_stops_when_the_window_goes_away(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """A backoff that outlived its window would keep dialling a machine nobody
    is watching - and would hand a link to a view that has already been torn
    down."""
    link = ScriptedLink()
    split = build(project, app_config, tmp_path, Dialler(link, ConnectionResetError("down")))
    split.view.start()
    await settle()
    link.drop()
    await settle(6)

    split.view.shutdown()
    dialled = len(split.dial.dialled)
    await settle(60)

    assert len(split.dial.dialled) == dialled


# == what split mode takes away ================================================


async def test_calibration_is_refused_and_points_at_the_other_machine(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The calibration window captures THIS screen (§6.4), and in split mode
    the browser is on another one - so opening it here would save a picture of
    the operator's desktop as the chat service's appearance."""
    split = build(project, app_config, tmp_path, Dialler(ScriptedLink()))
    split.view.start()
    await settle()
    split.flush().clear()

    split.view.open_calibration()

    assert split.calibrations == 0
    assert CALIBRATION_REMOTE in split.toasts()
