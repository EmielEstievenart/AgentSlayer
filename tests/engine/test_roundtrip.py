"""The M1 exit criterion: a ScriptedLLM drives the whole headless agent loop -
real files on disk, zero clipboard - plus the protocol edge-case scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.engine.engine import Done, Engine, NewTurn, Noise, Send
from agentclip.engine.states import Decision, EngineStateError, Phase


def _utils_py(project: Path) -> str:
    return (project / "src" / "utils.py").read_text(encoding="utf-8")


class ScriptedLLM:
    """Maps expected-outbound substrings to canned CLIP reply strings."""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self._script = list(script)

    def reply(self, outbound_text: str) -> str:
        assert self._script, "ScriptedLLM ran out of canned replies"
        expected, response = self._script.pop(0)
        assert expected in outbound_text, (
            f"outbound did not contain {expected!r}:\n{outbound_text[:500]}"
        )
        return response


REPLY_FIX_AND_VERIFY = """I'll read the file, apply the ISO fix, and verify it runs.

~~~~
===CLIP:CALL id=1 tool=read_file===
path: src/utils.py
===CLIP:END===
===CLIP:CALL id=2 tool=edit_file===
path: src/utils.py
find <<EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace <<EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
===CLIP:CALL id=3 tool=run_command===
command: python -c "print(601)"
===CLIP:END===
===CLIP:EOM calls=3 turn=1===
~~~~
"""

REPLY_TASK_DONE = """All good - the edit applied and the command ran.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Changed parse_date in src/utils.py to ISO format (%Y-%m-%d).
Verified with a python command.
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=2===
~~~~
"""


def test_full_roundtrip(project: Path, make_engine) -> None:
    (project / ".agentclip.toml").write_text(
        '[approval]\ncommand_allowlist = ["python -c*"]\n', encoding="utf-8"
    )
    engine = make_engine()
    llm = ScriptedLLM(
        [
            ("Fix the date parsing bug", REPLY_FIX_AND_VERIFY),
            ("replaced 1 occurrence", REPLY_TASK_DONE),
        ]
    )

    out = engine.start_task("Fix the date parsing bug in src/utils.py: use ISO dates.")
    assert out.kind == "bootstrap" and len(out.chunks) == 1

    result = engine.ingest(llm.reply(out.chunks[0]))
    assert isinstance(result, NewTurn)
    assert [c.tool for c in result.reply.calls] == ["read_file", "edit_file", "run_command"]

    # Only the edit gates: read_file is auto, the command matched "python -c*".
    pend = engine.pending()
    assert [p.call.id for p in pend] == [2]
    assert pend[0].kind == "edit"
    assert '"%Y-%m-%d"' in pend[0].preview  # unified diff carries the replacement
    engine.decide(2, Decision.APPROVE)
    assert engine.all_decided()

    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULTS turn=2===" in payload
    assert payload.count("status=ok") == 3
    assert "replaced 1 occurrence" in payload
    assert "601" in payload  # run_command output made it into the results
    assert payload.rstrip().endswith("===CLIP:EOM turn=2===")

    # THE EDIT IS ON DISK.
    on_disk = (project / "src" / "utils.py").read_text(encoding="utf-8")
    assert '"%Y-%m-%d"' in on_disk and "%d/%m/%Y" not in on_disk

    # outbound payload persisted for manual re-copy / postmortem
    session_dir = engine.status().session_dir
    assert (session_dir / "outbound" / "turn-0002.txt").is_file()

    result2 = engine.ingest(llm.reply(payload))
    assert isinstance(result2, NewTurn)
    step2 = engine.execute()
    assert isinstance(step2, Done)
    assert "ISO format" in step2.summary
    assert step2.outbound is None
    assert engine.status().phase is Phase.DONE


REPLY_REJECTION_TURN = """===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace <<EOT
    return None
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: src/extra.py
content <<EOT
x = 1
EOT
===CLIP:END===
===CLIP:CALL id=3 tool=run_command===
command: pytest -q
===CLIP:END===
===CLIP:CALL id=4 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=4 turn=1===
"""


def test_rejection_aborts_rest_of_turn(project: Path, engine: Engine) -> None:
    original = _utils_py(project)
    engine.start_task("t")
    assert isinstance(engine.ingest(REPLY_REJECTION_TURN), NewTurn)
    assert [p.call.id for p in engine.pending()] == [1, 2]

    engine.decide(1, Decision.REJECT, note="wrong approach, keep the parser")
    assert engine.pending() == ()  # the other gated call was aborted with the turn
    assert engine.all_decided()

    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=denied===" in payload
    assert "user: wrong approach, keep the parser" in payload
    assert "===CLIP:RESULT id=2 status=skipped===" in payload
    assert "===CLIP:RESULT id=3 status=skipped===" in payload
    assert "===CLIP:RESULT id=4 status=skipped===" in payload
    assert "turn aborted after a rejection" in payload

    # nothing ran: no edit, no new file
    assert _utils_py(project) == original
    assert not (project / "src" / "extra.py").exists()


UNKNOWN_TOOL_REPLY = """===CLIP:CALL id=1 tool=frobnicate===
target: src/utils.py
===CLIP:END===
===CLIP:CALL id=2 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=2 turn=1===
"""


def test_unknown_tool_pre_resolved_error(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(UNKNOWN_TOOL_REPLY), NewTurn)
    assert engine.pending() == ()  # pre-resolved errors are never pending
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "===CLIP:RESULT id=1 status=error code=unknown_tool===" in payload
    assert "'frobnicate'" in payload
    assert "read_file" in payload and "task_done" in payload  # valid names listed
    assert "===CLIP:RESULT id=2 status=ok===" in payload  # sibling still executed


TRUNCATED_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: notes.txt
content <<EOT
this heredoc never terminates and the reply just stops"""


