"""The user guide, as the page's "docs" button shows it.

``docs/commands.md`` and ``docs/configuration.md`` at the repo root are the
SOURCE OF TRUTH and stay that way: nothing here copies them into ``assets/``,
and no build step turns them into HTML. This module reads the two files at run
time and hands their markdown across the bridge; the page renders it with the
same hand-written renderer the transcripts use (``app.js``'s
``renderMarkdown``). A doc that changed is a doc the next window shows.

**Where they are**, in the order tried - the same question ``shell.py``'s
``asset_dir`` answers for the page itself, with one candidate more because these
files live OUTSIDE the package tree in a checkout:

1. **Package data** - ``importlib.resources.files("agentclip") / "docs"``. This
   is the wheel (``pyproject.toml`` force-includes both files at exactly that
   path) and the PyInstaller onefile build (``packaging/agentclip.spec``
   collects them to ``agentclip/docs``, which ``FrozenImporter`` answers by
   looking under ``sys._MEIPASS`` at that layout - the assets' own rule, and the
   reason ``--gui-smoke`` reads these back out of a frozen build).
2. **The repo checkout** - walk up from this file until a directory has
   ``docs/commands.md`` in it. This is what an editable install and a plain
   ``uv run`` get, where the package tree has no ``docs`` in it at all.

A file that is nowhere is NOT an error: the page gets a short markdown page
saying the guide was not shipped with this build and where it lives, because a
help button that opens an empty window is worse than one that explains itself.

Stdlib only and no imports of its own package, so if the TUI ever wants the same
surface this module moves up a layer unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

# The package the docs are collected INTO for a wheel and a frozen build, and
# the directory under it. Both are spelled in packaging/agentclip.spec and in
# pyproject.toml's force-include; this is the reader's half of that contract.
DOC_PACKAGE = "agentclip"
DOC_DIR = "docs"

#: The guide, in the order the viewer's switcher shows it. Adding a page here
#: is the whole change page-side - the tab strip is drawn from what arrives.
DOC_PAGES: tuple[tuple[str, str], ...] = (
    ("commands", "Commands"),
    ("configuration", "Configuration"),
)


@dataclass(frozen=True, slots=True)
class DocPage:
    """One page of the guide: its file stem, its tab label and its markdown."""

    name: str
    title: str
    text: str
    #: False when the file could not be found. ``text`` is then the note that
    #: says so - the viewer always has something to render.
    found: bool


def _from_package(name: str) -> str | None:
    """The file as package data, or None if this install carries none.

    ``Traversable.read_text`` rather than ``as_file``: nothing here needs a real
    path, and a zipped install would otherwise materialize a copy that outlives
    nothing useful.
    """
    try:
        return files(DOC_PACKAGE).joinpath(DOC_DIR, f"{name}.md").read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError, TypeError, ValueError):
        # Missing, unreadable, or a loader with no resource reader at all.
        return None


def repo_doc_dir() -> Path | None:
    """The checkout's ``docs/`` directory, walking up from this file.

    ``src/agentclip/shell/chat/docs.py`` -> ... -> the repo root. The first
    directory holding ``docs/commands.md`` wins, so an install that DOES carry
    the files as package data (candidate 1 above) is matched here too rather
    than being a second code path.
    """
    first, _ = DOC_PAGES[0]
    for parent in Path(__file__).resolve().parents:
        candidate = parent / DOC_DIR / f"{first}.md"
        if candidate.is_file():
            return candidate.parent
    return None


def _from_checkout(name: str) -> str | None:
    root = repo_doc_dir()
    if root is None:
        return None
    try:
        return (root / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return None


def missing_page(name: str, title: str) -> str:
    """What the viewer shows when the file is nowhere - markdown, like the rest."""
    return (
        f"# {title}\n\n"
        "This build does not carry the user guide.\n\n"
        f"The page lives at `docs/{name}.md` in the AgentClip repository - "
        "read it there, or run AgentClip from a source checkout.\n"
    )


def load_doc_page(name: str, title: str) -> DocPage:
    """One page, from wherever this install keeps it."""
    text = _from_package(name)
    if text is None:
        text = _from_checkout(name)
    if text is None or not text.strip():
        return DocPage(name=name, title=title, text=missing_page(name, title), found=False)
    return DocPage(name=name, title=title, text=text, found=True)


def load_doc_pages() -> tuple[DocPage, ...]:
    """The whole guide, in :data:`DOC_PAGES` order. Never raises, never empty.

    Read per call rather than cached: the two files are ~13 KB together, this
    runs once per page load, and editing a doc and reloading the window is worth
    more than the microseconds.
    """
    return tuple(load_doc_page(name, title) for name, title in DOC_PAGES)
