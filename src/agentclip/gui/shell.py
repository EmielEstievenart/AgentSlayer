"""Open the GUI window and run its loop until the user closes it.

Slice 2 of the GUI shell (docs/design/gui.md section 3): the window is wired to
the real controllers. What lives here is only the pywebview-shaped part of that
- create the window, hand it the ``js_api`` object, point the bridge at its
``evaluate_js``, run the native pump on the main thread, and tear everything
down when it returns. The concurrency model behind it (one asyncio loop on its
own thread) is :mod:`agentclip.gui.runner`; the ports it drives are
:mod:`agentclip.gui.view`.

Everything pywebview is imported INSIDE functions: the ``gui`` extra is
optional, so importing this module must stay free for a TUI launch and must not
fail when pywebview is absent.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Protocol

from agentclip import __version__
from agentclip.app.types import EngineRequest
from agentclip.clip.base import ClipboardProvider
from agentclip.config import Config
from agentclip.engine.engine import Engine
from agentclip.gui.remote import RemoteConnect
from agentclip.gui.runner import GuiRunner
from agentclip.gui.view import McpStatusSource

WINDOW_TITLE = "AgentClip"
WINDOW_SIZE = (1200, 800)
MIN_WINDOW_SIZE = (900, 600)
# The page paints its own background; matching it here stops the white flash
# WebView2 would otherwise show between window creation and first paint.
WINDOW_BACKGROUND = "#14161a"

WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"

# The two ways this shell can fail before it has a window to complain in, said
# in the same shape as the other optional extras (mcp.client.MISSING_SDK_HINT,
# service_editor.OPENCV_MISSING_SOURCE): what is missing, then the one command
# that fixes it.
MISSING_PYWEBVIEW = (
    "agentclip: the gui extra is not installed - run: uv sync --extra gui"
    " (or: pip install 'agentclip[gui]')"
)
MISSING_WEBVIEW2 = (
    "agentclip: this Windows install has no Microsoft Edge WebView2 runtime, which the"
    f" GUI shell renders in. Install the Evergreen runtime from {WEBVIEW2_DOWNLOAD}"
    " (or use the TUI: run agentclip without --gui)."
)

# The asset files the window needs; also what tests/gui checks is really
# shipped, and what --gui-smoke reads back out of a frozen build. They live in
# agentclip/gui/assets/ as package data - hatchling puts every file under
# src/agentclip in the wheel, but PyInstaller collects only what a spec names,
# so packaging/agentclip.spec adds this directory to `datas` AT THE
# PACKAGE-RELATIVE PATH, which is what makes `asset_dir` below resolve inside
# the onefile extraction (docs/design/gui.md 5).
ASSET_PACKAGE = "agentclip.gui"
ASSET_DIR = "assets"
ENTRY_PAGE = "index.html"
ASSET_NAMES = (ENTRY_PAGE, "app.css", "app.js")


class LaunchLike(Protocol):
    """What a UI shell reads off :class:`agentclip.cli.Launch`.

    A structural type rather than the class itself, because ``cli`` sits ABOVE
    both shells: the TUI is handed unpacked pieces for the same reason, and a
    ``from agentclip.cli import Launch`` here - even under ``TYPE_CHECKING`` -
    would point the dependency the wrong way (tests/test_layering.py).
    """

    @property
    def project_root(self) -> Path: ...

    @property
    def config(self) -> Config: ...


@contextmanager
def asset_dir() -> Iterator[Path]:
    """The packaged ``assets/`` directory as a real path, for as long as it is used.

    ``importlib.resources`` rather than ``__file__``: from source and inside the
    PyInstaller extraction the assets ARE files on disk and this yields them
    where they lie, while an install that ever ends up zipped gets a materialized
    copy that lives exactly as long as the ``with`` block - which is why the
    window loop runs inside it.
    """
    with as_file(files(ASSET_PACKAGE).joinpath(ASSET_DIR)) as path:
        yield Path(path)


def entry_url(assets: Path) -> str:
    """The ``file://`` URL for the window's page, carrying the app version.

    A ``file://`` URL, not a bare path: pywebview spins up a local Bottle HTTP
    server for a plain local path, and this shell has nothing to serve - the
    page is three files next to each other. The cost of that choice is that the
    file:// origin cannot load ES modules (Chromium blocks module scripts from
    an opaque origin), so ``app.js`` is a classic script; see gui.md section 2.

    The version rides in the fragment because there is no bridge yet and a
    hard-coded string in the HTML would drift the first time __version__ moves.
    """
    return f"{(assets / ENTRY_PAGE).as_uri()}#v={__version__}"


def webview2_missing() -> bool:
    """True when this Windows box would render the GUI in the deprecated IE engine.

    pywebview does not raise when the WebView2 runtime is absent - it silently
    falls back to MSHTML (``webview/platforms/winforms.py`` picks ``renderer``
    at import time off the EdgeUpdate registry keys), which would render this
    page as a broken 2013 web page rather than fail. So the check IS reading
    that verdict, before a window exists; gui.md section 2 asks for the pointer
    at the Evergreen installer, and this is what earns the right to show it.

    Anything unanswerable (not Windows, pywebview not importable, a pywebview
    that renamed the attribute) is False: never block a launch on a question
    this could not ask.
    """
    if platform.system() != "Windows":
        return False
    try:
        from webview.platforms import winforms
    except Exception:
        return False
    renderer = getattr(winforms, "renderer", "")
    return bool(renderer) and renderer != "edgechromium"


def run_gui(
    launch: LaunchLike,
    *,
    provider: ClipboardProvider,
    engine_factory: Callable[[EngineRequest], Engine],
    mcp_manager: McpStatusSource | None = None,
    on_config_change: Callable[[Config], None] | None = None,
    host: Any = None,
    remote: RemoteConnect | None = None,
) -> int:
    """Open the window, run the GUI loop, return an exit code when it closes.

    The keyword arguments are the shell-agnostic pieces ``cli.main`` builds for
    both frontends - the clipboard backend, the per-session engine factory and
    the process-wide MCP runtime. They are handed IN rather than built here for
    the reason the module docstring gives: choosing a clipboard backend and
    wiring an engine factory are launch questions, not window questions, and a
    second construction site is a second thing to drift.

    ``on_config_change`` is the way BACK for the one launch question a running
    window can answer differently: the service editor saves a new preset table,
    and the engine factory - built above this call, over a closure cli.py owns -
    has to read it for the next session. The TUI's equivalent is that its
    closure reads the attribute its editor reassigns.

    ``remote`` is the way back for the OTHER one, and the bigger of the two:
    which machine the session runs on. It carries the two command-line facts the
    connect sequence reads and the callable that turns a successful dial into a
    whole new set of the arguments above (``gui/remote.py:RemoteConnect``).
    ``None`` - which is what every caller but ``cli.main`` passes - simply means
    this window cannot go remote, and the affordance is absent rather than
    broken. ``host`` is what the session runs on TODAY, read only for the
    sidebar's link indicator.

    Order matters and is the design's (gui.md section 2). The window is created
    with the ``js_api`` object first, because pywebview injects the API into the
    page at load; the bridge is pointed at its ``evaluate_js`` second, because
    the window has to exist to have one; the loop thread starts third, before
    the native pump takes the main thread for good; and the controllers start on
    the page's ``loaded`` event, so the very first thing the page paints is the
    "describe the task" prompt rather than a frame of nothing.

    The teardown hangs off ``webview.start()`` RETURNING rather than off the
    window's ``closing`` event, deliberately: ``closing`` runs on the window's
    own thread, and the bridge's drainer parks inside ``evaluate_js`` waiting on
    that very thread - tearing down from there would make the two wait on each
    other for as long as the join allows. Once the pump has returned there is no
    such knot, and a window that closed is exactly the moment the TUI's quit
    path does its own unwinding. The ``closing`` event IS subscribed - but only
    as a GATE: it decides whether this close may happen at all (a turn in flight
    asks first) and does nothing else, which is the one thing that thread can
    safely do.
    """
    try:
        import webview
    except ImportError:
        print(MISSING_PYWEBVIEW, file=sys.stderr)
        return 2
    if webview2_missing():
        print(MISSING_WEBVIEW2, file=sys.stderr)
        return 2

    runner = GuiRunner(
        config=launch.config,
        provider=provider,
        engine_factory=engine_factory,
        project_root=launch.project_root,
        mcp_manager=mcp_manager,
        host=host,
        remote=remote,
        on_config_change=on_config_change,
    )
    width, height = WINDOW_SIZE
    with asset_dir() as assets:
        try:
            window = webview.create_window(
                WINDOW_TITLE,
                url=entry_url(assets),
                width=width,
                height=height,
                min_size=MIN_WINDOW_SIZE,
                background_color=WINDOW_BACKGROUND,
                js_api=runner.js_api,
            )
            runner.attach(window.evaluate_js, on_close=window.destroy)
            window.events.loaded += runner.page_loaded
            # Closing mid-turn asks first. This handler runs on the window's own
            # thread and RETURNS FALSE to cancel the close (pywebview's
            # ``closing`` is a locking event and a False from any handler sets
            # ``args.Cancel``); it never tears anything down from there - see
            # ``GuiRunner.window_closing``. The teardown still hangs off
            # ``webview.start()`` returning, below.
            window.events.closing += runner.window_closing
            runner.start()
            # Blocks until the last window is closed. Everything slow belongs
            # AFTER first paint (gui.md section 2), which is why the controllers
            # start on `loaded` rather than above this line.
            webview.start()
        except Exception as exc:  # pywebview's WebViewException and friends
            print(f"agentclip: the GUI shell could not start: {exc}", file=sys.stderr)
            print(f"agentclip: if this is about the web engine, see {WEBVIEW2_DOWNLOAD}",
                  file=sys.stderr)
            return 2
        finally:
            runner.stop()
    return 0
