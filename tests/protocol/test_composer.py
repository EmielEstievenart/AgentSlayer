"""Tests for the outbound payload composer (agentclip.protocol.composer)."""

from __future__ import annotations

import re

import pytest

from agentclip.config import ServicePreset, caps_for_budget
from agentclip.protocol.composer import (
    TRUNCATION_MARKER,
    BudgetExceeded,
    Composer,
    pick_heredoc_tag,
)
from agentclip.protocol.types import ToolResult

# ---------------------------------------------------------------------------
# helpers


def make_composer(
    budget: int = 12_000,
    *,
    fence: bool = True,
    attach: bool = True,
    catalog: str = "read_file(path, start, end)\n  Read a file.\n",
) -> Composer:
    preset = ServicePreset(
        "test", "Test preset", budget, wrap_blocks_in_fence=fence, attachment_note=attach
    )
    return Composer(preset, caps_for_budget(budget), catalog, "AgentClip", "Windows 11")


def representative_catalog(target: int = 4_200) -> str:
    """A deterministic stand-in for the registry-generated 10-tool catalog."""
    tools = (
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "list_dir",
        "glob",
        "grep",
        "run_command",
        "ask_user",
        "task_done",
    )
    entries = []
    for i, name in enumerate(tools, start=1):
        entries.append(
            f"{name}(path, start, end)\n"
            "  One or two semantic notes about what the tool does and returns,\n"
            "  including range clamping and error behavior on bad input.\n"
            "  Example:\n"
            f"  ===CLIP:CALL id={i} tool={name}===\n"
            "  path: src/example.py\n"
            "  ===CLIP:END===\n"
        )
    text = "\n".join(entries)
    filler = "\nnote: results are capped to the paste budget; ask for specific ranges."
    while len(text) < target:
        text += filler
    return text


def extract_result_bodies(payload: str) -> dict[int, str]:
    """Minimal heredoc-aware extraction of RESULT bodies, keyed by call id."""
    lines = payload.split("\n")
    bodies: dict[int, str] = {}
    current_id: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        header = re.match(r"===CLIP:RESULT id=(\d+) ", line)
        if header:
            current_id = int(header.group(1))
        elif line.startswith("body <<") and current_id is not None:
            tag = line[len("body <<") :]
            content: list[str] = []
            i += 1
            while lines[i].strip() != tag:
                content.append(lines[i])
                i += 1
            bodies[current_id] = "\n".join(content)
            current_id = None
        i += 1
    return bodies


# ---------------------------------------------------------------------------
# pick_heredoc_tag


def test_pick_tag_no_collision() -> None:
    assert pick_heredoc_tag("plain content\nno tags here") == "R"


def test_pick_tag_single_collision() -> None:
    assert pick_heredoc_tag("first\nR\nlast") == "Rx"


def test_pick_tag_chained_collisions() -> None:
    assert pick_heredoc_tag("R\nRx\nRxx is fine inside a longer line\nRxx") == "Rxxx"


def test_pick_tag_collision_is_whitespace_trimmed() -> None:
    # Heredoc terminators match after whitespace trim, so "  R  " collides too.
    assert pick_heredoc_tag("first\n  R \t\nlast") == "Rx"


def test_pick_tag_custom_base() -> None:
    assert pick_heredoc_tag("body with\nR1\ninside", base="R1") == "R1x"
    assert pick_heredoc_tag("no collision", base="R7") == "R7"


# ---------------------------------------------------------------------------
# bootstrap


def test_bootstrap_kind_turn_and_eom() -> None:
    out = make_composer().bootstrap("Fix the bug in src/utils.py")
    assert out.kind == "bootstrap"
    assert out.turn == 1
    assert len(out.chunks) == 1
    assert out.total_chars == len(out.chunks[0])
    assert out.chunks[0].endswith("===CLIP:EOM turn=1===\n")


def test_bootstrap_contains_task_block_and_batching_instruction() -> None:
    out = make_composer().bootstrap("Fix the date parsing bug")
    payload = out.chunks[0]
    assert "===CLIP:TASK===\nFix the date parsing bug\n===CLIP:EOM turn=1===\n" in payload
    assert (
        "Batch all independent calls into one reply - read every file you need at once, "
        "do not request files one at a time; each round trip costs the user a manual "
        "copy-paste." in payload
    )


