"""Command-line entry point: argparse, config, clipboard provider, engine, TUI."""

from __future__ import annotations

import argparse
import getpass
import platform
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentclip import __version__
from agentclip.app.types import EngineRequest
from agentclip.clip.base import select_provider
from agentclip.config import Config, default_remote_state_dir, load_config
from agentclip.engine.engine import Engine
from agentclip.hosts.base import Host
from agentclip.hosts.connect import (
    STEP_CONNECT,
    STEP_ENV,
    STEP_ROOT,
    ConnectedRemote,
    ConnectError,
    ConnectPrompts,
    StepEvent,
    connect_remote,
    parse_environment,
    remote_environment,
)
from agentclip.hosts.local import LocalHost
from agentclip.mcp.client import McpManager
from agentclip.protocol.composer import Composer
from agentclip.protocol.names import generate_chat_name
from agentclip.protocol.spec import render_spec
from agentclip.screen.matchers import MATCHERS, select_matcher
from agentclip.store.backups import BackupStore
from agentclip.store.session import SessionStore, prune_sessions
from agentclip.tools.mcp_tools import make_mcp_specs
from agentclip.tools.registry import ToolRegistry, default_registry
from agentclip.tools.sandbox import Workspace
from agentclip.tools.skills import Skill, discover_skills
from agentclip.tui.app import AgentClipApp
from agentclip.tui.graphics import probe_terminal

if TYPE_CHECKING:  # paramiko rides in with SshHost, so the real import stays lazy
    from agentclip.hosts.ssh import SshHost

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


def make_engine_factory(
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentclip",
        description="Use any web-chat LLM as a coding agent over the clipboard.",
    )
    parser.add_argument(
        "--project",
        default=".",
        help="project root directory the agent works in (default: current directory)",
    )
    parser.add_argument(
        "--service",
        default=None,
        help="service preset key, e.g. chatgpt-attach (see --list-services)",
    )
    parser.add_argument(
        "--list-services",
        action="store_true",
        help="print the configured service presets and exit",
    )
    parser.add_argument(
        "--ssh",
        default=None,
        metavar="TARGET",
        help="work on a remote machine: a [remote.<name>] target, an ~/.ssh/config"
        " alias, or [user@]host[:port]",
    )
    parser.add_argument(
        "--remote-root",
        default=None,
        metavar="PATH",
        help="project root ON the remote machine (required with --ssh unless the"
        " saved target names one)",
    )
    # Hidden: the TUI re-invokes itself with this flag to run the draw-a-box
    # screen overlay in a child process (tkinter can't share the TUI's process).
    parser.add_argument("--pick-region", action="store_true", help=argparse.SUPPRESS)
    # Hidden: the instruction that overlay shows; only meaningful with --pick-region.
    parser.add_argument("--pick-prompt", default=None, help=argparse.SUPPRESS)
    # Hidden: /identify's read-only twin of --pick-region - the element list
    # arrives as JSON on stdin and is drawn where each element sits on screen.
    parser.add_argument("--show-identify", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--gui",
        action="store_true",
        help="launch the experimental GUI shell instead of the TUI",
    )
    parser.add_argument(
        "--list-matchers",
        action="store_true",
        help="print which appearance-matcher backends this build can run, and exit",
    )
    parser.add_argument("--version", action="version", version=f"agentclip {__version__}")
    return parser


def _list_matchers() -> int:
    """Say which candidate-generation backends this build can actually run.

    The service editor already reports this where a user configures it, but by
    then it is a complaint rather than a check - and it is unanswerable from
    outside the TUI, which is the one place a *build* has to be able to answer
    it. Bundling OpenCV into the frozen exe (architecture.md 6) is only correct
    if it is really in there and really loads, and "the file is in the archive"
    is not the same claim: a onefile build extracts to a temp directory and a
    compiled extension can still fail to find its DLLs. So this actually
    imports the backend and reports what happened, which is what
    scripts/build-exe.ps1 runs against the exe it just produced.
    """
    frozen = bool(getattr(sys, "frozen", False))
    print(f"agentclip {__version__} ({'frozen build' if frozen else 'from source'})")
    for name in MATCHERS:
        chosen = select_matcher(name)
        if chosen.name == name:
            print(f"  {name:<8} available")
        else:
            print(f"  {name:<8} NOT AVAILABLE - would fall back to {chosen.name!r}")
    return 0


