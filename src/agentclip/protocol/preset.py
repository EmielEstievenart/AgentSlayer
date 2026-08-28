"""Which service a session composes against - asked, never cached.

The seam docs/design/ui-monitor.md §11.9 puts under the whole engine half. The
service is the MONITOR's (§10.5): the ``[services.*]`` table that decides what a
paste may weigh, whether a reply must arrive fenced and what extra sentence this
host needs lives on the machine the browser is on, is edited in the Monitor UI,
and reaches the brain on :class:`~agentclip.driver.monitor.protocol.Watched`. An
engine that read ``Config.preset()`` at construction was therefore composing
turns for a service somebody else was running - and could not see a budget the
user changed while the session ran.

So the engine, the composer and the factory stop holding a ``ServicePreset`` and
hold one of these instead. It is a callable behind a docstring: every read is a
fresh question, so a monitor that pushes a new budget mid-session is obeyed by
the next composed turn rather than by the next app launch.

**The monitor-less fallback is the point of the second field.** A provider that
returns ``None`` - no monitor attached, a monitor that has not been pointed at
a service yet, an ``agentclip-engine`` on a target where nothing above the link
knows a screen exists - falls through to the ``ServicePreset`` this was built
with, which every caller takes from the local ``Config.preset()``. So a CLI or
headless launch, a remote session (the provider does not cross the engine link)
and an idle Chat UI all behave exactly as they did before this seam existed:
the local ``[services.*]`` table, read once per session.
"""

from __future__ import annotations

from collections.abc import Callable

from agentclip.config import BudgetCaps, ServicePreset, caps_for_budget

#: What a shell hands down: "the service the monitor is driving right now", or
#: ``None`` for "nobody is driving one" - which is a fallback, not an error.
PresetSource = Callable[[], ServicePreset | None]


class LivePreset:
    """The session's service preset, re-read on every question.

    Mirrors the two methods it replaces - ``Config.preset()`` and
    ``Config.caps()`` - so the call sites below the shell read the same way they
    always did; the caps are DERIVED from whatever budget the current answer
    carries rather than snapshotted beside it, because the two drifting apart
    is the bug this seam exists to prevent.
    """

    __slots__ = ("_fallback", "_source")

    def __init__(self, fallback: ServicePreset, source: PresetSource | None = None) -> None:
        self._fallback = fallback
        self._source = source

    def preset(self) -> ServicePreset:
        """The service to compose the next payload against."""
        if self._source is not None:
            live = self._source()
            if live is not None:
                return live
        return self._fallback

    def caps(self) -> BudgetCaps:
        """The per-tool result caps that budget implies (protocol.md §5.3)."""
        return caps_for_budget(self.preset().max_paste_chars)
