"""The ``AutomationView`` port: the narrow interface the automation drives.

Sibling of :mod:`agentclip.shell.app.view`, and deliberately the same shape. Where
``ChatView`` decouples the *session orchestration* from the UI, this decouples
the *screen automation* - the half of AgentClip that watches a browser chat
window, clicks it, pastes into it and harvests the reply.
:class:`~agentclip.driver.automation.controller.AutomationController` holds an
``AutomationView`` and never imports Textual; the Textual ``MainScreen``
implements it structurally (it does not subclass it, to avoid a metaclass clash
with Textual's ``Screen``), and the pywebview GUI will implement the same
handful of methods over its JS bridge (docs/design/gui.md §1).

**THE THREAD CONTRACT.** Where ``ChatView`` has one small family of methods
pushed from a worker thread (``call_started`` / ``call_finished`` /
``call_output``), *every* method here has that contract, because that is what
this port is: the clipboard watcher and the detector poller are plain
``threading.Thread``s owned by the controller, and they are the ones with
something to say. So an implementation of any method below must be
**non-blocking and thread-safe** - the Textual front-end posts a message and
returns, the GUI enqueues onto its bridge queue and returns - and must tolerate
being called with the state it is already showing (paints are idempotent, and
several of them are re-issued unconditionally rather than only on a change).

**Paint-only, by decision.** No OS primitive and no scheduling primitive appears
here: the controller reaches ``agentclip.driver.screen`` directly for the first
(:mod:`agentclip.driver.automation.ops`) and owns its own threads for the second, so a
shell can never quietly become the place a click or a timer lives. What crosses
this seam is "here is what is true now, show it".

The other direction - the handful of things the automation still has to ASK a
shell - is deliberately a port of its own
(:class:`agentclip.driver.automation.host.AutomationHost`), so "tell" and "ask" cannot
quietly merge into one interface with a mixed thread contract: every method here
may be called from a poller thread and must not block, while a host is only ever
called from the event loop.

The port is deliberately incomplete: it grows one method family per extraction
slice, and only what the controller actually calls today is declared - a
Protocol listing methods nothing calls is a promise no implementation is held
to.

**Text, not decisions.** Everything below takes the finished words (or the
finished value) and is told where to put them. What a probe reads as, which of
the send gate's four exits was taken, why the loop moved - all of that is
decided in the controller, out of :mod:`agentclip.driver.automation.finish`, so the two
shells cannot phrase the same reading differently. A view that had to choose the
wording would be a second state machine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from agentclip.driver.automation.harness_log import HarnessEntry
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.screen.profile import TemplateKind

# Same vocabulary as ``ChatView.notify``'s, spelled again rather than imported:
# the automation layer may not import ``agentclip.shell.app`` (tests/test_layering.py),
# and three words is a cheaper duplication than a shared leaf package.
Severity = Literal["information", "warning", "error"]


class AutomationView(Protocol):
    # -- the loop's state and its reasons -------------------------------------
    # Where the browser-automation loop is now (the TUI's STATE rail), and the
    # entry saying how it got there (the TUI's `/log` pane). Two calls because
    # they have two different lifetimes: the state is a value that is REPAINTED,
    # the entry is a line that is APPENDED, and the controller's deque - not the
    # pane - is the log (see ``AutomationController.harness_log``).
    def paint_loop_state(self, state: LoopState) -> None: ...

    def paint_harness_entry(self, entry: HarnessEntry) -> None: ...

    # -- what the detectors are seeing ----------------------------------------
    # One line per appearance the live window's service is calibrated for, plus
    # the stale detector's, which has no appearance behind it and so gets a call
    # of its own. ``text`` is already phrased (agentclip.driver.automation.finish).
    def paint_detection(self, kind: TemplateKind, text: str) -> None: ...

    def paint_stale(self, text: str) -> None: ...

    # One tick's RECOGNITIONS as pictures - what the detectors matched, so the
    # user can see it is their send button and not a corner of their wallpaper.
    # The crops are opaque here on purpose: a crop is sized for whatever renderer
    # will draw it, so cutting one is the shell's and this layer only routes the
    # mapping through. It closes no tick and folds into no verdict.
    def paint_elements(self, crops: Mapping[TemplateKind, object]) -> None: ...

    # -- the "your move" banner -----------------------------------------------
    # Up while the payload is waiting on the user (Ctrl+V, or Enter), down the
    # moment the send is proven - which is the half the controller drives, since
    # it is the finish decision and the send gate that PROVE it. ``text`` is the
    # caller's wording: what the banner asks for depends on how the payload was
    # delivered, and ``retry`` offers the one-press re-run beside the nag that
    # says the paste never landed.
    def show_paste_flash(self, text: str, *, retry: bool = False) -> None: ...

    def hide_paste_flash(self) -> None: ...

    # -- the ARMED switch -----------------------------------------------------
    # Put the standing DISARMED indication up or take it down. Called on every
    # ``set_os_armed``, INCLUDING the ones that did not change the flag: an
    # explicit `/armed off` typed twice has to confirm itself rather than look
    # ignored, so the paint is unconditional and the implementation must be
    # idempotent (see ``AutomationController.set_os_armed``).
    def paint_armed(self, armed: bool) -> None: ...

    # -- notifications --------------------------------------------------------
    # A transient toast. Same three severities as ``ChatView.notify``, so a view
    # that already implements one satisfies the other for free.
    def notify(
        self,
        message: str,
        *,
        severity: Severity = "information",
        timeout: float | None = None,
    ) -> None: ...
