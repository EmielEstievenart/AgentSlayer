"""The Serve panel: a real listener, a real dial, and the sentence in between.

``docs/design/ui-monitor.md`` §9.1. Nothing below is mocked under the panel:
:class:`~agentclip.shell.monitor_ui.serve.ServePanel` starts the same
:class:`~agentclip.driver.monitor.server.MonitorServer` the headless door does,
on an ephemeral loopback port, in front of a
:class:`~agentclip.driver.monitor.fake.FakeUIMonitor`, and a real
:class:`~agentclip.driver.monitor.remote.RemoteUIMonitor` dials it with a token
that is right, wrong, missing or stale. What is under test is the *panel*, so
every assertion is either "the sentence the page was pushed" or "what the far
side got when it dialled".

The window is absent on purpose, exactly as in ``test_view.py``: the panel is
bound to a real :class:`~agentclip.shell.monitor_ui.view.CalibrationView` over a
real :class:`~agentclip.shell.webview.bridge.Bridge` whose sink is a list, so the
``serve`` event is read the way the page would read it rather than off the
panel's own ``state()``.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import Config, load_config
from agentclip.driver.monitor.auth import TOKEN_CHARS, load_or_create_token
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.interfaces import Interface
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.regions import load_region
from agentclip.driver.monitor.remote import MonitorRefused, RemoteUIMonitor
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.monitor_ui.serve import (
    DEFAULT_PORT,
    FALLBACK_INTERFACES,
    NOT_SERVING,
    REMOTE_WARNING,
    ServePanel,
)
from agentclip.shell.monitor_ui.view import CalibrationView
from agentclip.shell.webview.bridge import Bridge

from .conftest import Recorder, settle

TIMEOUT_S = 5.0

# Two rows that are not this machine's, so the dropdown's shape is asserted
# against something a test controls rather than against whatever NICs the
# developer's laptop has up.
INTERFACES = [
    Interface(name="lo", address="127.0.0.1", family="ipv4", loopback=True),
    Interface(name="eth0", address="192.168.1.40", family="ipv4", loopback=False),
]


class ServeHarness:
    """A panel, the view it pushes through, and the events that came out."""

    def __init__(self, panel: ServePanel, view: CalibrationView, recorder: Recorder) -> None:
        self.panel = panel
        self.view = view
        self.recorder = recorder
        self.tasks: list[asyncio.Task[Any]] = []

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.tasks.append(asyncio.get_running_loop().create_task(coro))

    async def pump(self) -> None:
        """Let everything the panel scheduled actually run, then drain."""
        await settle(6)
        self.bridge_drain()

    def bridge_drain(self) -> None:
        self.view._bridge.drain_pending()

    @property
    def last(self) -> dict[str, Any]:
        self.bridge_drain()
        return self.recorder.last("serve")

    @property
    def status(self) -> str:
        return str(self.last["status"])

    @property
    def port(self) -> int:
        server = self.panel.server
        assert server is not None, "nothing is listening"
        return server.port

    async def start(self, address: str = "127.0.0.1", *, no_token: bool = False) -> None:
        self.panel.start(address, 0, no_token)
        await self.pump()

    async def stop(self) -> None:
        self.panel.stop()
        await self.pump()

    async def dial(self, token: str | None) -> RemoteUIMonitor:
        return await asyncio.wait_for(
            RemoteUIMonitor.connect("127.0.0.1", self.port, token=token), TIMEOUT_S
        )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    project = tmp_path / "project"
    project.mkdir()
    return load_config(project, global_config_path=tmp_path / "none.toml")


@pytest.fixture
async def serving(tmp_path: Path, config: Config) -> AsyncIterator[ServeHarness]:
    recorder = Recorder()
    bridge = Bridge(recorder)
    monitor = FakeUIMonitor()
    panel = ServePanel(
        monitor,
        config_dir=tmp_path / "monitor",
        interfaces=lambda: list(INTERFACES),
        # Fast enough that a test can watch the status line refresh without
        # sleeping for a second of wall clock.
        tick_seconds=0.01,
    )
    holder: dict[str, ServeHarness] = {}
    view = CalibrationView(
        bridge,
        config=config,
        monitor=monitor,
        profile_root=tmp_path / "profiles",
        global_config_path=tmp_path / "none.toml",
        schedule=lambda coro: holder["h"].schedule(coro),
        serve=panel,
    )
    holder["h"] = ServeHarness(panel, view, recorder)
    yield holder["h"]
    await panel.close()
    for task in holder["h"].tasks:
        task.cancel()


# == starting and stopping ===================================================


async def test_the_panel_comes_up_not_serving(serving: ServeHarness) -> None:
    """A window that opened is a monitor nobody may drive yet: the decision is
    the operator's and the panel does not make it for them."""
    serving.panel.push()
    state = serving.last
    assert state["status"] == NOT_SERVING
    assert state["serving"] is False
    assert (state["link"], state["peer"]) == ("off", "")
    assert state["driving"] is None
    assert state["port"] == DEFAULT_PORT
    assert state["address"] == "127.0.0.1"


