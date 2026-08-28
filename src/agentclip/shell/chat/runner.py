"""GuiRunner: the GUI's asyncio loop, on a thread of its own, and its lifecycle.

The crux of the GUI shell, and the piece every later increment is built on.

:class:`~agentclip.shell.app.SessionController` is asyncio to the bone - its flows are
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

**Closing mid-turn.** pywebview's ``closing`` event is created with
``should_lock=True`` (``webview/window.py``), so its handlers run SYNCHRONOUSLY
on the window's own thread and a handler returning ``False`` sets
``args.Cancel`` on the WinForms ``FormClosing`` args - the close is refused
(``webview/platforms/winforms.py:on_closing``, ``webview/event.py:Event.set``).
That is the whole mechanism :meth:`GuiRunner.window_closing` uses, and it is
also why that method may do nothing but read a flag and post: the bridge's
drainer parks inside ``evaluate_js`` waiting on this very thread, and
``destroy_window`` reaches it through a blocking ``Control.Invoke``, so anything
that waited here would be waiting on itself. It posts the confirm onto the loop,
returns ``False``, and the *answer* comes back through the ordinary bridge path
and closes the window from the loop thread.

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

from agentclip.config import Config
from agentclip.driver.clip.base import ClipboardProvider
from agentclip.engine.link.factory import EngineRequest
from agentclip.protocol.preset import PresetSource
from agentclip.shell.app.link import Link, SkillReport
from agentclip.shell.app.monitor_launch import LocalMonitorLauncher
from agentclip.shell.chat.remote import RemoteConnect
from agentclip.shell.chat.view import GuiView, McpStatusSource, MonitorLaunch
from agentclip.shell.webview.bridge import Bridge, EmitFn, JsApi

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
        engine_factory: Callable[[EngineRequest], Link],
        project_root: Path,
        global_config_path: Path | None = None,
        mcp_manager: McpStatusSource | None = None,
        skills: Callable[[], SkillReport] | None = None,
        host: Any = None,
        remote: RemoteConnect | None = None,
        on_close: Callable[[], None] | None = None,
        on_config_change: Callable[[Config], None] | None = None,
        on_preset_source: Callable[[PresetSource], None] | None = None,
        monitor_target: MonitorLaunch = None,
        launcher: LocalMonitorLauncher | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopped = False
        self._page_started = False
        # Has a close already been approved? Set by every PROGRAMMATIC close
        # (``request_close``, which is both ``ChatView.exit_app`` and the
        # confirm's yes) and by the first ``closing`` that found nothing to
        # lose, so the ``closing`` that ``window.destroy()`` itself raises does
        # not ask the same question a second time.
        self._quit_ok = threading.Event()
        self._on_close = on_close if on_close is not None else _no_close
        self.bridge = Bridge()
        self.view = GuiView(
            self.bridge,
            config=config,
            provider=provider,
            engine_factory=engine_factory,
            project_root=project_root,
            global_config_path=global_config_path,
            mcp_manager=mcp_manager,
            skills=skills,
            host=host,
            remote=remote,
            schedule=self.schedule,
            on_exit=self.request_close,
            on_config_change=on_config_change,
            monitor_target=monitor_target,
            launcher=launcher,
        )
        # The window's answer to "which service is the monitor driving?", handed
        # UP to whoever built the engine factory (docs/design/ui-monitor.md
        # §11.9). It has to happen here, right after the view exists: the
        # factory was built before this window was, and the question can only be
        # answered by the object that adopts the monitor's ``Watched``.
        if on_preset_source is not None:
            on_preset_source(self.view.engine_preset)
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
        happens after ``webview.start()`` returns, so quitting from the page and
        quitting from the title bar take exactly one path.

        Called from the GUI's loop thread - by the controller when it wants out,
        and by the quit confirm's yes. Either way the close is already approved,
        so the flag goes down first: ``window.destroy()`` raises ``closing`` on
        the window's own thread on its way out, and that one must sail through
        rather than re-ask.
        """
        self._quit_ok.set()
        try:
            self._on_close()
        except Exception:  # noqa: BLE001 - a window already gone is not an error
            return

    def window_closing(self) -> bool | None:
        """The window is being closed. Return ``False`` to refuse it.

        **Runs on the window's own thread** and must never block there (see the
        module docstring): the drainer is parked against that thread inside
        ``evaluate_js``, and a ``destroy`` reaches it through a blocking
        ``Invoke``. So this reads two flags and, at most, posts one callback.

        Three outcomes, and only the middle one is new:

        * already approved (``exit_app``, or a confirmed quit) -> ``None``,
          close proceeds;
        * a turn would be lost (``GuiView.mid_turn``, ``action_quit``'s own
          formula) -> post the confirm onto the loop and return ``False``, which
          is what ``Event.set`` turns into ``args.Cancel = True``;
        * nothing in flight -> ``None``, exactly as this shell has always
          behaved.
        """
        if self._quit_ok.is_set():
            return None
        try:
            mid_turn = self.view.mid_turn
        except Exception:  # noqa: BLE001 - never trap a user in a window
            return None
        if not mid_turn:
            self._quit_ok.set()
            return None
        self.schedule_call(self.view.confirm_quit)
        return False

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
        # 1. Nothing may keep touching the machine: the clipboard watcher and
        #    the attention alarm are threads with a synchronous off switch, so
        #    they are stopped by name (MainScreen.on_unmount).
        self.view.shutdown()
        thread = self._thread
        if thread is not None and self._loop.is_running():
            # 2. The monitor's poll thread. ``UIMonitor.close`` is a coroutine by
            #    contract - a remote monitor's is a round trip - so it needs a
            #    loop, and this is the last moment there is one: step 3 cancels
            #    everything left on it. Waited for rather than fired off, because
            #    what it stops is a thread that captures the screen.
            closed = threading.Event()
            self._loop.call_soon_threadsafe(self._close_view, closed)
            closed.wait(SHUTDOWN_TIMEOUT_S)
            # 3. Cancel the session worker - here, every task on the loop.
            #    Textual does this for a screen's workers on unmount; this loop's
            #    tasks ARE those workers.
            done = threading.Event()
            self._loop.call_soon_threadsafe(self._cancel_all, done)
            done.wait(SHUTDOWN_TIMEOUT_S)
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join(SHUTDOWN_TIMEOUT_S)
        self._thread = None
        # 3b. The local monitor CHILD, if this window started one (§10.1).
        #     After the link is closed and not before: the child dies with a
        #     terminate either way, and stopping it first would turn an orderly
        #     close of the wire into a dropped link the teardown then reports.
        #     A launcher that never started anything is a no-op here.
        self.view.launcher.stop()
        # 4. Last, so anything the teardown said still reaches the page.
        self.bridge.stop()

    def _close_view(self, done: threading.Event) -> None:
        """Close the view's monitor. Runs ON the loop, by call_soon.

        A task rather than a call because the verb is a coroutine; the event is
        set from its completion callback, so the waiting thread learns about a
        failure the same way it learns about a success rather than hanging on
        one.
        """
        try:
            task = self._loop.create_task(self.view.close())
        except RuntimeError:  # the loop went away between the check and here
            done.set()
            return
        task.add_done_callback(lambda _task: done.set())

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

    def dismiss_pending_question(self) -> None:
        self.schedule_call(self.view.dismiss_question)

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

    def press_enter(self) -> None:
        self.schedule_call(self.view.press_enter)

    def copy_again(self) -> None:
        self.schedule_call(self.view.copy_again)

    def select_window(self, window: str) -> None:
        self.schedule_call(self.view.select_window, window)

    def next_window(self) -> None:
        self.schedule_call(self.view.next_window)

    def end_session(self) -> None:
        self.schedule_call(self.view.end_session)

    def undo(self) -> None:
        self.schedule_call(self.view.undo)

    def export_log(self) -> None:
        self.schedule_call(self.view.export_log)

    def set_theme(self, theme: str) -> None:
        self.schedule_call(self.view.set_theme, theme)

    def request_quit(self) -> None:
        self.schedule_call(self.view.request_quit)

    # The SSH connect dialog (increment 7). One-line marshals for the reason
    # every method here is one: the dialog's state is a MODEL on the loop
    # (``chat/remote.py``), and pywebview hands each of these a thread of its own.

    def open_connect(self) -> None:
        self.schedule_call(self.view.open_connect)

    def connect_select(self, key: str) -> None:
        self.schedule_call(self.view.connect_select, key)

    def connect_fields(self, target: str, root: str) -> None:
        self.schedule_call(self.view.connect_fields, target, root)

    def connect_start(self) -> None:
        self.schedule_call(self.view.connect_start)

    def connect_edit(self) -> None:
        self.schedule_call(self.view.connect_edit)

    def connect_cancel(self) -> None:
        self.schedule_call(self.view.connect_cancel)

    def connect_save(self, name: str) -> None:
        self.schedule_call(self.view.connect_save, name)

    def reconnect_now(self) -> None:
        self.schedule_call(self.view.reconnect_now)

    # The Monitor tab of the same dialog (ui-monitor.md §9.2). Same shape and
    # same reason: the tab is a MODEL on the loop (``chat/remote.py``), and a
    # dial is a link event rather than a new session, so nothing here differs
    # from its SSH sibling except which question the form asks.

    def monitor_open(self) -> None:
        self.schedule_call(self.view.monitor_open)

    def monitor_select(self, key: str) -> None:
        self.schedule_call(self.view.monitor_select, key)

    def monitor_fields(self, mode: str, host: str, port: str, token: str, via: str) -> None:
        self.schedule_call(self.view.monitor_fields, mode, host, port, token, via)

    def monitor_start(self) -> None:
        self.schedule_call(self.view.monitor_start)

    def monitor_edit(self) -> None:
        self.schedule_call(self.view.monitor_edit)

    def monitor_cancel(self) -> None:
        self.schedule_call(self.view.monitor_cancel)

    def monitor_save(self, name: str) -> None:
        self.schedule_call(self.view.monitor_save, name)

    def monitor_forget(self, name: str) -> None:
        self.schedule_call(self.view.monitor_forget, name)

    def monitor_disconnect(self) -> None:
        self.schedule_call(self.view.monitor_disconnect)


def _no_close() -> None:
    """The close a runner with no window behind it gets (tests)."""


__all__: Sequence[str] = ["SHUTDOWN_TIMEOUT_S", "GuiRunner"]
