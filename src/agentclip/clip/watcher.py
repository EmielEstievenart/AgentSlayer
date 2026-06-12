"""Clipboard polling loop and self-write suppression.

Thread-agnostic: :func:`watch` is a plain blocking function; the TUI wraps it
in a Textual thread worker and bridges captures back via thread-safe messages.

This module is a leaf - it never imports from ``agentclip.protocol``. The host
passes the protocol pre-filter (``protocol.parser.looks_like_protocol``) as the
``accepts`` predicate instead.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable

from agentclip.clip.base import ClipboardProvider

_log = logging.getLogger(__name__)


def _hash_text(text: str) -> str:
    """blake2b-128 hex over the VERBATIM text (no protocol normalization here:
    leaf layer; the engine owns normalized reply dedup)."""
    data = text.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(data, digest_size=16).hexdigest()


class SelfWriteSet:
    """Thread-safe registry of hashes of texts AgentClip itself wrote.

    The watcher consults it so AgentClip's own outbound payloads (which contain
    protocol markers) are never re-ingested. Bounded LRU: oldest entries are
    evicted beyond ``max_entries``.
    """

    def __init__(self, max_entries: int = 20) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._lock = threading.Lock()
        self._hashes: dict[str, None] = {}  # insertion-ordered, used as an LRU set
        self._max_entries = max_entries

    def note(self, text: str) -> None:
        """Register a payload as self-written (call BEFORE the clipboard write)."""
        digest = _hash_text(text)
        with self._lock:
            self._hashes.pop(digest, None)
            self._hashes[digest] = None
            while len(self._hashes) > self._max_entries:
                self._hashes.pop(next(iter(self._hashes)))

    def contains_text(self, text: str) -> bool:
        return self.contains_hash(_hash_text(text))

    def contains_hash(self, digest: str) -> bool:
        with self._lock:
            return digest in self._hashes


def write_via(provider: ClipboardProvider, self_writes: SelfWriteSet, text: str) -> None:
    """Write ``text`` through ``provider``, registering it as a self-write FIRST.

    Ordering is the point: if the hash were registered after the write, the
    watcher thread could capture our own payload in between. Provider write
    failures (ClipboardUnavailable) propagate to the caller.
    """
    self_writes.note(text)
    provider.write_text(text)


def watch(
    provider: ClipboardProvider,
    interval_ms: int,
    should_stop: Callable[[], bool],
    accepts: Callable[[str], bool],
    on_capture: Callable[[str], None],
    self_writes: SelfWriteSet,
    *,
    max_text_chars: int = 8_000_000,
) -> None:
    """Blocking poll loop; returns when ``should_stop()`` is true.

    Each tick: sleep ``interval_ms``, then

    1. Fast path: if the provider exposes ``sequence_number()`` (the Windows
       clipboard sequence counter; FakeClipboard mirrors it) and the value is
       unchanged since the last successful read, skip the read entirely.
    2. Read. ``None``/empty, or an unchanged ``(len, blake2b)`` pair vs the
       last seen text, means nothing to do.
    3. Skip self-written payloads, texts over ``max_text_chars``, and texts
       ``accepts`` rejects - all remembered, so the same clipboard content is
       never re-examined on later ticks.
    4. Otherwise hand the text to ``on_capture``.

    No exception escapes the loop except via ``should_stop`` itself: provider
    or callback failures skip the tick.
    """
    interval_s = max(interval_ms, 0) / 1000.0
    seq_fn = getattr(provider, "sequence_number", None)
    if not callable(seq_fn):
        seq_fn = None
    last_seq: int | None = None
    last_seen: tuple[int, str] | None = None

    while not should_stop():
        time.sleep(interval_s)
        try:
            seq: int | None = None
            if seq_fn is not None:
                try:
                    seq = seq_fn()
                except Exception:
                    seq = None
                if seq is not None and seq == last_seq:
                    continue

            try:
                text = provider.read_text()
            except Exception:
                continue  # providers promise not to raise; never trust a tick anyway
            if not text:
                # Failed/empty/non-text read: leave last_seq unchanged so the
                # next tick retries (the change is not consumed; nothing lost).
                continue
            if seq is not None:
                last_seq = seq

            seen = (len(text), _hash_text(text))
            if seen == last_seen:
                continue
            last_seen = seen

            if self_writes.contains_hash(seen[1]):
                continue
            if len(text) > max_text_chars:
                continue
            try:
                ok = accepts(text)
            except Exception:
                _log.debug("clipboard watcher: accepts() raised", exc_info=True)
                ok = False
            if not ok:
                continue
            on_capture(text)
        except Exception:
            # The TUI owns shutdown via should_stop; a bad tick never kills the loop.
            _log.debug("clipboard watcher: tick failed", exc_info=True)
            continue
