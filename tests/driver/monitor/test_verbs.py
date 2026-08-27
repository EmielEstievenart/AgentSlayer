"""The pixel verdicts: what looking at the configured region comes to.

docs/design/ui-monitor.md §2.3 splits the old ``click_profile_element`` in two -
MISMATCH, AMBIGUOUS, CLICKED and NOT_CLICKED are things you learn by looking at
a screen, and they are the monitor's; DISARMED and NOT_CALIBRATED are refusals
the brain makes before it calls. These are the four, plus the three verbs the
auto-copy harvest is built out of: ``find_all``, ``locate``, ``hover_scan`` and
``snap_to_bottom``.

Nothing here compares a pixel. The search is scripted (``ScriptedOps``) exactly
as ``tests/driver/automation/test_find_all.py`` scripts it, because what these
verbs are about is not whether a template matches - that is
``driver/screen/template.py``'s question, answered there - but what the monitor
DOES with the answer: which searches it spends, where it translates the result
to, what it clicks, and which of the ways there is nothing to look at it treats
as the same way.

The refusals are the theme. A monitor with no region drawn, no profile for its
service, nothing captured for the kind, or a capture that failed must answer the
empty answer and must never raise: every one of those is "you may not click",
and a caller that had to tell them apart to know that would be a caller with
four branches where one is honest.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agentclip.driver.monitor.beats import (
    ELEMENT_CLICK_SETTLE_S,
    PAGE_DOWN_TAPS,
    SNAP_WHEEL_DETENTS,
)
from agentclip.driver.monitor.protocol import ElementClick, Located
from agentclip.driver.screen.hover import hover_scan_points
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.template import RegionMatch

from .conftest import REGION, TIMEOUT_S, ScriptedOps, Wiring, profile_with, spec

COPY = TemplateKind.COPY
CHATBOX = TemplateKind.CHATBOX_ONGOING

# The drawn chat window, deliberately not at the origin: a rectangle handed to a
# click is absolute, and an offset of (0, 0) would pass whether or not the
# translation out of scene-local coordinates happens at all.
CHAT = ScreenRegion(1000, 500, 400, 300)
# Where the scripted searches say the appearance is, scene-local, and the
# absolute rectangle that has to come back out. 20x16 is ``conftest.template``'s
# size, and a located rectangle is the size of the image that matched.
HIT = RegionMatch(30, 40, 0.0)
HIT_RECT = ScreenRegion(1030, 540, 20, 16)
# The one pixel a click on that rectangle lands on with nobody's click point
# adjusted: dead centre, as the 1x1 rectangle every click in the app takes.
HIT_TARGET = ScreenRegion(1030 + 10, 540 + 8, 1, 1)
ELSEWHERE = RegionMatch(30, 200, 0.0)
ELSEWHERE_RECT = ScreenRegion(1030, 700, 20, 16)


async def configured(
    wire: Callable[..., Wiring],
    ops: ScriptedOps,
    *,
    region: ScreenRegion | None = CHAT,
    kinds: tuple[TemplateKind, ...] = (COPY,),
    service: str = "svc",
    profile: ServiceProfile | None = None,
) -> Wiring:
    """A monitor pointed at ``region``, with ``kinds`` captured for its service.

    ``finish_signals=()`` because no verb here reads a probe: the finish
    detectors would only add captures to the record. The sighting kinds still
    give the poller something to watch, which is why :func:`retarget` has to
    hold the screen still afterwards.
    """
    held = profile if profile is not None else profile_with(*kinds)
    wiring = wire(ops=ops, profile=held, service=service)
    await retarget(wiring, ops, region)
    return wiring


async def retarget(wiring: Wiring, ops: ScriptedOps, region: ScreenRegion | None) -> None:
    """Point the monitor at ``region``, then hold the screen still.

    A configured monitor POLLS, and a poll captures through the very hand these
    tests are recording, so the run is suspended and joined before any verb is
    called and the record is wiped clean. That leaves exactly one thing touching
    the machine: the verb under test. The detector, the trackers and the spec
    all survive a suspend (that is what makes it a suspend and not a close), so
    what the verbs answer against is a genuinely configured monitor.
    """
    await wiring.monitor.configure(spec(region=region, finish_signals=()))
    poller = wiring.monitor.poller
    await wiring.monitor.suspend()
    if poller is not None:
        poller.thread.join(TIMEOUT_S)
        assert not poller.thread.is_alive(), "a poll thread outlived the suspend"
    ops.forget()


# == find_all ==================================================================


async def test_find_all_answers_in_absolute_screen_coordinates(
    wire: Callable[..., Wiring],
) -> None:
    """The one translation that matters: matches are scene-local, clicks are not.

    Two of them, because "how many" is the question this verb exists for - a
    tuple longer than one is a second window of the same service under one drawn
    region, and every caller reads it as a refusal to guess.
    """
    ops = ScriptedOps(all_matches=([HIT, ELSEWHERE],))
    wiring = await configured(wire, ops)

    assert await wiring.monitor.find_all(COPY) == (HIT_RECT, ELSEWHERE_RECT)
    assert ops.captures == [CHAT], "the verb searched something other than the drawn region"


async def test_find_all_is_empty_with_no_region_drawn(wire: Callable[..., Wiring]) -> None:
    """And spends nothing finding that out: there is nowhere to point a camera."""
    ops = ScriptedOps(all_matches=([HIT],))
    wiring = await configured(wire, ops, region=None)

    assert await wiring.monitor.find_all(COPY) == ()
    assert ops.captures == []


async def test_find_all_is_empty_when_the_service_has_no_profile_here(
    wire: Callable[..., Wiring],
) -> None:
    """The split-mode case, and the cold-start one: the spec names a service by
    KEY (§2.10) and this machine may simply have no captures for it."""
    ops = ScriptedOps(all_matches=([HIT],))
    wiring = await configured(wire, ops, service="somebody-else")

    assert await wiring.monitor.find_all(COPY) == ()
    assert ops.captures == []


async def test_find_all_is_empty_for_an_uncaptured_kind(wire: Callable[..., Wiring]) -> None:
    """A profile that has a copy button and no chat box is the ordinary
    half-calibrated state, not an error."""
    ops = ScriptedOps(all_matches=([HIT],))
    wiring = await configured(wire, ops, kinds=(COPY,))

    assert await wiring.monitor.find_all(CHATBOX) == ()
    assert ops.captures == []


async def test_find_all_is_empty_when_the_capture_fails(wire: Callable[..., Wiring]) -> None:
    """A frame that never arrived is not evidence of an empty screen - but it
    licenses exactly the same next move, which is none."""
    ops = ScriptedOps(all_matches=([HIT],), capture_error=True)
    wiring = await configured(wire, ops)

    assert await wiring.monitor.find_all(COPY) == ()
    assert ops.searches == [], "a failed capture was searched anyway"


# == locate ====================================================================


async def test_locate_answers_where_the_lowest_one_is(wire: Callable[..., Wiring]) -> None:
    """The copy button's question. One capture, and the rectangle absolute."""
    ops = ScriptedOps(lowest=((HIT, 0.9),), all_matches=([HIT],))
    wiring = await configured(wire, ops)

    found = await wiring.monitor.locate(COPY)

    assert found == Located(HIT_RECT, False, None, HIT_TARGET)
    assert ops.captures == [CHAT]


