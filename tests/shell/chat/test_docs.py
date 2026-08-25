"""The user guide: the titlebar's "docs" button and what it opens.

Three things are pinned here, and they are the three ways this feature can rot:

1. **The files stay the source of truth.** ``docs/commands.md`` and
   ``docs/configuration.md`` are READ, never copied into the assets and never
   pre-rendered, so the text that crosses the bridge is the file's own bytes.
2. **A build that lost them says so.** The viewer degrades to a note; it never
   raises, and ``--gui-smoke`` fails the BUILD rather than leaving a button
   that opens an empty box.
3. **The page escapes before it renders.** Both documents are full of literal
   ``<project>``, ``<key>`` and ``<name>``; a renderer that escaped afterwards
   (or not at all) would swallow them into tags. The page's half is pinned by
   reading the asset, the way ``test_chrome.py`` pins ``LOG_MAX`` - there is no
   JS runtime in this suite, so what a test can hold is the SHAPE of the code.

Nothing here opens a window.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentclip import cli
from agentclip.shell.chat import docs as gui_docs
from agentclip.shell.chat.docs import DOC_PAGES, load_doc_pages, repo_doc_dir
from tests.shell.chat.conftest import Harness
from tests.shell.chat.test_chrome import KeySpy, api_of

REPO = Path(__file__).resolve().parents[3]
ASSETS = REPO / "src" / "agentclip" / "shell" / "chat" / "assets"


def asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


# == finding the files ========================================================


def test_the_guide_is_found_from_a_source_checkout() -> None:
    """The candidate an editable install and every `uv run` take: walk up from
    the package until a directory has the guide in it. There is no package data
    to find in a checkout - these files live at the repo root, not under src."""
    assert repo_doc_dir() == REPO / "docs"


@pytest.mark.parametrize(("name", "title"), DOC_PAGES)
def test_every_page_of_the_guide_loads_whole(name: str, title: str) -> None:
    page = next(page for page in load_doc_pages() if page.name == name)
    assert page.found is True
    assert page.title == title
    # Byte for byte the file, which is the whole point: no build step, no copy
    # in the assets, nothing between the document and the reader.
    assert page.text == (REPO / "docs" / f"{name}.md").read_text(encoding="utf-8")


def test_a_build_without_the_guide_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both candidates come up empty - a wheel built without the force-include,
    a frozen exe whose spec lost the datas. The viewer still has a page to draw
    and it says where the real one lives."""
    monkeypatch.setattr(gui_docs, "_from_package", lambda name: None)
    monkeypatch.setattr(gui_docs, "repo_doc_dir", lambda: None)

    pages = load_doc_pages()
    assert [page.name for page in pages] == [name for name, _ in DOC_PAGES]
    for page in pages:
        assert page.found is False
        assert f"docs/{page.name}.md" in page.text
        # Still markdown, so the viewer renders it exactly like a real page.
        assert page.text.startswith("# ")


def test_a_file_that_is_there_but_empty_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-byte doc is a packaging failure wearing a success - the same
    reason --gui-smoke checks the assets for content rather than existence."""
    monkeypatch.setattr(gui_docs, "_from_package", lambda name: "   \n")
    monkeypatch.setattr(gui_docs, "repo_doc_dir", lambda: None)
    assert [page.found for page in load_doc_pages()] == [False for _ in DOC_PAGES]


def test_package_data_is_preferred_over_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel and a frozen build carry the guide AS PACKAGE DATA, at the path
    ``files("agentclip") / "docs"`` resolves - and that answer must win, because
    walking up from an installed package finds whatever happens to be above the
    site-packages tree."""
    monkeypatch.setattr(gui_docs, "_from_package", lambda name: f"# {name}\n\nfrom the package\n")
    pages = load_doc_pages()
    assert all(page.found for page in pages)
    assert all("from the package" in page.text for page in pages)


# == what crosses the bridge ==================================================


def test_the_guide_reaches_the_page_at_mount(harness: Harness) -> None:
    """Through ``start`` rather than the push: the guide is chrome the window
    has from its first frame, not something a session brings with it."""
    harness.view._controller = KeySpy()  # type: ignore[assignment]
    harness.view.start()
    pages = harness.flush().last("docs")["pages"]
    assert [page["name"] for page in pages] == [name for name, _ in DOC_PAGES]
    assert [page["title"] for page in pages] == [title for _, title in DOC_PAGES]
    assert all(page["found"] for page in pages)
    assert pages[0]["text"].startswith("# Commands")


def test_a_reloaded_page_gets_the_guide_back(harness: Harness) -> None:
    """`commands`' twin: page state a reload wipes, with nothing on the page to
    rebuild it from."""
    api_of(harness).ready()
    assert "docs" in harness.flush().types