async def test_start_says_it_is_listening_and_that_nobody_is_on_the_line(
    serving: ServeHarness,
) -> None:
    """The first of §9.1's two sentences, and it names the address a person has
    to carry to the other machine."""
    await serving.start()
    assert serving.status == f"listening on 127.0.0.1:{serving.port} — no Chat UI attached"
    assert serving.last["serving"] is True
    assert serving.last["link"] == "waiting"


async def test_a_chat_ui_that_dials_with_the_token_is_named_in_the_status_line(
    serving: ServeHarness,
) -> None:
    """The second sentence. The peer's address is the whole content: "attached"
    with no name would leave the operator's actual question - WHICH machine has
    my mouse - unanswered."""
    await serving.start()
    brain = await serving.dial(serving.panel.token)
    try:
        serving.panel.push()
        status = serving.status
        assert status.startswith(f"listening on 127.0.0.1:{serving.port} — attached: ")
        assert serving.last["link"] == "attached"
        assert serving.last["peer"] and status.endswith(serving.last["peer"])
        assert "127.0.0.1:" in status.split("attached: ")[1]
    finally:
        await brain.close()


async def test_the_status_line_refreshes_on_its_own_while_serving(
    serving: ServeHarness,
) -> None:
    """Nothing pushes "a brain attached": the panel polls, because that is one
    read a second of two properties and a hook would be a hook grown for a
    label."""
    await serving.start()
    serving.recorder.clear()
    brain = await serving.dial(serving.panel.token)
    try:
        await asyncio.wait_for(_until(serving, "attached: "), TIMEOUT_S)
    finally:
        await brain.close()


async def test_a_wrong_token_is_refused_and_gets_no_session(
    serving: ServeHarness,
) -> None:
    """The whole point of the panel's token row. ``kind="unauthorized"`` is what
    the Chat UI turns into a form error on its token field rather than into a
    redial loop (§9.2)."""
    await serving.start()
    with pytest.raises(MonitorRefused) as refused:
        await serving.dial("0" * TOKEN_CHARS)
    assert refused.value.kind == "unauthorized"


async def test_a_missing_token_is_refused_the_same_way(serving: ServeHarness) -> None:
    """"You sent none" and "you sent the wrong one" are one refusal, said once:
    a message that told them apart would tell a dialler which half to fix."""
    await serving.start()
    with pytest.raises(MonitorRefused) as refused:
        await serving.dial(None)
    assert refused.value.kind == "unauthorized"


async def test_stop_stops_listening_and_says_so(serving: ServeHarness) -> None:
    """And the monitor is NOT closed by it: this panel borrowed one (§2.8)."""
    await serving.start()
    port = serving.port
    await serving.stop()
    assert serving.status == NOT_SERVING
    assert serving.panel.server is None
    with pytest.raises((ConnectionRefusedError, OSError)):
        await asyncio.wait_for(
            RemoteUIMonitor.connect("127.0.0.1", port, token=serving.panel.token), TIMEOUT_S
        )


# == the attached brain's palette (§11.7) ====================================


async def test_a_window_nobody_has_attached_to_wears_its_own_default(
    serving: ServeHarness,
) -> None:
    """"" is the panel's whole answer for "nobody has told me what to wear" -
    not "dark", which would be this window deciding something it does not get
    to decide."""
    serving.panel.push()
    assert serving.last["theme"] == ""
    assert serving.panel.theme is None


async def test_a_dial_that_names_a_theme_dresses_the_page_on_attach(
    serving: ServeHarness,
) -> None:
    """The palette rides the hello, so the page is painted right by the time
    the operator has read "attached"."""
    await serving.start()
    brain = await asyncio.wait_for(
        RemoteUIMonitor.connect(
            "127.0.0.1", serving.port, token=serving.panel.token, theme="claude-warm"
        ),
        TIMEOUT_S,
    )
    try:
        await serving.pump()
        assert serving.last["theme"] == "claude-warm"
    finally:
        await brain.close()


async def test_the_page_follows_the_theme_changing_mid_link(
    serving: ServeHarness,
) -> None:
    """F4 on the other machine: one ``set_theme`` verb, one repaint here."""
    await serving.start()
    brain = await serving.dial(serving.panel.token)
    try:
        await brain.set_theme("light")
        await serving.pump()
        assert serving.last["theme"] == "light"
        await brain.set_theme("claude-dark")
        await serving.pump()
        assert serving.last["theme"] == "claude-dark"
    finally:
        await brain.close()