async def test_locate_aims_at_the_services_own_click_point(
    wire: Callable[..., Wiring],
) -> None:
    """§11.3: the ONE pixel to click comes back with the rectangle.

    The brain holds no appearances any more, so it cannot apply a click point
    itself - and the middle of a control is only the right pixel until a service
    draws one whose middle is a label. 25%/75% of the 20x16 match, the same
    arithmetic ``click_element`` does with the same profile.
    """
    ops = ScriptedOps(lowest=((HIT, 0.9),), all_matches=([HIT],))
    aimed = profile_with(COPY)
    aimed.set_click_point(COPY, 25, 75)
    wiring = await configured(wire, ops, profile=aimed)

    found = await wiring.monitor.locate(COPY)

    assert found.region == HIT_RECT
    assert found.target == ScreenRegion(1030 + 5, 540 + 11, 1, 1)


async def test_a_located_miss_has_nothing_to_aim_at(wire: Callable[..., Wiring]) -> None:
    """``target`` is None if and only if ``region`` is - the invariant every
    caller that clicks leans on."""
    ops = ScriptedOps(lowest=((None, 0.31),))
    wiring = await configured(wire, ops)

    assert (await wiring.monitor.locate(COPY)).target is None


async def test_locate_flags_two_of_one_kind_and_still_says_where(
    wire: Callable[..., Wiring],
) -> None:
    """Ambiguity is a DRAWING failure, not a search failure, so the answer still
    carries the rectangle: a caller reporting the trouble should not have to
    search again, and every caller that CLICKS is expected to refuse anyway."""
    ops = ScriptedOps(lowest=((ELSEWHERE, None),), all_matches=([HIT, ELSEWHERE],))
    wiring = await configured(wire, ops)

    found = await wiring.monitor.locate(COPY)

    assert found.ambiguous is True
    assert found.region == ELSEWHERE_RECT


