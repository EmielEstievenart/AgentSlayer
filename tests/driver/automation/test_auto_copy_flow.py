"""The auto-copy harvest, with no terminal, no browser and no mouse.

Since slice 6 the OS-acting sequences are the AutomationController's: the
harvest that fires when the detectors agree a reply finished, the find-then-click
primitive under it, the hover-scan fallback, and the two calls that move the
automation between browser windows. This is where their RULES are asserted; the
Pilot suites in ``tests/shell/tui`` stay as the wiring check that the real screen is
still plugged into them.

Two seams make that possible and neither is the paint port. The machine is
reached through :class:`~agentclip.driver.automation.ops.ScreenOps`, which is
``agentclip.driver.screen`` behind one object - so the fixture below patches
``agentclip.driver.automation.ops``' own names, which is the use site the default
implementation calls through. Everything the sequences still have to ASK a shell
is :class:`~agentclip.driver.automation.host.AutomationHost`, and ``FakeHost`` is a
scripted one: what the live service looks like, where its appearances are, and
whether the copy click took.

The beats are shrunk at their own use site for the same reason: three snap
rounds at the real settle is over a second of a test doing nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

import agentclip.driver.automation.controller as controller_mod
import agentclip.driver.automation.ops as ops_mod
from agentclip.config import ServicePreset
from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.template import RegionMatch, Template

from .conftest import FakeAutomationView

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
ICON = (24, 24)
# Where the stubbed search "finds" the icon: region-local, so the click target
# is CHAT_REGION's origin plus this.
MATCH = RegionMatch(x=120, y=300, diff=0.03)
MATCH_RECT = ScreenRegion(CHAT_REGION.left + MATCH.x, CHAT_REGION.top + MATCH.y, *ICON)
# What is actually clicked: ONE pixel of that rectangle, the middle of it while
# the service has not moved its click point (screen.profile).
CLICK_TARGET = click_point_region(MATCH_RECT, 50, 50)
OUR_WINDOW = 4242


def _image(width: int, height: int) -> RegionImage:
    """A capture varied enough for ``Template.build`` to anchor on."""
    size = width * height * 4
    return RegionImage(width, height, (bytes(range(256)) * (size // 256 + 1))[:size])


class _Machine:
    """Every OS call the sequences made, in the order they made it."""

    def __init__(self) -> None:
        self.clicks: list[tuple[ScreenRegion, float | None]] = []
        self.moves: list[tuple[int, int]] = []
        self.scrolls: list[tuple[ScreenRegion, int]] = []
        self.keys: list[tuple[str, int]] = []
        self.focuses: list[int] = []
        self.captures: list[ScreenRegion] = []
        self.order: list[str] = []
        # What the next search should answer, popped one look at a time; the
        # last entry repeats for ever, so a test scripts only what it cares
        # about. ``None`` in the first slot is a miss.
        self.looks: list[tuple[RegionMatch | None, float | None]] = [(None, 0.21)]
        self.capture_fails = False

    def nothing(self) -> bool:
        return not (self.clicks or self.moves or self.scrolls or self.keys or self.focuses)


class FakeHost:
    """A scripted ``AutomationHost``: what the shell would answer, as data."""

    def __init__(self, preset: ServicePreset, profile: ServiceProfile) -> None:
        self.preset = preset
        self.profile = profile
        # What ``find_all`` answers, per kind. Absent = nothing on screen, which
        # is what sends ``chatbox_region`` to its whole-window fallback.
        self.on_screen: dict[TemplateKind, list[ScreenRegion]] = {}
        self.click_takes = True
        self.copy_clicks: list[ScreenRegion] = []
        self.harvests = 0
        self.rebuilds = 0
        self.seen_note = ""

    def live_preset(self) -> ServicePreset:
        return self.preset

    def profile_for(self, slot: AgentSlot) -> ServiceProfile:
        return self.profile

    async def find_all(
        self,
        kind: TemplateKind,
        slot: AgentSlot | None = None,
        *,
        scene: RegionImage | None = None,
    ) -> list[ScreenRegion]:
        return list(self.on_screen.get(kind, []))

    async def verified_copy_click(self, target: ScreenRegion) -> bool:
        self.copy_clicks.append(target)
        return self.click_takes

    async def ingest_harvest(self) -> None:
        self.harvests += 1

    def copy_seen_note(self) -> str:
        return self.seen_note

    def rebuild_detectors(self) -> None:
        self.rebuilds += 1


def _preset(**overrides: Any) -> ServicePreset:
    base = ServicePreset(
        key="fake", label="Fake", max_paste_chars=10_000, total_context_chars=100_000
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Machine]:
    """Record (never perform) every primitive ``ScreenOps`` reaches for.

    Patched on ``agentclip.driver.automation.ops`` - the module that imported them from
    ``agentclip.driver.screen`` - because that is the use site the default
    implementation calls through, the same discipline the Pilot suites follow one
    layer up.
    """
    rec = _Machine()

    def capture(region: ScreenRegion) -> RegionImage:
        rec.captures.append(region)
        if rec.capture_fails:
            raise CaptureError("no display")
        return _image(region.width, region.height)

    def click(region: ScreenRegion, settle_s: float = 0.0) -> bool:
        rec.clicks.append((region, settle_s))
        rec.order.append("click")
        return True

    def move(x: int, y: int) -> bool:
        rec.moves.append((x, y))
        rec.order.append("move")
        return True

    def scroll(region: ScreenRegion, detents: int) -> bool:
        rec.scrolls.append((region, detents))
        rec.order.append("scroll")
        return True

    def scroll_key(key: str, taps: int = 1) -> bool:
        rec.keys.append((key, taps))
        rec.order.append("keys")
        return True

    def focus(handle: int) -> bool:
        rec.focuses.append(handle)
        rec.order.append("focus")
        return True

    def look(template: Template, scene: RegionImage, **kw: object) -> object:
        return rec.looks.pop(0) if len(rec.looks) > 1 else rec.looks[0]

    monkeypatch.setattr(ops_mod, "capture_region", capture)
    monkeypatch.setattr(ops_mod, "click_region", click)
    monkeypatch.setattr(ops_mod, "move_cursor", move)
    monkeypatch.setattr(ops_mod, "scroll_region", scroll)
    monkeypatch.setattr(ops_mod, "send_scroll_key", scroll_key)
    monkeypatch.setattr(ops_mod, "focus_window_verified", focus)
    monkeypatch.setattr(ops_mod, "find_lowest_with_best_miss", look)
    monkeypatch.setattr(ops_mod, "STEP_DELAY_S", 0.0)
    # The harvest's own beats: three snap rounds at the real settle is over a
    # second of a test waiting for a page that does not exist.
    monkeypatch.setattr(controller_mod, "SNAP_SETTLE_S", 0.0)
    yield rec


@pytest.fixture
def host() -> FakeHost:
    profile = ServiceProfile(key="fake")
    profile.put(TemplateKind.COPY, _image(*ICON))
    return FakeHost(_preset(), profile)


@pytest.fixture
def flow(
    view: FakeAutomationView, host: FakeHost, machine: _Machine
) -> AutomationController:
    """A controller with a drawn chat window and a captured copy button - the
    state the finish decision fires the harvest out of."""
    automation = AutomationController(view=view, host=host, clipboard=FakeClipboard())
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    automation.set_own_window(OUR_WINDOW)
    return automation


# -- the happy path --------------------------------------------------------------


async def test_the_harvest_snaps_hunts_clicks_and_comes_home(
    flow: AutomationController, host: FakeHost, machine: _Machine, view: FakeAutomationView
) -> None:
    """One round finds the icon: focus the chat, park the pointer, snap, search,
    click the rectangle the match translates back to - then hand focus back to
    the tool and let the shell ingest what was copied."""
    machine.looks = [(MATCH, None)]

    await flow.auto_copy_flow()

    # No chat box is calibrated, so the focus click lands on the drawn window.
    assert [region for region, _settle in machine.clicks] == [CHAT_REGION]
    assert machine.moves == [CHAT_REGION.center]
    assert machine.scrolls == [(CHAT_REGION, controller_mod.SNAP_WHEEL_DETENTS)]
    assert host.copy_clicks == [CLICK_TARGET]
    # The click first, focus strictly after it: a snap-back that overtook the
    # click would take the copy away from the window it was aimed at.
    assert machine.focuses == [OUR_WINDOW]
    assert machine.order[-1] == "focus"
    assert host.harvests == 1
    # And the readout says what happened, with the captured size in front of it.
    assert (TemplateKind.COPY, "24×24 · clicked (diff 0.03)") in view.detection_lines
    assert flow.loop_state is not LoopState.MANUAL_COPY


async def test_the_snap_back_needs_a_handle_to_snap_to(
    view: FakeAutomationView, host: FakeHost, machine: _Machine
) -> None:
    """``set_own_window`` is the only source of that handle. Without one there is
    no window to come home to, and the harvest simply leaves the browser
    focused rather than guessing at one."""
    machine.looks = [(MATCH, None)]
    flow = AutomationController(view=view, host=host, clipboard=FakeClipboard())
    flow.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    flow.set_own_window(None)  # a reading that failed keeps nothing

    await flow.auto_copy_flow()

    assert flow.own_window is None
    assert machine.focuses == []
    assert host.harvests == 1  # the harvest still happened


# -- when the hunt comes up empty -------------------------------------------------


async def test_a_miss_re_snaps_and_then_hands_the_harvest_over(
    flow: AutomationController, host: FakeHost, machine: _Machine, view: FakeAutomationView
) -> None:
    """A page that had not finished arriving is the commonest reason the icon is
    not on the frame, so one miss re-scrolls rather than giving up - and only
    after the last round does the harvest become the user's."""
    host.seen_note = "; the poller last saw one 3s ago"

    await flow.auto_copy_flow()

    assert len(machine.scrolls) == controller_mod.COPY_SNAP_ROUNDS
    # ...and the choreography in front of the snap happened once: nothing
    # between rounds touches the mouse or the focus.
    assert len(machine.clicks) == 1
    assert machine.moves == [CHAT_REGION.center]
    assert host.copy_clicks == []  # nothing found means nothing clicked
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert view.logged("copy button not found after 3 snaps")
    assert view.logged("the poller last saw one 3s ago")  # the shell's own note


