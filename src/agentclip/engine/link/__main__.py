"""``python -m agentclip.engine.link`` - the remote half, as a process.

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

No MCP in this increment: the builder is called with ``mcp_manager=None``, so a
session hosted here is byte-identical to a pre-MCP one. Increment 4
(docs/design/remote-executor.md section 4) brings the target-side McpManager -
which is the whole point of running the engine over there - and it lands here,
in the assembly, exactly like every other argument below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentclip.config import Config, load_config
from agentclip.engine.link.factory import make_engine_builder
from agentclip.engine.link.server import serve


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentclip.engine.link",
        description="Host AgentClip engines over the JSON-lines link on stdin/stdout.",
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
        # local one - the remote half never SSHes out (design section 2.6).
        host=None,
        data_root=data_root,
        home=home,
        mcp_manager=None,
    )
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    return serve(sys.stdin, sys.stdout, builder)


if __name__ == "__main__":
    sys.exit(main())
