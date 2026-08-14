"""Open the GUI window and run its loop until the user closes it.

Slice 1 of the GUI shell (docs/design/gui.md section 3): the window exists and
renders the packaged page, and nothing else. No bridge, no controllers - the
next slice puts a ``js_api`` object and an ``evaluate_js`` queue behind this
same entry point, which is why ``run_gui`` already takes the ``Launch``
``cli.main`` built rather than the pieces this slice happens to need (none).

Everything pywebview is imported INSIDE functions: the ``gui`` extra is
optional, so importing this module must stay free for a TUI launch and must not
fail when pywebview is absent.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Protocol

from agentclip import __version__
from agentclip.config import Config

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
# shipped. They live in agentclip/gui/assets/ as package data - hatchling puts
# every file under src/agentclip in the wheel, but PyInstaller does not: when
# the frozen exe grows the GUI, packaging/agentclip.spec needs this directory
# added to `datas` or the window will open on nothing (docs/design/gui.md 5).
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


def run_gui(launch: LaunchLike) -> int:
    """Open the window, run the GUI loop, return an exit code when it closes.

    ``launch`` is unused in this slice - the window has no session behind it
    yet - and is taken anyway so the wiring in ``cli.main`` is the one the next
    slice keeps.
    """
    try:
        import webview
    except ImportError:
        print(MISSING_PYWEBVIEW, file=sys.stderr)
        return 2
    if webview2_missing():
        print(MISSING_WEBVIEW2, file=sys.stderr)
        return 2

    width, height = WINDOW_SIZE
    with asset_dir() as assets:
        try:
            webview.create_window(
                WINDOW_TITLE,
                url=entry_url(assets),
                width=width,
                height=height,
                min_size=MIN_WINDOW_SIZE,
                background_color=WINDOW_BACKGROUND,
            )
            # Blocks until the last window is closed. Everything slow belongs
            # AFTER first paint (gui.md section 2), which is why nothing is
            # built above this line.
            webview.start()
        except Exception as exc:  # pywebview's WebViewException and friends
            print(f"agentclip: the GUI shell could not start: {exc}", file=sys.stderr)
            print(f"agentclip: if this is about the web engine, see {WEBVIEW2_DOWNLOAD}",
                  file=sys.stderr)
            return 2
    return 0
