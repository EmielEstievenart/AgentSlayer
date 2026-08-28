"""One ``UIMonitor`` handle that outlives the monitors behind it.

docs/design/ui-monitor.md §2.9. A split-mode brain is built long before it has
a link and keeps running after it loses one: the window opens, the controller
is constructed, the chrome paints, and only *then* does the first dial go out -
and every disconnect after that is followed by a redial that produces a brand
new :class:`~agentclip.driver.monitor.remote.RemoteUIMonitor`. The controller
must not be rebuilt for either event, so what it is handed is this: a handle
that forwards every member to whichever monitor is current and can have that
monitor replaced under it (:meth:`SwitchableMonitor.swap`).

Three things make it more than a ``getattr`` proxy:

* **It starts INERT, not empty.** The first inner is :class:`IdleMonitor`, whose
  every action is the "nothing happened" answer the real monitors give for a
  machine with no region drawn - ``False``, ``None``, ``()``, ``MISMATCH``. A
  brain that acts before the link is up therefore takes the branch it would take
  against an uncalibrated screen (refuse, tell the user, fall back to a manual
  paste) instead of crashing on a ``None`` monitor.
* **Hooks are ours, not the inner's.** Subscribers register HERE and stay
  registered across every swap; exactly one forwarding hook is installed on the
  inner and moved with it. A subscriber that had to re-register after each
  reconnect would be a subscriber with a window in which ticks reach nobody.
* **:meth:`observe` is answered from our own hook**, so a wait that was parked
  while nothing was attached is resolved by the first tick the NEXT monitor
  pushes. That is the whole point of the class in one method: the recipe parked
  on ``observe`` when the link dropped does not have to know it dropped.

The local-only tier (§3 - the trackers, the frame hook, the self-write set) is
forwarded when the inner has one and answered with nothing when it does not,
which is the honest answer for a remote monitor: those objects live on the
machine the pixels are on and were never going to cross the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.clip.watcher import SelfWriteSet
from agentclip.driver.monitor.protocol import (
    EMPTY_WATCHED,
    ClipHook,
    ElementClick,
    Located,
    MonitorSpec,
    Tick,
    TickHook,
    UIMonitor,
    Watched,
)
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import ScreenDetector, Sighting
from agentclip.driver.screen.presence import PresenceTracker
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.stale import StaleTracker

_log = logging.getLogger(__name__)

FrameHook = Callable[[RegionImage, Mapping[TemplateKind, Sighting | None]], None]


class IdleMonitor:
    """A ``UIMonitor`` attached to nothing at all.

    Every verb answers the way the real monitors answer for a machine with no
    region and no profile: the action did not land, the search found nothing,
    the clipboard is not there. Deliberately not "raise": "there is nothing to
    look at" and "it is not on screen" are the same fact to a caller that may
    not click either way (protocol.py's own rule for the pixel verdicts), and a
    brain that has not dialled yet is in exactly that position.

    :meth:`observe` is the one member with no answer to give - a tick can only
    come from a monitor - so it parks forever. In practice nothing ever waits
    on THIS one: :class:`SwitchableMonitor` answers ``observe`` from its own
    hook, so the wait is resolved by the first tick after a swap.
    """

    @property
    def spec(self) -> MonitorSpec | None:
        return None

    async def configure(self, spec: MonitorSpec) -> int:
        """Accepted and dropped. The generation stays 0: nothing has been
        watched, so there is no run for a tick to be a ghost of."""
        return 0

    async def watch(self, slot: AgentSlot) -> Watched:
        """The empty answer, exactly as an uncalibrated monitor gives it: no
        service, no box, no profile. A brain that has not dialled yet reads
        "there is nothing over there" off the same fields it would read a
        monitor with an empty service table off."""
        return EMPTY_WATCHED

    async def watched(self) -> Watched:
        return EMPTY_WATCHED

    async def suspend(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def set_theme(self, theme: str) -> None:
        """Heard and dropped. There is no window on the other end of nothing."""
        return None

    @property
    def generation(self) -> int:
        return 0

    @property
    def latest(self) -> Tick | None:
        return None

    async def observe(self) -> Tick:
        await asyncio.get_running_loop().create_future()
        raise AssertionError("unreachable: an idle monitor never produces a tick")

    def subscribe(self, hook: TickHook) -> Callable[[], None]:
        return _no_unsubscribe

    def on_clip(self, hook: ClipHook) -> Callable[[], None]:
        return _no_unsubscribe

    async def focus_window(self, handle: int) -> bool:
        return False

    async def foreground_window(self) -> int | None:
        return None

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        return False

    async def move_cursor(self, x: int, y: int) -> bool:
        return False

    async def scroll(self, region: ScreenRegion, detents: int) -> bool:
        return False

    async def scroll_key(self, key: str, taps: int = 1) -> bool:
        return False

    async def send_paste(self) -> bool:
        return False

    async def send_enter(self) -> bool:
        return False

    async def read_clipboard(self) -> str | None:
        return None

    async def write_clipboard(self, text: str) -> None:
        """The one verb that RAISES rather than shrugging.

        ``ClipboardUnavailable`` is what the delivery path catches to fall back
        to "copy this yourself" (``driver/clip/base.py``), and a write that
        quietly returned would leave the brain believing an outbound is on a
        clipboard that does not exist - the one failure the manual fallback was
        built for.
        """
        raise ClipboardUnavailable("no monitor is attached")

    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]:
        return ()

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        return Located(None, False, None)

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        return ElementClick.MISMATCH

    async def hover_scan(self, kind: TemplateKind) -> Located:
        return Located(None, False, None)

    async def snap_to_bottom(self, action: str) -> None:
        return None

    def watch_clipboard(self, on: bool) -> bool:
        return False

    @property
    def clipboard_kind(self) -> str | None:
        return None


def _no_unsubscribe() -> None:
    """The unsubscribe for a hook that was never going to be called."""


class _Waiter:
    """One parked :meth:`SwitchableMonitor.observe` and the loop to answer it on.

    The loop is captured at call time because a tick may arrive on a poll
    THREAD (a local inner) or on a reader task (a remote one), and
    ``call_soon_threadsafe`` is the one door that is right either way -
    ``LocalUIMonitor``'s own waiter keeps the same field for the same reason.
    """

    __slots__ = ("future", "loop")

    def __init__(self, loop: asyncio.AbstractEventLoop, future: asyncio.Future[Tick]) -> None:
        self.loop = loop
        self.future = future


class SwitchableMonitor:
    """A ``UIMonitor`` whose implementation can be replaced under its callers."""

    def __init__(self, inner: UIMonitor | None = None) -> None:
        self._inner: UIMonitor = inner if inner is not None else IdleMonitor()
        self._drops: list[Callable[[], None]] = []
        self._hooks: list[TickHook] = []
        self._clip_hooks: list[ClipHook] = []
        self._frame_hooks: list[FrameHook] = []
        self._waiters: list[_Waiter] = []
        # The spec the BRAIN last asked for, which is the one that matters: it
        # is what a reconnect re-sends, and reading it back off an inner that
        # has just been replaced would be reading it off a monitor that has not
        # been told anything yet.
        self._spec: MonitorSpec | None = None
        self._self_writes = SelfWriteSet()
        self._attach()

    # == the swap ==============================================================

    @property
    def inner(self) -> UIMonitor:
        """Whichever monitor is current - what a status line asks about."""
        return self._inner

    @property
    def attached(self) -> bool:
        """Is there a real monitor behind this handle right now?"""
        return not isinstance(self._inner, IdleMonitor)

    def swap(self, inner: UIMonitor) -> UIMonitor:
        """Point everything at ``inner`` and hand the previous one back.

        Synchronous on purpose - it is called from a disconnect hook and from a
        dial's continuation, neither of which may block - so nothing here closes
        anything: the monitor that was current is RETURNED, and closing it (or
        deciding it is already dead) is the caller's, which is the only side
        that knows whether the swap is a reconnect or a retarget.

        The hooks move first and the pointer second, so no tick from the new
        monitor can arrive while the old one's forwarding hook is still the one
        installed.
        """
        previous = self._inner
        self._detach()
        self._inner = inner
        self._attach()
        return previous

    def _attach(self) -> None:
        """Install exactly one forwarding hook of each kind on the inner."""
        self._drops = [
            self._inner.subscribe(self._tick_arrived),
            self._inner.on_clip(self._clip_arrived),
        ]
        on_frame = getattr(self._inner, "on_frame", None)
        if callable(on_frame):
            self._drops.append(on_frame(self._frame_arrived))

    def _detach(self) -> None:
        drops, self._drops = self._drops, []
        for drop in drops:
            try:
                drop()
            except Exception:  # noqa: BLE001 - a monitor that is already gone
                _log.debug("unsubscribing from a replaced monitor failed", exc_info=True)

    # == lifecycle / configuration =============================================

    @property
    def spec(self) -> MonitorSpec | None:
        return self._spec

    async def configure(self, spec: MonitorSpec) -> int:
        self._spec = spec
        return await self._inner.configure(spec)

    async def watch(self, slot: AgentSlot) -> Watched:
        """Straight through. Nothing is remembered here, deliberately: what
        ``watch`` settles on is the INNER monitor's own configuration (§10.5),
        and a copy kept on this side would be a second answer to the question
        this handle exists to forward."""
        return await self._inner.watch(slot)

    async def watched(self) -> Watched:
        return await self._inner.watched()

    async def suspend(self) -> None:
        await self._inner.suspend()

    async def set_theme(self, theme: str) -> None:
        """Straight through, and nothing remembered.

        Unlike ``spec``, which this handle keeps because a reconnect has to
        re-send it: a theme is re-sent by the DIAL itself (it rides in the
        ``hello``, remote.py), so a copy here would be a second answer to a
        question the handshake already answers.
        """
        await self._inner.set_theme(theme)

    async def resume(self) -> None:
        await self._inner.resume()

    async def close(self) -> None:
        """Close whatever is current, and stop forwarding. Idempotent.

        The handle itself is not "closed": a caller that closes and then swaps
        gets a working handle again, which is what a window that redials after a
        deliberate detach would need. What is guaranteed is that nothing is
        double-closed here - a monitor already swapped out was handed back by
        :meth:`swap` and is that caller's to close.
        """
        self._detach()
        await self._inner.close()

    # == observation ===========================================================

    @property
    def generation(self) -> int:
        return self._inner.generation

    @property
    def latest(self) -> Tick | None:
        return self._inner.latest

    async def observe(self) -> Tick:
        """The next tick from whichever monitor delivers one - swaps included."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Tick] = loop.create_future()
        self._waiters.append(_Waiter(loop, future))
        return await future

    def subscribe(self, hook: TickHook) -> Callable[[], None]:
        self._hooks.append(hook)

        def unsubscribe() -> None:
            if hook in self._hooks:
                self._hooks.remove(hook)

        return unsubscribe

    def on_clip(self, hook: ClipHook) -> Callable[[], None]:
        self._clip_hooks.append(hook)

        def unsubscribe() -> None:
            if hook in self._clip_hooks:
                self._clip_hooks.remove(hook)

        return unsubscribe

    def on_frame(self, hook: FrameHook) -> Callable[[], None]:
        """The local-only frame hook (§2.2), forwarded when there is one.

        Registered here whether or not the current inner can produce frames, so
        a swap from a remote monitor to a local one starts feeding the ELEMENTS
        surface without anybody re-subscribing.
        """
        self._frame_hooks.append(hook)

        def unsubscribe() -> None:
            if hook in self._frame_hooks:
                self._frame_hooks.remove(hook)

        return unsubscribe

    def _tick_arrived(self, tick: Tick) -> None:
        # A notice announces a run with nothing to poll (§11.10) - no frame was
        # captured, so it relays to the subscribers (the generation on it is why
        # it exists) and leaves the parked waits alone: an ``observe`` here is
        # waiting for the first tick a monitor actually takes.
        if not tick.notice:
            waiters, self._waiters = self._waiters, []
            for waiter in waiters:
                _resolve(waiter, tick)
        for hook in tuple(self._hooks):
            _safely(hook, tick)

    def _clip_arrived(self, text: str) -> None:
        for hook in tuple(self._clip_hooks):
            _safely(hook, text)

    def _frame_arrived(
        self, scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
    ) -> None:
        for hook in tuple(self._frame_hooks):
            _safely(hook, scene, sightings)

    # == actions ===============================================================

    async def focus_window(self, handle: int) -> bool:
        return await self._inner.focus_window(handle)

    async def foreground_window(self) -> int | None:
        return await self._inner.foreground_window()

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        return await self._inner.click(region, settle_s=settle_s)

    async def move_cursor(self, x: int, y: int) -> bool:
        return await self._inner.move_cursor(x, y)

    async def scroll(self, region: ScreenRegion, detents: int) -> bool:
        return await self._inner.scroll(region, detents)

    async def scroll_key(self, key: str, taps: int = 1) -> bool:
        return await self._inner.scroll_key(key, taps)

    async def send_paste(self) -> bool:
        return await self._inner.send_paste()

    async def send_enter(self) -> bool:
        return await self._inner.send_enter()

    async def read_clipboard(self) -> str | None:
        return await self._inner.read_clipboard()

    async def write_clipboard(self, text: str) -> None:
        await self._inner.write_clipboard(text)

    # -- the pixel verdicts ----------------------------------------------------

    async def find_all(self, kind: TemplateKind) -> tuple[ScreenRegion, ...]:
        return await self._inner.find_all(kind)

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        return await self._inner.locate(kind, exclude_kinds=exclude_kinds)

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        return await self._inner.click_element(kind, settle_s=settle_s)

    async def hover_scan(self, kind: TemplateKind) -> Located:
        return await self._inner.hover_scan(kind)

    async def snap_to_bottom(self, action: str) -> None:
        await self._inner.snap_to_bottom(action)

    # == the clipboard watcher =================================================

    def watch_clipboard(self, on: bool) -> bool:
        return self._inner.watch_clipboard(on)

    @property
    def clipboard_kind(self) -> str | None:
        return self._inner.clipboard_kind

    # == the local-only tier (§3) ==============================================
    # Present because the controller's ``MonitorLike`` asks for it and a shell's
    # chrome mirrors it. Forwarded when the inner has it; the empty answer when
    # it does not, which is every remote monitor: the trackers and the detector
    # are objects on the machine the pixels are on.

    @property
    def detector(self) -> ScreenDetector | None:
        return _tier(self._inner, "detector", ScreenDetector)

    @property
    def busy_tracker(self) -> PresenceTracker | None:
        return _tier(self._inner, "busy_tracker", PresenceTracker)

    @property
    def idle_tracker(self) -> PresenceTracker | None:
        return _tier(self._inner, "idle_tracker", PresenceTracker)

    @property
    def stale_tracker(self) -> StaleTracker | None:
        return _tier(self._inner, "stale_tracker", StaleTracker)

    @property
    def self_writes(self) -> SelfWriteSet:
        """The register that stops the watcher reading our own writes back.

        The inner's when it has one, because the write and the tagging must
        happen on the same side of the seam (§2.11) - which for a remote monitor
        is the far side, and the set kept here is then simply never consulted.
        """
        register = getattr(self._inner, "self_writes", None)
        return register if isinstance(register, SelfWriteSet) else self._self_writes

    def reset_trackers(self) -> None:
        """Make the inner's trackers forget their frames, if it has any.

        A no-op against a remote monitor in this phase: the reset is not a wire
        verb, so a split-mode brain cannot ask for one. The consequence is
        bounded - the debounce keeps a streak the caller's own paste produced
        for one poll longer - and it is written down here rather than hidden
        because closing it is a wire change, not a shell one.
        """
        reset = getattr(self._inner, "reset_trackers", None)
        if callable(reset):
            reset()


def _tier(inner: UIMonitor, name: str, kind: type[Any]) -> Any:
    """One local-only member off ``inner``, or None when it has none."""
    value = getattr(inner, name, None)
    return value if isinstance(value, kind) else None


def _resolve(waiter: _Waiter, tick: Tick) -> None:
    """Answer one parked ``observe`` on the loop it was made on."""

    def deliver() -> None:
        if not waiter.future.done():
            waiter.future.set_result(tick)

    # RuntimeError: the loop closed under us mid-teardown, and a waiter on a
    # dead loop is a wait nobody is left to notice.
    with contextlib.suppress(RuntimeError):
        waiter.loop.call_soon_threadsafe(deliver)


def _safely(hook: Callable[..., None], *args: Any) -> None:
    try:
        hook(*args)
    except Exception:  # noqa: BLE001 - one bad subscriber, not one dead handle
        _log.exception("a monitor subscriber raised")


def _conforms(monitor: SwitchableMonitor, idle: IdleMonitor) -> tuple[UIMonitor, UIMonitor]:
    """Structural pin: mypy fails HERE if either class drifts from the Protocol.

    The same pin ``local.py``, ``remote.py`` and ``fake.py`` carry, for the same
    reason: the tests are not type-checked, and a Protocol nothing declares is a
    Protocol nothing enforces. Both are pinned because the inert one is what a
    split-mode brain runs on until its first dial lands.
    """
    return monitor, idle
