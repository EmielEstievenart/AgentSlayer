# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile spec for the ENGINE half - ``agentclip-engine``.

Build via ``scripts/build-exe.sh`` (Linux/macOS) or by hand from the repo root::

    uv run --group build pyinstaller --noconfirm packaging/agentclip-engine.spec

This is the binary that runs on an SSH TARGET (docs/design/remote-executor.md
section 2.6). The master opens an exec channel running ``agentclip-engine
--project <root>`` and speaks the JSON-lines wire v1 over that channel's stdio.
Section 2.6 currently describes the target install as "the user pip/uv-installs
this same package over there"; section 5's standalone-binary open point is what
this spec starts to close - one artifact copied to the target, no Python needed
over there, still per-arch and still the user's job to place.

Two consequences of *where* it runs shape everything below.

**The stream is the protocol.** stdin carries frames in, stdout carries frames
out. The onefile bootloader is silent on both (it logs only under a debug
build), so the wire stays clean - but it is why ``console=True`` here is not the
TUI argument the main spec makes. There is no TUI. It is that a windowed build
would detach the standard streams and there would be no link at all.

**Nothing GUI-shaped may ride along.** The engine half already may not import
``agentclip.shell`` or ``agentclip.driver`` - ``tests/test_layering.py`` pins
that with an AST check - and the ``excludes`` list below is that same rule
expressed to PyInstaller, so a future stray import shows up as a fat binary and
a spec conflict rather than as a target that needs GTK to start a session.

What it MUST carry, and the only part that needs help: **MCP**. Section 2.7 puts
the servers on the target - spawned there, with the target's environment - so an
engine binary that cannot speak MCP has given up the reason it is on that
machine. And the SDK is imported *lazily inside functions* throughout
``executor/mcp/client.py`` (deliberately: the ``mcp`` extra is optional and its
absence is a per-server state, not a crash), which means PyInstaller's static
analysis sees exactly nothing of it. Same silent-failure shape as ``cv2`` in the
main spec: no build error, just a target whose every MCP server reports
``missing_sdk`` and tells the user to install an extra into a binary that has no
environment to install into. ``scripts/build-exe.sh`` syncs ``--extra mcp`` and
refuses to build without it for that reason.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller. Relative paths in Analysis() resolve
# against the invoking CWD, which would make the build directory-sensitive;
# anchor everything to the repo root instead.
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

# The MCP SDK, whole. Every one of its uses in executor/mcp/client.py is a
# function-body import - `import mcp` (the presence check in `start()`),
# `from mcp.client import Client`, `.client.stdio`, `.client.streamable_http`,
# `.client.sse`, and the private `mcp.shared._httpx_utils` - so nothing static
# reaches the package at all and the collection has to be named.
#
# collect_all rather than a hand-listed set of those six, and that is a
# deliberate reversal of the "name exactly what is reached" discipline the rest
# of this file follows. `import mcp` alone already pulls the server half
# (mcp/__init__.py re-exports it, dragging starlette, uvicorn and sse-starlette
# in), so there is no slim subset to win by being clever - only a list that
# drifts the next time a new import site is added to client.py. collect_all also
# brings the datas, which is what the SDK's own schema/resource files need.
#
# `mcp.cli` is the one subpackage filtered back out, and not merely to save
# space: it is the SDK's own DEVELOPER command (`mcp dev`, `mcp install`), it
# belongs to the SDK's optional `cli` extra, and its `__init__` imports typer at
# module scope behind an `except ImportError: sys.exit(1)`. SystemExit is not an
# Exception, so PyInstaller's collector does not catch it, and without this
# filter `collect_all` aborts the entire build with "No module named 'typer'".
# Nothing in AgentClip reaches it - the same shape, and the same fix, as the
# main spec dropping textual_image's demo app.
mcp_datas, mcp_binaries, mcp_hidden = collect_all(
    "mcp",
    filter_submodules=lambda name: name != "mcp.cli" and not name.startswith("mcp.cli."),
)

# `mcp-types` is a SEPARATE distribution (top-level package `mcp_types`) that
# carries the wire schema, versioned one submodule per protocol revision
# (`_v2025_11_25`, `_v2026_07_28`, ...). Those are selected by protocol version
# rather than by a static import from a fixed name, so the walk is named here -
# a binary that shipped only the revision the build box happened to negotiate
# would fail against a peer on another one, and it would fail as a wire error
# rather than as anything that points here.
mcp_types_hidden = collect_submodules("mcp_types")

