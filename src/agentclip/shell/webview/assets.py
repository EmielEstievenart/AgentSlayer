"""Where a window's packaged page lives, and the URL that opens it.

One parameterised pair for both windows: the Chat UI's assets sit under
``agentclip/shell/chat/assets/`` and the Monitor UI's under
``agentclip/shell/monitor_ui/assets/``, and the only thing that differs between
resolving them is the package name - so each window names its own
``ASSET_PACKAGE`` and calls in here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

from agentclip import __version__

ASSET_DIR = "assets"
ENTRY_PAGE = "index.html"
# What every window's page is made of, and what tests check is really shipped.
ASSET_NAMES = (ENTRY_PAGE, "app.css", "app.js")


@contextmanager
def package_asset_dir(package: str, subdir: str = ASSET_DIR) -> Iterator[Path]:
    """``package``'s ``assets/`` directory as a real path, for as long as it is used.

    ``importlib.resources`` rather than ``__file__``: from source and inside the
    PyInstaller extraction the assets ARE files on disk and this yields them
    where they lie, while an install that ever ends up zipped gets a materialized
    copy that lives exactly as long as the ``with`` block - which is why a window
    loop runs inside it.
    """
    with as_file(files(package).joinpath(subdir)) as path:
        yield Path(path)


def page_url(assets: Path, entry_page: str = ENTRY_PAGE) -> str:
    """The ``file://`` URL for a window's page, carrying the app version.

    A ``file://`` URL, not a bare path: pywebview spins up a local Bottle HTTP
    server for a plain local path, and neither window has anything to serve -
    each page is three files next to each other. The cost of that choice is that
    the file:// origin cannot load ES modules (Chromium blocks module scripts
    from an opaque origin), so ``app.js`` is a classic script; see gui.md
    section 2.

    The version rides in the fragment because a hard-coded string in the HTML
    would drift the first time ``__version__`` moves.
    """
    return f"{(assets / entry_page).as_uri()}#v={__version__}"
