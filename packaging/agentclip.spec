# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for AgentClip.

Build via ``scripts/build-exe.ps1``, or by hand from the repo root::

    uv run --group build pyinstaller --noconfirm packaging/agentclip.spec

The app itself is deliberately freeze-friendly (architecture.md S7): third-party
assets live in Python source, the protocol templates are string constants, and
nothing resolves paths relative to ``__file__``. Two things are deliberate
exceptions and are the only ``datas`` of our own here: the GUI shell's page
(three hand-written files that a browser engine loads over a ``file://`` URL
cannot be string constants - see ``gui_datas``) and the user guide the GUI's
"docs" button shows (``doc_datas``: the markdown IS the source of truth and is
read, never compiled in). Everything else exists to work around *dependency*
dynamism that PyInstaller's static analysis cannot see.

The build environment must have the ``cv`` AND ``gui`` extras installed: the exe
bundles the OpenCV matcher backend and the UI shell, and PyInstaller can only
collect a package that is there. ``scripts/build-exe.ps1`` syncs both and then
refuses to build without either, because both failures are otherwise silent - an
exe missing cv2 starts, runs, and quietly gives every service the anchor search,
and an exe missing pywebview starts, runs, and answers a launch with an
"install the gui extra" line a frozen user cannot act on.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is injected by PyInstaller. Relative paths in Analysis() resolve
# against the invoking CWD, which would make the build directory-sensitive;
# anchor everything to the repo root instead.
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

# Textual, textual-image and the pygments lexers that fed the TUI's action panel
# were all collected here until phase 6 of docs/design/ui-monitor.md deleted that
# shell. Nothing dynamic is left to chase on that side: the one remaining shell's
# page is three files this spec names below, and it reaches them by
# package-relative resource path rather than by import.

# The GUI shell's page (docs/design/gui.md S2 and S5). These three files are
# package data under src/agentclip/shell/gui/assets - hatchling puts everything
# below src/agentclip into the wheel, but PyInstaller collects only what a spec
# names, so without this the frozen `--gui` opens a window on nothing.
#
# The destination keeps the PACKAGE-RELATIVE path, which is the whole point:
# shell/gui/shell.py resolves the directory with ``importlib.resources``
# (``files("agentclip.shell.gui") / "assets"``), never ``__file__``, and
# PyInstaller's FrozenImporter answers that by looking under sys._MEIPASS at
# exactly this layout. Copy them to the bundle root instead and the exe would
# carry the page and still fail to find it - which is why `--gui-smoke` READS
# all three out of the frozen build rather than trusting they were collected.
#
# Globbed rather than listed: shell.py's ASSET_NAMES is the contract for what
# must be there, and a spec holding a second copy of that list is a fourth asset
# away from silently shipping three.
GUI_ASSETS = os.path.join(SRC, "agentclip", "shell", "gui", "assets")
gui_datas = [
    (os.path.join(GUI_ASSETS, name), "agentclip/shell/gui/assets")
    for name in sorted(os.listdir(GUI_ASSETS))
    if os.path.isfile(os.path.join(GUI_ASSETS, name))
]

# The CALIBRATION window's page (docs/design/ui-monitor.md 6.4). A SECOND bundle
# rather than more files in the one above: it is a second pywebview window with
# its own file:// URL, its own bridge and its own js_api, and
# shell/gui/calibration/window.py resolves it through
# ``files("agentclip.shell.gui.calibration") / "assets"`` - so the destination
# has to be that package's own relative path, exactly as the chat page's is.
# Without this, `agentclip --calibrate` in a frozen build opens a window on
# nothing (and neither shell would notice until somebody pressed the button).
CALIB_ASSETS = os.path.join(SRC, "agentclip", "shell", "gui", "calibration", "assets")
gui_datas += [
    (os.path.join(CALIB_ASSETS, name), "agentclip/shell/gui/calibration/assets")
    for name in sorted(os.listdir(CALIB_ASSETS))
    if os.path.isfile(os.path.join(CALIB_ASSETS, name))
]

