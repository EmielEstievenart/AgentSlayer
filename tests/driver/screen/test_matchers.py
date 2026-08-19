"""The pluggable half of a search: two ways to propose origins, one verdict.

The cases here are what "pluggable" has to mean if one tolerance setting is
going to govern both backends honestly. So: the selector's fallback when cv2 is
absent, the two backends agreeing about the diff at an origin they both find,
and the one place they genuinely differ - the residual 17-24 band the two-ruler
anchor fix left open, which the exhaustive sweep closes. That last one is the
whole reason the OpenCV backend exists, so it is pinned against the real Gemini
fixture rather than against noise.

The OpenCV cases skip cleanly where cv2 is not installed (it is an optional
extra: `pip install agentclip[cv]`); the fallback case runs everywhere, because
the graceful degradation is exactly what a machine without it needs to get.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip import config as config_module
from agentclip.driver.screen import matchers as matchers_module
from agentclip.driver.screen import template as template_module
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.matchers import (
    MATCHER_ANCHORS,
    MATCHER_OPENCV,
    MATCHERS,
    opencv_available,
    opencv_origins,
    select_matcher,
)
from agentclip.driver.screen.png import decode_png
from agentclip.driver.screen.template import (
    DEFAULT_TOLERANCE,
    RegionMatch,
    Template,
    find_in_region,
    match_at_xy,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BUTTON_AT = (20, 68)
SIDEBAR = (31, 31, 31)
NEW_CHAT_MAX_DIFF = 0.10

needs_cv2 = pytest.mark.skipif(not opencv_available(), reason="the cv extra is not installed")


def frame() -> RegionImage:
    return decode_png((FIXTURES / "gemini-sidebar-frame.png").read_bytes())


def crop() -> RegionImage:
    return decode_png((FIXTURES / "gemini-new-chat-crop.png").read_bytes())


def retinted(image: RegionImage, delta: int) -> RegionImage:
    """``image`` with the flat sidebar surface moved by ``delta``.

    The same instrument as test_template_flat_background, and the same reason:
    it isolates a change to the background shade, which is what the anchors'
    remaining blind spot is about.
    """
    shade = SIDEBAR[0] + delta
    pixels = bytearray(image.pixels)
    for index in range(image.width * image.height):
        at = index * 4
        if tuple(pixels[at : at + 3]) == SIDEBAR:
            pixels[at : at + 3] = bytes((shade, shade, shade))
    return RegionImage(image.width, image.height, bytes(pixels))


# -- the two names, in two layers that may not import each other ---------------


def test_the_config_leaf_and_the_screen_layer_agree_on_the_backend_names() -> None:
    """config.py is a stdlib-only leaf and may not import the screen layer
    (architecture.md 0), so the two names are spelled out twice. This is the
    seam that stops the copies drifting - a matcher config.py would accept and
    screen.matchers has never heard of would fall back silently forever."""
    assert MATCHERS == config_module.MATCHERS
    assert (MATCHER_ANCHORS, MATCHER_OPENCV) == (
        config_module.MATCHER_ANCHORS,
        config_module.MATCHER_OPENCV,
    )
    assert matchers_module.DEFAULT_MATCHER == config_module.DEFAULT_MATCHER


def test_the_configurable_tolerance_starts_where_the_matcher_always_did() -> None:
    """A user who never touches the slider must get exactly the search that
    shipped, so the config default and the template default are one number."""
    assert config_module.DEFAULT_TOLERANCE == DEFAULT_TOLERANCE
    assert config_module.TOLERANCE_MIN <= DEFAULT_TOLERANCE <= config_module.TOLERANCE_MAX


# -- choosing a backend --------------------------------------------------------


def test_the_default_and_every_unknown_name_select_the_built_in_anchors() -> None:
    for name in ("anchors", "", "sift", "OPENCV"):
        chosen = select_matcher(name)
        assert chosen.name == MATCHER_ANCHORS
        assert chosen.origins is template_module._candidate_origins
        assert not chosen.fell_back


def test_asking_for_opencv_without_it_installed_falls_back_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graceful degradation, which is the only behaviour a machine without
    the extra ever sees: a working search, plus a flag the editor turns into a
    warning. A backend that quietly searched for nothing would be indis-
    tinguishable from a chat window with no buttons in it."""
    monkeypatch.setattr(matchers_module, "opencv_available", lambda: False)
    chosen = select_matcher(MATCHER_OPENCV)
    assert chosen.name == MATCHER_ANCHORS
    assert chosen.requested == MATCHER_OPENCV
    assert chosen.fell_back
    # And it is a real search, not a stub that finds nothing.
    assert find_in_region(
        Template.build(crop()), frame(), max_diff=NEW_CHAT_MAX_DIFF, matcher=chosen.origins
    ) == RegionMatch(*BUTTON_AT, 0.0)


