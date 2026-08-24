"""A calibration window with no window behind it, so the whole surface is testable.

The same bargain ``tests/shell/gui/conftest.py`` makes one directory up, and
deliberately the same shapes: the bridge's sink is a list (a
:class:`~agentclip.shell.gui.bridge.Bridge` takes its ``evaluate_js`` as a plain
``Callable[[str], None]``, so a recorder is a perfectly good window), and the
machine is a :class:`~agentclip.driver.monitor.fake.FakeUIMonitor` - which is
what makes "push a frame" a one-liner instead of a poller capturing the
developer's actual screen.

Nothing here opens a window, starts a loop, spawns a child process or writes
outside ``tmp_path``: the profile store and the global config path are both
pointed at the temporary tree, so no run touches the user's captures or their
config.toml, and ``pick_region`` / ``capture_region`` / ``draw_identify_overlay``
are monkeypatched at their own modules' scope by the tests that reach them
(project rule: no real OS side effects while the user is present).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import Config, load_config
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.gui.bridge import Bridge, payload_of
from agentclip.shell.gui.calibration.view import CalibrationView


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


class CalibHarness:
    """A ``CalibrationView`` wired to a recorder, plus the flushing every read needs."""

    def __init__(
        self,
        view: CalibrationView,
        bridge: Bridge,
        recorder: Recorder,
        monitor: FakeUIMonitor,
        profile_root: Path,
        global_config_path: Path,
    ) -> None:
        self.view = view
        self.bridge = bridge
        self.recorder = recorder
        # The machine, so a test can push a frame or count suspends.
        self.monitor = monitor
        self.profile_root = profile_root
        self.global_config_path = global_config_path
        # Every coroutine the view handed to the (absent) loop, and every config
        # or region it handed back out.
        self.scheduled: list[str] = []
        self.exits = 0
        self.configs: list[Config] = []
        self.calibrations: list[tuple[AgentSlot, ScreenRegion | None]] = []

    def flush(self) -> Recorder:
        """Drain the queue on this thread and hand the recorder back."""
        self.bridge.drain_pending()
        return self.recorder

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.scheduled.append(getattr(coro, "__qualname__", repr(coro)))
        coro.close()

    def on_exit(self) -> None:
        self.exits += 1

    def on_config_change(self, config: Config) -> None:
        self.configs.append(config)

    def on_calibration(self, slot: AgentSlot, region: ScreenRegion | None) -> None:
        self.calibrations.append((slot, region))


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
def calib(project: Path, app_config: Config, tmp_path: Path) -> CalibHarness:
    recorder = Recorder()
    bridge = Bridge(recorder)
    holder: dict[str, CalibHarness] = {}
    monitor = FakeUIMonitor()
    profile_root = tmp_path / "profiles"
    global_config_path = tmp_path / "global.toml"
    view = CalibrationView(
        bridge,
        config=app_config,
        monitor=monitor,
        profile_root=profile_root,
        global_config_path=global_config_path,
        schedule=lambda coro: holder["h"].schedule(coro),
        on_exit=lambda: holder["h"].on_exit(),
        on_config_change=lambda config: holder["h"].on_config_change(config),
        on_calibration=lambda slot, region: holder["h"].on_calibration(slot, region),
    )
    holder["h"] = CalibHarness(
        view, bridge, recorder, monitor, profile_root, global_config_path
    )
    return holder["h"]


async def settle(times: int = 3) -> None:
    """Give the loop a few turns so a just-created task actually runs."""
    for _ in range(times):
        await asyncio.sleep(0)