# The USER GUIDE - docs/commands.md and docs/configuration.md, which the GUI's
# "docs" button renders (shell/gui/docs.py). The same argument as the page
# assets above, one package up, with one wrinkle of its own: these files live at
# the REPO ROOT rather than under src/, so they are the only data of ours that a
# checkout does not already carry as package data. The destination is
# `agentclip/docs` because that is where ``files("agentclip") / "docs"`` looks -
# and pyproject.toml force-includes them at exactly that path for a wheel, so
# the frozen build and the wheel answer the reader identically. Without this the
# exe opens the guide on a "this build does not carry it" note, which is a real
# failure mode rather than a crash, which is why `--gui-smoke` reads them back
# out of the frozen build beside the three assets.
#
# Globbed for the assets' reason: the docs/ subdirectories (design notes, UI
# briefs) are not files and so are not collected, and a new top-level guide page
# should not need this list edited to ship.
DOC_PAGES_DIR = os.path.join(ROOT, "docs")
doc_datas = [
    (os.path.join(DOC_PAGES_DIR, name), "agentclip/docs")
    for name in sorted(os.listdir(DOC_PAGES_DIR))
    if os.path.isfile(os.path.join(DOC_PAGES_DIR, name))
]

# pywebview's backend, named per PLATFORM (see the long note in hiddenimports
# below for why any of this has to be named at all). webview/guilib.py's
# `initialize()` picks the backend off `platform.system()` and imports it by
# name, so the module the frozen app needs is decided by the OS the build ran
# on - and a spec that hardcodes one OS's answer produces a Linux binary whose
# --gui fails with "You must have either QT or GTK with Python extensions
# installed", which is a lie about the user's machine.
#
# The choice below deliberately mirrors guilib.py rather than the build box:
# where guilib tries two backends in order, BOTH are named, for the same reason
# the Windows branch names both edgechromium and mshtml - the pick belongs to
# the machine the exe lands on, not to the one it was frozen on.
if sys.platform == "win32":
    # winforms is guilib's only Windows candidate (short of PYWEBVIEW_GUI=qt),
    # and it chooses between the two renderers below by reading the EdgeUpdate
    # registry keys AT IMPORT.
    webview_platforms = [
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "webview.platforms.mshtml",
    ]
    # pythonnet's tree, which is what winforms runs on. `clr` is named
    # separately because it is a two-line shim module rather than a package and
    # it is what pulls pythonnet in.
    webview_runtime = ["clr", "pythonnet", "clr_loader"]
elif sys.platform == "darwin":
    # guilib tries cocoa then qt on Darwin. Only cocoa is named: it is PyObjC,
    # which ships with the platform's Python and costs nothing to reach, while
    # a Qt binding is a heavyweight the mac path has never been asked for. If a
    # macOS build ever needs it, add "webview.platforms.qt" here - the failure
    # it fixes is the same "must have PyObjC or Qt" line as Linux's.
    webview_platforms = ["webview.platforms.cocoa"]
    webview_runtime = []
else:
    # Linux (and OpenBSD): guilib tries gtk then qt, in that order, unless
    # KDE_FULL_SESSION or PYWEBVIEW_GUI=qt flips it. Both are named because
    # BOTH of those inputs are read from the user's environment at run time -
    # a binary frozen with only gtk would ignore a KDE user's session and then
    # report that neither toolkit is installed.
    #
    # Naming them is again REACHABILITY only, and here it buys less than on
    # Windows: both backends bind to SYSTEM libraries through PyGObject / a Qt
    # binding, neither of which is a dependency of the `gui` extra, so on a
    # build box without them PyInstaller simply records a missing module and
    # the exe's --gui still needs those packages present on the target. That is
    # the per-distro system-dependency question docs/design/remote-executor.md
    # section 5 still lists as open; what this branch guarantees is that
    # whatever the build box HAS gets collected instead of silently skipped in
    # favour of a Windows-only list.
    webview_platforms = ["webview.platforms.gtk", "webview.platforms.qt"]
    webview_runtime = []