hiddenimports = (
    mcp_hidden
    + mcp_types_hidden
    # httpx2 is imported by OUR code, lazily, in `_open_streamable` - the remote
    # MCP transport builds its own client there to attach the 401/403 event hook
    # the auth classification reads. It is a dependency of `mcp` as well, so the
    # collection would very likely happen anyway; it is named for REACHABILITY,
    # which is the half a function-body import makes fragile, and because a
    # remote MCP server failing to dial is exactly the kind of thing that gets
    # blamed on the network rather than on the freeze.
    + ["httpx2"]
    # anyio picks its event-loop backend by name at run time
    # (`import_module("anyio._backends._asyncio")`), so the module the manager's
    # whole loop thread runs on is invisible to static analysis. contrib ships
    # hook-anyio which handles the backends; this names the package so the hook
    # has something to fire on even though every path to it is lazy.
    #
    # pydantic is the SDK's model layer and is likewise reached only through the
    # lazy `mcp` imports. Collection needs no help (contrib ships hook-pydantic,
    # which deals with pydantic_core and the compiled validators); reachability
    # does.
    + ["anyio", "pydantic"]
)

# What the engine half is NOT. Every name here is either a package this process
# must never import (the layering rule of tests/test_layering.py, made
# mechanical) or a dev-group package sharing the same .venv. None of them is
# reachable from `agentclip.engine.link.__main__` today - verified by importing
# it and reading sys.modules, which comes back stdlib + platformdirs + tomli_w +
# agentclip - so excluding them changes nothing about THIS build. They are here
# so that the day one of them becomes reachable, it is a build-time surprise
# instead of forty extra megabytes on a target that never opens a window.
excludes = [
    # SHELL: the two UIs and their trees. `textual` and `pillow`/`textual_image`
    # are hard runtime dependencies of the package, which is precisely why they
    # have to be named - a full install on a target drags them (design section
    # 2.6, "the unused weight is disk, not coupling") and a *binary* has no
    # excuse to.
    "textual",
    "textual_image",
    "pygments",
    # The GUI shell's stack. On Linux this is also the awkward one to have
    # collected by accident: pywebview's Linux backends want PyGObject/Qt
    # bindings against system libraries a headless target does not have.
    "webview",
    "pythonnet",
    "clr",
    "clr_loader",
    # DRIVER: the desktop-automation half. The engine touches no screen and no
    # clipboard - it is handed frames.
    "cv2",
    "numpy",
    "copykitten",
    "pyperclip",
    # The --pick-region overlay's toolkit. Excluded here where the main spec
    # deliberately keeps it: there is no region to pick on a target, and Tcl/Tk
    # is several megabytes of runtime for nobody.
    "tkinter",
    # The remote half never SSHes OUT (design section 2.6: it uses LocalHost).
    # paramiko lives in executor/hosts/ssh.py and nothing the engine imports
    # reaches that module - the layering test says so and the sys.modules walk
    # above confirms it. If this exclude ever starts mattering, the engine has
    # grown a second hop nobody designed.
    "paramiko",
    # The dev group, same shared .venv, same reasoning as the main spec.
    #
    # NB: `click` is NOT excluded here, unlike in packaging/agentclip.spec.
    # uvicorn imports it eagerly and uvicorn arrives with `mcp` above, so
    # excluding it would swap a few unused kilobytes for an import error
    # somewhere inside the SDK. `jinja2` stays excluded because starlette's
    # templating module guards its import and nothing on the client path asks
    # for it.
    "textual_dev",
    "textual_serve",
    "aiohttp",
    "aiohttp_jinja2",
    "jinja2",
    "msgpack",
    "pytest",
    "_pytest",
    "mypy",
]

a = Analysis(
    # The console script's module, reached as a file. `[project.scripts]` names
    # `agentclip.engine.link.__main__:main` and this is that module - its
    # `if __name__ == "__main__"` guard fires because PyInstaller runs an entry
    # script exactly as `__main__`, so the frozen binary and `python -m
    # agentclip.engine.link` are the same door.
    [os.path.join(SRC, "agentclip", "engine", "link", "__main__.py")],
    pathex=[SRC],
    binaries=mcp_binaries,
    # No datas of our own. The whole package is freeze-friendly by design
    # (architecture.md section 7) and the ONE exception - the GUI shell's page
    # assets, the main spec's `gui_datas` - is on the other side of the layering
    # line and excluded above.
    datas=mcp_datas,
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
    # The name the master launches over the exec channel, unchanged from the
    # console script (design section 2.6). A binary copied to a target's PATH
    # under this name is indistinguishable from an installed one to the launcher.
    name="agentclip-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX trips AV heuristics and has a history of corrupting Windows DLLs.
    upx=False,
    runtime_tmpdir=None,
    # Not "it is a TUI" - it is not one. It is that stdin/stdout ARE the link
    # (design section 2.9), and a windowed build detaches exactly those.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
