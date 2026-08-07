"""UI-agnostic value types shared by the session controller and any view.

These are plain dataclasses with no Textual/clipboard dependency so the
orchestration layer (and a future non-Textual UI) can use them freely.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["master", "subagent"]


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """What the New-Session prompt returns: the task plus the chosen service preset."""

    task: str
    service: str


@dataclass(frozen=True, slots=True)
class EngineRequest:
    """What the controller asks the engine factory to build.

    A request object rather than a bare service key so role, catalog gating and
    chat naming travel as plain data: the factory lives in `cli` (it needs the
    tool/store/composer wiring) while the decision to spawn a sub-agent is made
    in `app`, which must not import screen or tui to make it.
    """

    service: str
    role: Role = "master"
    # Whether the `delegate` tool appears in the catalog at all. Only ever true
    # for a master, and only when the sub-agent chat window is fully calibrated:
    # offering a tool the host cannot honour wastes a whole round trip.
    allow_delegate: bool = False
    chat_name: str | None = None  # None -> the factory draws a fresh one
    parent_chat_name: str | None = None  # the delegating chat, for the audit log


@dataclass(frozen=True, slots=True)
class SessionRef:
    """Identity of one live session (the master or one sub-agent run).

    Carried between the controller and the view so both agree on which
    transcript tab, which chat window and which chat name a piece of work
    belongs to.
    """

    id: str  # "master", "sub-1", "sub-2", ...
    role: Role
    title: str  # short label for the transcript tab
    chat_name: str  # the generated chat name this session's replies must carry


@dataclass(slots=True)
class SessionStats:
    """Per-session counters accumulated across turns (shown in the summary)."""

    service: str = ""
    replies: int = 0
    calls: Counter[str] = field(default_factory=Counter)
    chars_out: int = 0
    chars_in: int = 0
    summary: str = ""
