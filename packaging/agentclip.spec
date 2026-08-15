# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for AgentClip.

Build via ``scripts/build-exe.ps1``, or by hand from the repo root::

    uv run --group build pyinstaller --noconfirm packaging/agentclip.spec

The app itself is deliberately freeze-friendly (architecture.md S7): the Textual
CSS lives in a class var, the protocol templates are string constants, and
nothing resolves paths relative to ``__file__``. The GUI shell's page is the one
exception and a deliberate one - three hand-written files that a browser engine
loads over a ``file://`` URL cannot be string constants - so it is the only
``datas`` of our own here (see ``gui_datas`` below). Everything else exists to
work around *dependency* dynamism that PyInstaller's static analysis cannot see.

The build environment must have the ``cv`` AND ``gui`` extras installed: the exe
bundles the OpenCV matcher backend and both UI shells, and PyInstaller can only
collect a package that is there. ``scripts/build-exe.ps1`` syncs both and then
refuses to build without either, because both failures are otherwise silent - an
exe missing cv2 starts, runs, and quietly gives every service the anchor search,
and an exe missing pywebview starts, runs, and answers ``--gui`` with an
"install the gui extra" line a frozen user cannot act on.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller. Relative paths in Analysis() resolve
# against the invoking CWD, which would make the build directory-sensitive;
# anchor everything to the repo root instead.
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

# textual.widgets lazy-loads through a module-level __getattr__, so the static
# analysis finds none of the widgets we actually use (Collapsible, Markdown,
# Select, TextArea, Button, Input, Footer, ...).
textual_datas, textual_binaries, textual_hidden = collect_all("textual")

# tui/widgets/action_panel.py calls Syntax.guess_lexer(), a fully dynamic
# pygments lookup. Without these the plain ``diff`` highlight keeps working
# while any extension-recognised file preview blows up - a half-broken build.
pygments_hidden = (
    collect_submodules("pygments.lexers")
    + collect_submodules("pygments.styles")
    + collect_submodules("pygments.formatters")
)

# The ELEMENTS column reaches textual-image only through lazy, guarded imports
# (tui/graphics.py: probe_terminal and sixel_image_class both import inside a
# try, so the whole app can run without the package). PyInstaller's static
# analysis therefore sees nothing, and a missed collection would not error -
# the probe would just answer "no sixel" and the frozen exe would silently draw
# half blocks on a terminal that can do better, which is the exact failure the
# probe exists to end. Named explicitly so it cannot happen.
#
# Pillow needs no help here: PyInstaller ships a hook for it, and the platform
# plugins it drags in are the hook's business, not ours.
#
# Its bundled demo app is dropped: it is a `python -m textual_image` playground
# that pulls click (which `excludes` below deliberately keeps out) and nothing
# in AgentClip imports it.
textual_image_hidden = [
    name
    for name in collect_submodules("textual_image")
    if not name.startswith("textual_image.demo") and name != "textual_image.__main__"
]

# The GUI shell's page (docs/design/gui.md S2 and S5). These three files are
# package data under src/agentclip/gui/assets - hatchling puts everything below
# src/agentclip into the wheel, but PyInstaller collects only what a spec names,
# so without this the frozen `--gui` opens a window on nothing.
#
# The destination keeps the PACKAGE-RELATIVE path, which is the whole point:
# gui/shell.py resolves the directory with ``importlib.resources``
# (``files("agentclip.gui") / "assets"``), never ``__file__``, and PyInstaller's
# FrozenImporter answers that by looking under sys._MEIPASS at exactly this
# layout. Copy them to the bundle root instead and the exe would carry the page
# and still fail to find it - which is why `--gui-smoke` READS all three out of
# the frozen build rather than trusting that they were collected.
#
# Globbed rather than listed: shell.py's ASSET_NAMES is the contract for what
# must be there, and a spec holding a second copy of that list is a fourth asset
# away from silently shipping three.
GUI_ASSETS = os.path.join(SRC, "agentclip", "gui", "assets")
gui_datas = [
    (os.path.join(GUI_ASSETS, name), "agentclip/gui/assets")
    for name in sorted(os.listdir(GUI_ASSETS))
    if os.path.isfile(os.path.join(GUI_ASSETS, name))
]

hiddenimports = (
    textual_hidden
    + pygments_hidden
    + textual_image_hidden
    # copykitten is imported lazily inside a function and guarded by
    # try/except, so a missed collection would not error - it would silently
    # fall back to pyperclip and lose the Windows sequence-number polling.
    + ["copykitten", "pyperclip"]
    # The --pick-region overlay imports tkinter lazily (inside a function) so a
    # tk-less Linux still runs the rest of the app; name it explicitly so the
    # frozen build can never silently lose the picker.
    + ["tkinter"]
    # The OpenCV matcher backend (screen/matchers.py, tui.md S3.4g). Same lazy,
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
    # cli.py imports agentclip.gui.shell only under --gui, and that module
    # imports `webview` inside its functions, so nothing static reaches any of
    # this. And pywebview itself is dynamic below that line - webview/guilib.py
    # picks a backend per platform at import time, so ``webview.platforms.
    # winforms`` (which is the Windows one, and which then chooses between
    # edgechromium and mshtml by reading the EdgeUpdate registry keys AT IMPORT)
    # is reachable only by name. Both renderers are named because that choice
    # belongs to the USER'S machine, not to the build box: an exe frozen here,
    # where WebView2 is present, must still land on a Windows install where it
    # is not - and gui/shell.py's webview2_missing() check reads winforms'
    # verdict, so it has to be able to import that module either way.
    #
    # Collection below that needs no help: pywebview and pythonnet each ship a
    # PyInstaller hook through the `pyinstaller40` entry point (webview/
    # __pyinstaller/, pythonnet/_pyinstaller/) which PyInstaller discovers
    # automatically, and pyinstaller-hooks-contrib adds hook-clr and
    # hook-clr_loader. Between them the WebView2 interop DLLs (webview/lib/),
    # webview/js/, Python.Runtime.dll and clr_loader's ffi DLLs all ride along
    # once the modules are REACHABLE - which is, once more, the only part a
    # lazy import makes fragile. `clr` is named anyway because it is a two-line
    # shim module rather than a package and it is what pulls pythonnet in.
    + [
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "webview.platforms.mshtml",
        "clr",
        "pythonnet",
        "clr_loader",
    ]
)

# The dev group shares the same .venv. None of this is reachable from our
# imports, but excluding it explicitly guarantees textual-dev's web-serving
# tree can never be dragged in.
excludes = [
    "textual_dev",
    "textual_serve",
    "aiohttp",
    "aiohttp_jinja2",
    "jinja2",
    "click",
    "msgpack",
    "pytest",
    "_pytest",
    "mypy",
    # NB: tkinter is NOT excluded - the --pick-region overlay (screen/overlay.py)
    # needs it, and PyInstaller's tkinter hook bundles Tcl/Tk once it's reachable.
]

a = Analysis(
    [os.path.join(SRC, "agentclip", "__main__.py")],
    pathex=[SRC],
    binaries=textual_binaries,
    datas=textual_datas + gui_datas,
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
