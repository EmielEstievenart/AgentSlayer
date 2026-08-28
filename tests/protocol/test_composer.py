"""Tests for the outbound payload composer (agentclip.protocol.composer)."""

from __future__ import annotations

import re

import pytest

from agentclip.config import ServicePreset
from agentclip.protocol.composer import (
    TRUNCATION_MARKER,
    BudgetExceeded,
    Composer,
    pick_heredoc_tag,
    wrap_in_fence,
)
from agentclip.protocol.parser import normalized_hash
from agentclip.protocol.preset import LivePreset
from agentclip.protocol.types import ToolResult

CHAT_NAME = "amber-falcon"

# ---------------------------------------------------------------------------
# helpers


def make_composer(
    budget: int = 12_000,
    *,
    fence: bool = True,
    attach: bool = True,
    catalog: str = "read_file(path, start, end)\n  Read a file.\n",
    extra: str = "",
) -> Composer:
    preset = ServicePreset(
        "test",
        "Test preset",
        budget,
        budget * 20,
        wrap_blocks_in_fence=fence,
        attachment_note=attach,
        extra_instructions=extra,
    )
    return Composer(LivePreset(preset), catalog, "AgentClip", "Windows 11", CHAT_NAME)


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
            tag = line[len("body <<") :].strip()
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
    assert out.chunks[0].endswith("===CLIP:EOM turn=1 chat=amber-falcon===\n")


def test_bootstrap_teaches_the_chat_name() -> None:
    composer = make_composer()
    assert composer.chat_name == CHAT_NAME
    payload = composer.bootstrap("Fix the bug").chunks[0]
    assert "This chat's name is amber-falcon." in payload
    assert "===CLIP:EOM calls=N chat=amber-falcon===" in payload
    assert "===CLIP:ACK k/n chat=amber-falcon===" in payload
    assert "===CLIP:NACK reason=truncated chat=amber-falcon===" in payload
    assert "turn=T" not in payload  # the turn echo is gone from the model's instructions


def test_every_outbound_kind_carries_the_chat_name() -> None:
    composer = make_composer()
    for payload in (
        composer.bootstrap("t").chunks[0],
        composer.task(2, "more").chunks[0],
        composer.note(3, "reverted").chunks[0],
        composer.results(4, [ToolResult(1, "ok", "fine")]).chunks[0],
    ):
        # The EOM is the last PROTOCOL line of every payload; on the three that
        # are fenced, the closing fence sits behind it (see the fence tests).
        assert f" chat={CHAT_NAME}===" in payload
        assert payload.rstrip().removesuffix("~~~~").rstrip().endswith(
            f" chat={CHAT_NAME}==="
        )


def test_bootstrap_contains_task_block_and_batching_instruction() -> None:
    out = make_composer().bootstrap("Fix the date parsing bug")
    payload = out.chunks[0]
    assert "===CLIP:TASK===\nFix the date parsing bug\n===CLIP:EOM turn=1 chat=amber-falcon===\n" in payload
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


def test_the_bootstrap_may_run_ten_percent_over_the_budget() -> None:
    """max_paste_chars is a comfort setting for the paste the user makes EVERY
    turn; the bootstrap is sent once and has no chunked fallback, so holding it
    to that figure turns "a long first paste" into "the session never arms"
    (protocol.md section 2, "Budget headroom")."""
    budget = 20_000
    composer = make_composer(budget)
    overhead = len(composer.bootstrap("x").chunks[0]) - 1
    ceiling = int(budget * 1.10)

    fits = composer.bootstrap("x" * (ceiling - overhead))
    assert budget < fits.total_chars == ceiling

    with pytest.raises(BudgetExceeded) as exc_info:
        composer.bootstrap("x" * (ceiling - overhead + 1))
    # The exception still reports the budget the USER set, not the slack.
    assert exc_info.value.budget_chars == budget


# ---------------------------------------------------------------------------
# task / note


def test_task_payload_exact_form() -> None:
    out = make_composer().task(5, "Also update the README")
    assert out.kind == "user_answer"
    assert out.turn == 5
    assert out.chunks == (
        "~~~~\n"
        "===CLIP:TASK===\nAlso update the README\n===CLIP:EOM turn=5 chat=amber-falcon===\n"
        "~~~~\n",
    )
    assert out.total_chars == len(out.chunks[0])


def test_note_payload_exact_form() -> None:
    out = make_composer().note(7, "the user reverted turn 6; file states rolled back")
    assert out.kind == "note"
    assert out.turn == 7
    assert out.chunks == (
        "~~~~\n"
        "===CLIP:NOTE===\nthe user reverted turn 6; file states rolled back\n"
        "===CLIP:EOM turn=7 chat=amber-falcon===\n"
        "~~~~\n",
    )


