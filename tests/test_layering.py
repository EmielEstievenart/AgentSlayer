"""Enforce the architecture's import direction with ast (architecture.md section 0).

Three named layers sit above the engine - SHELL (the UI), DRIVER (what
AgentClip does to the desktop chat app it operates) and EXECUTOR (permission-
gated execution, reaching the machine through the Host seam):

    SHELL  shell/gui ──► driver/clip   gui is the ONLY importer of pywebview
            ├──► shell/app ──► engine
            └──► DRIVER: driver/automation ──► driver/monitor ──► driver/clip,
                         (the shared core, also a clip/screen importer) driver/screen
    engine ──► EXECUTOR: executor/tools ──► sandbox
      │  └──► engine/store
      └──► protocol (leaf)
    config (leaf) ◄── imported by everyone
     ├──► executor/permissions (leaf: the rule model, also used by engine/approval)
     ├──► executor/mcp (leaf: permissions.json's mcp block - types/reader for config,
     │                  the client runtime for tools; docs/design/mcp.md)
     └──► executor/hosts (to read the project's .agentclip.toml off its machine)
    executor/hosts (leaf: the OS seam - config, executor/tools, engine/store and
           engine touch files and run commands only through it, so a remote host
           can take the local one's place; paramiko lives in
           executor/hosts/ssh.py and nowhere else. One exception,
           executor/hosts/connect.py: the remote connect sequence both shells
           drive, whose first and last steps are config loads - it is the only
           module here that may import config, and nothing in the package
           imports IT, so the cycle never closes)

Only module-level imports count: lazy third-party imports inside functions
(e.g. copykitten in the driver's clip providers) are allowed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "agentclip"
STDLIB = frozenset(sys.stdlib_module_names)

# Names that match ONLY themselves, never as a package prefix: the root package
# (whose __init__ is __version__) and the three layer packages of
# architecture.md section 0, whose __init__.py files are a docstring and nothing
# else. Listing them here is what lets the bare-layer RULES entries at the
# bottom pin those four files import-free without swallowing everything
# underneath them.
EXACT_ONLY = frozenset(
    {"agentclip", "agentclip.driver", "agentclip.executor", "agentclip.shell"}
)

# Per-layer allowed import roots beyond the stdlib. An entry matches the
# imported module name exactly or as a package prefix, except for the EXACT_ONLY
# names above.
RULES: list[tuple[str, frozenset[str]]] = [
    # permissions: a stdlib-only leaf below config, so the same rule model can be
    # loaded by config.py and applied by engine/approval.py.
    ("agentclip.executor.permissions", frozenset()),
    # hosts.connect: the ONE module in the seam that reads configuration, and it
    # must come before the ``agentclip.executor.hosts`` rule below (first match
    # wins) so the allowance is this file's alone. It is the remote CONNECT
    # SEQUENCE (docs/design/ui-briefs/ssh-connect.md §2), whose first and last
    # steps ARE config loads: the LOCAL config names the target, and the REMOTE
    # project's is read back through the host that was just dialled. Both shells
    # drive it - cli.remote_launch with terminal prompts, the GUI's dialog with
    # modals - so it cannot live in either of them without the other re-deriving
    # it. The direction is still one-way at run time: nothing in
    # agentclip/executor/hosts/__init__.py imports this module, so config's own
    # ``from agentclip.executor.hosts.base import Host`` never pulls it in, and
    # the rest of the seam stays the stdlib-and-paramiko leaf the diagram above
    # describes.
    (
        "agentclip.executor.hosts.connect",
        frozenset({"agentclip.config", "agentclip.executor.hosts", "paramiko"}),
    ),
    # hosts: the OS seam (this PC's subprocess/fs, or a machine over SSH). A
    # leaf like permissions, so executor/tools, engine/store and the engine can
    # all resolve their file and command access through the same object.
    # Stdlib-only except for the one third-party client the remote
    # implementation IS - and that one is confined to executor/hosts/ssh.py
    # (see test_paramiko_only_in_the_ssh_host).
    ("agentclip.executor.hosts", frozenset({"agentclip.executor.hosts", "paramiko"})),
    # mcp: OpenCode's mcp block (types + reader + client runtime). A leaf like
    # permissions and for the same reason: config.py LOADS the servers where
    # config lives, tools invoke them where handlers live, and only a leaf both
    # import can be true of both. The optional SDK is imported lazily inside
    # client.py functions, so it never shows up at module level here; the
    # ToolSpecs live in agentclip/executor/tools/mcp_tools.py, NOT in this package,
    # precisely so mcp never has to import tools (docs/design/mcp.md section 2).
    ("agentclip.executor.mcp", frozenset({"agentclip.executor.mcp"})),
    # config: reads the project's .agentclip.toml, which in a remote session is
    # on the remote machine - hence the Host seam, one leaf below it.
    (
        "agentclip.config",
        frozenset(
            {
                "platformdirs",
                "tomli_w",
                "agentclip.executor.hosts",
                "agentclip.executor.permissions",
                # Only the stdlib-only submodules: the client runtime (and the
                # SDK behind it) stays out of config's import graph by listing
                # the two leaves rather than the package.
                "agentclip.executor.mcp.types",
                "agentclip.executor.mcp.config",
            }
        ),
    ),
    ("agentclip.protocol", frozenset({"agentclip.config", "agentclip.protocol"})),
    (
        "agentclip.executor.tools",
        frozenset(
            {
                "agentclip.config",
                "agentclip.executor.hosts",
                "agentclip.executor.mcp",
                "agentclip.executor.tools",
                "agentclip.protocol.types",
            }
        ),
    ),
    # engine.link: the ENGINE HALF of the Shell<->Engine link
    # (docs/design/remote-executor.md) - the assembly both the `cli` and the
    # server process a later increment adds call to turn one EngineRequest into
    # one Engine. Like engine.store it lives inside the engine package with its
    # own allowance, so it must come before the ``agentclip.engine`` rule below
    # (first match wins). It reaches executor.mcp because SIZING the tool catalog
    # against the session's paste budget is its job and nobody else's - the
    # config it reads the budget from is a leaf and cannot ask a running server
    # what it offers. What it must NEVER import is shell or driver: this IS the
    # code that runs on the target, where there is no clipboard and no window.
    (
        "agentclip.engine.link",
        frozenset(
            {
                # The root package, which is ``__version__`` and nothing else:
                # the handshake states the PACKAGE version of each half so a
                # version refusal can name both installs (remote-executor.md
                # section 2.9). EXACT_ONLY, so this buys the constant and no
                # subpackage.
                "agentclip",
                "agentclip.config",
                "agentclip.engine",
                "agentclip.engine.store",
                "agentclip.executor.hosts",
                "agentclip.executor.mcp",
                "agentclip.executor.tools",
                "agentclip.protocol",
            }
        ),
    ),
    # engine.store: session + backup persistence. It lives INSIDE the engine
    # package but keeps its own narrower allowance, so it must come before the
    # ``agentclip.engine`` rule below (first match wins) - otherwise the store
    # would silently inherit the engine's wider one.
    (
        "agentclip.engine.store",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.engine.store",
                "agentclip.executor.hosts",
            }
        ),
    ),
    ("agentclip.driver.clip", frozenset({"agentclip", "agentclip.driver.clip"})),
    ("agentclip.driver.screen", frozenset({"agentclip", "agentclip.driver.screen"})),
    (
        "agentclip.engine",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.engine",
                "agentclip.engine.store",
                "agentclip.executor.hosts",
                "agentclip.executor.permissions",
                "agentclip.executor.tools",
                "agentclip.protocol",
            }
        ),
    ),
    # shell.app: the Shell's UI-agnostic orchestration layer. Drives the engine
    # through the ChatView port; imports engine/engine.store/protocol/config but
    # never the clipboard and never the shell above it.
    (
        "agentclip.shell.app",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.engine",
                "agentclip.engine.store",
                "agentclip.protocol",
                "agentclip.shell.app",
            }
        ),
    ),
    # automation.describe: the ONE label both shells show is a function of the
    # engine's Phase (where the task is) and the loop's LoopState (where the
    # round trip is) - docs/design/ui-monitor.md §2.5. engine/states.py is a
    # stdlib-only enum leaf, so this single module may read it; first match
    # wins, so the allowance is this file's alone and the Driver stays
    # engine-free otherwise.
    (
        "agentclip.driver.automation.describe",
        frozenset(
            {
                "agentclip",
                "agentclip.driver.automation",
                "agentclip.engine.states",
            }
        ),
    ),
    # driver.automation: the Driver's core, which the GUI drives. It IS the loop
    # that watches and clicks the chat window, so it needs
    # driver.monitor/driver.screen/driver.clip the way cli does - but it must
    # never import shell.app or shell.gui: a shell depends on the Driver, never
    # the other way round. It outlived the first shell built on it
    # (docs/design/ui-monitor.md 6.6) precisely because of this rule.
    (
        "agentclip.driver.automation",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.driver.automation",
                "agentclip.driver.clip",
                "agentclip.driver.monitor",
                "agentclip.driver.screen",
            }
        ),
    ),
    # driver.monitor: the UI monitor (docs/design/ui-monitor.md §3) - pixels,
    # matching, debounce, mouse, keyboard, clipboard. It sits BELOW automation:
    # driver/automation -> driver/monitor -> driver/screen, driver/clip, and
    # nothing here may import driver/automation (the monitor does not know what
    # a LoopState is).
    (
        "agentclip.driver.monitor",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.driver.clip",
                "agentclip.driver.monitor",
                "agentclip.driver.screen",
            }
        ),
    ),
    # gui: the pywebview shell (docs/design/gui.md section 2), and since
    # docs/design/ui-monitor.md 6.6 the ONLY one. It drives the app +
    # automation controllers over the OS seams, plus the one toolkit that IS
    # this shell, which no other module may import (see
    # test_pywebview_only_in_the_gui_shell). It has a rule at all, where cli is
    # unrestricted, because saying what a shell may reach for is cheapest before
    # it reaches.
    (
        "agentclip.shell.gui",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.driver.automation",
                "agentclip.driver.clip",
                # The machine the automation acts on. A shell CONSTRUCTS the
                # monitor and hands it to the controller (ui-monitor.md §6.1) -
                # which is a shell's job precisely because it is the launch
                # question "whose screen is this?", the same shape as deciding
                # which machine the executor runs on. The chat GUI builds a
                # ``LocalUIMonitor``; §6.4's calibration window is built over one
                # too, and only ever over a local one.
                "agentclip.driver.monitor",
                "agentclip.driver.screen",
                # The engine's VALUE types, and only ever as values: `Decision`
                # is what an approval answer IS,
                # `PendingAction` is what a gate is handed, and `Engine` is the
                # return type of the factory cli.py builds. A shell that could
                # not name them would have to re-declare the vocabulary its own
                # controller already speaks - which is exactly the drift the
                # ports exist to prevent. `agentclip.shell.app` already depends
                # on this layer, so nothing about the direction changes.
                "agentclip.engine",
                # The OS seam, and only for the surface increment 7 built: the
                # connect dialog IS the construction of a remote host
                # (`executor.hosts.connect`), and the sidebar's link indicator reads the
                # host it made. cli.py has always named this layer for the same
                # reason - deciding which machine a session runs on is a launch
                # question, and this shell is now one of the two places a human
                # answers it (docs/design/ui-briefs/ssh-connect.md).
                "agentclip.executor.hosts",
                "agentclip.protocol",
                "agentclip.shell.app",
                "agentclip.shell.gui",
                "webview",
            }
        ),
    ),
    # The three layer packages themselves. They come LAST, below every rule that
    # names something inside them, because first match wins - and they are
    # EXACT_ONLY, so they govern exactly one file each: driver/__init__.py,
    # executor/__init__.py, shell/__init__.py. Each is a docstring saying what
    # the layer is for and nothing else, which is the point: a layer package
    # that re-exported its contents would make "import the shell" pull in
    # pywebview and the whole OS seam at once.
    ("agentclip.driver", frozenset()),
    ("agentclip.executor", frozenset()),
    ("agentclip.shell", frozenset()),
]

# Modules allowed to import agentclip.driver.clip / agentclip.driver.screen:
# the shell and the launcher, plus the Driver's own core and monitor (which are
# made of exactly those seams).
CLIP_SCREEN_IMPORTERS = (
    "agentclip.cli",
    "agentclip.__main__",
    "agentclip.shell.gui",
    "agentclip.driver.automation",
    "agentclip.driver.monitor",
)

# OS side-effect layers (clipboard, screen overlay/click): only CLIP_SCREEN_IMPORTERS.
OS_LAYERS = ("agentclip.driver.clip", "agentclip.driver.screen")


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def module_level_imports(path: Path) -> set[str]:
    """Imported module names at module level (function/lambda bodies skipped:
    lazy imports are an allowed pattern for optional third-party deps)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    def collect(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Import):
                found.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                assert child.level == 0, f"relative import in {path} (use absolute imports)"
                assert child.module is not None
                found.add(child.module)
            collect(child)

    collect(tree)
    return found


