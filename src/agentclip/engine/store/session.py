"""Session persistence: the ``.agentclip/`` data directory (architecture §4).

Layout::

    <project root>/.agentclip/
    ├── .gitignore                 # "*" — written/overwritten on every session start
    └── sessions/
        ├── LATEST                 # text file with the most recent session id (no symlinks)
        └── 20260612-143015-7f3a/  # id = local "YYYYmmdd-HHMMSS-" + 4 hex rand
            ├── meta.json          # {schema, started, service, agentclip_version, root}
            ├── transcript.jsonl   # append-only audit log, one JSON object per line
            ├── outbound/turn-NNNN.txt
            └── backups/...        # owned by engine.store.backups.BackupStore

No session resume in MVP: the transcript is audit-only; backups remain undoable
from disk after a restart (see engine.store.backups).

The tree always lives on the LOCAL disk - it is AgentClip's own state, not the
project's. In a remote session ``<project root>`` is on the far machine, so the
caller passes a ``data_root`` on this PC to hold it instead (cli.py), and the
project root survives only as the ``root`` recorded in meta.json.
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from agentclip import __version__

_SCHEMA = 1


def _now_iso() -> str:
    """ISO 8601 local time with UTC offset, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_text(path: Path, text: str) -> None:
    """Write UTF-8 text verbatim (no newline translation — LF stays LF)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


class SessionStore:
    """Owns one session directory under ``<root>/.agentclip/sessions/``.

    Constructing a SessionStore creates the directory tree, writes ``meta.json``,
    (re)writes ``.agentclip/.gitignore`` so the data dir never lands in the
    user's VCS, and points ``sessions/LATEST`` at the new session.
    """

    project_root: Path
    session_id: str
    session_dir: Path

    def __init__(
        self,
        project_root: Path,
        *,
        service: str,
        session_id: str | None = None,
        data_root: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        # Where the .agentclip tree goes: beside the project, unless the project
        # is on another machine and the caller named a local home for it.
        data_dir = (Path(data_root) if data_root is not None else self.project_root) / ".agentclip"
        sessions_dir = data_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        if session_id is not None:  # deterministic override for tests
            self.session_id = session_id
            self.session_dir = sessions_dir / session_id
            self.session_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.session_id, self.session_dir = _create_session_dir(sessions_dir)

        _write_text(data_dir / ".gitignore", "*\n")

        meta = {
            "schema": _SCHEMA,
            "started": _now_iso(),
            "service": service,
            "agentclip_version": __version__,
            # Resolving is a question for THIS filesystem; a remote root has
            # already been resolved on its own machine and must be left alone
            # (resolve() would graft this PC's drive onto it).
            "root": str(self.project_root if data_root is not None else self.project_root.resolve()),
        }
        _write_text(
            self.session_dir / "meta.json",
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        )
        _write_text(sessions_dir / "LATEST", self.session_id + "\n")

    def append_event(self, t: str, **fields: object) -> None:
        """Append one event to ``transcript.jsonl``: {"t": t, "ts": <iso local>, **fields}.

        Non-JSON-serializable field values fall back to ``str()`` — the audit
        log must never crash the session.
        """
        event: dict[str, object] = {"t": t, "ts": _now_iso(), **fields}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(
            self.session_dir / "transcript.jsonl", "a", encoding="utf-8", newline=""
        ) as f:
            f.write(line + "\n")

    def write_outbound(self, turn: int, text: str) -> Path:
        """Persist the exact composed payload for a turn (chunks pre-joined by the
        caller) to ``outbound/turn-NNNN.txt`` for manual re-copy / postmortem."""
        outbound_dir = self.session_dir / "outbound"
        outbound_dir.mkdir(exist_ok=True)
        path = outbound_dir / f"turn-{turn:04d}.txt"
        _write_text(path, text)
        return path


def _create_session_dir(sessions_dir: Path) -> tuple[str, Path]:
    """Create a fresh uniquely-named session dir; retry on the (rare) collision."""
    for _ in range(16):
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        candidate = sessions_dir / sid
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return sid, candidate
    raise RuntimeError(f"could not create a unique session directory under {sessions_dir}")


def prune_sessions(data_root: Path, keep: int) -> tuple[str, ...]:
    """Delete the oldest session directories beyond ``keep``; return deleted ids.

    ``data_root`` is where the ``.agentclip`` tree lives - the project root for
    a local session, the local mirror for a remote one (see the module docstring).

    Session ids start with a local timestamp, so lexicographic order is
    chronological. Sessions that cannot be removed (e.g. a file held open on
    Windows) are skipped and not reported as deleted.
    """
    sessions_dir = Path(data_root) / ".agentclip" / "sessions"
    if not sessions_dir.is_dir():
        return ()
    ids = sorted(p.name for p in sessions_dir.iterdir() if p.is_dir())
    excess = len(ids) - max(keep, 0)
    if excess <= 0:
        return ()
    deleted: list[str] = []
    for sid in ids[:excess]:
        try:
            shutil.rmtree(sessions_dir / sid)
        except OSError:
            continue
        deleted.append(sid)
    return tuple(deleted)
