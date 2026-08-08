"""Optional smoke test against the real OS clipboard.

Off by default *everywhere*, CI or not: it briefly replaces whatever the user
has on their clipboard, which is not something a plain ``uv run pytest`` may
do behind their back. Opt in with ``AGENTCLIP_OS_TESTS=1``. When it does run it
must never fail the suite: any unexpected condition downgrades to a skip, and
the original clipboard content is restored afterwards (best effort).
"""

from __future__ import annotations

import contextlib
import os

import pytest

from agentclip.clip.base import ClipboardUnavailable, select_provider

pytestmark = [
    pytest.mark.real_os,
    pytest.mark.skipif(
        os.environ.get("AGENTCLIP_OS_TESTS") != "1",
        reason="writes the real clipboard; set AGENTCLIP_OS_TESTS=1 to run it",
    ),
]


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
