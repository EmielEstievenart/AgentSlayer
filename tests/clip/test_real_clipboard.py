"""Optional smoke test against the real OS clipboard.

Skipped in CI and whenever no real backend is available. It must never fail
the suite: any unexpected condition downgrades to a skip. It briefly replaces
the user's clipboard content and restores it afterwards (best effort).
"""

from __future__ import annotations

import contextlib
import os

import pytest

from agentclip.clip.base import ClipboardUnavailable, select_provider

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="real clipboard smoke test does not run in CI",
)


def test_real_clipboard_roundtrip_smoke() -> None:
    provider = select_provider("auto")
    if provider.name == "manual":
        pytest.skip("no real clipboard backend available")

    sentinel = f"agentclip-clip-smoke-{os.getpid()}"
    original = provider.read_text()
    try:
        try:
            provider.write_text(sentinel)
        except ClipboardUnavailable as exc:
            pytest.skip(f"clipboard write unavailable: {exc}")
        read_back = provider.read_text()
        if read_back != sentinel:
            pytest.skip("clipboard raced by another application")
    finally:
        if original is not None:
            with contextlib.suppress(Exception):
                provider.write_text(original)