async def test_the_click_that_lands_on_a_second_round_stops_the_hunt(
    flow: AutomationController, machine: _Machine, host: FakeHost
) -> None:
    """The rounds are a retry budget, not a schedule."""
    machine.looks = [(None, 0.30), (MATCH, None)]

    await flow.auto_copy_flow()

    assert len(machine.scrolls) == 2
    assert host.copy_clicks == [CLICK_TARGET]


async def test_a_failed_capture_of_the_chat_region_is_reported(
    flow: AutomationController, machine: _Machine, view: FakeAutomationView
) -> None:
    """There is nothing to search in, so the harvest says so and stops."""
    machine.capture_fails = True

    await flow.auto_copy_flow()

    assert flow.loop_state is LoopState.MANUAL_COPY
    assert (TemplateKind.COPY, "24×24 · capture failed") in view.detection_lines
    assert any("could not capture" in text for text, _severity in view.notifications)


async def test_nothing_to_search_never_touches_the_machine(
    view: FakeAutomationView, host: FakeHost, machine: _Machine
) -> None:
    """No drawn window means nowhere to look - and the refusal happens before a
    single click, scroll or cursor move."""
    flow = AutomationController(view=view, host=host, clipboard=FakeClipboard())

    await flow.auto_copy_flow()

    assert machine.nothing()
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert view.logged("no chat window is drawn")


