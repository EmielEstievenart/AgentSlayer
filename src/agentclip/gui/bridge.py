"""The GUI's two-way bridge: one FIFO into the page, one ``js_api`` out of it.

The GUI's answer to the thread contracts both view ports carry
(``ChatView.call_started`` and friends, every method of ``AutomationView``):
those calls arrive from the engine's worker thread, from the clipboard watcher
and from the detector poller, and none of them may block or touch the page
directly. So every one of them ends here, in :meth:`Bridge.send` - a JSON dict
with a ``type`` tag appended to a queue - and exactly one thread ever calls
``evaluate_js``.

**Why a drainer thread rather than calling ``evaluate_js`` where the event was
raised.** pywebview 5's ``Window.evaluate_js`` *is* safe to call from any
thread: the WebView2 backend marshals the script onto the WinForms UI thread
with ``Control.Invoke`` and then blocks the caller on a semaphore until the
script has run (``webview/platforms/edgechromium.py``). Both halves of that
matter. Safe-to-call means the queue is not needed for safety; *blocking* means
it is needed for everything else - a paint raised on the detector poller would
stall that tick behind a UI round trip, and the asyncio loop would stall behind
one mid-turn. And two threads calling it concurrently interleave in whatever
order the OS scheduler picks, which is precisely the ordering hazard phase 0
paid for once already (the paint-epoch filter in ``docs/design/gui.md`` §1,
slice 5b): a paint the outgoing run asked for landing after the rebuild that
replaced it. **One FIFO, one drainer, never interleaved** is the whole design,
and it makes the ordering property structural instead of something the shell
has to re-prove per event family.

The drainer also gives the page's own readiness for free: ``evaluate_js`` waits
on pywebview's ``_pywebviewready`` event, so events queued before the window has
finished loading are simply delivered late, in order, rather than lost.

**The other direction** is pywebview's ``js_api`` object. Its methods are called
by pywebview on a FRESH THREAD per call (``webview/util.py:js_bridge_call``
starts a ``Thread`` for every invocation), so nothing here may touch controller
state directly either: :class:`JsApi` is a marshalling shim onto whatever the
runner hands it, and the runner puts every one of them on the GUI's asyncio
loop - the same loop the session controller lives on.

Nothing in this module imports pywebview, or knows a window exists. The sink is
a plain ``Callable[[str], None]``, which is what lets the whole bridge be
exercised - ordering included - by a test with a list in it.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

# The one function the page exposes to Python. Guarded rather than called bare
# so an event that arrives before app.js has run is dropped instead of raising
# a ReferenceError inside the page's console (app.js installs the receiver at
# parse time and buffers, so this guard is the belt to that braces).
_RECEIVER = "window.agentclip && window.agentclip.receive"

EmitFn = Callable[[str], None]


def render_call(event: Mapping[str, Any]) -> str:
    """One event as the line of JavaScript that delivers it.

    ``json.dumps`` with the default ``ensure_ascii`` is what makes this safe to
    paste into a script: the result is ASCII, contains no raw newline and no
    unescaped quote, and JSON is a subset of JavaScript's object literal syntax,
    so the page receives a real object rather than a string it has to parse.
    """
    return f"{_RECEIVER}({json.dumps(event)});"


def payload_of(script: str) -> dict[str, Any]:
    """The inverse of :func:`render_call` - what the page would have received.

    Exists for the tests: the bridge's sink is a string sink (it is
    ``evaluate_js``), and a test that asserted on JavaScript source rather than
    on the event inside it would be pinning the wrapper instead of the message.
    """
    start = script.index("(") + 1
    end = script.rindex(")")
    parsed = json.loads(script[start:end])
    if not isinstance(parsed, dict):  # pragma: no cover - render_call only makes dicts
        raise ValueError(f"not an event payload: {script!r}")
    return parsed


class Bridge:
    """Python -> JS: a thread-safe queue, drained by one thread, in order.

    Constructible without a window on purpose (``emit`` is injected), which is
    what lets ``tests/gui`` drive the whole event vocabulary against a recorder.
    """

    def __init__(self, emit: EmitFn | None = None) -> None:
        # SimpleQueue rather than Queue: unbounded, lock-free on the put side,
        # and this queue must never make a poller thread wait.
        self._events: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._emit: EmitFn | None = emit
        self._thread: threading.Thread | None = None
        # Set by ``stop`` before the sentinel goes in, so a late ``send`` from a
        # poller thread that has not noticed the teardown yet is dropped rather
        # than queued behind a drainer that is already leaving.
        self._closed = False

    # -- the sink -------------------------------------------------------------

    def attach(self, emit: EmitFn) -> None:
        """Point the bridge at the real ``evaluate_js``.

        Separate from construction because the window does not exist yet when
        the runner builds its object graph - the ``js_api`` the window needs is
        built from the same graph, so one of the two has to come second.
        """
        self._emit = emit

    def start(self, emit: EmitFn | None = None) -> None:
        """Begin draining. Idempotent; a second call is a no-op."""
        if emit is not None:
            self._emit = emit
        if self._thread is not None:
            return
        self._closed = False
        self._thread = threading.Thread(target=self._drain, name="agentclip-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Flush what is queued, end the drainer, and wait for it (bounded).

        The sentinel goes in BEHIND everything already queued, so a clean close
        still delivers the last toast and the last state push - which is the
        difference between a window that closes and a window that closes having
        said why.
        """
        self._closed = True
        self._events.put(None)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    # -- sending --------------------------------------------------------------

    def send(self, event_type: str, **fields: Any) -> None:
        """Queue one event for the page. Never blocks, never raises.

        The one method every worker thread in the app reaches, which is why it
        does nothing but serialise and append: whatever is wrong with the window
        is the drainer's problem, not the detector poller's.
        """
        if self._closed:
            return
        self._events.put(render_call({"type": event_type, **fields}))

    def drain_pending(self) -> int:
        """Emit everything queued right now, on the CALLING thread, and stop.

        Not used by the app - the drainer thread is - but it is how a test reads
        the bridge without one, and how ``stop`` behaves if a drainer was never
        started. Returns how many events went out.
        """
        sent = 0
        while True:
            try:
                script = self._events.get_nowait()
            except queue.Empty:
                return sent
            if script is None:
                return sent
            self._deliver(script)
            sent += 1

    # -- the drainer ----------------------------------------------------------

    def _drain(self) -> None:
        while True:
            script = self._events.get()
            if script is None:
                return
            self._deliver(script)

    def _deliver(self, script: str) -> None:
        emit = self._emit
        if emit is None:
            return
        try:
            emit(script)
        except Exception:  # noqa: BLE001 - a closing window is not an app error
            # ``evaluate_js`` raises whenever the window is on its way out (and
            # ``JavascriptException`` for a page that broke). Neither is worth
            # taking the app down for, and neither is worth a toast: the toast
            # would go through this same channel.
            return


