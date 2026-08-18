"""UI-agnostic value types shared by the session controller and any view.

These are plain dataclasses with no Textual/clipboard dependency so the
orchestration layer (and a future non-Textual UI) can use them freely.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# The role vocabulary is the ENGINE's - it decides which catalog a session gets
# and stamps every audit line - and it travels to the engine on an
# ``EngineRequest``, which lives with the assembly that reads it
# (docs/design/remote-executor.md section 2.2). The types below name it because
# a session ref has to say which kind of run it refers to; they do not own it.
from agentclip.engine.link.factory import Role


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """What the New-Session prompt returns: the task plus a service preset per role.

    Two services, not one, because AgentClip drives two browser windows and the
    user picks a service *per window tab* (tui.md 1.6) - a big-context chat for
    the conversation they steer, something cheap and fast for delegated
    sub-tasks. Both are frozen for the session's life, which is why they travel
    together in the spec rather than being pulled from the view mid-run: the
    master's budget is baked into its Engine at bootstrap, and the sub-agent
    tab's picker is locked for exactly as long.

    ``subagent_service`` is blank when the sub-agent window is on the same
    service as the master's, so a front-end that has no second picker (and every
    test that predates one) can keep building a one-service spec.
    """

    task: str
    service: str
    subagent_service: str = ""


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
    # Delegated sub-agent runs started from this session (master stats only; a
    # sub-agent cannot delegate, so its own counter never moves).
    subagents: int = 0