# -- the hover scan ---------------------------------------------------------------


async def test_the_hover_scan_runs_after_the_last_static_miss(
    view: FakeAutomationView, machine: _Machine, host: FakeHost
) -> None:
    """A chat that only paints the copy icon under the pointer: every static
    round misses, and then - once, at the end - the cursor climbs the region and
    stops at the first frame the icon appears in."""
    host.preset = _preset(hover_scan=True)
    flow = AutomationController(view=view, host=host, clipboard=FakeClipboard())
    flow.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    rounds = controller_mod.COPY_SNAP_ROUNDS
    machine.looks = [(None, 0.21)] * (rounds + 1) + [(MATCH, None)]

    await flow.auto_copy_flow()

    # The park in front of the snap, then one stop per look the scan took.
    assert machine.moves[0] == CHAT_REGION.center
    assert len(machine.moves) == 3  # park + two hover stops
    assert host.copy_clicks == [CLICK_TARGET]
    assert (TemplateKind.COPY, "24×24 · hover-scanning") in view.detection_lines


async def test_a_static_hit_never_starts_a_scan(
    view: FakeAutomationView, machine: _Machine, host: FakeHost
) -> None:
    """The cheap path stays cheap: the only cursor move is the pre-snap park."""
    host.preset = _preset(hover_scan=True)
    flow = AutomationController(view=view, host=host, clipboard=FakeClipboard())
    flow.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    machine.looks = [(MATCH, None)]

    await flow.auto_copy_flow()

    assert machine.moves == [CHAT_REGION.center]


async def test_the_scan_is_opt_in_per_service(
    flow: AutomationController, machine: _Machine, view: FakeAutomationView
) -> None:
    """With ``hover_scan`` off - the default - a static miss is simply a miss,
    and the user's cursor is never walked up the transcript."""
    await flow.auto_copy_flow()

    assert machine.moves == [CHAT_REGION.center]
    assert not any("hover" in text for _kind, text in view.detection_lines)


# -- a click that does not take ---------------------------------------------------