def test_bootstrap_attachment_note_toggles() -> None:
    task = "do something"
    with_note = make_composer(attach=True).bootstrap(task).chunks[0]
    without_note = make_composer(attach=False).bootstrap(task).chunks[0]
    assert "paste.txt" in with_note
    assert "paste.txt" not in without_note


def test_bootstrap_fence_instruction_toggles() -> None:
    task = "do something"
    with_fence = make_composer(fence=True).bootstrap(task).chunks[0]
    without_fence = make_composer(fence=False).bootstrap(task).chunks[0]
    assert "~~~~" in with_fence
    assert "~~~~" not in without_fence


def test_bootstrap_size_sanity_with_representative_catalog() -> None:
    catalog = representative_catalog()
    assert 4_000 <= len(catalog) <= 4_400  # the catalog itself is representative
    out = make_composer(12_000, catalog=catalog).bootstrap(
        "tests/test_utils.py fails: parse_date expects DD/MM/YYYY but the spec "
        "says ISO dates (YYYY-MM-DD). Fix it and verify with pytest."
    )
    assert 7_000 <= out.total_chars <= 12_000


def test_bootstrap_over_budget_raises() -> None:
    composer = make_composer(500)
    with pytest.raises(BudgetExceeded) as exc_info:
        composer.bootstrap("any task")
    assert exc_info.value.budget_chars == 500
    assert exc_info.value.needed_chars > 500


# ---------------------------------------------------------------------------
# task / note


def test_task_payload_exact_form() -> None:
    out = make_composer().task(5, "Also update the README")
    assert out.kind == "user_answer"
    assert out.turn == 5
    assert out.chunks == ("===CLIP:TASK===\nAlso update the README\n===CLIP:EOM turn=5===\n",)
    assert out.total_chars == len(out.chunks[0])


def test_note_payload_exact_form() -> None:
    out = make_composer().note(7, "the user reverted turn 6; file states rolled back")
    assert out.kind == "note"
    assert out.turn == 7
    assert out.chunks == (
        "===CLIP:NOTE===\nthe user reverted turn 6; file states rolled back\n"
        "===CLIP:EOM turn=7===\n",
    )


def test_task_and_note_over_budget_raise() -> None:
    composer = make_composer(600)
    with pytest.raises(BudgetExceeded):
        composer.task(2, "x" * 700)
    with pytest.raises(BudgetExceeded):
        composer.note(2, "x" * 700)


# ---------------------------------------------------------------------------
# results


def test_results_basic_round_trip() -> None:
    results = [
        ToolResult(1, "ok", "replaced 1 occurrence at line 88", tool="edit_file"),
        ToolResult(2, "ok", "exit 0 (1.4s)\n5 passed in 0.31s", tool="run_command"),
    ]
    out = make_composer().results(4, results)
    payload = out.chunks[0]
    assert out.kind == "results"
    assert out.turn == 4
    assert payload.startswith("===CLIP:RESULTS turn=4===\n")
    assert payload.endswith("===CLIP:EOM turn=4===\n")
    assert "===CLIP:RESULT id=1 status=ok===" in payload
    assert "===CLIP:RESULT id=2 status=ok===" in payload
    assert payload.index("id=1") < payload.index("id=2")  # execution order preserved
    assert payload.count("===CLIP:END===") == 2
    bodies = extract_result_bodies(payload)
    assert bodies[1] == "replaced 1 occurrence at line 88"
    assert bodies[2] == "exit 0 (1.4s)\n5 passed in 0.31s"


def test_results_error_code_in_header() -> None:
    results = [
        ToolResult(
            1,
            "error",
            "find-block not found in src/utils.py.\nhint: re-read lines 80-95.",
            tool="edit_file",
            code="match_not_found",
        )
    ]
    payload = make_composer().results(3, results).chunks[0]
    assert "===CLIP:RESULT id=1 status=error code=match_not_found===" in payload


def test_results_denied_renders_user_note_as_first_body_line() -> None:
    results = [
        ToolResult(
            2,
            "denied",
            "edit_file was not applied",
            tool="edit_file",
            user_note="wrong file, fix the copy in src/b.py instead",
        )
    ]
    payload = make_composer().results(6, results).chunks[0]
    assert "===CLIP:RESULT id=2 status=denied===" in payload
    body = extract_result_bodies(payload)[2]
    assert body.split("\n")[0] == "user: wrong file, fix the copy in src/b.py instead"
    assert "edit_file was not applied" in body