def _matches(imported: str, allowed: str) -> bool:
    if allowed in EXACT_ONLY:  # bare root / layer package: never a wildcard
        return imported == allowed
    return imported == allowed or imported.startswith(allowed + ".")


def all_modules() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, f"no sources found under {SRC}"
    return files


def test_layer_rules() -> None:
    violations: list[str] = []
    for path in all_modules():
        mod = module_name(path)
        allowed = next(
            (extra for prefix, extra in RULES if _matches(mod, prefix)),
            None,
        )
        if allowed is None:
            continue  # cli / __main__ / package root: unrestricted layer
        for imported in module_level_imports(path):
            if imported.split(".")[0] in STDLIB:
                continue
            if any(_matches(imported, entry) for entry in allowed):
                continue
            violations.append(f"{mod} imports {imported}")
    assert not violations, "layering violations:\n" + "\n".join(violations)


def test_only_the_shell_and_the_driver_import_clip_or_screen() -> None:
    """``driver.clip``/``driver.screen`` are the OS seams the Driver's core is
    MADE of, so it may reach them alongside the shell and the launcher - and
    nobody else may, because an import of either is an import of the clipboard,
    the mouse and the screen.
    """
    violations: list[str] = []
    for path in all_modules():
        mod = module_name(path)
        is_os_layer = any(_matches(mod, layer) for layer in OS_LAYERS)
        if is_os_layer or any(_matches(mod, ui) for ui in CLIP_SCREEN_IMPORTERS):
            continue
        for imported in module_level_imports(path):
            if any(_matches(imported, layer) for layer in OS_LAYERS):
                violations.append(f"{mod} imports {imported}")
    assert not violations, (
        "driver.clip/driver.screen leaked outside the shell, the cli and the Driver:\n"
        + "\n".join(violations)
    )


