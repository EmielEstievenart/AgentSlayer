"""How a session's Engine is assembled - on whatever machine the engine runs on.

This is the engine half of the link (docs/design/remote-executor.md section
2.2): the ONE place a session's registry, workspace jail, session store, backup
store and composer are put together. It lives below the Shell so that a server
process on a target machine can build an engine from a decoded
:class:`EngineRequest` without importing a clipboard, a window toolkit or a
controller - `cli.make_engine_factory` is a three-line wrapper that calls
:func:`make_engine_builder` and hands the result to the Shell behind a
``LocalLink``.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from agentclip.config import Config
from agentclip.engine.engine import Engine
from agentclip.engine.store.backups import BackupStore
from agentclip.engine.store.session import SessionStore
from agentclip.executor.hosts.base import Host
from agentclip.executor.hosts.local import LocalHost
from agentclip.executor.mcp.client import McpManager
from agentclip.executor.tools.mcp_tools import make_mcp_specs
from agentclip.executor.tools.registry import ToolRegistry, default_registry
from agentclip.executor.tools.sandbox import Workspace
from agentclip.executor.tools.skills import Skill, discover_skills
from agentclip.protocol.composer import Composer
from agentclip.protocol.names import generate_chat_name
from agentclip.protocol.spec import render_spec

Role = Literal["master", "subagent"]


@dataclass(frozen=True, slots=True)
class EngineRequest:
    """What the controller asks the engine factory to build.

    A request object rather than a bare service key so role, catalog gating and
    chat naming travel as plain data: the assembly lives down here (it needs the
    tool/store/composer wiring) while the decision to spawn a sub-agent is made
    in `shell/app`, which must not import screen or tui to make it.

    It lives ENGINE-side for the same reason the builder does: it is the message
    a remote engine will be asked to build from, so it must be decodable on a
    machine that has no shell in it at all (docs/design/remote-executor.md
    section 2.2).
    """

    service: str
    role: Role = "master"
    # Whether the `delegate` tool appears in the catalog at all. Only ever true
    # for a master, and only when the sub-agent chat window is fully calibrated:
    # offering a tool the host cannot honour wastes a whole round trip.
    allow_delegate: bool = False
    chat_name: str | None = None  # None -> the factory draws a fresh one
    parent_chat_name: str | None = None  # the delegating chat, for the audit log
    # len() of the exact task text start_task will be given, when the caller
    # already holds it - both controller flows do. The factory sizes the MCP
    # catalog addition against the preset's paste budget (docs/design/mcp.md
    # section 5), and the bootstrap = spec + THIS task: sizing against a guessed
    # task allowance is how a four-sentence task on a 12k preset turned into
    # BudgetExceeded. 0 means "unknown", which falls back to a conservative
    # allowance in the factory.
    task_chars: int = 0


# -- MCP catalog sizing (docs/design/mcp.md section 5: the budget rule) --------

# What the real bootstrap adds BEYOND the rendered spec: render_spec covers
# sections 1-5, and composer.bootstrap then appends section 6 - its fixed
# scaffolding ("SECTION 6 - THE TASK", the ===CLIP:TASK=== marker, the
# newline joins and the ===CLIP:EOM turn=1 chat=...=== line come to ~85 chars
# with a generous chat name). The task itself is NOT an allowance any more:
# both controller flows now pass the real task length on the request
# (EngineRequest.task_chars), because an allowance was exactly how the rule
# broke - a four-sentence task on a 12k preset measured fine at build time
# and raised BudgetExceeded at bootstrap. The fallback below covers only
# callers that cannot know their task yet (tests building registries
# directly); it errs large for the same reason the old reserve erred small.
_MCP_SECTION6_SCAFFOLD = 120
_MCP_TASK_FALLBACK = 1_000

# The MCP listing never takes more than this, however huge the preset's
# budget: past a couple thousand chars a bigger bootstrap teaser stops earning
# its keep - mcp_schema serves the full list on demand for one cheap call.
_MCP_LISTING_CAP = 2_000

# Bounded refinement passes when shrinking the listing to the measured room -
# the same shape (and the same reason) as the composer's _FIT_ATTEMPTS: the
# fixed doc prose rides on top of the listing budget, so the first candidate
# can overshoot and the budget is re-derived from the real overshoot. Failing
# to converge falls through to the drop, never to an over-budget catalog.
_MCP_FIT_ATTEMPTS = 6

# Each pass shrinks by at least this much: the listing shrinks in whole-line
# steps (a line is an id plus a clipped description), so a small overshoot
# alone can stall above the room forever - a line-sized floor sheds roughly
# one listed tool per pass instead.
_MCP_FIT_STEP = 160

# The user-visible half of degradation step 3 (design section 5): both specs
# dropped for the session, said once in the transcript via Engine.build_warnings.
_MCP_DROPPED_WARNING = (
    "paste budget too small for MCP tools on this service - "
    "the mcp/mcp_schema tools were left out of this session"
)


def make_engine_builder(
    get_config: Callable[[], Config],
    project_root: Path,
    chat_name: str | None = None,
    *,
    host: Host | None = None,
    os_name: str | None = None,
    data_root: Path | None = None,
    home: Path | None = None,
    mcp_manager: McpManager | None = None,
) -> Callable[[EngineRequest | str], Engine]:
    """Build one fresh Engine (and session directory) per started session.

    What comes back is the bare engine. This is the assembly BOTH sides share:
    `cli.make_engine_factory` wraps each engine in a
    :class:`~agentclip.shell.app.link.LocalLink` for a local session, and the
    server process a later increment adds will call this builder on the target
    and put the wire behind it instead (docs/design/remote-executor.md section
    2.2). Wrapping is the caller's job precisely so this module never has to
    know a Shell exists.

    ``get_config`` is called fresh on every session start (not once) so that a
    service edited/added/removed via the service editor takes effect for the
    NEXT session in this process without restarting the app - the caller
    typically passes something like ``lambda: app.app_config`` so the factory
    always reads whatever Config is currently live.

    The sidebar's service picker may select a different preset than the config
    default, so the factory rebuilds a Config with that service active - the
    engine reads its budget/caps from config.preset().

    Each session gets a fresh chat name, which the bootstrap teaches the model
    and every reply must echo back. ``chat_name`` pins it (tests need a fixed
    name to write canned replies against); the default draws a new one per
    session, so two sessions never accept each other's pastes.

    The returned callable takes an ``EngineRequest`` - service plus role,
    delegation gating and chat naming - or, for the plain master case, just the
    service key, which is coerced to the equivalent default request.

    ``host`` is the machine the project lives on, and this is the ONE place it
    is chosen: every session this factory builds hands the same Host to the
    workspace jail, the tool context, the backup store and the engine, so a
    session cannot end up half local and half remote. ``os_name`` is what the
    bootstrap's "on {os}" slot says (the REMOTE kernel in a remote session);
    ``data_root`` is where the .agentclip session tree goes when the project
    root is not on this PC; ``home`` is whose home directory holds the global
    skill folders. All of them default to "this machine", which is what every
    local run passes.

    ``mcp_manager`` is the process-wide MCP runtime built in main(), or None
    when MCP is unconfigured - in which case every session this factory builds
    is byte-identical to a pre-MCP one. With a manager, each build sizes the
    mcp/mcp_schema catalog addition against the chosen preset's paste budget
    by MEASUREMENT and degrades before it ever breaks a bootstrap
    (docs/design/mcp.md section 5; see _sized_registry).
    """
    # Skills are discovered once per process from the same folders Claude Code
    # and OpenCode use. The registry is rebuilt per session so the skill listing
    # is bounded to the chosen preset's budget (the bootstrap has no truncation
    # fallback - a big skills library must not be able to overflow it).
    # The rest of the bootstrap is ~9k (spec text plus the built-in catalog), so
    # the listing gets a sixth of the budget, not a quarter: at the 12k presets a
    # quarter leaves no headroom and a full skills library tips it over.
    # Skills describe the project, so in a remote session they are discovered in
    # the remote machine's skill folders (design 6): the project-local ones AND
    # the ones under the REMOTE user's home, all through the same host.
    session_host: Host = host if host is not None else LocalHost()
    session_os = os_name or platform.system() or "unknown OS"
    skills = discover_skills(project_root, home=home, host=session_host)

    def build(request: EngineRequest | str) -> Engine:
        req = EngineRequest(service=request) if isinstance(request, str) else request
        session_chat_name = req.chat_name or chat_name or generate_chat_name()
        cfg = get_config()
        if req.service != cfg.general.service and req.service in cfg.services:
            cfg = replace(cfg, general=replace(cfg.general, service=req.service))
        registry, build_warnings = _sized_registry(
            mcp_manager, cfg, skills, req, project_root.name, session_os, session_chat_name
        )
        workspace = Workspace(project_root, cfg.excluded_names(), host=session_host)
        session = SessionStore(
            project_root, service=cfg.general.service, data_root=data_root
        )
        # Recorded up front so a sub-agent's session directory can be tied back
        # to the run that spawned it when reading the audit log later.
        session.append_event(
            "session",
            role=req.role,
            chat_name=session_chat_name,
            parent_chat_name=req.parent_chat_name,
            allow_delegate=req.allow_delegate,
        )
        backups = BackupStore(session.session_dir, host=session_host)
        composer = Composer(
            cfg.preset(),
            cfg.caps(),
            registry.render_catalog(),
            project_root.name,
            session_os,
            session_chat_name,
            role=req.role,
        )
        return Engine(
            cfg,
            registry,
            workspace,
            session,
            backups,
            composer,
            session_chat_name,
            role=req.role,
            host=session_host,
            build_warnings=build_warnings,
        )

    return build


def _sized_registry(
    manager: McpManager | None,
    cfg: Config,
    skills: Sequence[Skill],
    req: EngineRequest,
    workdir_name: str,
    session_os: str,
    session_chat_name: str,
) -> tuple[ToolRegistry, tuple[str, ...]]:
    """The session's registry, with the MCP tools included only if they FIT.

    THE BUDGET RULE (docs/design/mcp.md section 5, binding): adding MCP must
    never push a preset that bootstrapped without it over its paste budget -
    the bootstrap has no chunked fallback, so over budget is a session that
    never arms. So this measures instead of guessing: render_spec is pure, and
    the same inputs the composer will use render the MCP-free spec once, the
    MCP-carrying spec once, and the difference is the real addition. In order
    of degradation: the listing inside the mcp_schema doc is bounded (its cap
    below), and when even the fixed prose plus mcp_listing's guaranteed one
    line cannot fit the remaining room, both specs are dropped for the session
    and the second return value carries the one-line warning the controller
    surfaces (Engine.build_warnings).
    """
    max_skill_chars = cfg.preset().max_paste_chars // 6
    base = default_registry(
        skills,
        max_skill_listing_chars=max_skill_chars,
        role=req.role,
        allow_delegate=req.allow_delegate,
    )
    # No manager: MCP is unconfigured and this build is byte-identical to a
    # pre-MCP one. All-disabled servers keep the manager for status display but
    # add no catalog text (design section 5, degradation step 1).
    if manager is None or all(s.state == "disabled" for s in manager.statuses()):
        return base, ()
    # Eager-on-arm (design section 3): main() already kicked the connects off
    # at construction; this covers factories built without that path (tests,
    # embedders). The bounded wait is a catch-up, not a gate: when servers are
    # still settling - the first build of an app run, typically - half a second
    # buys the catalog its real tool listing instead of "(none connected
    # yet)", and once everything has settled it returns immediately, so every
    # later build pays nothing.
    manager.ensure_started()
    if any(s.state in ("pending", "connecting") for s in manager.statuses()):
        manager.wait_ready(0.5)

    preset = cfg.preset()
    spec_free = render_spec(
        preset,
        cfg.caps(),
        base.render_catalog(),
        workdir_name,
        session_os,
        session_chat_name,
        role=req.role,
    )
    # The bootstrap this sizing protects is spec + section-6 scaffold + the
    # TASK - so the task is subtracted at its real length whenever the caller
    # knew it (both controller flows do), and at a deliberately fat fallback
    # when it did not.
    task_chars = req.task_chars if req.task_chars > 0 else _MCP_TASK_FALLBACK
    room = preset.max_paste_chars - len(spec_free) - task_chars - _MCP_SECTION6_SCAFFOLD
    # The same budget/6 discipline the skills listing follows, with a hard cap
    # on top and never more than the measured room. The listing budget bounds
    # only the LISTING, and the docs' fixed prose rides on top - so the fit is
    # re-measured and the budget shrunk by the observed overshoot, bounded
    # attempts, the same shape as the composer's results fitting. The listing
    # always emits at least one line however small its budget, which is exactly
    # what makes a hopeless room fail the measurement here (and drop the specs)
    # instead of raising BudgetExceeded at bootstrap time.
    listing_budget = min(room, preset.max_paste_chars // 6, _MCP_LISTING_CAP)
    for _ in range(_MCP_FIT_ATTEMPTS):
        if listing_budget <= 0:
            break
        candidate = default_registry(
            skills,
            max_skill_listing_chars=max_skill_chars,
            role=req.role,
            allow_delegate=req.allow_delegate,
            mcp_specs=make_mcp_specs(manager, max_listing_chars=listing_budget),
        )
        spec_with = render_spec(
            preset,
            cfg.caps(),
            candidate.render_catalog(),
            workdir_name,
            session_os,
            session_chat_name,
            role=req.role,
        )
        addition = len(spec_with) - len(spec_free)
        if addition <= room:
            return candidate, ()
        listing_budget -= max(addition - room, _MCP_FIT_STEP)
    return base, (_MCP_DROPPED_WARNING,)
