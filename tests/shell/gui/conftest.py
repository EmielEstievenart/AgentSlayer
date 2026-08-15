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
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from agentclip.cli import make_engine_factory
from agentclip.config import Config, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.shell.gui.bridge import Bridge, payload_of
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

    def __init__(self, view: GuiView, bridge: Bridge, recorder: Recorder) -> None:
        self.view = view
        self.bridge = bridge
        self.recorder = recorder
        # Every coroutine ``spawn``/the watcher handed to the (absent) loop.
        self.scheduled: list[str] = []
        self.exits = 0

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

    view = GuiView(
        bridge,
        config=app_config,
        # ManualOnlyProvider: no OS clipboard is touched by anything below.
        provider=select_provider("manual"),
        engine_factory=make_engine_factory(lambda: app_config, project),
        project_root=project,
        profile_root=tmp_path / "profiles",
        schedule=lambda coro: holder["h"].schedule(coro),
        on_exit=lambda: holder["h"].on_exit(),
    )
    holder["h"] = Harness(view, bridge, recorder)
    return holder["h"]


async def settle(times: int = 3) -> None:
    """Give the loop a few turns so a just-created task actually runs."""
    for _ in range(times):
        await asyncio.sleep(0)
