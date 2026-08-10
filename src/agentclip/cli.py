"""Command-line entry point: argparse, config, clipboard provider, engine, TUI."""

from __future__ import annotations

import argparse
import platform
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from agentclip import __version__
from agentclip.app.types import EngineRequest
from agentclip.clip.base import select_provider
from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine
from agentclip.protocol.composer import Composer
from agentclip.protocol.names import generate_chat_name
from agentclip.screen.matchers import MATCHERS, select_matcher
from agentclip.store.backups import BackupStore
from agentclip.store.session import SessionStore, prune_sessions
from agentclip.tools.registry import default_registry
from agentclip.tools.sandbox import Workspace
from agentclip.tools.skills import discover_skills
from agentclip.tui.app import AgentClipApp
from agentclip.tui.graphics import probe_terminal


def make_engine_factory(
    get_config: Callable[[], Config],
    project_root: Path,
    chat_name: str | None = None,
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
    """
    # Skills are discovered once per process from the same folders Claude Code
    # and OpenCode use. The registry is rebuilt per session so the skill listing
    # is bounded to the chosen preset's budget (the bootstrap has no truncation
    # fallback - a big skills library must not be able to overflow it).
    # The rest of the bootstrap is ~9k (spec text plus the built-in catalog), so
    # the listing gets a sixth of the budget, not a quarter: at the 12k presets a
    # quarter leaves no headroom and a full skills library tips it over.
    skills = discover_skills(project_root)

    def build(request: EngineRequest | str) -> Engine:
        req = EngineRequest(service=request) if isinstance(request, str) else request
        session_chat_name = req.chat_name or chat_name or generate_chat_name()
        cfg = get_config()
        if req.service != cfg.general.service and req.service in cfg.services:
            cfg = replace(cfg, general=replace(cfg.general, service=req.service))
        registry = default_registry(
            skills,
            max_skill_listing_chars=cfg.preset().max_paste_chars // 6,
            role=req.role,
            allow_delegate=req.allow_delegate,
        )
        workspace = Workspace(project_root, cfg.excluded_names())
        session = SessionStore(project_root, service=cfg.general.service)
        # Recorded up front so a sub-agent's session directory can be tied back
        # to the run that spawned it when reading the audit log later.
        session.append_event(
            "session",
            role=req.role,
            chat_name=session_chat_name,
            parent_chat_name=req.parent_chat_name,
            allow_delegate=req.allow_delegate,
        )
        backups = BackupStore(session.session_dir)
        composer = Composer(
            cfg.preset(),
            cfg.caps(),
            registry.render_catalog(),
            project_root.name,
            platform.system() or "unknown OS",
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
        )

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
    # Hidden: /identify's read-only twin of --pick-region - the element list
    # arrives as JSON on stdin and is drawn where each element sits on screen.
    parser.add_argument("--show-identify", action="store_true", help=argparse.SUPPRESS)
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


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.pick_region:
        return _pick_region_child(args.pick_prompt)
    if args.show_identify:
        return _show_identify_child(sys.stdin.read())
    if args.list_matchers:
        return _list_matchers()
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
    # BEFORE app.run(), and this is the only place it may happen: probing asks
    # the terminal questions over stdin/stdout, and once Textual starts its own
    # reader thread the answers go to Textual instead - which is exactly how the
    # ELEMENTS column ends up silently drawing blocks on a terminal that can do
    # sixel (tui.graphics, tui.md 1.7).
    probe_terminal()
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