@dataclass(frozen=True, slots=True)
class Launch:
    """Everything a session needs to know about WHERE it runs.

    Assembled before the TUI starts, by :func:`local_launch` or
    :func:`remote_launch`, and never mixed: one session is one machine (design
    decision 4). ``data_root`` is where the ``.agentclip`` tree goes - beside
    the project locally, on this PC for a remote one - and ``home`` is whose
    home directory holds the global skill folders.
    """

    project_root: Path
    config: Config
    host: Host
    os_name: str
    data_root: Path
    home: Path


@dataclass(frozen=True, slots=True)
class GuiRuntime:
    """A :class:`Launch` after everything derived from it has been built.

    What the GUI's connect dialog gets back when it goes remote mid-window
    (``gui/remote.py:RemoteRuntime``, structurally): the config read off the
    target, the engine factory over its host, and the MCP runtime built from ITS
    servers. The TUI has no equivalent because its launch cannot change - the
    process is already inside ``app.run()`` by the time a user could ask.
    """

    project_root: Path
    config: Config
    engine_factory: Callable[[EngineRequest], Engine]
    mcp_manager: McpManager | None
    host: Host
    target: str


def local_launch(args: argparse.Namespace) -> Launch | int:
    """The ordinary session: this PC's project, this PC's OS. Errors return 2."""
    try:
        project_root = Path(args.project).resolve(strict=True)
    except OSError as exc:
        print(f"agentclip: cannot resolve --project {args.project!r}: {exc}", file=sys.stderr)
        return 2
    if not project_root.is_dir():
        print(f"agentclip: --project is not a directory: {project_root}", file=sys.stderr)
        return 2
    return Launch(
        project_root=project_root,
        config=load_config(project_root, service_override=args.service),
        host=LocalHost(),
        os_name=platform.system() or "unknown OS",
        data_root=project_root,
        home=Path.home(),
    )


def ask_password(prompt: str) -> str | None:
    """Read a secret from the terminal. Only ever called before the TUI starts."""
    try:
        return getpass.getpass(prompt) or None
    except (EOFError, KeyboardInterrupt, OSError):
        return None


