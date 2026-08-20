"""How a session's Engine is assembled - on whatever machine the engine runs on.

This is the engine half of the link (docs/design/remote-executor.md section
2.2): the ONE place a session's registry, workspace jail, session store, backup
store and composer are put together. It lives below the Shell so that a server
process on a target machine can build an engine from a decoded
:class:`EngineRequest` without importing a clipboard, a window toolkit or a
controller - `cli.make_engine_factory` is a three-line wrapper that calls
:func:`make_engine_builder` and hands the result to the Shell behind a
``LocalLink``.

The MCP runtime is built HERE too, and that is the whole of section 2.7: MCP
servers belong to the machine the engine runs on, so the manager is constructed
at the same altitude as skill discovery, from the config THIS side reads. In the
``agentclip-engine`` process that means a stdio server spawns on the target with
the target's environment; on this PC it means exactly what it meant when
``cli.main()`` built it. The Shell reaches its statuses through the builder
object rather than by importing ``executor.mcp`` at all.
"""

from __future__ import annotations

import platform
import threading
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
from agentclip.executor.mcp.types import McpServerStatus
from agentclip.executor.tools.mcp_tools import make_mcp_specs
from agentclip.executor.tools.registry import ToolRegistry, default_registry
from agentclip.executor.tools.sandbox import Workspace
from agentclip.executor.tools.skills import (
    Skill,
    SkillReport,
    discover_skills,
    skill_report,
    skill_search_roots,
)
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