hiddenimports = (
    # copykitten is imported lazily inside a function and guarded by
    # try/except, so a missed collection would not error - it would silently
    # fall back to pyperclip and lose the Windows sequence-number polling.
    ["copykitten", "pyperclip"]
    # The --pick-region overlay imports tkinter lazily (inside a function) so a
    # tk-less Linux still runs the rest of the app; name it explicitly so the
    # frozen build can never silently lose the picker.
    + ["tkinter"]
    # The OpenCV matcher backend (driver/screen/matchers.py, tui.md S3.4g). Same lazy,
    # try/except-guarded shape as copykitten and for the same reason - a
    # from-source install without the `cv` extra has to keep working - and the
    # same consequence if it is missed: no error, just a service configured for
    # the exhaustive sweep silently getting the anchors, with the editor
    # blaming the build. The exe BUNDLES it (architecture.md S6), so the build
    # environment needs `uv sync --group build --extra cv`; scripts/build-exe.ps1
    # does that and fails loudly if cv2 is not importable.
    #
    # Collection itself needs no help: PyInstaller ships hook-numpy and
    # pyinstaller-hooks-contrib ships hook-cv2, and both pull their own binary
    # extensions once the module is reachable. Naming them is about
    # REACHABILITY, which is exactly what a lazy import makes fragile.
    + ["cv2", "numpy"]
    # The GUI shell (docs/design/gui.md). Same lazy shape again, twice over:
    # cli.py imports agentclip.shell.gui.shell inside the function that opens
    # the window, and that module imports `webview` inside its own functions, so
    # nothing static reaches any of this. And pywebview is itself dynamic below that line -
    # webview/guilib.py picks a backend per platform at import time, so
    # ``webview.platforms.winforms`` (the Windows one, which then chooses
    # between edgechromium and mshtml by reading the EdgeUpdate registry keys
    # AT IMPORT) is reachable only by name. Both renderers are named because
    # that choice belongs to the USER'S machine, not to the build box: an exe
    # frozen here, where WebView2 is present, must still land on a Windows
    # install where it is not - and gui/shell.py's webview2_missing() check
    # reads winforms' verdict, so it has to be able to import that module
    # either way.
    #
    # Which backend, and therefore which of those names, is decided ABOVE by
    # sys.platform - see the webview_platforms/webview_runtime block, which
    # extends the same argument to guilib.py's Linux and Darwin branches.
    # `webview` itself is unconditional: the package is the same everywhere and
    # it is what cli.py's launch path reaches for.
    #
    # Collection below that needs no help: pywebview and pythonnet each ship a
    # PyInstaller hook through the `pyinstaller40` entry point (webview/
    # __pyinstaller/, pythonnet/_pyinstaller/) which PyInstaller discovers
    # automatically, and pyinstaller-hooks-contrib adds hook-clr and
    # hook-clr_loader. Between them the WebView2 interop DLLs (webview/lib/),
    # webview/js/, Python.Runtime.dll and clr_loader's ffi DLLs all ride along
    # once the modules are REACHABLE - which is, once more, the only part a
    # lazy import makes fragile.
    + ["webview"]
    + webview_platforms
    + webview_runtime
    # The X11 backend (driver/screen/x11.py): capture, XTest input and EWMH
    # focus on Linux, which is what `agentclip --calibrate` and the in-process
    # monitor use there. Lazy inside functions like every other OS binding, so
    # nothing static reaches it; guarded by platform because python-xlib is a
    # Linux-only dependency (pyproject's marker). Submodules, not the bare
    # package: Xlib/__init__.py imports none of Xlib.display, Xlib.X, Xlib.XK,
    # Xlib.ext.xtest or Xlib.protocol.event.
    + (collect_submodules("Xlib") if sys.platform.startswith("linux") else [])
)

# The dev group shares the same .venv. None of this is reachable from our
# imports; excluding it explicitly is what keeps a test-only tree from being
# dragged into the exe by something's optional import.
excludes = [
    "click",
    "pytest",
    "_pytest",
    "mypy",
    # NB: tkinter is NOT excluded - the --pick-region overlay (driver/screen/overlay.py)
    # needs it, and PyInstaller's tkinter hook bundles Tcl/Tk once it's reachable.
]

a = Analysis(
    [os.path.join(SRC, "agentclip", "__main__.py")],
    pathex=[SRC],
    binaries=[],
    datas=gui_datas + doc_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# OpenCV's hook collects the whole wheel, including the 29 MB FFmpeg video-I/O
# plugin. AgentClip decodes no video: the matcher backend calls exactly
# ``matchTemplate`` and ``dilate`` (screen/matchers.py), both core imgproc, and
# the plugin is loaded lazily by cv2 the first time something opens a
# VideoCapture - which nothing here ever does. Verified rather than assumed:
# with this DLL removed from the environment, cv2 still imports and both calls
# return the same answers. Dropping it is a third of the cost of bundling
# OpenCV at all, so it is worth the four lines.
a.binaries = [entry for entry in a.binaries if "opencv_videoio_ffmpeg" not in entry[0]]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agentclip",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX trips AV heuristics and has a history of corrupting Windows DLLs.
    upx=False,
    runtime_tmpdir=None,
    # It is a TUI: it needs the console. Never --windowed.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