def test_task_and_note_over_budget_raise() -> None:
    """Held to the budget EXACTLY - the bootstrap's 10% slack is its alone
    (see it above). A task and a note are pastes the user makes over and over,
    which is what the budget is a budget for. 610 chars of body is inside the
    slack a bootstrap would get and still over budget here."""
    composer = make_composer(600)
    with pytest.raises(BudgetExceeded):
        composer.task(2, "x" * 610)
    with pytest.raises(BudgetExceeded):
        composer.note(2, "x" * 610)


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
    assert payload.startswith("~~~~\n===CLIP:RESULTS turn=4===\n")
    assert payload.endswith("===CLIP:EOM turn=4 chat=amber-falcon===\n~~~~\n")
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
    assert "body << R1xx\n" in payload
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
    assert payload == (
        "~~~~\n===CLIP:RESULTS turn=8===\n===CLIP:EOM turn=8 chat=amber-falcon===\n~~~~\n"
    )


def test_results_eom_turn_stamping() -> None:
    payload = make_composer().results(42, [ToolResult(1, "ok", "x")]).chunks[0]
    assert payload.endswith("===CLIP:EOM turn=42 chat=amber-falcon===\n~~~~\n")


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
    # Sentinel lines are never truncated - and neither is the fence, which is
    # inside the budget the fit loop just met.
    assert payload.startswith("~~~~\n===CLIP:RESULTS turn=3===\n")
    assert "===CLIP:RESULT id=1 status=ok===" in payload
    assert "===CLIP:RESULT id=2 status=ok===" in payload
    assert payload.count("===CLIP:END===") == 2
    assert payload.endswith("===CLIP:EOM turn=3 chat=amber-falcon===\n~~~~\n")


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


# ---------------------------------------------------------------------------
# caller-supplied truncation markers (the engine's fetch_chunk hint)


def _two_big_results() -> list[ToolResult]:
    return [
        ToolResult(1, "ok", "\n".join(f"first body line {i:04d}" for i in range(200))),
        ToolResult(2, "ok", "\n".join(f"second body line {i:04d}" for i in range(200))),
    ]


def test_a_cut_body_carries_the_marker_its_own_result_was_given() -> None:
    """Per result, not per payload: the id in a marker names THAT body's cached
    text, so swapping two of them would send the model to the wrong output."""
    markers = {1: "[cut: fetch_chunk id=c1 part=1..3]", 2: "[cut: fetch_chunk id=c2 part=1..3]"}
    payload = make_composer(2_000).results(3, _two_big_results(), markers=markers).chunks[0]
    bodies = extract_result_bodies(payload)
    assert markers[1] in bodies[1]
    assert markers[2] in bodies[2]
    assert TRUNCATION_MARKER not in payload


def test_a_result_with_no_marker_offered_falls_back_to_the_plain_one() -> None:
    """A partial mapping is legal: the engine mints ids only for bodies big
    enough to be worth caching, and everything else still says what it can."""
    payload = (
        make_composer(2_000)
        .results(3, _two_big_results(), markers={1: "[cut: fetch_chunk id=c1 part=1..3]"})
        .chunks[0]
    )
    bodies = extract_result_bodies(payload)
    assert "id=c1" in bodies[1]
    assert TRUNCATION_MARKER in bodies[2]


def test_no_markers_at_all_is_exactly_the_old_behaviour() -> None:
    results = _two_big_results()
    assert (
        make_composer(2_000).results(3, results, markers={}).chunks[0]
        == make_composer(2_000).results(3, results).chunks[0]
    )


def test_a_marker_is_only_stamped_into_a_body_that_was_actually_cut() -> None:
    """What the engine keys its cache off: a minted id whose marker is nowhere
    in the rendered payload is an id for text the model can already see whole."""
    results = [ToolResult(1, "ok", "\n".join(f"line {i}" for i in range(20)))]
    payload = make_composer(12_000).results(2, results, markers={1: "[cut: id=c1]"}).chunks[0]
    assert "c1" not in payload


# ---------------------------------------------------------------------------
# the outbound fence (composer module docstring; protocol.md section 4)


def _fence_lines(payload: str) -> tuple[str, str]:
    lines = payload.split("\n")
    return lines[0], lines[-2]  # -1 is the empty string after the trailing \n


def test_results_task_and_note_ride_inside_a_tilde_fence() -> None:
    """Some hosts rewrite plain text pasted into the input box (blank lines came
    back as literal <br>). A fence tells the box this is code; the model reads
    the raw text either way."""
    composer = make_composer()
    for payload in (
        composer.results(4, [ToolResult(1, "ok", "fine")]).chunks[0],
        composer.task(5, "more").chunks[0],
        composer.note(6, "reverted").chunks[0],
    ):
        opener, closer = _fence_lines(payload)
        assert opener == "~~~~"
        assert closer == "~~~~"
        assert payload.endswith("~~~~\n")  # the convention: payloads end in \n


