"""A GUI shell with no window behind it, so the whole event vocabulary is testable.

The same bargain the two suites next door make (``tests/shell/app`` fakes the
``ChatView``, ``tests/driver/automation`` fakes the ``AutomationView``): here the fake
is the *sink*. :class:`agentclip.shell.gui.bridge.Bridge` takes its ``evaluate_js`` as
a plain ``Callable[[str], None]``, so a list is a perfectly good window - and
what lands in it is the exact JavaScript a real WebView2 would have run, which
:func:`agentclip.shell.gui.bridge.payload_of` decodes back into the event.

Nothing here opens a window, starts a loop or touches the clipboard: the
provider is the manual one (no OS access at all) and the bridge is drained by
the calling thread on demand, so every assertion is about what the page WOULD
have been told and in what order.

The second WINDOW is faked too, and by the same trick: ``GuiView`` reaches
pywebview through one injected callable (``open_calibration``), so a test can
press F2, read back the ``CalibrationRunner`` the view built and fire the close
hook, with no toolkit imported and nothing on screen. The window's own surface
is pinned one directory down, in ``tests/shell/gui/calibration/``.

The machine is faked the same way. Since docs/design/ui-monitor.md phase 6.1
everything on the far side of the screen - the poll thread, the detector, the
mouse and the clipboard watcher - is one object the view is handed
(:class:`~agentclip.driver.monitor.fake.FakeUIMonitor` here), so a test that
wants the automation to see something pushes a tick
(``harness.monitor.feed(harness.monitor.make_tick(...))``) rather than waiting
for a poller that would be capturing the developer's actual screen. Its
clipboard is the manual provider, which is what makes the watcher refuse to
start unless a test asks for a backend that can be polled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from agentclip.cli import make_engine_factory
from agentclip.config import Config, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.shell.gui.bridge import Bridge, payload_of
from agentclip.shell.gui.calibration import CalibrationRunner
from agentclip.shell.gui.view import GuiView


class Recorder:
    """The bridge's sink: every script, and every event inside them."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def __call__(self, script: str) -> None:
        self.scripts.append(script)

    @property
    def events(self) -> list[dict[str, Any]]:
        return [payload_of(script) for script in self.scripts]

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == event_type]

    def last(self, event_type: str) -> dict[str, Any]:
        matches = self.of_type(event_type)
        assert matches, f"no {event_type!r} event was sent (saw {self.types})"
        return matches[-1]

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def clear(self) -> None:
        self.scripts.clear()


class Harness:
    """A ``GuiView`` wired to a recorder, plus the flushing every read needs."""

    def __init__(
        self, view: GuiView, bridge: Bridge, recorder: Recorder, monitor: FakeUIMonitor
    ) -> None:
        self.view = view
        self.bridge = bridge
        self.recorder = recorder
        # The machine, so a test can push a tick, script an action's answer or
        # swap the clipboard backend under the watcher.
        self.monitor = monitor
        # Every coroutine ``spawn``/the watcher handed to the (absent) loop.
        self.scheduled: list[str] = []
        self.exits = 0
        # Every calibration window the view asked for, and the close hook it
        # handed over with each. No toolkit is imported and no second window is
        # created: what is under test up here is the DOOR (one at a time, the
        # suspend bracket, the answers coming back), and the window itself has
        # a suite of its own in tests/shell/gui/calibration/.
        self.calibrations: list[CalibrationRunner] = []
        self.closers: list[Callable[[], None]] = []

    def flush(self) -> Recorder:
        """Drain the queue on this thread and hand the recorder back.

        The app has a drainer thread; a test does not need one, and reading
        through ``drain_pending`` keeps every assertion deterministic instead of
        racing a background flush.
        """
        self.bridge.drain_pending()
        return self.recorder

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.scheduled.append(getattr(coro, "__qualname__", repr(coro)))
        coro.close()

    def on_exit(self) -> None:
        self.exits += 1

    def open_calibration(
        self, runner: CalibrationRunner, on_closed: Callable[[], None]
    ) -> None:
        self.calibrations.append(runner)
        self.closers.append(on_closed)

    def close_calibration(self) -> None:
        """What pywebview's ``closed`` event does when the user shuts it."""
        self.closers[-1]()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return root


@pytest.fixture
def app_config(project: Path) -> Config:
    return load_config(project, global_config_path=project / "no-such-global.toml")


@pytest.fixture
def harness(project: Path, app_config: Config, tmp_path: Path) -> Harness:
    recorder = Recorder()
    bridge = Bridge(recorder)
    holder: dict[str, Harness] = {}

    # ManualOnlyProvider: no OS clipboard is touched by anything below, and the
    # monitor holds the same one - "manual" is what the watcher refuses.
    provider = select_provider("manual")
    monitor = FakeUIMonitor(clipboard=provider)
    view = GuiView(
        bridge,
        config=app_config,
        provider=provider,
        engine_factory=make_engine_factory(lambda: app_config, project),
        project_root=project,
        profile_root=tmp_path / "profiles",
        monitor=monitor,
        schedule=lambda coro: holder["h"].schedule(coro),
        on_exit=lambda: holder["h"].on_exit(),
        open_calibration=lambda runner, on_closed: holder["h"].open_calibration(
            runner, on_closed
        ),
    )
    holder["h"] = Harness(view, bridge, recorder, monitor)
    return holder["h"]


async def settle(times: int = 3) -> None:
    """Give the loop a few turns so a just-created task actually runs."""
    for _ in range(times):
        await asyncio.sleep(0)
