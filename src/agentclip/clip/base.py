"""Clipboard provider abstraction and startup selection.

Leaf layer: stdlib-only at import time. The concrete backends (copykitten,
pyperclip) are imported lazily inside their provider modules so this package
imports cleanly on machines where a backend is missing or broken.
"""

from __future__ import annotations

from typing import Protocol


class ClipboardUnavailable(Exception):
    """No working clipboard backend for the attempted operation."""


class ClipboardProvider(Protocol):
    """What the watcher and the TUI need from a clipboard backend.

    Contract:
      - ``read_text`` returns None for a non-text/empty clipboard or a
        transient read failure; it never raises.
      - ``write_text`` raises :class:`ClipboardUnavailable` when the payload
        could not be placed on the clipboard (after the provider's own
        retries).
      - ``healthcheck`` is a cheap, roundtrip-less probe (init + read): True
        means the backend can talk to the OS clipboard at all.
      - Providers MAY additionally expose ``sequence_number() -> int | None``,
        a cheap change counter (the Windows clipboard sequence number); the
        watcher uses it to skip reads entirely when nothing changed.
    """

    name: str

    def read_text(self) -> str | None: ...

    def write_text(self, text: str) -> None: ...

    def healthcheck(self) -> bool: ...


class ManualOnlyProvider:
    """Sentinel used when no backend works: the user copies/pastes by hand."""

    name = "manual"

    def read_text(self) -> str | None:
        return None

    def write_text(self, text: str) -> None:
        raise ClipboardUnavailable(
            "no clipboard backend available - copy the payload from the TUI manually"
        )

    def healthcheck(self) -> bool:
        return False


def _try_copykitten() -> ClipboardProvider | None:
    try:
        from agentclip.clip.copykitten_provider import CopykittenProvider

        return CopykittenProvider()
    except Exception:
        return None


def _try_pyperclip() -> ClipboardProvider | None:
    try:
        from agentclip.clip.pyperclip_provider import PyperclipProvider

        return PyperclipProvider()
    except Exception:
        return None


def _healthy(provider: ClipboardProvider) -> bool:
    try:
        return provider.healthcheck()
    except Exception:
        return False


def select_provider(prefer: str = "auto") -> ClipboardProvider:
    """Pick the clipboard backend at startup.

    prefer:
      - ``"manual"``: :class:`ManualOnlyProvider`, no OS clipboard access.
      - ``"copykitten"`` / ``"pyperclip"``: forced - that backend if it can be
        constructed (import + init), else ManualOnly. No healthcheck gate: the
        user asked for it, and the TUI surfaces health separately.
      - ``"auto"``: copykitten if it constructs and passes healthcheck, else
        pyperclip likewise, else ManualOnly.

    Unknown values behave like ``"auto"`` (config validates upstream).
    """
    if prefer == "manual":
        return ManualOnlyProvider()
    if prefer == "copykitten":
        return _try_copykitten() or ManualOnlyProvider()
    if prefer == "pyperclip":
        return _try_pyperclip() or ManualOnlyProvider()

    for factory in (_try_copykitten, _try_pyperclip):
        provider = factory()
        if provider is not None and _healthy(provider):
            return provider
    return ManualOnlyProvider()