async def test_the_theme_survives_the_brain_going_away(serving: ServeHarness) -> None:
    """A detach is not a reason to flash back to dark: the monitor is the
    standing half (§2.8) and what it wears is the last thing it was told."""
    await serving.start()
    brain = await serving.dial(serving.panel.token)
    await brain.set_theme("light")
    await serving.pump()

    await brain.close()
    await asyncio.wait_for(_until(serving, "no Chat UI attached"), TIMEOUT_S)

    assert serving.last["theme"] == "light"
    assert serving.panel.theme == "light"


# == the token ===============================================================


async def test_the_token_is_the_one_on_disk_and_survives_a_second_panel(
    tmp_path: Path, serving: ServeHarness
) -> None:
    """The panel SHOWS a secret the operator can also find in a file, because
    the panel is not always the thing they are looking at."""
    serving.panel.push()
    assert serving.panel.token == load_or_create_token(tmp_path / "monitor")
    assert len(serving.panel.token) == TOKEN_CHARS
    assert serving.last["token"] == serving.panel.token
    assert serving.last["token_path"].endswith("monitor-token")


async def test_regenerating_changes_the_token_and_the_old_one_stops_authorising(
    serving: ServeHarness,
) -> None:
    """The Regenerate button's whole contract: what changes is what the NEXT
    hello must carry."""
    await serving.start()
    old = serving.panel.token
    serving.panel.regenerate()
    await serving.pump()
    new = serving.panel.token
    assert new != old
    assert serving.last["token"] == new

    with pytest.raises(MonitorRefused) as refused:
        await serving.dial(old)
    assert refused.value.kind == "unauthorized"
    brain = await serving.dial(new)
    await brain.close()


async def test_regenerating_does_not_drop_the_attached_chat_ui(
    serving: ServeHarness,
) -> None:
    """§9.1, in as many words: a connection that already shook hands was already
    authorised, so the operator who rotates a secret does not lose the session
    they are watching while they do it."""
    await serving.start()
    brain = await serving.dial(serving.panel.token)
    try:
        serving.panel.regenerate()
        await serving.pump()
        assert brain.connected
        server = serving.panel.server
        assert server is not None and server.attached
    finally:
        await brain.close()


# == the loopback-only rule ==================================================


async def test_no_token_on_loopback_serves_an_unauthenticated_port(
    serving: ServeHarness,
) -> None:
    """The escape hatch, and it really is one: anything that can reach
    127.0.0.1 can already drive this mouse."""
    await serving.start(no_token=True)
    assert serving.last["serving"] is True
    brain = await serving.dial(None)
    await brain.close()


async def test_no_token_off_loopback_is_refused_into_the_panel(
    serving: ServeHarness,
) -> None:
    """The two opt-ins compose to "anyone on this network may drive this
    desktop", and that is not a thing a checkbox gets to say by accident. The
    refusal lands in the panel's error line rather than in a toast: the reason a
    port did not open belongs beside the port field."""
    serving.panel.start("192.168.1.40", 0, True)
    await serving.pump()
    state = serving.last
    assert state["serving"] is False
    assert "token" in state["error"]
    assert serving.panel.server is None


async def test_the_panel_says_which_addresses_need_no_opt_in(
    serving: ServeHarness,
) -> None:
    """Loopback first, "name — address" per row, and the flag the page gates the
    warning and the no-token box on."""
    serving.panel.push()
    state = serving.last
    assert [row["label"] for row in state["interfaces"]] == [
        "lo — 127.0.0.1",
        "eth0 — 192.168.1.40",
    ]
    assert [row["loopback"] for row in state["interfaces"]] == [True, False]
    assert state["loopback"] is True
    # The words are Python's and travel unconditionally; WHERE they are shown is
    # the page's call, because the dropdown moves without pressing Start.
    assert state["warning"] == REMOTE_WARNING


async def test_a_build_with_no_psutil_still_offers_loopback(tmp_path: Path) -> None:
    """``list_interfaces`` answers ``[]`` when the dependency is missing, and a
    dropdown with nothing in it is a window that cannot serve at all."""
    panel = ServePanel(
        FakeUIMonitor(), config_dir=tmp_path / "monitor", interfaces=lambda: []
    )
    rows = panel.state()["interfaces"]
    assert [row["value"] for row in rows] == [row.address for row in FALLBACK_INTERFACES]


# == the awkward ports =======================================================


