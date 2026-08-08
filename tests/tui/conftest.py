"""Pilot-suite fixtures.

One job so far, and it is a safety gate rather than a convenience: no test in
this directory may read or write the user's real service profiles. A profile is
a folder of captured PNGs under the app's config home
(``config.default_profile_dir``), and the TUI writes into it the moment a
capture button is pressed - so a Pilot test that built its ``AgentClipApp``
without an explicit ``profile_root`` would calibrate the developer's actual
ChatGPT profile out from under them.

The autouse fixture below redirects that default to ``tmp_path`` for every test
here, patched at BOTH the definition and the ``tui.app`` use site (``app.py``
from-imports the name, so patching only ``agentclip.config`` would miss it) -
the same "patch at the use site" discipline the OS gate in tests/conftest.py
uses. Tests that want to inspect what landed on disk pass ``profile_root``
explicitly and get the same directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    """The tmp directory every app in this suite persists appearances into."""
    return tmp_path / "profiles"


@pytest.fixture(autouse=True)
def _no_real_profiles(monkeypatch: pytest.MonkeyPatch, profile_root: Path) -> None:
    """Point ``default_profile_dir`` at the tmp path, definition and use site."""

    def fake_default_profile_dir() -> Path:
        return profile_root

    monkeypatch.setattr("agentclip.config.default_profile_dir", fake_default_profile_dir)
    monkeypatch.setattr(
        "agentclip.tui.app.default_profile_dir", fake_default_profile_dir, raising=False
    )
