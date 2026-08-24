"""Command-line entry point: argparse, config, clipboard provider, engine, shell."""

from __future__ import annotations

import argparse
import getpass
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentclip import __version__
from agentclip.config import Config, default_remote_state_dir, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.driver.screen.matchers import MATCHERS, select_matcher
from agentclip.engine.link.factory import EngineBuilder, EngineRequest, make_engine_builder
from agentclip.engine.link.wire import EngineLinkError
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
from agentclip.executor.mcp.types import McpServerStatus
from agentclip.shell.app.engine_launch import (
    classify_launch_failure,
    engine_command,
    fallback_engine_command,
    is_missing_engine,
)
from agentclip.shell.app.link import Link, LocalLink, McpStatusLine, SkillReport
from agentclip.shell.app.remote_link import LINK_VERSION, RemoteLinkClient
from agentclip.shell.app.sizes import fmt_tokens

if TYPE_CHECKING:  # paramiko rides in with SshHost, so the real import stays lazy
    from agentclip.executor.hosts.ssh import LinkChannel, SshHost


class LinkFactory:
    """The local mode's session factory: the shared builder, behind a LocalLink.

    The assembly itself lives in
    :class:`agentclip.engine.link.factory.EngineBuilder` - engine-side, so a
    server process on a target machine can run it without a shell in the process
    (docs/design/remote-executor.md section 2.2). All this adds is the seam the
    Shell drives a session through: what a call returns is a
    :class:`~agentclip.shell.app.link.Link`, never the engine itself, and the
    remote mode this prepares for hands back a ``RemoteLink`` over the same
    interface without the Shell noticing which side of the wire its session is
    on. Wrapping happens HERE rather than in the builder because `cli` is the one
    module allowed to know both halves.

    It is also the MCP status source both shells are handed. The builder owns
    the runtime (section 2.7: servers spawn where the engine runs), and this
    re-states the two calls a status pane makes under the names the shells
    already consume - ``statuses()`` and ``set_status_hook()`` - so neither
    shell has to grow an ``executor.mcp`` import to paint a sidebar block.
    """

    __slots__ = ("_builder",)

    def __init__(self, builder: EngineBuilder) -> None:
        self._builder = builder

    def __call__(self, request: EngineRequest | str) -> Link:
        # The link gets the same status source the shells do, so
        # ``await link.mcp_statuses()`` answers in local mode exactly as it does
        # over the wire - one seam, two modes, no branch in the caller.
        return LocalLink(self._builder(request), mcp_statuses=self.statuses, skills=self.skills)

    def statuses(self) -> tuple[McpServerStatus, ...]:
        return self._builder.mcp_statuses()

    def skills(self) -> SkillReport:
        """What the builder discovered beside the project, for `/skills`.

        A read of memory, not of disk: discovery ran once when the builder was
        made. Handed to the link as well as to the shells so the seam answers
        ``await link.skills()`` in local mode exactly as it does over the wire.
        """
        return self._builder.skills()

    def set_status_hook(self, cb: Callable[[McpServerStatus], None] | None) -> None:
        self._builder.set_mcp_status_hook(cb)

    def close(self) -> None:
        """Hand back what the builder owns - today, the MCP loop thread."""
        self._builder.close()


