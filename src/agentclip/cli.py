"""Command-line entry point: argparse, config, clipboard provider, engine, TUI."""

from __future__ import annotations

import argparse
import getpass
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentclip import __version__
from agentclip.config import Config, default_remote_state_dir, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.driver.screen.matchers import MATCHERS, select_matcher
from agentclip.engine.link.factory import EngineRequest, make_engine_builder
from agentclip.engine.store.session import prune_sessions
from agentclip.executor.hosts.base import Host
from agentclip.executor.hosts.connect import (
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
from agentclip.executor.hosts.local import LocalHost
from agentclip.executor.mcp.client import McpManager
from agentclip.shell.app.link import Link, LocalLink
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.graphics import probe_terminal

if TYPE_CHECKING:  # paramiko rides in with SshHost, so the real import stays lazy
    from agentclip.executor.hosts.ssh import SshHost


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
) -> Callable[[EngineRequest | str], Link]:
    """The local mode's session factory: the shared builder, behind a LocalLink.

    The assembly itself lives in
    :func:`agentclip.engine.link.factory.make_engine_builder` - engine-side, so a
    server process on a target machine can run it without a shell in the process
    (docs/design/remote-executor.md section 2.2). All this adds is the seam the
    Shell drives a session through: what comes back is a
    :class:`~agentclip.shell.app.link.Link`, never the engine itself, and the
    remote mode this prepares for will hand back a ``RemoteLink`` over the same
    interface without the Shell noticing which side of the wire its session is
    on. Wrapping happens HERE rather than in the builder because `cli` is the one
    module allowed to know both halves.

    Every argument is the builder's - see its docstring for what each one means
    and for the per-session rules (fresh config read, fresh chat name, one host
    per session, MCP catalog sizing). The name stays ``make_engine_factory``
    because an engine is still exactly what it builds - the link is how the
    caller reaches it.
    """
    builder = make_engine_builder(
        get_config,
        project_root,
        chat_name,
        host=host,
        os_name=os_name,
        data_root=data_root,
        home=home,
        mcp_manager=mcp_manager,
    )

    def build(request: EngineRequest | str) -> Link:
        return LocalLink(builder(request))

    return build


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
    # Hidden: --list-matchers' twin for the GUI shell - does THIS build carry a
    # working window? Run against the frozen exe by scripts/build-exe.ps1; see
    # _gui_smoke for what it proves and what it deliberately does not.
    parser.add_argument("--gui-smoke", action="store_true", help=argparse.SUPPRESS)
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


def _gui_smoke() -> int:
    """Say whether this BUILD can open the GUI window - without opening one.

    ``--list-matchers``' twin, and it exists for the same reason (see its
    docstring): the exe bundles an optional extra whose absence is silent at
    runtime. ``--gui`` on a build that lost pywebview prints the "install the
    gui extra" line a frozen user can do nothing about, and a build that kept
    pywebview but lost the packaged page opens a window on nothing - neither
    fails until somebody launches the GUI.

    So this does the three things ``run_gui`` does before it has a window, in
    the same order, and then stops: import ``webview`` (which drags in the
    winforms backend, clr and the .NET runtime - the half PyInstaller can only
    see because the spec names it), resolve the assets through
    ``importlib.resources`` and read every one of them (the classic frozen-app
    failure: the files are IN the archive but the resource reader cannot find
    them under ``_MEIPASS``), and ask ``webview2_missing()``.

    A machine without the WebView2 runtime still exits 0 and says so. What is
    under test is the FREEZE, not the box the build ran on - a build server
    with no Evergreen runtime must not be able to fail a packaging check, and
    the renderer word is printed so the state is never merely assumed.
    """
    from agentclip.shell.gui.shell import ASSET_NAMES, asset_dir, webview2_missing

    try:
        import webview  # noqa: F401
    except ImportError as exc:
        print(f"gui-smoke: pywebview is not in this build ({exc})", file=sys.stderr)
        return 2
    with asset_dir() as assets:
        for name in ASSET_NAMES:
            try:
                text = (assets / name).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"gui-smoke: asset {name} is not readable ({exc})", file=sys.stderr)
                return 2
            if not text.strip():
                print(f"gui-smoke: asset {name} is empty", file=sys.stderr)
                return 2
    if platform.system() != "Windows":
        renderer = "n/a"
    else:
        renderer = "missing" if webview2_missing() else "edgechromium"
    print(f"gui-smoke: ok renderer={renderer}")
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
    (``shell/gui/remote.py:RemoteRuntime``, structurally): the config read off the
    target, the engine factory over its host, and the MCP runtime built from ITS
    servers. The TUI has no equivalent because its launch cannot change - the
    process is already inside ``app.run()`` by the time a user could ask.
    """

    project_root: Path
    config: Config
    engine_factory: Callable[[EngineRequest], Link]
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
    """``printenv`` output as a mapping - :func:`executor.hosts.connect.parse_environment`.

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
    :func:`agentclip.executor.hosts.connect.connect_remote` so the GUI's connect dialog
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
    if args.gui_smoke:
        return _gui_smoke()

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
    # this PC's, read from this PC's permissions.json, for a session that is about
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
        from agentclip.shell.gui.remote import RemoteConnect
        from agentclip.shell.gui.shell import run_gui

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
    from agentclip.driver.screen.region import format_region

    try:
        from agentclip.driver.screen.overlay import run_overlay

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
    from agentclip.driver.screen.identify import parse_payload

    try:
        elements = parse_payload(payload)
    except ValueError as exc:
        print(f"identify overlay got a bad payload: {exc}", file=sys.stderr)
        return 1
    try:
        from agentclip.driver.screen.overlay import run_identify_overlay

        run_identify_overlay(elements)
    except Exception as exc:  # anything here means "overlay unavailable"
        print(f"identify overlay unavailable: {exc}", file=sys.stderr)
        return 1
    return 0