async def test_a_copy_click_that_never_takes_hands_it_back(
    flow: AutomationController, host: FakeHost, machine: _Machine, view: FakeAutomationView
) -> None:
    """The icon was found and clicked and the clipboard never changed. The
    browser keeps focus deliberately - that is where the user has to finish the
    job - so there is no snap-back at all."""
    machine.looks = [(MATCH, None)]
    host.click_takes = False

    await flow.auto_copy_flow()

    assert host.copy_clicks == [CLICK_TARGET]
    assert machine.focuses == []  # no snap-back on failure
    assert host.harvests == 0
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert (TemplateKind.COPY, "24×24 · click did not take") in view.detection_lines


# -- the suspension bracket -------------------------------------------------------


async def test_the_suspension_lifts_however_the_harvest_ends(
    flow: AutomationController, machine: _Machine
) -> None:
    """``flow_running`` suspends the finish evaluation, and only the bracket's
    ``finally`` lifts it - so a harvest that raises must not wedge detection
    shut for the rest of the session."""
    flow.flow_running = True

    async def explode() -> None:
        raise RuntimeError("the browser vanished")

    with pytest.raises(RuntimeError):
        await flow.run_auto_copy_flow(explode)

    assert flow.flow_running is False


async def test_the_bracket_runs_the_body_the_shell_handed_in(
    flow: AutomationController
) -> None:
    """The shell's seam, not this object's own harvest: the Textual side keeps a
    stubbable ``_auto_copy_flow`` and hands it down, which is why the bracket
    takes a body at all."""
    flow.flow_running = True
    ran: list[str] = []

    async def body() -> None:
        ran.append("theirs")

    await flow.run_auto_copy_flow(body)

    assert ran == ["theirs"]
    assert flow.flow_running is False
    # ...and the frames the harvest's own scrolling produced go with it, so
    # polling resumes from a clean post-flow baseline.
    assert flow.stale_arm_streak == 0
    assert flow.stale_diff is None


# -- where inside an appearance the click lands -----------------------------------


async def test_the_copy_click_goes_where_the_service_aims_it(
    flow: AutomationController, host: FakeHost, machine: _Machine
) -> None:
    """Some services draw a copy control that is wider than the bit of it that
    copies. The point is a percentage of the matched picture, so it survives the
    icon turning up anywhere on screen."""
    machine.looks = [(MATCH, None)]
    host.profile.set_click_point(TemplateKind.COPY, 0, 100)

    await flow.auto_copy_flow()

    assert host.copy_clicks == [ScreenRegion(MATCH_RECT.left, MATCH_RECT.top + 23, 1, 1)]


async def test_the_chat_box_click_goes_where_the_service_aims_it(
    flow: AutomationController, host: FakeHost, machine: _Machine
) -> None:
    """A chat box that is clickable end to end is the whole reason for this: the
    focus click before the snap lands on the point, not on the middle."""
    box = ScreenRegion(1100, 800, 601, 41)
    host.on_screen[TemplateKind.CHATBOX_ONGOING] = [box]
    host.profile.set_click_point(TemplateKind.CHATBOX_ONGOING, 10, 50)

    await flow.auto_copy_flow()

    assert [region for region, _settle in machine.clicks] == [ScreenRegion(1160, 820, 1, 1)]


async def test_the_whole_window_fallback_is_still_clicked_in_its_middle(
    flow: AutomationController, host: FakeHost, machine: _Machine
) -> None:
    """No chat box found means the target is the region the USER drew, which is
    not the picture any click point describes - aiming a tenth into it would
    land in the transcript."""
    host.profile.set_click_point(TemplateKind.CHATBOX_ONGOING, 10, 50)

    await flow.auto_copy_flow()

    assert [region for region, _settle in machine.clicks] == [CHAT_REGION]


async def test_a_clicked_element_is_aimed_by_its_own_click_point(
    flow: AutomationController, host: FakeHost, machine: _Machine
) -> None:
    """``click_profile_element`` is the one programmatic click on an appearance
    in the app (the new-chat button today), and it aims the same way."""
    button = ScreenRegion(300, 90, 121, 41)
    host.profile.put(TemplateKind.NEW_CHAT, _image(24, 24))
    host.profile.set_click_point(TemplateKind.NEW_CHAT, 100, 0)
    host.on_screen[TemplateKind.NEW_CHAT] = [button]

    assert await flow.click_profile_element(AgentSlot.MASTER, TemplateKind.NEW_CHAT) is (
        ElementClick.CLICKED
    )
    assert [region for region, _settle in machine.clicks] == [ScreenRegion(420, 90, 1, 1)]
