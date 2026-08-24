"""Where the loop is, how it got there, and what that sounds like.

Three things that must not be able to disagree, so they are one object with one
door. ``LoopState`` is what the sidebar's STATE rail draws; the harness log is
why it moved (docs/design/ui-monitor.md's diff-check, §4.8); the attention alarm
is the audible half of "your move". A move that reached the rail without reaching
the log would be a decision nobody can read back afterwards, and an alarm hung
off one branch instead of the door is nine places to forget to stop it.

The door demands a REASON, and that is the point of it: the rail draws one box
for four different roads into ``MANUAL_COPY``, and a caller that could move the
loop without saying which road it took is a caller whose decision is unreadable.

Two callers, and the difference is what happens to the loop TASK rather than
anything here: the loop's own transition (``AutomationController.enter``) and a
shell speaking over it (``set_loop_state``, which pre-empts). Both land on
:meth:`LoopNarration.moved`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from agentclip.config import ServicePreset
from agentclip.driver.automation.alerts import AttentionAlarm
from agentclip.driver.automation.harness_log import (
    HARNESS_LOG_MAX,
    KIND_STATE,
    HarnessEntry,
    state_text,
)
from agentclip.driver.automation.loop_state import ATTENTION_STATES, LoopState
from agentclip.driver.automation.view import AutomationView


class LoopNarration:
    """The rail, the log and the alarm, moved together or not at all."""

    def __init__(
        self,
        view: AutomationView,
        *,
        live_preset: Callable[[], ServicePreset],
        alarm: AttentionAlarm,
    ) -> None:
        self._view = view
        self._live_preset = live_preset
        self._alarm = alarm
        self.state = LoopState.IDLE
        # Bounded, never written to disk, and deliberately NOT cleared by /new: a
        # wedged user resets the session first and goes looking for the evidence
        # second. The reset writes its own entry instead.
        self.log: deque[HarnessEntry] = deque(maxlen=HARNESS_LOG_MAX)

    def moved(self, state: LoopState, reason: str) -> None:
        """Move the loop to ``state``, say why, and repaint - as one act.

        Logged only when the state actually CHANGES, so the same evidence arriving
        twice does not fill the log with a repeated non-event.
        """
        if state is self.state:
            return
        before = self.state
        self.state = state
        self.entry(KIND_STATE, state_text(before.name, state.name, reason))
        self._view.paint_loop_state(state)
        self._attention(state)

    def entry(self, kind: str, text: str) -> None:
        """Append one decision to the harness log (`/log`).

        The single append site, so the bound and the timestamp are decided once.
        The deque is the log; a shell's pane is a VIEW of it, mirrored one entry at
        a time so an open pane shows a decision as it is taken.
        """
        entry = HarnessEntry(kind, text)
        self.log.append(entry)
        self._view.paint_harness_entry(entry)

    def _attention(self, state: LoopState) -> None:
        """Start or stop the "your move" alarm for the state just entered.

        Arm on the states that need a human (``ATTENTION_STATES``), disarm on
        everything else - including an attention state on a service whose alert is
        off, which is what makes turning the setting off mid-nag stop the noise at
        the next transition rather than never.
        """
        preset = self._live_preset()
        if state in ATTENTION_STATES and preset.alert_sound:
            self._alarm.arm(repeat_seconds=preset.alert_repeat_seconds)
        else:
            self._alarm.disarm()

    def chime(self) -> None:
        """One uh-oh for a re-sync the LOOP never hears about - a protocol error,
        where the reply arrived, the loop moved on, and the user must still go back
        to the browser and re-copy. There is no attention state to arm against and
        nothing to disarm it afterwards, so it is a single chime, still gated on
        the live service's ``alert_sound``."""
        if self._live_preset().alert_sound:
            self._alarm.chime()

    def hush(self) -> None:
        """Silence the alarm for good (shutdown). Safe when nothing is sounding."""
        self._alarm.disarm()
