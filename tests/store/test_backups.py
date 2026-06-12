"""BackupStore: copy-on-first-touch snapshots, manifests, undo, restart safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentclip.store.backups import BackupStore, UndoReport


@pytest.fixture()
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """(project root, session dir) mimicking <root>/.agentclip/sessions/<id>."""
    root = tmp_path / "proj"
    session_dir = root / ".agentclip" / "sessions" / "20260612-120000-abcd"
    session_dir.mkdir(parents=True)
    return root, session_dir


def _manifest(session_dir: Path, turn: int) -> dict:
    path = session_dir / "backups" / f"turn-{turn:04d}" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── write path ──────────────────────────────────────────────────────────────


def test_copy_on_first_touch_is_idempotent(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "src" / "utils.py"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("src/utils.py", target)
    target.write_text("first write\n", encoding="utf-8")
    store.snapshot_before_write("src/utils.py", target)  # 2nd touch same turn: no-op
    target.write_text("second write\n", encoding="utf-8")
    store.finish_turn()

    manifest = _manifest(session_dir, 1)
    assert manifest["schema"] == 1
    assert manifest["turn"] == 1
    assert len(manifest["entries"]) == 1
    entry = manifest["entries"][0]
    assert entry["path"] == "src/utils.py"
    assert entry["action"] == "modified"
    assert entry["backup"] == "files/src/utils.py"
    assert entry["sha256_before"]
    # the backup holds the PRE-TURN bytes, not any intermediate write
    backup = session_dir / "backups" / "turn-0001" / "files" / "src" / "utils.py"
    assert backup.read_text(encoding="utf-8") == "original\n"
    assert store.touched_paths() == ("src/utils.py",)


def test_manifest_paths_use_forward_slashes(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "src" / "utils.py"
    target.parent.mkdir(parents=True)
    target.write_text("x\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("src\\utils.py", target)  # Windows-flavored rel
    store.snapshot_before_write("src/utils.py", target)  # same file: still first-touch
    store.finish_turn()

    entries = _manifest(session_dir, 1)["entries"]
    assert len(entries) == 1
    assert entries[0]["path"] == "src/utils.py"
    assert entries[0]["backup"] == "files/src/utils.py"
    assert store.touched_paths() == ("src/utils.py",)


def test_finish_turn_with_no_entries_writes_nothing(workspace: tuple[Path, Path]) -> None:
    _, session_dir = workspace
    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.finish_turn()
    assert not (session_dir / "backups").exists()
    assert store.latest_undoable_turn() is None
    assert not store.has_turn(1)


def test_finish_turn_leaves_no_tmp_file(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "a.txt"
    target.write_text("x\n", encoding="utf-8")
    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("a.txt", target)
    store.finish_turn()
    turn_dir = session_dir / "backups" / "turn-0001"
    assert (turn_dir / "manifest.json").is_file()
    assert list(turn_dir.glob("*.tmp")) == []


def test_snapshot_without_begin_turn_raises(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    store = BackupStore(session_dir)
    with pytest.raises(RuntimeError):
        store.snapshot_before_write("a.txt", root / "a.txt")


def test_snapshot_before_delete_of_missing_file_is_noop(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_delete("ghost.txt", root / "ghost.txt")
    store.finish_turn()
    assert store.touched_paths() == ()
    assert not store.has_turn(1)


# ── undo ────────────────────────────────────────────────────────────────────


def test_undo_modified_restores_bytes(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "src" / "utils.py"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("src/utils.py", target)
    target.write_text("llm version\n", encoding="utf-8")
    store.finish_turn()

    report = store.undo_turn(1)
    assert target.read_text(encoding="utf-8") == "original\n"
    assert report == UndoReport(
        turn=1, restored=("src/utils.py",), deleted=(), recreated=(), warnings=()
    )


def test_undo_created_deletes_file_and_now_empty_dirs(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    keeper = root / "existing" / "keep.txt"
    keeper.parent.mkdir(parents=True)
    keeper.write_text("keep\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(2)

    new_deep = root / "newpkg" / "sub" / "mod.py"
    store.snapshot_before_write("newpkg/sub/mod.py", new_deep)
    new_deep.parent.mkdir(parents=True)
    new_deep.write_text("print('hi')\n", encoding="utf-8")

    new_beside = root / "existing" / "made.txt"
    store.snapshot_before_write("existing/made.txt", new_beside)
    new_beside.write_text("made\n", encoding="utf-8")

    store.finish_turn()
    report = store.undo_turn(2)

    assert not new_deep.exists()
    assert not (root / "newpkg").exists()  # the whole created tree is gone
    assert root.is_dir()  # ...but never the workspace root
    assert not new_beside.exists()
    assert keeper.read_text(encoding="utf-8") == "keep\n"  # pre-existing dir untouched
    assert (root / "existing").is_dir()
    assert sorted(report.deleted) == ["existing/made.txt", "newpkg/sub/mod.py"]
    assert report.restored == () and report.recreated == () and report.warnings == ()


def test_undo_deleted_recreates_file(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "old.txt"
    target.write_text("precious bytes\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_delete("old.txt", target)
    target.unlink()
    store.finish_turn()

    assert _manifest(session_dir, 1)["entries"][0]["action"] == "deleted"
    report = store.undo_turn(1)
    assert target.read_text(encoding="utf-8") == "precious bytes\n"
    assert report.recreated == ("old.txt",)
    assert report.deleted == () and report.restored == () and report.warnings == ()


def test_undo_mixed_turn(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    modified = root / "mod.txt"
    modified.write_text("before\n", encoding="utf-8")
    doomed = root / "gone.txt"
    doomed.write_text("bye\n", encoding="utf-8")
    created = root / "made.txt"

    store = BackupStore(session_dir)
    store.begin_turn(7)
    store.snapshot_before_write("mod.txt", modified)
    modified.write_text("after\n", encoding="utf-8")
    store.snapshot_before_write("made.txt", created)
    created.write_text("new\n", encoding="utf-8")
    store.snapshot_before_delete("gone.txt", doomed)
    doomed.unlink()
    store.finish_turn()

    assert store.touched_paths() == ("mod.txt", "made.txt", "gone.txt")
    report = store.undo_turn(7)
    assert modified.read_text(encoding="utf-8") == "before\n"
    assert not created.exists()
    assert doomed.read_text(encoding="utf-8") == "bye\n"
    assert report.restored == ("mod.txt",)
    assert report.deleted == ("made.txt",)
    assert report.recreated == ("gone.txt",)
    assert report.warnings == ()


def test_undo_warns_on_sha_mismatch_but_proceeds(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "f.txt"
    target.write_text("original\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("f.txt", target)
    target.write_text("llm version\n", encoding="utf-8")
    store.finish_turn()

    target.write_text("user hand edit\n", encoding="utf-8")  # changed since the turn
    report = store.undo_turn(1)

    assert target.read_text(encoding="utf-8") == "original\n"  # restored anyway
    assert report.restored == ("f.txt",)
    assert len(report.warnings) == 1
    assert "f.txt" in report.warnings[0]
    assert "sha mismatch" in report.warnings[0]


def test_undo_warns_on_missing_backup_file(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "f.txt"
    target.write_text("original\n", encoding="utf-8")

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("f.txt", target)
    target.write_text("llm version\n", encoding="utf-8")
    store.finish_turn()

    (session_dir / "backups" / "turn-0001" / "files" / "f.txt").unlink()
    report = store.undo_turn(1)

    assert target.read_text(encoding="utf-8") == "llm version\n"  # nothing to restore from
    assert report.restored == ()
    assert any("backup file missing" in w for w in report.warnings)


def test_undo_created_already_absent_warns(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    created = root / "made.txt"

    store = BackupStore(session_dir)
    store.begin_turn(1)
    store.snapshot_before_write("made.txt", created)
    created.write_text("new\n", encoding="utf-8")
    store.finish_turn()

    created.unlink()  # user removed it before undoing
    report = store.undo_turn(1)
    assert report.deleted == ()
    assert any("already absent" in w for w in report.warnings)


def test_undo_unknown_turn_raises(workspace: tuple[Path, Path]) -> None:
    _, session_dir = workspace
    store = BackupStore(session_dir)
    with pytest.raises(FileNotFoundError):
        store.undo_turn(42)


# ── restart safety: everything read from disk ───────────────────────────────


def test_undo_from_fresh_instance_after_restart(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "f.txt"
    target.write_text("original\n", encoding="utf-8")

    writer = BackupStore(session_dir)
    writer.begin_turn(3)
    writer.snapshot_before_write("f.txt", target)
    target.write_text("llm version\n", encoding="utf-8")
    writer.finish_turn()
    del writer  # "restart": no in-memory state survives

    fresh = BackupStore(session_dir)
    assert fresh.has_turn(3)
    assert fresh.latest_undoable_turn() == 3

    report = fresh.undo_turn(3)
    assert target.read_text(encoding="utf-8") == "original\n"
    assert report.restored == ("f.txt",)

    # undone turn drops out of the undoable set, but its bytes stay for audit
    assert not fresh.has_turn(3)
    assert fresh.latest_undoable_turn() is None
    turn_dir = session_dir / "backups" / "turn-0003"
    assert (turn_dir / "manifest.undone.json").is_file()
    assert (turn_dir / "files" / "f.txt").is_file()


def test_newest_first_undo_chain_across_turns(workspace: tuple[Path, Path]) -> None:
    root, session_dir = workspace
    target = root / "f.txt"
    target.write_text("v0\n", encoding="utf-8")

    store = BackupStore(session_dir)
    for turn, content in ((1, "v1\n"), (2, "v2\n")):
        store.begin_turn(turn)
        store.snapshot_before_write("f.txt", target)
        target.write_text(content, encoding="utf-8")
        store.finish_turn()

    assert store.latest_undoable_turn() == 2
    assert store.undo_turn(2).warnings == ()
    assert target.read_text(encoding="utf-8") == "v1\n"

    assert store.latest_undoable_turn() == 1
    assert store.undo_turn(1).warnings == ()
    assert target.read_text(encoding="utf-8") == "v0\n"

    assert store.latest_undoable_turn() is None