def test_results_skipped_status_renders() -> None:
    payload = (
        make_composer()
        .results(5, [ToolResult(3, "skipped", "skipped: earlier call on this path was denied")])
        .chunks[0]
    )
    assert "===CLIP:RESULT id=3 status=skipped===" in payload


def test_results_heredoc_tag_avoids_collision_with_body() -> None:
    body = "grep output:\nR1\nmore lines\nR1x is mentioned but not alone? no:\nR1x"
    payload = make_composer().results(2, [ToolResult(1, "ok", body)]).chunks[0]
    assert "body <<R1xx\n" in payload
    lines = payload.split("\n")
    assert "R1" in lines  # the colliding content line survives verbatim
    assert extract_result_bodies(payload)[1] == body


def test_results_notes_render_as_note_block_before_results() -> None:
    note = "you sent two calls with id=2; treated as id=2 and id=3 below."
    payload = make_composer().results(4, [ToolResult(2, "ok", "fine")], notes=[note]).chunks[0]
    assert "===CLIP:NOTE===" in payload
    assert note in payload
    assert payload.index("===CLIP:NOTE===") < payload.index("===CLIP:RESULT id=2")
    assert payload.index(note) < payload.index("===CLIP:RESULT id=2")


def test_results_empty_list_still_framed() -> None:
    payload = make_composer().results(8, []).chunks[0]
    assert payload == "===CLIP:RESULTS turn=8===\n===CLIP:EOM turn=8===\n"


def test_results_eom_turn_stamping() -> None:
    payload = make_composer().results(42, [ToolResult(1, "ok", "x")]).chunks[0]
    assert payload.endswith("===CLIP:EOM turn=42===\n")


# ---------------------------------------------------------------------------
# fit-by-truncation (M1 single-chunk policy)


def test_results_over_budget_truncates_largest_body_to_fit() -> None:
    big_body = "\n".join(f"line {i:04d} of the command output" for i in range(200))
    small_body = "small result intact"
    results = [
        ToolResult(1, "ok", big_body, tool="run_command"),
        ToolResult(2, "ok", small_body, tool="edit_file"),
    ]
    out = make_composer(2_000).results(3, results)
    payload = out.chunks[0]
    assert len(payload) <= 2_000
    assert out.total_chars == len(payload)
    assert TRUNCATION_MARKER in payload

    bodies = extract_result_bodies(payload)
    # First and last lines of the truncated body are kept.
    truncated_lines = bodies[1].split("\n")
    assert truncated_lines[0] == "line 0000 of the command output"
    assert truncated_lines[-1] == "line 0199 of the command output"
    assert TRUNCATION_MARKER in truncated_lines
    # The small body is untouched.
    assert bodies[2] == small_body
    # Sentinel lines are never truncated.
    assert payload.startswith("===CLIP:RESULTS turn=3===\n")
    assert "===CLIP:RESULT id=1 status=ok===" in payload
    assert "===CLIP:RESULT id=2 status=ok===" in payload
    assert payload.count("===CLIP:END===") == 2
    assert payload.endswith("===CLIP:EOM turn=3===\n")


def test_results_under_budget_not_truncated() -> None:
    body = "\n".join(f"line {i}" for i in range(20))
    payload = make_composer(12_000).results(2, [ToolResult(1, "ok", body)]).chunks[0]
    assert TRUNCATION_MARKER not in payload
    assert extract_result_bodies(payload)[1] == body


def test_results_truncation_shrinks_only_the_largest_bodies() -> None:
    big = "\n".join(f"big body line number {i:04d}" for i in range(150))
    medium = "\n".join(f"medium line {i}" for i in range(10))
    out = make_composer(2_000).results(5, [ToolResult(1, "ok", big), ToolResult(2, "ok", medium)])
    bodies = extract_result_bodies(out.chunks[0])
    assert TRUNCATION_MARKER in bodies[1]
    assert bodies[2] == medium  # under the cap, untouched


def test_results_unfittable_raises_budget_exceeded() -> None:
    # Two-line body: nothing can be cut without touching the first/last line.
    body = "x" * 500 + "\n" + "y" * 500
    composer = make_composer(300)
    with pytest.raises(BudgetExceeded) as exc_info:
        composer.results(2, [ToolResult(1, "ok", body)])
    assert exc_info.value.budget_chars == 300
    assert exc_info.value.needed_chars > 300
