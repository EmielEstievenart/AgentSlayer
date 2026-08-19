"""``agentclip-engine`` - the remote half, as a process.

Two ways in, one function. On a target this is the **console script**
``agentclip-engine``, which the user pre-installs there by installing this same
package (``uv tool install agentclip``, ``pipx``, pip) and which the master
launches by name over an SSH exec channel - the deployment model of
docs/design/remote-executor.md section 2.6. In tests and for a checkout without
an install it is ``python -m agentclip.engine.link``, the same ``main`` reached
by a different door. The parser names the executable, not the module file,
because ``--help`` on a target is read by somebody who typed the former.

Argument parsing and assembly, and nothing else: the loop is
:func:`agentclip.engine.link.server.serve` and the wiring of one session's
engine is :func:`agentclip.engine.link.factory.make_engine_builder`. What this
module decides is only what a launcher can tell it - which project, which
service preset, and (for tests and for a sandboxed target) which config and home
directories to read instead of this machine's real ones.

The stream IS the protocol: stdin carries frames in, stdout carries frames out,
and stderr is the log. Both are re-opened as UTF-8 with ``\\n`` line endings
before the first frame, because a Windows text stream would otherwise translate
every terminator to ``\\r\\n`` - tolerable for a JSON reader, but the framing
says one ``"\\n"``-terminated line per frame and a protocol should not depend on
the peer being forgiving.

MCP needs no argument here at all, and that is the point of section 2.7: the
builder constructs its own manager from the config THIS process reads, so the
servers of a hosted session are the target's, spawned on the target, with the
target's environment. ``mcp_remote_target`` stays "" precisely because this
process IS its machine - the stdio refusal it turns on belongs to the legacy
per-call ``SshHost`` path, where the config describes one box and the process is
on another. The builder is closed on the way out, which is what hands its loop
thread back.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentclip import __version__
from agentclip.config import Config, load_config
from agentclip.engine.link.factory import make_engine_builder
from agentclip.engine.link.server import serve


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentclip-engine",
        description="Host AgentClip engines over the JSON-lines link on stdin/stdout.",
    )
    # The one invocation that is neither a session nor a usage error. It exists
    # for two readers. A HUMAN on a target answers "which agentclip is over
    # there?" with it - the two halves are separate installs and a version skew
    # is an expected state (design section 2.6), so the handshake's version
    # refusal sends people looking for exactly this number. And a BUILD asks it
    # of a frozen `agentclip-engine` binary as the smoke test
    # (`scripts/build-exe.sh`): it walks the whole module-level import tree -
    # config, factory, server, the executor's tool registry - and exits 0
    # without needing a project, a link peer, or a single frame on stdout.
    #
    # argparse runs a `version` action the moment it consumes the flag, before
    # the `--project` required-check, so this works with no other argument.
    parser.add_argument(
        "--version",
        action="version",
        version=f"agentclip-engine {__version__}",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="project root the hosted sessions work in (on THIS machine)",
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
        "--home",
        default=None,
        metavar="PATH",
        help="home directory for permissions.json and the global skill folders",
    )
    parser.add_argument(
        "--data-root",
        default=None,
        metavar="PATH",
        help="where the .agentclip session tree goes (default: beside the project)",
    )
    return parser


def _path(value: str | None) -> Path | None:
    return None if value is None else Path(value).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    project_root = Path(args.project).expanduser().resolve()
    global_config_path = _path(args.global_config)
    home = _path(args.home)
    data_root = _path(args.data_root)

    def get_config() -> Config:
        # Re-read per session, exactly like the local factory: a preset edited
        # between two sessions of one process takes effect on the next one.
        return load_config(
            project_root,
            service_override=args.service,
            global_config_path=global_config_path,
            home=home,
        )

    builder = make_engine_builder(
        get_config,
        project_root,
        # This process runs ON the machine the project is on, so its Host is the
        # local one - the remote half never SSHes out (design section 2.6). Its
        # MCP servers are local to it for the same reason, which is why no
        # remote target is named.
        host=None,
        data_root=data_root,
        home=home,
    )
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    try:
        return serve(sys.stdin, sys.stdout, builder)
    finally:
        # The MCP loop thread is a daemon, so this is not what keeps the process
        # from exiting - it is what closes the clients (and stops the spawned
        # servers) in an orderly way when the link goes away.
        builder.close()


if __name__ == "__main__":
    sys.exit(main())
