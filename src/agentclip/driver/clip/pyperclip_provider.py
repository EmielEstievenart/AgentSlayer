"""pyperclip-backed clipboard provider (fallback backend).

Thin wrapper: pyperclip is imported lazily at construction time, all its
exceptions are caught, and on Windows transient read/write failures are
retried with a short backoff. pyperclip returns "" for an empty or non-text
clipboard, which maps to None per the ClipboardProvider contract.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from agentclip.driver.clip.base import ClipboardUnavailable
from agentclip.driver.clip.winseq import get_clipboard_sequence_number

# Reads and writes retry on transient failures, with a per-platform budget.
# Windows gets the biggest one (Win+V history, clipboard managers and RDP all
# hold the clipboard open for a beat). Linux gets a smaller one for a different
# reason: an X11 clipboard manager takes ownership of the selection right after
# a copy, and a read that lands inside that hand-off comes back empty or
# refused. macOS needs none.
if sys.platform == "win32":
    _RETRIES = 4
elif sys.platform.startswith("linux"):
    _RETRIES = 2
else:
    _RETRIES = 0
_BACKOFF_S = 0.075


class PyperclipProvider:
    name = "pyperclip"

    def __init__(self) -> None:
        # lazy: keep the clip package importable without it; pyperclip ships no stubs
        import pyperclip  # type: ignore[import-untyped]

        self._pc: Any = pyperclip

    def read_text(self) -> str | None:
        for attempt in range(_RETRIES + 1):
            try:
                text = self._pc.paste()
            except self._pc.PyperclipException:
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF_S)
                    continue
                return None
            except Exception:
                return None
            return text if text else None
        return None

    def write_text(self, text: str) -> None:
        last_exc: Exception | None = None
        for attempt in range(_RETRIES + 1):
            try:
                self._pc.copy(text)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF_S)
        raise ClipboardUnavailable(f"pyperclip write failed: {last_exc}") from last_exc

    def healthcheck(self) -> bool:
        """Roundtrip-less probe: paste() raises PyperclipException when no
        copy/paste mechanism exists (e.g. Linux without xclip/wl-clipboard)."""
        try:
            self._pc.paste()
        except Exception:
            return False
        return True

    def sequence_number(self) -> int | None:
        return get_clipboard_sequence_number()
