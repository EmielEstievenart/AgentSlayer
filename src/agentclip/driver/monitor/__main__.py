"""``agentclip-monitor`` - the standing monitor, as a process.

Two doors, one function. On the machine whose screen shows the chat this is the
**console script** ``agentclip-monitor`` (or the frozen binary from
``packaging/agentclip-monitor.spec``); in a checkout it is ``python -m
agentclip.driver.monitor``. The parser names the executable, because ``--help``
on a VM is read by somebody who typed the former.

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

**The bind is §5's, spelled as a flag.** The port is an unauthenticated channel
to this machine's mouse, keyboard and clipboard, so the default is loopback and
``--bind`` is the explicit opt-in the design asks for: typing it IS the consent,
which is why passing it hands ``allow_remote`` to the server.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agentclip import __version__
from agentclip.config import default_profile_dir, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.server import LOOPBACK, BindRefused, serve
from agentclip.driver.screen.profile_store import load_profile


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
    parser.add_argument(
        "--port",
        required=True,
        type=int,
        help="TCP port to listen on (0 picks a free one and prints it)",
    )
    parser.add_argument(
        "--bind",
        default=LOOPBACK[0],
        metavar="ADDRESS",
        help=(
            "address to listen on (default 127.0.0.1). Naming anything else is"
            " the explicit opt-in for an unauthenticated port onto this"
            " machine's mouse, keyboard and clipboard - use a host-only network"
            " or an SSH -L forward."
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
    return parser


def build_monitor(args: argparse.Namespace) -> LocalUIMonitor:
    """The machine this process serves.

    ``profile_for`` is a plain disk read rather than a cache, for the
    calibration window's reason: the appearances are edited on THIS machine, so
    a cache here would be a way to poll against templates the user just
    replaced.
    """
    project_root = Path(args.project).expanduser().resolve()
    global_config_path = (
        None if args.global_config is None else Path(args.global_config).expanduser().resolve()
    )
    config = load_config(
        project_root,
        service_override=args.service,
        global_config_path=global_config_path,
    )
    for warning in config.warnings:
        print(f"agentclip-monitor: {warning}", file=sys.stderr)
    root = (
        default_profile_dir()
        if args.profile_root is None
        else Path(args.profile_root).expanduser().resolve()
    )
    return LocalUIMonitor(
        profile_for=lambda key: load_profile(root, key),
        clipboard=select_provider(config.clipboard.provider),
        clip_poll_interval_ms=config.clipboard.poll_interval_ms,
    )


async def _run(args: argparse.Namespace, monitor: LocalUIMonitor) -> int:
    try:
        server = await serve(
            monitor,
            host=args.bind,
            port=args.port,
            # Typing --bind IS the opt-in (§5); the parser's default is loopback,
            # so anything else got here because somebody asked for it out loud.
            allow_remote=True,
        )
    except BindRefused as exc:  # kept for a future launcher that does not opt in
        print(f"agentclip-monitor: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"agentclip-monitor: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 2
    # stderr, not stdout: nothing here speaks a protocol on the standard
    # streams, but a launcher that captures one of them should get the port
    # somewhere it can read without racing a log line.
    print(f"agentclip-monitor: listening on {server.host}:{server.port}", file=sys.stderr)
    try:
        # Forever. The process is the standing half (§2.8) - it does not end
        # when a brain detaches, only when the operator ends it.
        await asyncio.Event().wait()
    finally:
        await server.close()
        await monitor.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
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
