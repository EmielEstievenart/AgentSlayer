"""Sandbox escape tests: the four-step check in tools/sandbox.py."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agentclip.config import Config
from agentclip.executor.tools.sandbox import SandboxViolation, Workspace


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
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}\n", encoding="utf-8")
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
        ".agentclip/secrets.txt",
        ".agentclip.toml",
        "src/.agentclip/notes.txt",  # sealed component anywhere in the path
    ],
)
def test_hard_excluded_rejected_for_read_and_write(ws: Workspace, rel: str) -> None:
    """The two sealed names: the model may neither read nor write its own rules."""
    with pytest.raises(SandboxViolation):
        ws.resolve_read(rel)
    with pytest.raises(SandboxViolation):
        ws.resolve_write(rel)


@pytest.mark.parametrize(
    "rel",
    [
        ".git/config",
        ".vscode/settings.json",
        "node_modules/pkg/index.js",
        "src/node_modules/x.js",  # excluded component anywhere in the path
    ],
)
def test_configured_excludes_are_readable_but_not_writable(ws: Workspace, rel: str) -> None:
    """`paths.exclude` is budget hygiene, not secrecy: name the file and you get it."""
    assert ws.resolve_read(rel) == ws.root.joinpath(*rel.split("/"))
    with pytest.raises(SandboxViolation):
        ws.resolve_write(rel)


def test_the_hard_floor_holds_even_if_the_caller_omits_it(tmp_path: Path) -> None:
    """A Workspace built from a bare exclude list still seals .agentclip."""
    (tmp_path / ".agentclip").mkdir()
    ws = Workspace(tmp_path, ["node_modules"])
    assert ".agentclip" in ws.excludes
    with pytest.raises(SandboxViolation):
        ws.resolve_read(".agentclip/x.txt")
    with pytest.raises(SandboxViolation):
        ws.resolve_write(".agentclip/x.txt")
    assert ws.is_excluded(ws.root / ".agentclip")


def test_is_excluded_helper(ws: Workspace) -> None:
    """Traversal still skips the FULL merged set - readability changes nothing here."""
    assert ws.is_excluded(ws.root / ".git" / "config")
    assert ws.is_excluded(ws.root / ".vscode" / "settings.json")
    assert ws.is_excluded(ws.root / ".agentclip")
    assert ws.is_excluded(ws.root.parent / "elsewhere.txt")  # outside root counts
    assert not ws.is_excluded(ws.root / "src" / "utils.py")
    assert not ws.is_excluded(ws.root)


# -- extra read roots (the skill-folder carve-out) -----------------------------


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    """A skill folder OUTSIDE the project root, as a home-rooted one always is."""
    folder = tmp_path / "home" / ".claude" / "skills" / "deploy"
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: deploy\n---\nship it\n", encoding="utf-8")
    (folder / "reference.md").write_text("ref\n", encoding="utf-8")
    (folder / "scripts" / "check.py").write_text("print(1)\n", encoding="utf-8")
    return folder


@pytest.fixture
def carved(tmp_path: Path, skills: Path) -> Workspace:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "utils.py").write_text("x = 1\n", encoding="utf-8")
    return Workspace(root, Config().excluded_names(), extra_read_roots=(skills,))


def test_absolute_read_inside_an_extra_root_is_allowed(carved: Workspace, skills: Path) -> None:
    """The whole point: a skill saying "run scripts/check.py" is now actionable."""
    assert carved.resolve_read(str(skills / "scripts" / "check.py")) == (
        skills / "scripts" / "check.py"
    ).resolve()
    assert carved.resolve_read(str(skills / "reference.md")) == (skills / "reference.md").resolve()


def test_the_extra_root_itself_resolves(carved: Workspace, skills: Path) -> None:
    assert carved.resolve_read(str(skills)) == skills.resolve()


def test_relative_paths_still_mean_the_project_root(carved: Workspace) -> None:
    """A skill folder never shadows a project file: only ABSOLUTE reaches it."""
    assert carved.resolve_read("src/utils.py") == carved.root / "src" / "utils.py"
    # `reference.md` exists in the skill folder and nowhere else; relative, it
    # still resolves to the project's own (missing) file, which the caller then
    # reports as file_not_found.
    assert carved.resolve_read("reference.md") == carved.root / "reference.md"


def test_dotdot_out_of_an_extra_root_rejected(carved: Workspace, skills: Path) -> None:
    """Containment is asked of the RESOLVED path, so the carve-out is not a tunnel."""
    with pytest.raises(SandboxViolation):
        carved.resolve_read(str(skills / ".." / ".." / ".." / "secret.txt"))


@needs_symlinks
def test_symlink_out_of_an_extra_root_rejected(carved: Workspace, skills: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n", encoding="utf-8")
    os.symlink(outside, skills / "link_out", target_is_directory=True)
    with pytest.raises(SandboxViolation):
        carved.resolve_read(str(skills / "link_out" / "secret.txt"))


def test_sealed_names_hold_inside_an_extra_root(carved: Workspace, skills: Path) -> None:
    """The hard floor is not the project root's alone."""
    (skills / ".agentclip").mkdir()
    (skills / ".agentclip" / "notes.txt").write_text("n\n", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        carved.resolve_read(str(skills / ".agentclip" / "notes.txt"))
    with pytest.raises(SandboxViolation):
        carved.resolve_read(str(skills / ".agentclip.toml"))


def test_an_unrelated_absolute_path_is_still_refused(carved: Workspace, tmp_path: Path) -> None:
    (tmp_path / "loose.txt").write_text("l\n", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        carved.resolve_read(str(tmp_path / "loose.txt"))
    with pytest.raises(SandboxViolation):
        carved.resolve_read("/etc/passwd")


def test_writes_into_an_extra_root_say_why(carved: Workspace, skills: Path) -> None:
    """write/edit/delete all land on resolve_write, and it names the reason."""
    for target in (skills / "reference.md", skills / "new.txt", skills / "scripts" / "check.py"):
        with pytest.raises(SandboxViolation) as exc_info:
            carved.resolve_write(str(target))
        assert "read-only" in exc_info.value.detail


def test_traversal_does_not_extend_into_extra_roots(carved: Workspace, skills: Path) -> None:
    """list_dir/glob/grep resolve through resolve_scan, which knows no carve-out."""
    with pytest.raises(SandboxViolation):
        carved.resolve_scan(str(skills))
    assert carved.is_excluded(skills / "reference.md")  # and a sweep would skip it anyway


def test_a_vanished_extra_root_is_dropped_not_raised(tmp_path: Path) -> None:
    """A skill folder deleted between discovery and session start costs the
    carve-out, never the session."""
    root = tmp_path / "proj"
    root.mkdir()
    ws = Workspace(root, frozenset(), extra_read_roots=(tmp_path / "gone",))
    assert ws.extra_read_roots == ()
    with pytest.raises(SandboxViolation):
        ws.resolve_read(str(tmp_path / "gone" / "x.md"))


def test_without_extra_roots_absolute_reads_refuse_exactly_as_before(ws: Workspace) -> None:
    with pytest.raises(SandboxViolation) as exc_info:
        ws.resolve_read("/etc/passwd")
    assert "absolute paths are not allowed" in exc_info.value.detail


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