async def test_locate_carries_the_best_miss_and_spends_one_search_to_get_it(
    wire: Callable[..., Wiring],
) -> None:
    """The number that turns "not found" into an actionable report - and the
    cost rule around it.

    Nothing found is not ambiguous, so there is nothing left to ask, and the
    miss path is the one that must not double: the auto-copy's hunt re-snaps and
    re-searches three times over and every round is a full-region comparison.
    """
    ops = ScriptedOps(lowest=((None, 0.31),), all_matches=([HIT],))
    wiring = await configured(wire, ops)

    assert await wiring.monitor.locate(COPY) == Located(None, False, 0.31)
    assert ops.searches == ["lowest"], "a miss paid for the ambiguity question too"


async def test_locate_refuses_a_match_that_lands_on_an_excluded_kind(
    wire: Callable[..., Wiring],
) -> None:
    """``exclude_kinds``: it is on screen, it is just not this kind's element.

    Two kinds of one service can share pixels, and a hit that is really the
    other one is a click on the wrong control. Reported as a miss rather than as
    an ambiguity because there is nothing a redrawn window would fix.
    """
    ops = ScriptedOps(lowest=((HIT, 0.4),), all_matches=([HIT],))
    wiring = await configured(wire, ops, kinds=(COPY, CHATBOX))

    assert await wiring.monitor.locate(COPY, exclude_kinds=(CHATBOX,)) == Located(None, False, 0.4)
    # One frame, both searches: the veto has to describe the same instant as the
    # match it vetoes, or it is a veto about a screen that has since moved.
    assert ops.captures == [CHAT]


async def test_locate_keeps_a_match_the_excluded_kind_is_nowhere_near(
    wire: Callable[..., Wiring],
) -> None:
    """The other half of the same rule, and the one that would silently break
    every hunt if the exclusion were a blanket "was it found at all"."""
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([ELSEWHERE], [HIT]))
    wiring = await configured(wire, ops, kinds=(COPY, CHATBOX))

    found = await wiring.monitor.locate(COPY, exclude_kinds=(CHATBOX,))

    assert found.region == HIT_RECT


async def test_locate_searches_nothing_extra_when_nothing_is_excluded(
    wire: Callable[..., Wiring],
) -> None:
    """The default costs what it always cost: the lowest-match search, plus the
    one that answers "were there several?" now that there is one to click."""
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],))
    wiring = await configured(wire, ops, kinds=(COPY, CHATBOX))

    await wiring.monitor.locate(COPY)

    assert ops.searches == ["lowest", "all"]


