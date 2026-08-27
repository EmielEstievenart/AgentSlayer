"""The Monitor tab of the connect dialog (docs/design/ui-monitor.md §9.2).

Split mode stops being a launch flag here: the Chat UI dials a Monitor from a
form, mid-session, and lets go of one the same way. What that is - and the whole
reason it needs no session boundary - is that a dial is a **link event**: the
loop parks in ``DISCONNECTED``, the ``SwitchableMonitor``'s inner is replaced
and the recipe re-derives from the screen (§2.9). The transcript, the engine and
the files are the Executor's half and none of them move.

Nothing here opens a socket, and nothing here opens an SSH connection. The dial
is the same injected seam ``test_monitor_link.py`` uses (a ``ScriptedLink``,
which is a ``FakeUIMonitor`` with the three members a LINK has), and "the
Executor is connected" is a fake host with an ``open_tunnel`` that hands back a
loopback address without a paramiko channel behind it. What is under test is the
sequence and the refusals, which a real transport would only make slower to
assert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentclip.cli import make_engine_factory
from agentclip.config import Config, MonitorTarget, load_config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.switchable import SwitchableMonitor
from agentclip.driver.screen.region import ScreenRegion
from agentclip.shell.chat import view as view_module
from agentclip.shell.chat.remote import (
    MONITOR_BAD_TOKEN,
    MONITOR_CONNECT_FIRST,
    MONITOR_MISSING_HOST,
)
from agentclip.shell.chat.view import CALIBRATION_REMOTE, GuiView
from agentclip.shell.webview.bridge import Bridge

from .conftest import Recorder

TOKEN = "a" * 32
PEER = "10.0.0.5:7777"


class ScriptedLink(FakeUIMonitor):
    """A dialled monitor: the fake, plus the three members only a LINK has."""

    def __init__(self, *, server_id: str = "monitor-1", peer: str = PEER) -> None:
        super().__init__(clipboard=select_provider("manual"))
        self.server_id = server_id
        self.peer = peer
        self.disconnect_hooks: list[Callable[[], None]] = []

    def on_disconnect(self, hook: Callable[[], None]) -> Callable[[], None]:
        self.disconnect_hooks.append(hook)
        return lambda: None

    def drop(self) -> None:
        for hook in list(self.disconnect_hooks):
            hook()


class Dialler:
    """The scripted dial, recording every address and token it was offered."""

    def __init__(self, *outcomes: ScriptedLink | Exception) -> None:
        self.outcomes = list(outcomes)
        self.dialled: list[tuple[str, int]] = []
        self.tokens: list[str] = []

    async def __call__(self, host: str, port: int, token: str = "") -> Any:
        self.dialled.append((host, port))
        self.tokens.append(token)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTunnel:
    """What ``SshHost.open_tunnel`` hands back, minus the paramiko channel.

    The two members the view reads (``local_host``/``local_port``) and the one
    it calls, so "the dial went to the tunnel's loopback address" and "the
    tunnel was closed exactly once" are both observable.
    """

    def __init__(self, dest: str) -> None:
        self.dest = dest
        self.local_host = "127.0.0.1"
        self.local_port = 54321
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class FakeSshHost:
    """A live ``SshHost``, for the one method the Monitor tab needs of it."""

    def __init__(self, target: str = "pi") -> None:
        self.target = target
        self.connected = True
        self.tunnels: list[FakeTunnel] = []

    def open_tunnel(self, dest_host: str, dest_port: int) -> FakeTunnel:
        tunnel = FakeTunnel(f"{dest_host}:{dest_port}")
        self.tunnels.append(tunnel)
        return tunnel


class Tab:
    """A view with the Monitor tab drivable, its recorder and its dial script."""

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

    def event(self) -> dict[str, Any]:
        return self.flush().last("monitor")

    @property
    def inner(self) -> Any:
        """Whichever monitor the switchable handle is pointed at right now."""
        assert isinstance(self.view.monitor, SwitchableMonitor)
        return self.view.monitor.inner


SAVED = f"""\
[remote.pi]
host = "raspberrypi.local"
user = "emiel"
root = "/home/emiel/code/thing"

[monitor.vm]
host = "10.0.0.5"
port = 7777
token = "{TOKEN}"

