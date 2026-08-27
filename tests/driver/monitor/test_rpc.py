"""The monitor RPC end to end: a real socket, a real server, a real monitor.

docs/design/ui-monitor.md §6.5. Nothing here is mocked below the ``UIMonitor``
seam: :func:`~agentclip.driver.monitor.server.serve` listens on an ephemeral
loopback port in front of the same ``LocalUIMonitor`` the local suites drive
(scripted hand, fake clipboard, real threads), and
:class:`~agentclip.driver.monitor.remote.RemoteUIMonitor` dials it. What is
under test is the *seam*, so every assertion is of the form "the answer a caller
gets through the wire is the answer the monitor gave".

The four properties that are only true of the REMOTE monitor, and so can only be
asserted here:

* **The tick stream is pushed, not asked for** (§2.1, §2.7). The client owns a
  reader task from day one, ``latest`` is a field it updates, and ``observe()``
  resolves off it.
* **A ghost never crosses.** The filter is the monitor's (§4.2), and the wire
  must not reintroduce one.
* **One brain at a time** (§2.8), with the first one named in the refusal and
  unaffected by the second one's arrival.
* **Link loss is loud, and the monitor survives it** (§2.9). The far side keeps
  polling through a disconnect and answers a redial.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.protocol import ElementClick, Located, UIMonitor, Watched
from agentclip.driver.monitor.remote import (
    CONFIGURE_IS_LOCAL,
    MonitorCallError,
    MonitorDisconnected,
    MonitorRefused,
    RemoteUIMonitor,
)
from agentclip.driver.monitor.server import BindRefused, MonitorServer, serve
from agentclip.driver.monitor.wire import decode_line, encode_line, read_error
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.template import RegionMatch

from .conftest import TIMEOUT_S, ScriptedOps, Wiring, await_until, profile_with, spec

COPY = TemplateKind.COPY

# The drawn chat window, deliberately not at the origin: a rectangle handed to a
# click is absolute, and (0, 0) would pass whether or not the translation out of
# scene-local coordinates happened at all.
CHAT = ScreenRegion(1000, 500, 400, 300)
HIT = RegionMatch(30, 40, 0.0)
HIT_RECT = ScreenRegion(1030, 540, 20, 16)
# Where a click on that match lands with nobody's click point adjusted: the
# centre pixel, as ``Located.target`` carries it.
HIT_TARGET = ScreenRegion(1040, 548, 1, 1)
ELSEWHERE = RegionMatch(30, 200, 0.0)
ELSEWHERE_RECT = ScreenRegion(1030, 700, 20, 16)

# What the two fixtures below hand back: a coroutine function that opens the
# thing and registers it for teardown. Named, because every test signature says
# them and a bare ``Callable`` would say nothing about what it opens.
Listen = Callable[[UIMonitor], Awaitable[MonitorServer]]
Dial = Callable[[MonitorServer], Awaitable[RemoteUIMonitor]]


@pytest.fixture
async def listen() -> AsyncIterator[Listen]:
    """Start servers on ephemeral loopback ports; close every one afterwards.

    ``port=0`` and :attr:`MonitorServer.port` rather than a fixed number, because
    a suite that hardcoded a port would fail on the machine that already has
    something there - and would fail as a flake rather than as a message.
    """
    started: list[MonitorServer] = []

    async def start(monitor: UIMonitor) -> MonitorServer:
        server = await serve(monitor, port=0)
        started.append(server)
        return server

    yield start
    for server in reversed(started):
        await server.close()


@pytest.fixture
async def dial() -> AsyncIterator[Dial]:
    """Connect brains, and close every one afterwards."""
    opened: list[RemoteUIMonitor] = []

    async def connect(server: MonitorServer) -> RemoteUIMonitor:
        client = await RemoteUIMonitor.connect("127.0.0.1", server.port)
        opened.append(client)
        return client

    yield connect
    for client in reversed(opened):
        await client.close()


async def linked(
    wiring: Wiring, listen: Listen, dial: Dial
) -> tuple[MonitorServer, RemoteUIMonitor]:
    """One monitor, one server in front of it, one brain attached to that."""
    server = await listen(wiring.monitor)
    return server, await dial(server)


async def targeted(
    wiring: Wiring, client: RemoteUIMonitor, slot: AgentSlot = AgentSlot.MASTER, **kwargs: object
) -> Watched:
    """Point the far monitor at a window, the way §10.5 says it happens.

    The brain names a SLOT and nothing else; what that slot means is the far
    monitor's own ``spec_for``, which is what a test installs here in place of
    the config file (headless) or the Monitor UI's selection (with a window).
    Every retarget below goes through this, because ``client.configure`` is now
    a refusal - see ``test_configure_is_refused_over_the_wire``.
    """
    wiring.monitor.set_spec_for(lambda _slot: spec(**kwargs))  # type: ignore[arg-type]
    return await client.watch(slot)


async def quiet(wiring: Wiring, ops: ScriptedOps) -> None:
    """Suspend the poll loop and wipe the record, exactly as ``test_verbs`` does.

    A configured monitor POLLS, and a poll captures through the very hand these
    tests record, so the run is stopped before any verb is called. The detector,
    the trackers and the spec all survive a suspend, so what the verbs answer
    against is a genuinely configured monitor.
    """
    poller = wiring.monitor.poller
    await wiring.monitor.suspend()
    if poller is not None:
        poller.thread.join(TIMEOUT_S)
        assert not poller.thread.is_alive(), "a poll thread outlived the suspend"
    ops.forget()


# == watch =====================================================================


async def test_watch_round_trips_the_generation(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The number is the FAR monitor's, and it is what makes a tick a ghost.

    A brain that invented its own would be comparing its counter against the
    monitor's stamps, which is the one arithmetic a split deployment cannot get
    away with.
    """
    wiring = wire()
    _server, client = await linked(wiring, listen, dial)

    first = await targeted(wiring, client, region=None)
    assert first.generation == wiring.monitor.generation == 1
    assert client.generation == 1

    second = await targeted(wiring, client, region=None)
    assert second.generation == wiring.monitor.generation == 2
    assert client.generation == 2


