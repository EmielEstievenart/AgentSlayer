"""The DETECTION readout: what a tick SAYS, painted where the user reads it.

Not a decision - that is what makes this the one thing in the automation still
delivered on the MONITOR's own thread rather than pulled off ``observe()`` by the
loop. Every rule about when the model stopped is a recipe's; this only turns the
three probes into the three lines the sidebar shows, and the crops into the
pictures beside them, and hands both to the ``AutomationView`` port, whose
contract has always been "non-blocking, thread-safe, may be called from a poller
thread".

It lives beside the controller rather than inside it because it is the last thing
in this layer that is neither state nor decision: a shell that wanted to draw its
own readout could subscribe to the monitor and call these two functions itself,
which is exactly what a later phase does with them.
"""

from __future__ import annotations

from collections.abc import Mapping

from agentclip.driver.automation.finish import (
    format_busy_probe,
    format_idle_probe,
    format_stale_probe,
)
from agentclip.driver.automation.machine import CropFn
from agentclip.driver.automation.view import AutomationView
from agentclip.driver.monitor.protocol import Tick
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import Sighting
from agentclip.driver.screen.profile import TemplateKind


def paint_tick(view: AutomationView, tick: Tick) -> None:
    """One observation of the chat window, as up to three lines of readout.

    Each probe is skipped when the configuration has no such detector, which is
    what ``None`` means on a tick - a line for a detector that is not running
    would be a line about nothing.
    """
    if tick.busy is not None:
        view.paint_detection(TemplateKind.BUSY, format_busy_probe(tick.busy))
    if tick.idle is not None:
        view.paint_detection(TemplateKind.IDLE, format_idle_probe(tick.idle))
    if tick.stale is not None:
        view.paint_stale(format_stale_probe(tick.stale))


def paint_frame(
    view: AutomationView,
    crop: CropFn,
    scene: RegionImage,
    sightings: Mapping[TemplateKind, Sighting | None],
) -> None:
    """One tick's pictures (the monitor's local-only frame hook).

    Beside the tick rather than on it, because a crop is pixels and a tick carries
    none (§2.2). A frame that recognised nothing says nothing: an empty map would
    blank rows this tick is no evidence about, so it is dropped rather than drawn.
    """
    if not sightings:
        return
    view.paint_elements(crop(scene, sightings))
