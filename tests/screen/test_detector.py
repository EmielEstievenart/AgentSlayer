"""The standalone detector (screen/detector.py).

Textual-free, so every one of these is a plain function call over hand-built
frames. What is worth pinning is the promise the module exists for: **what gets
searched is decided by calibration and by nothing else**, and every calibrated
kind is searched on every frame - which is what makes the ELEMENTS column able
to show the user what the tool can see at any moment, rather than only during
the two windows the old code happened to look in.

The consumers (the send gate, the auto-copy flow) are tested where they live,
in tests/tui - here they are deliberately absent, because the point is that
this module cannot see them.
"""

from __future__ import annotations

import random

from agentclip.screen.busy import BusyState
from agentclip.screen.capture import RegionImage
from agentclip.screen.detector import (
    RUNTIME_KINDS,
    ScreenDetector,
    build_detector,
)
from agentclip.screen.presence import PresenceTracker
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.stale import StaleState
from agentclip.screen.template import Template

REGION = ScreenRegion(100, 50, 140, 90)


def noise(width: int, height: int, seed: int = 1) -> RegionImage:
    rng = random.Random(seed)
    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes((rng.randrange(256), rng.randrange(256), rng.randrange(256), 0))
    return RegionImage(width, height, bytes(pixels))


def paste(scene: RegionImage, patch: RegionImage, x: int, y: int) -> RegionImage:
    pixels = bytearray(scene.pixels)
    row = patch.width * 4
    for ty in range(patch.height):
        start = ((y + ty) * scene.width + x) * 4
        pixels[start : start + row] = patch.pixels[ty * row : (ty + 1) * row]
    return RegionImage(scene.width, scene.height, bytes(pixels))


SEND_BUTTON = noise(20, 16, seed=2)
COPY_ICON = noise(18, 14, seed=3)
BUSY_ICON = noise(16, 12, seed=4)
EMPTY = noise(REGION.width, REGION.height, seed=1)
SEND_ON_SCREEN = paste(EMPTY, SEND_BUTTON, 60, 40)
COPY_ON_SCREEN = paste(EMPTY, COPY_ICON, 20, 70)
BOTH_ON_SCREEN = paste(SEND_ON_SCREEN, COPY_ICON, 20, 70)
BUSY_ON_SCREEN = paste(EMPTY, BUSY_ICON, 10, 10)


def profile_with(**kinds: RegionImage) -> ServiceProfile:
    """A service profile holding one capture of each named kind."""
    profile = ServiceProfile(key="test")
    for name, image in kinds.items():
        profile.put(TemplateKind(name.replace("_", "-")), image)
    return profile


