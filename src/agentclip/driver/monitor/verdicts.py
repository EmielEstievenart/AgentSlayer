"""What one probe MEANS, and the two streaks a run of them adds up to.

Pure functions over a probe: no screen, no clock, no state of their own beyond
the count handed in. They live here rather than in ``driver/automation`` because
the monitor is what folds a probe into a verdict now (docs/design/ui-monitor.md
§2.2: the tick carries "the debounced busy/idle/stale verdict inputs ... the
copy-changed streak") and a shell may not reach across the seam to a brain-side
copy of the same arithmetic. :mod:`agentclip.driver.automation.finish` re-exports
every name below under the spelling its suites already reach for.

Three groups:

* **The verdicts.** :func:`busy_verdict` / :func:`idle_verdict` /
  :func:`stale_verdict` turn one detector's probe into the same three-valued
  answer - True finished, False generating, None no verdict (the capture
  failed) - so a fold above them never has to remember that a busy appearance
  means the opposite of an idle one. That inversion is the whole reason these
  are functions and not a comparison at the call site.
* **The readout.** ``format_*_probe`` are the words a shell shows for those same
  probes. They are paint TEXT rather than paint DECISIONS, and they are pure
  functions of a probe, which is why they sit beside the verdicts they narrate.
* **The streaks.** :func:`roll_arm_streak` and :func:`roll_changed_streak` are
  the two consecutive-tick counters ``evaluate_finish`` kept as controller
  fields until phase 2. Both take the previous count and one tick's probes and
  hand back the next count - which is what makes them testable without a screen
  and reusable by whatever ends up doing the counting.

What is NOT here, deliberately: every rule that says what a streak is worth.
Whether two changed ticks may fire an auto-copy, whether the trigger is armed,
whether a send gate is holding - all policy, all the brain's (§2.3).
"""

from __future__ import annotations

from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.stale import StaleProbe, StaleState


def format_busy_probe(probe: BusyProbe) -> str:
    """Unmistakable readout for the sidebar - this is the whole deliverable."""
    if probe.state is BusyState.ERROR:
        return "✗ capture failed"
    pct = f"{(probe.diff or 0.0) * 100:.1f}%"
    if probe.state is BusyState.MATCH:
        return f"● GENERATING · match (diff {pct})"
    return f"○ response ready · changed (diff {pct})"


def format_idle_probe(probe: BusyProbe) -> str:
    """Same readout for the idle element, with the polarity flipped: it was
    calibrated while the chat was idle, so MATCH is the *finished* verdict."""
    if probe.state is BusyState.ERROR:
        return "✗ capture failed"
    pct = f"{(probe.diff or 0.0) * 100:.1f}%"
    if probe.state is BusyState.MATCH:
        return f"○ response ready · match (diff {pct})"
    return f"● GENERATING · changed (diff {pct})"


def format_stale_probe(probe: StaleProbe) -> str:
    """Same unmistakable readout for the stale detector: the response region
    still moving means the model is still typing; long enough unchanged means
    the answer is done. ``still ×N`` shows the streak building toward STALE."""
    if probe.state is StaleState.ERROR:
        return "✗ capture failed"
    if probe.state is StaleState.STALE:
        return f"○ response ready · stale (still ×{probe.stable_ticks})"
    pct = f"{(probe.diff or 0.0) * 100:.2f}%"
    return f"● GENERATING · changing (diff {pct} · still ×{probe.stable_ticks})"


def busy_verdict(probe: BusyProbe) -> bool | None:
    """The busy element's probe as a finish verdict: True = finished,
    False = generating, None = no verdict (capture error).

    It was calibrated WHILE the model was generating, so a MATCH means the
    generation is still going.
    """
    if probe.state is BusyState.ERROR:
        return None
    return probe.state is BusyState.CHANGED


def idle_verdict(probe: BusyProbe) -> bool | None:
    """The idle element's probe as a finish verdict, same three values.

    It was calibrated while the chat was IDLE, so a MATCH means the response
    has finished - the exact inverse of the busy element.
    """
    if probe.state is BusyState.ERROR:
        return None
    return probe.state is BusyState.MATCH


def stale_verdict(probe: StaleProbe) -> bool | None:
    """The stale tracker's probe as a finish verdict, same three values.

    STALE (unchanged long enough) means finished; CHANGING - including the
    settling polls before the streak completes - means generating.
    """
    if probe.state is StaleState.ERROR:
        return None
    return probe.state is StaleState.STALE


def roll_arm_streak(previous: int, stale: StaleProbe | None, *, min_diff: float) -> int:
    """How many consecutive ticks the response region has changed *a lot*.

    The evidence the STALE detector alone is allowed to arm an auto-copy on
    (``finish.SEND_ARM_MIN_DIFF`` / ``SEND_ARM_TICKS``). Frame-to-frame change
    is weak evidence: after AgentClip pastes, the user still has to press Enter,
    and in that window a blinking caret or a mouse-over highlight makes the
    region "change" by a handful of pixels. So a CHANGING verdict counts only
    when it is BIG - ``min_diff`` of the sampled pixels - and only while it
    keeps being big; anything else (a small-diff CHANGING, a STALE, a capture
    error) drops the run back to zero.

    A tick with NO stale probe at all leaves the count exactly as it was, which
    is the one asymmetry worth spelling out: the run is a property of the stale
    detector, and a configuration that does not run it has no run to break.
    That is the roll ``evaluate_finish`` made under ``if self._stale_seen``.
    """
    if stale is None:
        return previous
    big_delta = stale_verdict(stale) is False and stale.diff is not None and stale.diff >= min_diff
    return previous + 1 if big_delta else 0


def roll_changed_streak(
    previous: int,
    *,
    busy: BusyProbe | None,
    idle: BusyProbe | None,
    stale: StaleProbe | None,
    active_detectors: tuple[str, ...],
) -> int:
    """How many consecutive ticks EVERY active detector has said "finished".

    The agreement a second detector exists for: with one live signal this is
    today's plain run of finished verdicts, with two it is the run of ticks they
    agreed on. A detector this configuration does not run has no vote; one that
    is configured but has not reported on this tick (a probe that is ``None``)
    has no vote *yet*, which is different from voting "generating".

    Anything that is not unanimous agreement resets the run to zero - a
    "generating" verdict, a capture error (``None``), and a tick on which nobody
    voted at all. Biasing away from "finished" on no evidence is the only thing
    such a tick may do.

    The count and nothing else. What it is worth - two ticks, an armed trigger,
    a send gate that is holding, a flow already running - is the brain's (§2.3).
    """
    verdicts: list[bool | None] = []
    if "busy" in active_detectors and busy is not None:
        verdicts.append(busy_verdict(busy))
    if "idle" in active_detectors and idle is not None:
        verdicts.append(idle_verdict(idle))
    if "stale" in active_detectors and stale is not None:
        verdicts.append(stale_verdict(stale))
    if not verdicts or not all(verdict is True for verdict in verdicts):
        return 0
    return previous + 1