def test_the_guide_crosses_as_markdown_never_as_html(harness: Harness) -> None:
    """The rendering is the page's, which is what keeps the escaping in ONE
    place - the renderer that already escapes every transcript."""
    harness.view._push_docs()
    text = harness.flush().last("docs")["pages"][1]["text"]
    assert "<table" not in text
    assert "| Key | Default | Meaning |" in text


# == the packaging check ======================================================


def test_the_gui_smoke_fails_a_build_that_lost_the_guide(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The assets' rule, applied to the other thing the spec has to collect: a
    missing guide fails nothing at run time, so it has to fail at build time."""
    pytest.importorskip("webview", reason="the gui extra is not installed")
    monkeypatch.setattr(gui_docs, "_from_package", lambda name: None)
    monkeypatch.setattr(gui_docs, "repo_doc_dir", lambda: None)

    assert cli.main(["--gui-smoke"]) == 2
    assert "user guide is not in this build" in capsys.readouterr().err


def test_the_spec_and_the_wheel_ship_the_guide_where_the_reader_looks() -> None:
    """Both packaging paths put the files at ``agentclip/docs`` - the path
    ``files("agentclip") / "docs"`` resolves, under ``sys._MEIPASS`` for the
    frozen build and in the wheel for an install."""
    spec = (REPO / "packaging" / "agentclip.spec").read_text(encoding="utf-8")
    assert '"agentclip/docs"' in spec, "the guide's destination is not the package path"
    assert "page_datas + doc_datas" in spec, "collected but never handed to Analysis"

    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for name, _ in DOC_PAGES:
        assert f'"docs/{name}.md" = "agentclip/docs/{name}.md"' in pyproject


# == the page's half ==========================================================


def test_the_titlebar_carries_the_button_and_the_button_opens_the_viewer() -> None:
    """A visible door, not a shortcut: every function key is taken and a bare
    letter belongs to the main screen's session keys."""
    html = asset("index.html")
    assert 'id="docs-open"' in html
    assert html.index('id="docs-open"') > html.index('class="titlebar"')

    js = asset("app.js")
    assert 'el.docsOpen.addEventListener("click"' in js
    assert 'openPageModal("docs"' in js, "the viewer does not ride the one modal element"


def test_the_viewer_switches_between_the_pages_it_was_handed() -> None:
    """One button, two documents: the switcher is drawn from the event's pages
    rather than from a list of names written out here, so a third page is a
    Python-side change and no JavaScript."""
    js = asset("app.js")
    tabs = js[js.index("function openDocs(") : js.index("function showDoc(")]
    assert "docPages.forEach" in tabs
    assert 'tab.className = "doc-tab"' in tabs
    assert "body.innerHTML = renderMarkdown(page.text);" in js


def test_escaping_happens_before_rendering_and_never_the_other_way() -> None:
    """Both documents write literal <project>, <key> and <name> in tables and
    code spans. escapeHtml runs FIRST inside inlineMarkdown, the fence branch
    escapes its own body, and nothing anywhere puts a document into the DOM
    without going through the renderer."""
    js = asset("app.js")
    assert "var out = escapeHtml(text).replace(SPANS" in js
    assert '"<pre><code>" + escapeHtml(body.join' in js
    # The one insertion of document text into the page, and it is rendered.
    assert not re.search(r"innerHTML\s*=\s*page\.text", js)


def test_the_renderer_covers_what_the_two_documents_actually_use() -> None:
    """Pipe tables (with escaped pipes inside code spans), backtick RUNS,
    backslash escapes, block quotes and links - the five forms the guide needs
    that a transcript never asked for. A renderer that lost one of these would
    print raw markdown at the user, which no test of the Python side can see."""
    js = asset("app.js")
    for piece in (
        "function tableCells(",
        "function renderTable(",
        "function isTableRule(",
        "function codeSpan(",
        "function docLink(",
        "var QUOTE =",
        "var SPANS =",
    ):
        assert piece in js, piece
    # The escaped pipe is resolved by the TABLE, before any inline rule - which
    # is the only way `/armed [on\\|off]` inside a code span in a cell renders.
    cells = js[js.index("function tableCells(") : js.index("function renderTable(")]
    assert 'ch === "\\\\" && text.charAt(i + 1) === "|"' in cells


def test_the_document_body_is_content_and_scrolls_on_its_own() -> None:
    """The switcher stays put while the page under it scrolls, and the body is
    CONTENT - selectable, because a configuration table you cannot copy a key
    out of is half a table (test_chrome pins the user-select exception list, and
    nothing here is on it)."""
    css = asset("app.css")
    assert ".modal.docs" in css
    assert ".doc-body {" in css
    body = css[css.index(".doc-body {") :]
    assert "overflow: auto;" in body[: body.index("}")]
    # A four-column reference table scrolls inside its own box rather than
    # widening the modal past a 400px window (shell.py's minimum).
    assert ".doc-table {" in css
    table = css[css.index(".doc-table {") :]
    assert "overflow-x: auto;" in table[: table.index("}")]
