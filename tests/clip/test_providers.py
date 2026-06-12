"""Provider selection and thin-wrapper behavior, with stubbed backend modules."""

from __future__ import annotations

import sys

import pytest

import agentclip.clip.base as base
import agentclip.clip.copykitten_provider as ck_mod
import agentclip.clip.pyperclip_provider as pc_mod
from agentclip.clip.base import ClipboardUnavailable, ManualOnlyProvider, select_provider
from agentclip.clip.fake import FakeClipboard
from agentclip.clip.winseq import get_clipboard_sequence_number

# ── select_provider ──────────────────────────────────────────────────────


def test_manual_prefer_returns_manual_only() -> None:
    provider = select_provider("manual")
    assert isinstance(provider, ManualOnlyProvider)
    assert provider.name == "manual"
    assert provider.read_text() is None
    assert provider.healthcheck() is False
    with pytest.raises(ClipboardUnavailable):
        provider.write_text("payload")


def test_auto_prefers_copykitten(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeClipboard()
    second = FakeClipboard()
    monkeypatch.setattr(base, "_try_copykitten", lambda: first)
    monkeypatch.setattr(base, "_try_pyperclip", lambda: second)
    assert select_provider("auto") is first


def test_auto_falls_back_to_pyperclip(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = FakeClipboard()
    monkeypatch.setattr(base, "_try_copykitten", lambda: None)
    monkeypatch.setattr(base, "_try_pyperclip", lambda: fallback)
    assert select_provider("auto") is fallback


def test_auto_skips_unhealthy_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unhealthy(FakeClipboard):
        def healthcheck(self) -> bool:
            return False

    healthy = FakeClipboard()
    monkeypatch.setattr(base, "_try_copykitten", lambda: Unhealthy())
    monkeypatch.setattr(base, "_try_pyperclip", lambda: healthy)
    assert select_provider("auto") is healthy


def test_auto_manual_when_everything_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_try_copykitten", lambda: None)
    monkeypatch.setattr(base, "_try_pyperclip", lambda: None)
    assert isinstance(select_provider("auto"), ManualOnlyProvider)


def test_forced_backend_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_try_copykitten", lambda: None)
    monkeypatch.setattr(base, "_try_pyperclip", lambda: FakeClipboard())
    assert isinstance(select_provider("copykitten"), ManualOnlyProvider)


def test_forced_backend_ignores_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unhealthy(FakeClipboard):
        def healthcheck(self) -> bool:
            return False

    forced = Unhealthy()
    monkeypatch.setattr(base, "_try_pyperclip", lambda: forced)
    assert select_provider("pyperclip") is forced


def test_unknown_prefer_behaves_like_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    only = FakeClipboard()
    monkeypatch.setattr(base, "_try_copykitten", lambda: only)
    monkeypatch.setattr(base, "_try_pyperclip", lambda: None)
    assert select_provider("definitely-not-a-backend") is only


# ── copykitten wrapper (stubbed module) ──────────────────────────────────


class StubCopykittenError(Exception):
    pass


class StubCopykitten:
    CopykittenError = StubCopykittenError

    def __init__(
        self,
        paste_script: list[str | Exception] | None = None,
        copy_failures: int = 0,
    ) -> None:
        self.paste_script = list(paste_script or [])
        self.paste_calls = 0
        self.copy_calls = 0
        self.copy_failures = copy_failures
        self.copied: list[str] = []

    def paste(self) -> str:
        index = min(self.paste_calls, len(self.paste_script) - 1)
        self.paste_calls += 1
        entry: str | Exception
        if index < 0:
            entry = StubCopykittenError("the clipboard is empty")
        else:
            entry = self.paste_script[index]
        if isinstance(entry, Exception):
            raise entry
        return entry

    def copy(self, text: str) -> None:
        self.copy_calls += 1
        if self.copy_calls <= self.copy_failures:
            raise StubCopykittenError("clipboard occupied")
        self.copied.append(text)


def make_ck(stub: StubCopykitten) -> ck_mod.CopykittenProvider:
    provider = ck_mod.CopykittenProvider.__new__(ck_mod.CopykittenProvider)
    provider._ck = stub
    return provider


NON_TEXT_MSG = (
    "The clipboard contents were not available in the requested format "
    "or the clipboard is empty."
)


def test_copykitten_read_ok() -> None:
    assert make_ck(StubCopykitten(["hello"])).read_text() == "hello"


def test_copykitten_empty_string_maps_to_none() -> None:
    assert make_ck(StubCopykitten([""])).read_text() is None


def test_copykitten_non_text_error_returns_none_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 4)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    stub = StubCopykitten([StubCopykittenError(NON_TEXT_MSG)])
    assert make_ck(stub).read_text() is None
    assert stub.paste_calls == 1  # non-text is definitive: no retries burned


def test_copykitten_transient_read_error_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 4)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    stub = StubCopykitten(
        [StubCopykittenError("Access is denied"), StubCopykittenError("Access is denied"), "hello"]
    )
    assert make_ck(stub).read_text() == "hello"
    assert stub.paste_calls == 3