@pytest.mark.parametrize(
    ("what", "kwargs"),
    [
        ("no region drawn", {"region": None}),
        ("no profile on this machine", {"service": "somebody-else"}),
        ("nothing captured for the kind", {"kinds": (CHATBOX,)}),
    ],
)
async def test_locate_refuses_with_nothing_to_look_at(
    wire: Callable[..., Wiring], what: str, kwargs: dict[str, object]
) -> None:
    """Three ways there is nothing to look at, one answer, and no capture spent
    on any of them. ``what`` names the case in the failure output."""
    ops = ScriptedOps(lowest=((HIT, 0.1),), all_matches=([HIT],))
    wiring = await configured(wire, ops, **kwargs)  # type: ignore[arg-type]

    assert await wiring.monitor.locate(COPY) == Located(None, False, None), what
    assert ops.captures == []


async def test_locate_refuses_a_capture_that_failed(wire: Callable[..., Wiring]) -> None:
    ops = ScriptedOps(lowest=((HIT, 0.1),), capture_error=True)
    wiring = await configured(wire, ops)

    assert await wiring.monitor.locate(COPY) == Located(None, False, None)


# == click_element =============================================================


async def test_click_element_clicks_the_services_own_click_point(
    wire: Callable[..., Wiring],
) -> None:
    """CLICKED, and the pixel it lands on.

    The middle of a control is only the right pixel until a service draws one
    whose middle is a label and whose left third is the button, so the click
    goes through the profile's click point - here 25%/75% of a 20x16 rectangle.
    """
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],))
    aimed = profile_with(COPY)
    aimed.set_click_point(COPY, 25, 75)
    wiring = await configured(wire, ops, profile=aimed)

    assert await wiring.monitor.click_element(COPY) == ElementClick.CLICKED
    target, settle = ops.clicks[0]
    assert target == ScreenRegion(1030 + 5, 540 + 11, 1, 1)
    assert settle == ELEMENT_CLICK_SETTLE_S


async def test_click_element_takes_a_settle_of_the_callers_choosing(
    wire: Callable[..., Wiring],
) -> None:
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],))
    wiring = await configured(wire, ops)

    await wiring.monitor.click_element(COPY, settle_s=0.4)

    assert ops.clicks[0][1] == 0.4


async def test_click_element_mismatches_rather_than_clicking_blind(
    wire: Callable[..., Wiring],
) -> None:
    """Not on screen means nothing is clicked. Refusing is always the safe
    answer - the user can click it themselves."""
    ops = ScriptedOps(lowest=((None, 0.5),))
    wiring = await configured(wire, ops)

    assert await wiring.monitor.click_element(COPY) == ElementClick.MISMATCH
    assert ops.clicks == []


async def test_click_element_refuses_to_guess_between_two_of_them(
    wire: Callable[..., Wiring],
) -> None:
    """Two under one drawn region is two conversations, and picking one is a
    coin toss whose loser is a chat that gets clicked on behalf of the other."""
    ops = ScriptedOps(lowest=((ELSEWHERE, None),), all_matches=([HIT, ELSEWHERE],))
    wiring = await configured(wire, ops)

    assert await wiring.monitor.click_element(COPY) == ElementClick.AMBIGUOUS
    assert ops.clicks == []


async def test_click_element_reports_a_click_the_os_refused(
    wire: Callable[..., Wiring],
) -> None:
    """Found fine, pressed, and the OS said no (synthetic input is Windows-only)
    - which is a different thing to tell the user from "it is not there"."""
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],), click_ok=False)
    wiring = await configured(wire, ops)

    assert await wiring.monitor.click_element(COPY) == ElementClick.NOT_CLICKED


