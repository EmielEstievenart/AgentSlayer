# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for the MONITOR half - ``agentclip-monitor``.

Build via ``scripts/build-exe.ps1 -MonitorOnly`` (Windows) or
``scripts/build-exe.sh --monitor-only`` (Linux/macOS) - both also build it as
part of a plain, everything run - or by hand from the repo root::

    uv run --group build pyinstaller --noconfirm packaging/agentclip-monitor.spec

The build environment must have the ``cv`` AND ``gui`` extras installed - the
matcher backend for the reason the next paragraph gives, pywebview because this
binary opens the Monitor UI since ui-monitor.md 9.1. The scripts sync both and
refuse to build without either.

This is the binary that runs on the machine whose SCREEN shows the chat
(docs/design/ui-monitor.md 2.5, 6.5): a VM on a host-only network, or this PC in
split mode. It serves that machine's pixels, mouse, keyboard and clipboard to a
brain over the JSON-lines monitor wire, and it is a STANDING process - it keeps
polling whether or not a brain is attached (2.8).

It is the mirror image of ``packaging/agentclip-engine.spec``: same one-artifact
deployment story, opposite half of the app. Where the engine binary carries the
executor and no driver, this one carries the driver and no engine.

What it MUST carry, and why each name is here rather than found:

* **cv2/numpy** - the OpenCV matcher backend (driver/screen/matchers.py). Reached
  through a lazy, try/except-guarded import so a from-source install without the
  ``cv`` extra keeps working, which means static analysis sees nothing and a
  missed collection would not error: a service configured for the exhaustive
  sweep would silently get the anchor search instead, on the one machine where
  the matching actually happens. Exactly the main spec's argument, and it
  matters MORE here, because this binary is where every template search runs.
* **copykitten/pyperclip** - the clipboard backends, same lazy shape. The
  clipboard is a monitor resource (2.11): the watcher polls it here and the
  brain only ever sees the text.
* **pywebview and its platform backend** - the Monitor UI (ui-monitor.md 9.1).
  This binary IS the window now: ``agentclip-monitor`` opens the service editor,
  the ELEMENTS column, the region picker and the Serve panel, and only
  ``--headless`` skips it. Named per-platform for the main spec's reason -
  webview/guilib.py picks a backend off ``platform.system()`` and imports it BY
  NAME at import time, so nothing static reaches it - and the block below is
  ``packaging/agentclip.spec``'s, deliberately identical, because the two
  binaries open the same kind of window on the same kinds of machine.
* **tkinter** - the ``--pick-region`` overlay (driver/screen/overlay.py) imports
  it inside a function. Kept, where the engine spec drops it, because the region
  a user drags is drawn on THIS machine. No ``datas`` are needed for it:
  PyInstaller ships a tkinter hook that bundles Tcl/Tk (and, on Linux, the
  library trees they read at run time) once the module is REACHABLE - which is
  the only half a function-body import makes fragile, and the half named above.
  The hook can only collect a tkinter that EXISTS, though, which is why
  ``scripts/build-exe.sh`` checks ``import tkinter`` before building this spec
  and names the distro package if it is missing.

