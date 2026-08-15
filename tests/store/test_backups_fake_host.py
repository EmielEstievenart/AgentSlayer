"""Backups and undo against a non-local host.

The backups themselves always land on the local disk; the project files they
mirror do not. These tests put the project on a FakeHost - an in-memory
filesystem that is nothing like the one the session directory is on - so a
BackupStore that quietly read or wrote a project file with pathlib fails here.
"""

from __future__ import annotations

from pathlib import Path

from agentclip.executor.hosts.fake import FakeHost
from agentclip.store.backups import BackupStore

ROOT = "/project"


def _store(session_dir: Path) -> tuple[BackupStore, FakeHost]:
    host = FakeHost(ROOT)
    return BackupStore(session_dir, host=host), host


def test_undo_restores_a_modified_file_onto_the_host(tmp_path: Path) -> None:
    store, host = _store(tmp_path / "session")
    host.add_file(f"{ROOT}/src/utils.py", "original\n")

    store.begin_turn(1)
    store.snapshot_before_write("src/utils.py", Path(f"{ROOT}/src/utils.py"))
    host.write_bytes(Path(f"{ROOT}/src/utils.py"), b"rewritten\n")
    store.finish_turn()

    report = store.undo_turn(1)
    assert report.restored == ("src/utils.py",)
    assert report.warnings == ()
    assert host.text(f"{ROOT}/src/utils.py") == "original\n"


def test_undo_deletes_a_created_file_and_prunes_its_directory(tmp_path: Path) -> None:
    store, host = _store(tmp_path / "session")

    store.begin_turn(1)
    store.snapshot_before_write("pkg/new.py", Path(f"{ROOT}/pkg/new.py"))
    host.write_bytes(Path(f"{ROOT}/pkg/new.py"), b"print()\n")
    store.finish_turn()

    report = store.undo_turn(1)
    assert report.deleted == ("pkg/new.py",)
    assert host.stat(Path(f"{ROOT}/pkg/new.py")) is None
    # the directory the turn brought into being goes with it...
    assert host.stat(Path(f"{ROOT}/pkg")) is None
    # ...but never the workspace root itself
    assert host.stat(Path(ROOT)) is not None


def test_undo_keeps_a_directory_that_still_holds_something(tmp_path: Path) -> None:
    store, host = _store(tmp_path / "session")
    host.add_file(f"{ROOT}/pkg/keep.py", "keep\n")

    store.begin_turn(1)
    store.snapshot_before_write("pkg/new.py", Path(f"{ROOT}/pkg/new.py"))
    host.write_bytes(Path(f"{ROOT}/pkg/new.py"), b"print()\n")
    store.finish_turn()

    store.undo_turn(1)
    assert host.stat(Path(f"{ROOT}/pkg")) is not None
    assert host.text(f"{ROOT}/pkg/keep.py") == "keep\n"


def test_undo_recreates_a_deleted_file_and_its_directory(tmp_path: Path) -> None:
    store, host = _store(tmp_path / "session")
    host.add_file(f"{ROOT}/docs/old.md", "history\n")

    store.begin_turn(1)
    store.snapshot_before_delete("docs/old.md", Path(f"{ROOT}/docs/old.md"))
    host.delete(Path(f"{ROOT}/docs/old.md"))
    host.rmdir(Path(f"{ROOT}/docs"))
    store.finish_turn()

    report = store.undo_turn(1)
    assert report.recreated == ("docs/old.md",)
    assert host.text(f"{ROOT}/docs/old.md") == "history\n"


def test_undo_warns_when_the_host_file_changed_since_the_turn(tmp_path: Path) -> None:
    store, host = _store(tmp_path / "session")
    host.add_file(f"{ROOT}/a.txt", "one\n")

    store.begin_turn(1)
    store.snapshot_before_write("a.txt", Path(f"{ROOT}/a.txt"))
    host.write_bytes(Path(f"{ROOT}/a.txt"), b"two\n")
    store.finish_turn()

    host.write_bytes(Path(f"{ROOT}/a.txt"), b"three\n")  # somebody else, later
    report = store.undo_turn(1)
    assert report.restored == ("a.txt",)
    assert any("changed since turn 1" in w for w in report.warnings)
    assert host.text(f"{ROOT}/a.txt") == "one\n"


def test_manifest_root_is_written_with_forward_slashes(tmp_path: Path) -> None:
    """A POSIX root must survive the manifest even when written from Windows."""
    import json

    session_dir = tmp_path / "session"
    store, host = _store(session_dir)
    host.add_file(f"{ROOT}/src/a.txt", "x\n")

    store.begin_turn(1)
    store.snapshot_before_write("src/a.txt", Path(f"{ROOT}/src/a.txt"))
    store.finish_turn()

    manifest = json.loads(
        (session_dir / "backups" / "turn-0001" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["root"] == ROOT
