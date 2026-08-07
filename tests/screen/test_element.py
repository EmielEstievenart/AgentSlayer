"""CalibratedElement: "is this still the thing the user pointed at?"

``matches`` is pure, so it is tested directly; ``probe_element`` only adds the
capture, which is monkeypatched at its use site (agentclip.screen.element).
"""

from __future__ import annotations

import pytest

from agentclip.screen import element as element_mod
from agentclip.screen.busy import DEFAULT_TOLERANCE
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.element import DEFAULT_MAX_DIFF, CalibratedElement, probe_element
from agentclip.screen.region import ScreenRegion

REGION = ScreenRegion(400, 900, 320, 48)


def solid(width: int, height: int, colour: tuple[int, int, int] = (0, 0, 0)) -> RegionImage:
    """A uniformly coloured BGRX frame (the X byte is left at 0)."""
    blue, green, red = colour
    return RegionImage(width, height, bytes((blue, green, red, 0)) * (width * height))


def element(colour: tuple[int, int, int] = (30, 30, 30)) -> CalibratedElement:
    return CalibratedElement(REGION, solid(REGION.width, REGION.height, colour))


def test_the_same_pixels_match() -> None:
    assert element().matches(solid(REGION.width, REGION.height, (30, 30, 30))) is True


def test_wholly_different_pixels_do_not_match() -> None:
    """The fresh-chat box replaced by the ongoing layout: nothing survives."""
    assert element().matches(solid(REGION.width, REGION.height, (240, 240, 240))) is False


def test_anti_aliasing_noise_still_matches() -> None:
    """A caret blink and a hover tint must not make a chat input box a
    different chat input box."""
    current = solid(REGION.width, REGION.height, (30 + DEFAULT_TOLERANCE, 30 - 5, 30 + 2))
    assert element().matches(current) is True


def test_a_small_patch_of_change_still_matches() -> None:
    """The threshold is looser than the busy detector's on purpose: a typed
    character or a placeholder going away is a few percent of the box."""
    pixels = bytearray(solid(REGION.width, REGION.height, (30, 30, 30)).pixels)
    changed = int(REGION.width * REGION.height * (DEFAULT_MAX_DIFF / 2))
    pixels[: changed * 4] = b"\xff\xff\xff\x00" * changed
    current = RegionImage(REGION.width, REGION.height, bytes(pixels))
    assert element().matches(current) is True


def test_a_differently_sized_frame_never_matches() -> None:
    """A rescaled or re-drawn window is not the element that was calibrated."""
    assert element().matches(solid(REGION.width, REGION.height + 1, (30, 30, 30))) is False


def test_thresholds_are_overridable() -> None:
    current = solid(REGION.width, REGION.height, (240, 240, 240))
    assert element().matches(current, max_diff=1.0) is True
    assert element().matches(current, tolerance=255) is True


def test_describe_reports_the_region() -> None:
    assert element().describe() == REGION.describe()


def test_frozen_and_slotted() -> None:
    """Same house style as the other screen dataclasses - these get stashed on
    the screen for a whole session, so nobody gets to mutate one in place."""
    calibrated = element()
    with pytest.raises(AttributeError):
        calibrated.region = REGION  # type: ignore[misc]


# -- probe_element: the same question, asked of the live screen ----------------


def test_probe_captures_and_compares(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[ScreenRegion] = []

    def capture(region: ScreenRegion) -> RegionImage:
        captured.append(region)
        return solid(REGION.width, REGION.height, (30, 30, 30))

    monkeypatch.setattr(element_mod, "capture_region", capture)
    assert probe_element(element()) is True
    assert captured == [REGION]


def test_probe_reports_a_changed_element(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        element_mod,
        "capture_region",
        lambda region: solid(REGION.width, REGION.height, (250, 0, 0)),
    )
    assert probe_element(element()) is False


def test_a_failed_capture_reads_as_not_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers use this to decide whether it is safe to click - a screen we
    cannot see is not one to click blind."""

    def boom(region: ScreenRegion) -> RegionImage:
        raise CaptureError("no display")

    monkeypatch.setattr(element_mod, "capture_region", boom)
    assert probe_element(element()) is False