def test_the_bootstrap_is_deliberately_not_fenced() -> None:
    """It is the ROLE framing's payload (a code block reads as data, which is
    what the turn-1 refusal wants to believe), and the one payload with no
    budget headroom and no chunked fallback."""
    payload = make_composer().bootstrap("Fix the bug").chunks[0]
    assert not payload.startswith("~~~~\n")
    assert payload.rstrip().endswith("===CLIP:EOM turn=1 chat=amber-falcon===")


def test_the_fence_outgrows_a_tilde_run_in_a_result_body() -> None:
    """Section 2's collision rule, applied to our own payloads: the outer
    delimiter must not be reachable from inside."""
    body = "the model's own fenced reply, quoted back:\n~~~~\n===CLIP:CALL id=1===\n~~~~"
    payload = make_composer().results(2, [ToolResult(1, "ok", body)]).chunks[0]
    opener, closer = _fence_lines(payload)
    assert opener == "~~~~~" == closer  # strictly more than the longest run inside
    assert extract_result_bodies(payload)[1] == body


def test_the_fence_outgrows_the_longest_run_not_the_first() -> None:
    body = "~~~\nplain\n~~~~~~\nplain"
    payload = make_composer().results(2, [ToolResult(1, "ok", body)]).chunks[0]
    assert _fence_lines(payload)[0] == "~" * 7


def test_wrap_in_fence_only_counts_leading_tilde_runs() -> None:
    # A tilde run mid-line cannot close a fence, so it must not inflate one.
    assert wrap_in_fence("approx ~~~~~ five\n").startswith("~~~~\n")
    # ...and a backtick run cannot either: we fence with tildes on purpose.
    assert wrap_in_fence("```python\ncode\n```\n").startswith("~~~~\n")


def test_wrapping_does_not_move_the_self_suppression_hash() -> None:
    """normalized_hash strips fence lines, so a fenced payload and its bare body
    hash the same - which is what keeps a re-ingest of our own text
    Noise("own-outbound") instead of a reply."""
    payload = "===CLIP:RESULTS turn=4===\n===CLIP:EOM turn=4 chat=amber-falcon===\n"
    assert normalized_hash(wrap_in_fence(payload)) == normalized_hash(payload)


def test_the_fence_is_inside_the_paste_budget() -> None:
    """The fit loop measures the fenced string, so a payload that only just fits
    still fits WITH its fence - the fence is part of the message the host has to
    accept."""
    budget = 2_000
    body = "\n".join(f"line {i:04d} of the command output" for i in range(200))
    out = make_composer(budget).results(3, [ToolResult(1, "ok", body)])
    payload = out.chunks[0]
    assert len(payload) <= budget
    assert out.total_chars == len(payload)
    assert payload.startswith("~~~~\n") and payload.endswith("\n~~~~\n")
    # And it is actually close to the budget: the fence is not bought by leaving
    # a fence-sized hole unused.
    assert len(payload) > budget - 100


def test_task_notes_render_as_a_note_block_ahead_of_the_task() -> None:
    """`task()` carries the same notes channel `results()` does, so anything
    armed for "the next thing we send" is spent by a typed follow-up too
    (protocol.md section 4). Ahead of the TASK block, not inside it: a NOTE
    spliced between that header and the user's words reads as part of the task.
    """
    note = "note: user instructions reminder: keep ] and ( apart in code."
    out = make_composer().task(6, "carry on", notes=[note])
    assert out.chunks == (
        "~~~~\n"
        "===CLIP:NOTE===\n"
        f"{note}\n"
        "===CLIP:END===\n"
        "===CLIP:TASK===\ncarry on\n===CLIP:EOM turn=6 chat=amber-falcon===\n"
        "~~~~\n",
    )
    assert out.total_chars == len(out.chunks[0])


def test_task_without_notes_is_byte_identical_to_before() -> None:
    """The parameter is additive: an ordinary follow-up gains nothing at all."""
    assert make_composer().task(5, "x").chunks == make_composer().task(5, "x", ()).chunks


def test_bootstrap_extra_instructions_toggle() -> None:
    task = "do something"
    with_extra = make_composer(extra="keep ] and ( apart").bootstrap(task).chunks[0]
    without_extra = make_composer().bootstrap(task).chunks[0]
    assert "keep ] and ( apart" in with_extra
    assert "EXTRA INSTRUCTIONS FROM THE USER:" in with_extra
    assert "EXTRA INSTRUCTIONS FROM THE USER:" not in without_extra