@needs_cv2
def test_asking_for_opencv_with_it_installed_gets_the_sweep() -> None:
    chosen = select_matcher(MATCHER_OPENCV)
    assert chosen.name == MATCHER_OPENCV
    assert not chosen.fell_back
    assert chosen.origins is opencv_origins


# -- the sweep itself ----------------------------------------------------------


@needs_cv2
def test_the_sweep_finds_the_new_chat_button_on_the_frame_it_came_from() -> None:
    assert find_in_region(
        Template.build(crop()),
        frame(),
        max_diff=NEW_CHAT_MAX_DIFF,
        matcher=opencv_origins,
    ) == RegionMatch(*BUTTON_AT, 0.0)


@needs_cv2
def test_the_true_origin_is_among_the_proposed_peaks() -> None:
    """Exhaustiveness is the property being bought, and it lives here rather
    than in the verification: the correlation surface scores every origin, and
    the shortlist must not lose the one that matters. Local maxima rather than
    a raw top-N is what makes that true - the best 256 scores on a correlation
    surface are routinely 256 origins on one hill."""
    origins = opencv_origins(Template.build(crop()), frame())
    assert BUTTON_AT in origins
    assert len(origins) <= matchers_module.MAX_PEAKS
    assert len(set(origins)) == len(origins)


@needs_cv2
def test_the_peaks_arrive_bottom_most_first_like_the_anchor_sweep() -> None:
    """Which origins to compare is the sweep's business; the ORDER they are
    spent in belongs to screen.template, whose budget and whose
    ``find_lowest_in_region`` are both written around bottom-most-first."""
    origins = opencv_origins(Template.build(crop()), frame())
    assert origins == sorted(origins, key=lambda o: (-o[1], -o[0]))


@needs_cv2
def test_the_two_backends_agree_on_the_diff_where_they_both_find_it() -> None:
    """The claim one tolerance slider rests on. Candidate generation differs;
    the comparison does not, so the number the ELEMENTS column shows means the
    same thing whichever backend produced it."""
    template, scene = Template.build(crop()), frame()
    anchors = find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF)
    sweep = find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF, matcher=opencv_origins)
    assert anchors == sweep
    assert sweep is not None
    assert sweep.diff == match_at_xy(crop(), scene, *BUTTON_AT, tolerance=DEFAULT_TOLERANCE)


@needs_cv2
def test_the_sweep_closes_the_residual_band_the_two_rulers_left_open() -> None:
    """The selling point, against the real capture that motivated it.

    A background shift of 17-24 lands on an edge of BOTH quantisation rulers,
    so the anchors stop proposing the button's origin while the pixel
    comparison would still call it perfect (test_template_flat_background). A
    correlation sweep has no fingerprint to damage - and TM_CCOEFF_NORMED
    subtracts each window's mean, which is exactly what a uniform surface tint
    is - so the origin is proposed and the shared verification does the rest.
    """
    template, patch = Template.build(crop()), crop()
    for delta in (17, 20, 24):
        scene = retinted(frame(), delta)
        assert match_at_xy(patch, scene, *BUTTON_AT, tolerance=DEFAULT_TOLERANCE) == 0.0
        assert find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF) is None, (
            f"the anchors were expected to miss at {delta:+d}"
        )
        assert find_in_region(
            template, scene, max_diff=NEW_CHAT_MAX_DIFF, matcher=opencv_origins
        ) == RegionMatch(*BUTTON_AT, 0.0), f"the sweep lost the button at {delta:+d}"


@needs_cv2
def test_the_sweep_still_refuses_what_the_comparison_refuses() -> None:
    """Exhaustive candidate generation is not a looser threshold. A shift big
    enough to fail the per-channel tolerance fails for both backends, because
    both are judged by the same comparison - if this ever passed, the slider
    would be governing one backend and not the other."""
    scene = retinted(frame(), 40)  # well past DEFAULT_TOLERANCE
    template = Template.build(crop())
    assert match_at_xy(crop(), scene, *BUTTON_AT, tolerance=DEFAULT_TOLERANCE) > 0.8
    assert find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF) is None
    assert (
        find_in_region(template, scene, max_diff=NEW_CHAT_MAX_DIFF, matcher=opencv_origins) is None
    )


@needs_cv2
def test_a_scene_too_small_to_hold_the_template_proposes_nothing() -> None:
    """A resized browser window is a normal runtime condition, and the sweep
    answers it the same way ``_candidate_origins`` does - empty, not an
    exception. This runs on a poll timer."""
    tiny = RegionImage(10, 10, bytes(10 * 10 * 4))
    assert opencv_origins(Template.build(crop()), tiny) == []


def test_a_broken_cv2_reads_as_an_empty_proposal_rather_than_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel that imports and then explodes (a missing MSVC runtime is the
    classic) must not take a poll tick down with it. Empty is the honest answer
    and the caller already handles it."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cv2 is having a bad day")

    monkeypatch.setattr(matchers_module, "_bgr_array", explode)
    assert opencv_origins(Template.build(crop()), frame()) == []
