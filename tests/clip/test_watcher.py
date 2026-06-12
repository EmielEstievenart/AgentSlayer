"""Watcher loop tests, driven entirely by in-memory clipboard doubles.

watch() runs in the test thread; a scripted should_stop performs one step per
tick and stops the loop when the script is exhausted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from agentclip.clip.base import ClipboardProvider
from agentclip.clip.fake import FakeClipboard, ScriptedClipboard
from agentclip.clip.watcher import SelfWriteSet, watch, write_via

PROTO = "===CLIP:CALL id=1 tool=read_file===\npath: x\n===CLIP:END===\n===CLIP:EOM calls=1==="


def looks_protocol(text: str) -> bool:
    return "===CLIP:" in text


def drive(
    provider: ClipboardProvider,
    steps: Iterable[Callable[[], None] | None],
    *,
    accepts: Callable[[str], bool] = looks_protocol,
    self_writes: SelfWriteSet | None = None,
    max_text_chars: int = 8_000_000,
) -> list[str]:
    """Run watch() in this thread: one step per tick, stop when steps run out."""
    captured: list[str] = []
    iterator = iter(steps)

    def should_stop() -> bool:
        try:
            step = next(iterator)
        except StopIteration:
            return True
        if step is not None:
            step()
        return False

    watch(
        provider,
        1,
        should_stop,
        accepts,
        captured.append,
        self_writes if self_writes is not None else SelfWriteSet(),
        max_text_chars=max_text_chars,
    )
    return captured


# ── change detection ─────────────────────────────────────────────────────


def test_detects_new_protocol_text() -> None:
    fake = FakeClipboard()
    captured = drive(fake, [lambda: fake.set_text(PROTO), None, None])
    assert captured == [PROTO]


def test_same_text_captured_once_even_when_reread_every_tick() -> None:
    scripted = ScriptedClipboard([PROTO] * 5)
    captured = drive(scripted, [None] * 5)
    assert captured == [PROTO]
    assert scripted.reads == 5  # no fast path: read every tick, captured once


def test_successive_distinct_texts_all_captured() -> None:
    fake = FakeClipboard()
    second = PROTO + "\nsecond"
    captured = drive(
        fake,
        [lambda: fake.set_text(PROTO), None, lambda: fake.set_text(second), None],
    )
    assert captured == [PROTO, second]


def test_preexisting_clipboard_content_captured_on_first_tick() -> None:
    fake = FakeClipboard(initial=PROTO)
    captured = drive(fake, [None, None])
    assert captured == [PROTO]


def test_empty_text_ignored() -> None:
    fake = FakeClipboard()
    captured = drive(fake, [lambda: fake.set_text(""), None, None])
    assert captured == []


def test_non_text_clipboard_ignored() -> None:
    fake = FakeClipboard(initial=PROTO)
    captured = drive(fake, [lambda: fake.set_non_text(), None, None])
    assert captured == []


# ── self-write suppression ───────────────────────────────────────────────


def test_ignores_self_written_payload() -> None:
    fake = FakeClipboard()
    self_writes = SelfWriteSet()
    write_via(fake, self_writes, PROTO)
    captured = drive(fake, [None] * 5, self_writes=self_writes)
    assert captured == []
    assert fake.written == [PROTO]


def test_external_copy_after_self_write_is_captured() -> None:
    fake = FakeClipboard()
    self_writes = SelfWriteSet()
    write_via(fake, self_writes, PROTO)
    reply = PROTO + "\nactual LLM reply"
    captured = drive(
        fake,
        [None, lambda: fake.set_text(reply), None],
        self_writes=self_writes,
    )
    assert captured == [reply]


# ── accepts predicate ────────────────────────────────────────────────────


def test_non_accepted_text_hash_not_retested() -> None:
    seen: list[str] = []

    def accepts(text: str) -> bool:
        seen.append(text)
        return False

    scripted = ScriptedClipboard(["just prose, no marker"] * 5)
    captured = drive(scripted, [None] * 5, accepts=accepts)
    assert captured == []
    assert len(seen) == 1  # hash remembered: same text never re-tested
    assert scripted.reads == 5


def test_accepts_exception_treated_as_rejection() -> None:
    def accepts(text: str) -> bool:
        raise ValueError("bad predicate")

    scripted = ScriptedClipboard([PROTO])
    captured = drive(scripted, [None] * 3, accepts=accepts)
    assert captured == []


# ── robustness ───────────────────────────────────────────────────────────


def test_provider_read_exceptions_skip_tick_and_loop_survives() -> None:
    scripted = ScriptedClipboard([RuntimeError("boom"), RuntimeError("boom"), PROTO])
    captured = drive(scripted, [None] * 4)
    assert captured == [PROTO]
    assert scripted.reads == 4


def test_none_reads_tolerated_before_capture() -> None:
    scripted = ScriptedClipboard([None, None, PROTO])
    captured = drive(scripted, [None] * 3)
    assert captured == [PROTO]


def test_on_capture_exception_does_not_kill_loop() -> None:
    fake = FakeClipboard()
    captured: list[str] = []

    def on_capture(text: str) -> None:
        captured.append(text)
        raise RuntimeError("handler failed")

    second = PROTO + "\nsecond"
    steps = iter([lambda: fake.set_text(PROTO), None, lambda: fake.set_text(second), None])

    def should_stop() -> bool:
        try:
            step = next(steps)
        except StopIteration:
            return True
        if step is not None:
            step()
        return False

    watch(fake, 1, should_stop, looks_protocol, on_capture, SelfWriteSet())
    assert captured == [PROTO, second]


# ── size cap ─────────────────────────────────────────────────────────────


def test_oversize_text_skipped_smaller_followup_captured() -> None:
    big = "===CLIP:" + "x" * 400
    scripted = ScriptedClipboard([big, PROTO])
    captured = drive(scripted, [None] * 3, max_text_chars=200)
    assert captured == [PROTO]


# ── sequence-number fast path ────────────────────────────────────────────


class CountingFake(FakeClipboard):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read_text(self) -> str | None:
        self.reads += 1
        return super().read_text()


def test_sequence_number_fast_path_skips_reads() -> None:
    fake = CountingFake()
    fake.set_text(PROTO)
    captured = drive(fake, [None] * 6)
    assert captured == [PROTO]
    assert fake.reads == 1  # all later ticks skipped via the change counter


def test_sequence_number_change_triggers_one_read() -> None:
    fake = CountingFake()
    fake.set_text(PROTO)
    second = PROTO + "\nsecond"
    captured = drive(fake, [None, None, lambda: fake.set_text(second), None, None])
    assert captured == [PROTO, second]
    assert fake.reads == 2


# ── write_via ordering ───────────────────────────────────────────────────


def test_write_via_registers_hash_before_provider_write() -> None:
    self_writes = SelfWriteSet()
    registered_at_write_time: list[bool] = []

    class Probe(FakeClipboard):
        def write_text(self, text: str) -> None:
            registered_at_write_time.append(self_writes.contains_text(text))
            super().write_text(text)

    probe = Probe()
    write_via(probe, self_writes, PROTO)
    assert registered_at_write_time == [True]
    assert probe.written == [PROTO]


# ── SelfWriteSet ─────────────────────────────────────────────────────────


def test_selfwriteset_membership_and_eviction() -> None:
    self_writes = SelfWriteSet(max_entries=2)
    self_writes.note("a")
    self_writes.note("b")
    assert self_writes.contains_text("a")
    assert self_writes.contains_text("b")
    self_writes.note("c")
    assert not self_writes.contains_text("a")  # oldest evicted
    assert self_writes.contains_text("b")
    assert self_writes.contains_text("c")
    assert not self_writes.contains_text("never noted")


def test_selfwriteset_hashes_verbatim_text() -> None:
    self_writes = SelfWriteSet()
    self_writes.note("line\r\n")
    assert self_writes.contains_text("line\r\n")
    assert not self_writes.contains_text("line\n")  # no normalization in the leaf