class JsCalls(Protocol):
    """What :class:`JsApi` forwards to - the runner, structurally.

    A Protocol rather than the runner class so the bridge stays importable (and
    testable) without a loop, a window or a controller behind it. Every method
    here is called on a pywebview-owned thread and must return promptly; the
    implementation marshals onto the GUI's asyncio loop.
    """

    def page_ready(self) -> None: ...
    def submit_text(self, text: str) -> None: ...
    def submit_decision(self, choice: str, note: str) -> None: ...
    def cancel_execution(self) -> None: ...
    def answer_prompt(self, prompt_id: str, value: Any) -> None: ...


class JsApi:
    """JS -> Python: the object pywebview exposes as ``window.pywebview.api``.

    Deliberately tiny, and deliberately not a second controller: every method
    forwards to the same call the TUI's key bindings make
    (``SessionController.submit_message`` / ``submit_decision`` /
    ``cancel_execution``), so the two shells cannot grow two different ideas of
    what a send or an approval IS.

    ``submit_clipboard`` is absent on purpose: the clipboard watcher ingests,
    the page never does (``AutomationController`` owns the watcher thread and
    hands captures to the session itself).

    Every method swallows its own exceptions. pywebview logs and drops what a
    js_api method raises, so a failure here would otherwise be a click that
    silently did nothing - and the page is holding a promise that would never
    settle.
    """

    def __init__(self, calls: JsCalls) -> None:
        self._calls = calls

    def ready(self) -> None:
        """The page has installed its receiver and is ready to be painted."""
        self._safely(self._calls.page_ready)

    def submit(self, text: str = "") -> None:
        """The composer's Enter: a task, an ask_user answer, a follow-up or a
        slash command - the same one door ``MainScreen._submit_text`` is."""
        self._safely(lambda: self._calls.submit_text(text))

    def decide(self, choice: str = "", note: str = "") -> None:
        """The approval gate's answer: ``approve`` / ``approve_always`` / ``reject``."""
        self._safely(lambda: self._calls.submit_decision(choice, note))

    def cancel(self) -> None:
        """ctrl+x's equivalent: stop the tool calls in flight (the turn still
        finishes and still reports back - it is not an abort)."""
        self._safely(self._calls.cancel_execution)

    def prompt(self, prompt_id: str = "", value: Any = None) -> None:
        """One blocking prompt's answer, keyed by the id the modal was opened
        with (confirm / prompt_text / show_summary)."""
        self._safely(lambda: self._calls.answer_prompt(prompt_id, value))

    @staticmethod
    def _safely(call: Callable[[], None]) -> None:
        try:
            call()
        except Exception:  # noqa: BLE001 - see the class docstring
            return
