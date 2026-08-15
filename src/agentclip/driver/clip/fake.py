"""In-memory clipboard doubles for tests. No OS access, stdlib only."""

from __future__ import annotations

import threading
from collections.abc import Iterable


class FakeClipboard:
    """In-memory ClipboardProvider.

    ``set_text`` simulates an external application copying to the clipboard.
    Both it and ``write_text`` bump the internal change counter exposed via
    ``sequence_number()``, mirroring the Windows clipboard sequence number the
    watcher uses as its read-skipping fast path. ``written`` records every
    payload AgentClip wrote through this provider.
    """

    name = "fake"

    def __init__(self, initial: str | None = None) -> None:
        self._lock = threading.Lock()
        self._text: str | None = initial
        self._counter = 0
        self.written: list[str] = []

    def set_text(self, text: str) -> None:
        """Simulate an external copy (another app wrote to the clipboard)."""
        with self._lock:
            self._text = text
            self._counter += 1

    def set_non_text(self) -> None:
        """Simulate an external copy of non-text content (image, file list)."""
        with self._lock:
            self._text = None
            self._counter += 1

    def read_text(self) -> str | None:
        with self._lock:
            return self._text if self._text else None

    def write_text(self, text: str) -> None:
        with self._lock:
            self.written.append(text)
            self._text = text
            self._counter += 1

    def healthcheck(self) -> bool:
        return True

    def sequence_number(self) -> int | None:
        with self._lock:
            return self._counter


class ScriptedClipboard:
    """Plays back a fixed sequence of read outcomes, one per ``read_text`` call.

    Entries may be a str (returned), None (no text this tick), or an Exception
    instance (raised - deliberately violating the provider contract so tests
    can prove the watcher survives misbehaving providers). Once the script is
    exhausted the last entry repeats; an empty script always yields None.

    Intentionally has NO ``sequence_number`` method: the watcher exercises its
    read-every-tick path (the non-Windows behavior) against this double.
    """

    name = "scripted"

    def __init__(self, script: Iterable[str | None | Exception]) -> None:
        self._script: list[str | None | Exception] = list(script)
        self._lock = threading.Lock()
        self.reads = 0
        self.written: list[str] = []

    def read_text(self) -> str | None:
        with self._lock:
            if not self._script:
                entry: str | None | Exception = None
            elif self.reads < len(self._script):
                entry = self._script[self.reads]
            else:
                entry = self._script[-1]
            self.reads += 1
        if isinstance(entry, Exception):
            raise entry
        return entry if entry else None

    def write_text(self, text: str) -> None:
        with self._lock:
            self.written.append(text)

    def healthcheck(self) -> bool:
        return True
