"""Two ways to guess where an appearance might be, and one way to decide.

``screen.template`` splits a search in half: propose origins worth comparing,
then compare them properly. This module is the first half made pluggable, and
the second half is deliberately NOT here - the sniff test, the strided
per-channel comparison, the tolerance and the diff on the ``RegionMatch`` are
shared by every source. That is the whole point. Two backends that disagree
about *where to look* still agree about *what a match is*, so one tolerance
setting governs both, the number in the ELEMENTS column means the same thing
whichever is running, and a user can switch between them and compare the
result instead of re-learning the readout.

**anchors** is the built-in one, described at length in ``screen.template``:
eight short quantised byte runs per ruler, located at C speed with
``bytes.rfind``. It is fast, needs nothing installed, and is a FINGERPRINT
search - it finds what it recognises, and what it does not recognise it does
not propose at all. The two-ruler fix narrowed that blind spot to a background
shift of 17-24 units; it did not remove it, and it cannot, because an exact
byte search over a quantised plane will always have edges somewhere.

**opencv** is the exhaustive one: ``cv2.matchTemplate`` correlates the template
against every single origin in the scene, then the peaks of that correlation
become the candidates. There is no fingerprint to damage - a shade that has
drifted, a hover tint, a re-blended anti-aliased edge all just lower the
correlation slightly - so the class of failure the anchors have is structurally
absent. TM_CCOEFF_NORMED specifically subtracts each window's mean before
correlating, which is exactly the invariance the background-tint case needs.
It costs a compiled dependency and roughly a full sweep of the scene per
template per frame, which is why it is opt-in per service rather than the
default.

Two things are load-bearing about how this is wired:

* **cv2 and numpy are imported inside the function.** The screen layer is
  stdlib-only at module level (architecture.md 1, enforced by
  tests/test_layering.py), and AgentClip ships as a PyInstaller one-file build
  that must not carry 60MB of compiled wheels for a feature most users leave
  off. The import cost is paid on the first frame of a search and cached by
  ``sys.modules`` after that.
* **A missing cv2 falls back to the anchors, loudly.** :func:`select_matcher`
  returns a source that still works and reports which one the caller actually
  got, so the editor can say "OpenCV is not installed" next to the radio button
  the user just pressed. A backend that silently searched for nothing would
  look exactly like a chat window with no buttons in it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.template import (
    MAX_VERIFICATIONS,
    CandidateSource,
    Template,
    _candidate_origins,
)

# The two names a service's ``matcher`` setting can hold. Duplicated as plain
# strings in config.py, which may not import this layer - config is a leaf and
# the screen layer is an OS layer. ``tests/driver/screen/test_matchers.py`` asserts the
# two lists are the same, so the duplication cannot drift.
MATCHER_ANCHORS = "anchors"
MATCHER_OPENCV = "opencv"
MATCHERS: tuple[str, ...] = (MATCHER_ANCHORS, MATCHER_OPENCV)
DEFAULT_MATCHER = MATCHER_ANCHORS

# How many correlation peaks are handed on for verification. The same number as
# MAX_VERIFICATIONS, because that is the ceiling on how many of them can be
# fully compared anyway - proposing more would be work nothing can spend.
MAX_PEAKS = MAX_VERIFICATIONS
# How close two correlation peaks may be before they are treated as one. A
# strong match is not a spike but a hill - every origin within a pixel or two
# of it also correlates well - and without suppression the whole peak list is
# one button read 256 times, with the real second occurrence crowded off the
# end. Half the template's own size: two matches closer than that overlap, and
# ``same_element`` already calls overlapping rectangles one element.
_PEAK_SPACING = 2


@dataclass(frozen=True, slots=True)
class Matcher:
    """A named way of proposing candidate origins.

    ``name`` is what the user actually got, which is not always what they asked
    for: selecting "opencv" on a machine without cv2 yields this object with
    ``name == "anchors"`` and ``requested == "opencv"``, and the difference is
    what the editor's warning line is built from.
    """

    name: str
    origins: CandidateSource
    requested: str = ""

    @property
    def fell_back(self) -> bool:
        """Did the user ask for something this machine could not give them?"""
        return bool(self.requested) and self.requested != self.name


def opencv_available() -> bool:
    """Can this machine run the OpenCV backend at all?

    A real import rather than ``importlib.util.find_spec``: a cv2 wheel that is
    present but broken (a missing MSVC runtime is the classic one on Windows)
    imports its way to an exception, and for every purpose here that is the
    same answer as not being installed.
    """
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def _bgr_array(image: RegionImage):  # type: ignore[no-untyped-def]
    """One captured frame as an (h, w, 3) uint8 array of B, G, R.

    The X byte is dropped rather than zeroed: it is undefined in a GDI capture
    (``screen.capture``), and a channel of garbage would be a quarter of the
    correlation. ``ascontiguousarray`` because the slice that drops it is a
    strided view and cv2 wants a real buffer.
    """
    import numpy as np

    count = image.width * image.height
    flat = np.frombuffer(image.pixels[: count * 4], dtype=np.uint8)
    return np.ascontiguousarray(flat.reshape(image.height, image.width, 4)[:, :, :3])


def opencv_origins(
    template: Template, scene: RegionImage, *, limit: int = MAX_PEAKS
) -> list[tuple[int, int]]:
    """Candidate origins from a full correlation sweep, bottom-most first.

    Exhaustive where the anchors are selective: ``cv2.matchTemplate`` scores
    EVERY origin the scene can hold, so the true match is always somewhere in
    that surface and the only question is whether it survives being reduced to
    a shortlist. Three steps do the reducing, and each is about not losing it:

    1. **Local maxima** (a dilation compared against the original) rather than
       a global top-N. A correlation surface is hills, not spikes, and the top
       256 raw scores are routinely 256 origins on ONE hill - which would push
       every other occurrence in the scene off the list.
    2. **The best ``limit`` of those hills**, by score. Anything further down
       cannot be verified anyway (MAX_VERIFICATIONS).
    3. **Sorted bottom-most first**, which is the order the rest of
       ``screen.template`` is written around: it is the answer
       ``find_lowest_in_region`` wants outright, and it is what makes the
       verification budget get spent at the bottom of a chat where the controls
       live. Ranking by score chose WHICH origins; it is not the order they are
       spent in.

    Returns an empty list for anything it cannot answer - a scene too small to
    hold the template, a truncated buffer, or a cv2 that will not import - so
    it is a drop-in for ``_candidate_origins``, which is empty in those cases
    too. Never raises: a poll timer is not a place to discover that a wheel is
    broken.
    """
    image = template.image
    if scene.width < image.width or scene.height < image.height:
        return []
    if len(scene.pixels) < scene.width * scene.height * 4:
        return []
    if len(image.pixels) < image.width * image.height * 4:
        return []
    try:
        import cv2
        import numpy as np

        surface = cv2.matchTemplate(_bgr_array(scene), _bgr_array(image), cv2.TM_CCOEFF_NORMED)
        # A template with no variance at all (a solid colour) correlates to
        # 0/0. NaN would sort unpredictably and propose nonsense; zero is the
        # honest score for "this carries no information".
        surface = np.nan_to_num(surface, nan=0.0, posinf=0.0, neginf=0.0)
        window = (
            max(_PEAK_SPACING, image.height // 2),
            max(_PEAK_SPACING, image.width // 2),
        )
        peaks = cv2.dilate(surface, np.ones(window, dtype=np.uint8))
        ys, xs = np.nonzero(surface >= peaks)
        if ys.size == 0:
            return []
        scores = surface[ys, xs]
        if scores.size > limit:
            keep = np.argpartition(-scores, limit - 1)[:limit]
            ys, xs, scores = ys[keep], xs[keep], scores[keep]
        # Bottom-most first, then right-to-left, matching the anchor sweep's
        # rfind order so both backends spend the budget in the same place.
        order = np.lexsort((-xs, -ys))
        return [(int(xs[i]), int(ys[i])) for i in order]
    except Exception:
        return []


ANCHOR_MATCHER = Matcher(MATCHER_ANCHORS, _candidate_origins)


def select_matcher(prefer: str = DEFAULT_MATCHER) -> Matcher:
    """The candidate source a service asked for, or the one it can have.

    Unknown names behave like the default (config validates upstream and warns;
    this layer is reached by other callers too and must not raise for a typo).
    "opencv" on a machine with no working cv2 comes back as the anchor matcher
    with ``requested="opencv"`` still set, which is the whole reason this
    returns an object rather than a bare callable: "you are running anchors"
    and "you asked for OpenCV and did not get it" are different things to tell
    a user, and only the second one needs a warning next to a radio button.
    """
    if prefer != MATCHER_OPENCV:
        return ANCHOR_MATCHER
    if not opencv_available():
        return Matcher(MATCHER_ANCHORS, _candidate_origins, requested=MATCHER_OPENCV)
    return Matcher(MATCHER_OPENCV, opencv_origins, requested=MATCHER_OPENCV)
