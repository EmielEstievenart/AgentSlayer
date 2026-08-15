"""Textual messages bridging background threads to the UI.

``ClipboardCaptured`` is the documented injectable path for tests: posting it
to the MainScreen is equivalent to the watcher thread capturing protocol text
from the OS clipboard.

``CallStarted``/``CallFinished``/``CallOutput`` are the same idiom for the OTHER
background thread - the one the engine executes a turn's tool calls on. The
controller's ChatView port promises those three calls are thread-safe (see
app/view.py), and this is how MainScreen keeps that promise: the hook posts,
the UI thread handles, and the run panel is only ever touched from the loop.

**The paint family** is the third and largest group, and it runs the other way
round: not a background thread reporting a FACT, but the
:class:`~agentclip.driver.automation.controller.AutomationController` asking for a
REPAINT from whichever thread it happens to be on. Since the consumer moved onto
the poller thread (docs/design/gui.md §1, slice 5b) that thread is usually not
the UI's, and every ``AutomationView`` method MainScreen implements is therefore
a two-liner that posts one of these and returns - ``PaintLoopState``,
``PaintHarnessEntry``, ``PaintDetection``, ``PaintStale``, ``PaintElements``,
``PaintArmed``, ``ShowPasteFlash``, ``HidePasteFlash``, ``NotifyRequested`` - and
the handler on the other side does the widget writes.

One message per port method rather than one carrying a closure, deliberately:
the message pump is the one place the whole conversation between the automation
and its shell is visible at once, and a queue full of "call this function" says
nothing. ``AutoCopyRequested`` is the same idiom for the one call that is not a
paint - the finish decision's fire, which has to reach the UI thread because
launching a Textual worker is the UI thread's alone.

**The queue does not order the two threads.** ``post_message`` from a thread
that is not the app's goes through ``call_soon_threadsafe``, while one from the
app's own thread lands in the queue immediately - so a paint the poller asked
for FIRST can be delivered after one the UI thread asked for later. Three of the
messages below answer that rather than assume it away: the two with a re-readable
truth (``PaintLoopState``, ``PaintArmed``) are drawn from the controller instead
of from their payload, the run-scoped ones (``PaintDetection``, ``PaintStale``,
``PaintElements``) carry a paint EPOCH so a superseded run's verdict cannot
overwrite the rebuild that replaced it, and ``PaintHarnessEntry`` drains an
ordered queue because a log out of order is not a log.

There is deliberately no message for a PROBE any more. Probes are consumed in
the poll loop's own call stack, so the injectable path the finish suites drive
the state machine through is ``AutomationController.feed_probe`` - one call, the
same busy -> idle -> stale -> send_ready -> elements vocabulary, stamped with the
live generation unless a test says otherwise. What still crosses here is what
that consumption PAINTED, which is why a Pilot test now needs a ``pilot.pause()``
between feeding a probe and reading the sidebar back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from textual.message import Message
from textual.notifications import SeverityLevel

from agentclip.driver.automation.harness_log import HarnessEntry
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.executor.mcp.types import McpServerStatus


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


class McpStatusChanged(Message):
    """One MCP server changed state (posted from the manager's loop thread).

    The third background thread this module bridges, same idiom as the other
    two: ``McpManager.set_status_hook``'s contract is a non-blocking listener
    called from the manager's own loop thread (and, for ``missing_sdk``, from
    whatever thread called ``ensure_started`` - which can be Textual's own, so
    ``call_from_thread`` would refuse it), and a listener that raises is
    silently dropped for good. ``post_message`` is safe from every one of
    those threads including the UI's, the hook stays three lines, and the
    repaint - sidebar block, statusbar segment, the once-per-state transcript
    note - happens on the loop where widgets may be touched
    (docs/design/mcp.md section 6).

    Carries the transition itself so the note/toast logic knows WHAT changed;
    the repaint reads a fresh ``statuses()`` instead, because the message is a
    tick, not the state.
    """

    def __init__(self, status: McpServerStatus) -> None:
        self.status = status
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


# == the paint family ==========================================================
# One message per AutomationView method, because the pump should stay readable:
# "PaintDetection(BUSY, ...)" is a decision anybody can follow through the queue,
# and a generic envelope carrying a callable is not. Each is posted by the
# MainScreen method of the same name and handled by the one below it, which does
# the widget writes the synchronous implementations did before the consumer
# moved threads.


class PaintLoopState(Message):
    """Repaint the sidebar's STATE rail: the loop is HERE now.

    The handler paints ``AutomationController.loop_state``, not this payload -
    two threads move the loop and the queue cannot promise their order across
    the boundary, so the state travels for the pump's sake and the truth is
    re-read where it is drawn.
    """

    def __init__(self, state: LoopState) -> None:
        self.state = state
        super().__init__()


class PaintHarnessEntry(Message):
    """Mirror one appended decision into the `/log` pane.

    The deque in the controller is the log; this is the pane catching up one
    entry at a time, which is what makes an open pane show a decision as it is
    taken. Order is the whole contract - and it is the one thing the pump cannot
    promise, because Textual routes a cross-thread post through
    ``call_soon_threadsafe`` and a same-thread one straight into the queue, so a
    poller entry can be overtaken by a UI-thread entry logged after it. Which is
    why the ENTRIES ride an ordered queue on the screen and this message is only
    the nudge that drains it: the pump still names the decision, and the pane
    still draws them in the deque's order (``MainScreen.paint_harness_entry``).
    """

    def __init__(self, entry: HarnessEntry) -> None:
        self.entry = entry
        super().__init__()


class PaintDetection(Message):
    """One appearance's line in the sidebar's DETECTION block, already phrased.

    ``text`` comes out of ``agentclip.driver.automation.finish`` - the shell never
    chooses the wording, or the two shells would phrase the same reading
    differently (automation/view.py).

    ``epoch`` is which detector RUN the line belongs to (``MainScreen``'s
    ``_paint_epoch``, bumped by every rebuild). It is the ghost filter, moved to
    the paint side: the probe behind this line no longer crosses a thread
    boundary to be filtered on arrival, but the paint does - and Textual routes
    a cross-thread post through ``call_soon_threadsafe``, so an outgoing run's
    last verdict can be delivered after the rebuild that reset this block.
    """

    def __init__(self, kind: TemplateKind, text: str, epoch: int) -> None:
        self.kind = kind
        self.text = text
        self.epoch = epoch
        super().__init__()


class PaintStale(Message):
    """The stale detector's line, which has no appearance behind it.

    Same ``epoch`` rule as ``PaintDetection``.
    """

    def __init__(self, text: str, epoch: int) -> None:
        self.text = text
        self.epoch = epoch
        super().__init__()


class PaintElements(Message):
    """One tick's RECOGNITIONS as pictures, for the ELEMENTS column.

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

    The crop itself was cut on the poller thread, out of the frame the matches
    were verified against, so what crosses here is an icon and not a chat
    window. Same ``epoch`` rule as ``PaintDetection``: the controller's ghost
    filter answers "is this RUN still live", this one answers "is this picture
    still about the window whose name is over the column".
    """

    def __init__(self, crops: Mapping[TemplateKind, ElementCrop | None], epoch: int) -> None:
        self.crops = dict(crops)
        self.epoch = epoch
        super().__init__()


class PaintArmed(Message):
    """Put the standing DISARMED banner up or take it down.

    Posted on every ``set_os_armed``, including the ones that changed nothing:
    an explicit `/armed off` typed twice has to confirm itself rather than look
    ignored (automation/view.py). Like ``PaintLoopState`` the handler paints the
    controller's flag rather than this payload - the message is the ask, the
    controller is the truth.
    """

    def __init__(self, armed: bool) -> None:
        self.armed = armed
        super().__init__()


class ShowPasteFlash(Message):
    """Put the ">>> PRESS ... <<<" banner up. ``retry`` offers the one-press
    re-run beside the nag that says the paste never landed."""

    def __init__(self, text: str, retry: bool) -> None:
        self.text = text
        self.retry = retry
        super().__init__()


class HidePasteFlash(Message):
    """Take it down - the send is proven, so the nag is over."""


class NotifyRequested(Message):
    """A toast, asked for from wherever the automation happens to be running.

    Textual's own ``notify`` ends in a ``post_message`` and is safe from any
    thread, so this hop buys no safety Textual does not already give - it buys
    ORDER. A gate timing out paints its line, logs its entry and toasts about
    it; routing all three through the same queue is what keeps the toast from
    overtaking the line it is explaining.
    """

    def __init__(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.message = message
        self.title = title
        self.severity: SeverityLevel = severity
        self.timeout = timeout
        self.markup = markup
        super().__init__()


class AutoCopyRequested(Message):
    """The finish decision fired: harvest the reply.

    The one thing crossing this seam that is not a paint. It has to cross
    because launching a Textual worker is the UI thread's alone, and the
    decision is taken on the poller thread - but the ONE-SHOT guard does not
    ride along with it: ``AutomationController.evaluate_finish`` sets
    ``flow_running`` synchronously before asking, so the ticks that land in the
    hop between this post and its handler cannot ask a second time.
    """
