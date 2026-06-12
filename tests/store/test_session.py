"""SessionStore + prune_sessions: .agentclip/ layout, transcript JSONL, LATEST, pruning."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from agentclip import __version__
from agentclip.store.session import SessionStore, prune_sessions


def _sessions_dir(root: Path) -> Path:
    return root / ".agentclip" / "sessions"


def test_init_creates_layout_meta_and_gitignore(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, service="chatgpt-attach")

    assert store.session_dir.is_dir()
    assert store.session_dir.parent == _sessions_dir(tmp_path)
    assert store.session_dir.name == store.session_id
    # id = local "YYYYmmdd-HHMMSS-" + 4 hex rand
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", store.session_id)

    gitignore = tmp_path / ".agentclip" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == "*\n"

    meta = json.loads((store.session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["schema"] == 1
    assert meta["service"] == "chatgpt-attach"
    assert meta["agentclip_version"] == __version__
    assert Path(meta["root"]) == tmp_path.resolve()
    datetime.fromisoformat(meta["started"])  # valid ISO 8601


def test_gitignore_overwritten_even_if_user_edited_it(tmp_path: Path) -> None:
    SessionStore(tmp_path, service="x", session_id="20260101-000000-aaaa")
    gitignore = tmp_path / ".agentclip" / ".gitignore"
    gitignore.write_text("# edited\n", encoding="utf-8")
    SessionStore(tmp_path, service="x", session_id="20260101-000001-bbbb")
    assert gitignore.read_text(encoding="utf-8") == "*\n"


def test_latest_tracks_most_recent_session(tmp_path: Path) -> None:
    SessionStore(tmp_path, service="x", session_id="20260101-000000-aaaa")
    latest = _sessions_dir(tmp_path) / "LATEST"
    assert latest.read_text(encoding="utf-8").strip() == "20260101-000000-aaaa"

    SessionStore(tmp_path, service="x", session_id="20260101-000001-bbbb")
    assert latest.read_text(encoding="utf-8").strip() == "20260101-000001-bbbb"


def test_transcript_appends_one_valid_json_object_per_line(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, service="x", session_id="20260101-000000-aaaa")
    store.append_event("task", text="fix the bug")
    store.append_event("inbound", raw="===CLIP:EOM===", turn=2)
    store.append_event("decision", call_id=1, verdict="approve", source="user")

    raw = (store.session_dir / "transcript.jsonl").read_bytes()
    assert b"\r" not in raw  # LF line endings
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 3

    events = [json.loads(line) for line in lines]
    assert [e["t"] for e in events] == ["task", "inbound", "decision"]
    assert events[0]["text"] == "fix the bug"
    assert events[1]["raw"] == "===CLIP:EOM===" and events[1]["turn"] == 2
    assert events[2] == {"t": "decision", "ts": events[2]["ts"], "call_id": 1,
                         "verdict": "approve", "source": "user"}
    for event in events:
        datetime.fromisoformat(event["ts"])  # iso8601-local timestamp on every event


def test_write_outbound_dumps_payload_per_turn(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, service="x", session_id="20260101-000000-aaaa")
    path = store.write_outbound(3, "===CLIP:RESULTS turn=3===\nline two\n")
    assert path == store.session_dir / "outbound" / "turn-0003.txt"
    assert path.read_bytes() == b"===CLIP:RESULTS turn=3===\nline two\n"  # verbatim, LF

    # overwrite on re-compose of the same turn
    store.write_outbound(3, "v2\n")
    assert path.read_bytes() == b"v2\n"


def test_prune_sessions_deletes_oldest_beyond_keep_by_id_order(tmp_path: Path) -> None:
    ids = [
        "20260101-000000-aaaa",
        "20260102-000000-bbbb",
        "20260103-000000-cccc",
        "20260104-000000-dddd",
    ]
    # create out of chronological order: pruning must sort by id, not mtime
    for sid in (ids[2], ids[0], ids[3], ids[1]):
        SessionStore(tmp_path, service="x", session_id=sid)

    deleted = prune_sessions(tmp_path, keep=2)
    assert deleted == (ids[0], ids[1])
    assert not (_sessions_dir(tmp_path) / ids[0]).exists()
    assert not (_sessions_dir(tmp_path) / ids[1]).exists()
    assert (_sessions_dir(tmp_path) / ids[2]).is_dir()
    assert (_sessions_dir(tmp_path) / ids[3]).is_dir()
    # the LATEST text file is not a session dir and must survive pruning
    assert (_sessions_dir(tmp_path) / "LATEST").is_file()


def test_prune_sessions_noop_within_keep(tmp_path: Path) -> None:
    SessionStore(tmp_path, service="x", session_id="20260101-000000-aaaa")
    assert prune_sessions(tmp_path, keep=5) == ()
    assert (_sessions_dir(tmp_path) / "20260101-000000-aaaa").is_dir()


def test_prune_sessions_without_data_dir(tmp_path: Path) -> None:
    assert prune_sessions(tmp_path, keep=5) == ()