def make_engine_factory(
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
) -> LinkFactory:
    """One :class:`LinkFactory` over one engine-side builder.

    Every argument is the builder's - see its docstring for what each one means
    and for the per-session rules (fresh config read, fresh chat name, one host
    per session, MCP construction and catalog sizing). The name stays
    ``make_engine_factory`` because an engine is still exactly what it builds -
    the link is how the caller reaches it.
    """
    return LinkFactory(
        make_engine_builder(
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
    )


def _mcp_source(factory: LinkFactory) -> LinkFactory | None:
    """The status source a shell is handed - or None when there is nothing to show.

    Both shells treat "no manager" as "no MCP block", and unconfigured MCP is
    the everyday case, so the absence is passed on rather than a source that
    would answer every paint with an empty tuple. Asking is also what BUILDS the
    runtime (the builder makes it on first ask), which puts the connects exactly
    where ``main()`` used to kick them off: at launch, overlapping the terminal
    probe, the first paint and the user typing their task, so the first
    bootstrap usually lists real tools rather than an empty listing.
    """
    return factory if factory.statuses() else None


# How long a failed launch is given to report an exit status before the failure
# is described. The engine died, so the channel's EOF and its exit status arrive
# from two different places on the transport and not in a fixed order - and
# "exit 127" is the difference between naming the fault and quoting stderr at
# somebody. A second, spent only on a launch that already failed.
_LAUNCH_VERDICT_S = 1.0
_LAUNCH_POLL_S = 0.02


@dataclass(frozen=True, slots=True)
class RemoteEngine:
    """A live link to an engine on a target: the client, and what it rides on.

    Handed back beside the factory because the two have different lifetimes to
    the sessions built from them. Every session of one remote run is hosted by
    ONE server process on ONE channel (docs/design/remote-executor.md §2.10), so
    the thing to close at the end is this, not any individual link - and closing
    the channel is precisely how the remote engine is stopped, since it dies with
    the channel by design (§2.3).

    It is also the remote mode's **MCP status source**, which is why it grew two
    methods that have nothing to do with channels: the shells are handed one
    object that answers ``statuses()`` / ``set_status_hook()`` / ``close()``
    (the ``McpStatusSource`` shape both sidebars consume), and in local mode
    that object is :class:`LinkFactory`. Same three names, same duck type, no
    branch in either shell - and no ``executor.mcp`` import in one, because the
    rows travel as values (§2.7).
    """

    client: RemoteLinkClient
    channel: LinkChannel
    target: str

    def statuses(self) -> tuple[McpStatusLine, ...]:
        """The target's MCP rows as of the last session build. **No wire call.**

        This is called on the UI thread, on every status paint, by both shells -
        so it must never block on the network. A pane that waited on an SSH
        round trip would freeze the window for as long as the target took to
        answer, and it would do it while painting a number. So it hands back the
        settle the client cached from the most recent ``build_session``
        (``build_mcp_statuses``, §2.11) - refreshed on every build, and ``()``
        until the first session exists, which is the honest answer for a
        connection that has never asked the far side anything about MCP.

        A Shell that wants a FRESH reading takes one through the link
        (``await link.mcp_statuses()``, which is what ``/mcp`` does): there the
        round trip is the caller's to spend, and it is spent off the event loop.
        """
        return self.client.build_mcp_statuses

    def skills(self) -> SkillReport:
        """The target's skills, over the wire. **One round trip, on purpose.**

        The opposite call to :meth:`statuses`, and the difference is who asks.
        That one is called on every status paint, so it must never wait on the
        network; this one is called when a human types `/skills` before a session
        exists - once, on an idle app, with no turn to hold up. A round trip is
        the caller's to spend there, and spending it is what makes the answer the
        TARGET's rather than this PC's.
        """
        return self.client.skills()

    def set_status_hook(self, cb: Callable[[McpStatusLine], None] | None) -> None:
        """Accepted and dropped: v1 has **no MCP push over the wire** (§2.9).

        The local builder's hook fires from the MCP manager's loop thread the
        instant a server settles, and both shells register one at mount. There
        is no frame for that to ride on remotely, and inventing one needs
        somewhere for an unsolicited frame to land while no call is outstanding
        - a background reader thread or a poll timer, which §5 keeps open on
        purpose. Refusing the call instead of ignoring it would buy nothing and
        cost the shells a branch; what the remote cadence IS, in the meantime,
        is the ``build_session`` settle plus whatever a Shell pulls, so a server
        that comes up late shows up at the next session build.
        """

    def close(self) -> None:
        self.channel.close()


def make_remote_link_factory(
    connected: ConnectedRemote, *, service: str | None = None
) -> tuple[Callable[[EngineRequest | str], Link], RemoteEngine]:
    """The remote mode's session factory: an engine on the target, behind a RemoteLink.

    The twin of :func:`make_engine_factory`, and the point where the two halves
    of increment 3 meet: a dialled target (``executor.hosts.connect``) on one
    side, the wire client (``shell.app.remote_link``) on the other, and between
    them one ``agentclip-engine`` launched by name over an exec channel
    (docs/design/remote-executor.md §2.6, §2.12). It lives in ``cli`` for the
    same reason the ``LocalLink`` wrap does - this is the one module allowed to
    know both halves; ``shell.app`` may not import the host seam, and the host
    seam may not import a protocol.

    No ``--service`` unless the caller asks for one: the engine reads the
    TARGET's config over there, and a service key is the one part of it a local
    flag legitimately overrides (§2.5).

    **This is what ``--ssh`` and the GUI's connect dialog do now.** Increment 3
    shipped it additive and increment 4's parity pass flipped the default: a
    remote session runs its engine, its stores, its policy, its skills and its
    MCP servers on the target, and this is the one place that connection is
    made. The legacy per-call ``SshHost`` assembly
    (:func:`make_engine_factory` with a ``host=``) still works when called
    directly and is still tested, but nothing in either shell reaches it any
    more; §2.8's deletion of it is increment 5.

    Failure has one shape and it is an :class:`EngineLinkError`: a target with
    no engine on it, a wire-version mismatch, or a launch that died some other
    way. Every caller turns ``exc.detail`` into what the user reads - a line on
    stderr and exit 2 for the terminal launch, the checklist's ``engine`` row
    for the dialog - because the sentence is already the classified one (§2.12).

    **Two spellings, one launch.** The plain name is tried first, and a "no such
    command" verdict is retried once at ``<remote home>/.local/bin/`` - which is
    exactly where ``uv tool install agentclip``, the method we document, puts the
    console script, and exactly what sshd's non-interactive PATH leaves out
    (``engine_launch.USER_BIN_DIR``). Without the retry our own install
    instructions produce a target this function calls uninstalled.
    """
    host = connected.host
    root = connected.project_root.as_posix()
    channel, client = _launch_engine(
        host,
        (
            engine_command(root, service),
            fallback_engine_command(connected.home.as_posix(), root, service),
        ),
    )

    def build(request: EngineRequest | str) -> Link:
        # A bare service key is the same shorthand the local builder accepts;
        # the wire only carries the request object.
        req = EngineRequest(service=request) if isinstance(request, str) else request
        return client.build_session(req)

    return build, RemoteEngine(client=client, channel=channel, target=host.target)


def _launch_engine(
    host: SshHost, commands: tuple[str, ...]
) -> tuple[LinkChannel, RemoteLinkClient]:
    """Start the engine on the target: each spelling in turn, until one answers.

    One channel per attempt - a channel whose command died is finished, and the
    engine dies with its channel (§2.3), so there is nothing to reuse. Only a
    "no such command" verdict moves on to the next spelling; every other failure
    is the answer, and trying a second path after a version mismatch or a
    traceback would replace a real diagnosis with a worse one.

    Whatever the last attempt failed with is what the caller raises, so a target
    with no engine anywhere reports the classified sentence naming BOTH
    spellings (``classify_launch_failure``), not the first attempt's alone.
    """
    for attempt, command in enumerate(commands):
        channel = host.open_link_channel(command)
        client = RemoteLinkClient(channel.reader, channel.writer)
        try:
            client.hello()
        except EngineLinkError as exc:
            if exc.kind == LINK_VERSION:
                # The far side ANSWERED: ``hello`` already built the sentence
                # naming both installs (§2.9), and no channel fact can improve
                # on it - nor is another path worth trying, since the install
                # this one found is the one to update.
                channel.close()
                raise
            status, tail = _launch_verdict(channel)
            channel.close()
            if is_missing_engine(status, tail) and attempt + 1 < len(commands):
                continue
            message = classify_launch_failure(status, tail, host.target)
            raise EngineLinkError(exc.kind, message) from exc
        return channel, client
    raise ValueError("no engine command to try")  # pragma: no cover - callers pass two


def _launch_verdict(channel: LinkChannel) -> tuple[int | None, str]:
    """Turn "the link closed" into what actually happened on the target.

    A handshake that never arrived is the one failure the protocol cannot
    explain: from the client's side a missing engine, a broken install and a
    killed process are all EOF. The channel knows better - it has an exit status
    and the target's own stderr - so those two values are what the failure is
    described from (§2.12).

    The exit status is polled for up to a second first, because EOF and the exit
    status arrive from two different places on the transport and "exit 127" is
    the difference between naming the fault and quoting stderr at somebody.
    """
    deadline = time.monotonic() + _LAUNCH_VERDICT_S
    while channel.exit_status() is None and time.monotonic() < deadline:
        time.sleep(_LAUNCH_POLL_S)
    return channel.exit_status(), channel.stderr_tail()


# ``--monitor``'s one refusal, spelled here so the suite pins the sentence
# rather than a substring of it (docs/design/ui-monitor.md §6.5).
MONITOR_BAD_TARGET = "--monitor wants HOST:PORT with a numeric port, not {given!r}"

# ``--tui``'s epitaph. The flag survives one release as a stub that says what
# happened rather than an argparse "unrecognized arguments" a script would have
# to guess at (docs/design/ui-monitor.md §6.6, §8 "Textual removal timing").
TUI_REMOVED = "the Textual TUI was removed in this release; plain agentclip opens the GUI"


def parse_monitor_target(text: str) -> tuple[str, int] | None:
    """``host:port`` → ``(host, port)``, or None when it is not one.

    Split from ``main`` so the refusal has a test that needs no launch. The
    port is split off the RIGHT, so an IPv6 literal written the only way a
    ``host:port`` string can carry one (``[::1]:7777``) survives; anything whose
    tail is not a port number in range is refused rather than guessed at,
    because the alternative is dialling a port nobody asked for.
    """
    host, sep, port = text.rpartition(":")
    if not sep or not host:
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        return None
    try:
        number = int(port)
    except ValueError:
        return None
    if not 1 <= number <= 65535:
        return None
    return host, number


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
    # Hidden: the app re-invokes itself with this flag to run the draw-a-box
    # screen overlay in a child process (tkinter can't share either shell's
    # process).
    parser.add_argument("--pick-region", action="store_true", help=argparse.SUPPRESS)
    # Hidden: the instruction that overlay shows; only meaningful with --pick-region.
    parser.add_argument("--pick-prompt", default=None, help=argparse.SUPPRESS)
    # Hidden: /identify's read-only twin of --pick-region - the element list
    # arrives as JSON on stdin and is drawn where each element sits on screen.
    parser.add_argument("--show-identify", action="store_true", help=argparse.SUPPRESS)
    # Kept for one release as a STUB, not as a shell: the Textual TUI is gone
    # (docs/design/ui-monitor.md §6.6), and ``main`` refuses this flag with one
    # line saying so. Argparse would otherwise answer "unrecognized arguments",
    # which tells a script that carried the flag nothing about what happened.
    parser.add_argument(
        "--tui",
        action="store_true",
        help="removed: the Textual TUI is gone; plain agentclip opens the GUI",
    )
    # Accepted for one release so a muscle-memory `agentclip --gui` (and every
    # shortcut, script and doc that still carries it) keeps working: the GUI is
    # what a bare `agentclip` opens, so the flag asks for what it would get
    # anyway. Nothing reads it - there is only one shell.
    parser.add_argument(
        "--gui",
        action="store_true",
        help="deprecated no-op: the GUI shell is the only shell",
    )
    # The calibration window on its own (docs/design/ui-monitor.md 6.4): the
    # service editor, the ELEMENTS column, the chat-region picker and
    # /identify, over this machine's own screen. No engine, no session, no
    # transcript - which is the whole point, because in split mode this is what
    # runs on the VM the browser is on while the chat GUI stays on the
    # operator's desk.
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="open the calibration window alone (no session, no engine) and exit when it closes",
    )
    # Split mode (docs/design/ui-monitor.md §6.5): the screen this window drives
    # is on ANOTHER machine, reached over the monitor wire. There is
    # deliberately no in-app connect field for this in phase 5: the flag is the
    # whole entry.
    parser.add_argument(
        "--monitor",
        default=None,
        metavar="HOST:PORT",
        help="drive the screen of a machine running agentclip-monitor (GUI only)",
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
    then it is a complaint rather than a check, and it needs a window open to
    ask at all - where a *build* has to be able to answer it from a terminal.
    Bundling OpenCV into the frozen exe (architecture.md 6) is only correct
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
    runtime. A build that lost pywebview prints the "install the
    gui extra" line a frozen user can do nothing about, and a build that kept
    pywebview but lost the packaged page opens a window on nothing - neither
    fails until somebody launches the GUI.

    So this does the three things ``run_gui`` does before it has a window, in
    the same order, and then stops: import ``webview`` (which drags in the
    winforms backend, clr and the .NET runtime - the half PyInstaller can only
    see because the spec names it), resolve the assets through
    ``importlib.resources`` and read every one of them (the classic frozen-app
    failure: the files are IN the archive but the resource reader cannot find
    them under ``_MEIPASS``), and ask ``webview2_missing()``. The user guide the
    window's "docs" button opens is read the same way and for the same reason -
    it is collected by the spec at a package-relative path too, and a build that
    lost it fails nothing until somebody presses the button.

    A machine without the WebView2 runtime still exits 0 and says so. What is
    under test is the FREEZE, not the box the build ran on - a build server
    with no Evergreen runtime must not be able to fail a packaging check, and
    the renderer word is printed so the state is never merely assumed.
    """
    from agentclip.shell.gui.docs import load_doc_pages
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
    # ...and the user guide the window's "docs" button opens, for exactly the
    # assets' reason: the files are collected by the spec at a package-relative
    # path and found through the same resource machinery, so a build that lost
    # them opens a help button on a "this build does not carry it" note. It
    # fails nothing at run time, which is what makes it a packaging check.
    missing = [page.name for page in load_doc_pages() if not page.found]
    if missing:
        print(f"gui-smoke: the user guide is not in this build ({', '.join(missing)})",
              file=sys.stderr)
        return 2
    if platform.system() != "Windows":
        renderer = "n/a"
    else:
        renderer = "missing" if webview2_missing() else "edgechromium"
    print(f"gui-smoke: ok renderer={renderer}")
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    """``--calibrate``: the calibration window, alone (ui-monitor.md §6.4).

    Deliberately NOT a ``Launch``. A launch is the answer to "where does the
    session run", and this window runs no session: what it needs is a ``Config``
    (which services exist and how each is recognised) and a clipboard backend
    for the monitor it opens - and even that is only because ``LocalUIMonitor``
    takes one, since nothing here ever starts the watcher. Everything else
    ``main`` builds below this point - the engine factory, the session tree and
    its pruning, the MCP runtime, an SSH dial - is about running a task, and
    building any of it here would be paying for a machine this window never
    touches.

    The project root is still resolved and still used, for one reason: config is
    layered per project (``load_config``), so which project you are in decides
    which service presets you are editing.

    The import is inside the function for ``run_gui``'s reason: pywebview is the
    optional ``gui`` extra and importing ``cli`` must not pay for it.
    """
    from agentclip.shell.gui.calibration.window import run_calibration

    try:
        project_root = Path(args.project).resolve(strict=True)
    except OSError as exc:
        print(f"agentclip: cannot resolve --project {args.project!r}: {exc}", file=sys.stderr)
        return 2
    config = load_config(project_root, service_override=args.service)
    for warning in config.warnings:
        print(f"agentclip: {warning}", file=sys.stderr)
    return run_calibration(config, provider=select_provider(config.clipboard.provider))


@dataclass(frozen=True, slots=True)
class Launch:
    """Everything a session needs to know about WHERE it runs.

    Assembled before the window opens, by :func:`local_launch` or
    :func:`remote_launch`, and never mixed: one session is one machine (design
    decision 4). ``data_root`` is where the ``.agentclip`` tree goes - beside
    the project locally, on this PC for a remote one - and ``home`` is whose
    home directory holds the global skill folders.

    ``remote`` is the dialled machine itself, and it is what tells ``main``
    which kind of session this is. A remote launch now runs the ENGINE on the
    target (docs/design/remote-executor.md §2.12, increment 4's flip), so the
    session factory is built from this rather than from the four
    where-does-it-run fields: those describe a session assembled HERE over an
    ``SshHost``, which is the legacy per-call path, still constructable and no
    longer the default. ``None`` for every local launch.
    """

    project_root: Path
    config: Config
    host: Host
    os_name: str
    data_root: Path
    home: Path
    remote: ConnectedRemote | None = None


@dataclass(frozen=True, slots=True)
class GuiRuntime:
    """A :class:`Launch` after everything derived from it has been built.

    What the GUI's connect dialog gets back when it goes remote mid-window
    (``shell/gui/remote.py:RemoteRuntime``, structurally): the config read off the
    target, the session factory that reaches its engine, and the MCP runtime of
    ITS servers - none of which a launch-time dial can produce, because the
    machine is chosen after the window is already up.

    Since the flip (§2.12) the factory is a ``RemoteLink`` factory over an
    engine running on the target and ``mcp_manager`` is the
    :class:`RemoteEngine` that owns the channel it speaks on - which is also the
    status source, because MCP servers spawn where the engine runs (§2.7). Both
    fields are typed as what a Shell needs rather than as the concrete class, so
    the local shapes (a :class:`LinkFactory` in both slots) still satisfy them.
    """

    project_root: Path
    config: Config
    engine_factory: Callable[[EngineRequest | str], Link]
    mcp_manager: RemoteEngine | LinkFactory | None
    # Where a session-less `/skills` reads after this connect. Never None, unlike
    # the MCP slot: every machine has skill folders worth naming, even when it
    # has nothing in them.
    skills: Callable[[], SkillReport]
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
    """Read a secret from the terminal. Only ever called before a window opens."""
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
    """Connect, authenticate and probe BEFORE a session exists (design 7).

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
        # What ``main`` launches the engine over. Carried whole rather than
        # flattened, because ``make_remote_link_factory`` takes the dialled
        # machine (host + project root), not a launch's summary of it.
        remote=remote,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # First, and above every other branch: a launch that asked for the deleted
    # shell must not half-run as the GUI instead (docs/design/ui-monitor.md
    # §6.6). One line, exit 2, nothing started.
    if args.tui:
        print(f"agentclip: {TUI_REMOVED}", file=sys.stderr)
        return 2
    if args.pick_region:
        return _pick_region_child(args.pick_prompt)
    if args.show_identify:
        return _show_identify_child(sys.stdin.read())
    if args.list_matchers:
        return _list_matchers()
    if args.gui_smoke:
        return _gui_smoke()
    # ABOVE the launch, deliberately, and not below the shell fork: the
    # calibration window needs a Config and a clipboard backend and nothing
    # else. No engine factory is built, no session tree is created or pruned,
    # no MCP runtime is spawned and no connection is dialled - every one of
    # those is about running a TASK, and this window runs none
    # (docs/design/ui-monitor.md 6.4). ``--ssh`` with it is meaningless for the
    # same reason: calibration is about THIS machine's screen.
    if args.calibrate:
        return _calibrate(args)

    # WHICH MACHINE'S SCREEN, and it is a separate question from where the
    # session runs (``--ssh``, below): the monitor link moves the pixels, the
    # mouse and the clipboard onto another box, while the engine and the
    # project stay wherever the launch put them (docs/design/ui-monitor.md §1's
    # table - three halves, three flags).
    monitor_target: tuple[str, int] | None = None
    if args.monitor is not None:
        monitor_target = parse_monitor_target(args.monitor)
        if monitor_target is None:
            print(
                f"agentclip: {MONITOR_BAD_TARGET.format(given=args.monitor)}",
                file=sys.stderr,
            )
            return 2

    # WHERE the session runs, and it is settled AFTER the window opens rather
    # than before it: an ``--ssh`` launch does not block on a terminal dial. The
    # window opens on this PC and the connect dialog runs the identical sequence
    # in-app, with a checklist and a retry (docs/design/ui-briefs/ssh-connect.md;
    # gui.md §2's "everything slow happens after first paint"). The launch-time
    # dial went with the TUI, which was the shell that could not prompt once its
    # toolkit owned the terminal; :func:`remote_launch` is still the single
    # construction point that flow is assembled from.
    pending_connect = (args.ssh, args.remote_root) if args.ssh else None
    launch = local_launch(args)
    if isinstance(launch, int):
        return launch
    config = launch.config

    if args.list_services:
        for key in sorted(config.services):
            preset = config.services[key]
            marker = "*" if key == config.general.service else " "
            # Characters first (that is the unit the preset is CONFIGURED in, and
            # the number somebody comparing presets would edit), the token
            # estimate second, because that is the one that answers "will my
            # task fit" - the same pairing the sidebar's caption uses.
            #
            # ASCII only, and deliberately: the sidebar spells this pairing with
            # an "≈", but this line goes through a bare ``print`` to a stream
            # that is NOT always a UTF-8 console. Redirect it (``--list-services
            # > presets.txt``, or a pipe into ``more``) on a cp1252 Windows box
            # and Python encodes with the locale codec, where "≈" is a hard
            # UnicodeEncodeError - a listing that dies when you try to save it.
            tokens = fmt_tokens(preset.max_paste_chars, config.general.chars_per_token)
            print(
                f"{marker} {key:<16} {preset.max_paste_chars:>9,} chars "
                f"{'(' + tokens + ')':<16} {preset.label}"
            )
        return 0

    # The session tree, for the launches that keep one HERE. A remote launch no
    # longer does: the engine runs on the target, so its ``SessionStore`` and
    # ``BackupStore`` land in ``<project>/.agentclip/`` over there and this PC's
    # ``<user_data_dir>/agentclip/remote/...`` directory would be created empty
    # and pruned forever (docs/design/remote-executor.md §2.4 - "the flip of the
    # default is also the moment a remote session's transcripts stop being
    # local"). ``Launch.data_root`` is still computed because the legacy
    # assembly still takes one; nothing on the default path writes to it.
    if launch.remote is None:
        launch.data_root.mkdir(parents=True, exist_ok=True)
        prune_sessions(launch.data_root, config.backup.keep_sessions)
    # The clipboard backend, built here rather than inside the shell: it is a
    # launch decision (``[clipboard] provider``), and the shell is handed the
    # result, exactly as gui.md section 0 said it would be.
    provider = select_provider(config.clipboard.provider)
    # The MCP runtime is not built here any more: the engine-side builder owns
    # it, because servers must spawn on the machine the engine runs on
    # (docs/design/remote-executor.md section 2.7). What ``main`` still decides
    # is the two facts only a launch knows.
    #
    # ``mcp_remote_target`` is the legacy per-call SshHost path's one oddity:
    # the config came off the target while the process spawning servers is this
    # PC, so stdio entries are refused BY NAME and a dial that fails says who
    # dialled it. The host's name, not a bare flag - a status line that names
    # the box beats one that says "remote". Since the flip it evaluates to ""
    # on every path that still builds a local builder (a remote session's
    # servers are the target's, spawned by the target's own engine), and it goes
    # with §2.8's deletion in increment 5.
    #
    # A launch with a connect PENDING builds no MCP at all: those servers would
    # be this PC's, read from this PC's permissions.json, for a session that is
    # about to belong to another machine - and "the host PC's file is not
    # consulted at all in a remote session" is the rule, not a preference
    # (docs/design/remote-ssh.md, "the target owns its policy"). The runtime the
    # connect builds carries the target's instead.
    mcp_remote_target = launch.host.name if config.remote.is_remote() else ""
    # The shell, and there is only one of them (docs/design/ui-monitor.md §6.6
    # deleted the Textual TUI). Imported inside the function because pywebview
    # is the optional `gui` extra: ``--list-matchers``, ``--pick-region`` and a
    # plain ``import agentclip.cli`` must not pay for it, and an install without
    # the extra must still reach the flags above.
    #
    # The window does not exist yet at this line, so the "read the live Config"
    # closure reads a cell the shell writes back through ``on_config_change`` -
    # which is what lets the next session's Engine be built from whatever the
    # service editor last saved, without touching a session already in flight.
    from agentclip.shell.gui.remote import RemoteConnect
    from agentclip.shell.gui.shell import run_gui

    live_config = [config]
    gui_factory = make_engine_factory(
        lambda: live_config[0],
        launch.project_root,
        host=launch.host,
        os_name=launch.os_name,
        data_root=(launch.data_root if launch.data_root != launch.project_root else None),
        home=launch.home,
        mcp_remote_target=mcp_remote_target,
        mcp_enabled=pending_connect is None,
    )
    # What the process currently OWNS, as opposed to what it was launched
    # with: an in-app connect replaces both, and the teardown below has to
    # close what is live rather than what was true at startup. ``engine`` is
    # whatever holds the live engine - a :class:`LinkFactory` (and with it
    # this PC's MCP loop thread) at launch, a :class:`RemoteEngine` (and with
    # it the SSH exec channel the target's engine dies with) after a connect.
    # Both answer ``close()``, which is all a teardown needs to know.
    owned: dict[str, Any] = {"engine": gui_factory, "host": launch.host}

    def adopt_config(edited: Config) -> None:
        live_config[0] = edited

    def build_runtime(remote: ConnectedRemote) -> GuiRuntime:
        """A successful in-app connect, turned into a session's ingredients.

        Since increment 4's flip this is the launch of an engine ON the
        target: ``agentclip-engine`` over an exec channel, a wire handshake,
        and a factory that mints one ``RemoteLink`` per session
        (docs/design/remote-executor.md §2.12). It lives here rather than in
        the shell for the reason ``run_gui`` is handed its factory at all:
        how a session is BUILT is a launch question, and a second
        construction site is a second thing to drift.

        Nothing local is set up for the box any more - no session tree, no
        pruning, no MCP runtime on this PC. All three belong to the machine
        the engine runs on and are its own doing (§2.4, §2.7).

        The launch comes FIRST, before anything is swapped: a failed one
        raises :class:`EngineLinkError` carrying the classified sentence and
        must leave the window exactly as it was, on the machine it was
        already on. The dialled host is closed on the way out, because the
        failing path is the one that must not leak a connection per retry.
        Only then are the previous engine and host handed back - engine
        before host, so the link channel is closed before the transport
        under it.
        """
        try:
            factory, engine = make_remote_link_factory(remote, service=args.service)
        except EngineLinkError:
            closer = getattr(remote.host, "close", None)
            if closer is not None:
                closer()
            raise
        live_config[0] = remote.config
        previous, owned["engine"] = owned["engine"], engine
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
            engine_factory=factory,
            # The RemoteEngine IS the MCP status source in remote mode, and
            # unconditionally: unlike a local builder it has nothing to
            # report until the first session build carries the target's
            # settle home (§2.7), so gating it on a non-empty reading would
            # drop the source before it could ever answer.
            mcp_manager=engine,
            skills=engine.skills,
            host=remote.host,
            target=remote.host.target,
        )

    try:
        return run_gui(
            launch,
            provider=provider,
            on_config_change=adopt_config,
            engine_factory=gui_factory,
            mcp_manager=_mcp_source(gui_factory),
            # Ungated, where the MCP source is not: a project with no skills
            # still has six folders `/skills` should be able to name.
            skills=gui_factory.skills,
            host=launch.host,
            remote=RemoteConnect(
                local_root=Path(args.project).resolve(),
                build=build_runtime,
                service_override=args.service,
                pending=pending_connect,
            ),
            # Split mode, and deliberately handed over as the parsed pair
            # rather than the raw string: ``main`` is where a bad target is
            # refused, so nothing below this line can be given one.
            monitor_target=monitor_target,
        )
    finally:
        # The hand-back, over what is LIVE rather than what the launch started
        # with: a connect dialled from the dialog has replaced both by now. The
        # order is the flip's: the ENGINE goes first (which, remotely, closes
        # the exec channel its process dies with), then the transport under
        # it - a host closed first would take the channel with it and turn
        # an orderly shutdown into a dropped link.
        if owned["engine"] is not None:
            owned["engine"].close()
        close = getattr(owned["host"], "close", None)
        if close is not None:
            close()


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