@pytest.mark.parametrize(
    ("what", "kwargs"),
    [
        ("no region drawn", {"region": None}),
        ("no profile on this machine", {"service": "somebody-else"}),
        ("nothing captured for the kind", {"kinds": (CHATBOX,)}),
    ],
)
async def test_click_element_mismatches_with_nothing_to_look_at(
    wire: Callable[..., Wiring], what: str, kwargs: dict[str, object]
) -> None:
    """MISMATCH, never NOT_CALIBRATED: that verdict is a refusal the BRAIN makes
    before it calls (§2.3), and a monitor answering it would be answering about
    a calibration it does not own."""
    ops = ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],))
    wiring = await configured(wire, ops, **kwargs)  # type: ignore[arg-type]

    assert await wiring.monitor.click_element(COPY) == ElementClick.MISMATCH, what
    assert ops.clicks == []


async def test_click_element_never_answers_a_refusal_that_is_the_brains(
    wire: Callable[..., Wiring],
) -> None:
    """The rule itself, over every reachable branch: four verdicts, never six."""
    outcomes = set()
    for ops, kwargs in (
        (ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],)), {}),
        (ScriptedOps(lowest=((HIT, None),), all_matches=([HIT],), click_ok=False), {}),
        (ScriptedOps(lowest=((HIT, None),), all_matches=([HIT, ELSEWHERE],)), {}),
        (ScriptedOps(lowest=((None, 0.2),)), {}),
        (ScriptedOps(lowest=((HIT, None),)), {"region": None}),
        (ScriptedOps(lowest=((HIT, None),), capture_error=True), {}),
    ):
        wiring = await configured(wire, ops, **kwargs)  # type: ignore[arg-type]
        outcomes.add(await wiring.monitor.click_element(COPY))

    assert outcomes == {
        ElementClick.CLICKED,
        ElementClick.NOT_CLICKED,
        ElementClick.AMBIGUOUS,
        ElementClick.MISMATCH,
    }


# == hover_scan ================================================================


async def test_hover_scan_walks_the_cursor_up_and_stops_at_the_first_hit(
    wire: Callable[..., Wiring],
) -> None:
    """The whole point: some chats only paint the copy icon under the pointer.

    Bottom-up because the newest response is at the bottom, and STOPPING at the
    first hit because every further stop is another real move of the user's
    mouse for an answer already in hand.
    """
    stops = hover_scan_points(CHAT)
    assert len(stops) > 3, "the fixture's region is too small to be walked"
    ops = ScriptedOps(lowest=((None, None), (None, None), (HIT, None)))
    wiring = await configured(wire, ops)

    found = await wiring.monitor.hover_scan(COPY)

    # A ``Located``, so the click after a hover is aimed by the same point as
    # the click after a static search - and the two fields a walk cannot answer
    # are the empty ones: it stops at the first sight of the thing, so it never
    # counts a second and judges nothing it could report a diff for.
    assert found == Located(HIT_RECT, False, None, HIT_TARGET)
    assert ops.moves == stops[:3]
    assert ops.captures == [CHAT, CHAT, CHAT], "a stop looked without moving first"


async def test_hover_scan_walks_the_whole_region_before_giving_up(
    wire: Callable[..., Wiring],
) -> None:
    ops = ScriptedOps(lowest=((None, 0.6),))
    wiring = await configured(wire, ops)

    assert await wiring.monitor.hover_scan(COPY) == Located(None, False, None)
    assert ops.moves == hover_scan_points(CHAT)


async def test_hover_scan_stops_dead_when_the_cursor_cannot_be_moved(
    wire: Callable[..., Wiring],
) -> None:
    """A scan that cannot move cannot see, and pretending to keep looking would
    spend the rest of the walk asking a question no stop can answer."""
    ops = ScriptedOps(lowest=((HIT, None),), move_ok=False)
    wiring = await configured(wire, ops)

    assert await wiring.monitor.hover_scan(COPY) == Located(None, False, None)
    assert len(ops.moves) == 1
    assert ops.captures == []


