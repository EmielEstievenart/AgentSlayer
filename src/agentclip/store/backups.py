"""Per-turn copy-on-first-touch backups + undo (architecture §5).

Layout, inside one session directory::

    backups/turn-NNNN/
    ├── manifest.json          # written atomically by finish_turn (tmp + os.replace)
    └── files/<rel>            # mirrored relative paths, pre-change bytes (shutil.copy2)

Manifest schema::

    {"schema": 1, "turn": 3, "root": "<abs workspace root>",
     "entries": [{"path": "src/utils.py",        # forward slashes, cross-platform
                  "action": "modified" | "created" | "deleted",
                  "backup": "files/src/utils.py" | null,   # null for "created"
                  "sha256_before": "..." | null,           # hash of the backup copy
                  "sha256_after": "..." | null}]}          # hash at finish_turn time

``root`` is derived from the first ``(rel, abs_path)`` snapshot pair so undo can
map relative manifest paths back to absolute files without a Workspace handle.
``sha256_after`` records what the turn left on disk; undo compares it against
the current content to produce the "file changed since" warning.

After a successful undo the manifest is renamed to ``manifest.undone.json``:
the turn drops out of ``has_turn()`` / ``latest_undoable_turn()``, which makes
newest-first undo chains restart-safe (everything is read from disk), while the
backed-up bytes stay on disk for the audit trail.

Honest limitation (surfaced in the TUI, not here): only file-tool changes are
backed up; ``run_command`` side effects are outside the manifest.

The backups themselves always live on the local disk (they are AgentClip's own
state), but the project files they mirror are read AND restored through the
Host, so a session working on a remote machine gets a local, restorable copy of
every byte it overwrote and can put it back. Restoring needs the two directory
primitives the seam carries for exactly this (``mkdir``/``rmdir``): a restored
file may need its directory back, and an undone creation may leave one empty.
Metadata (mtime/mode) is NOT restored - the seam moves bytes, not stat blocks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from agentclip.executor.hosts.base import Host
from agentclip.executor.hosts.local import LocalHost

_SCHEMA = 1
_MANIFEST = "manifest.json"
_MANIFEST_UNDONE = "manifest.undone.json"
_TURN_DIR_RE = re.compile(r"^turn-(\d{4,})$")


@dataclass(frozen=True, slots=True)
class UndoReport:
    turn: int
    restored: tuple[str, ...]  # modified files restored from backup
    deleted: tuple[str, ...]  # created-by-LLM files removed
    recreated: tuple[str, ...]  # deleted-by-LLM files restored
    warnings: tuple[str, ...]  # e.g. sha mismatch (file changed since), missing backup


@dataclass(slots=True)
class _Entry:
    path: str
    action: str  # "modified" | "created" | "deleted"
    backup: str | None
    sha256_before: str | None
    sha256_after: str | None = None


def _sha256(path: Path) -> str:
    """Hash a LOCAL file (a backup copy, or a file being undone)."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _norm_rel(rel: str) -> str:
    """Normalize a workspace-relative path to forward slashes (manifest form)."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"bad relative path for backup: {rel!r}")
    return "/".join(parts)


class BackupStore:
    """Copy-on-first-touch snapshots per turn, with disk-backed undo.

    Write path (M1): ``begin_turn`` → ``snapshot_before_write`` /
    ``snapshot_before_delete`` (the tool layer calls these right before each
    mutation) → ``finish_turn``. Only finished turns are undoable: the manifest
    is the commit point.
    """

    session_dir: Path
    backups_dir: Path

    def __init__(self, session_dir: Path, *, host: Host | None = None) -> None:
        self.session_dir = Path(session_dir)
        self.backups_dir = self.session_dir / "backups"
        # Where the PROJECT files live; the backups always land locally.
        self._host: Host = host if host is not None else LocalHost()
        self._turn: int | None = None
        self._root: Path | None = None
        self._entries: list[_Entry] = []
        self._abs: dict[str, Path] = {}  # first-touch set: rel -> abs path

    # ── write path ────────────────────────────────────────────────────────

    def begin_turn(self, turn: int) -> None:
        self._turn = turn
        self._root = None
        self._entries = []
        self._abs = {}

    def snapshot_before_write(self, rel: str, abs_path: Path) -> None:
        """Call immediately before creating/overwriting/editing ``abs_path``."""
        self._snapshot(rel, abs_path, deleting=False)

    def snapshot_before_delete(self, rel: str, abs_path: Path) -> None:
        """Call immediately before deleting ``abs_path``."""
        self._snapshot(rel, abs_path, deleting=True)

    def _snapshot(self, rel: str, abs_path: Path, *, deleting: bool) -> None:
        if self._turn is None:
            raise RuntimeError("begin_turn() must be called before snapshotting")
        key = _norm_rel(rel)
        if key in self._abs:
            return  # copy-on-first-touch: the first snapshot is the turn baseline
        abs_path = Path(abs_path)
        if self._root is None:
            # abs_path == root / key, so the workspace root is this many parents up.
            self._root = abs_path.parents[key.count("/")]

        st = self._host.stat(abs_path)
        if st is not None and st.is_file:
            backup_rel = "files/" + key
            dest = self._turn_dir(self._turn) / backup_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self._host.read_bytes(abs_path))
            entry = _Entry(
                path=key,
                action="deleted" if deleting else "modified",
                backup=backup_rel,
                sha256_before=_sha256(dest),  # hash the copy: exactly what we can restore
            )
        elif deleting:
            return  # deleting a file that does not exist: nothing to back up or undo
        else:
            entry = _Entry(path=key, action="created", backup=None, sha256_before=None)
        self._abs[key] = abs_path
        self._entries.append(entry)

    def finish_turn(self) -> None:
        """Write ``manifest.json`` atomically (tmp + os.replace); no-op if the
        turn touched nothing. Current-turn state survives until ``begin_turn``."""
        if self._turn is None or not self._entries:
            return
        for entry in self._entries:
            abs_path = self._abs[entry.path]
            st = self._host.stat(abs_path)
            entry.sha256_after = (
                hashlib.sha256(self._host.read_bytes(abs_path)).hexdigest()
                if st is not None and st.is_file
                else None
            )
        turn_dir = self._turn_dir(self._turn)
        turn_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": _SCHEMA,
            "turn": self._turn,
            # Forward slashes: the root may be a POSIX path on a remote machine,
            # and str() of one on Windows would spell it with backslashes.
            "root": self._root.as_posix() if self._root is not None else None,
            "entries": [asdict(e) for e in self._entries],
        }
        tmp = turn_dir / (_MANIFEST + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, turn_dir / _MANIFEST)

    def touched_paths(self) -> tuple[str, ...]:
        """Relative paths touched in the current turn, in first-touch order
        (for the TUI backup notice)."""
        return tuple(e.path for e in self._entries)

    # ── read/undo path (everything below reads from disk: restart-safe) ──

    def has_turn(self, turn: int) -> bool:
        return (self._turn_dir(turn) / _MANIFEST).is_file()

    def latest_undoable_turn(self) -> int | None:
        if not self.backups_dir.is_dir():
            return None
        turns = [
            int(m.group(1))
            for p in self.backups_dir.iterdir()
            if p.is_dir() and (m := _TURN_DIR_RE.match(p.name)) and (p / _MANIFEST).is_file()
        ]
        return max(turns, default=None)

    def undo_turn(self, turn: int) -> UndoReport:
        """Revert one finished turn (newest-first; the engine enforces order).

        modified → restore backup bytes (warn on changed-since sha, proceed);
        created → delete the file and prune now-empty parent dirs up to the
        workspace root; deleted → restore from backup. Raises FileNotFoundError
        if the turn has no (still-undoable) manifest.
        """
        turn_dir = self._turn_dir(turn)
        manifest_path = turn_dir / _MANIFEST
        if not manifest_path.is_file():
            raise FileNotFoundError(f"no undoable backup manifest for turn {turn}: {manifest_path}")
        with open(manifest_path, "rb") as f:
            manifest = json.load(f)
        root = Path(manifest["root"])

        restored: list[str] = []
        deleted: list[str] = []
        recreated: list[str] = []
        warnings: list[str] = []

        for entry in reversed(manifest["entries"]):
            rel: str = entry["path"]
            action: str = entry["action"]
            target = root.joinpath(*rel.split("/"))
            st = self._host.stat(target)
            if st is not None and not st.is_file:
                warnings.append(f"{rel}: not a regular file anymore; skipped")
                continue
            current_sha = self._host_sha256(target) if st is not None else None
            sha_after = entry.get("sha256_after")

            if action in ("modified", "deleted"):
                backup = turn_dir / entry["backup"] if entry.get("backup") else None
                if backup is None or not backup.is_file():
                    warnings.append(f"{rel}: backup file missing; cannot restore")
                    continue
                if action == "modified":
                    if current_sha is None:
                        warnings.append(
                            f"{rel}: missing (changed since turn {turn}); restoring from backup"
                        )
                    elif current_sha != sha_after:
                        warnings.append(
                            f"{rel}: changed since turn {turn} (sha mismatch); "
                            "restoring from backup anyway"
                        )
                elif current_sha is not None:  # deleted, but something recreated it since
                    warnings.append(
                        f"{rel}: exists again (changed since turn {turn}); "
                        "overwriting with backup"
                    )
                self._host.mkdir(target.parent)
                self._host.write_bytes(target, backup.read_bytes())
                (restored if action == "modified" else recreated).append(rel)
            elif action == "created":
                if current_sha is None:
                    warnings.append(f"{rel}: already absent; nothing to delete")
                    self._remove_empty_parents(target, root)
                    continue
                if current_sha != sha_after:
                    warnings.append(
                        f"{rel}: changed since turn {turn} (sha mismatch); deleting anyway"
                    )
                self._host.delete(target)
                deleted.append(rel)
                self._remove_empty_parents(target, root)
            else:
                warnings.append(f"{rel}: unknown manifest action {action!r}; skipped")

        # Mark undone on disk (keeps the bytes for audit, drops the turn from
        # the undoable set) so newest-first chains survive a restart.
        os.replace(manifest_path, turn_dir / _MANIFEST_UNDONE)
        return UndoReport(
            turn=turn,
            restored=tuple(restored),
            deleted=tuple(deleted),
            recreated=tuple(recreated),
            warnings=tuple(warnings),
        )

    # ── helpers ───────────────────────────────────────────────────────────

    def _turn_dir(self, turn: int) -> Path:
        return self.backups_dir / f"turn-{turn:04d}"

    def _host_sha256(self, path: Path) -> str:
        """Hash a PROJECT file - on whichever machine the project lives on."""
        return hashlib.sha256(self._host.read_bytes(path)).hexdigest()

    def _remove_empty_parents(self, target: Path, root: Path) -> None:
        """Remove now-empty parent directories of an undone created file, walking
        up but never past (or including) the workspace root."""
        cur = target.parent
        while cur != root and root in cur.parents:
            try:
                self._host.rmdir(cur)  # fails on non-empty: that is the stop condition
            except OSError:
                return
            cur = cur.parent