class EngineBuilder:
    """Build one fresh Engine (and session directory) per started session.

    Callable - ``builder(request)`` is the whole of the build interface, which
    is why ``server.serve`` can keep taking a plain callable and the ``cli``
    wrapper can keep looking like a function. What the object adds beyond the
    call is the MCP runtime it OWNS: ``mcp_statuses``, ``set_mcp_status_hook``
    and ``close``, so a Shell can paint the status pane without importing
    ``executor.mcp`` and a process can hand the loop thread back on the way out.

    What comes back from a call is the bare engine. This is the assembly BOTH
    sides share: `cli.make_engine_factory` wraps each engine in a
    :class:`~agentclip.shell.app.link.LocalLink` for a local session, and the
    ``agentclip-engine`` server process calls this builder on the target and
    puts the wire behind it instead (docs/design/remote-executor.md section
    2.2). Wrapping is the caller's job precisely so this module never has to
    know a Shell exists.

    ``get_config`` is called fresh on every session start (not once) so that a
    service edited/added/removed via the service editor takes effect for the
    NEXT session in this process without restarting the app - the caller
    typically passes something like ``lambda: app.app_config`` so the builder
    always reads whatever Config is currently live.

    The sidebar's service picker may select a different preset than the config
    default, so the builder rebuilds a Config with that service active - the
    engine reads its budget/caps from config.preset().

    Each session gets a fresh chat name, which the bootstrap teaches the model
    and every reply must echo back. ``chat_name`` pins it (tests need a fixed
    name to write canned replies against); the default draws a new one per
    session, so two sessions never accept each other's pastes.

    A call takes an ``EngineRequest`` - service plus role, delegation gating and
    chat naming - or, for the plain master case, just the service key, which is
    coerced to the equivalent default request.

    ``host`` is the machine the project lives on, and this is the ONE place it
    is chosen: every session this builder makes hands the same Host to the
    workspace jail, the tool context, the backup store and the engine, so a
    session cannot end up half local and half remote. ``os_name`` is what the
    bootstrap's "on {os}" slot says (the REMOTE kernel in a remote session);
    ``data_root`` is where the .agentclip session tree goes when the project
    root is not on this PC; ``home`` is whose home directory holds the global
    skill folders. All of them default to "this machine", which is what every
    local run passes.

    ``mcp_remote_target`` names the machine the config came off when this
    builder is the legacy per-call ``SshHost`` path's - the one arrangement
    where the config describes one box and the process spawning servers is on
    another (see :meth:`_mcp`). It is "" everywhere else, including in the
    ``agentclip-engine`` process, which IS its machine. ``mcp_enabled`` is the
    single launch that must build no MCP runtime at all: a GUI window opened
    with a connect PENDING, whose servers would be this PC's for a session
    about to belong to another one.
    """

    __slots__ = (
        "_get_config",
        "_project_root",
        "_chat_name",
        "_host",
        "_os_name",
        "_data_root",
        "_skills",
        "_skill_report",
        "_mcp_remote_target",
        "_mcp_enabled",
        "_mcp_lock",
        "_manager",
        "_mcp_settled",
        "_status_hook",
        "_closed",
    )

    def __init__(
        self,
        get_config: Callable[[], Config],
        project_root: Path,
        chat_name: str | None = None,
        *,
        host: Host | None = None,
        os_name: str | None = None,
        data_root: Path | None = None,
        home: Path | None = None,
        mcp_remote_target: str = "",
        mcp_enabled: bool = True,
    ) -> None:
        self._get_config = get_config
        self._project_root = project_root
        self._chat_name = chat_name
        # Skills are discovered once per builder from the same folders Claude
        # Code and OpenCode use. The registry is rebuilt per session so the
        # skill listing is bounded to the chosen preset's budget (the bootstrap
        # has no truncation fallback - a big skills library must not be able to
        # overflow it). The rest of the bootstrap is ~9k (spec text plus the
        # built-in catalog), so the listing gets a sixth of the budget, not a
        # quarter: at the 12k presets a quarter leaves no headroom and a full
        # skills library tips it over. Skills describe the project, so in a
        # remote session they are discovered in the remote machine's skill
        # folders (design 6): the project-local ones AND the ones under the
        # REMOTE user's home, all through the same host.
        self._host: Host = host if host is not None else LocalHost()
        self._os_name = os_name or platform.system() or "unknown OS"
        self._data_root = data_root
        self._skills = discover_skills(project_root, home=home, host=self._host)
        # The same discovery, body-free and with the folders it scanned, for the
        # Shell above (:meth:`skills`). Built here rather than on demand because
        # both halves of it are already settled: discovery happens once per
        # builder, and the roots are a pure function of the arguments.
        self._skill_report = skill_report(self._skills, skill_search_roots(project_root, home))
        self._mcp_remote_target = mcp_remote_target
        self._mcp_enabled = mcp_enabled
        self._mcp_lock = threading.Lock()
        self._manager: McpManager | None = None
        self._mcp_settled = False
        self._status_hook: Callable[[McpServerStatus], None] | None = None
        self._closed = False

    # -- the assembly ---------------------------------------------------------

    def __call__(self, request: EngineRequest | str) -> Engine:
        req = EngineRequest(service=request) if isinstance(request, str) else request
        session_chat_name = req.chat_name or self._chat_name or generate_chat_name()
        cfg = self._get_config()
        if req.service != cfg.general.service and req.service in cfg.services:
            cfg = replace(cfg, general=replace(cfg.general, service=req.service))
        registry, build_warnings = _sized_registry(
            self._mcp(),
            cfg,
            self._skills,
            req,
            self._project_root.name,
            self._os_name,
            session_chat_name,
        )
        workspace = Workspace(self._project_root, cfg.excluded_names(), host=self._host)
        session = SessionStore(
            self._project_root, service=cfg.general.service, data_root=self._data_root
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
        backups = BackupStore(session.session_dir, host=self._host)
        composer = Composer(
            cfg.preset(),
            cfg.caps(),
            registry.render_catalog(),
            self._project_root.name,
            self._os_name,
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
            host=self._host,
            build_warnings=build_warnings,
        )

    # -- what this builder found beside the project ---------------------------

    def skills(self) -> SkillReport:
        """Every skill this builder loaded, and the folders it looked in.

        The Shell's whole read of the skills library - `/skills` - and it is the
        BUILDER's to answer for the same reason ``mcp_statuses`` is: discovery
        ran on the machine the engine runs on, through this builder's host, so in
        a remote session these are the target's folders and not the operator's.
        Bodies are left out on purpose; the model fetches those one at a time
        through the ``skill`` tool.
        """
        return self._skill_report

    # -- the MCP runtime this builder owns ------------------------------------

    def mcp_statuses(self) -> tuple[McpServerStatus, ...]:
        """One row per configured server, or () when MCP is unconfigured.

        The Shell's whole read of the MCP runtime, and the reason it never
        imports ``executor.mcp``: this and :meth:`set_mcp_status_hook` are the
        two calls the sidebar block and the ``/mcp`` listing are made of
        (docs/design/mcp.md sections 3 and 6).
        """
        manager = self._mcp()
        return () if manager is None else manager.statuses()

    def set_mcp_status_hook(self, hook: Callable[[McpServerStatus], None] | None) -> None:
        """Register the one listener told about every state change. A no-op
        without a manager, so a Shell needs no MCP-shaped branch of its own."""
        self._status_hook = hook
        manager = self._mcp()
        if manager is not None:
            manager.set_status_hook(hook)

    def close(self) -> None:
        """Hand the MCP loop thread back. Idempotent, and safe with no manager.

        The engine half owns the runtime, so the process that owns the builder
        closes it: ``cli.main``'s ``finally`` for a local run, the
        ``agentclip-engine`` process's for a remote one.
        """
        with self._mcp_lock:
            self._closed = True
            manager, self._manager = self._manager, None
        if manager is not None:
            manager.close()

    def _mcp(self) -> McpManager | None:
        """The manager, built on first ask and then remembered.

        Built ONCE per builder - the same lifetime as skill discovery, which is
        what docs/design/mcp.md section 3 has always said - and from the config
        THIS side reads, which is what makes an ``agentclip-engine`` process
        spawn its stdio servers on the target with the target's environment
        (remote-executor.md section 2.7 reverses remote-ssh.md here).

        "First ask" rather than construction time because the config closure a
        Shell hands in typically closes over the object it is about to be passed
        to (``lambda: app.app_config``): it is callable from the moment that
        constructor returns, not before. Every production caller asks
        immediately - ``cli.main`` reads :meth:`mcp_statuses` to decide whether
        the shells get a status source at all - so the connects still kick off
        at launch, overlapping the first paint exactly as they did when
        ``main()`` built the manager itself.

        Nothing connects at construction: ``ensure_started`` schedules and
        returns (the design's "lazy but eager-on-arm"), and every configured
        server goes in - disabled ones too, so ``statuses()`` can say
        "disabled".
        """
        with self._mcp_lock:
            if self._mcp_settled or self._closed:
                return self._manager
            self._mcp_settled = True
            if not self._mcp_enabled:
                return None
            cfg = self._get_config()
            # With no servers (or [mcp] enabled=false, which loads none) there
            # is NO manager at all: every session this builder makes is
            # byte-identical to a pre-MCP one.
            if not (cfg.mcp.enabled and cfg.mcp_servers.servers):
                return None
            manager = McpManager(
                cfg.mcp_servers.servers,
                self._project_root,
                remote_target=self._mcp_remote_target,
            )
            if self._status_hook is not None:
                manager.set_status_hook(self._status_hook)
            manager.ensure_started()
            self._manager = manager
            return manager


def make_engine_builder(
    get_config: Callable[[], Config],
    project_root: Path,
    chat_name: str | None = None,
    *,
    host: Host | None = None,
    os_name: str | None = None,
    data_root: Path | None = None,
    home: Path | None = None,
    mcp_remote_target: str = "",
    mcp_enabled: bool = True,
) -> EngineBuilder:
    """One :class:`EngineBuilder`; see its docstring for every argument."""
    return EngineBuilder(
        get_config,
        project_root,
        chat_name,
        host=host,
        os_name=os_name,
        data_root=data_root,
        home=home,
        mcp_remote_target=mcp_remote_target,
        mcp_enabled=mcp_enabled,
    )


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
    # The ranged-edit mode is a fact about the SERVICE, so it is read here, from
    # the config this build is for, and nothing downstream has to be told: a
    # remote session builds its registry through this same function on the
    # engine's side of the link, so SSH gets it for free.
    edit_by_lines = cfg.preset().edit_by_lines
    base = default_registry(
        skills,
        max_skill_listing_chars=max_skill_chars,
        role=req.role,
        allow_delegate=req.allow_delegate,
        edit_by_lines=edit_by_lines,
    )
    # No manager: MCP is unconfigured and this build is byte-identical to a
    # pre-MCP one. All-disabled servers keep the manager for status display but
    # add no catalog text (design section 5, degradation step 1).
    if manager is None or all(s.state == "disabled" for s in manager.statuses()):
        return base, ()
    # Eager-on-arm (design section 3): the builder kicked the connects off the
    # moment its manager was made; this covers a manager handed straight in
    # (tests, embedders). The bounded wait is a catch-up, not a gate: when servers are
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
            edit_by_lines=edit_by_lines,
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