def test_nothing_imports_textual() -> None:
    """The Textual TUI was deleted in phase 6 (docs/design/ui-monitor.md 6.6).

    It used to take three rules to keep one shell's toolkit out of the other's
    graph; there is one shell now, and the dependency is gone from
    pyproject.toml altogether - so the rule is simply that nobody imports it. A
    reappearing ``textual`` import would mean either a resurrected shell or a
    stray copy of a widget, and both want a conversation rather than a merge.
    """
    violations = [
        f"{module_name(path)} imports {imported}"
        for path in all_modules()
        for imported in module_level_imports(path)
        if imported.split(".")[0] in ("textual", "textual_image")
    ]
    assert not violations, "textual is not a dependency any more:\n" + "\n".join(violations)


def test_paramiko_only_in_the_ssh_host() -> None:
    """The SSH client is one module's business.

    executor/hosts/ssh.py IS paramiko wearing the Host interface, so it imports
    it; every other module - including executor/hosts/base.py, which everything
    above the seam imports - must stay clear, so a local session never loads a crypto stack it
    has no use for.
    """
    allowed = SRC / "executor" / "hosts" / "ssh.py"
    for path in all_modules():
        if path == allowed:
            continue
        assert not any(
            imported.split(".")[0] == "paramiko" for imported in module_level_imports(path)
        ), f"{module_name(path)} imports paramiko"


