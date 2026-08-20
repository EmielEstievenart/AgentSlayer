"""The ranged-edit guard: replace_lines may only touch lines the model was
actually shown, last turn (protocol.md section 3.1).

`edit_file` is self-verifying - a find-block that does not match refuses.
`replace_lines` is not: "lines 88-90" is true of every file with ninety lines,
so every one of these tests is about a way the model could otherwise write real
code into the wrong place and be told it worked.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine, NewTurn, Send
from agentclip.engine.numbered import describe_ranges, surviving_numbered_lines
from agentclip.executor.tools.registry import default_registry
from agentclip.protocol.composer import Composer
from agentclip.protocol.types import ToolResult

from ..conftest import CHAT_NAME, write_permissions

LINES_PY = "".join(f"line{i}\n" for i in range(1, 21))


@pytest.fixture
def ranged_engine(project: Path, make_engine) -> Engine:
    """An engine whose service has edit_by_lines on, with edits ungated so the
    turn runs to completion without a gate in the way."""
    write_permissions(project, {"permission": {"edit": {"*": "allow"}}})
    (project / "lines.py").write_text(LINES_PY, encoding="utf-8", newline="")
    cfg = load_config(project, global_config_path=project / "no-such-global.toml")
    services = dict(cfg.services)
    key = cfg.general.service
    services[key] = replace(services[key], edit_by_lines=True)
    cfg = replace(cfg, services=services)
    return make_engine(config=cfg, tools=default_registry(edit_by_lines=True))


def _reply(*calls: str) -> str:
    body = "\n".join(calls)
    return f"~~~~\n{body}\n===CLIP:EOM calls={len(calls)} chat={CHAT_NAME}===\n~~~~\n"


def _read(call_id: int, path: str = "lines.py", **params: str) -> str:
    extra = "".join(f"{k}: {v}\n" for k, v in params.items())
    return f"===CLIP:CALL id={call_id} tool=read_file===\npath: {path}\nnumbered: yes\n{extra}===CLIP:END==="


def _replace(call_id: int, start: int, end: int, text: str, path: str = "lines.py") -> str:
    return (
        f"===CLIP:CALL id={call_id} tool=replace_lines===\n"
        f"path: {path}\nstart: {start}\nend: {end}\n"
        f"replace << EOT\n{text}\nEOT\n===CLIP:END==="
    )


def _run(engine: Engine, reply: str) -> str:
    """Ingest one reply, run its whole plan, return the outbound payload text."""
    step = engine.ingest(reply)
    assert isinstance(step, NewTurn), step
    out = engine.execute()
    assert isinstance(out, Send), out
    return out.outbound.chunks[0]


# -- the happy path -----------------------------------------------------------


def test_a_range_read_numbered_last_turn_may_be_replaced(
    ranged_engine: Engine, project: Path
) -> None:
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    payload = _run(ranged_engine, _reply(_replace(1, 3, 4, "THREE\nFOUR")))
    assert "replaced lines 3-4 of lines.py (2 lines -> 2 lines)" in payload
    text = (project / "lines.py").read_text(encoding="utf-8")
    assert text.splitlines()[:5] == ["line1", "line2", "THREE", "FOUR", "line5"]


# -- refusal (a): never read numbered ----------------------------------------


def test_a_file_never_read_numbered_cannot_be_ranged_edited(
    ranged_engine: Engine, project: Path
) -> None:
    ranged_engine.start_task("edit by line numbers")
    payload = _run(ranged_engine, _reply(_replace(1, 3, 4, "NOPE")))
    assert "code=unverified_range" in payload
    assert "numbered: yes" in payload
    assert (project / "lines.py").read_text(encoding="utf-8") == LINES_PY


def test_a_plain_read_is_not_a_numbered_read(ranged_engine: Engine, project: Path) -> None:
    """The gutter is the evidence. Without it the model has line numbers only
    by counting, which is exactly the mistake this refuses."""
    ranged_engine.start_task("edit by line numbers")
    _run(
        ranged_engine,
        _reply("===CLIP:CALL id=1 tool=read_file===\npath: lines.py\n===CLIP:END==="),
    )
    payload = _run(ranged_engine, _reply(_replace(1, 3, 4, "NOPE")))
    assert "code=unverified_range" in payload
    assert (project / "lines.py").read_text(encoding="utf-8") == LINES_PY


# -- refusal (b): outside what was shown --------------------------------------


def test_a_range_outside_the_read_span_is_refused(ranged_engine: Engine, project: Path) -> None:
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1, start="5", end="8")))
    payload = _run(ranged_engine, _reply(_replace(1, 7, 12, "NOPE")))
    assert "code=unverified_range" in payload
    assert "5-8" in payload  # the body names what WAS shown
    assert (project / "lines.py").read_text(encoding="utf-8") == LINES_PY


# -- refusal (c): the file moved ---------------------------------------------


def test_a_file_changed_since_the_read_is_refused(ranged_engine: Engine, project: Path) -> None:
    """Any edit anywhere renumbers what is below it, so a changed file
    invalidates every range - not just overlapping ones."""
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    (project / "lines.py").write_text("prepended\n" + LINES_PY, encoding="utf-8", newline="")
    payload = _run(ranged_engine, _reply(_replace(1, 3, 4, "NOPE")))
    assert "code=stale_read" in payload
    assert (project / "lines.py").read_text(encoding="utf-8") == "prepended\n" + LINES_PY


# -- refusal (d): the ordering rules -----------------------------------------


def test_two_edits_to_one_file_must_go_bottom_to_top(
    ranged_engine: Engine, project: Path
) -> None:
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    payload = _run(
        ranged_engine,
        _reply(_replace(1, 3, 3, "THREE"), _replace(2, 9, 9, "NINE")),
    )
    assert "code=bad_edit_order" in payload
    # The first (higher-numbered-later) call still ran; only the ascending one
    # was refused - a refusal does not abort the turn.
    text = (project / "lines.py").read_text(encoding="utf-8").splitlines()
    assert text[2] == "THREE"
    assert text[8] == "line9"


def test_bottom_to_top_edits_both_land_on_the_right_lines(
    ranged_engine: Engine, project: Path
) -> None:
    """The whole reason for the rule: an applied edit only touches HIGHER
    numbers, so the lower range that has not run yet is still correct - even
    when the first edit changes the file's length."""
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    payload = _run(
        ranged_engine,
        _reply(_replace(1, 15, 16, "FIFTEEN"), _replace(2, 3, 3, "THREE\nEXTRA")),
    )
    assert "code=" not in payload
    text = (project / "lines.py").read_text(encoding="utf-8").splitlines()
    assert text[2:5] == ["THREE", "EXTRA", "line4"]
    assert "FIFTEEN" in text
    assert "line15" not in text and "line16" not in text


