"""A GUI shell with no window behind it, so the whole event vocabulary is testable.

The same bargain the two suites next door make (``tests/shell/app`` fakes the
``ChatView``, ``tests/driver/automation`` fakes the ``AutomationView``): here the fake
is the *sink*. :class:`agentclip.shell.webview.bridge.Bridge` takes its ``evaluate_js`` as
a plain ``Callable[[str], None]``, so a list is a perfectly good window - and
what lands in it is the exact JavaScript a real WebView2 would have run, which
:func:`agentclip.shell.webview.bridge.payload_of` decodes back into the event.

Nothing here opens a window, starts a loop or touches the clipboard: the
provider is the manual one (no OS access at all) and the bridge is drained by
the calling thread on demand, so every assertion is about what the page WOULD
have been told and in what order.

The MACHINE is faked twice over, because since docs/design/ui-monitor.md §10.2
there are two seams between this window and a screen and neither of them is an
object the view builds. A monitor is a PROCESS the Chat UI dials, so the harness
injects:

* a :class:`FakeLauncher` in place of ``SubprocessLauncher`` - it spawns nothing
  and hands back a :class:`~agentclip.config.MonitorTarget` on 127.0.0.1, which
  is exactly what a real launch produces and all the view ever reads off one;
* a fake ``dial`` in place of a socket, answering with a
  :class:`~agentclip.driver.monitor.fake.FakeUIMonitor`.

So the default harness is **a local child, attached**: the state a plain
``agentclip`` launch reaches, driving the service the MONITOR answers ``watch``
with (``claude``, out of the real config) rather than one this window picked.
That inversion is §10.5, and a harness that let the Chat UI choose would be
pinning a shell that no longer exists.

The attach is done by hand at construction rather than by running ``start()``,
because this harness's ``schedule`` deliberately CLOSES the coroutines it is
handed (:meth:`Harness.schedule`): what most of these suites are about is what
the page is told, not what the loop does. The two suites that ARE about the loop
- ``test_monitor_connect.py`` and ``test_monitor_link.py`` - build their own view
over a real one.

A test that wants the automation to see something pushes a tick
(``harness.monitor.feed(harness.monitor.make_tick(...))``) rather than waiting
for a poller that would be capturing the developer's actual screen. The
monitor's clipboard is the manual provider, which is what makes the watcher
refuse to start unless a test asks for a backend that can be polled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from agentclip.cli import make_engine_factory
from agentclip.config import MONITOR_LOOPBACK, Config, MonitorTarget, load_config
from agentclip.driver.clip.base import ClipboardProvider, select_provider
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.protocol import spec_from_preset
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.app.monitor_launch import LaunchedMonitor, LaunchLocal
from agentclip.shell.chat.view import GuiView
from agentclip.shell.webview.bridge import Bridge, payload_of

#: What the fake launcher's child would be listening on. A real port number and
#: the real loopback address, because the view builds a ``MonitorTarget`` out of
#: them and the badge, the dialog and the toasts all render it.
FAKE_MONITOR_PORT = 45_678

#: The service the fake monitor answers ``watch`` with. A REAL preset key, so
#: the budgets that reach the session builder are a real service's rather than
#: the double's default zeros - a paste budget of 0 is not a state any monitor
#: can be in, and a bootstrap sized against one refuses every task.
HARNESS_SERVICE = "claude"


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


class FakeLauncher:
    """A ``LocalMonitorLauncher`` that starts nothing and says so.

    Every field the view reads off a launch is real - a target with a host, a
    port and a token, and an ``alive()`` a test can turn off to play "the child
    died". Nothing is spawned, which is the point: ``tests/shell/app`` pins the
    real launcher's command line, and no suite may put an ``agentclip-monitor``
    on the developer's desktop.
    """

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.running = False
        self.code: int | None = None
        #: Raised by :meth:`start` when a test wants the spawn itself to fail.
        self.refuse: Exception | None = None

    def start(
        self, project_root: Path, *, global_config_path: Path | None = None
    ) -> LaunchedMonitor:
        if self.refuse is not None:
            raise self.refuse
        self.starts += 1
        self.running = True
        self.code = None
        return LaunchedMonitor(
            target=MonitorTarget(
                name="local", host=MONITOR_LOOPBACK, port=FAKE_MONITOR_PORT, token=""
            ),
            process_id=4242,
        )

    def stop(self) -> None:
        self.stops += 1
        self.running = False

    def alive(self) -> bool:
        return self.running

    def exit_code(self) -> int | None:
        return self.code

    def died(self, code: int = 1) -> None:
        """The child exited on its own - what a crashed monitor looks like."""
        self.running = False
        self.code = code


class Harness:
    """A ``GuiView`` wired to a recorder, plus the flushing every read needs."""

    def __init__(
        self,
        view: GuiView,
        bridge: Bridge,
        recorder: Recorder,
        monitor: FakeUIMonitor,
        launcher: FakeLauncher,
    ) -> None:
        self.view = view
        self.bridge = bridge
        self.recorder = recorder
        # The machine, so a test can push a tick, script an action's answer or
        # swap the clipboard backend under the watcher.
        self.monitor = monitor
        # ...and the process it would have been, so a test can count launches,
        # count stops, or kill the child mid-run.
        self.launcher = launcher
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


def fake_monitor(config: Config, provider: ClipboardProvider) -> FakeUIMonitor:
    """The double a dial answers with, running a REAL service for both windows.

    ``specs_for`` is what ``watch(slot)`` resolves against, so this is the whole
    of "which service is the monitor driving" - the fact the Chat UI reads back
    instead of choosing (§10.5).
    """
    monitor = FakeUIMonitor(clipboard=provider)
    preset = config.services[HARNESS_SERVICE]
    monitor.specs_for = {slot: spec_from_preset(preset) for slot in AgentSlot}
    return monitor


def attach(view: GuiView, monitor: FakeUIMonitor, launcher: FakeLauncher) -> None:
    """Put a view into the state a launched-and-dialled child leaves it in.

    By hand because this harness's ``schedule`` closes coroutines, so the async
    attach would never run. What it reproduces is exactly what
    ``_launch_local_monitor`` + ``_attach_monitor`` leave behind: the launcher
    holds a child, the handle points at the link, and the view has adopted the
    monitor's first ``Watched``.
    """
    launched = launcher.start(Path("."))
    view._local_launched = True
    view._monitor_target = launched.target
    view._switch.swap(monitor)
    asyncio.run(view._retarget_monitor())


async def _never_dialled(host: str, port: int, token: str) -> Any:
    """The dial this harness never reaches: it attaches by hand (see :func:`attach`).

    A raise rather than a stub, so a test that started expecting the real
    sequence finds out here rather than against a monitor nobody configured.
    """
    raise AssertionError("the default harness attaches by hand, not by dialling")


@pytest.fixture
def harness(project: Path, app_config: Config, tmp_path: Path) -> Harness:
    recorder = Recorder()
    bridge = Bridge(recorder)
    holder: dict[str, Harness] = {}

    # ManualOnlyProvider: no OS clipboard is touched by anything below, and the
    # monitor holds the same one - "manual" is what the watcher refuses.
    provider = select_provider("manual")
    monitor = fake_monitor(app_config, provider)
    launcher = FakeLauncher()
    view = GuiView(
        bridge,
        config=app_config,
        provider=provider,
        engine_factory=make_engine_factory(lambda: app_config, project),
        project_root=project,
        monitor_target=LaunchLocal(),
        launcher=launcher,
        dial=_never_dialled,
        schedule=lambda coro: holder["h"].schedule(coro),
        on_exit=lambda: holder["h"].on_exit(),
    )
    attach(view, monitor, launcher)
    # The attach paints (a sidebar, five detection lines); a suite reading "what
    # did start() say" must not find them in front of its own events.
    bridge.drain_pending()
    recorder.clear()
    holder["h"] = Harness(view, bridge, recorder, monitor, launcher)
    return holder["h"]


async def settle(times: int = 3) -> None:
    """Give the loop a few turns so a just-created task actually runs."""
    for _ in range(times):
        await asyncio.sleep(0)