class Clock:
    """A hand-cranked monotonic clock, so timestamps are assertable."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# -- what gets searched -------------------------------------------------------


def test_every_calibrated_kind_is_searched_on_every_frame() -> None:
    """The whole point. No gate, no flow and no session state can reach in
    here, so there is nothing that could make a calibrated kind skip a frame."""
    detector = build_detector(
        REGION,
        profile_with(busy=BUSY_ICON, send_ready=SEND_BUTTON, copy=COPY_ICON),
        signals=("busy", "stale"),
        required_ticks=2,
    )
    assert detector.searched_kinds == (
        TemplateKind.SEND_READY,
        TemplateKind.BUSY,
        TemplateKind.COPY,
    )

    for _ in range(3):
        tick = detector.observe(BOTH_ON_SCREEN)
        assert tick.searched(TemplateKind.SEND_READY)
        assert tick.searched(TemplateKind.COPY)
        assert tick.present(TemplateKind.SEND_READY) is True
        assert tick.present(TemplateKind.COPY) is True
        assert tick.present(TemplateKind.BUSY) is False


def test_an_uncalibrated_kind_is_never_searched_and_says_nothing() -> None:
    """The third state of the contract: absent from the map, so the column's
    row for it keeps whatever it last said instead of being blanked."""
    detector = build_detector(
        REGION, profile_with(send_ready=SEND_BUTTON), signals=("stale",), required_ticks=2
    )
    tick = detector.observe(EMPTY)

    assert detector.searches(TemplateKind.SEND_READY)
    assert not detector.searches(TemplateKind.COPY)
    assert tick.searched(TemplateKind.SEND_READY)
    assert not tick.searched(TemplateKind.COPY)
    assert tick.present(TemplateKind.COPY) is None


def test_a_ticked_signal_with_no_capture_searches_for_nothing() -> None:
    """Both halves of busy/idle calibration are required: a checklist entry is
    a wish, and a wish with no picture behind it is not a detector."""
    detector = build_detector(REGION, profile_with(), signals=("busy", "idle"), required_ticks=2)

    assert detector.active_detectors == ()
    assert detector.searched_kinds == ()
    assert detector.watching is False


def test_a_captured_appearance_the_checklist_does_not_tick_stays_out() -> None:
    detector = build_detector(
        REGION, profile_with(busy=BUSY_ICON), signals=("stale",), required_ticks=2
    )

    assert detector.active_detectors == ("stale",)
    assert not detector.searches(TemplateKind.BUSY)


def test_send_and_copy_alone_are_still_worth_a_poll_loop() -> None:
    """No finish detection at all, but two calibrated appearances: the user has
    something to be shown, and ``watching`` is the question a caller asks about
    that - ``active_detectors`` answers only whether anything can FINISH."""
    detector = build_detector(
        REGION, profile_with(send_ready=SEND_BUTTON, copy=COPY_ICON), signals=(), required_ticks=2
    )

    assert detector.active_detectors == ()
    assert detector.watching is True
    assert detector.observe(EMPTY).present(TemplateKind.COPY) is False


def test_the_order_it_reports_in_is_the_columns_order() -> None:
    detector = build_detector(
        REGION,
        profile_with(busy=BUSY_ICON, idle=SEND_BUTTON, send_ready=SEND_BUTTON, copy=COPY_ICON),
        signals=("busy", "idle", "stale"),
        required_ticks=2,
    )
    assert detector.searched_kinds == RUNTIME_KINDS
    assert detector.active_detectors == ("busy", "idle", "stale")


# -- one frame, one answer ----------------------------------------------------


def test_one_frame_feeds_every_detector() -> None:
    detector = build_detector(
        REGION,
        profile_with(busy=BUSY_ICON, send_ready=SEND_BUTTON),
        signals=("busy", "stale"),
        required_ticks=2,
    )
    detector.observe(EMPTY)
    tick = detector.observe(BUSY_ON_SCREEN)

    assert tick.busy is not None and tick.busy.state is BusyState.MATCH
    assert tick.stale is not None and tick.stale.state is StaleState.CHANGING
    assert tick.idle is None  # not built, so it reports nothing at all
    assert tick.present(TemplateKind.BUSY) is True
    assert tick.present(TemplateKind.SEND_READY) is False


def test_a_failed_capture_is_an_error_everywhere_and_evidence_nowhere() -> None:
    """A dropped frame is not a button going away: every tracker hears ERROR,
    the streaks are intact, and nothing is claimed about any appearance."""
    detector = build_detector(
        REGION,
        profile_with(busy=BUSY_ICON, send_ready=SEND_BUTTON),
        signals=("busy", "stale"),
        required_ticks=2,
    )
    detector.observe(SEND_ON_SCREEN)

    tick = detector.observe(None)

    assert tick.captured is False
    assert tick.busy is not None and tick.busy.state is BusyState.ERROR
    assert tick.stale is not None and tick.stale.state is StaleState.ERROR
    assert dict(tick.sightings) == {}
    assert tick.present(TemplateKind.SEND_READY) is None


def test_the_busy_tracker_is_not_asked_to_search_twice() -> None:
    """The presence trackers already scan the frame, so the detector reads
    their sighting back instead of paying for a second scan of the same kind."""
    scans = 0
    real = PresenceTracker((Template.build(BUSY_ICON),), found_is_busy=True)

    class Counting(PresenceTracker):
        def _find(self, scene: RegionImage) -> object:  # type: ignore[override]
            nonlocal scans
            scans += 1
            return real._find(scene)  # noqa: SLF001

    detector = ScreenDetector(
        REGION, busy=Counting((Template.build(BUSY_ICON),), found_is_busy=True)
    )
    tick = detector.observe(BUSY_ON_SCREEN)

    assert scans == 1
    assert tick.found(TemplateKind.BUSY) is not None


# -- what it detected, and where ----------------------------------------------


def test_a_sighting_says_where_on_the_real_screen() -> None:
    detector = build_detector(
        REGION, profile_with(send_ready=SEND_BUTTON), signals=(), required_ticks=2
    )
    tick = detector.observe(SEND_ON_SCREEN)

    sighting = tick.found(TemplateKind.SEND_READY)
    assert sighting is not None
    assert (sighting.match.x, sighting.match.y) == (60, 40)
    # Scene-local becomes absolute: the region's own corner plus the match.
    assert sighting.rect(REGION) == ScreenRegion(160, 90, 20, 16)
    assert detector.locate(TemplateKind.SEND_READY) == ScreenRegion(160, 90, 20, 16)


def test_the_memory_outlives_the_frame_it_was_seen_in() -> None:
    """"Last seen 8 seconds ago" and "never seen at all" are different
    diagnoses of a harvest that failed, and only the memory tells them apart."""
    clock = Clock()
    detector = build_detector(
        REGION, profile_with(copy=COPY_ICON), signals=(), required_ticks=2, clock=clock
    )
    assert detector.last_seen(TemplateKind.COPY) is None
    assert detector.seen_ago(TemplateKind.COPY) is None

    detector.observe(COPY_ON_SCREEN)
    clock.now += 8.0
    detector.observe(EMPTY)

    assert detector.latest is not None
    assert detector.latest.found(TemplateKind.COPY) is None  # not on screen NOW
    seen = detector.last_seen(TemplateKind.COPY)
    assert seen is not None and seen.at == 100.0
    assert detector.seen_ago(TemplateKind.COPY) == 8.0


def test_resetting_forgets_the_debounce_and_keeps_the_memory() -> None:
    """The trackers' streaks describe frames AgentClip itself produced (a paste,
    a scroll); the record of what was on screen is not un-seen by any of that."""
    clock = Clock()
    detector = build_detector(
        REGION,
        profile_with(busy=BUSY_ICON, copy=COPY_ICON),
        signals=("busy",),
        required_ticks=2,
        clock=clock,
    )
    detector.observe(paste(COPY_ON_SCREEN, BUSY_ICON, 10, 10))
    assert detector.busy is not None and detector.busy.last_sighting is not None

    detector.reset()

    assert detector.busy.last_sighting is None
    assert detector.last_seen(TemplateKind.COPY) is not None


def test_the_latest_snapshot_is_the_whole_last_frame() -> None:
    detector = build_detector(
        REGION, profile_with(send_ready=SEND_BUTTON), signals=("stale",), required_ticks=2
    )
    assert detector.latest is None

    first = detector.observe(EMPTY)
    assert detector.latest is first
    second = detector.observe(SEND_ON_SCREEN)
    assert detector.latest is second
    assert first.present(TemplateKind.SEND_READY) is False


def test_a_stack_of_pictures_of_one_control_is_ored() -> None:
    """A greyed-out send button is still the send button: any variant on screen
    is the same answer, which is what keeps the gate armed mid-upload."""
    profile = profile_with(send_ready=SEND_BUTTON)
    profile.put(TemplateKind.SEND_READY, COPY_ICON)  # the "greyed" second picture
    detector = build_detector(REGION, profile, signals=(), required_ticks=2)

    assert detector.observe(COPY_ON_SCREEN).present(TemplateKind.SEND_READY) is True
