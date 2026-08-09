"""Textual messages bridging background threads to the UI.

``ClipboardCaptured`` is the documented injectable path for tests: posting it
to the MainScreen is equivalent to the watcher thread capturing protocol text
from the OS clipboard.

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

All three carry the ``generation`` of the poller run that produced them - a
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

from textual.message import Message

from agentclip.screen.busy import BusyProbe
from agentclip.screen.stale import StaleProbe


class ClipboardCaptured(Message):
    """Protocol-looking text captured from the clipboard (or injected by tests)."""

    def __init__(self, text: str) -> None:
        self.text = text
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
