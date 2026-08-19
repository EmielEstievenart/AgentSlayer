"""The bridge: one FIFO into the page, one js_api out of it.

What is pinned here is the property the whole GUI rests on - ORDER. Every port
the GUI implements has methods that arrive from a worker thread (the engine's,
the clipboard watcher's, the detector poller's), and phase 0 already paid once
for a channel that did not preserve order across threads (the paint-epoch
filter, docs/design/gui.md section 1). So the concurrency test below uses real
threads and asserts the thing that actually matters: what one thread sent, the
page sees in that order, with nothing lost and nothing duplicated.
"""

from __future__ import annotations

import json
import threading

from agentclip.shell.gui.bridge import Bridge, JsApi, payload_of, render_call


class Sink:
    """A window, as far as the bridge is concerned."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, script: str) -> None:
        with self.lock:
            self.scripts.append(script)

    @property
    def events(self) -> list[dict[str, object]]:
        return [payload_of(script) for script in self.scripts]


# == the wire format ==========================================================


def test_an_event_is_a_json_object_inside_one_guarded_call() -> None:
    script = render_call({"type": "toast", "message": "hi"})
    assert script.startswith("window.agentclip && window.agentclip.receive(")
    assert script.endswith(");")
    assert payload_of(script) == {"type": "toast", "message": "hi"}


def test_the_payload_survives_everything_a_model_can_write() -> None:
    """A transcript carries quotes, backslashes, newlines and emoji verbatim.

    ``evaluate_js`` embeds this string in a script pywebview escapes again, so
    the one thing that must be true here is that the rendered call is ASCII with
    no raw newline in it - anything else is a script that ends early.
    """
    nasty = 'he said "hi"\\ then\nnewline\ttab ✓ ✓ </script> \r\n end'
    script = render_call({"type": "transcript", "kind": "prose", "text": nasty})
    assert "\n" not in script
    assert script.isascii()
    assert payload_of(script)["text"] == nasty


def test_nested_structures_round_trip() -> None:
    rows = [{"call_id": 1, "tool": "run_command", "streams": True, "glyph": "•"}]
    script = render_call({"type": "run", "running": True, "calls": rows})
    assert payload_of(script)["calls"] == json.loads(json.dumps(rows))


# == ordering =================================================================


def test_one_thread_sees_every_event_in_the_order_it_was_sent() -> None:
    sink = Sink()
    bridge = Bridge(sink)
    bridge.start()
    for i in range(200):
        bridge.send("tick", n=i)
    bridge.stop()
    assert [event["n"] for event in sink.events] == list(range(200))


def test_concurrent_emitters_never_interleave_one_thread_with_itself() -> None:
    """The real contract, with real threads.

    The engine's worker, the clipboard watcher and the detector poller all send
    at once in a live session. Nothing promises which THREAD's event lands
    first - that is genuinely undefined - but each thread's own sequence must
    arrive intact and exactly once, which is what one queue and one drainer buy
    and what a per-caller ``evaluate_js`` would not.
    """
    sink = Sink()
    bridge = Bridge(sink)
    bridge.start()
    threads_n, per_thread = 6, 150
    start = threading.Barrier(threads_n)

    def emit(worker: int) -> None:
        start.wait()
        for i in range(per_thread):
            bridge.send("tick", worker=worker, n=i)

    workers = [threading.Thread(target=emit, args=(w,)) for w in range(threads_n)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    bridge.stop()

    events = sink.events
    assert len(events) == threads_n * per_thread
    for worker in range(threads_n):
        seen = [event["n"] for event in events if event["worker"] == worker]
        assert seen == list(range(per_thread)), f"thread {worker} arrived out of order"


def test_stop_flushes_what_is_still_queued() -> None:
    """A closing window still says why it closed: the sentinel goes in BEHIND
    everything already queued."""
    sink = Sink()
    bridge = Bridge(sink)
    for i in range(50):
        bridge.send("tick", n=i)
    bridge.start()
    bridge.stop()
    assert [event["n"] for event in sink.events] == list(range(50))


def test_sends_after_stop_are_dropped_rather_than_queued_forever() -> None:
    """A poller thread that has not noticed the teardown must not pile events
    onto a queue nobody will ever drain."""
    sink = Sink()
    bridge = Bridge(sink)
    bridge.start()
    bridge.stop()
    bridge.send("tick", n=99)
    assert bridge.drain_pending() == 0
    assert sink.scripts == []


def test_a_failing_sink_does_not_end_the_drainer() -> None:
    """``evaluate_js`` raises for a window on its way out, and for a page that
    broke. Neither may take the channel down - the next event is usually the
    one that explains what happened."""
    seen: list[str] = []

    def flaky(script: str) -> None:
        if "boom" in script:
            raise RuntimeError("window is gone")
        seen.append(script)

    bridge = Bridge(flaky)
    bridge.start()
    bridge.send("ok", n=1)
    bridge.send("boom")
    bridge.send("ok", n=2)
    bridge.stop()
    assert [payload_of(script)["n"] for script in seen] == [1, 2]


def test_a_bridge_with_no_sink_yet_still_queues() -> None:
    """``attach`` comes after ``create_window``, so the first events are always
    raised before there is anywhere to put them."""
    bridge = Bridge()
    bridge.send("early", n=1)
    sink = Sink()
    bridge.attach(sink)
    assert bridge.drain_pending() == 1
    assert sink.events[0]["n"] == 1


# == JS -> Python =============================================================


class Calls:
    """A ``JsCalls`` recorder - structurally what the runner is."""

    def __init__(self) -> None:
        self.trace: list[tuple[str, object, ...]] = []  # type: ignore[misc]
        self.explode = False

    def page_ready(self) -> None:
        self._note(("page_ready",))

    def submit_text(self, text: str) -> None:
        self._note(("submit_text", text))

    def submit_decision(self, choice: str, note: str) -> None:
        self._note(("submit_decision", choice, note))

    def cancel_execution(self) -> None:
        self._note(("cancel_execution",))

    def cancel_pending_question(self) -> None:
        self._note(("cancel_pending_question",))

    def answer_prompt(self, prompt_id: str, value: object) -> None:
        self._note(("answer_prompt", prompt_id, value))

    def set_os_armed(self, target: bool | None) -> None:
        self._note(("set_os_armed", target))

    def cycle_permission_mode(self) -> None:
        self._note(("cycle_permission_mode",))

    def toggle_watch(self) -> None:
        self._note(("toggle_watch",))

    def recopy(self) -> None:
        self._note(("recopy",))

    def force_ingest(self) -> None:
        self._note(("force_ingest",))

    def reinstruct(self) -> None:
        self._note(("reinstruct",))

    def retry_insert(self) -> None:
        self._note(("retry_insert",))

    def set_service(self, key: str) -> None:
        self._note(("set_service", key))

    def select_window(self, window: str) -> None:
        self._note(("select_window", window))

    def next_window(self) -> None:
        self._note(("next_window",))

    def end_session(self) -> None:
        self._note(("end_session",))

    def _note(self, entry: tuple[object, ...]) -> None:
        if self.explode:
            raise RuntimeError("controller blew up")
        self.trace.append(entry)  # type: ignore[arg-type]


def test_every_js_api_method_reaches_the_call_the_tui_makes() -> None:
    calls = Calls()
    api = JsApi(calls)
    api.ready()
    api.submit("do the thing")
    api.decide("approve", "")
    api.decide("reject", "not like that")
    api.cancel()
    api.cancel_question()
    api.prompt("p1", True)
    assert calls.trace == [
        ("page_ready",),
        ("submit_text", "do the thing"),
        ("submit_decision", "approve", ""),
        ("submit_decision", "reject", "not like that"),
        ("cancel_execution",),
        # Esc's last stage. A DIFFERENT door from `cancel` (ctrl+x): one stops
        # the tool calls running, the other answers a question "cancelled".
        ("cancel_pending_question",),
        ("answer_prompt", "p1", True),
    ]


def test_every_key_action_reaches_the_binding_the_tui_makes() -> None:
    """Parity increment 2's keys, each one method rather than a ``key(name)``
    door: the marshal stays typed, and a page asking for something this shell
    does not do fails at the bridge instead of inside a controller."""
    calls = Calls()
    api = JsApi(calls)
    api.armed(None)
    api.armed(False)
    api.mode()
    api.watch()
    api.recopy()
    api.ingest()
    api.reinstruct()
    api.retry_insert()
    api.service("chatgpt")
    api.window("m1-s1")
    api.next_window()
    api.end_session()
    assert calls.trace == [
        ("set_os_armed", None),
        ("set_os_armed", False),
        ("cycle_permission_mode",),
        ("toggle_watch",),
        ("recopy",),
        ("force_ingest",),
        ("reinstruct",),
        ("retry_insert",),
        ("set_service", "chatgpt"),
        # Increment 3's: the two tab moves are pure view-side navigation (no
        # controller call is made for either) and `e` is gated on the far side.
        ("select_window", "m1-s1"),
        ("next_window",),
        ("end_session",),
    ]


def test_a_bare_armed_toggles_and_a_bare_service_is_a_no_key() -> None:
    """``armed``'s default is the bare ``/armed`` and F5 - toggle - and every
    other parameter defaults to "the user said nothing"."""
    calls = Calls()
    api = JsApi(calls)
    api.armed()
    api.service()
    assert calls.trace == [("set_os_armed", None), ("set_service", "")]


def test_the_js_api_methods_take_the_page_s_defaults() -> None:
    """pywebview builds each JS function from the Python signature and passes
    only the arguments the page actually gave, so every parameter needs a
    default that means "the user said nothing"."""
    calls = Calls()
    api = JsApi(calls)
    api.submit()
    api.decide()
    api.prompt()
    assert calls.trace == [
        ("submit_text", ""),
        ("submit_decision", "", ""),
        ("answer_prompt", "", None),
    ]


def test_a_raising_call_never_escapes_into_pywebview() -> None:
    """pywebview logs and drops what a js_api method raises, which would leave
    the page holding a promise that never settles - and a button that silently
    did nothing."""
    calls = Calls()
    calls.explode = True
    api = JsApi(calls)
    api.submit("x")
    api.cancel()
    api.decide("approve", "")
    assert calls.trace == []
