"""The picker child command line, including the overlay's instruction text."""

from __future__ import annotations

import pytest

from agentclip.screen import picker


def test_command_without_a_prompt_is_unchanged() -> None:
    argv = picker._command(None)
    assert argv[-1] == "--pick-region"
    assert "--pick-prompt" not in argv


def test_command_passes_the_prompt_through() -> None:
    argv = picker._command("Draw around the stop button")
    assert argv[-2:] == ["--pick-prompt", "Draw around the stop button"]
    assert "--pick-region" in argv


def test_frozen_command_invokes_the_exe_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """PyInstaller: sys.executable IS agentclip.exe, so no ``-m agentclip``."""
    monkeypatch.setattr(picker.sys, "frozen", True, raising=False)
    argv = picker._command("hello")
    assert "-m" not in argv
    assert argv[-2:] == ["--pick-prompt", "hello"]