def test_copykitten_persistent_read_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 2)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    stub = StubCopykitten([StubCopykittenError("Access is denied")])
    assert make_ck(stub).read_text() is None
    assert stub.paste_calls == 3  # 1 attempt + 2 retries


def test_copykitten_write_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 4)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    stub = StubCopykitten(copy_failures=2)
    make_ck(stub).write_text("payload")
    assert stub.copied == ["payload"]
    assert stub.copy_calls == 3


def test_copykitten_write_raises_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 2)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    stub = StubCopykitten(copy_failures=99)
    with pytest.raises(ClipboardUnavailable):
        make_ck(stub).write_text("payload")
    assert stub.copy_calls == 3


def test_copykitten_healthcheck_true_on_empty_clipboard() -> None:
    stub = StubCopykitten([StubCopykittenError(NON_TEXT_MSG)])
    assert make_ck(stub).healthcheck() is True


def test_copykitten_healthcheck_false_on_backend_failure() -> None:
    stub = StubCopykitten(
        [StubCopykittenError("The selected clipboard is not supported by this configuration")]
    )
    assert make_ck(stub).healthcheck() is False


def test_copykitten_healthcheck_retries_transient_occupation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 2)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    occupied = StubCopykittenError("the native clipboard is not accessible, held by another party")
    stub = StubCopykitten([occupied, "text"])
    assert make_ck(stub).healthcheck() is True
    assert stub.paste_calls == 2


def test_copykitten_healthcheck_false_when_occupation_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ck_mod, "_RETRIES", 2)
    monkeypatch.setattr(ck_mod, "_BACKOFF_S", 0.0)
    occupied = StubCopykittenError("the native clipboard is not accessible, held by another party")
    stub = StubCopykitten([occupied])
    assert make_ck(stub).healthcheck() is False
    assert stub.paste_calls == 3


# ── pyperclip wrapper (stubbed module) ───────────────────────────────────


class StubPyperclipError(Exception):
    pass


class StubPyperclip:
    PyperclipException = StubPyperclipError

    def __init__(
        self,
        paste_script: list[str | Exception] | None = None,
        copy_failures: int = 0,
    ) -> None:
        self.paste_script = list(paste_script or [""])
        self.paste_calls = 0
        self.copy_calls = 0
        self.copy_failures = copy_failures
        self.copied: list[str] = []

    def paste(self) -> str:
        index = min(self.paste_calls, len(self.paste_script) - 1)
        self.paste_calls += 1
        entry = self.paste_script[index]
        if isinstance(entry, Exception):
            raise entry
        return entry

    def copy(self, text: str) -> None:
        self.copy_calls += 1
        if self.copy_calls <= self.copy_failures:
            raise StubPyperclipError("could not open clipboard")
        self.copied.append(text)


def make_pc(stub: StubPyperclip) -> pc_mod.PyperclipProvider:
    provider = pc_mod.PyperclipProvider.__new__(pc_mod.PyperclipProvider)
    provider._pc = stub
    return provider


def test_pyperclip_empty_string_maps_to_none() -> None:
    assert make_pc(StubPyperclip([""])).read_text() is None


def test_pyperclip_read_ok() -> None:
    assert make_pc(StubPyperclip(["hello"])).read_text() == "hello"


def test_pyperclip_read_error_retried_then_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_mod, "_RETRIES", 2)
    monkeypatch.setattr(pc_mod, "_BACKOFF_S", 0.0)
    stub = StubPyperclip([StubPyperclipError("could not open clipboard")])
    assert make_pc(stub).read_text() is None
    assert stub.paste_calls == 3  # 1 attempt + 2 retries


def test_pyperclip_write_raises_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_mod, "_RETRIES", 1)
    monkeypatch.setattr(pc_mod, "_BACKOFF_S", 0.0)
    stub = StubPyperclip(copy_failures=99)
    with pytest.raises(ClipboardUnavailable):
        make_pc(stub).write_text("payload")
    assert stub.copy_calls == 2


def test_pyperclip_healthcheck() -> None:
    assert make_pc(StubPyperclip([""])).healthcheck() is True
    assert make_pc(StubPyperclip([StubPyperclipError("no mechanism")])).healthcheck() is False


# ── winseq ───────────────────────────────────────────────────────────────


def test_winseq_matches_platform() -> None:
    seq = get_clipboard_sequence_number()
    if sys.platform == "win32":
        assert isinstance(seq, int)
    else:
        assert seq is None
