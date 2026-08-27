"""``SwitchableMonitor``: one handle, several monitors, nobody rebuilt.

The class exists for one launch shape (``--monitor host:port``, ui-monitor.md
§6.5) and one event inside it (§2.9's link loss): the brain is constructed
before there is a link, and every reconnect produces a NEW
``RemoteUIMonitor``. So what these tests are about is not forwarding - that
part is a one-liner per verb - but the four things a plain proxy would get
wrong:

* the inert start, where a brain that acts before the first dial has to take
  the "nothing there" branch rather than crash;
* subscribers and clipboard hooks surviving a swap, because the automation
  registers exactly once, in its constructor;
* an ``observe`` parked while nothing was attached being answered by the first
  tick the NEXT monitor pushes;
* the old monitor going quiet the instant it is replaced.

The inners are ``FakeUIMonitor``s, so a "reconnect" here is a second one being
swapped in - which is exactly what a redial is from this object's side.
"""

from __future__ import annotations

import asyncio

import pytest

from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.protocol import EMPTY_WATCHED, ElementClick, Tick
from agentclip.driver.monitor.switchable import IdleMonitor, SwitchableMonitor
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.stale import StaleTracker

from .conftest import spec

REGION = ScreenRegion(1, 2, 3, 4)


async def settle(times: int = 3) -> None:
    """Let ``call_soon_threadsafe`` callbacks run: a tick resolves a parked
    ``observe`` through the loop, never inline."""
    for _ in range(times):
        await asyncio.sleep(0)


# == the inert start ==========================================================


async def test_before_the_first_dial_every_action_is_nothing_happened() -> None:
    """A brain that acts before its link is up must read as an uncalibrated
    machine, not as a broken one: the same answers ``LocalUIMonitor`` gives for
    a screen with no region drawn, so the caller takes the branch it already
    has (refuse, tell the user, fall back to a manual paste)."""
    monitor = SwitchableMonitor()

    assert not monitor.attached
    assert monitor.latest is None
    assert monitor.generation == 0
    assert monitor.clipboard_kind is None
    assert await monitor.focus_window(7) is False
    assert await monitor.foreground_window() is None
    assert await monitor.click(REGION) is False
    assert await monitor.move_cursor(3, 4) is False
    assert await monitor.scroll(REGION, 3) is False
    assert await monitor.scroll_key("pagedown", 2) is False
    assert await monitor.send_paste() is False
    assert await monitor.send_enter() is False
    assert await monitor.read_clipboard() is None
    assert await monitor.find_all(TemplateKind.COPY) == ()
    assert (await monitor.locate(TemplateKind.COPY)).region is None
    assert await monitor.click_element(TemplateKind.COPY) is ElementClick.MISMATCH
    assert (await monitor.hover_scan(TemplateKind.COPY)).region is None
    assert await monitor.snap_to_bottom("wheel") is None
    assert monitor.watch_clipboard(True) is False


async def test_a_write_with_no_monitor_raises_the_fallbacks_own_exception() -> None:
    """The one verb that may not shrug. A write that quietly returned would
    leave the brain believing an outbound is on a clipboard that does not
    exist; ``ClipboardUnavailable`` is what the delivery path already catches
    to ask the user to paste it themselves."""
    with pytest.raises(ClipboardUnavailable):
        await SwitchableMonitor().write_clipboard("payload")


async def test_the_spec_is_remembered_even_with_nothing_attached() -> None:
    """``configure`` before the first dial is not lost: this handle keeps the
    last spec it was given, so a caller can read back what the local side asked
    for while the link is still coming up."""
    monitor = SwitchableMonitor()
    assert await monitor.configure(spec()) == 0  # an idle inner has no run to stamp
    assert monitor.spec is not None
    assert monitor.spec.service == "svc"


async def test_watch_forwards_and_an_idle_inner_answers_the_empty_service() -> None:
    """§10.5: ``watch`` is the brain's only retarget, and a handle attached to
    nothing answers it the way an uncalibrated monitor does - no service, no
    box, no profile - rather than raising at a brain that has not dialled yet."""
    monitor = SwitchableMonitor()
    assert await monitor.watch(AgentSlot.MASTER) == EMPTY_WATCHED

    inner = FakeUIMonitor()
    inner.specs_for[AgentSlot.SUBAGENT] = spec(service="zai")
    monitor.swap(inner)

    watched = await monitor.watch(AgentSlot.SUBAGENT)
    assert inner.watches == [AgentSlot.SUBAGENT]
    assert watched.service == "zai"
    # Nothing is cached here: what ``watch`` settled on is the INNER monitor's
    # own configuration, and a copy on this side would be a second answer.
    assert monitor.spec is None


# == the swap =================================================================


async def test_a_subscriber_registered_before_the_link_hears_the_first_tick() -> None:
    """The whole point: the automation subscribes once, in its constructor,
    long before any monitor exists. A handle that made it re-register after
    each reconnect would have a window in which ticks reach nobody."""
    monitor = SwitchableMonitor()
    seen: list[Tick] = []
    clips: list[str] = []
    monitor.subscribe(seen.append)
    monitor.on_clip(clips.append)

    inner = FakeUIMonitor()
    monitor.swap(inner)
    inner.feed(inner.make_tick())
    inner.push_clip("a reply")

    assert [tick.seq for tick in seen] == [1]
    assert clips == ["a reply"]