def confirm_host_key(hostname: str, keytype: str, fingerprint: str) -> bool:
    """OpenSSH's own question, asked in OpenSSH's own words. No auto-add."""
    print(f"The authenticity of host '{hostname}' can't be established.")
    print(f"{keytype} key fingerprint is {fingerprint}.")
    try:
        answer = input("Are you sure you want to continue connecting (yes/no)? ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("yes", "y")


def _parse_environment(text: str) -> dict[str, str]:
    """``printenv`` output as a mapping - :func:`hosts.connect.parse_environment`.

    Kept as a name here because it is what the launch tests call; the rule it
    encodes moved down with the sequence it belongs to.
    """
    return parse_environment(text)


def _remote_environment(host: SshHost) -> dict[str, str]:
    """The target's login-shell environment, with the complaint on stderr.

    The read itself is :func:`hosts.connect.remote_environment`, which hands
    back a (mapping, complaint) pair because it has no stream to write to. This
    is the terminal's half of that pair, unchanged: an unusable answer is a note
    and an empty environment, never a failed launch.
    """
    environment, complaint = remote_environment(host)
    if complaint:
        print(f"agentclip: {complaint}", file=sys.stderr)
    return environment


def _print_step(event: StepEvent) -> None:
    """The connect sequence's progress, on the streams it has always used.

    Three of the eighteen (step, state) pairs say anything on this path, and
    each keeps the stream it had before the sequence moved: the "connecting to
    ..." line is stderr (it is progress, not output), the "<box> is Linux, ...
    working in ..." line is stdout (it is the launch reporting where the session
    landed), and a failed step says nothing here at all - ``remote_launch``
    prints its message once, next to the exit code it returns.
    """
    if event.step == STEP_CONNECT and event.state == "running":
        print(f"agentclip: {event.note}", file=sys.stderr)
    elif event.step == STEP_ROOT and event.state == "ok":
        print(f"agentclip: {event.note}")
    elif event.step == STEP_ENV and event.state == "ok" and event.note:
        print(f"agentclip: {event.note}", file=sys.stderr)


def remote_launch(args: argparse.Namespace) -> Launch | int:
    """Connect, authenticate and probe BEFORE the TUI starts (design 7).

    A thin wrapper now: the sequence lives in
    :func:`agentclip.hosts.connect.connect_remote` so the GUI's connect dialog
    drives the identical one (docs/design/ui-briefs/ssh-connect.md). What stays
    here is what a terminal launch IS - the two blocking prompts, the stderr
    wording, and exit code 2 for every fatal step. Order matters and is the
    design's: CLI flags + the LOCAL global config name the target; the
    connection is made and the remote root checked; only then is the REMOTE
    project's ``.agentclip.toml`` read, through the host, into the config the
    session actually runs on. Every failure here is fatal and explained on
    stderr - a half-connected session is not a thing.
    """
    try:
        remote = connect_remote(
            args.ssh,
            args.remote_root,
            local_root=Path(args.project).resolve(),
            service_override=args.service,
            prompts=ConnectPrompts(password=ask_password, host_key=confirm_host_key),
            on_step=_print_step,
        )
    except ConnectError as err:
        print(f"agentclip: {err.message}", file=sys.stderr)
        return 2
    return Launch(
        project_root=remote.project_root,
        config=remote.config,
        host=remote.host,
        os_name=remote.os_name,
        # The same pure function ``connect_remote`` already applied (and the
        # same answer, off the same two values it carries), called again at the
        # seam this module owns: ``Launch`` is cli's shape, and both of its
        # launches resolve their state dir here.
        data_root=default_remote_state_dir(remote.host.target, remote.project_root.as_posix()),
        home=remote.home,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.pick_region:
        return _pick_region_child(args.pick_prompt)
    if args.show_identify:
        return _show_identify_child(sys.stdin.read())
    if args.list_matchers:
        return _list_matchers()

    # WHERE the session runs, decided before either shell exists - except on the
    # one path where it is decided AFTER: ``--gui --ssh`` no longer blocks the
    # launch on a terminal dial. The window opens on this PC and the connect
    # dialog runs the identical sequence in-app, with a checklist and a retry
    # (docs/design/ui-briefs/ssh-connect.md; gui.md §2's "everything slow happens
    # after first paint"). The TUI keeps the launch-time flow verbatim: it cannot
    # prompt once Textual owns the terminal, which is the whole reason for the
    # carve-out.
    pending_connect = (args.ssh, args.remote_root) if (args.gui and args.ssh) else None
    if args.ssh and pending_connect is None:
        launch = remote_launch(args)
    else:
        launch = local_launch(args)
    if isinstance(launch, int):
        return launch
    config = launch.config

    if args.list_services:
        for key in sorted(config.services):
            preset = config.services[key]
            marker = "*" if key == config.general.service else " "
            print(f"{marker} {key:<16} {preset.max_paste_chars:>9,} chars  {preset.label}")
        return 0

    launch.data_root.mkdir(parents=True, exist_ok=True)
    prune_sessions(launch.data_root, config.backup.keep_sessions)
    # The clipboard backend and the MCP runtime are shell-agnostic - both shells
    # drive the same AutomationController and the same engine factory - so they
    # are built ABOVE the fork, exactly as gui.md section 0 said they would rise
    # as the GUI grew. The sixel probe is the one step that is NOT: it asks a
    # terminal questions over stdin/stdout, and there is no terminal on the GUI
    # path, so it stays below the branch.
    provider = select_provider(config.clipboard.provider)
    # The MCP runtime: built ONCE per process, the same lifetime as skill
    # discovery (docs/design/mcp.md section 3). Every configured server goes in
    # - disabled ones too, so statuses() can say "disabled" - but nothing
    # connects until the first session build calls ensure_started(). With no
    # servers (or [mcp] enabled=false, which loads none) there is NO manager at
    # all: the factory and the app hold None and behave exactly as before MCP
    # existed.
    #
    # A launch with a connect PENDING builds none of it: those servers would be
    # this PC's, read from this PC's opencode.json, for a session that is about
    # to belong to another machine - and "the host PC's file is not consulted at
    # all in a remote session" is the rule, not a preference
    # (docs/design/remote-ssh.md, "the target owns its policy"). The runtime the
    # connect builds carries the target's instead.
    mcp_manager: McpManager | None = None
    if config.mcp.enabled and config.mcp_servers.servers and pending_connect is None:
        # The host's name, not a bare flag: in a remote session it is what the
        # refused stdio servers and the "dialed from this PC" note are ABOUT,
        # and a status line that names the box beats one that says "remote".
        mcp_manager = McpManager(
            config.mcp_servers.servers,
            launch.project_root,
            remote_target=launch.host.name if config.remote.is_remote() else "",
        )
        # Kick the connects off NOW, not at the first session build: they
        # overlap the terminal probe, the shell's first paint and the user
        # typing their task, so the first bootstrap usually lists real tools
        # instead of the guaranteed-empty listing a build-time kick-off produced
        # (the connect has not begun when the catalog snapshot is taken
        # microseconds later). Still non-blocking - a slow server delays nothing.
        mcp_manager.ensure_started()
    # The fork between the two shells (docs/design/gui.md section 0). It sits
    # here, after everything about WHERE the session runs is settled and before
    # the first TUI-only step. Imported inside the function because pywebview is
    # the optional `gui` extra - a TUI launch must not pay for the import, and
    # an install without the extra must still run.
    #
    # The engine factory is built the same way for both, with the same
    # arguments; only the SHAPE of the "read the live Config" closure differs.
    # The TUI's reads ``app.app_config``, an attribute its service editor
    # reassigns. The GUI's window does not exist yet at this line, so the cell
    # is here and the shell writes it back through ``on_config_change`` - both
    # shells therefore build the next session's Engine from whatever the editor
    # last saved, and neither touches a session already in flight.
    if args.gui:
        from agentclip.gui.remote import RemoteConnect
        from agentclip.gui.shell import run_gui

        live_config = [config]
        # What the process currently OWNS, as opposed to what it was launched
        # with: an in-app connect replaces both, and the teardown below has to
        # close what is live rather than what was true at startup.
        owned: dict[str, Any] = {"mcp": mcp_manager, "host": launch.host}

        def adopt_config(edited: Config) -> None:
            live_config[0] = edited

        def build_runtime(remote: ConnectedRemote) -> GuiRuntime:
            """A successful in-app connect, turned into a session's ingredients.

            Everything ``main`` does above for a launch, done again for the box
            that was just dialled - the session tree, the pruning, the MCP
            runtime against the TARGET's servers, and an engine factory over the
            remote host, root, OS name and home. It lives here rather than in
            the shell for the reason ``run_gui`` is handed its factory at all:
            how a session is BUILT is a launch question, and a second
            construction site is a second thing to drift.

            The previous host and MCP runtime are closed as the new ones take
            over - one session, one host (remote-ssh.md decision 4), and a link
            nobody can reach any more is a socket, not a session.
            """
            live_config[0] = remote.config
            remote.data_root.mkdir(parents=True, exist_ok=True)
            prune_sessions(remote.data_root, remote.config.backup.keep_sessions)
            manager: McpManager | None = None
            if remote.config.mcp.enabled and remote.config.mcp_servers.servers:
                manager = McpManager(
                    remote.config.mcp_servers.servers,
                    remote.project_root,
                    remote_target=remote.host.name,
                )
                manager.ensure_started()
            previous, owned["mcp"] = owned["mcp"], manager
            if previous is not None:
                previous.close()
            old_host, owned["host"] = owned["host"], remote.host
            if old_host is not None and old_host is not remote.host:
                closer = getattr(old_host, "close", None)
                if closer is not None:
                    closer()
            return GuiRuntime(
                project_root=remote.project_root,
                config=remote.config,
                engine_factory=make_engine_factory(
                    lambda: live_config[0],
                    remote.project_root,
                    host=remote.host,
                    os_name=remote.os_name,
                    data_root=remote.data_root,
                    home=remote.home,
                    mcp_manager=manager,
                ),
                mcp_manager=manager,
                host=remote.host,
                target=remote.host.target,
            )

        try:
            return run_gui(
                launch,
                provider=provider,
                on_config_change=adopt_config,
                engine_factory=make_engine_factory(
                    lambda: live_config[0],
                    launch.project_root,
                    host=launch.host,
                    os_name=launch.os_name,
                    data_root=(
                        launch.data_root if launch.data_root != launch.project_root else None
                    ),
                    home=launch.home,
                    mcp_manager=mcp_manager,
                ),
                mcp_manager=mcp_manager,
                host=launch.host,
                remote=RemoteConnect(
                    local_root=Path(args.project).resolve(),
                    build=build_runtime,
                    service_override=args.service,
                    pending=pending_connect,
                ),
            )
        finally:
            # The same hand-back the TUI path does below, over what is LIVE: a
            # --ssh launch has a connection open by now whichever shell it was
            # headed for, and so does one the user dialled from the dialog.
            if owned["mcp"] is not None:
                owned["mcp"].close()
            close = getattr(owned["host"], "close", None)
            if close is not None:
                close()
    # BEFORE app.run(), and this is the only place it may happen: probing asks
    # the terminal questions over stdin/stdout, and once Textual starts its own
    # reader thread the answers go to Textual instead - which is exactly how the
    # ELEMENTS column ends up silently drawing blocks on a terminal that can do
    # sixel (tui.graphics, tui.md 1.7).
    probe_terminal()
    app = AgentClipApp(
        config=config,
        provider=provider,
        # app.app_config is reassigned in place when the service editor saves, so
        # this closure keeps reading whatever config is current.
        engine_factory=make_engine_factory(
            lambda: app.app_config,
            launch.project_root,
            host=launch.host,
            os_name=launch.os_name,
            data_root=launch.data_root if launch.data_root != launch.project_root else None,
            home=launch.home,
            mcp_manager=mcp_manager,
        ),
        project_root=launch.project_root,
        mcp_manager=mcp_manager,
    )
    try:
        app.run()
    finally:
        if mcp_manager is not None:
            mcp_manager.close()
        close = getattr(launch.host, "close", None)
        if close is not None:
            close()
    return 0


def _pick_region_child(prompt: str | None = None) -> int:
    """The --pick-region child: overlay up, region wire format out, exit.

    Cancel is exit 0 with no output (the parent's parse_region yields None);
    a broken environment (no tkinter, no display) is exit 1 with the reason on
    stderr, which screen.picker surfaces as a ScreenPickError.
    """
    from agentclip.screen.region import format_region

    try:
        from agentclip.screen.overlay import run_overlay

        region = run_overlay(prompt)
    except Exception as exc:  # anything here means "picker unavailable"
        print(f"region picker unavailable: {exc}", file=sys.stderr)
        return 1
    if region is not None:
        print(format_region(region))
    return 0


def _show_identify_child(payload: str) -> int:
    """The --show-identify child: boxes up, wait for a dismissal, exit.

    Prints nothing on success - the overlay IS the result. A malformed payload
    or a broken environment (no tkinter, no display) is exit 1 with the reason
    on stderr, which screen.picker surfaces as a ScreenPickError; the parent
    toasts that instead of leaving the user staring at a screen where nothing
    happened.
    """
    from agentclip.screen.identify import parse_payload

    try:
        elements = parse_payload(payload)
    except ValueError as exc:
        print(f"identify overlay got a bad payload: {exc}", file=sys.stderr)
        return 1
    try:
        from agentclip.screen.overlay import run_identify_overlay

        run_identify_overlay(elements)
    except Exception as exc:  # anything here means "overlay unavailable"
        print(f"identify overlay unavailable: {exc}", file=sys.stderr)
        return 1
    return 0
