"""GuiRunner: the GUI's asyncio loop, on a thread of its own, and its lifecycle.

The crux of the GUI shell, and the piece every later increment is built on.

:class:`~agentclip.app.SessionController` is asyncio to the bone - its flows are
coroutines, its approval gate and its ``ask_user`` answer are ``asyncio.Future``
s, and every engine call is an ``asyncio.to_thread``. The TUI hands it Textual's
loop. pywebview has no loop to hand it: ``webview.start()`` runs a native
message pump on the MAIN thread and blocks there until the last window closes.
So the GUI brings its own.

**The model, in one paragraph.** One dedicated thread runs one
``asyncio`` event loop for the whole app run. Every session flow, every blocking
prompt and every OS-acting sequence lives on it (``GuiView.spawn`` is
:meth:`GuiRunner.schedule`). The main thread does nothing but ``webview.start()``.
pywebview calls ``js_api`` methods on a fresh thread per call, so each of the
five below is a one-line marshal onto the loop with ``call_soon_threadsafe`` -
the page never touches controller state, and neither does the window's event
thread. Going the other way, the bridge's single drainer thread is the only
caller of ``evaluate_js``. The automation's watcher and detector threads are
unchanged: the AutomationController has always owned them, and this shell only
starts and stops them exactly as ``MainScreen`` does.

**Shutdown** is the same shape as the TUI's quit path (``AgentClipApp.action_quit``
-> ``MainScreen.on_unmount``), in the same order and for the same reasons: stop
what touches the machine first (the clipboard watcher and the detector poller,
by name, because they are threads and not workers), then cancel the session
worker - which here means cancelling every task on the loop, the equivalent of
Textual cancelling a screen's workers on unmount - then stop the loop, then let
the bridge flush what it still owes the page. Every wait is bounded: a window
that will not close is worse than a paint that was never delivered.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from agentclip.app.types import EngineRequest
from agentclip.clip.base import ClipboardProvider
from agentclip.config import Config
from agentclip.engine.engine import Engine
from agentclip.gui.bridge import Bridge, EmitFn, JsApi
from agentclip.gui.view import GuiView, McpStatusSource

# How long the loop thread gets to unwind before the window closes anyway. The
# tasks being cancelled are flows parked on a future or inside an engine call on
# a worker thread; the first unwinds instantly and the second is a daemon thread
# the process exit collects. Neither is worth a window that hangs.
SHUTDOWN_TIMEOUT_S = 3.0


class GuiRunner:
    """Owns the loop, the bridge, the view, and the order they start and stop in."""

    def __init__(
        self,
        *,
        config: Config,
        provider: ClipboardProvider,
        engine_factory: Callable[[EngineRequest], Engine],
        project_root: Path,
        profile_root: Path | None = None,
        mcp_manager: McpStatusSource | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopped = False
        self._page_started = False
        self._on_close = on_close if on_close is not None else _no_close
        self.bridge = Bridge()
        self.view = GuiView(
            self.bridge,
            config=config,
            provider=provider,
            engine_factory=engine_factory,
            project_root=project_root,
            profile_root=profile_root,
            mcp_manager=mcp_manager,
            schedule=self.schedule,
            on_exit=self.request_close,
        )
        self.js_api = JsApi(self)

    # -- the window's side ----------------------------------------------------

    def attach(self, emit: EmitFn, on_close: Callable[[], None] | None = None) -> None:
        """Point the bridge at the window's ``evaluate_js`` (and name the close).

        Separate from construction because the ``js_api`` object above has to
        exist before ``create_window`` is called and the window has to exist
        before its ``evaluate_js`` can be bound - one of the two must come
        second, and it is this one.
        """
        self.bridge.attach(emit)
        if on_close is not None:
            self._on_close = on_close

    def request_close(self) -> None:
        """``ChatView.exit_app``: ask the window to go away. Shutdown itself
        happens on the window's ``closing`` event, so quitting from the page and
        quitting from the title bar take exactly one path."""
        try:
            self._on_close()
        except Exception:  # noqa: BLE001 - a window already gone is not an error
            return

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the loop thread and block until it is really running.

        Before ``webview.start()``: the controller's first act is to park on
        ``prompt_new_session``, and a flow scheduled onto a loop that has not
        begun would sit in the call-soon queue rather than paint the prompt.
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="agentclip-loop", daemon=True)
        self._thread.start()
        self._running.wait(SHUTDOWN_TIMEOUT_S)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._running.set)
        try:
            self._loop.run_forever()
        finally:
            self._running.clear()

    def page_loaded(self) -> None:
        """The window finished loading: start draining, and start the session.

        Both here rather than at ``start``, so the first thing the page ever
        receives is the state it is meant to show. Queued events would have been
        delivered anyway (``evaluate_js`` waits for the page itself), but the
        session flow's very first paint is the "describe the task" prompt and it
        should not race the page's own first frame.

        Once per window, not once per load: pywebview re-fires ``loaded`` for
        any later navigation, and starting a second session flow on top of a
        live one would put two ``prompt_new_session`` futures on the same view.
        """
        self.bridge.start()
        if self._page_started:
            return
        self._page_started = True
        self.schedule_call(self.view.start)

    def stop(self) -> None:
        """Tear the whole shell down, in the order the TUI's quit path uses.

        Idempotent: the window's ``closing`` event and ``run_gui``'s own
        ``finally`` both reach it, and a second call must be free.
        """
        if self._stopped:
            return
        self._stopped = True
        # 1. Nothing may keep touching the machine: the watcher and the poller
        #    are threads, so they are stopped by name (MainScreen.on_unmount).
        self.view.shutdown()
        # 2. Cancel the session worker - here, every task on the loop. Textual
        #    does this for a screen's workers on unmount; this loop's tasks ARE
        #    those workers.
        thread = self._thread
        if thread is not None and self._loop.is_running():
            done = threading.Event()
            self._loop.call_soon_threadsafe(self._cancel_all, done)
            done.wait(SHUTDOWN_TIMEOUT_S)
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join(SHUTDOWN_TIMEOUT_S)
        self._thread = None
        # 3. Last, so anything the teardown said still reaches the page.
        self.bridge.stop()

    def _cancel_all(self, done: threading.Event) -> None:
        """Cancel every task on the loop. Runs ON the loop, by call_soon."""
        try:
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
        finally:
            done.set()

    # -- scheduling -----------------------------------------------------------

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Put a coroutine on the GUI's loop, from wherever the caller is.

        ``GuiView.spawn`` and every worker-thread hand-off end here. Both cases
        are real: a controller flow spawning another is already ON the loop, and
        the clipboard watcher's capture is not - so the thread is checked rather
        than assumed, because ``run_coroutine_threadsafe`` called from the loop
        thread itself would deadlock anything that then waited on it.
        """
        if self._stopped or self._loop.is_closed():
            coro.close()
            return
        try:
            if threading.current_thread() is self._thread:
                self._loop.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:  # the loop closed under us mid-teardown
            coro.close()

    def schedule_call(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run a plain callable on the loop. The synchronous twin of
        :meth:`schedule`, and what every ``js_api`` method marshals through."""
        if self._stopped or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            return

    # -- JsCalls: what the page can ask for -----------------------------------
    # Each of these is called by pywebview on a thread of its own (one per
    # call), so each does exactly one thing: hand the intent to the loop.

    def page_ready(self) -> None:
        self.schedule_call(self.view.page_ready)

    def submit_text(self, text: str) -> None:
        self.schedule_call(self.view.submit_text, text)

    def submit_decision(self, choice: str, note: str) -> None:
        self.schedule_call(self.view.submit_decision, choice, note)

    def cancel_execution(self) -> None:
        self.schedule_call(self.view.cancel_execution)

    def answer_prompt(self, prompt_id: str, value: Any) -> None:
        self.schedule_call(self.view.answer_prompt, prompt_id, value)

    def set_os_armed(self, target: bool | None) -> None:
        self.schedule_call(self.view.set_os_armed, target)

    def cycle_permission_mode(self) -> None:
        self.schedule_call(self.view.cycle_permission_mode)

    def toggle_watch(self) -> None:
        self.schedule_call(self.view.toggle_watch)

    def recopy(self) -> None:
        self.schedule_call(self.view.recopy)

    def force_ingest(self) -> None:
        self.schedule_call(self.view.force_ingest)

    def reinstruct(self) -> None:
        self.schedule_call(self.view.reinstruct)

    def retry_insert(self) -> None:
        self.schedule_call(self.view.retry_insert)

    def set_service(self, key: str) -> None:
        self.schedule_call(self.view.set_service, key)


def _no_close() -> None:
    """The close a runner with no window behind it gets (tests)."""


__all__: Sequence[str] = ["SHUTDOWN_TIMEOUT_S", "GuiRunner"]
