"""Enforce the architecture's import direction with ast (architecture.md section 0).

    tui ──► clip                       tui & cli are the ONLY importers of clip/textual
     └──► engine ──► tools ──► sandbox
            │  └──► store
            └──► protocol (leaf)
    config (leaf) ◄── imported by everyone
     ├──► permissions (leaf: the rule model, also used by engine/approval)
     ├──► mcp (leaf: opencode.json's mcp block - types/reader for config,
     │         the client runtime for tools; docs/design/mcp.md)
     └──► hosts (to read the project's .agentclip.toml off the project's machine)
    hosts (leaf: the OS seam - config/tools/store/engine touch files and run
           commands only through it, so a remote host can take the local one's
           place; paramiko lives in hosts/ssh.py and nowhere else)

Only module-level imports count: lazy third-party imports inside functions
(e.g. copykitten in the clip providers) are allowed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "agentclip"
STDLIB = frozenset(sys.stdlib_module_names)

# Per-layer allowed import roots beyond the stdlib. An entry matches the
# imported module name exactly or as a package prefix, EXCEPT the bare
# "agentclip" entry which matches only the root package itself (__version__).
RULES: list[tuple[str, frozenset[str]]] = [
    # permissions: a stdlib-only leaf below config, so the same rule model can be
    # loaded by config.py and applied by engine/approval.py.
    ("agentclip.permissions", frozenset()),
    # hosts: the OS seam (this PC's subprocess/fs, or a machine over SSH). A
    # leaf like permissions, so tools/store/engine can all resolve their file
    # and command access through the same object. Stdlib-only except for the
    # one third-party client the remote implementation IS - and that one is
    # confined to hosts/ssh.py (see test_paramiko_only_in_the_ssh_host).
    ("agentclip.hosts", frozenset({"agentclip.hosts", "paramiko"})),
    # mcp: OpenCode's mcp block (types + reader + client runtime). A leaf like
    # permissions and for the same reason: config.py LOADS the servers where
    # config lives, tools invoke them where handlers live, and only a leaf both
    # import can be true of both. The optional SDK is imported lazily inside
    # client.py functions, so it never shows up at module level here; the
    # ToolSpecs live in agentclip/tools/mcp_tools.py, NOT in this package,
    # precisely so mcp never has to import tools (docs/design/mcp.md section 2).
    ("agentclip.mcp", frozenset({"agentclip.mcp"})),
    # config: reads the project's .agentclip.toml, which in a remote session is
    # on the remote machine - hence the Host seam, one leaf below it.
    (
        "agentclip.config",
        frozenset(
            {
                "platformdirs",
                "tomli_w",
                "agentclip.hosts",
                "agentclip.permissions",
                # Only the stdlib-only submodules: the client runtime (and the
                # SDK behind it) stays out of config's import graph by listing
                # the two leaves rather than the package.
                "agentclip.mcp.types",
                "agentclip.mcp.config",
            }
        ),
    ),
    ("agentclip.protocol", frozenset({"agentclip.config", "agentclip.protocol"})),
    (
        "agentclip.tools",
        frozenset(
            {
                "agentclip.config",
                "agentclip.hosts",
                "agentclip.mcp",
                "agentclip.protocol.types",
                "agentclip.tools",
            }
        ),
    ),
    (
        "agentclip.store",
        frozenset({"agentclip", "agentclip.config", "agentclip.hosts", "agentclip.store"}),
    ),
    ("agentclip.clip", frozenset({"agentclip", "agentclip.clip"})),
    ("agentclip.screen", frozenset({"agentclip", "agentclip.screen"})),
    (
        "agentclip.engine",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.engine",
                "agentclip.hosts",
                "agentclip.permissions",
                "agentclip.protocol",
                "agentclip.store",
                "agentclip.tools",
            }
        ),
    ),
    # app: UI-agnostic orchestration layer. Drives the engine through the ChatView
    # port; imports engine/protocol/store/config but NOT textual, clip, or tui.
    (
        "agentclip.app",
        frozenset(
            {
                "agentclip",
                "agentclip.app",
                "agentclip.config",
                "agentclip.engine",
                "agentclip.protocol",
                "agentclip.store",
            }
        ),
    ),
]

# Modules allowed to import agentclip.clip / agentclip.screen / textual.
UI_MODULES = ("agentclip.cli", "agentclip.__main__", "agentclip.tui")

# OS side-effect layers only the UI shell may touch (clipboard, screen overlay/click).
OS_LAYERS = ("agentclip.clip", "agentclip.screen")


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
    if allowed == "agentclip":  # bare root: __version__ only, not a wildcard
        return imported == "agentclip"
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
            continue  # tui / cli / __main__ / package root: unrestricted layer
        for imported in module_level_imports(path):
            if imported.split(".")[0] in STDLIB:
                continue
            if any(_matches(imported, entry) for entry in allowed):
                continue
            violations.append(f"{mod} imports {imported}")
    assert not violations, "layering violations:\n" + "\n".join(violations)


def test_only_tui_and_cli_import_clip_screen_or_textual() -> None:
    violations: list[str] = []
    for path in all_modules():
        mod = module_name(path)
        if any(_matches(mod, ui) for ui in UI_MODULES) or any(
            _matches(mod, layer) for layer in OS_LAYERS
        ):
            continue
        for imported in module_level_imports(path):
            if imported.split(".")[0] == "textual" or any(
                _matches(imported, layer) for layer in OS_LAYERS
            ):
                violations.append(f"{mod} imports {imported}")
    assert not violations, "clip/screen/textual leaked outside tui/cli:\n" + "\n".join(violations)


def test_paramiko_only_in_the_ssh_host() -> None:
    """The SSH client is one module's business.

    hosts/ssh.py IS paramiko wearing the Host interface, so it imports it; every
    other module - including hosts/base.py, which everything above the seam
    imports - must stay clear, so a local session never loads a crypto stack it
    has no use for.
    """
    allowed = SRC / "hosts" / "ssh.py"
    for path in all_modules():
        if path == allowed:
            continue
        assert not any(
            imported.split(".")[0] == "paramiko" for imported in module_level_imports(path)
        ), f"{module_name(path)} imports paramiko"


def test_engine_never_imports_ui_or_clipboard() -> None:
    engine_files = sorted((SRC / "engine").glob("*.py"))
    assert engine_files
    for path in engine_files:
        for imported in module_level_imports(path):
            root = imported.split(".")[0]
            assert root != "textual", f"{path.name} imports textual"
            assert not _matches(imported, "agentclip.clip"), f"{path.name} imports agentclip.clip"
            assert not _matches(imported, "agentclip.tui"), f"{path.name} imports agentclip.tui"


def test_app_never_imports_clip_textual_or_tui() -> None:
    """The orchestration layer must stay UI-agnostic: no Textual, no clipboard, no tui."""
    app_files = sorted((SRC / "app").glob("*.py"))
    assert app_files
    for path in app_files:
        for imported in module_level_imports(path):
            root = imported.split(".")[0]
            assert root != "textual", f"{path.name} imports textual"
            assert not _matches(imported, "agentclip.clip"), f"{path.name} imports agentclip.clip"
            assert not _matches(imported, "agentclip.tui"), f"{path.name} imports agentclip.tui"