[monitor.tunnelled]
via = "pi"
"""


@pytest.fixture
def global_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(SAVED, encoding="utf-8")
    return path


@pytest.fixture
def saved_config(project: Path, global_path: Path) -> Config:
    return load_config(project, global_config_path=global_path)


def build(
    project: Path,
    config: Config,
    tmp_path: Path,
    dial: Dialler,
    *,
    global_path: Path | None = None,
    host: Any = None,
) -> Tab:
    recorder = Recorder()
    bridge = Bridge(recorder)
    holder: dict[str, Tab] = {}
    view = GuiView(
        bridge,
        config=config,
        provider=select_provider("manual"),
        engine_factory=make_engine_factory(lambda: config, project),
        project_root=project,
        profile_root=tmp_path / "profiles",
        global_config_path=(
            global_path if global_path is not None else tmp_path / "no-such-global.toml"
        ),
        host=host,
        # The real loop: the dial, the configure and the detach are coroutines
        # the view puts on it, and a schedule that closed them would test
        # nothing (``test_monitor_link.py``'s bargain).
        schedule=lambda coro: asyncio.ensure_future(coro),
        dial=dial,
        open_calibration=lambda *_a: holder["t"].__setattr__(
            "calibrations", holder["t"].calibrations + 1
        ),
    )
    holder["t"] = Tab(view, recorder, bridge, dial)
    return holder["t"]


async def settle(times: int = 40) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redial's wait, flattened - ``test_monitor_link.py``'s bargain: what
    the backoff is FOR is not a claim a suite can make faster."""
    monkeypatch.setattr(view_module, "MONITOR_BACKOFF_START", 0.0)


# == the handle is switchable in EVERY mode ====================================


