"""What does the tool think it can see in this chat window, and where?

The `/identify` command's compute half, and the calibration aid the whole
recognition model was missing. Everything AgentClip does to a browser - clicking
the input box, waiting for the stop button to go, harvesting the copy icon -
rests on searching a service's captured appearances inside one drawn rectangle
(screen.template, screen.profile, screen.slot). When that goes wrong the user
sees a symptom ("the paste landed in the wrong box", "the copy never fired") and
has no way to ask the question underneath it: *what did you actually match, and
where?* This module answers exactly that, and screen.overlay draws the answer on
the screen it is about.

Deliberately a pure function of its inputs - a rectangle, a profile, one already
captured frame - with no capture, no ctypes, no tkinter and no OS, in the style
of screen.template and screen.slot. The capture happens once, in the caller,
*before* any overlay exists: an overlay in the frame would be identified as part
of the chat window.

The same list is also the wire format the drawing child process is fed
(:func:`format_payload` / :func:`parse_payload`), because the overlay cannot
live in the TUI's process - see screen.picker for why.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.template import find_all_in_region, match_rect, same_element

# What the drawn chat region itself is called in the list. It is not a
# TemplateKind (nothing was matched to find it - the user drew it), and it is
# reported anyway because half of what goes wrong is the box being in the wrong
# place: a listing of buttons inside a rectangle nobody can see is unreadable.
CHAT_REGION_LABEL = "chat region"

# Matches of one appearance worth reporting. The same handful ``_find_all`` uses
# for the automation's "one, or more than one?" question - past a few boxes of
# one kind the screen is unreadable anyway, and each extra candidate is a full
# pixel comparison.
MAX_MATCHES = 8


@dataclass(frozen=True, slots=True)
class IdentifiedElement:
    """One thing the tool believes it can see, in absolute screen pixels.

    ``label`` is the TemplateKind's own value (``"copy"``, ``"new-chat"``) so the
    box on screen carries the name the service editor captured it under, rather
    than a prettified one the user would have to translate back.

    ``diff`` is how much of the sampled appearance differed at that spot: 0.0 is
    pixel-perfect and the kind's ``max_diff`` is the worst that still counts as a
    match, so it reads as "how sure" - a copy button at 0.07 against a threshold
    of 0.08 is a calibration about to fail intermittently, which is precisely the
    thing this overlay exists to make visible. None for the drawn chat region,
    which was not matched at all.
    """

    label: str
    rect: ScreenRegion
    diff: float | None = None

    def describe(self) -> str:
        """The text drawn on the box: ``copy 0.013``, or a bare name."""
        return self.label if self.diff is None else f"{self.label} {self.diff:.3f}"


def identify_elements(
    region: ScreenRegion, profile: ServiceProfile, scene: RegionImage
) -> list[IdentifiedElement]:
    """Every appearance ``profile`` can find in ``scene``, as screen rectangles.

    ``scene`` must be a capture of ``region`` - that is what makes a scene-local
    match translatable back to absolute coordinates (``match_rect``).

    Every kind, every captured variant of it, every match of each: the debug
    question is the opposite of the automation's, which stops as soon as it knows
    enough to click. Near-duplicate hits on one physical element are folded away
    per kind exactly as ``MainScreen._find_all`` folds them (``same_element``), so
    two boxes of one kind really do mean two things on screen - which is the
    misconfiguration (a second window inside the drawn one) most worth seeing.
    Across kinds nothing is folded: a send button that also matches the busy
    indicator is a genuine finding, and hiding one of the two boxes would hide it.

    The drawn region is always the first entry, so the list is never empty and
    the overlay always shows where the tool was looking.
    """
    elements = [IdentifiedElement(CHAT_REGION_LABEL, region)]
    for kind in TemplateKind:
        templates = profile.variants(kind)
        if not templates:
            continue
        found = [
            (template, match)
            for template in templates
            for match in find_all_in_region(
                template, scene, max_diff=kind.max_diff, limit=MAX_MATCHES
            )
        ]
        # Reading order across the whole union before the fold, so which variant
        # happened to be searched first cannot change which rectangle survives as
        # a duplicate's representative (screen.profile's variant stacks).
        found.sort(key=lambda pair: (pair[1].y, pair[1].x))
        kept: list[IdentifiedElement] = []
        for template, match in found:
            rect = match_rect(region, template, match)
            if any(same_element(rect, other.rect) for other in kept):
                continue
            kept.append(IdentifiedElement(kind.value, rect, match.diff))
        elements.extend(kept)
    return elements


def summarise(elements: Sequence[IdentifiedElement]) -> str:
    """One line for the toast: what was found, counted by kind.

    The chat region is excluded from the count and from the "nothing" verdict:
    it is always there (the user drew it), so counting it would turn the empty
    result - the one this command is most often run to diagnose - into "1
    element".
    """
    found = [element for element in elements if element.label != CHAT_REGION_LABEL]
    if not found:
        return (
            "identified nothing inside the chat window - only the drawn region is boxed; "
            "capture the service's appearances (F2) or redraw the window"
        )
    counts = Counter(element.label for element in found)
    listing = ", ".join(f"{label}×{count}" for label, count in counts.items())
    return f"identified {len(found)} elements: {listing}"


# -- the wire format ----------------------------------------------------------
#
# JSON on the drawing child's stdin. A rectangle list is too big for argv (eight
# kinds' worth of boxes) and stdout is already spoken for by the picker's own
# wire format, so the two children stay symmetrical: arguments in, result out.


def format_payload(elements: Iterable[IdentifiedElement]) -> str:
    """The element list as the child process reads it."""
    return json.dumps(
        {
            "elements": [
                {
                    "label": element.label,
                    "left": element.rect.left,
                    "top": element.rect.top,
                    "width": element.rect.width,
                    "height": element.rect.height,
                    "diff": element.diff,
                }
                for element in elements
            ]
        }
    )


def parse_payload(text: str) -> list[IdentifiedElement]:
    """The wire format back into elements.

    Raises ValueError on anything that is not this format - the child turns that
    into a one-line stderr message and a non-zero exit, which the parent reports
    rather than leaving a blank overlay on the user's screen.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"identify payload is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise ValueError("identify payload has no element list")
    elements: list[IdentifiedElement] = []
    for entry in data["elements"]:
        if not isinstance(entry, dict):
            raise ValueError("identify payload entry is not an object")
        try:
            rect = ScreenRegion(
                int(entry["left"]), int(entry["top"]), int(entry["width"]), int(entry["height"])
            )
            diff = entry.get("diff")
            elements.append(
                IdentifiedElement(str(entry["label"]), rect, None if diff is None else float(diff))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"identify payload entry is malformed: {exc}") from exc
    return elements