async def test_watch_answers_with_the_whole_service_the_monitor_chose(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """§10.5: the brain names a WINDOW and the monitor answers with the service.

    Region, key and preset in one round trip, because the brain cannot compose a
    turn for this chat until it has all three - and it has no other way to learn
    any of them: the ``[services.*]`` table and the region store are both on the
    far machine.
    """
    wiring = wire()
    _server, client = await linked(wiring, listen, dial)

    watched = await targeted(wiring, client, region=CHAT, service="claude")

    assert watched.service == "claude"
    assert watched.region == CHAT
    # Straight off the spec the MONITOR composed, which is the whole point: a
    # brain that read its own host's presets would be sizing pastes for a
    # service somebody else is running.
    assert watched.max_paste_chars == spec().max_paste_chars
    assert watched.delivery == spec().delivery
    assert watched.snap_back == spec().snap_back


async def test_watch_says_which_appearances_the_far_machine_has(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """§11.3: which kinds are captured is an answer only the far side can give.

    The brain holds no templates at all now, so "is there a copy button?" used
    to be a read of its own profile store - empty on every machine but the one
    the pictures were taken on, which is how a fully calibrated desktop came to
    refuse every click.
    """
    wiring = wire(profile=profile_with(COPY, TemplateKind.NEW_CHAT), service="claude")
    _server, client = await linked(wiring, listen, dial)

    watched = await targeted(wiring, client, region=CHAT, service="claude")

    assert watched.profiled is True
    assert watched.captured == (COPY, TemplateKind.NEW_CHAT)


async def test_a_service_with_no_pictures_over_there_captures_nothing(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The split-mode trap, in the field the brain now gates on: a monitor
    calibrated for another service answers an empty tuple, and every element
    verdict the brain makes off it is a refusal with a reason."""
    wiring = wire(profile=profile_with(COPY), service="zai")
    _server, client = await linked(wiring, listen, dial)

    watched = await targeted(wiring, client, region=CHAT, service="claude")

    assert (watched.profiled, watched.captured) == (False, ())


async def test_watch_names_the_window_and_the_monitor_picks_the_service(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The slot crosses; nothing else about the target does."""
    wiring = wire()
    _server, client = await linked(wiring, listen, dial)
    asked: list[AgentSlot] = []
    wiring.monitor.set_spec_for(
        lambda slot: (asked.append(slot), spec(region=None, service=f"svc-{slot.value}"))[1]
    )

    assert (await client.watch(AgentSlot.SUBAGENT)).service == "svc-subagent"
    assert (await client.watch(AgentSlot.MASTER)).service == "svc-master"
    assert asked == [AgentSlot.SUBAGENT, AgentSlot.MASTER]


async def test_configure_is_refused_over_the_wire(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """A brain may not name a service, a rectangle or a search tolerance on a
    desktop it cannot see (§10.5) - and the refusal says which door replaced it
    rather than being an AttributeError at the call site."""
    wiring = wire()
    _server, client = await linked(wiring, listen, dial)

    with pytest.raises(MonitorCallError) as caught:
        await client.configure(spec(region=None))
    assert CONFIGURE_IS_LOCAL in str(caught.value)
    assert wiring.monitor.generation == 0, "the refusal retargeted the far monitor anyway"


# == the tick stream ===========================================================


async def test_a_tick_arrives_and_resolves_a_parked_observe(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """``latest`` is a local read (§2.1); ``observe()`` waits for the NEXT one.

    Armed against the newest ``seq`` the client had when it was called, so a
    tick from before the call cannot answer it - the same rule the local monitor
    keeps, and the reason a recipe may await one straight after a scroll.
    """
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=None)

    monitor.feed(monitor.stamp(busy=BusyProbe(BusyState.CHANGED, 0.5), active_detectors=("busy",)))
    await await_until(lambda: client.latest is not None, "the first tick to cross")
    first = client.latest
    assert first is not None and first.busy == BusyProbe(BusyState.CHANGED, 0.5)

    parked = asyncio.ensure_future(client.observe())
    await asyncio.sleep(0)  # let it arm against `first`
    monitor.feed(monitor.stamp(stale=None, active_detectors=("busy",)))
    observed = await asyncio.wait_for(parked, TIMEOUT_S)

    assert observed.seq > first.seq, "observe() answered with the tick it was armed against"
    assert client.latest == observed


async def test_ticks_reach_a_subscriber(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The fan-out the GUI's live panel hangs off, unchanged by the wire."""
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=None)
    seen: list[int] = []
    client.subscribe(lambda tick: seen.append(tick.seq))

    monitor.feed(monitor.stamp())
    await await_until(lambda: len(seen) == 1, "the first tick to reach the subscriber")
    monitor.feed(monitor.stamp())
    await await_until(lambda: len(seen) == 2, "the second tick to reach the subscriber")
    assert seen == sorted(seen), "ticks arrived out of order"


async def test_a_tick_backlog_is_dropped_to_the_latest(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """§8's undecided-until-now backlog policy, decided: drop-to-latest.

    Three ticks stamped before the server's loop gets a turn are three
    observations of a screen that has moved on twice, and ``observe()`` only
    ever wants the newest. So the slot holds one and the older two are dropped
    where they queued - on a WAN link that is the difference between a brain
    reading the screen and a brain reading a recording of it.
    """
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=None)
    seen: list[int] = []
    client.subscribe(lambda tick: seen.append(tick.seq))

    stamps = [monitor.stamp() for _ in range(3)]
    for tick in stamps:
        monitor.feed(tick)

    await await_until(lambda: seen == [stamps[-1].seq], "only the newest tick to cross")
    assert client.latest is not None and client.latest.seq == stamps[-1].seq


async def test_a_ghost_never_crosses(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """A tick stamped with a generation the monitor has moved past is dropped
    where it is made (§4.2). The wire must not smuggle one back in."""
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    _server, client = await linked(wiring, listen, dial)
    generation = (await targeted(wiring, client, region=None)).generation
    seen: list[int] = []
    client.subscribe(lambda tick: seen.append(tick.generation))

    monitor.feed(monitor.stamp(generation=generation - 1))
    monitor.feed(monitor.stamp())
    await await_until(lambda: seen == [generation], "only the live tick to cross")
    assert client.latest is not None and client.latest.generation == generation


# == the pixel verdicts ========================================================


async def test_find_all_crosses_with_every_match_in_screen_coordinates(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """Two of them, because "how many" is the question this verb exists for -
    and both in ABSOLUTE coordinates, because a click aimed at a scene-local
    rectangle would land in the top-left corner of the screen."""
    ops = ScriptedOps(all_matches=([HIT, ELSEWHERE],))
    wiring = wire(ops=ops, profile=profile_with(COPY))
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=CHAT, finish_signals=())
    await quiet(wiring, ops)

    assert await client.find_all(COPY) == (HIT_RECT, ELSEWHERE_RECT)
    assert ops.captures == [CHAT], "the verb searched something other than the drawn region"


async def test_locate_and_click_element_marshal_both_ways(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The find-then-click primitive, whole: where it is, that there was one of
    it, and the pixel that got pressed - which is the service's own click point
    inside the matched rectangle, computed on the monitor's machine."""
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],))
    wiring = wire(ops=ops, profile=profile_with(COPY))
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=CHAT, finish_signals=())
    await quiet(wiring, ops)

    assert await client.locate(COPY) == Located(
        region=HIT_RECT, ambiguous=False, best_miss=None, target=HIT_TARGET
    )
    assert await client.click_element(COPY) is ElementClick.CLICKED
    assert ops.captures == [CHAT, CHAT], "the verbs searched something else"
    assert [region for region, _settle in ops.clicks] == [HIT_TARGET]


async def test_the_click_point_is_applied_on_the_far_side(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """§11.3: ``target`` crosses already aimed.

    A click point is a percentage kept beside the picture it belongs to, and the
    pictures never leave the monitor - so a brain that had the rectangle and not
    the point would press the middle of whatever the service drew there.
    """
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],))
    aimed = profile_with(COPY)
    aimed.set_click_point(COPY, 25, 75)
    wiring = wire(ops=ops, profile=aimed)
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=CHAT, finish_signals=())
    await quiet(wiring, ops)

    assert (await client.locate(COPY)).target == ScreenRegion(1035, 551, 1, 1)