def test_pywebview_only_in_the_gui_shell() -> None:
    """The window toolkit is one package's business.

    ``agentclip.shell.gui`` IS pywebview wearing a UI shell, so it imports it;
    nothing else may, because the ``gui`` extra is optional and an install with
    no window - the engine half on an SSH target, the standing monitor - must
    not be able to trip over a missing window library. ``cli.main`` reaches the
    shell through an import inside the function, which this checker does not
    see - by design, that is the same allowance every lazy optional import in
    this project uses.
    """
    for path in all_modules():
        mod = module_name(path)
        if _matches(mod, "agentclip.shell.gui"):
            continue
        assert not any(
            imported.split(".")[0] == "webview" for imported in module_level_imports(path)
        ), f"{mod} imports pywebview"


def test_engine_never_imports_ui_or_clipboard() -> None:
    """The engine runs where there is no window and no clipboard - on an SSH
    target, inside the frozen ``agentclip-engine`` - so neither may reach its
    import graph."""
    engine_files = sorted((SRC / "engine").rglob("*.py"))
    assert engine_files
    for path in engine_files:
        for imported in module_level_imports(path):
            assert not _matches(
                imported, "agentclip.driver.clip"
            ), f"{path.name} imports agentclip.driver.clip"
            assert not _matches(
                imported, "agentclip.shell"
            ), f"{path.name} imports the shell ({imported})"


def test_automation_never_imports_a_shell() -> None:
    """The Driver's core must outlive any one shell - and has: the shell it was
    lifted out of is gone (docs/design/ui-monitor.md 6.6) and this layer did not
    move. It may touch driver.monitor/driver.screen/driver.clip (it is the loop
    that drives them), but a reach back up into shell.app or shell.gui would
    weld it to today's frontend and leave the next one nothing to share.
    """
    automation_files = sorted((SRC / "driver" / "automation").rglob("*.py"))
    assert automation_files
    for path in automation_files:
        for imported in module_level_imports(path):
            assert not _matches(
                imported, "agentclip.shell"
            ), f"{path.name} imports the shell ({imported})"


def test_app_never_imports_the_clipboard_or_the_window() -> None:
    """The orchestration layer must stay UI-agnostic: no clipboard, and no reach
    up into the shell that drives it."""
    app_files = sorted((SRC / "shell" / "app").glob("*.py"))
    assert app_files
    for path in app_files:
        for imported in module_level_imports(path):
            assert not _matches(
                imported, "agentclip.driver.clip"
            ), f"{path.name} imports agentclip.driver.clip"
            assert not _matches(
                imported, "agentclip.shell.gui"
            ), f"{path.name} imports agentclip.shell.gui"
