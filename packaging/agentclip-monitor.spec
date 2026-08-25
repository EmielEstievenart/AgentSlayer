# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for the MONITOR half - ``agentclip-monitor``.

Build via ``scripts/build-exe.ps1 -MonitorOnly`` (Windows) or
``scripts/build-exe.sh --monitor-only`` (Linux/macOS) - both also build it as
part of a plain, everything run - or by hand from the repo root::

    uv run --group build pyinstaller --noconfirm packaging/agentclip-monitor.spec

The build environment must have the ``cv`` extra installed, for the reason the
next paragraph gives; the scripts sync it and refuse to build without it.

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

**No window UI here, deliberately.** The Monitor UI (6.4) does run where the
pixels are, but it is a SHELL package (``agentclip.shell.monitor_ui``, with
its own pywebview window and its own asset bundle) and this binary may not
import a shell at all. It ships in the app binary instead, which is what
``agentclip --calibrate`` opens on the monitor machine - so ``webview`` and its
runtime stay excluded here rather than half-collected, and ``agentclip-monitor``
has no ``--calibrate`` of its own (driver/monitor/__main__.py). That is also why
this spec wants only the ``cv`` extra where the main one wants ``cv`` and
``gui``.
"""

import os
import sys

# SPECPATH is injected by PyInstaller. Relative paths in Analysis() resolve
# against the invoking CWD, which would make the build directory-sensitive;
# anchor everything to the repo root instead.
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

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
    # The window UI's stack. See the module docstring: the Monitor UI is
    # a later phase's addition to this binary, not a silent one.
    "webview",
    "pythonnet",
    "clr",
    "clr_loader",
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
    # `agentclip.driver.monitor.__main__:main` and this is that module - its
    # `if __name__ == "__main__"` guard fires because PyInstaller runs an entry
    # script exactly as `__main__`, so the frozen binary and `python -m
    # agentclip.driver.monitor` are the same door.
    [os.path.join(SRC, "agentclip", "driver", "monitor", "__main__.py")],
    pathex=[SRC],
    binaries=[],
    # No datas of our own. The package is freeze-friendly by design
    # (architecture.md 7), and the one exception in the tree - the GUI shells'
    # page assets - is on the other side of the layering line and excluded above.
    datas=[],
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
    # Not a TUI - there is no UI at all. It is that the operator starts this in
    # a terminal, reads the "listening on ..." line and ends it with Ctrl+C, and
    # a windowed build detaches exactly those.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
