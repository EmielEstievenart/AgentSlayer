"""Pilot-suite fixtures.

Two jobs. The first is a safety gate rather than a convenience: no test in
this directory may read or write the user's real service profiles. A profile is
a folder of captured PNGs under the app's config home
(``config.default_profile_dir``), and the service editor writes into it the
moment a capture button is pressed - so a Pilot test that built its
``AgentClipApp`` without an explicit ``profile_root`` would calibrate the
developer's actual ChatGPT profile out from under them.

The autouse fixture below redirects that default to ``tmp_path`` for every test
here, patched at BOTH the definition and the ``tui.app`` use site (``app.py``
from-imports the name, so patching only ``agentclip.config`` would miss it) -
the same "patch at the use site" discipline the OS gate in tests/conftest.py
uses. Tests that want to inspect what landed on disk pass ``profile_root``
explicitly and get the same directory.

The second is ``seed_templates``: almost every suite here needs a service that
already knows what its copy button (or chat box, or new-chat control) looks
like, and almost none of them are about *how* it came to know. Driving the
editor's capture flow to arrange that is slow, brittle and re-tests the same
path a dozen times over, so those suites write straight into the profile store
instead - the same files a real capture leaves behind. The capture UI itself is
covered once, in test_profile_capture_ui.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from agentclip.screen.capture import RegionImage
from agentclip.screen.profile import TemplateKind
from agentclip.screen.profile_store import save_template


def template_image(width: int = 64, height: int = 64) -> RegionImage:
    """A capture varied enough for ``Template.build`` to anchor on.

    Anchors are picked from the most *varied* windows of the quantised blue
    plane, so a flat block of one colour cannot be searched for and
    ``ServiceProfile.put`` rejects it - a cycling byte pattern is the cheapest
    thing that is always accepted.
    """
    size = width * height * 4
    return RegionImage(width, height, (bytes(range(256)) * (size // 256 + 1))[:size])


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    """The tmp directory every app in this suite persists appearances into."""
    return tmp_path / "profiles"


@pytest.fixture
def seed_templates(profile_root: Path) -> Callable[..., None]:
    """Give ``key`` the listed appearances, as if they had been captured.

    ``seed(key, TemplateKind.COPY, TemplateKind.NEW_CHAT, size=(24, 24))``. Call
    it before ``app.run_test`` and the app simply loads them; call it during a
    run and clear ``MainScreen._profiles`` (or go through ``update_config``) to
    make the screen re-read them, exactly as an editor visit does.
    """

    def seed(
        key: str, *kinds: TemplateKind, size: Iterable[int] = (64, 64)
    ) -> None:
        width, height = tuple(size)
        for kind in kinds:
            save_template(profile_root, key, kind, template_image(width, height))

    return seed


@pytest.fixture(autouse=True)
def _no_real_profiles(monkeypatch: pytest.MonkeyPatch, profile_root: Path) -> None:
    """Point ``default_profile_dir`` at the tmp path, definition and use site."""

    def fake_default_profile_dir() -> Path:
        return profile_root

    monkeypatch.setattr("agentclip.config.default_profile_dir", fake_default_profile_dir)
    monkeypatch.setattr(
        "agentclip.tui.app.default_profile_dir", fake_default_profile_dir, raising=False
    )