async def test_a_port_already_in_use_lands_in_the_panel(serving: ServeHarness) -> None:
    """No toast, no traceback: one line beside the field that named the port."""
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        serving.panel.start("127.0.0.1", held.getsockname()[1], False)
        await serving.pump()
        state = serving.last
        assert state["serving"] is False
        assert state["error"]
        assert serving.panel.server is None
    finally:
        held.close()


async def test_a_command_line_port_comes_up_already_listening(
    tmp_path: Path, config: Config
) -> None:
    """``--port`` (and ``--bind``) pre-fill the panel and arm its auto-start, so
    a launcher that used to type the whole thing still works - and it fires
    exactly once, on the page's first build."""
    recorder = Recorder()
    bridge = Bridge(recorder)
    monitor = FakeUIMonitor()
    panel = ServePanel(
        monitor,
        config_dir=tmp_path / "monitor",
        serve_at=("127.0.0.1", 0),
        interfaces=lambda: list(INTERFACES),
    )
    tasks: list[asyncio.Task[Any]] = []
    view = CalibrationView(
        bridge,
        config=config,
        monitor=monitor,
        profile_root=tmp_path / "profiles",
        global_config_path=tmp_path / "none.toml",
        schedule=lambda coro: tasks.append(asyncio.get_running_loop().create_task(coro)),
        serve=panel,
    )
    try:
        view.start()
        await settle(6)
        bridge.drain_pending()
        assert panel.server is not None
        assert "listening on 127.0.0.1:" in recorder.last("serve")["status"]
        # Once: a second build must not try to bind the same port again.
        panel.start_if_requested()
        await settle(3)
        assert panel.server is not None
    finally:
        await panel.close()
        for task in tasks:
            task.cancel()


# == the chat region, persisted ==============================================


async def test_a_region_drawn_in_this_window_is_written_to_the_store(
    tmp_path: Path, config: Config
) -> None:
    """§8's open point, closed. The picker's ``set_region`` flows into
    ``configure``, and a ``LocalUIMonitor`` with a config dir saves what a spec
    carries - so the box survives the process that drew it.
    """
    store = tmp_path / "monitor"
    region = ScreenRegion(120, 60, 400, 300)
    view, tasks = _local_view(tmp_path, config, store)
    try:
        view.set_region(AgentSlot.MASTER, region)
        await settle(4)
        assert load_region(store, config.general.service) == region
    finally:
        for task in tasks:
            task.cancel()


async def test_a_restarted_monitor_serves_the_box_that_was_drawn_here(
    tmp_path: Path, config: Config
) -> None:
    """The other half of the one-line rule: a spec that omits a region is served
    from the store, so a second Monitor UI over the same config dir is pointed
    at the rectangle the first one drew without anybody re-drawing it."""
    store = tmp_path / "monitor"
    region = ScreenRegion(120, 60, 400, 300)
    first, tasks = _local_view(tmp_path, config, store)
    try:
        first.set_region(AgentSlot.MASTER, region)
        await settle(4)
    finally:
        for task in tasks:
            task.cancel()

    monitor = LocalUIMonitor(profile_for=lambda key: None, regions_dir=store)
    second, tasks = _local_view(tmp_path, config, store, monitor=monitor)
    try:
        # A brand-new view has drawn nothing, so its spec carries no region.
        second._retarget()
        await settle(4)
        assert monitor.saved_region(config.general.service) == region
        assert monitor._spec is not None and monitor._spec.region == region
    finally:
        for task in tasks:
            task.cancel()


def _local_view(
    tmp_path: Path,
    config: Config,
    store: Path,
    monitor: LocalUIMonitor | None = None,
) -> tuple[CalibrationView, list[asyncio.Task[Any]]]:
    """A view over a REAL ``LocalUIMonitor`` - the only one with a region store.

    ``profile_for`` answers None, so ``configure`` remembers the region and then
    returns without composing a detector or starting a poll thread: this suite
    is about the store, not about the screen.
    """
    tasks: list[asyncio.Task[Any]] = []
    machine = (
        monitor
        if monitor is not None
        else LocalUIMonitor(profile_for=lambda key: None, regions_dir=store)
    )
    view = CalibrationView(
        Bridge(Recorder()),
        config=config,
        monitor=machine,
        profile_root=tmp_path / "profiles",
        global_config_path=tmp_path / "none.toml",
        schedule=lambda coro: tasks.append(asyncio.get_running_loop().create_task(coro)),
    )
    return view, tasks


async def _until(harness: ServeHarness, needle: str) -> None:
    while True:
        harness.bridge_drain()
        events = harness.recorder.of_type("serve")
        if any(needle in event["status"] for event in events):
            return
        await asyncio.sleep(0.01)