async def test_the_replaced_monitor_goes_quiet_and_is_handed_back() -> None:
    """A swap detaches before it attaches, so nothing from the monitor that was
    current can arrive afterwards - and the old one is RETURNED rather than
    closed here, because only the caller knows whether the swap is a reconnect
    (it is already dead) or a deliberate retarget."""
    first, second = FakeUIMonitor(), FakeUIMonitor()
    monitor = SwitchableMonitor(first)
    seen: list[int] = []
    monitor.subscribe(lambda tick: seen.append(tick.seq))

    previous = monitor.swap(second)

    assert previous is first
    first.feed(first.make_tick(seq=99))
    second.feed(second.make_tick(seq=1))
    assert seen == [1]


async def test_an_observe_parked_with_nothing_attached_takes_the_next_links_tick() -> None:
    """§2.9 in one method: a recipe parked on ``observe`` when the link dropped
    does not have to know it dropped. The wait is answered by the first tick the
    monitor that replaces it pushes."""
    monitor = SwitchableMonitor()
    waiting = asyncio.ensure_future(monitor.observe())
    await settle()
    assert not waiting.done()

    inner = FakeUIMonitor()
    monitor.swap(inner)
    inner.feed(inner.make_tick(seq=4))
    await settle()

    assert (await waiting).seq == 4


async def test_observe_still_waits_for_the_NEXT_tick_after_a_swap() -> None:
    """The rule ``observe`` keeps everywhere (never the cached one) survives the
    swap: a monitor that arrives already holding a tick does not answer a wait
    made after it - the bug that rule exists to stop is reading a tick from
    before the action just performed, and a reconnect is no exception."""
    inner = FakeUIMonitor()
    inner.feed(inner.make_tick(seq=1))
    monitor = SwitchableMonitor()
    monitor.swap(inner)

    waiting = asyncio.ensure_future(monitor.observe())
    await settle()
    assert not waiting.done()

    inner.feed(inner.make_tick(seq=2))
    await settle()
    assert (await waiting).seq == 2


async def test_every_verb_reaches_whichever_monitor_is_current() -> None:
    """Forwarding, asserted once over the verbs a recipe actually spends: the
    call lands on the monitor that is current NOW, not on the one the handle was
    built over."""
    first, second = FakeUIMonitor(), FakeUIMonitor()
    monitor = SwitchableMonitor(first)
    await monitor.configure(spec())
    monitor.swap(second)

    second.answers["click_element"] = ElementClick.CLICKED
    await monitor.configure(spec(service="other"))
    assert await monitor.click_element(TemplateKind.COPY) is ElementClick.CLICKED
    await monitor.snap_to_bottom("end")
    await monitor.suspend()
    await monitor.resume()

    assert [name for name, _ in second.calls] == [
        "click_element",
        "snap_to_bottom",
        "suspend",
        "resume",
    ]
    # ...and the first one saw only what happened while it was current.
    assert [name for name, _ in first.calls] == []
    assert second.spec is not None and second.spec.service == "other"
    # The BRAIN's spec is the handle's own, so it survives a swap intact.
    assert monitor.spec is not None and monitor.spec.service == "other"


async def test_close_ends_the_current_monitor_and_stops_forwarding() -> None:
    monitor = SwitchableMonitor()
    inner = FakeUIMonitor()
    monitor.swap(inner)
    seen: list[int] = []
    monitor.subscribe(lambda tick: seen.append(tick.seq))

    await monitor.close()

    assert inner.closed
    inner.feed(inner.make_tick())
    assert seen == []


# == the local-only tier (§3) =================================================


async def test_the_local_only_tier_is_the_empty_answer_over_a_remote_monitor() -> None:
    """A ``RemoteUIMonitor`` has no detector, no trackers and no frame hook -
    those are objects on the machine the pixels are on. The handle answers for
    them rather than raising, because the controller's ``MonitorLike`` asks and
    a shell's chrome mirrors what it gets.
    """
    monitor = SwitchableMonitor()
    assert isinstance(monitor.inner, IdleMonitor)
    assert monitor.detector is None
    assert monitor.busy_tracker is None
    assert monitor.idle_tracker is None
    assert monitor.stale_tracker is None
    assert monitor.self_writes is not None
    monitor.reset_trackers()  # a no-op, not an AttributeError


async def test_the_local_only_tier_is_forwarded_when_the_inner_has_one() -> None:
    """Local mode is the other half of the same handle: swap a monitor that DOES
    hold trackers in and the chrome reads them through here unchanged."""
    inner = FakeUIMonitor()
    inner.stale_tracker = StaleTracker(REGION, required_ticks=2)
    monitor = SwitchableMonitor(inner)

    assert monitor.stale_tracker is inner.stale_tracker
    assert monitor.self_writes is inner.self_writes