async def test_hover_scan_stops_dead_when_a_capture_fails(
    wire: Callable[..., Wiring],
) -> None:
    ops = ScriptedOps(lowest=((HIT, None),), capture_error=True)
    wiring = await configured(wire, ops)

    assert await wiring.monitor.hover_scan(COPY) == Located(None, False, None)
    assert len(ops.captures) == 1


@pytest.mark.parametrize(
    ("what", "kwargs"),
    [
        ("no region drawn", {"region": None}),
        ("no profile on this machine", {"service": "somebody-else"}),
        ("nothing captured for the kind", {"kinds": (CHATBOX,)}),
    ],
)
async def test_hover_scan_moves_nothing_with_nothing_to_look_for(
    wire: Callable[..., Wiring], what: str, kwargs: dict[str, object]
) -> None:
    """The strongest form of the refusal rule, because this one is the verb that
    drives the user's real mouse across their real screen."""
    ops = ScriptedOps(lowest=((HIT, None),))
    wiring = await configured(wire, ops, **kwargs)  # type: ignore[arg-type]

    assert await wiring.monitor.hover_scan(COPY) == Located(None, False, None), what
    assert ops.moves == []


# == snap_to_bottom ============================================================


async def test_snap_to_bottom_taps_page_down_in_a_burst(wire: Callable[..., Wiring]) -> None:
    """A generous over-shoot rather than a measured scroll: the flow wants the
    newest reply on screen, and a snap that stops short is a silent MANUAL_COPY."""
    ops = ScriptedOps()
    wiring = await configured(wire, ops)

    await wiring.monitor.snap_to_bottom("page_down")

    assert ops.keys == [("page_down", PAGE_DOWN_TAPS)]
    assert ops.scrolls == []


async def test_snap_to_bottom_taps_end_exactly_once(wire: Callable[..., Wiring]) -> None:
    """One tap IS the bottom, by definition - there is nothing to over-shoot."""
    ops = ScriptedOps()
    wiring = await configured(wire, ops)

    await wiring.monitor.snap_to_bottom("end")

    assert ops.keys == [("end", 1)]


async def test_snap_to_bottom_flicks_the_wheel_over_the_drawn_region(
    wire: Callable[..., Wiring],
) -> None:
    """The wheel is the only one of the three that has to be AIMED: a scroll key
    goes to whatever holds focus, a detent goes where the pointer is."""
    ops = ScriptedOps()
    wiring = await configured(wire, ops)

    await wiring.monitor.snap_to_bottom("scroll")

    assert ops.scrolls == [(CHAT, SNAP_WHEEL_DETENTS)]
    assert ops.keys == []


async def test_snap_to_bottom_with_no_region_does_nothing_at_all(
    wire: Callable[..., Wiring],
) -> None:
    """...and does not raise. It is the wheel that needs a rectangle; with none
    drawn there is nothing to aim at, and turning it wherever the pointer
    happens to be would scroll a window nobody asked about."""
    ops = ScriptedOps()
    wiring = await configured(wire, ops, region=None)

    await wiring.monitor.snap_to_bottom("scroll")

    assert ops.scrolls == [] and ops.keys == []


async def test_the_keyboard_snaps_need_no_region(wire: Callable[..., Wiring]) -> None:
    """The other side of it: a key goes to whatever holds focus, so an
    uncalibrated monitor can still send one."""
    ops = ScriptedOps()
    wiring = await configured(wire, ops, region=None)

    await wiring.monitor.snap_to_bottom("end")

    assert ops.keys == [("end", 1)]


async def test_the_verbs_answer_against_the_region_they_were_configured_with(
    wire: Callable[..., Wiring],
) -> None:
    """Not the region they were built with, and not a caller's: reconfiguring is
    how the automation follows a window, and a verb that had cached the old
    rectangle would search the screen the user just moved away from."""
    ops = ScriptedOps(all_matches=([HIT],))
    wiring = await configured(wire, ops)
    await retarget(wiring, ops, REGION)

    await wiring.monitor.find_all(COPY)

    assert ops.captures == [REGION]
