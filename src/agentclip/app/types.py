"""UI-agnostic value types shared by the session controller and any view.

These are plain dataclasses with no Textual/clipboard dependency so the
orchestration layer (and a future non-Textual UI) can use them freely.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """What the New-Session prompt returns: the task plus the chosen service preset."""

    task: str
    service: str


@dataclass(slots=True)
class SessionStats:
    """Per-session counters accumulated across turns (shown in the summary)."""

    service: str = ""
    replies: int = 0
    calls: Counter[str] = field(default_factory=Counter)
    chars_out: int = 0
    chars_in: int = 0
    summary: str = ""