def test_overlapping_ranges_in_one_reply_are_refused(
    ranged_engine: Engine, project: Path
) -> None:
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    payload = _run(
        ranged_engine,
        _reply(_replace(1, 10, 14, "TEN"), _replace(2, 8, 11, "EIGHT")),
    )
    assert "code=bad_edit_order" in payload
    assert "overlap" in payload


# -- the record is replaced wholesale, every turn -----------------------------


def test_the_permission_expires_after_one_turn(ranged_engine: Engine, project: Path) -> None:
    """"the results you were JUST given" is literal: a read that has had a turn
    of other work happen since it is no longer evidence of anything."""
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    _run(
        ranged_engine,
        _reply("===CLIP:CALL id=1 tool=list_dir===\npath: .\n===CLIP:END==="),
    )
    payload = _run(ranged_engine, _reply(_replace(1, 3, 4, "NOPE")))
    assert "code=unverified_range" in payload
    assert (project / "lines.py").read_text(encoding="utf-8") == LINES_PY


def test_a_second_numbered_read_re_arms_it(ranged_engine: Engine, project: Path) -> None:
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    _run(ranged_engine, _reply("===CLIP:CALL id=1 tool=list_dir===\npath: .\n===CLIP:END==="))
    _run(ranged_engine, _reply(_read(1)))
    payload = _run(ranged_engine, _reply(_replace(1, 3, 4, "THREE\nFOUR")))
    assert "replaced lines 3-4" in payload


def test_a_read_of_another_file_does_not_authorise_this_one(
    ranged_engine: Engine, project: Path
) -> None:
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1, path="README.md")))
    payload = _run(ranged_engine, _reply(_replace(1, 1, 1, "NOPE")))
    assert "code=unverified_range" in payload


def test_the_path_spelling_does_not_matter(ranged_engine: Engine, project: Path) -> None:
    """Read it as ./lines.py, edit it as lines.py: one file, one record."""
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1, path="./lines.py")))
    payload = _run(ranged_engine, _reply(_replace(1, 3, 3, "THREE")))
    assert "replaced lines 3-3" in payload


# -- the apply-time slice guard ----------------------------------------------