The frozen binary answers two questions about all of that without opening a
socket, and both build scripts ask them of the artifact they just produced:
``--version`` walks the whole module-level import tree, and ``--list-matchers``
imports each matcher backend for real and prints "NOT AVAILABLE" for any that
did not load - the main binary's check, one machine over, where it matters more
(see driver/monitor/__main__.py's ``_list_matchers``).

What it must NOT carry is the other three quarters of the app. The monitor
package may import only ``agentclip``, ``agentclip.config``,
``agentclip.driver.clip``, ``agentclip.driver.monitor`` and
``agentclip.driver.screen`` (tests/test_layering.py), so nothing below reaches a
shell, the engine or the executor - and the ``excludes`` list is that rule said
to PyInstaller, so the day a stray import appears it is a fat binary and a spec
conflict rather than a VM that needs a browser engine to start polling.

**The window ships here now** (ui-monitor.md 9.1), which is the one thing this
spec used to say the opposite of. The Monitor UI runs where the pixels are, and
that is this binary's machine - so ``agentclip-monitor`` carries
``agentclip.shell.monitor_ui`` (its own pywebview window, its own bridge, its own
asset bundle) and the ``gui`` extra along with ``cv``. The entry script is that
package's ``__main__.py``: a dispatcher that opens the window, or delegates to
``driver/monitor/__main__.py`` verbatim under ``--headless``.

What is still excluded is everything else a shell could drag in: this binary has
no session, no engine, no transcript and no Chat UI. ``agentclip.shell.chat`` and
``agentclip.shell.app`` are unreachable from ``shell.monitor_ui`` by the layering
test's rule, and the excludes below are that rule said to PyInstaller.

**``--headless`` still imports no toolkit**, which is what keeps it honest on a
server with no desktop: the shell entry point reaches ``webview`` only inside the
function that creates a window, and the delegation happens before that.
"""

import os
import sys

# SPECPATH is injected by PyInstaller. Relative paths in Analysis() resolve
# against the invoking CWD, which would make the build directory-sensitive;
# anchor everything to the repo root instead.
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

# The MONITOR UI's page. ``shell/monitor_ui/window.py`` resolves it through
# ``files("agentclip.shell.monitor_ui") / "assets"`` and never ``__file__``, and
# PyInstaller's FrozenImporter answers that by looking under sys._MEIPASS at
# exactly this layout - so the destination has to be the PACKAGE's relative path
# or a frozen build opens a window on nothing. Globbed rather than listed, for
# ``packaging/agentclip.spec``'s reason: webview/assets.py's ASSET_NAMES is the
# contract for what must be there, and a second copy of that list here would be a
# fourth asset away from silently shipping three.
#
# ``shell/webview`` needs none of its own: it is the plumbing both windows are
# made of (the bridge, the service editor's model, the asset RESOLUTION), and
# every file it resolves belongs to the package that owns the window.
MONITOR_UI_ASSETS = os.path.join(SRC, "agentclip", "shell", "monitor_ui", "assets")
page_datas = [
    (os.path.join(MONITOR_UI_ASSETS, name), "agentclip/shell/monitor_ui/assets")
    for name in sorted(os.listdir(MONITOR_UI_ASSETS))
    if os.path.isfile(os.path.join(MONITOR_UI_ASSETS, name))
]

# pywebview's backend, named per PLATFORM. Lifted from ``packaging/agentclip.spec``
# unchanged and for its reasons: webview/guilib.py's ``initialize()`` picks the
# backend off ``platform.system()`` and imports it by name, so the module the
# frozen binary needs is decided by the OS the build ran on - and where guilib
# tries two backends in order, BOTH are named, because the pick belongs to the
# machine the exe lands on rather than to the one it was frozen on.
if sys.platform == "win32":
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
    webview_platforms = ["webview.platforms.cocoa"]
    webview_runtime = []
else:
    webview_platforms = ["webview.platforms.gtk", "webview.platforms.qt"]
    webview_runtime = []

hiddenimports = [
    # The OpenCV matcher backend. Collection itself needs no help (PyInstaller
    # ships hook-numpy, hooks-contrib ships hook-cv2, and both pull their binary
    # extensions once the module is reachable) - naming them is about
    # REACHABILITY, which is the half a function-body import makes fragile.
    "cv2",
    "numpy",
    # The clipboard backends, both lazily imported behind try/except in
    # driver/clip/base.py.
    "copykitten",
    "pyperclip",
    # The region picker's toolkit, imported inside a function so a tk-less
    # machine can still run the rest.
    "tkinter",
    # The interface list behind the Serve panel's "dial me here" addresses
    # (driver/monitor/interfaces.py). Same lazy, function-body import as
    # everything above, and the same consequence if it were missed: a monitor
    # that runs but cannot tell the operator which address to type.
    "psutil",
    # The Monitor UI's toolkit. `webview` itself is unconditional (the package
    # is the same everywhere and shell/monitor_ui/window.py reaches it inside
    # the function that creates the window); the backend and its runtime come
    # from the per-platform block above. Collection below that needs no help:
    # pywebview and pythonnet each ship a PyInstaller hook through the
    # `pyinstaller40` entry point, and pyinstaller-hooks-contrib adds hook-clr
    # and hook-clr_loader - so the WebView2 interop DLLs, webview/js/,
    # Python.Runtime.dll and clr_loader's ffi DLLs all ride along once the
    # modules are REACHABLE, which is the only half a lazy import makes fragile.
    "webview",
    *webview_platforms,
    *webview_runtime,
]

# The X11 backend (driver/screen/x11.py) - capture, XTest input and EWMH focus
# on Linux. Same lazy shape as everything above, and the same consequence if it
# were missed: a monitor binary that starts, polls, and can neither see the
# screen nor click on it. Guarded by platform because python-xlib is a
# Linux-only dependency (pyproject's marker) and naming it on a Windows build
# box would only produce a warning. `collect_submodules` rather than the bare
# package: x11.py reaches Xlib.display, Xlib.X, Xlib.XK, Xlib.ext.xtest and
# Xlib.protocol.event, and Xlib/__init__.py imports none of them.
if sys.platform.startswith("linux"):
    from PyInstaller.utils.hooks import collect_submodules

    hiddenimports += collect_submodules("Xlib")

excludes = [
    # SHELL: the UI and its tree. `textual` and `textual_image` were named here
    # too - hard package dependencies, which is exactly why a binary that never
    # opens a window had to say so - until docs/design/ui-monitor.md 6.6 deleted
    # the shell and dropped both dependencies, which is a stronger version of the
    # same guarantee.
    "pygments",
    # `webview`, `pythonnet`, `clr` and `clr_loader` were HERE until 9.1, with a
    # note saying the Monitor UI would be a later phase's addition rather than a
    # silent one. This is that phase: they are hiddenimports above now, and the
    # excludes below are what still is not in this binary.
    # ENGINE / EXECUTOR: the whole other half of the app. The monitor runs no
    # session, spawns no tool and speaks to no model, so none of this is
    # reachable - the layering test says so, and excluding it makes that
    # mechanical.
    "mcp",
    "mcp_types",
    "anyio",
    "pydantic",
    "httpx2",
    "uvicorn",
    "starlette",
    # The monitor never SSHes anywhere: the brain dials IT.
    "paramiko",
    # The dev group, same shared .venv, same reasoning as the other two specs.
    "aiohttp",
    "aiohttp_jinja2",
    "jinja2",
    "click",
    "msgpack",
    "pytest",
    "_pytest",
    "mypy",
]

a = Analysis(
    # The console script's module, reached as a file. `[project.scripts]` names
    # `agentclip.shell.monitor_ui.__main__:main` and this is that module - its
    # `if __name__ == "__main__"` guard fires because PyInstaller runs an entry
    # script exactly as `__main__`, so the frozen binary and `python -m
    # agentclip.shell.monitor_ui` are the same door. It is the SHELL's entry
    # since 9.1 rather than the Driver's: the binary opens a window, and
    # `--headless` is one delegation away inside it.
    [os.path.join(SRC, "agentclip", "shell", "monitor_ui", "__main__.py")],
    pathex=[SRC],
    binaries=[],
    # The Monitor UI's page, and nothing else: the rest of the tree is
    # freeze-friendly by design (architecture.md 7), and a browser engine
    # loading a page over a file:// URL is the one deliberate exception - which
    # is now on THIS side of the layering line.
    datas=page_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# OpenCV's hook collects the whole wheel, including the 29 MB FFmpeg video-I/O
# plugin. AgentClip decodes no video: the matcher calls exactly ``matchTemplate``
# and ``dilate`` (driver/screen/matchers.py), both core imgproc, and the plugin is
# loaded lazily the first time something opens a VideoCapture - which nothing
# here ever does. Dropped for the main spec's reason, and it is a third of the
# cost of bundling OpenCV at all.
a.binaries = [entry for entry in a.binaries if "opencv_videoio_ffmpeg" not in entry[0]]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    # The name a launcher starts on the monitor machine, unchanged from the
    # console script. A binary copied onto a PATH under this name is
    # indistinguishable from an installed one.
    name="agentclip-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX trips AV heuristics and has a history of corrupting Windows DLLs.
    upx=False,
    runtime_tmpdir=None,
    # Kept console even though this binary now opens a window, because
    # `--headless` is the door a VM with no desktop uses: the operator starts it
    # in a terminal, reads the "listening on ..." line and the token off stderr,
    # and ends it with Ctrl+C - and a windowed build detaches exactly those. The
    # cost is a console behind the Monitor UI on Windows, which is a window an
    # operator standing at a VM can live with; losing the headless door is not.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
