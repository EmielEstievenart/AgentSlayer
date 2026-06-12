"""Command-line entry point: argparse, config, clipboard provider, engine, TUI."""

from __future__ import annotations

import argparse
import platform
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from agentclip import __version__
from agentclip.clip.base import select_provider
from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine
from agentclip.protocol.composer import Composer
from agentclip.store.backups import BackupStore
from agentclip.store.session import SessionStore, prune_sessions
from agentclip.tools.registry import default_registry
from agentclip.tools.sandbox import Workspace
from agentclip.tui.app import AgentClipApp


def make_engine_factory(config: Config, project_root: Path) -> Callable[[str], Engine]:
    """Build one fresh Engine (and session directory) per started session.

    The NewSessionScreen may pick a different service preset than the config
    default, so the factory rebuilds a Config with that service active - the
    engine reads its budget/caps from config.preset().
    """
    registry = default_registry()

    def build(service_key: str) -> Engine:
        cfg = config
        if service_key != cfg.general.service and service_key in cfg.services:
            cfg = replace(cfg, general=replace(cfg.general, service=service_key))
        workspace = Workspace(project_root, cfg.excluded_names())
        session = SessionStore(project_root, service=cfg.general.service)
        backups = BackupStore(session.session_dir)
        composer = Composer(
            cfg.preset(),
            cfg.caps(),
            registry.render_catalog(),
            project_root.name,
            platform.system() or "unknown OS",
        )
        return Engine(cfg, registry, workspace, session, backups, composer)

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
    parser.add_argument("--version", action="version", version=f"agentclip {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        project_root = Path(args.project).resolve(strict=True)
    except OSError as exc:
        print(f"agentclip: cannot resolve --project {args.project!r}: {exc}", file=sys.stderr)
        return 2
    if not project_root.is_dir():
        print(f"agentclip: --project is not a directory: {project_root}", file=sys.stderr)
        return 2

    config = load_config(project_root, service_override=args.service)

    if args.list_services:
        for key in sorted(config.services):
            preset = config.services[key]
            marker = "*" if key == config.general.service else " "
            print(f"{marker} {key:<16} {preset.max_paste_chars:>9,} chars  {preset.label}")
        return 0

    prune_sessions(project_root, config.backup.keep_sessions)
    provider = select_provider(config.clipboard.provider)
    app = AgentClipApp(
        config=config,
        provider=provider,
        engine_factory=make_engine_factory(config, project_root),
        project_root=project_root,
    )
    app.run()
    return 0
