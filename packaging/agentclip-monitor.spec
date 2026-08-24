# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for the MONITOR half - ``agentclip-monitor``.

Build by hand from the repo root::

    uv run --group build --extra cv pyinstaller --noconfirm packaging/agentclip-monitor.spec

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
  a user drags is drawn on THIS machine.

What it must NOT carry is the other three quarters of the app. The monitor
package may import only ``agentclip``, ``agentclip.config``,
``agentclip.driver.clip``, ``agentclip.driver.monitor`` and
``agentclip.driver.screen`` (tests/test_layering.py), so nothing below reaches a
shell, the engine or the executor - and the ``excludes`` list is that rule said
to PyInstaller, so the day a stray import appears it is a fat binary and a spec
conflict rather than a VM that needs a browser engine to start polling.

**No GUI here, deliberately.** The calibration window (6.4) is pywebview and is
part of what a monitor machine eventually hosts, but it is a separate window
with its own asset bundle; wiring it into this binary is 6.4's business, and
until it is, ``webview`` stays excluded rather than half-collected.
"""

import os

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
]

excludes = [
    # SHELL: both UIs and their trees. `textual` and `pillow`/`textual_image`
    # are hard package dependencies, which is exactly why they have to be named:
    # a full install on the monitor machine drags them and a BINARY has no
    # excuse to.
    "textual",
    "textual_image",
    "pygments",
    # The GUI shell's stack. See the module docstring: the calibration window is
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