def test_a_file_moved_mid_reply_stops_the_edit(ranged_engine: Engine, project: Path) -> None:
    """The one thing the plan-time check cannot see: the file was right when the
    plan was built and wrong by the time the write ran, because an earlier call
    in the SAME reply moved it. Loud, never a silent wrong edit."""
    ranged_engine.start_task("edit by line numbers")
    _run(ranged_engine, _reply(_read(1)))
    rewrite = (
        "===CLIP:CALL id=1 tool=write_file===\n"
        "path: lines.py\ncontent << EOT\nX\n"
        + LINES_PY.rstrip("\n")
        + "\nEOT\n===CLIP:END==="
    )
    payload = _run(ranged_engine, _reply(rewrite, _replace(2, 3, 3, "THREE")))
    assert "code=stale_read" in payload
    assert "THREE" not in (project / "lines.py").read_text(encoding="utf-8")


# -- truncation-aware derivation ---------------------------------------------


def test_only_the_lines_that_survived_truncation_count(project: Path, make_engine) -> None:
    """A read_file body can be middle-cut AFTER it wrote its own "lines 1-20 of
    20" header, so the header would authorise lines the model never saw. The
    record is derived from the delivered text instead."""
    write_permissions(project, {"permission": {"edit": {"*": "allow"}}})
    (project / "lines.py").write_text(LINES_PY, encoding="utf-8", newline="")
    cfg = load_config(project, global_config_path=project / "no-such-global.toml")
    services = dict(cfg.services)
    key = cfg.general.service
    services[key] = replace(services[key], edit_by_lines=True)
    # A per-result cap tight enough that fit_results middle-cuts the read body.
    cfg = replace(cfg, services=services, limits=replace(cfg.limits, max_result_chars=150))
    engine = make_engine(config=cfg, tools=default_registry(edit_by_lines=True))
    engine.start_task("edit by line numbers")

    payload = _run(engine, _reply(_read(1)))
    assert "truncated by AgentClip" in payload
    assert "lines 1-20 of 20" in payload  # the header still claims all twenty...
    shown = set(surviving_numbered_lines(payload, [1])[1])
    assert shown and shown != set(range(1, 21))  # ...but the middle is gone
    gone = next(n for n in range(1, 21) if n not in shown)
    survivor = max(shown)

    assert "code=unverified_range" in _run(engine, _reply(_replace(1, gone, gone, "NOPE")))
    _run(engine, _reply(_read(1)))  # re-arm, then edit a line that DID survive
    out = _run(engine, _reply(_replace(1, survivor, survivor, "KEPT")))
    assert f"replaced lines {survivor}-{survivor}" in out


# -- the scanner itself -------------------------------------------------------


def test_surviving_numbered_lines_reads_the_composed_payload() -> None:
    composer = Composer(
        Config().preset(), Config().caps(), "catalog", "proj", "TestOS", CHAT_NAME
    )
    body = "f.py lines 1-2 of 9\n1| alpha\n2| bravo"
    payload = composer.results(
        3, [ToolResult(7, "ok", body), ToolResult(8, "ok", "unrelated")]
    ).chunks[0]
    assert surviving_numbered_lines(payload, [7]) == {7: {1: "alpha", 2: "bravo"}}
    assert surviving_numbered_lines(payload, [8]) == {}


def test_a_gutter_shaped_source_line_is_read_as_its_own_gutter() -> None:
    """A file whose content looks like a gutter is exactly the ambiguity an
    anchored pattern removes: 50 is the served text, 12 is the line number."""
    composer = Composer(
        Config().preset(), Config().caps(), "catalog", "proj", "TestOS", CHAT_NAME
    )
    payload = composer.results(
        3, [ToolResult(1, "ok", "f.py lines 12-12 of 99\n12| 50| x")]
    ).chunks[0]
    assert surviving_numbered_lines(payload, [1]) == {1: {12: "50| x"}}


def test_describe_ranges_condenses_runs() -> None:
    assert describe_ranges([1, 2, 3, 7, 8, 20]) == "1-3, 7-8, 20"
    assert describe_ranges([]) == "none"


# -- with the toggle OFF, nothing changes -------------------------------------


def test_without_the_toggle_replace_lines_is_simply_unknown(
    project: Path, make_engine
) -> None:
    (project / "lines.py").write_text(LINES_PY, encoding="utf-8", newline="")
    engine = make_engine()
    engine.start_task("plain service")
    payload = _run(engine, _reply(_replace(1, 3, 4, "NOPE")))
    assert "code=unknown_tool" in payload
    assert (project / "lines.py").read_text(encoding="utf-8") == LINES_PY
