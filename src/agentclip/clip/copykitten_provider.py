"""copykitten-backed clipboard provider (primary backend on all platforms).

Thin wrapper: copykitten is imported lazily at construction time, all its
exceptions are caught, and on Windows transient read/write failures (clipboard
held open by Win+V history, clipboard managers, RDP) are retried with a short
backoff per the clipboard research digest.
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

# Message fragments of copykitten/arboard errors that mean "the backend works,
# the clipboard just holds no text right now" (empty, image, file list). These
# are not transient, so they are never retried. Anything else (occupied /
# access denied / unknown OS error) is treated as transient.
_NON_TEXT_MARKERS = ("empty", "requested format", "contentnotavailable")


def _is_non_text_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _NON_TEXT_MARKERS)


class CopykittenProvider:
    name = "copykitten"

    def __init__(self) -> None:
        import copykitten  # lazy: keep the clip package importable without it

        self._ck: Any = copykitten

    def read_text(self) -> str | None:
        for attempt in range(_RETRIES + 1):
            try:
                text = self._ck.paste()
            except self._ck.CopykittenError as exc:
                if _is_non_text_error(exc):
                    return None
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
                self._ck.copy(text)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF_S)
        raise ClipboardUnavailable(f"copykitten write failed: {last_exc}") from last_exc

    def healthcheck(self) -> bool:
        """Roundtrip-less probe: a paste that succeeds, or fails only because
        the clipboard holds no text, proves the backend can reach the OS.
        Transient failures (clipboard briefly held by another process) get the
        same retry budget as reads so startup selection is not derailed."""
        for attempt in range(_RETRIES + 1):
            try:
                self._ck.paste()
            except self._ck.CopykittenError as exc:
                if _is_non_text_error(exc):
                    return True
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF_S)
                    continue
                return False
            except Exception:
                return False
            return True
        return False

    def sequence_number(self) -> int | None:
        return get_clipboard_sequence_number()
