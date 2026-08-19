"""The filesystem tools driven against FakeHost - no real disk anywhere.

These are the tests that keep the seam honest: they pass only while every tool
(and the sandbox jail underneath it) reaches the machine through ctx.host. A
handler that quietly opened a Path of its own would fail here with an empty
in-memory filesystem beneath it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import Config, LimitsConfig, caps_for_budget
from agentclip.executor.hosts import FakeHost
from agentclip.executor.tools import fs_tools
from agentclip.executor.tools.registry import ToolContext
from agentclip.executor.tools.sandbox import SandboxViolation, Workspace
from agentclip.protocol.types import ToolCall


def make_call(tool: str, **params: str) -> ToolCall:
    return ToolCall(id=1, tool=tool, params=dict(params), raw="")


@pytest.fixture
def host() -> FakeHost:
    return FakeHost("/project")


@pytest.fixture
def ctx(host: FakeHost) -> ToolContext:
    return ToolContext(
        workspace=Workspace(host.root, Config().excluded_names(), host=host),
        limits=LimitsConfig(),
        caps=caps_for_budget(12_000),
        host=host,
    )


def test_read_file_reads_the_hosts_bytes(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/src/a.py", "alpha\nbravo\n")
    res = fs_tools.read_file(ctx, make_call("read_file", path="src/a.py"))
    assert res.status == "ok"
    assert res.body == "src/a.py lines 1-2 of 2\nalpha\nbravo"


def test_read_file_refuses_binary_from_the_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/blob.bin", b"\x00\x01\x02")
    res = fs_tools.read_file(ctx, make_call("read_file", path="blob.bin"))
    assert res.status == "error" and res.code == "binary_file"


def test_write_file_lands_in_the_host(ctx: ToolContext, host: FakeHost) -> None:
    res = fs_tools.write_file(
        ctx, make_call("write_file", path="new/deep/f.txt", content="hi\n")
    )
    assert res.status == "ok" and "(created)" in res.body
    assert host.text("/project/new/deep/f.txt") == "hi\n"


def test_write_file_append_goes_through_the_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/log.txt", "one\n")
    fs_tools.write_file(ctx, make_call("write_file", path="log.txt", mode="append", content="two\n"))
    assert host.text("/project/log.txt") == "one\ntwo\n"


def test_edit_file_rewrites_through_the_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/src/a.py", "value = OLD\n")
    res = fs_tools.edit_file(
        ctx, make_call("edit_file", path="src/a.py", find="OLD", replace="NEW")
    )
    assert res.status == "ok"
    assert host.text("/project/src/a.py") == "value = NEW\n"


def test_edit_file_preserves_crlf_through_the_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/win.txt", "a\r\nOLD\r\n")
    fs_tools.edit_file(ctx, make_call("edit_file", path="win.txt", find="OLD", replace="NEW"))
    assert host.text("/project/win.txt") == "a\r\nNEW\r\n"


def test_delete_file_removes_it_from_the_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/gone.txt", "x")
    res = fs_tools.delete_file(ctx, make_call("delete_file", path="gone.txt"))
    assert res.status == "ok"
    assert host.stat(Path("/project/gone.txt")) is None


def test_list_dir_reads_the_hosts_listing(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/src/a.py", "abc")
    host.add_file("/project/readme.md", "hello")
    res = fs_tools.list_dir(ctx, make_call("list_dir", path=".", depth="2"))
    assert res.body.split("\n") == ["src/", "  a.py (3 B)", "readme.md (5 B)"]


def test_list_dir_marks_excluded_directories(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/.git/config", "x")
    res = fs_tools.list_dir(ctx, make_call("list_dir", path="."))
    assert res.body == ".git/ (excluded, not listed)"


def test_glob_walks_the_hosts_tree_and_prunes(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/src/a.py", "")
    host.add_file("/project/src/deep/b.py", "")
    host.add_file("/project/.venv/lib/c.py", "")
    res = fs_tools.glob(ctx, make_call("glob", pattern="**/*.py"))
    assert res.body.split("\n") == ["src/a.py", "src/deep/b.py", "2 matches"]


def test_grep_scans_the_hosts_files(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/src/a.py", "def parse():\n    pass\n")
    host.add_file("/project/src/b.py", "nothing here\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="def parse"))
    assert res.body == "src/a.py:1: def parse():"


def test_grep_skips_excluded_subtrees_on_the_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/.venv/lib/x.py", "needle\n")
    host.add_file("/project/src/y.py", "needle\n")
    res = fs_tools.grep(ctx, make_call("grep", pattern="needle"))
    assert res.body == "src/y.py:1: needle"


# -- the jail, drawn on the host's filesystem ----------------------------------


def test_a_symlink_out_of_the_root_is_refused(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/outside/secrets.txt", "s3cret")
    host.add_symlink("/project/link.txt", "/outside/secrets.txt")
    with pytest.raises(SandboxViolation):
        ctx.workspace.resolve_read("link.txt")
    res = fs_tools.read_file(ctx, make_call("read_file", path="link.txt"))
    assert res.status == "error" and res.code == "path_outside_workspace"


def test_a_write_through_a_symlinked_directory_is_refused(
    ctx: ToolContext, host: FakeHost
) -> None:
    host.add_dir("/outside/dir")
    host.add_symlink("/project/escape", "/outside/dir")
    with pytest.raises(SandboxViolation):
        ctx.workspace.resolve_write("escape/new.txt")


def test_a_symlink_inside_the_root_is_allowed(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/real/a.txt", "content")
    host.add_symlink("/project/alias", "/project/real")
    res = fs_tools.read_file(ctx, make_call("read_file", path="alias/a.txt"))
    assert res.status == "ok" and "content" in res.body


# -- case sensitivity belongs to the host, not to the operator's PC ------------


def _glob_body(ctx: ToolContext, pattern: str) -> str:
    res = fs_tools.glob(ctx, make_call("glob", pattern=pattern))
    assert res.status == "ok"
    return res.body


def test_glob_is_case_sensitive_on_a_case_sensitive_host(ctx: ToolContext, host: FakeHost) -> None:
    host.add_file("/project/src/main.py", "x")
    assert _glob_body(ctx, "src/*.PY") == "0 matches"
    assert _glob_body(ctx, "src/*.py") == "src/main.py\n1 matches"


def test_glob_folds_case_on_a_case_insensitive_host() -> None:
    host = FakeHost("/project", case_sensitive=False)
    ctx = ToolContext(
        workspace=Workspace(host.root, Config().excluded_names(), host=host),
        limits=LimitsConfig(),
        caps=caps_for_budget(12_000),
        host=host,
    )
    host.add_file("/project/src/main.py", "x")
    assert _glob_body(ctx, "src/*.PY") == "src/main.py\n1 matches"
