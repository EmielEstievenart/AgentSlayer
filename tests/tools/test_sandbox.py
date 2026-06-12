"""Sandbox escape tests: the four-step check in tools/sandbox.py."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agentclip.config import Config
from agentclip.tools.sandbox import SandboxViolation, Workspace


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as td:
        try:
            os.symlink(td, os.path.join(td, "probe"))
        except (OSError, NotImplementedError):
            return False
    return True


needs_symlinks = pytest.mark.skipif(
    not _symlinks_supported(), reason="OS/user does not permit creating symlinks"
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "utils.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".agentclip").mkdir()
    (tmp_path / ".agentclip" / "secrets.txt").write_text("s\n", encoding="utf-8")
    return Workspace(tmp_path, Config().excluded_names())


# -- step 1: shape ---------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "/etc/passwd",  # POSIX absolute (must be rejected even on Windows)
        "C:\\Windows\\system32\\drivers",  # Windows absolute
        "C:/Windows/notepad.exe",
        "c:relative-to-drive.txt",  # drive designator without separator
        "\\\\server\\share\\file.txt",  # UNC backslash
        "//server/share/file.txt",  # UNC forward-slash
        "fo\x00o.txt",  # NUL byte
        "\\rooted\\windows\\path.txt",  # rooted (drive-less) Windows path
    ],
)
def test_shape_rejected_for_read_and_write(ws: Workspace, rel: str) -> None:
    with pytest.raises(SandboxViolation):
        ws.resolve_read(rel)
    with pytest.raises(SandboxViolation):
        ws.resolve_write(rel)


# -- step 2/3: traversal and containment ------------------------------------


def test_dotdot_escape_rejected(ws: Workspace) -> None:
    with pytest.raises(SandboxViolation):
        ws.resolve_read("../outside.txt")
    with pytest.raises(SandboxViolation):
        ws.resolve_write("../outside.txt")


def test_dotdot_escape_through_existing_dir_rejected(ws: Workspace) -> None:
    with pytest.raises(SandboxViolation):
        ws.resolve_write("src/../../escape.txt")


def test_dotdot_in_nonexistent_write_tail_rejected(ws: Workspace) -> None:
    # "newdir" does not exist, so ".." lands in the unresolvable tail.
    with pytest.raises(SandboxViolation):
        ws.resolve_write("newdir/../../escape.txt")


def test_internal_dotdot_that_stays_inside_is_fine(ws: Workspace) -> None:
    p = ws.resolve_read("src/../README.md")
    assert p == ws.root / "README.md"


def test_plain_read_and_write_resolve(ws: Workspace) -> None:
    assert ws.resolve_read("src/utils.py") == ws.root / "src" / "utils.py"
    assert ws.resolve_write("a/b/new.txt") == ws.root / "a" / "b" / "new.txt"


def test_backslash_separators_accepted(ws: Workspace) -> None:
    assert ws.resolve_read("src\\utils.py") == ws.root / "src" / "utils.py"


def test_write_to_root_itself_rejected(ws: Workspace) -> None:
    with pytest.raises(SandboxViolation):
        ws.resolve_write(".")


def test_missing_root_raises_at_construction(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        Workspace(tmp_path / "does-not-exist", frozenset())


# -- step 4: exclusions ------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".git/config",
        ".agentclip/secrets.txt",
        ".agentclip.toml",
        "node_modules/pkg/index.js",
        "src/node_modules/x.js",  # excluded component anywhere in the path
    ],
)
def test_excluded_rejected_for_read_and_write(ws: Workspace, rel: str) -> None:
    with pytest.raises(SandboxViolation):
        ws.resolve_read(rel)
    with pytest.raises(SandboxViolation):
        ws.resolve_write(rel)


def test_is_excluded_helper(ws: Workspace) -> None:
    assert ws.is_excluded(ws.root / ".git" / "config")
    assert ws.is_excluded(ws.root / ".agentclip")
    assert ws.is_excluded(ws.root.parent / "elsewhere.txt")  # outside root counts
    assert not ws.is_excluded(ws.root / "src" / "utils.py")
    assert not ws.is_excluded(ws.root)


# -- symlinks -----------------------------------------------------------------


@needs_symlinks
def test_symlink_dir_out_of_root_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    os.symlink(outside, root / "link_out", target_is_directory=True)
    ws = Workspace(root, Config().excluded_names())

    with pytest.raises(SandboxViolation):
        ws.resolve_read("link_out/secret.txt")
    with pytest.raises(SandboxViolation):
        ws.resolve_write("link_out/new.txt")  # write THROUGH an out-of-root symlinked dir


@needs_symlinks
def test_symlink_file_out_of_root_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("t\n", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    os.symlink(outside / "target.txt", root / "alias.txt")
    ws = Workspace(root, Config().excluded_names())

    with pytest.raises(SandboxViolation):
        ws.resolve_read("alias.txt")
    with pytest.raises(SandboxViolation):
        ws.resolve_write("alias.txt")


@needs_symlinks
def test_in_root_symlink_allowed(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.txt").write_text("f\n", encoding="utf-8")
    os.symlink(root / "real", root / "alias", target_is_directory=True)
    ws = Workspace(root, Config().excluded_names())

    assert ws.resolve_read("alias/f.txt") == ws.root / "real" / "f.txt"
    assert ws.resolve_write("alias/new.txt") == ws.root / "real" / "new.txt"


@needs_symlinks
def test_broken_symlink_in_write_path_rejected(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    os.symlink(root / "gone", root / "dangling")
    ws = Workspace(root, Config().excluded_names())

    with pytest.raises(SandboxViolation):
        ws.resolve_write("dangling/new.txt")
