# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for AgentClip.

Build via ``scripts/build-exe.ps1``, or by hand from the repo root::

    uv run --group build pyinstaller --noconfirm packaging/agentclip.spec

The app itself is deliberately freeze-friendly (architecture.md S7): the Textual
CSS lives in a class var, the protocol templates are string constants, and
nothing resolves paths relative to ``__file__``. So there are no ``datas`` of
our own here - everything below exists to work around *dependency* dynamism
that PyInstaller's static analysis cannot see.
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

hiddenimports = (
    textual_hidden
    + pygments_hidden
    # copykitten is imported lazily inside a function and guarded by
    # try/except, so a missed collection would not error - it would silently
    # fall back to pyperclip and lose the Windows sequence-number polling.
    + ["copykitten", "pyperclip"]
    # The --pick-region overlay imports tkinter lazily (inside a function) so a
    # tk-less Linux still runs the rest of the app; name it explicitly so the
    # frozen build can never silently lose the picker.
    + ["tkinter"]
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
    datas=textual_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

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
