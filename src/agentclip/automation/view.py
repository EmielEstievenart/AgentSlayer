"""The ``AutomationView`` port: the narrow interface the automation drives.

Sibling of :mod:`agentclip.app.view`, and deliberately the same shape. Where
``ChatView`` decouples the *session orchestration* from the UI, this decouples
the *screen automation* - the half of AgentClip that watches a browser chat
window, clicks it, pastes into it and harvests the reply.
:class:`~agentclip.automation.controller.AutomationController` holds an
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
here: the controller imports ``agentclip.screen`` directly for the first and
owns its own threads for the second, so a shell can never quietly become the
place a click or a timer lives. What crosses this seam is "here is what is true
now, show it".

The port is deliberately incomplete: it grows one method family per extraction
slice (``paint_loop_state``, ``paint_detection``, ``paint_stale``,
``paint_elements``, the paste-flash pair), and only what the controller actually
calls today is declared - a Protocol listing methods nothing calls is a promise
no implementation is held to.
"""

from __future__ import annotations

from typing import Literal, Protocol

# Same vocabulary as ``ChatView.notify``'s, spelled again rather than imported:
# the automation layer may not import ``agentclip.app`` (tests/test_layering.py),
# and three words is a cheaper duplication than a shared leaf package.
Severity = Literal["information", "warning", "error"]


class AutomationView(Protocol):
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
