"""The auto-copy harvest, with no terminal, no browser and no mouse.

Since slice 6 the OS-acting sequences are the AutomationController's: the
harvest that fires when the detectors agree a reply finished, the find-then-click
primitive under it, the hover-scan fallback, and the two calls that move the
automation between browser windows. This is where their RULES are asserted; the
Pilot suites in ``tests/shell/tui`` stay as the wiring check that the real screen is
still plugged into them.

Two seams make that possible and neither is the paint port. The machine is the
:class:`~agentclip.driver.monitor.protocol.UIMonitor`, and since phase 6.2 that is
the whole of it: the hunt for the copy icon, the snap to the bottom and the
hover scan are monitor VERBS (``locate``, ``snap_to_bottom``, ``hover_scan``,
ui-monitor.md 2.3), so the double below answers those rather than a template
search somebody stubbed underneath them. Everything the sequences still have to
ASK a shell is :class:`~agentclip.driver.automation.host.AutomationHost`, and
``FakeHost`` is a scripted one: what the live service looks like, where its
appearances are, and whether the copy click took.

What is left in this file is therefore the POLICY around those verbs, which is
what stayed on this side of the seam: how many rounds a hunt is worth, when the
hover scan is allowed to run, where the focus click lands before a keyboard
snap, and what the user is told when the whole thing comes up empty.

The beats are shrunk at their own use site for the same reason: three snap
rounds at the real settle is over a second of a test doing nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import agentclip.driver.automation.controller as controller_mod
from agentclip.config import ServicePreset
from agentclip.driver.automation.controller import AutomationController
from agentclip.driver.automation.flow import ELEMENT_CLICK_SETTLE_S
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.ops import ElementClick
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.protocol import Located
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot

from .conftest import FakeAutomationView

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
ICON = (24, 24)
# Where the scripted search "finds" the icon. Absolute, because that is what a
# monitor answers with: a rectangle on the real screen, translated on the far
# side of the seam (ui-monitor.md 2.2).
MATCH_RECT = ScreenRegion(CHAT_REGION.left + 120, CHAT_REGION.top + 300, *ICON)
# What is actually clicked: ONE pixel of that rectangle, the middle of it while
# the service has not moved its click point (screen.profile).
CLICK_TARGET = click_point_region(MATCH_RECT, 50, 50)
# The two answers a copy-icon hunt gets. A miss carries how close the closest
# rejected candidate came, which is the number the failure report is built out
# of; a hit needs no diagnosis and carries none.
HIT = Located(MATCH_RECT, False, None)
MISS = Located(None, False, 0.21)
OUR_WINDOW = 4242


def _image(width: int, height: int) -> RegionImage:
    """A capture varied enough for ``Template.build`` to anchor on."""
    size = width * height * 4
    return RegionImage(width, height, (bytes(range(256)) * (size // 256 + 1))[:size])


class ScriptedMonitor(FakeUIMonitor):
    """Every verb the harvest asks the machine for, recorded rather than
    performed - and every search answered from a script.

    A ``FakeUIMonitor`` subclass rather than a stub one layer down: there is no
    layer down any more. The copy-icon hunt is ``locate`` and nothing else, so
    what a test scripts is the ANSWER (:attr:`looks`) rather than the pixels a
    search would have been run over.
    """

    def __init__(self, host: FakeHost) -> None:
        super().__init__(clipboard=FakeClipboard())
        self.host = host
        self.clicks: list[tuple[ScreenRegion, float | None]] = []
        self.moves: list[tuple[int, int]] = []
        self.focuses: list[int] = []
        # One entry per snap, naming the scroll action the service asked for -
        # WHICH keys or detents that turns into is the monitor's business now.
        self.snaps: list[str] = []
        self.hover_scans: list[TemplateKind] = []
        self.element_clicks: list[tuple[TemplateKind, float | None]] = []
        self.element_verdict = ElementClick.CLICKED
        self.order: list[str] = []
        # What the next copy-icon search should answer, popped one look at a
        # time; the last entry repeats for ever, so a test scripts only what it
        # cares about. A miss by default.
        self.looks: list[Located] = [MISS]
        # What the hover scan finds, when one is asked for at all.
        self.hover: ScreenRegion | None = None

    def nothing(self) -> bool:
        return not (self.clicks or self.moves or self.snaps or self.focuses)

    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool:
        self.clicks.append((region, settle_s))
        self.order.append("click")
        return True

    async def move_cursor(self, x: int, y: int) -> bool:
        self.moves.append((x, y))
        self.order.append("move")
        return True

    async def focus_window(self, handle: int) -> bool:
        self.focuses.append(handle)
        self.order.append("focus")
        return True

    async def snap_to_bottom(self, action: str) -> None:
        self.snaps.append(action)
        self.order.append("snap")

    async def locate(
        self, kind: TemplateKind, *, exclude_kinds: tuple[TemplateKind, ...] = ()
    ) -> Located:
        if kind is TemplateKind.COPY:
            return self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]
        found = self.host.on_screen.get(kind, [])
        if not found:
            return Located(None, False, 0.21)
        return Located(max(found, key=lambda rect: rect.top), len(found) > 1, None)

    async def hover_scan(self, kind: TemplateKind) -> ScreenRegion | None:
        self.hover_scans.append(kind)
        self.order.append("hover")
        return self.hover

    async def click_element(
        self, kind: TemplateKind, *, settle_s: float | None = None
    ) -> ElementClick:
        self.element_clicks.append((kind, settle_s))
        self.order.append("element")
        return self.element_verdict


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
        # The controller under test, when a test wants to see the prose window
        # from INSIDE the two calls it brackets - the only vantage point from
        # which "armed for exactly this act" is observable at all.
        self.watch: AutomationController | None = None
        self.window_at_click: list[bool | None] = []
        self.window_at_harvest: list[bool | None] = []

    def _window(self) -> bool | None:
        return self.watch.prose_window if self.watch is not None else None

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
        self.window_at_click.append(self._window())
        return self.click_takes

    async def ingest_harvest(self) -> None:
        self.harvests += 1
        self.window_at_harvest.append(self._window())

    def copy_seen_note(self) -> str:
        return self.seen_note

    def rebuild_detectors(self) -> None:
        self.rebuilds += 1


def _preset(**overrides: Any) -> ServicePreset:
    base = ServicePreset(
        key="fake", label="Fake", max_paste_chars=10_000, total_context_chars=100_000
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture(autouse=True)
def quick_beats(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harvest's own beat, shrunk: three snap rounds at the real settle is
    over a second of a test waiting for a page that does not exist."""
    monkeypatch.setattr(controller_mod, "SNAP_SETTLE_S", 0.0)


