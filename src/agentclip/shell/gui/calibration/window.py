"""The calibration window itself: its pywebview window, its bridge, its js_api.

``docs/design/ui-monitor.md`` §6.4. This is the GUI's **second** ``create_window``
- the chat shell has exactly one (``shell/gui/shell.py``) - so nothing here is a
refactor of an existing pattern; what it copies from ``shell.py`` and
``runner.py`` is their SHAPE, and the two-window facts that shape does not cover
are spelled out below.

**One pump, two windows.** ``webview.start()`` runs a native message pump on the
process's MAIN thread and returns only when the LAST window closes; calling it
twice in one process is not a thing pywebview supports. So there are two entry
points and they are not symmetric:

* :func:`run_calibration` is the STANDALONE one (``agentclip --calibrate``). It
  owns everything - the asyncio loop thread, the window, and the pump.
* :func:`open_calibration_window` is the door a shell that is ALREADY pumping
  uses (the chat GUI's titlebar button, phase 4B). It creates the window and
  wires it, and returns; the running pump picks the new window up. It
  deliberately does not call ``webview.start()`` and does not own a loop - the
  caller passes ``schedule``, which in the chat GUI is ``GuiRunner.schedule``,
  so both windows' work runs on the one loop that shell already has.

**The js_api is per window.** pywebview binds the object passed as ``js_api`` to
that window's ``window.pywebview.api``, so the calibration page reaches
:class:`CalibrationJsApi` and the chat page reaches ``bridge.JsApi`` - two
vocabularies, no prefixing, and neither page can call the other's methods.

**The bridge is per window too**, and that is the reason
:class:`CalibrationBridge` exists at all: a bridge IS a FIFO plus the one thread
allowed to call a particular window's ``evaluate_js``, and pointing two windows
at one drainer would serialise each window's paints behind the other's.

Everything pywebview is imported INSIDE functions: the ``gui`` extra is
optional, so importing this module must stay free.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Protocol

from agentclip import __version__
from agentclip.config import Config, default_profile_dir
from agentclip.driver.clip.base import ClipboardProvider
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.screen.profile_store import load_profile
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.gui.bridge import Bridge, EmitFn
from agentclip.shell.gui.calibration.view import CalibrationMonitor, CalibrationView

WINDOW_TITLE = "AgentClip · calibration"
WINDOW_SIZE = (1180, 820)
# Small on purpose, for the chat window's reason: this window has to be able to
# sit beside the browser it is calibrating, on whatever panel that browser is on.
MIN_WINDOW_SIZE = (420, 320)
SCREEN_MARGIN = 80
# The page paints its own background; matching it here stops the white flash
# WebView2 would otherwise show between window creation and first paint.
WINDOW_BACKGROUND = "#14161a"
# pywebview injects ``body { user-select: none }`` when this is False, which no
# rule in app.css can win back - and a service key nobody can copy out of the
# form is a form that fights its user (``shell.py:WINDOW_TEXT_SELECT``).
WINDOW_TEXT_SELECT = True

# How long the loop thread gets to unwind before the window closes anyway.
SHUTDOWN_TIMEOUT_S = 3.0

# The asset files this window needs, resolved the way ``shell.py`` resolves the
# chat window's: ``importlib.resources`` against the PACKAGE, never ``__file__``,
# so a frozen build finds them under the onefile extraction. That only works if
# ``packaging/agentclip.spec`` collects this directory at the package-relative
# path, which it does beside the chat shell's (gui.md §5).
ASSET_PACKAGE = "agentclip.shell.gui.calibration"
ASSET_DIR = "assets"
ENTRY_PAGE = "index.html"
ASSET_NAMES = (ENTRY_PAGE, "app.css", "app.js")

MISSING_PYWEBVIEW = (
    "agentclip: the gui extra is not installed - run: uv sync --extra gui"
    " (or: pip install 'agentclip[gui]')"
)


class CalibrationBridge(Bridge):
    """This window's own FIFO into this window's own page.

    A subclass rather than a second implementation: what a bridge does - queue a
    JSON event, drain it from exactly one thread, never block the caller - is
    identical for both windows and is already pinned by ``tests/shell/gui``.
    What must NOT be shared is the INSTANCE: a bridge owns one drainer thread
    parked inside one window's ``evaluate_js``, so two windows on one bridge
    would put every calibration paint behind whatever the chat window's WebView2
    was doing, and vice versa.

    The event vocabulary is this package's own and is small enough to say here:
    ``editor`` (the whole :class:`~agentclip.shell.gui.service_editor.ServiceEditor`
    state, exactly the chat shell's shape), ``elements`` (the crop rows),
    ``calib`` (which window, its rectangle, its service), ``toast``, and
    ``modal``/``modal_close`` for the editor's confirms.
    """


class CalibrationCalls(Protocol):
    """What :class:`CalibrationJsApi` forwards to - the runner, structurally.

    A Protocol rather than the runner class, so this module stays importable and
    testable with no loop, no window and no monitor behind it. Every method here
    is called by pywebview on a thread of its own and must return promptly; the
    implementation marshals onto the calibration loop.
    """

    def page_ready(self) -> None: ...
    def start_page(self) -> None: ...
    def select_slot(self, name: str) -> None: ...
    def set_chat_region(self) -> None: ...
    def show_identify(self) -> None: ...
    def set_elements_visible(self, visible: bool) -> None: ...
    def answer_prompt(self, prompt_id: str, value: Any) -> None: ...
    def close_from_page(self) -> None: ...
    def svc_select(self, key: str) -> None: ...
    def svc_form(self, fields: dict[str, Any]) -> None: ...
    def svc_detection(self, state: dict[str, Any]) -> None: ...
    def svc_edit_by_lines(self, on: bool) -> None: ...
    def svc_after_delivery(self, state: dict[str, Any]) -> None: ...
    def svc_scroll(self, action: str) -> None: ...
    def svc_matcher(self, matcher: str) -> None: ...
    def svc_tolerance(self, value: int) -> None: ...
    def svc_add(self) -> None: ...
    def svc_reset(self) -> None: ...
    def svc_delete(self) -> None: ...
    def svc_prev(self, kind: str) -> None: ...
    def svc_next(self, kind: str) -> None: ...
    def svc_capture(self, kind: str) -> None: ...
    def svc_clear(self, kind: str) -> None: ...
    def svc_click_point(self, kind: str, x: int, y: int) -> None: ...
    def svc_forget(self) -> None: ...
    def svc_close(self) -> None: ...


class CalibrationJsApi:
    """JS -> Python: the object pywebview exposes as this window's
    ``window.pywebview.api``.

    The twin of ``bridge.JsApi`` and deliberately not a merge with it: the two
    pages have two vocabularies, and a single api object with both would be a
    chat window one ``svc_capture`` away from throwing an overlay over itself.

    Every method swallows its own exceptions. pywebview logs and drops what a
    js_api method raises, so a failure here would otherwise be a click that
    silently did nothing.
    """

    def __init__(self, calls: CalibrationCalls) -> None:
        self._calls = calls

    def ready(self) -> None:
        """The page installed its receiver and is ready to be painted."""
        self._safely(self._calls.page_ready)

    def start(self) -> None:
        """The page's first load: build the editor and start painting."""
        self._safely(self._calls.start_page)

    def slot(self, name: str = "") -> None:
        """The MASTER / SUB-AGENT picker."""
        self._safely(lambda: self._calls.select_slot(name))

    def set_region(self) -> None:
        """"Set chat region...": the fullscreen draw-a-box child process."""
        self._safely(self._calls.set_chat_region)

    def identify(self) -> None:
        """"Identify": box everything recognisable inside the drawn window."""
        self._safely(self._calls.show_identify)

    def elements(self, visible: bool = False) -> None:
        """The ELEMENTS column opened or closed - the encoder has to be told."""
        self._safely(lambda: self._calls.set_elements_visible(bool(visible)))

    def prompt(self, prompt_id: str = "", value: Any = None) -> None:
        """A modal's answer, keyed by the id it was asked under."""
        self._safely(lambda: self._calls.answer_prompt(prompt_id, value))

    def close_window(self) -> None:
        """The window's own close: routed through the editor's apply path."""
        self._safely(self._calls.close_from_page)

    # -- the service editor ---------------------------------------------------

    def svc_select(self, key: str = "") -> None:
        self._safely(lambda: self._calls.svc_select(key))

    def svc_form(self, fields: Any = None) -> None:
        """A keystroke in the form column: the WHOLE candidate, every time."""
        self._safely(lambda: self._calls.svc_form(dict(fields or {})))

    def svc_detection(self, state: Any = None) -> None:
        """Any toggle on the left column, read as a SET."""
        self._safely(lambda: self._calls.svc_detection(dict(state or {})))

    def svc_edit_by_lines(self, on: bool = False) -> None:
        self._safely(lambda: self._calls.svc_edit_by_lines(bool(on)))

    def svc_after_delivery(self, state: Any = None) -> None:
        """The AFTER DELIVERY ticks, read as a pair."""
        self._safely(lambda: self._calls.svc_after_delivery(dict(state or {})))

    def svc_scroll(self, action: str = "") -> None:
        self._safely(lambda: self._calls.svc_scroll(action))

    def svc_matcher(self, matcher: str = "") -> None:
        self._safely(lambda: self._calls.svc_matcher(matcher))

    def svc_tolerance(self, value: int = 0) -> None:
        self._safely(lambda: self._calls.svc_tolerance(int(value)))

    def svc_add(self) -> None:
        self._safely(self._calls.svc_add)

    def svc_reset(self) -> None:
        self._safely(self._calls.svc_reset)

    def svc_delete(self) -> None:
        self._safely(self._calls.svc_delete)

    def svc_capture(self, kind: str = "") -> None:
        self._safely(lambda: self._calls.svc_capture(kind))

    def svc_prev(self, kind: str = "") -> None:
        self._safely(lambda: self._calls.svc_prev(kind))

    def svc_next(self, kind: str = "") -> None:
        self._safely(lambda: self._calls.svc_next(kind))

    def svc_clear(self, kind: str = "") -> None:
        self._safely(lambda: self._calls.svc_clear(kind))

    def svc_click_point(self, kind: str = "", x: int = 50, y: int = 50) -> None:
        self._safely(lambda: self._calls.svc_click_point(kind, int(x), int(y)))

    def svc_forget(self) -> None:
        self._safely(self._calls.svc_forget)

    def svc_close(self) -> None:
        """Esc: apply the valid edits and leave - which here closes the window."""
        self._safely(self._calls.svc_close)

    @staticmethod
    def _safely(call: Callable[[], None]) -> None:
        try:
            call()
        except Exception:  # noqa: BLE001 - see the class docstring
            return


class CalibrationRunner:
    """Owns this window's loop, bridge and view, and the order they start in.

    ``GuiRunner``'s shape at a tenth the size, and with one extra knob: ``schedule``
    may be handed in. A standalone run leaves it None and gets a loop thread of
    its own; a chat GUI opening this window passes ``GuiRunner.schedule``, so the
    two windows' coroutines run on the loop that shell already owns rather than
    on a second one that would have to be shut down in the right order.
    """

    def __init__(
        self,
        *,
        config: Config,
        monitor: CalibrationMonitor,
        profile_root: Path | None = None,
        global_config_path: Path | None = None,
        schedule: Callable[[Coroutine[Any, Any, Any]], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_config_change: Callable[[Config], None] | None = None,
        on_calibration: Callable[[AgentSlot, ScreenRegion | None], None] | None = None,
    ) -> None:
        self._owns_loop = schedule is None
        self._loop = asyncio.new_event_loop() if self._owns_loop else None
        # The door onto whatever loop this runner's work runs on: its own, or
        # the one a hosting shell handed in.
        self._schedule: Callable[[Coroutine[Any, Any, Any]], None] = (
            self.schedule if schedule is None else schedule
        )
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._stopped = False
        self._page_started = False
        self._on_close = on_close if on_close is not None else _no_close
        self.bridge = CalibrationBridge()
        self.view = CalibrationView(
            self.bridge,
            config=config,
            monitor=monitor,
            profile_root=profile_root,
            global_config_path=global_config_path,
            schedule=self._schedule,
            on_exit=self.request_close,
            on_config_change=on_config_change,
            on_calibration=on_calibration,
        )
        self.js_api = CalibrationJsApi(self)

    # -- the window's side ----------------------------------------------------

    def attach(self, emit: EmitFn, on_close: Callable[[], None] | None = None) -> None:
        """Point the bridge at this window's ``evaluate_js`` (and name the close).

        Separate from construction because the ``js_api`` object has to exist
        before ``create_window`` is called and the window has to exist before its
        ``evaluate_js`` can be bound - one of the two must come second.
        """
        self.bridge.attach(emit)
        if on_close is not None:
            self._on_close = on_close

    def request_close(self) -> None:
        """Close this window. Nothing here can be lost - every edit was either
        applied or explicitly discarded before this is reached - so unlike the
        chat window's close there is no question to ask first."""
        try:
            self._on_close()
        except Exception:  # noqa: BLE001 - a window already gone is not an error
            return

    def page_loaded(self) -> None:
        """The window finished loading: start draining, then build the surface.

        Once per window, not once per load: pywebview re-fires ``loaded`` for any
        later navigation, and a second ``start`` would subscribe a second frame
        hook onto the same monitor.
        """
        self.bridge.start()
        if self._page_started:
            return
        self._page_started = True
        self.schedule_call(self.view.start)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the loop thread (when this runner owns one) and wait for it."""
        if not self._owns_loop or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="agentclip-calibration", daemon=True
        )
        self._thread.start()
        self._running.wait(SHUTDOWN_TIMEOUT_S)

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None  # only reached when this runner owns one
        asyncio.set_event_loop(loop)
        loop.call_soon(self._running.set)
        try:
            loop.run_forever()
        finally:
            self._running.clear()

    def stop(self) -> None:
        """Tear this window down. Idempotent: the pump's return and an explicit
        close both reach it, and a second call must be free.

        Order is ``GuiRunner.stop``'s, minus everything a session owns: close the
        view (which unsubscribes the frame hook and ends the monitor's poll
        thread - a coroutine by contract, so it needs a loop and this is the last
        moment there is one), cancel what is left, stop the loop, flush the
        bridge last so anything the teardown said still reaches the page.
        """
        if self._stopped:
            return
        self._stopped = True
        loop = self._loop
        if loop is not None and self._thread is not None and loop.is_running():
            closed = threading.Event()
            loop.call_soon_threadsafe(self._close_view, closed)
            closed.wait(SHUTDOWN_TIMEOUT_S)
            done = threading.Event()
            loop.call_soon_threadsafe(self._cancel_all, done)
            done.wait(SHUTDOWN_TIMEOUT_S)
            loop.call_soon_threadsafe(loop.stop)
            self._thread.join(SHUTDOWN_TIMEOUT_S)
        self._thread = None
        self.bridge.stop()

    def _close_view(self, done: threading.Event) -> None:
        """Close the view. Runs ON the loop, by call_soon."""
        loop = self._loop
        if loop is None:
            done.set()
            return
        try:
            task = loop.create_task(self.view.close())
        except RuntimeError:  # the loop went away between the check and here
            done.set()
            return
        task.add_done_callback(lambda _task: done.set())

    def _cancel_all(self, done: threading.Event) -> None:
        loop = self._loop
        if loop is None:
            done.set()
            return
        try:
            for task in asyncio.all_tasks(loop):
                task.cancel()
        finally:
            done.set()

    # -- scheduling -----------------------------------------------------------

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Put a coroutine on this window's loop, from wherever the caller is."""
        loop = self._loop
        if loop is None or self._stopped or loop.is_closed():
            coro.close()
            return
        try:
            if threading.current_thread() is self._thread:
                loop.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # the loop closed under us mid-teardown
            coro.close()

    def schedule_call(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run a plain callable on the loop - what every js_api call marshals
        through, because pywebview hands each of them a thread of its own."""
        loop = self._loop
        if loop is None:
            # Borrowing another shell's loop: its ``schedule`` is the only door,
            # so a plain call becomes a one-line coroutine.
            self._borrowed(fn, *args)
            return
        if self._stopped or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            return

    def _borrowed(self, fn: Callable[..., Any], *args: Any) -> None:
        async def run() -> None:
            fn(*args)

        self._schedule(run())

    # -- CalibrationCalls: what the page can ask for --------------------------

    def page_ready(self) -> None:
        self.schedule_call(self.view.page_ready)

    def start_page(self) -> None:
        self.schedule_call(self.view.start)

    def select_slot(self, name: str) -> None:
        self.schedule_call(self.view.select_slot, name)

    def set_chat_region(self) -> None:
        self.schedule_call(self.view.set_chat_region)

    def show_identify(self) -> None:
        self.schedule_call(self.view.show_identify_overlay)

    def set_elements_visible(self, visible: bool) -> None:
        self.schedule_call(self.view.set_elements_visible, visible)

    def answer_prompt(self, prompt_id: str, value: Any) -> None:
        self.schedule_call(self.view.answer_prompt, prompt_id, value)

    def close_from_page(self) -> None:
        """The page's close button. Routed through the VIEW rather than straight
        to :meth:`request_close`, so a close can never skip the editor's apply
        path - the view calls back here through ``on_exit`` once it may."""
        self.schedule_call(self.view.request_close)

    def svc_select(self, key: str) -> None:
        self.schedule_call(self.view.svc_select, key)

    def svc_form(self, fields: dict[str, Any]) -> None:
        self.schedule_call(self.view.svc_form, fields)

    def svc_detection(self, state: dict[str, Any]) -> None:
        self.schedule_call(self.view.svc_detection, state)

    def svc_edit_by_lines(self, on: bool) -> None:
        self.schedule_call(self.view.svc_edit_by_lines, on)

    def svc_after_delivery(self, state: dict[str, Any]) -> None:
        self.schedule_call(self.view.svc_after_delivery, state)

    def svc_scroll(self, action: str) -> None:
        self.schedule_call(self.view.svc_scroll, action)

    def svc_matcher(self, matcher: str) -> None:
        self.schedule_call(self.view.svc_matcher, matcher)

    def svc_tolerance(self, value: int) -> None:
        self.schedule_call(self.view.svc_tolerance, value)

    def svc_add(self) -> None:
        self.schedule_call(self.view.svc_add)

    def svc_reset(self) -> None:
        self.schedule_call(self.view.svc_reset)

    def svc_delete(self) -> None:
        self.schedule_call(self.view.svc_delete)

    def svc_prev(self, kind: str) -> None:
        self.schedule_call(self.view.svc_prev, kind)

    def svc_next(self, kind: str) -> None:
        self.schedule_call(self.view.svc_next, kind)

    def svc_capture(self, kind: str) -> None:
        self.schedule_call(self.view.svc_capture, kind)

    def svc_clear(self, kind: str) -> None:
        self.schedule_call(self.view.svc_clear, kind)

    def svc_click_point(self, kind: str, x: int, y: int) -> None:
        self.schedule_call(self.view.svc_click_point, kind, x, y)

    def svc_forget(self) -> None:
        self.schedule_call(self.view.svc_forget)

    def svc_close(self) -> None:
        self.schedule_call(self.view.svc_close)


@contextmanager
def asset_dir() -> Iterator[Path]:
    """The packaged ``assets/`` directory as a real path, for as long as it is used.

    ``importlib.resources`` rather than ``__file__``, exactly as
    ``shell.py:asset_dir`` does it and for the same reason: from source and
    inside the PyInstaller extraction the assets ARE files on disk and this
    yields them where they lie, while an install that ever ends up zipped gets a
    materialized copy that lives exactly as long as the ``with`` block.
    """
    with as_file(files(ASSET_PACKAGE).joinpath(ASSET_DIR)) as path:
        yield Path(path)


def entry_url(assets: Path) -> str:
    """The ``file://`` URL for this window's page, carrying the app version.

    A ``file://`` URL, not a bare path: pywebview spins up a local Bottle HTTP
    server for a plain local path and this window has nothing to serve.
    """
    return f"{(assets / ENTRY_PAGE).as_uri()}#v={__version__}"


def build_monitor(
    config: Config,
    *,
    profile_root: Path | None = None,
    provider: ClipboardProvider | None = None,
) -> LocalUIMonitor:
    """The monitor a standalone calibration window runs over.

    A ``LocalUIMonitor`` and never anything else (§6.4): every surface in this
    window is made of pixels, and pixels do not cross the wire. The clipboard is
    handed in because the constructor takes one, not because this window watches
    it - nothing here ever calls ``watch_clipboard``.

    ``profile_for`` is a plain disk read rather than a cache: this process has
    no chat shell to share one with, and the editor writes captures straight to
    the store, so a cache here would be a way to poll against appearances the
    user just replaced.
    """
    root = profile_root if profile_root is not None else default_profile_dir()
    return LocalUIMonitor(
        profile_for=lambda key: load_profile(root, key),
        clipboard=provider,
        clip_poll_interval_ms=config.clipboard.poll_interval_ms,
    )


def open_calibration_window(
    webview: Any,
    runner: CalibrationRunner,
    *,
    width: int | None = None,
    height: int | None = None,
) -> Any:
    """Create the window for ``runner`` and wire it. Does NOT start a pump.

    The door for a shell that is already pumping (phase 4B's titlebar button):
    pywebview shows a window created after ``start()`` as soon as the pump gets
    round to it, and ``webview.start()`` may only ever be called once per
    process. Standalone, :func:`run_calibration` calls this and then takes the
    main thread.
    """
    with asset_dir() as assets:
        window = webview.create_window(
            WINDOW_TITLE,
            url=entry_url(assets),
            width=width if width is not None else WINDOW_SIZE[0],
            height=height if height is not None else WINDOW_SIZE[1],
            min_size=MIN_WINDOW_SIZE,
            background_color=WINDOW_BACKGROUND,
            text_select=WINDOW_TEXT_SELECT,
            js_api=runner.js_api,
        )
        runner.attach(window.evaluate_js, on_close=window.destroy)
        window.events.loaded += runner.page_loaded
        return window


def run_calibration(
    config: Config,
    *,
    profile_root: Path | None = None,
    global_config_path: Path | None = None,
    monitor: CalibrationMonitor | None = None,
    provider: ClipboardProvider | None = None,
    on_config_change: Callable[[Config], None] | None = None,
    on_calibration: Callable[[AgentSlot, ScreenRegion | None], None] | None = None,
) -> int:
    """``agentclip --calibrate``: open the calibration window and run it.

    Standalone by construction - there is no engine, no session, no transcript
    and no clipboard watcher anywhere below this call. What it needs is a
    ``Config`` (which services exist, and how each is recognised) and a place to
    read and write captured appearances; ``monitor`` is injectable so a suite can
    drive the whole window over ``FakeUIMonitor``, and defaults to a real
    ``LocalUIMonitor`` because a calibration window nobody configured is still a
    window watching this machine's screen.

    Order is ``run_gui``'s and is the design's: the window is created with the
    ``js_api`` object first (pywebview injects the API at load), the bridge is
    pointed at its ``evaluate_js`` second, the loop thread starts third - before
    the pump takes the main thread for good - and the surface is built on the
    page's ``loaded`` event, so the first thing painted is the editor rather than
    a frame of nothing.
    """
    try:
        import webview
    except ImportError:
        print(MISSING_PYWEBVIEW, file=sys.stderr)
        return 2

    runner = CalibrationRunner(
        config=config,
        monitor=(
            monitor
            if monitor is not None
            else build_monitor(config, profile_root=profile_root, provider=provider)
        ),
        profile_root=profile_root,
        global_config_path=global_config_path,
        on_config_change=on_config_change,
        on_calibration=on_calibration,
    )
    try:
        open_calibration_window(webview, runner)
        runner.start()
        # Blocks until the last window is closed. The teardown hangs off this
        # RETURNING rather than off the window's ``closing`` event, for the chat
        # shell's reason: ``closing`` runs on the window's own thread and the
        # bridge's drainer parks inside ``evaluate_js`` waiting on that very
        # thread.
        webview.start()
    except Exception as exc:  # pywebview's WebViewException and friends
        print(f"agentclip: the calibration window could not start: {exc}", file=sys.stderr)
        return 2
    finally:
        runner.stop()
    return 0


def _no_close() -> None:
    """The close a runner with no window behind it gets (tests)."""


__all__: Sequence[str] = [
    "ASSET_DIR",
    "ASSET_NAMES",
    "ASSET_PACKAGE",
    "ENTRY_PAGE",
    "MIN_WINDOW_SIZE",
    "MISSING_PYWEBVIEW",
    "SHUTDOWN_TIMEOUT_S",
    "WINDOW_BACKGROUND",
    "WINDOW_SIZE",
    "WINDOW_TEXT_SELECT",
    "WINDOW_TITLE",
    "CalibrationBridge",
    "CalibrationCalls",
    "CalibrationJsApi",
    "CalibrationRunner",
    "asset_dir",
    "build_monitor",
    "entry_url",
    "open_calibration_window",
    "run_calibration",
]