def test_a_local_window_runs_over_a_switchable_handle_around_a_local_monitor(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """§9.2's one structural change: the controller is handed a
    ``SwitchableMonitor`` even with no ``--monitor`` flag, whose inner is the
    ordinary ``LocalUIMonitor``. That is what makes a mid-session dial a swap
    rather than a rebuild - and what makes the way back a swap too."""
    tab = build(project, app_config, tmp_path, Dialler(ScriptedLink()))

    assert isinstance(tab.view.monitor, SwitchableMonitor)
    assert isinstance(tab.inner, LocalUIMonitor)
    # ...and the local-only tier still answers through the handle, which is
    # what the DETECTION block and the ELEMENTS surface read.
    assert tab.view.monitor.self_writes is tab.inner.self_writes
    tab.view.monitor.reset_trackers()  # forwarded, and not an AttributeError


# == the form ==================================================================


def test_the_tab_opens_with_the_saved_monitors_and_the_saved_ssh_targets(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """Two lists, from two tables, neither re-parsed here: the monitors come
    from ``[monitor.<name>]`` and the Via-SSH dropdown from the very
    ``[remote.<name>]`` tables the Executor tab offers."""
    tab = build(project, saved_config, tmp_path, Dialler(ScriptedLink()))

    tab.view.monitor_open()
    event = tab.event()

    assert event["open"] is True
    assert [row["name"] for row in event["saved"]] == ["tunnelled", "vm"]
    assert {row["name"]: row["detail"] for row in event["saved"]}["vm"] == PEER
    assert {row["name"]: row["detail"] for row in event["saved"]}["tunnelled"] == (
        "via pi -> 127.0.0.1:7777"
    )
    assert [row["name"] for row in event["ssh"]] == ["pi"]
    assert event["attached"] == ""


def test_selecting_a_saved_monitor_fills_the_form_including_its_token(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """The row carries no token - a picker list is a thing people screenshot -
    so the view fills it in off the config when the row is picked."""
    tab = build(project, saved_config, tmp_path, Dialler(ScriptedLink()))

    tab.view.monitor_open()
    tab.view.monitor_select("monitor:vm")
    event = tab.event()

    assert (event["mode"], event["host"], event["port"]) == ("direct", "10.0.0.5", "7777")
    assert event["token"] == TOKEN
    # ...and the row itself never carried it.
    assert all("token" not in row for row in event["saved"])


def test_a_form_with_no_host_and_a_short_token_says_so_before_any_dial(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The two things a form CAN know. A wrong-length token is a paste that went
    wrong, and catching it here costs one refused handshake less."""
    dial = Dialler(ScriptedLink())
    tab = build(project, app_config, tmp_path, dial)
    tab.view.monitor_open()

    tab.view.monitor_fields("direct", "", "7777", "", "")
    tab.view.monitor_start()
    assert tab.event()["error"] == MONITOR_MISSING_HOST

    tab.view.monitor_fields("direct", "10.0.0.5", "7777", "abc", "")
    tab.view.monitor_start()
    assert tab.event()["error"] == MONITOR_BAD_TOKEN.format(n=3)

    assert dial.dialled == []  # nothing was ever asked of the network


# == the Direct dial ===========================================================


async def test_a_direct_dial_swaps_the_monitor_in_and_configures_it(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The whole point of §9.2, in one test: the screen moves onto another
    machine mid-session. The link is swapped into the handle the controller
    already holds, the monitor is retargeted (it kept polling and counting while
    nobody was attached, so nothing it says is trustworthy until it has been),
    the far clipboard watcher is re-armed and the loop lands on IDLE - from
    which the recipe re-runs off the screen rather than off anything replayed.
    """
    link = ScriptedLink()
    tab = build(project, app_config, tmp_path, Dialler(link))
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")

    tab.view.monitor_start()
    await settle()

    assert tab.dial.dialled == [("10.0.0.5", 7777)]
    assert tab.dial.tokens == [TOKEN]
    assert tab.inner is link
    assert link.specs, "the attached monitor was never retargeted"
    assert ("watch_clipboard", (True,)) in link.calls
    assert tab.view.automation.loop_state is LoopState.IDLE
    event = tab.event()
    assert event["phase"] == "done"
    assert event["attached"] == PEER


async def test_a_wrong_token_lands_on_the_dialogs_failed_phase(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """§9.1's refusal, shown where it can be acted on. A retry LOOP against a
    bad token is how you lock yourself out of noticing, so the message goes on
    the form and the dialog waits for a human."""
    refusal = ConnectionError("the monitor refused this connection: bad or missing token")
    tab = build(project, app_config, tmp_path, Dialler(refusal))
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", "b" * 32, "")

    tab.view.monitor_start()
    await settle()

    event = tab.event()
    assert event["phase"] == "failed"
    assert "bad or missing token" in event["failure"]
    # ...and the failed attempt left no redial loop chasing that machine: the
    # window is still watching the screen it was watching before.
    assert isinstance(tab.inner, LocalUIMonitor)


# == the Via-SSH dial ==========================================================


async def test_via_ssh_opens_a_tunnel_on_the_live_host_and_dials_its_loopback(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """The deployment §5 always documented, finally spelled as a button: one
    ``direct-tcpip`` channel on the connection the Executor already holds, and
    the monitor client dials a local port. No second login, no second host-key
    question, no external ``ssh -L`` - and a token is STILL required, because
    SSH proves who reached the port, not which of the several things on that VM
    did."""
    link = ScriptedLink(peer="via pi -> 127.0.0.1:7777")
    host = FakeSshHost("pi")
    tab = build(project, saved_config, tmp_path, Dialler(link), host=host)
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("ssh", "127.0.0.1", "7777", TOKEN, "pi")

    tab.view.monitor_start()
    await settle()

    assert [t.dest for t in host.tunnels] == ["127.0.0.1:7777"]
    assert tab.dial.dialled == [("127.0.0.1", 54321)]  # the TUNNEL's local end
    assert tab.dial.tokens == [TOKEN]
    assert tab.inner is link
    assert tab.event()["phase"] == "done"


async def test_via_ssh_refuses_when_the_executor_is_on_another_machine(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """Deliberately a refusal with a hint rather than a second connect flow:
    running the SSH sequence from here would end the user's session (one
    session, one host) from behind a button that says "attach a monitor"."""
    host = FakeSshHost("other-box")
    tab = build(project, saved_config, tmp_path, Dialler(ScriptedLink()), host=host)
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("ssh", "127.0.0.1", "7777", "", "pi")

    tab.view.monitor_start()
    await settle()

    event = tab.event()
    assert event["phase"] == "failed"
    assert MONITOR_CONNECT_FIRST.format(name="pi") in event["failure"]
    assert host.tunnels == []
    assert tab.dial.dialled == []


async def test_via_ssh_with_no_executor_connection_at_all_refuses_the_same_way(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """A local session has no SSH connection to ride, and the answer is the same
    sentence: connect the Executor first."""
    tab = build(project, saved_config, tmp_path, Dialler(ScriptedLink()))
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("ssh", "", "", "", "pi")

    tab.view.monitor_start()
    await settle()

    assert MONITOR_CONNECT_FIRST.format(name="pi") in tab.event()["failure"]


async def test_a_redial_over_ssh_opens_a_fresh_tunnel(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """A tunnel serves exactly one local connection and the link that used it is
    the one that died, so the backoff cannot reuse it - it opens another, and
    the spent one is closed on the way past."""
    first, second = ScriptedLink(), ScriptedLink()
    host = FakeSshHost("pi")
    tab = build(project, saved_config, tmp_path, Dialler(first, second), host=host)
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("ssh", "127.0.0.1", "7777", TOKEN, "pi")
    tab.view.monitor_start()
    await settle()

    first.drop()
    await settle(80)

    assert len(host.tunnels) == 2
    assert host.tunnels[0].closes == 1
    assert tab.inner is second


# == letting go ================================================================


async def test_disconnect_swaps_a_local_monitor_back_and_reopens_calibration(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The swap in the other direction, and it is the same swap. The calibration
    door - closed for as long as the pixels were on another machine (§6.4) -
    opens again, because they are back on this one."""
    link = ScriptedLink()
    tab = build(project, app_config, tmp_path, Dialler(link))
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")
    tab.view.monitor_start()
    await settle()

    tab.view.open_calibration()
    assert tab.calibrations == 0
    assert CALIBRATION_REMOTE in tab.toasts()

    tab.view.monitor_disconnect()
    await settle()

    assert isinstance(tab.inner, LocalUIMonitor)
    assert link.closed
    assert tab.event()["attached"] == ""
    assert tab.view.automation.loop_state is LoopState.IDLE

    tab.view.open_calibration()
    assert tab.calibrations == 1


async def test_disconnect_closes_the_ssh_tunnel_under_the_link(
    project: Path, saved_config: Config, tmp_path: Path
) -> None:
    """Nothing should be left running on the target because a window stopped
    looking at it."""
    host = FakeSshHost("pi")
    tab = build(project, saved_config, tmp_path, Dialler(ScriptedLink()), host=host)
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("ssh", "127.0.0.1", "7777", TOKEN, "pi")
    tab.view.monitor_start()
    await settle()

    tab.view.monitor_disconnect()
    await settle()

    assert host.tunnels[0].closes == 1


# == saving ====================================================================


async def test_a_successful_dial_offers_a_save_that_round_trips_through_the_config(
    project: Path, app_config: Config, tmp_path: Path, global_path: Path
) -> None:
    """"Save this monitor as..." writes one ``[monitor.<name>]`` table in the
    GLOBAL file - token included, stated rather than hidden - and the picker
    offers it on the next visit without the file being re-read."""
    tab = build(
        project, app_config, tmp_path, Dialler(ScriptedLink()), global_path=global_path
    )
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")
    tab.view.monitor_start()
    await settle()

    assert tab.event()["can_save"] is True
    tab.view.monitor_save("desk-vm")

    written = load_config(project, global_config_path=global_path).monitor.targets
    assert written["desk-vm"] == MonitorTarget(
        name="desk-vm", host="10.0.0.5", port=7777, token=TOKEN
    )
    event = tab.event()
    assert event["can_save"] is False
    assert "[monitor.desk-vm]" in event["saved_note"]
    assert "desk-vm" in [row["name"] for row in event["saved"]]


async def test_a_monitor_this_pc_already_has_is_not_offered_for_saving_again(
    project: Path, saved_config: Config, tmp_path: Path, global_path: Path
) -> None:
    """The offer is about the ADDRESS, not the name: nothing in this form is a
    name, so "already saved" can only mean "the same machine and port"."""
    tab = build(
        project, saved_config, tmp_path, Dialler(ScriptedLink()), global_path=global_path
    )
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_select("monitor:vm")
    tab.view.monitor_start()
    await settle()

    assert tab.event()["phase"] == "done"
    assert tab.event()["can_save"] is False


# == the page and the bridge agree ============================================


def test_every_monitor_verb_the_page_calls_exists_on_the_bridge() -> None:
    """The page and the shim are two files that have to say the same words. A
    typo'd ``api("monitor_...")`` is a button that silently does nothing, which
    is the one failure mode a webview gives no feedback for at all."""
    import re
    from importlib.resources import files

    from agentclip.shell.webview.bridge import JsApi

    js = (files("agentclip.shell.chat.assets") / "app.js").read_text(encoding="utf-8")
    called = set(re.findall(r'api\("(monitor_[a-z_]+)"', js))

    assert called, "the page calls no monitor verb at all"
    assert called <= {name for name in dir(JsApi) if name.startswith("monitor_")}


def test_every_monitor_element_the_page_reaches_for_is_in_the_page() -> None:
    """The other half of the same agreement: an ``id()`` that resolves to null
    is a repaint that throws on the first event and takes the whole dispatch
    with it."""
    import re
    from importlib.resources import files

    assets = files("agentclip.shell.chat.assets")
    js = (assets / "app.js").read_text(encoding="utf-8")
    html = (assets / "index.html").read_text(encoding="utf-8")
    wanted = set(re.findall(r'id\("((?:mon|conn-tab|conn-exec|conn-monitor)[a-z-]*)"\)', js))

    assert wanted, "the page reaches for no monitor element at all"
    for name in sorted(wanted):
        assert f'id="{name}"' in html, f"app.js reaches for #{name}, index.html has none"


async def test_the_titlebar_badge_says_local_then_connected_then_local_again(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The standing fact, not the toast: the titlebar's MONITOR badge is painted
    on mount (this PC), goes `up` with the peer on a landed dial, and back to
    local on a deliberate detach."""
    link = ScriptedLink()
    tab = build(project, app_config, tmp_path, Dialler(link))
    tab.view.start()
    await settle()
    assert tab.flush().last("monitor_link")["state"] == "local"

    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")
    tab.view.monitor_start()
    await settle()
    badge = tab.flush().last("monitor_link")
    assert (badge["state"], badge["peer"]) == ("up", "10.0.0.5:7777")

    tab.view.monitor_disconnect()
    await settle()
    assert tab.flush().last("monitor_link")["state"] == "local"


async def test_a_dropped_link_paints_the_badge_down_with_the_reason(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    link = ScriptedLink()
    tab = build(project, app_config, tmp_path, Dialler(link))
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")
    tab.view.monitor_start()
    await settle()

    tab.view._monitor_dropped("10.0.0.5:7777")
    badge = tab.flush().last("monitor_link")
    assert badge["state"] == "down"
    assert "lost" in badge["reason"]


async def test_a_box_remembered_over_there_becomes_the_brains_calibration(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The bug as seen: the monitor's badge is green, its ELEMENTS column sees
    everything, and /new still says no chat window is drawn - because the box
    was drawn on the MONITOR's desktop and the brain never asked for it. After
    a dial the brain adopts what the monitor settled on."""
    link = ScriptedLink()
    tab = build(project, app_config, tmp_path, Dialler(link))
    tab.view.start()
    await settle()
    live = tab.view.automation.live_slot
    assert tab.view.automation.calibration(live).chat_region is None
    service = tab.view._service_for(live)
    box = ScreenRegion(100, 200, 640, 480)
    link.fills_from_store = True
    link.saved_regions[service] = box

    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")
    tab.view.monitor_start()
    await settle()

    assert tab.view.automation.calibration(live).chat_region == box
    assert "chatbot window" in tab.flush().last("sidebar")["region"]


async def test_a_service_the_monitor_has_no_appearance_for_is_said_out_loud(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The other half of the bug as seen: the monitor's badge is green and its
    ELEMENTS column sees everything - for the service ITS window selected. The
    Chat UI drives its own key; if that names nothing over there, every click
    is NOT_CALIBRATED. Now the DETECTION line and one toast say so."""
    link = ScriptedLink()
    link.profiled = False
    tab = build(project, app_config, tmp_path, Dialler(link))
    tab.view.start()
    await settle()
    tab.view.monitor_open()
    tab.view.monitor_fields("direct", "10.0.0.5", "7777", TOKEN, "")
    tab.view.monitor_start()
    await settle()

    service = tab.view._service_for(tab.view.automation.live_slot)
    toasts = tab.toasts()
    assert any("no captured appearance for service '" + service + "'" in t for t in toasts)
    assert any("10.0.0.5:7777" in t for t in toasts)
