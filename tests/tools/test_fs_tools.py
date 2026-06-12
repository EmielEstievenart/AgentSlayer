"""Semantics tests for the filesystem tools (protocol.md section 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import BudgetCaps, Config, LimitsConfig, caps_for_budget
from agentclip.protocol.types import ToolCall
from agentclip.tools import fs_tools
from agentclip.tools.registry import ToolContext
from agentclip.tools.sandbox import Workspace


def make_call(tool: str, **params: str) -> ToolCall:
    return ToolCall(id=1, tool=tool, params=dict(params), raw="")


def make_ctx(
    root: Path,
    *,
    limits: LimitsConfig | None = None,
    caps: BudgetCaps | None = None,
    backup_hook=None,
) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root, Config().excluded_names()),
        limits=limits or LimitsConfig(),
        caps=caps or caps_for_budget(12_000),
        backup_hook=backup_hook,
    )


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return make_ctx(tmp_path)


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")
    return p


# -- read_file ----------------------------------------------------------------


def test_read_full_file_header_and_no_gutter(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "alpha\nbravo\ncharlie\n")
    res = fs_tools.read_file(ctx, make_call("read_file", path="f.txt"))
    assert res.status == "ok"
    lines = res.body.split("\n")
    assert lines[0] == "f.txt lines 1-3 of 3"
    assert lines[1:] == ["alpha", "bravo", "charlie"]  # raw lines, no line-number gutter


def test_read_range_one_based_inclusive(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "".join(f"line{i}\n" for i in range(1, 11)))
    res = fs_tools.read_file(ctx, make_call("read_file", path="f.txt", start="3", end="5"))
    lines = res.body.split("\n")
    assert lines[0] == "f.txt lines 3-5 of 10"
    assert lines[1:] == ["line3", "line4", "line5"]


def test_read_range_clamped_with_note(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "".join(f"line{i}\n" for i in range(1, 11)))
    res = fs_tools.read_file(ctx, make_call("read_file", path="f.txt", start="8", end="99"))
    assert res.status == "ok"
    assert res.body.split("\n")[0] == "f.txt lines 8-10 of 10"
    assert "[note: requested lines 8-99 clamped to 8-10]" in res.body


def test_read_default_span_capped(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, caps=caps_for_budget(4_000))  # span = 120 lines
    _write(tmp_path, "big.txt", "".join(f"line{i}\n" for i in range(1, 131)))
    res = fs_tools.read_file(ctx, make_call("read_file", path="big.txt"))
    lines = res.body.split("\n")
    assert lines[0] == "big.txt lines 1-120 of 130"
    assert "line120" in res.body and "line121" not in res.body
    assert "[truncated:" in lines[-1]


def test_read_char_cap_cuts_on_line_boundary(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, limits=LimitsConfig(max_file_read_chars=500))
    _write(tmp_path, "wide.txt", "".join("x" * 100 + "\n" for _ in range(20)))
    res = fs_tools.read_file(ctx, make_call("read_file", path="wide.txt"))
    assert res.status == "ok"
    assert "[truncated:" in res.body and "char cap" in res.body
    content = res.body.split("\n")[1:-1]
    assert all(line == "x" * 100 for line in content)
    assert len(content) <= 5


def test_read_binary_file_error(ctx: ToolContext, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02PNG")
    res = fs_tools.read_file(ctx, make_call("read_file", path="blob.bin"))
    assert res.status == "error" and res.code == "binary_file"
    assert res.body.splitlines()[-1].startswith("hint: ")


def test_read_missing_file_and_missing_param(ctx: ToolContext) -> None:
    res = fs_tools.read_file(ctx, make_call("read_file", path="nope.txt"))
    assert res.status == "error" and res.code == "file_not_found"
    res = fs_tools.read_file(ctx, make_call("read_file"))
    assert res.status == "error" and res.code == "missing_param"
    assert "path" in res.body
    assert res.body.splitlines()[-1].startswith("hint: ")


def test_read_escape_maps_to_path_outside_workspace(ctx: ToolContext) -> None:
    res = fs_tools.read_file(ctx, make_call("read_file", path="../etc/passwd"))
    assert res.status == "error" and res.code == "path_outside_workspace"
    assert res.body.splitlines()[-1].startswith("hint: ")


# -- write_file ----------------------------------------------------------------


def test_write_create_makes_parents(ctx: ToolContext, tmp_path: Path) -> None:
    call = make_call("write_file", path="a/b/new.py", mode="create", content="x = 1\ny = 2\n")
    res = fs_tools.write_file(ctx, call)
    assert res.status == "ok"
    assert res.body == "wrote 2 lines (12 chars) to a/b/new.py (created)"
    assert (tmp_path / "a" / "b" / "new.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_write_create_errors_if_exists(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "old\n")
    res = fs_tools.write_file(ctx, make_call("write_file", path="f.txt", mode="create", content="new\n"))
    assert res.status == "error" and res.code == "bad_param"
    assert "exists" in res.body
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "old\n"


def test_write_overwrite_and_append(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "old\n")
    res = fs_tools.write_file(ctx, make_call("write_file", path="f.txt", content="new\n"))
    assert res.status == "ok" and "(overwritten)" in res.body
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\n"

    res = fs_tools.write_file(ctx, make_call("write_file", path="f.txt", mode="append", content="more\n"))
    assert res.status == "ok" and "(appended)" in res.body
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\nmore\n"


def test_write_bad_mode(ctx: ToolContext) -> None:
    res = fs_tools.write_file(ctx, make_call("write_file", path="f.txt", mode="clobber", content=""))
    assert res.status == "error" and res.code == "bad_param"


def test_write_backup_hook_called_before_touch(tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "old\n")
    seen: list[tuple[str, str, str]] = []

    def hook(rel: str, abs_path: Path, action: str) -> None:
        # the hook must fire BEFORE the file is touched
        seen.append((rel, abs_path.read_text(encoding="utf-8"), action))

    ctx = make_ctx(tmp_path, backup_hook=hook)
    fs_tools.write_file(ctx, make_call("write_file", path="f.txt", content="new\n"))
    assert seen == [("f.txt", "old\n", "write")]


# -- edit_file -----------------------------------------------------------------


def test_edit_exactly_once(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "a = 1\nb = 2\nc = 3\n")
    res = fs_tools.edit_file(ctx, make_call("edit_file", path="u.py", find="b = 2", replace="b = 20"))
    assert res.status == "ok"
    assert res.body == "replaced 1 occurrence at line 2"
    assert (tmp_path / "u.py").read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"


def test_edit_multiple_matches_lists_line_numbers(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "x = 0\nfoo()\ny = 1\nfoo()\n")
    res = fs_tools.edit_file(ctx, make_call("edit_file", path="u.py", find="foo()", replace="bar()"))
    assert res.status == "error" and res.code == "multiple_matches"
    assert "lines 2, 4" in res.body
    assert res.body.splitlines()[-1].startswith("hint: ")
    assert "foo()" in (tmp_path / "u.py").read_text(encoding="utf-8")  # unchanged


def test_edit_occurrence_selection(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "foo()\nfoo()\nfoo()\n")
    res = fs_tools.edit_file(
        ctx, make_call("edit_file", path="u.py", find="foo()", replace="bar()", occurrence="2")
    )
    assert res.status == "ok" and "at line 2" in res.body
    assert (tmp_path / "u.py").read_text(encoding="utf-8") == "foo()\nbar()\nfoo()\n"

    res = fs_tools.edit_file(
        ctx, make_call("edit_file", path="u.py", find="foo()", replace="baz()", occurrence="all")
    )
    assert res.status == "ok"
    assert res.body.startswith("replaced 2 occurrences at lines 1, 3")
    assert (tmp_path / "u.py").read_text(encoding="utf-8") == "baz()\nbar()\nbaz()\n"


def test_edit_occurrence_first_and_bad_values(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "foo()\nfoo()\n")
    res = fs_tools.edit_file(
        ctx, make_call("edit_file", path="u.py", find="foo()", replace="bar()", occurrence="first")
    )
    assert res.status == "ok" and "at line 1" in res.body

    res = fs_tools.edit_file(
        ctx, make_call("edit_file", path="u.py", find="foo()", replace="x", occurrence="9")
    )
    assert res.status == "error" and res.code == "bad_param"
    res = fs_tools.edit_file(
        ctx, make_call("edit_file", path="u.py", find="foo()", replace="x", occurrence="zeroth")
    )
    assert res.status == "error" and res.code == "bad_param"


def test_edit_not_found_includes_near_miss(ctx: ToolContext, tmp_path: Path) -> None:
    _write(
        tmp_path,
        "u.py",
        "import datetime\n\n\ndef parse_date(s):\n    # NOTE: legacy format\n"
        '    return datetime.strptime(s, "%d/%m/%Y")\n',
    )
    res = fs_tools.edit_file(
        ctx,
        make_call(
            "edit_file",
            path="u.py",
            find='def parse_date(s):\n    return datetime.strptime(s, "%d/%m/%Y")',
            replace="x",
        ),
    )
    assert res.status == "error" and res.code == "match_not_found"
    assert "Closest near-miss at lines" in res.body
    assert "def parse_date(s):" in res.body  # the region itself is quoted
    assert res.body.splitlines()[-1].startswith("hint: ")


def test_edit_trailing_whitespace_fallback(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "def f():   \n    return 1\n")
    res = fs_tools.edit_file(
        ctx,
        make_call("edit_file", path="u.py", find="def f():\n    return 1", replace="def f():\n    return 2"),
    )
    assert res.status == "ok"
    assert "ignoring trailing whitespace" in res.body
    assert (tmp_path / "u.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_edit_backup_hook_called(tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "a\n")
    seen: list[tuple[str, str]] = []
    ctx = make_ctx(tmp_path, backup_hook=lambda rel, p, action: seen.append((rel, action)))
    fs_tools.edit_file(ctx, make_call("edit_file", path="u.py", find="a", replace="b"))
    assert seen == [("u.py", "write")]


def test_edit_preserves_crlf(ctx: ToolContext, tmp_path: Path) -> None:
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    res = fs_tools.edit_file(ctx, make_call("edit_file", path="w.txt", find="two", replace="2"))
    assert res.status == "ok"
    assert (tmp_path / "w.txt").read_bytes() == b"one\r\n2\r\n"


# -- delete_file ---------------------------------------------------------------


def test_delete_with_backup_hook(tmp_path: Path) -> None:
    _write(tmp_path, "old.py", "gone\n")
    seen: list[tuple[str, bool, str]] = []

    def hook(rel: str, abs_path: Path, action: str) -> None:
        seen.append((rel, abs_path.exists(), action))  # must still exist at hook time

    ctx = make_ctx(tmp_path, backup_hook=hook)
    res = fs_tools.delete_file(ctx, make_call("delete_file", path="old.py"))
    assert res.status == "ok"
    assert res.body == "deleted old.py (backed up)"
    assert not (tmp_path / "old.py").exists()
    assert seen == [("old.py", True, "delete")]


def test_delete_missing(ctx: ToolContext) -> None:
    res = fs_tools.delete_file(ctx, make_call("delete_file", path="nope.py"))
    assert res.status == "error" and res.code == "file_not_found"


# -- list_dir -------------------------------------------------------------------


def test_list_dir_tree_sizes_and_exclusions(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "x" * 100)
    _write(tmp_path, "src/sub/deep.py", "y\n")
    (tmp_path / ".git").mkdir()
    _write(tmp_path, ".git/config", "secret\n")
    res = fs_tools.list_dir(ctx, make_call("list_dir", depth="2"))
    assert res.status == "ok"
    assert ".git/ (excluded, not listed)" in res.body
    assert "config" not in res.body  # nothing under .git listed
    assert "src/" in res.body
    assert "app.py (100 B)" in res.body
    assert "sub/" in res.body
    assert "deep.py" not in res.body  # level 3 needs depth 3


def test_list_dir_depth_3_and_clamp(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "a/b/c/d.txt", "deep\n")
    res = fs_tools.list_dir(ctx, make_call("list_dir", depth="9"))
    assert res.status == "ok"
    assert "c/" in res.body
    assert "d.txt" not in res.body  # depth clamped to 3
    assert "[note: depth 9 clamped to 3" in res.body


def test_list_dir_cap(tmp_path: Path) -> None:
    caps = BudgetCaps(600, 100, 250, 12_000, listing_max_entries=3, advised_max_calls=8)
    ctx = make_ctx(tmp_path, caps=caps)
    for i in range(6):
        _write(tmp_path, f"f{i}.txt", "x\n")
    res = fs_tools.list_dir(ctx, make_call("list_dir"))
    assert res.status == "ok"
    assert "[truncated: listing capped at 3 entries" in res.body


# -- glob -------------------------------------------------------------------------


def test_glob_recursive_with_footer_and_exclusions(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a\n")
    _write(tmp_path, "src/pkg/b.py", "b\n")
    _write(tmp_path, "node_modules/evil.py", "e\n")
    res = fs_tools.glob(ctx, make_call("glob", pattern="**/*.py"))
    assert res.status == "ok"
    lines = res.body.split("\n")
    assert lines[-1] == "2 matches"
    assert "src/a.py" in lines and "src/pkg/b.py" in lines
    assert all("node_modules" not in line for line in lines)


def test_glob_cap_with_truncation_marker(tmp_path: Path) -> None:
    caps = BudgetCaps(600, 100, 250, 12_000, listing_max_entries=2, advised_max_calls=8)
    ctx = make_ctx(tmp_path, caps=caps)
    for i in range(4):
        _write(tmp_path, f"m{i}.txt", "x\n")
    res = fs_tools.glob(ctx, make_call("glob", pattern="*.txt"))
    assert "[truncated: showing first 2 of 4 matches" in res.body
    assert res.body.split("\n")[-1] == "4 matches"


def test_glob_rejects_absolute_and_dotdot_patterns(ctx: ToolContext) -> None:
    for pattern in ("C:/evil/*.py", "/etc/*", "../*.py"):
        res = fs_tools.glob(ctx, make_call("glob", pattern=pattern))
        assert res.status == "error" and res.code == "bad_param"


# -- grep -------------------------------------------------------------------------


def test_grep_format_and_line_numbers(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "src/u.py", "import os\n\ndef parse_date(s):\n    return s\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern=r"def parse_\w+"))
    assert res.status == "ok"
    assert res.body == "src/u.py:3: def parse_date(s):"


def test_grep_context_lines_use_dash(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "one\ntwo\nthree\nfour\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="three", context="1"))
    assert res.body.split("\n") == [
        "f.txt:2- two",
        "f.txt:3: three",
        "f.txt:4- four",
    ]


def test_grep_ignore_case_and_glob_filter(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "HELLO = 1\n")
    _write(tmp_path, "b.txt", "hello again\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="hello", ignore_case="yes", glob="*.py"))
    assert res.body == "a.py:1: HELLO = 1"
    res = fs_tools.grep(ctx, make_call("grep", pattern="hello", ignore_case="true"))
    assert "a.py:1:" in res.body and "b.txt:1:" in res.body


def test_grep_bad_regex(ctx: ToolContext) -> None:
    res = fs_tools.grep(ctx, make_call("grep", pattern="(unclosed"))
    assert res.status == "error" and res.code == "bad_param"
    assert "regex" in res.body
    assert res.body.splitlines()[-1].startswith("hint: ")


def test_grep_cap_and_truncation(tmp_path: Path) -> None:
    caps = BudgetCaps(600, grep_max_hits=2, command_tail_lines=250, command_tail_chars=12_000,
                      listing_max_entries=400, advised_max_calls=8)
    ctx = make_ctx(tmp_path, caps=caps)
    _write(tmp_path, "f.txt", "hit\nhit\nhit\nhit\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="hit"))
    assert res.body.count(": hit") == 2
    assert "[truncated: showing first 2 matches" in res.body


def test_grep_max_param_tightens_cap(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "hit\nhit\nhit\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="hit", max="1"))
    assert res.body.count(": hit") == 1
    assert "[truncated:" in res.body


def test_grep_skips_excluded_and_binary(ctx: ToolContext, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _write(tmp_path, ".git/config", "needle\n")
    (tmp_path / "blob.bin").write_bytes(b"needle\x00needle")
    _write(tmp_path, "ok.txt", "needle\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="needle"))
    assert res.body == "ok.txt:1: needle"


def test_grep_no_matches(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "f.txt", "nothing here\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="absent_term"))
    assert res.status == "ok" and res.body == "no matches"


# -- previews ---------------------------------------------------------------------


def test_preview_edit_is_unified_diff(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "a = 1\nb = 2\n")
    text = fs_tools.preview_edit_file(
        ctx, make_call("edit_file", path="u.py", find="b = 2", replace="b = 3")
    )
    assert "--- a/u.py" in text and "+++ b/u.py" in text
    assert "-b = 2" in text and "+b = 3" in text


def test_preview_edit_failure_is_explained(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "u.py", "a = 1\n")
    text = fs_tools.preview_edit_file(
        ctx, make_call("edit_file", path="u.py", find="zz", replace="yy")
    )
    assert text.startswith("(edit will fail: match_not_found)")


def test_preview_write_new_file_and_overwrite_diff(ctx: ToolContext, tmp_path: Path) -> None:
    text = fs_tools.preview_write_file(
        ctx, make_call("write_file", path="new.py", content="x = 1\n")
    )
    assert text.startswith("NEW FILE new.py (1 lines)")
    assert "x = 1" in text

    _write(tmp_path, "old.py", "a = 1\n")
    text = fs_tools.preview_write_file(
        ctx, make_call("write_file", path="old.py", content="a = 2\n")
    )
    assert "-a = 1" in text and "+a = 2" in text


def test_preview_delete(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, "old.py", "one\ntwo\n")
    assert fs_tools.preview_delete_file(ctx, make_call("delete_file", path="old.py")) == (
        "DELETE old.py (2 lines)"
    )
