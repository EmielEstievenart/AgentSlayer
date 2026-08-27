"""Finish detection's vocabulary: what a probe MEANS, and what it reads as.

The third pure-vocabulary module of the automation package, alongside
:mod:`agentclip.driver.automation.loop_state` and
:mod:`agentclip.driver.automation.harness_log`, and here for the same reason they are:
:class:`~agentclip.driver.automation.controller.AutomationController` folds the poller's
probes into one "the model stopped" decision, and every constant and phrase that
decision is made of has to live below both UI shells or it drifts between them.

Three groups, and they are three different kinds of thing:

* **The verdicts.** ``busy_verdict`` / ``idle_verdict`` / ``stale_verdict`` turn
  one detector's probe into the same three-valued answer - True finished, False
  generating, None no verdict (the capture failed) - so the fold above them
  never has to remember that a busy appearance means the opposite of an idle
  one. That inversion is the whole reason these are functions and not a
  comparison at the call site. They are :mod:`agentclip.driver.monitor.verdicts`'
  now (phase 2 counts the streaks made of them where the pixels are) and are
  re-exported below.
* **The readout.** ``format_*_probe`` and the ``SEND_READY_*`` lines are the
  words the sidebar (and, later, the GUI) shows for those same probes. They are
  paint TEXT rather than paint DECISIONS, which is why they cross the
  ``AutomationView`` port as strings: the controller knows what happened, the
  shell only knows where to put it. The three ``format_*_probe`` functions
  travelled with the verdicts they narrate; the ``SEND_READY_*`` lines are about
  a GATE rather than a probe, and stay here.
* **The tunables.** How much change counts as change, how long a gate may wait.
  They are the MONITOR's now (:mod:`agentclip.driver.monitor.beats`, wave 3
  §10.5): both are counted in the monitor's own units - a tick and a
  frame-to-frame diff - so the machine that produces those units owns the
  numbers. Re-exported below at the address every caller already names.

``SendGate`` is here too, for the same reason ``LoopState`` is in its own module:
it is a state the automation is IN, and both shells will want to show it.
"""

from __future__ import annotations

from enum import Enum

# The send gate's four budgets MOVED to the monitor in wave 3
# (docs/design/ui-monitor.md §10.5): they are counted in TICKS and in
# frame-to-frame DIFF, and a tick is the monitor's unit - so they are the
# monitor's constants and no longer ride on ``MonitorSpec``. Re-exported at this
# address because every caller in the brain, and every suite, names them here.
from agentclip.driver.monitor.beats import (  # noqa: F401
    SEND_ARM_MIN_DIFF,
    SEND_ARM_TICKS,
    SEND_GATE_SEEN_TIMEOUT_TICKS,
    SEND_GATE_TIMEOUT_TICKS,
)

# The verdicts and their readout MOVED to the monitor in phase 2
# (docs/design/ui-monitor.md §6.2): the monitor is what folds a probe into a
# verdict now, and it counts the two streaks that used to be controller fields.
# They stay reachable from here because every caller in the brain, and every
# suite, names them at this address - and because this module is where the
# vocabulary they belong to is documented.
from agentclip.driver.monitor.verdicts import (  # noqa: F401
    busy_verdict,
    format_busy_probe,
    format_idle_probe,
    format_stale_probe,
    idle_verdict,
    stale_verdict,
)


class SendGate(Enum):
    """Where the ready-to-send gate is between AgentClip's paste and the send.

    Two states and an absence. ``None`` is by far the commonest and means "not
    gating": the live service has no ``SEND_READY`` appearance, or the gate has
    already let go. Only the two below hold finish detection back.
    """

    HOLD = "hold"  # pasted, and the send button has not been seen yet
    SEEN = "seen"  # it is on screen; its disappearance is the user's Enter



# The send gate's line in the DETECTION readout (tui.md 3.4b). It is not a finish
# detector: it reports whether the ready-to-send button is holding finish
# detection back between AgentClip's paste and the user's Enter, and the four
# terminal lines below name WHICH way it let go, because the user's fix differs
# per way.
SEND_READY_RESTING = "no gate - not captured"
SEND_READY_ARMED = "gate armed - holds the next paste"
SEND_READY_HOLDING = "watching for the send button"
SEND_READY_SEEN = "on screen - press Enter to send"
SEND_READY_RELEASED = "gone - sent, finish detection running"
SEND_READY_OVERRIDDEN = "generating - sent, finish detection running"
SEND_READY_TIMEOUT = "never appeared - finish detection running"
SEND_READY_STUCK = "never went away - finish detection running"