def test_truncated_reply_gets_id0_result(project: Path, engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(TRUNCATED_REPLY)
    assert isinstance(result, NewTurn)
    assert result.reply.truncated
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    id0 = payload.index("===CLIP:RESULT id=0 status=error code=reply_truncated===")
    id1 = payload.index("===CLIP:RESULT id=1 status=ok===")
    assert id0 < id1  # the truncation notice leads the payload
    assert "resend call id=2" in payload
    assert "code=unterminated_heredoc" in payload  # the partial call's own result
    assert not (project / "notes.txt").exists()  # the partial call did NOT run


DUPLICATE_IDS_REPLY = """===CLIP:CALL id=5 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:CALL id=5 tool=read_file===
path: src/utils.py
===CLIP:END===
===CLIP:EOM calls=2 turn=1===
"""


def test_renumbered_ids_surface_as_note(engine: Engine) -> None:
    engine.start_task("t")
    result = engine.ingest(DUPLICATE_IDS_REPLY)
    assert isinstance(result, NewTurn)
    assert [c.id for c in result.reply.calls] == [1, 2]
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "===CLIP:NOTE===" in payload
    assert "renumbered" in payload
    assert "===CLIP:RESULT id=1 status=ok===" in payload
    assert "===CLIP:RESULT id=2 status=ok===" in payload


SAME_PATH_REPLY = """===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
THIS TEXT DOES NOT EXIST ANYWHERE
EOT
replace <<EOT
irrelevant
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=write_file===
path: src/utils.py
content <<EOT
overwritten!
EOT
===CLIP:END===
===CLIP:EOM calls=2 turn=1===
"""


def test_same_path_skip_after_failed_edit(project: Path, engine: Engine) -> None:
    original = _utils_py(project)
    engine.start_task("t")
    assert isinstance(engine.ingest(SAME_PATH_REPLY), NewTurn)
    for action in engine.pending():
        engine.decide(action.call.id, Decision.APPROVE)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "code=match_not_found" in payload
    assert "===CLIP:RESULT id=2 status=skipped===" in payload
    assert "prior edit of this file failed" in payload
    assert _utils_py(project) == original


EDIT_FOR_UNDO_REPLY = """===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace <<EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
===CLIP:EOM calls=1 turn=1===
"""


def test_undo_last_turn_restores_file_and_notifies(project: Path, engine: Engine) -> None:
    original = _utils_py(project)
    engine.start_task("t")
    assert isinstance(engine.ingest(EDIT_FOR_UNDO_REPLY), NewTurn)
    engine.decide(1, Decision.APPROVE)
    assert isinstance(engine.execute(), Send)
    assert '"%Y-%m-%d"' in _utils_py(project)

    report, notice = engine.undo_last_turn()
    assert report.turn == 1
    assert report.restored == ("src/utils.py",)
    assert _utils_py(project) == original
    assert notice is not None
    assert notice.kind == "note"
    assert "reverted turn 1" in notice.chunks[0]
    assert "src/utils.py" in notice.chunks[0]
    assert notice.turn == 3  # bootstrap=1, results=2, notice=3
    assert engine.status().turn == 3

    with pytest.raises(EngineStateError, match="nothing to undo"):
        engine.undo_last_turn()


def test_undo_without_notice(project: Path, engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(EDIT_FOR_UNDO_REPLY), NewTurn)
    engine.decide(1, Decision.APPROVE)
    engine.execute()
    report, notice = engine.undo_last_turn(compose_notice=False)
    assert notice is None
    assert report.restored == ("src/utils.py",)
    assert engine.status().turn == 2  # no notice payload, turn unchanged


def test_duplicate_ingest_is_noise(engine: Engine) -> None:
    engine.start_task("t")
    assert isinstance(engine.ingest(EDIT_FOR_UNDO_REPLY), NewTurn)
    engine.decide(1, Decision.APPROVE)
    engine.execute()
    again = engine.ingest(EDIT_FOR_UNDO_REPLY)
    assert isinstance(again, Noise) and again.reason == "duplicate"
