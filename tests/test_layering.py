"""Enforce the architecture's import direction with ast (architecture.md section 0).

    tui ──► clip                       tui & cli are the ONLY importers of clip/textual
     └──► engine ──► tools ──► sandbox
            │  └──► store
            └──► protocol (leaf)
    config (leaf) ◄── imported by everyone

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
    ("agentclip.config", frozenset({"platformdirs"})),
    ("agentclip.protocol", frozenset({"agentclip.config", "agentclip.protocol"})),
    (
        "agentclip.tools",
        frozenset({"agentclip.config", "agentclip.protocol.types", "agentclip.tools"}),
    ),
    ("agentclip.store", frozenset({"agentclip", "agentclip.config", "agentclip.store"})),
    ("agentclip.clip", frozenset({"agentclip", "agentclip.clip"})),
    (
        "agentclip.engine",
        frozenset(
            {
                "agentclip",
                "agentclip.config",
                "agentclip.engine",
                "agentclip.protocol",
                "agentclip.store",
                "agentclip.tools",
            }
        ),
    ),
]

# Modules allowed to import agentclip.clip / textual.
UI_MODULES = ("agentclip.cli", "agentclip.__main__", "agentclip.tui")


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


def test_only_tui_and_cli_import_clip_or_textual() -> None:
    violations: list[str] = []
    for path in all_modules():
        mod = module_name(path)
        if any(_matches(mod, ui) for ui in UI_MODULES) or _matches(mod, "agentclip.clip"):
            continue
        for imported in module_level_imports(path):
            if imported.split(".")[0] == "textual" or _matches(imported, "agentclip.clip"):
                violations.append(f"{mod} imports {imported}")
    assert not violations, "clip/textual leaked outside tui/cli:\n" + "\n".join(violations)


def test_engine_never_imports_ui_or_clipboard() -> None:
    engine_files = sorted((SRC / "engine").glob("*.py"))
    assert engine_files
    for path in engine_files:
        for imported in module_level_imports(path):
            root = imported.split(".")[0]
            assert root != "textual", f"{path.name} imports textual"
            assert not _matches(imported, "agentclip.clip"), f"{path.name} imports agentclip.clip"
            assert not _matches(imported, "agentclip.tui"), f"{path.name} imports agentclip.tui"
