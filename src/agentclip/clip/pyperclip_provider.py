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

from agentclip.clip.base import ClipboardUnavailable
from agentclip.clip.winseq import get_clipboard_sequence_number

# On Windows reads/writes retry on transient failures; elsewhere one attempt.
_RETRIES = 4 if sys.platform == "win32" else 0
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
