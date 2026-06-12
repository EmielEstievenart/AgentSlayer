"""Command-line entry point. Full TUI wiring lands in milestone M2."""

from __future__ import annotations

import argparse

from agentclip import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentclip",
        description="Use any web-chat LLM as a coding agent over the clipboard.",
    )
    parser.add_argument("--version", action="version", version=f"agentclip {__version__}")
    parser.parse_args(argv)
    print("AgentClip TUI is not wired up yet (milestone M2).")
    return 0