@pytest.fixture
def machine(host: FakeHost) -> ScriptedMonitor:
    return ScriptedMonitor(host)


@pytest.fixture
def host() -> FakeHost:
    profile = ServiceProfile(key="fake")
    profile.put(TemplateKind.COPY, _image(*ICON))
    return FakeHost(_preset(), profile)


@pytest.fixture
def flow(
    view: FakeAutomationView, host: FakeHost, machine: ScriptedMonitor
) -> AutomationController:
    """A controller with a drawn chat window and a captured copy button - the
    state the finish decision fires the harvest out of."""
    automation = AutomationController(view=view, host=host, monitor=machine)
    automation.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    automation.set_own_window(OUR_WINDOW)
    host.watch = automation
    return automation


# -- the happy path --------------------------------------------------------------


async def test_the_harvest_snaps_hunts_clicks_and_comes_home(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """One round finds the icon: focus the chat, park the pointer, snap, search,
    click the rectangle the match translates back to - then hand focus back to
    the tool and let the shell ingest what was copied."""
    machine.looks = [HIT]

    await flow.auto_copy_flow()

    # No chat box is calibrated, so the focus click lands on the drawn window.
    assert [region for region, _settle in machine.clicks] == [CHAT_REGION]
    assert machine.moves == [CHAT_REGION.center]
    # One snap, named by the action the service asked for. WHICH keys or wheel
    # detents that turns into is the monitor's (``UIMonitor.snap_to_bottom``).
    assert machine.snaps == [host.preset.scroll_action]
    assert host.copy_clicks == [CLICK_TARGET]
    # The click first, focus strictly after it: a snap-back that overtook the
    # click would take the copy away from the window it was aimed at.
    assert machine.focuses == [OUR_WINDOW]
    assert machine.order[-1] == "focus"
    assert host.harvests == 1
    # And the readout says what happened, with the captured size in front of it.
    assert (TemplateKind.COPY, "24×24 · clicked") in view.detection_lines
    assert flow.loop_state is not LoopState.MANUAL_COPY


async def test_a_service_can_refuse_the_harvests_snap_back_too(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """``ServicePreset.snap_back`` off is the debugging aid, and an aid that
    covered only the auto-SEND would be no aid at all: the harvest fires seconds
    later on the same turn and would take the browser away again just as the
    user was watching where the clicks landed. Everything else is unchanged -
    the copy is still clicked and the reply is still ingested."""
    machine.looks = [HIT]
    host.preset = _preset(snap_back=False)

    await flow.auto_copy_flow()

    assert host.copy_clicks == [CLICK_TARGET]
    assert machine.focuses == []
    assert host.harvests == 1


async def test_the_snap_back_needs_a_handle_to_snap_to(
    view: FakeAutomationView, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """``set_own_window`` is the only source of that handle. Without one there is
    no window to come home to, and the harvest simply leaves the browser
    focused rather than guessing at one."""
    machine.looks = [HIT]
    flow = AutomationController(view=view, host=host, monitor=machine)
    flow.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    flow.set_own_window(None)  # a reading that failed keeps nothing

    await flow.auto_copy_flow()

    assert flow.own_window is None
    assert machine.focuses == []
    assert host.harvests == 1  # the harvest still happened


# -- when the hunt comes up empty -------------------------------------------------


async def test_a_miss_re_snaps_and_then_hands_the_harvest_over(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """A page that had not finished arriving is the commonest reason the icon is
    not on the frame, so one miss re-scrolls rather than giving up - and only
    after the last round does the harvest become the user's."""
    host.seen_note = "; the poller last saw one 3s ago"

    await flow.auto_copy_flow()

    assert len(machine.snaps) == controller_mod.COPY_SNAP_ROUNDS
    # ...and the choreography in front of the snap happened once: nothing
    # between rounds touches the mouse or the focus.
    assert len(machine.clicks) == 1
    assert machine.moves == [CHAT_REGION.center]
    assert host.copy_clicks == []  # nothing found means nothing clicked
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert view.logged("copy button not found after 3 snaps")
    assert view.logged("the poller last saw one 3s ago")  # the shell's own note


async def test_the_click_that_lands_on_a_second_round_stops_the_hunt(
    flow: AutomationController, machine: ScriptedMonitor, host: FakeHost
) -> None:
    """The rounds are a retry budget, not a schedule."""
    machine.looks = [Located(None, False, 0.30), HIT]

    await flow.auto_copy_flow()

    assert len(machine.snaps) == 2
    assert host.copy_clicks == [CLICK_TARGET]


async def test_a_screen_that_could_not_be_read_reports_as_a_miss(
    flow: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """A capture that failed is not its own branch up here any more.

    The monitor answers "nothing there" for every way a search can come up
    empty - no region, no capture, no candidate - because a caller that may not
    click has the same next move for all of them (ui-monitor.md 2.3). What still
    tells them apart is ``best_miss``: None means nothing was ever judged, which
    is the phrase that separates "the icon simply was not on the frame" from
    "the capture has drifted, recapture it".
    """
    machine.looks = [Located(None, False, None)]

    await flow.auto_copy_flow()

    assert flow.loop_state is LoopState.MANUAL_COPY
    assert (TemplateKind.COPY, "24×24 · not found") in view.detection_lines
    assert view.logged("no candidate cleared the first-stage sniff test")


async def test_nothing_to_search_never_touches_the_machine(
    view: FakeAutomationView, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """No drawn window means nowhere to look - and the refusal happens before a
    single click, scroll or cursor move."""
    flow = AutomationController(view=view, host=host, monitor=machine)

    await flow.auto_copy_flow()

    assert machine.nothing()
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert view.logged("no chat window is drawn")


# -- the hover scan ---------------------------------------------------------------


async def test_the_hover_scan_runs_after_the_last_static_miss(
    view: FakeAutomationView, machine: ScriptedMonitor, host: FakeHost
) -> None:
    """A chat that only paints the copy icon under the pointer: every static
    round misses, and then - once, at the end - the scan runs.

    The cursor walk itself is the monitor's now (``UIMonitor.hover_scan``, which
    moves the real mouse and captures at every stop); what is asserted here is
    the POLICY around it, which is what stayed: it runs after the LAST static
    miss rather than the first, exactly once, and the rectangle it hands back is
    clicked like any other.
    """
    host.preset = _preset(hover_scan=True)
    flow = AutomationController(view=view, host=host, monitor=machine)
    flow.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    rounds = controller_mod.COPY_SNAP_ROUNDS
    machine.looks = [MISS]
    machine.hover = MATCH_RECT

    await flow.auto_copy_flow()

    assert len(machine.snaps) == rounds
    assert machine.hover_scans == [TemplateKind.COPY]  # once, after the last miss
    assert machine.order.index("hover") > max(
        index for index, verb in enumerate(machine.order) if verb == "snap"
    )
    assert machine.moves == [CHAT_REGION.center]  # only the pre-snap park is ours
    assert host.copy_clicks == [CLICK_TARGET]
    assert (TemplateKind.COPY, "24×24 · hover-scanning") in view.detection_lines


async def test_a_static_hit_never_starts_a_scan(
    view: FakeAutomationView, machine: ScriptedMonitor, host: FakeHost
) -> None:
    """The cheap path stays cheap: the scan is never even asked for."""
    host.preset = _preset(hover_scan=True)
    flow = AutomationController(view=view, host=host, monitor=machine)
    flow.set_calibration(AgentSlot.MASTER, CHAT_REGION)
    machine.looks = [HIT]

    await flow.auto_copy_flow()

    assert machine.hover_scans == []
    assert machine.moves == [CHAT_REGION.center]


async def test_the_scan_is_opt_in_per_service(
    flow: AutomationController, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """With ``hover_scan`` off - the default - a static miss is simply a miss,
    and the user's cursor is never walked up the transcript."""
    await flow.auto_copy_flow()

    assert machine.hover_scans == []
    assert machine.moves == [CHAT_REGION.center]
    assert not any("hover" in text for _kind, text in view.detection_lines)


# -- a click that does not take ---------------------------------------------------


async def test_a_copy_click_that_never_takes_hands_it_back(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor, view: FakeAutomationView
) -> None:
    """The icon was found and clicked and the clipboard never changed. The
    browser keeps focus deliberately - that is where the user has to finish the
    job - so there is no snap-back at all."""
    machine.looks = [HIT]
    host.click_takes = False

    await flow.auto_copy_flow()

    assert host.copy_clicks == [CLICK_TARGET]
    assert machine.focuses == []  # no snap-back on failure
    assert host.harvests == 0
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert (TemplateKind.COPY, "24×24 · click did not take") in view.detection_lines


# -- the prose window -------------------------------------------------------------
# The one-shot loosening of protocol.md 1.4 tolerance #11: while it is open a
# shell's harvest may show a reply that carries no CLIP blocks at all, because
# the flow just watched the copy button write that text. Everything asserted
# here is about its EXTENT - one act, and not a moment either side of it.


async def test_the_prose_window_is_open_for_the_click_and_the_harvest(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """Armed immediately before the verified click, still open while the harvest
    runs - that pair is the whole permission - and shut the moment it returns."""
    machine.looks = [HIT]
    assert flow.prose_window is False  # nothing is armed until there is a target

    await flow.auto_copy_flow()

    assert host.window_at_click == [True]
    assert host.window_at_harvest == [True]
    assert flow.prose_window is False


async def test_a_click_that_never_takes_leaves_no_window_open(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """The MANUAL_COPY exit: the click happened, the clipboard did not change,
    so there is nothing to ingest and nothing may be ingested."""
    machine.looks = [HIT]
    host.click_takes = False

    await flow.run_auto_copy_flow()

    assert host.harvests == 0
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert flow.prose_window is False


async def test_a_hunt_that_finds_nothing_never_arms_the_window(
    flow: AutomationController, host: FakeHost
) -> None:
    """No copy button on screen means no click of ours to vouch for a harvest."""
    await flow.run_auto_copy_flow()

    assert host.copy_clicks == []
    assert flow.loop_state is LoopState.MANUAL_COPY
    assert flow.prose_window is False


async def test_a_failed_capture_leaves_no_window_open(
    flow: AutomationController, machine: ScriptedMonitor
) -> None:
    """The flow stops before there is anything to look in, let alone click."""
    machine.capture_fails = True

    await flow.run_auto_copy_flow()

    assert flow.prose_window is False


async def test_the_bracket_shuts_the_window_a_raising_harvest_left_open(
    flow: AutomationController
) -> None:
    """``end_flow``'s defensive close, from the one direction the flow's own
    path cannot cover: a body that dies mid-flight. A window left armed would
    hand the next thing the USER copies the reply's treatment."""

    async def explode() -> None:
        flow._prose_window = True
        raise RuntimeError("the browser vanished")

    with pytest.raises(RuntimeError):
        await flow.run_auto_copy_flow(explode)

    assert flow.prose_window is False


# -- the suspension bracket -------------------------------------------------------


async def test_the_suspension_lifts_however_the_harvest_ends(
    flow: AutomationController, machine: ScriptedMonitor
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
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """Some services draw a copy control that is wider than the bit of it that
    copies. The point is a percentage of the matched picture, so it survives the
    icon turning up anywhere on screen."""
    machine.looks = [HIT]
    host.profile.set_click_point(TemplateKind.COPY, 0, 100)

    await flow.auto_copy_flow()

    assert host.copy_clicks == [ScreenRegion(MATCH_RECT.left, MATCH_RECT.top + 23, 1, 1)]


async def test_the_chat_box_click_goes_where_the_service_aims_it(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """A chat box that is clickable end to end is the whole reason for this: the
    focus click before the snap lands on the point, not on the middle."""
    box = ScreenRegion(1100, 800, 601, 41)
    host.on_screen[TemplateKind.CHATBOX_ONGOING] = [box]
    host.profile.set_click_point(TemplateKind.CHATBOX_ONGOING, 10, 50)

    await flow.auto_copy_flow()

    assert [region for region, _settle in machine.clicks] == [ScreenRegion(1160, 820, 1, 1)]


async def test_the_whole_window_fallback_is_still_clicked_in_its_middle(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """No chat box found means the target is the region the USER drew, which is
    not the picture any click point describes - aiming a tenth into it would
    land in the transcript."""
    host.profile.set_click_point(TemplateKind.CHATBOX_ONGOING, 10, 50)

    await flow.auto_copy_flow()

    assert [region for region, _settle in machine.clicks] == [CHAT_REGION]


async def test_a_clicked_element_is_named_and_never_aimed_from_up_here(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """``click_profile_element`` is the one programmatic click on an appearance
    in the app (the new-chat button today), and since phase 6.2 it names a KIND
    and nothing else.

    Finding it, refusing a second one of it, and aiming at the service's own
    click point are all inside ``UIMonitor.click_element`` - which is why there
    is no rectangle here to assert on: a template, a tolerance and a click point
    are things that could not cross the wire (ui-monitor.md 2.3).
    """
    host.profile.put(TemplateKind.NEW_CHAT, _image(24, 24))

    assert await flow.click_profile_element(AgentSlot.MASTER, TemplateKind.NEW_CHAT) is (
        ElementClick.CLICKED
    )
    assert machine.element_clicks == [(TemplateKind.NEW_CHAT, ELEMENT_CLICK_SETTLE_S)]
    assert machine.clicks == []  # nothing is aimed from this side of the seam


async def test_the_two_refusals_the_brain_makes_never_reach_the_screen(
    flow: AutomationController, host: FakeHost, machine: ScriptedMonitor
) -> None:
    """DISARMED and NOT_CALIBRATED are decided HERE, above the search: the armed
    switch is policy, and "nothing is captured to look for" is answered against
    calibration this object is already holding. Either way the monitor is never
    asked, which is the point - a refusal that had already searched the screen
    would be answering a question nobody may act on."""
    assert await flow.click_profile_element(AgentSlot.MASTER, TemplateKind.NEW_CHAT) is (
        ElementClick.NOT_CALIBRATED  # the service has no new-chat button captured
    )
    host.profile.put(TemplateKind.NEW_CHAT, _image(24, 24))
    flow.set_os_armed(False)
    assert await flow.click_profile_element(AgentSlot.MASTER, TemplateKind.NEW_CHAT) is (
        ElementClick.DISARMED
    )
    assert machine.element_clicks == []
