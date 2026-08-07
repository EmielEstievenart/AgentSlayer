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
from agentclip.protocol.names import generate_chat_name
from agentclip.store.backups import BackupStore
from agentclip.store.session import SessionStore, prune_sessions
from agentclip.tools.registry import default_registry
from agentclip.tools.sandbox import Workspace
from agentclip.tools.skills import discover_skills
from agentclip.tui.app import AgentClipApp


def make_engine_factory(
    get_config: Callable[[], Config],
    project_root: Path,
    chat_name: str | None = None,
) -> Callable[[str], Engine]:
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
    """
    # Skills are discovered once per process from the same folders Claude Code
    # and OpenCode use. The registry is rebuilt per session so the skill listing
    # is bounded to the chosen preset's budget (the bootstrap has no truncation
    # fallback - a big skills library must not be able to overflow it).
    # The rest of the bootstrap is ~9k (spec text plus the built-in catalog), so
    # the listing gets a sixth of the budget, not a quarter: at the 12k presets a
    # quarter leaves no headroom and a full skills library tips it over.
    skills = discover_skills(project_root)

    def build(service_key: str) -> Engine:
        session_chat_name = chat_name or generate_chat_name()
        cfg = get_config()
        if service_key != cfg.general.service and service_key in cfg.services:
            cfg = replace(cfg, general=replace(cfg.general, service=service_key))
        registry = default_registry(skills, max_skill_listing_chars=cfg.preset().max_paste_chars // 6)
        workspace = Workspace(project_root, cfg.excluded_names())
        session = SessionStore(project_root, service=cfg.general.service)
        backups = BackupStore(session.session_dir)
        composer = Composer(
            cfg.preset(),
            cfg.caps(),
            registry.render_catalog(),
            project_root.name,
            platform.system() or "unknown OS",
            session_chat_name,
        )
        return Engine(cfg, registry, workspace, session, backups, composer, session_chat_name)

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
    # Hidden: the TUI re-invokes itself with this flag to run the draw-a-box
    # screen overlay in a child process (tkinter can't share the TUI's process).
    parser.add_argument("--pick-region", action="store_true", help=argparse.SUPPRESS)
    # Hidden: the instruction that overlay shows; only meaningful with --pick-region.
    parser.add_argument("--pick-prompt", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"agentclip {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.pick_region:
        return _pick_region_child(args.pick_prompt)
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
        # app.app_config is reassigned in place when the service editor saves, so
        # this closure keeps reading whatever config is current.
        engine_factory=make_engine_factory(lambda: app.app_config, project_root),
        project_root=project_root,
    )
    app.run()
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
