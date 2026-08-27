"""``agentclip-monitor --headless`` - the standing monitor, with no window.

Two doors, one function. On a machine with no desktop to open a window on this
is ``agentclip-monitor --headless`` (the console script itself is
``shell/monitor_ui/__main__.py`` since ui-monitor.md 9.1, and delegates here
verbatim for that flag); in a checkout it is ``python -m
agentclip.driver.monitor``. The parser names the executable, because ``--help``
on a VM is read by somebody who typed the former - and it is the SAME parser
either way: :func:`build_arg_parser` is imported by the shell entry point, so
the argument grammar has one home and it is the Driver's.

Argument parsing and assembly, and nothing else: the loop is
:func:`agentclip.driver.monitor.server.serve` and the machine itself is a
:class:`~agentclip.driver.monitor.local.LocalUIMonitor`. What this module decides
is only what a launcher can tell it - which project's configuration to read
(services are configured per project, exactly as ``cli.py --calibrate`` resolves
them), which service preset, and where to listen.

**It starts unconfigured, and that is correct.** No ``MonitorSpec`` is built
here: the spec is the brain's payload (§2.10) and arrives over the wire on the
first ``configure``. Until then the monitor is a process with a clipboard
backend, a profile lookup and no region to watch - and it stays running through
every disconnect after that, because a monitor outlives every brain that dials
it (§2.8).

**The bind is §5's, spelled as a flag.** The port is a channel to this machine's
mouse, keyboard and clipboard, so the default is loopback and ``--bind`` is the
explicit opt-in the design asks for: typing it IS the consent, which is why
passing it hands ``allow_remote`` to the server.

**And so is the token.** §5's other half, and the reason that port is no longer
unauthenticated: a secret is loaded (or minted) from this machine's monitor
config directory unless ``--token`` / ``--token-file`` names another, and it is
PRINTED to stderr at startup - once, next to the address - because the person
who has to type it into the brain is looking at this terminal. ``--no-token``
turns the guard off and the server refuses that off loopback: on one machine
anything that can reach ``127.0.0.1`` can already drive the mouse, and on a
network it cannot.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agentclip import __version__
from agentclip.config import Config, default_profile_dir, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.auth import default_monitor_dir, load_or_create_token
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.server import LOOPBACK, BindRefused, serve
from agentclip.driver.screen.matchers import MATCHERS, select_matcher
from agentclip.driver.screen.picker import add_overlay_flags, overlay_child
from agentclip.driver.screen.profile_store import load_profile


def _list_matchers() -> int:
    """Say which candidate-generation backends THIS build can actually run.

    ``agentclip --list-matchers`` asks the same question of the app binary, and
    the wording is deliberately identical so a build script can grep one token
    ("NOT AVAILABLE") against either. It is re-implemented rather than imported
    because ``agentclip.cli`` is off this package's layering allowance
    (tests/test_layering.py) - and the shared part, the part that could drift,
    is not this loop but :data:`MATCHERS` and :func:`select_matcher`, which both
    binaries do share.

    It matters MORE here than it does there. The monitor binary is where every
    template search actually runs (docs/design/ui-monitor.md 2.5), and
    ``cv2`` reaches it through a lazy, try/except-guarded import - so a freeze
    that lost OpenCV, or kept it and cannot load its shared objects out of a
    onefile extraction directory, raises nothing at all: every service
    configured for the exhaustive sweep silently gets the anchor search on the
    one machine that does the matching. This imports each backend for real and
    reports what happened, which is what the build scripts run against the
    binary they just produced.
    """
    frozen = bool(getattr(sys, "frozen", False))
    print(f"agentclip-monitor {__version__} ({'frozen build' if frozen else 'from source'})")
    for name in MATCHERS:
        chosen = select_matcher(name)
        if chosen.name == name:
            print(f"  {name:<8} available")
        else:
            print(f"  {name:<8} NOT AVAILABLE - would fall back to {chosen.name!r}")
    return 0


class _ListMatchersAction(argparse.Action):
    """``--list-matchers`` as an ACTION, for ``--version``'s reason.

    ``--port`` was required when this was written, and argparse enforces
    required arguments only after the whole command line has been consumed - so
    a plain ``store_true`` flag would have answered a build's question with "the
    following arguments are required: --port". The port is optional now (the
    Serve panel asks for it instead), but the action stays: printing and exiting
    from inside parsing is what keeps this smoke test a single-flag invocation
    that never reaches an assembly, a config read or a socket, on either of the
    two doors that share this parser.
    """

    def __init__(self, option_strings: list[str], dest: str, help: str | None = None) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=help)

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[override]
        parser.exit(_list_matchers())


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentclip-monitor",
        description=(
            "Watch this machine's chat window and serve it to a brain over the"
            " JSON-lines monitor wire."
        ),
    )
    # The one invocation that is neither a run nor a usage error, and it has the
    # engine binary's two readers: a human asking "which agentclip is on this
    # VM?" (the halves are separate installs and a version skew is an expected
    # state, which is what the handshake's refusal sends people looking for),
    # and a BUILD asking it of the frozen binary as a smoke test - it walks the
    # whole import tree and exits 0 without opening a socket.
    parser.add_argument("--version", action="version", version=f"agentclip-monitor {__version__}")
    # The region picker and the identify overlay run as a CHILD of this same
    # binary (screen.picker re-invokes ``sys.executable``), so the Monitor UI's
    # Capture button lands here with --pick-region, exactly as the Chat UI's
    # lands in cli.py. Hidden, and answered before any door (``main`` below).
    add_overlay_flags(parser)
    # --version proves the import tree; it says nothing about a backend that is
    # only ever imported inside a function on a poll tick. Its twin, and the
    # other half of what the build scripts ask of the frozen binary.
    parser.add_argument(
        "--list-matchers",
        action=_ListMatchersAction,
        help="print which appearance-matcher backends this build can run, and exit",
    )
    # OPTIONAL since the Monitor UI landed (ui-monitor.md 9.1), and required
    # only by the door that has nowhere else to ask: with a window the port is a
    # field in the Serve panel, and naming it here is the "come up already
    # listening" shortcut rather than the only spelling. ``--headless`` has no
    # panel to fall back on, so :func:`main` demands it there.
    parser.add_argument(
        "--port",
        default=None,
        type=int,
        help=(
            "TCP port to listen on (0 picks a free one and prints it)."
            " Required with --headless; with a window it pre-fills the Serve"
            " panel and starts it"
        ),
    )
    # The one flag this parser owns for a window it may not import. The GRAMMAR
    # is the Driver's - one parser, so ``agentclip-monitor --help`` says the
    # same thing whichever half answers it - while the dispatch on this flag is
    # ``shell/monitor_ui/__main__.py``'s, which is the only module allowed to
    # know what a window is.
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "serve with no Monitor UI at all - the windowless door for a VM"
            " with no desktop. Requires --port"
        ),
    )
    parser.add_argument(
        "--bind",
        default=LOOPBACK[0],
        metavar="ADDRESS",
        help=(
            "address to listen on (default 127.0.0.1). Naming anything else is"
            " the explicit opt-in for a port onto this machine's mouse,"
            " keyboard and clipboard, and requires a token - use a host-only"
            " network or an SSH -L forward."
        ),
    )
    parser.add_argument(
        "--project",
        default=".",
        help="project root whose configuration names the services (default: cwd)",
    )
    parser.add_argument(
        "--service",
        default=None,
        help="service preset key to load config with (default: the config's own)",
    )
    parser.add_argument(
        "--global-config",
        default=None,
        metavar="PATH",
        help="read the global config.toml from here instead of the platform location",
    )
    parser.add_argument(
        "--profile-root",
        default=None,
        metavar="PATH",
        help="where captured appearances live (default: the platform profile dir)",
    )
    # Everything this process persists about THIS machine: the token and the
    # chat regions drawn here. One flag for both, because they are one
    # directory and a deployment that relocates one wants the other with it.
    parser.add_argument(
        "--config-dir",
        default=None,
        metavar="PATH",
        help=(
            "where this monitor keeps its token and its remembered chat regions"
            " (default: the platform config dir's monitor/ folder)"
        ),
    )
    # The three ways to answer "what is the secret", and exactly one may be
    # given: a token that came from two places at once is a token nobody can
    # tell you the value of.
    token = parser.add_mutually_exclusive_group()
    token.add_argument(
        "--token",
        default=None,
        metavar="TEXT",
        help=(
            "the shared secret a brain's hello must carry (default: read or mint one"
            " in the config dir). Beware the shell history - --token-file is the"
            " quieter door."
        ),
    )
    token.add_argument(
        "--token-file",
        default=None,
        metavar="PATH",
        help="read the shared secret from this file instead of the config dir",
    )
    token.add_argument(
        "--no-token",
        action="store_true",
        help=(
            "serve with no authentication at all. Loopback only: the server"
            " refuses a non-loopback bind without a token."
        ),
    )
    return parser


def load_project_config(args: argparse.Namespace) -> Config:
    """Which services exist on this machine, layered for ``--project``.

    Split out of :func:`build_monitor` because the Monitor UI needs the same
    answer for a second reason: the window EDITS this configuration (the service
    editor is a monitor-side surface, ui-monitor.md 9.1), so both doors have to
    resolve it identically - same project root, same override, same global path,
    same warnings on stderr - or a service key would mean one thing to the poller
    and another to the panel above it.
    """
    project_root = Path(args.project).expanduser().resolve()
    config = load_config(
        project_root,
        service_override=args.service,
        global_config_path=global_config_path(args),
    )
    for warning in config.warnings:
        print(f"agentclip-monitor: {warning}", file=sys.stderr)
    return config


def global_config_path(args: argparse.Namespace) -> Path | None:
    """Where ``config.toml`` is read from, or None for the platform location."""
    if args.global_config is None:
        return None
    return Path(args.global_config).expanduser().resolve()


def profile_root(args: argparse.Namespace) -> Path:
    """Where captured appearances live on THIS machine."""
    if args.profile_root is None:
        return default_profile_dir()
    return Path(args.profile_root).expanduser().resolve()


def build_monitor(args: argparse.Namespace, config: Config | None = None) -> LocalUIMonitor:
    """The machine this process serves.

    ``profile_for`` is a plain disk read rather than a cache, for the
    calibration window's reason: the appearances are edited on THIS machine, so
    a cache here would be a way to poll against templates the user just
    replaced.

    ``config`` is accepted so the Monitor UI can build the monitor over the very
    ``Config`` object its editor is about to hand around, rather than re-reading
    the same files and getting two objects that drift the moment one is saved.
    """
    resolved = load_project_config(args) if config is None else config
    root = profile_root(args)
    return LocalUIMonitor(
        profile_for=lambda key: load_profile(root, key),
        clipboard=select_provider(resolved.clipboard.provider),
        clip_poll_interval_ms=resolved.clipboard.poll_interval_ms,
        # The standing monitor is the deployment §8 named: it outlives every
        # brain, so the box an operator drew over here has to survive a reboot
        # rather than being redrawn after each one.
        regions_dir=config_dir(args),
    )


def config_dir(args: argparse.Namespace) -> Path:
    """Where this monitor keeps the token and the remembered regions."""
    if args.config_dir is None:
        return default_monitor_dir()
    return Path(args.config_dir).expanduser().resolve()


def resolve_token(args: argparse.Namespace) -> str | None:
    """The secret this run serves with, or None for ``--no-token``.

    ``--token-file`` is read whole and stripped, so a file written by an echo,
    a password manager or a provisioning script all work; an empty one is a
    usage error rather than a silent ``--no-token``, which is the one way this
    could quietly open the port.
    """
    if args.no_token:
        return None
    if args.token is not None:
        token = args.token.strip()
        if not token:
            raise ValueError("--token is empty")
        return token
    if args.token_file is not None:
        path = Path(args.token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read --token-file {path}: {exc}") from exc
        if not token:
            raise ValueError(f"--token-file {path} is empty")
        return token
    return load_or_create_token(config_dir(args))


async def _run(args: argparse.Namespace, monitor: LocalUIMonitor) -> int:
    try:
        token = resolve_token(args)
    except ValueError as exc:
        print(f"agentclip-monitor: {exc}", file=sys.stderr)
        return 2
    try:
        server = await serve(
            monitor,
            host=args.bind,
            port=args.port,
            # Typing --bind IS the opt-in (§5); the parser's default is loopback,
            # so anything else got here because somebody asked for it out loud.
            allow_remote=True,
            token=token,
        )
    except BindRefused as exc:  # --no-token off loopback is the live one
        print(f"agentclip-monitor: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"agentclip-monitor: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr
        )
        return 2
    # stderr, not stdout: nothing here speaks a protocol on the standard
    # streams, but a launcher that captures one of them should get the port
    # somewhere it can read without racing a log line.
    print(f"agentclip-monitor: listening on {server.address}", file=sys.stderr)
    # The token, in the clear, once. This is a terminal on the machine with the
    # screen: the operator standing at it is the person who has to type the
    # secret into the brain on the other machine, and a secret they cannot read
    # is a monitor nobody can attach to.
    if token is None:
        print(
            "agentclip-monitor: token: none - this port is unauthenticated",
            file=sys.stderr,
        )
    else:
        print(f"agentclip-monitor: token: {token}", file=sys.stderr)
    try:
        # Forever. The process is the standing half (§2.8) - it does not end
        # when a brain detaches, only when the operator ends it.
        await asyncio.Event().wait()
    finally:
        await server.close()
        await monitor.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    child = overlay_child(args)
    if child is not None:
        return child
    if args.port is None:
        # The windowless door has nowhere to ask. ``parser.error`` rather than a
        # bare return, so the refusal carries the usage line the person typing
        # this on a VM console needs to read next.
        parser.error("--port is required without a Monitor UI (see --headless)")
    monitor = build_monitor(args)
    try:
        return asyncio.run(_run(args, monitor))
    except KeyboardInterrupt:
        # Ctrl+C is how this process is meant to end; it is not an error and it
        # does not deserve a traceback across the operator's terminal.
        print("agentclip-monitor: stopped", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