async def test_a_hover_scan_crosses_as_a_located_with_its_target(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The slow verb answers the same shape as the fast one, so the copy click
    after a hover is aimed by the same pixel as the one after a static search."""
    ops = ScriptedOps(lowest=((None, None), (HIT, None)))
    wiring = wire(ops=ops, profile=profile_with(COPY))
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=CHAT, finish_signals=())
    await quiet(wiring, ops)

    assert await client.hover_scan(COPY) == Located(
        region=HIT_RECT, ambiguous=False, best_miss=None, target=HIT_TARGET
    )


async def test_an_ambiguous_element_is_refused_across_the_wire(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """Two of them under one drawn region is two conversations, and picking one
    is a coin toss whose loser is a chat clicked on behalf of the other."""
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT, ELSEWHERE],))
    wiring = wire(ops=ops, profile=profile_with(COPY))
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=CHAT, finish_signals=())
    await quiet(wiring, ops)

    assert (await client.locate(COPY)).ambiguous is True
    assert await client.click_element(COPY) is ElementClick.AMBIGUOUS
    assert ops.clicks == [], "an ambiguous element was clicked anyway"


async def test_a_miss_crosses_as_a_miss_with_its_diagnosis(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """``best_miss`` is the number that turns "not found" into a report, so it is
    exactly the field a codec that only carried ``region`` would drop."""
    ops = ScriptedOps(lowest=((None, 0.31),))
    wiring = wire(ops=ops, profile=profile_with(COPY))
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=CHAT, finish_signals=())
    await quiet(wiring, ops)

    assert await client.locate(COPY) == Located(
        region=None, ambiguous=False, best_miss=0.31, target=None
    )
    assert await client.click_element(COPY) is ElementClick.MISMATCH
    assert ops.clicks == [], "a miss was clicked anyway"


async def test_a_plain_click_carries_its_rectangle_and_settle(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    ops = ScriptedOps()
    wiring = wire(ops=ops, profile=profile_with(COPY))
    _server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=None)
    ops.forget()

    assert await client.click(CHAT, settle_s=0.25) is True
    assert ops.clicks == [(CHAT, 0.25)]
    assert await client.scroll(CHAT, -3) is True
    assert ops.scrolls == [(CHAT, -3)]
    assert await client.scroll_key("end") is True
    assert ops.keys == [("end", 1)]


# == the clipboard =============================================================


async def test_the_clipboard_round_trips_and_the_watcher_pushes(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """§2.11: the clipboard is a monitor resource and only its TEXT crosses.

    Three facts in one test because they are one mechanism: a write goes to the
    far machine's clipboard, a read comes back from it, and the watcher polling
    that same clipboard pushes what somebody ELSE copied - never our own write,
    which is what the self-write set is for.
    """
    clipboard = FakeClipboard()
    wiring = wire(clipboard=clipboard)
    _server, client = await linked(wiring, listen, dial)
    captured: list[str] = []
    client.on_clip(captured.append)

    assert client.clipboard_kind == "fake", "the handshake did not carry the backend"
    await client.write_clipboard("the outbound message")
    assert await client.read_clipboard() == "the outbound message"
    assert clipboard.written == ["the outbound message"]

    assert client.watch_clipboard(True) is True
    # A round trip AFTER the watcher call, so the watcher is provably running
    # before anything is copied into it.
    await client.read_clipboard()
    clipboard.set_text("a reply, copied by the browser")

    await await_until(lambda: captured == ["a reply, copied by the browser"], "the clip to cross")
    assert client.watch_clipboard(False) is False


async def test_a_monitor_with_no_clipboard_refuses_to_watch_without_asking(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """``watch_clipboard`` is the Protocol's one SYNCHRONOUS verb, so it cannot
    await its own round trip - it answers from the backend the handshake named,
    which is the same arithmetic the local monitor does."""
    wiring = wire()  # no provider at all
    _server, client = await linked(wiring, listen, dial)

    assert client.clipboard_kind is None
    assert client.watch_clipboard(True) is False


# == one brain at a time =======================================================


async def test_a_second_brain_is_refused_by_name_and_the_first_keeps_working(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """Two brains would be two hands on one mouse (§2.8).

    The refusal NAMES the first peer, because the operator's next question is
    always "which of my two windows is holding it".
    """
    wiring = wire()
    server, first = await linked(wiring, listen, dial)
    held_by = server.peer
    assert held_by is not None

    with pytest.raises(MonitorRefused) as caught:
        await dial(server)
    assert caught.value.kind == "busy"
    assert held_by in caught.value.message

    watched = await targeted(wiring, first, region=None)
    assert watched.generation == 1, "the first brain lost its link"
    assert server.peer == held_by


# == link loss and redial ======================================================


async def test_a_dropped_connection_raises_out_of_observe_and_fires_the_hook(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """§2.9: nothing is buffered and nothing is replayed, so the ONLY correct
    thing to do with a parked wait is to fail it loudly."""
    wiring = wire()
    server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=None)
    dropped: list[str] = []
    client.on_disconnect(lambda: dropped.append("gone"))
    parked = asyncio.ensure_future(client.observe())
    await asyncio.sleep(0)

    assert await server.disconnect() is True

    with pytest.raises(MonitorDisconnected):
        await asyncio.wait_for(parked, TIMEOUT_S)
    await await_until(lambda: dropped == ["gone"], "the disconnect hook to fire")
    assert client.connected is False
    with pytest.raises(MonitorDisconnected):
        await client.watch(AgentSlot.MASTER)


async def test_a_disconnect_stops_the_clipboard_watcher_it_started(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The watcher is session-scoped: the brain turned it on, and once nobody is
    listening it would be capturing into a fan-out with no far end."""
    wiring = wire(clipboard=FakeClipboard())
    server, client = await linked(wiring, listen, dial)
    assert client.watch_clipboard(True) is True
    await client.read_clipboard()  # a barrier: the watcher is running
    await await_until(
        lambda: any(t.name == "agentclip-clipwatch" for t in threading.enumerate()),
        "the watcher thread to start",
    )

    await server.disconnect()

    await await_until(
        lambda: (
            not any(t.name == "agentclip-clipwatch" and t.is_alive() for t in threading.enumerate())
        ),
        "the watcher thread to stop",
    )


async def test_a_redial_works_and_the_monitor_never_stopped_polling(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The whole of §2.8 in one assertion: the monitor is a standing process.

    It polled through the disconnect, so the redialled brain gets a tick without
    anybody feeding one - and it reaches the SAME process, which is what the
    ``server_id`` is for.
    """
    ops = ScriptedOps()
    wiring = wire(ops=ops, profile=profile_with(COPY))
    server, first = await linked(wiring, listen, dial)
    generation = (await targeted(wiring, first, region=CHAT, finish_signals=())).generation
    await server.disconnect()

    second = await dial(server)
    assert second.server_id == first.server_id, "the redial reached a different monitor"

    tick = await asyncio.wait_for(second.observe(), TIMEOUT_S)
    assert tick.generation == generation, "the monitor forgot what it was watching"
    # And the generation the new brain holds is the far side's, without a
    # retarget - though a brain that redials still calls ``watch`` (§2.9: it
    # re-derives from the screen rather than trusting what it remembers, and
    # §10.5: that is also how it re-reads the service).
    assert second.generation == generation
    assert (await targeted(wiring, second, region=None)).generation == generation + 1


async def test_closing_the_client_leaves_the_monitor_running(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """``close()`` on a remote monitor closes the LINK. The far monitor is a
    process that outlives every brain, and a client cannot end it."""
    wiring = wire()
    server, client = await linked(wiring, listen, dial)
    await targeted(wiring, client, region=None)

    await client.close()
    await client.close()  # idempotent

    await await_until(lambda: server.peer is None, "the server to notice the goodbye")
    again = await dial(server)
    assert (await targeted(wiring, again, region=None)).generation == 2


# == the theme (§11.7) =========================================================


async def test_the_hello_dresses_the_monitor_before_the_first_call(
    wire: Callable[..., Wiring], listen: Listen
) -> None:
    """The palette rides the handshake, so a window is right on the first paint.

    Dialled by hand rather than through the ``dial`` fixture, because the theme
    is exactly what that fixture does not pass - and the point of the field is
    that it is settled by the time the connection is usable.
    """
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    server = await listen(monitor)

    client = await RemoteUIMonitor.connect("127.0.0.1", server.port, theme="claude-warm")
    try:
        assert monitor.theme == "claude-warm"
        assert server.peer_theme == "claude-warm"
    finally:
        await client.close()


async def test_a_hello_with_no_theme_leaves_the_monitor_its_own_default(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """A brain that names no palette says nothing, rather than saying "dark"."""
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    server, _client = await linked(wiring, listen, dial)

    assert monitor.theme is None
    assert server.peer_theme is None


async def test_set_theme_crosses_and_fires_the_monitors_hook(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """The user changed it mid-link (F4, ``/theme``): the far window follows.

    And the same palette twice is not an event - the hook is a page repaint,
    and a redial under an unchanged theme would repaint for nothing.
    """
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    _server, client = await linked(wiring, listen, dial)
    worn: list[str] = []
    monitor.on_theme(worn.append)

    await client.set_theme("light")
    await client.set_theme("light")
    await client.set_theme("claude-dark")

    assert worn == ["light", "claude-dark"]
    assert monitor.theme == "claude-dark"


async def test_the_theme_outlives_the_brain_that_asked_for_it(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """A detach must not flash the monitor's window back to dark (§2.8: the
    monitor is the standing half, and what it wears is its own now)."""
    wiring = wire()
    monitor: LocalUIMonitor = wiring.monitor
    server, client = await linked(wiring, listen, dial)
    await client.set_theme("light")

    await client.close()
    await await_until(lambda: server.peer is None, "the link to be forgotten")

    assert server.peer_theme is None, "a theme cannot be read off a connection that is gone"
    assert monitor.theme == "light", "the monitor forgot the palette when the brain left"


# == the handshake and the bind ================================================


async def test_a_version_mismatch_is_refused_naming_both_installs(
    wire: Callable[..., Wiring], listen: Listen
) -> None:
    """Every frame after the handshake is decoded as v1, so a peer on another
    version is refused before a second line is read - and refused with both
    PACKAGE versions, which is the half a human can act on."""
    wiring = wire()
    server = await listen(wiring.monitor)

    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        hello = {"type": "hello", "version": 99, "package": "9.9.9"}
        writer.write(encode_line(hello).encode("utf-8"))
        await writer.drain()
        refusal = read_error(decode_line((await reader.readline()).decode("utf-8")))
        assert refusal.id is None and refusal.kind == "bad_request"
        assert "99" in refusal.message and "9.9.9" in refusal.message
        assert not await reader.readline(), "the server kept talking to a v99 peer"
    finally:
        writer.close()

    # And the refusal cost the monitor nothing: the slot is free again.
    await await_until(lambda: server.peer is None, "the refused peer to be forgotten")


async def test_a_non_loopback_bind_needs_the_opt_in(wire: Callable[..., Wiring]) -> None:
    """§5: the monitor port is a channel to this machine's mouse, keyboard and
    clipboard, so listening anywhere but loopback has to be asked for by name -
    and, since the token landed, guarded by one too (test_auth.py owns that
    half). Checked at construction, so nothing is bound either way - which is
    also why this test does not open a socket on every interface.
    """
    wiring = wire()
    with pytest.raises(BindRefused):
        MonitorServer(wiring.monitor, host="0.0.0.0", port=0)
    # The opt-in is the whole API: one keyword, set by ``--bind``.
    MonitorServer(wiring.monitor, host="0.0.0.0", port=0, allow_remote=True, token="s3cret")
    MonitorServer(wiring.monitor, port=0)


async def test_watched_is_the_far_monitors_answer(
    wire: Callable[..., Wiring], listen: Listen, dial: Dial
) -> None:
    """A brain with no rectangle asks, and gets the one the monitor's machine
    remembered (§9.1): the only way a split-mode Chat UI ever learns where the
    chat window is."""
    wiring = wire()
    _server, client = await linked(wiring, listen, dial)
    assert (await client.watched()).region is None
    await targeted(wiring, client, region=CHAT)
    watched = await client.watched()
    assert watched.region == CHAT
    assert watched.service == spec(region=CHAT).service
    assert watched.generation == client.generation
