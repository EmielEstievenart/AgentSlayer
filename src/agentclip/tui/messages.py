"""Textual messages bridging background threads to the UI.

``ClipboardCaptured`` is the documented injectable path for tests: posting it
to the MainScreen is equivalent to the watcher thread capturing protocol text
from the OS clipboard.

``CallStarted``/``CallFinished``/``CallOutput`` are the same idiom for the OTHER
background thread - the one the engine executes a turn's tool calls on. The
controller's ChatView port promises those three calls are thread-safe (see
app/view.py), and this is how MainScreen keeps that promise: the hook posts,
the UI thread handles, and the run panel is only ever touched from the loop.

``BusyProbed``, ``IdleProbed`` and ``StaleProbed`` are the three finish
detectors' poll results, posted in a FIXED order within each poller tick:
busy first, then idle, then stale.

* ``BusyProbed`` carries the *busy* element's verdict. That element was
  calibrated WHILE the model was generating, so MATCH means "still generating"
  and CHANGED means "finished".
* ``IdleProbed`` carries the *idle* element's verdict. That element was
  calibrated while the chat was IDLE, so the reading is inverted: MATCH means
  "finished", CHANGED means "generating".
* ``StaleProbed`` carries the *stale* tracker's verdict for the response
  region: CHANGING means "generating", STALE (unchanged for the required run
  of polls) means "finished". No calibration baseline - stability is relative
  to the previous frame.

``ElementsMatched`` carries no verdict at all: it is the tick's RECOGNITIONS,
one small picture per appearance that was found, so the elements panel can SHOW
the user the pixels the detectors matched rather than a diff percentage. It
closes no tick and folds into no verdict.

``SendReadyProbed`` is the fourth, and it is not a finish detector at all: it
answers "is the ready-to-send button on screen?" for the send gate (tui.md
§3.4b), which holds finish detection back between AgentClip's paste and the
user's Enter. Posted on every tick the live window's service has a capture of
that button - the detector searches on calibration alone (screen/detector.py)
and the gate is a READER of the answer, not the thing that asks the question -
so a tick lands whether or not anything is waiting for one, and
``on_send_ready_probed`` ignores the ones that arrive while the gate is not
holding. ``found`` is True (on screen), False (not on screen) or None (the
tick's capture failed).

All of them carry the ``generation`` of the poller run that produced them - a
monotonic counter MainScreen bumps every time it (re)builds the detectors. A
cancelled thread worker still finishes the tick it was interrupted in and posts
its verdicts, so a probe can land after the live browser window has already
moved (a delegation starting or ending retargets the automation and restarts
the poller). Those verdicts describe the OLD window and must not arm or fire the
auto-copy flow against the new one, which the stamp is what makes decidable:
the detector's *name* alone cannot tell two runs apart.

Posting any of them directly is equivalent to that detector's poll completing,
and is how the tests drive MainScreen's combined finish logic - pass
``main._detector_generation`` as the stamp to speak as the current poller.
Whatever subset of the three is calibrated, the tick is *closed* by the LAST
calibrated detector in the busy -> idle -> stale order - MainScreen evaluates
the combined verdict exactly once per tick, on the closing message - so a test
exercising a multi-detector path must post the whole tick, in order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from textual.message import Message

from agentclip.screen.busy import BusyProbe
from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.stale import StaleProbe


class ClipboardCaptured(Message):
    """Protocol-looking text captured from the clipboard (or injected by tests)."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class CallStarted(Message):
    """The engine entered one call's handler (posted from the worker thread)."""

    def __init__(self, call_id: int, tool: str, detail: str) -> None:
        self.call_id = call_id
        self.tool = tool
        self.detail = detail
        super().__init__()


class CallFinished(Message):
    """One call resolved; ``glyph`` is ✓ / ✗ / − (posted from the worker thread)."""

    def __init__(self, call_id: int, glyph: str) -> None:
        self.call_id = call_id
        self.glyph = glyph
        super().__init__()


class CallOutput(Message):
    """New characters from a running command - the delta, never the whole buffer.

    The third of the run-panel messages and the only high-frequency one: a
    chatty command posts one of these per poll slice (5/s), which is why the
    handler's only job is to append to the screen's per-call deque and repaint
    a pane that is usually not even displayed.
    """

    def __init__(self, call_id: int, chunk: str) -> None:
        self.call_id = call_id
        self.chunk = chunk
        super().__init__()


class BusyProbed(Message):
    """One poll of the busy element (MATCH = generating), or injected by tests."""

    def __init__(self, probe: BusyProbe, generation: int) -> None:
        self.probe = probe
        self.generation = generation
        super().__init__()


class IdleProbed(Message):
    """One poll of the idle element (MATCH = finished), or injected by tests."""

    def __init__(self, probe: BusyProbe, generation: int) -> None:
        self.probe = probe
        self.generation = generation
        super().__init__()


class StaleProbed(Message):
    """One poll of the stale tracker (STALE = finished), or injected by tests."""

    def __init__(self, probe: StaleProbe, generation: int) -> None:
        self.probe = probe
        self.generation = generation
        super().__init__()


@dataclass(frozen=True, slots=True)
class ElementCrop:
    """One recognised appearance: the matched pixels, and how well they matched.

    ``image`` was cut from the frame and sized for the live renderer wherever
    that frame was captured (``tui.pixels.crop`` then
    ``elements.element_crop_image``), so what crosses here is an icon rather
    than a chat window: the exact cell grid the half-block pass will draw, or -
    on a sixel terminal - the matched pixels untouched, because drawing them at
    their real size is the whole point. Either way the UI thread's share is one
    small image.
    """

    image: RegionImage
    diff: float


class ElementsMatched(Message):
    """What the LIVE window's detectors RECOGNISED this tick, as pictures.

    ``crops`` maps an appearance to the crop that matched it, and the three
    states it can express are all different:

    * a kind present with an ``ElementCrop`` - found, here it is;
    * a kind present with ``None`` - searched this tick and not on screen;
    * a kind ABSENT - not searched at all this tick, so its row keeps whatever
      it last said. That means one thing only: the live window's service is not
      CALIBRATED for it (no capture, or - for busy/idle - a checklist that does
      not tick it). A fully calibrated service carries an entry for all SEVEN
      appearances on every tick, because the detector searches for everything it
      can see regardless of what the state machine is doing
      (screen/detector.py). A tick whose capture failed carries nothing at all.

    Like ``SendReadyProbed`` it closes no tick and folds into no verdict - these
    are pictures, and their only job is letting the user see that the detectors
    are recognising their send button and not a corner of their wallpaper.

    Carries the same ``generation`` stamp for the same reason: a cancelled run's
    in-flight crops are pictures from a window that may no longer be live, and
    painting them under a heading naming the new one would be a lie.
    """

    def __init__(
        self, crops: Mapping[TemplateKind, ElementCrop | None], generation: int
    ) -> None:
        self.crops = dict(crops)
        self.generation = generation
        super().__init__()


class SendReadyProbed(Message):
    """One look for the ready-to-send button, or injected by tests.

    ``found``: True on screen, False not on screen, None the capture failed.
    Unlike the three above it closes no tick and folds into no verdict - it
    drives the send gate alone (MainScreen ``_on_send_ready_probed``).
    """

    def __init__(self, found: bool | None, generation: int) -> None:
        self.found = found
        self.generation = generation
        super().__init__()
