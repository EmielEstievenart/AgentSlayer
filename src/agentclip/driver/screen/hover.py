"""Where to park the cursor while hunting a hover-only button.

Claude's web chat only *renders* a response's copy button while the pointer is
over that response, so the cheap "capture the gutter and look for the icon"
pass (screen.template) finds nothing there no matter how well calibrated it is.
The fix is to drag the real pointer up the search band a step at a time -
``screen.focus.move_cursor`` does the moving, ``screen.template`` does the
looking - and stop at the first stop where the icon appears.

Only the geometry lives here: no capture, no ctypes, no OS, no sleeping. That
keeps the step sequence unit-testable anywhere and leaves the caller in charge
of how long to let each hover settle.
"""

from __future__ import annotations

from agentclip.driver.screen.region import ScreenRegion

# One step is a bit smaller than a chat's line height: big enough that a full
# transcript is a few dozen stops, small enough that no response is skipped.
STEP_PX = 24
# How long the pointer has to sit still before the page has processed the move
# and painted its hover state. Matches the settle pause on the copy click.
STEP_DELAY_S = 0.08
# Hard cap on stops per scan. A user may draw a band spanning a 4K monitor, and
# every stop costs a capture plus a template scan - the scan is a latency
# budget, not a completeness guarantee.
MAX_STEPS = 80


def hover_scan_points(
    band: ScreenRegion, *, step_px: int = STEP_PX, max_steps: int = MAX_STEPS
) -> list[tuple[int, int]]:
    """Cursor stops for one bottom-up hover scan of ``band``, nearest first.

    All stops share the band's horizontal centre - the band is a same-width
    slice of the copy button's gutter, so its centre column is the one place
    guaranteed to be inside the response the icon belongs to. The walk starts
    on the band's bottom row (the newest response, which is what the auto-copy
    flow wants) and climbs in ``step_px`` jumps, always finishing exactly on the
    top row so the band really is covered.

    Returns an empty list for a degenerate band or a non-positive step/cap, so
    a caller can loop over the result without guarding first.
    """
    if band.width <= 0 or band.height <= 0 or step_px <= 0 or max_steps <= 0:
        return []
    x = band.left + band.width // 2
    y = band.top + band.height - 1
    points: list[tuple[int, int]] = []
    while len(points) < max_steps:
        points.append((x, y))
        if y <= band.top:
            break
        y = max(band.top, y - step_px)
    return points
