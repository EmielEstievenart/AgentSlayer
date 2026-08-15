"""Window tabs, per-window transcripts and the session summary.

Parity increment 3's surface, against ``docs/design/ui-briefs/
tabs-delegation-summary.md``. What is tested here is the three pointers that
make delegation readable rather than dangerous - SELECTED (what the user is
looking at), FOCUSED (where output lands) and LIVE (what the automation drives)
- plus the tab's derived state glyph, the export's per-run slicing and the
summary's payload and four answers.

Nothing here opens a window: the bridge's sink is a list (tests/gui/conftest.py).
"""

from __future__ import annotations

import asyncio

import pytest

from agentclip.app.types import SessionRef
from agentclip.driver.screen.slot import AgentSlot
from agentclip.engine.engine import Phase
from agentclip.gui.bridge import JsApi
from agentclip.gui.view import (
    MASTER_WINDOW,
    SLOT_NOTE_MASTER,
    SUBAGENT_WINDOW,
    GuiView,
)
from tests.gui.conftest import Harness, settle
from tests.gui.test_chrome import KeySpy, api_of
from tests.gui.test_view import session_view, snapshot


def sub_ref(index: int = 1, title: str = "rename the helper", chat: str = "jade-otter") -> SessionRef:
    return SessionRef(id=f"sub-{index}", role="subagent", title=title, chat_name=chat)


def tabs(harness: Harness) -> dict[str, dict[str, str]]:
    """The last tab bar, keyed by window id - both rows in one map, because the
    bar owns one selection across them."""
    event = harness.flush().last("tabs")
    return {tab["window"]: tab for tab in list(event["masters"]) + list(event["subs"])}


def bar(harness: Harness) -> dict[str, object]:
    return harness.flush().last("tabs")


class SummarySpy(KeySpy):
    """A controller that records ``end_session`` instead of running the flow."""

    def __init__(self) -> None:
        super().__init__()
        self.ends = 0

    def end_session(self) -> None:
        self.ends += 1


# == the tabs are windows, not sessions =======================================


def test_the_bar_has_a_row_of_masters_and_a_row_of_that_masters_subs(
    harness: Harness,
) -> None:
    """Today exactly one of each (``m1`` and ``m1-s1``), which is the current
    real contract - the brief says not to build N-window chrome speculatively."""
    harness.view._push_tabs()
    event = bar(harness)
    assert [tab["window"] for tab in event["masters"]] == [MASTER_WINDOW]  # type: ignore[union-attr]
    assert [tab["window"] for tab in event["subs"]] == [SUBAGENT_WINDOW]  # type: ignore[union-attr]
    assert event["selected"] == MASTER_WINDOW
    assert event["focused"] == MASTER_WINDOW


def test_a_tab_names_its_window_and_the_service_that_window_runs_on(
    harness: Harness,
) -> None:
    """The service is per window and the sidebar only shows the selected one's,
    so without it "which chat is the sub-agent going to open?" is a question you
    answer by clicking around."""
    harness.view._push_tabs()
    master = tabs(harness)[MASTER_WINDOW]
    assert master["name"] == "MASTER"
    assert master["service"] == harness.view._service_for(AgentSlot.MASTER)
    assert master["label"] == f"MASTER · {master['service']}"


def test_the_master_tab_never_carries_a_run_glyph(harness: Harness) -> None:
    """It is the user's own conversation; there is no "run" of it to have
    succeeded or failed."""
    harness.view._push_tabs()
    assert tabs(harness)[MASTER_WINDOW]["state"] == "none"


async def test_a_run_in_flight_badges_the_sub_agent_tab_and_focuses_it(
    harness: Harness,
) -> None:
    await harness.view.open_session_view(sub_ref())
    tab = tabs(harness)[SUBAGENT_WINDOW]
    assert tab["state"] == "running"
    assert tab["label"].startswith("▶ SUB-AGENT")
    event = bar(harness)
    assert event["selected"] == SUBAGENT_WINDOW
    assert event["focused"] == SUBAGENT_WINDOW


@pytest.mark.parametrize(
    ("ok", "state", "glyph"),
    [(True, "ok", "✓ "), (False, "failed", "✗ ")],
)
async def test_the_tab_reports_how_the_run_ended_from_the_callers_answer(
    harness: Harness, ok: bool, state: str, glyph: str
) -> None:
    """``ok`` is a parameter, never inferred: a run refused a fresh chat, over
    budget or crashed reaches ``finish_session_view`` exactly like one that
    handed a result back."""
    view = harness.view
    await view.open_session_view(sub_ref())
    await view.finish_session_view("sub-1", "done, one way or another", ok=ok)
    tab = tabs(harness)[SUBAGENT_WINDOW]
    assert tab["state"] == state
    assert tab["label"].startswith(glyph)


async def test_only_the_last_runs_outcome_is_shown(harness: Harness) -> None:
    """The tab is a status light for the window, not a log - the earlier runs
    stay readable by scrolling its transcript."""
    view = harness.view
    await view.open_session_view(sub_ref(1))
    await view.finish_session_view("sub-1", "failed", ok=False)
    await view.open_session_view(sub_ref(2, title="second try"))
    assert tabs(harness)[SUBAGENT_WINDOW]["state"] == "running"
    await view.finish_session_view("sub-2", "delivered", ok=True)
    assert tabs(harness)[SUBAGENT_WINDOW]["state"] == "ok"


# == transcripts route by window ==============================================


async def test_each_windows_output_lands_in_its_own_transcript(harness: Harness) -> None:
    view = harness.view
    await view.add_user("the master's task")
    await view.open_session_view(sub_ref())
    await view.add_user("the sub-task")
    await view.finish_session_view("sub-1", "✓ sub-agent delivered", ok=True)
    view.focus_session_view("master")
    await view.add_prose("back in the master's conversation")

    routed = [(e["window"], e["kind"]) for e in harness.flush().of_type("transcript")]
    assert routed == [
        (MASTER_WINDOW, "user"),
        (SUBAGENT_WINDOW, "note"),  # ── task: ... ──
        (SUBAGENT_WINDOW, "user"),
        (SUBAGENT_WINDOW, "note"),  # the outcome note
        (MASTER_WINDOW, "prose"),
    ]


async def test_output_keeps_landing_in_the_focused_window_while_another_is_shown(
    harness: Harness,
) -> None:
    """The specific bug class the TUI's docstrings call out: the user reads the
    master's transcript while the sub-agent's output keeps arriving correctly."""
    view = harness.view
    await view.open_session_view(sub_ref())
    view.select_window(MASTER_WINDOW)  # the user clicks back to their own chat
    await view.add_prose("the sub-agent is still thinking")

    assert view._focused_window == SUBAGENT_WINDOW
    assert view._selected_window == MASTER_WINDOW
    assert harness.flush().last("transcript")["window"] == SUBAGENT_WINDOW


async def test_the_sub_agent_transcript_is_appended_never_re_created(
    harness: Harness,
) -> None:
    """A delegation does not mint a pane: run two appends under its own divider
    below run one, which is what makes the window's history one scroll."""
    view = harness.view
    await view.open_session_view(sub_ref(1, title="first"))
    await view.add_note("run one said something")
    await view.finish_session_view("sub-1", "✓ delivered", ok=True)
    await view.open_session_view(sub_ref(2, title="second"))

    notes = [e["text"] for e in harness.flush().of_type("transcript")]
    assert notes[0] == "── task: first ──"
    assert notes[-1] == "── task: second ──"
    assert len(view._events[SUBAGENT_WINDOW]) == 4


async def test_the_export_puts_one_heading_over_each_run(harness: Harness) -> None:
    """Per RUN rather than per window: five sub-tasks under a single heading is
    a wall, and the slices are what make them separable."""
    view = harness.view
    await view.add_user("the master's task")
    await view.open_session_view(sub_ref(1, title="first", chat="jade-otter"))
    await view.add_user("sub-task one")
    await view.finish_session_view("sub-1", "✓ delivered", ok=True)
    await view.open_session_view(sub_ref(2, title="second", chat="amber-falcon"))
    await view.add_user("sub-task two")

    log = view.render_log(["service: chatgpt"])
    assert log.index("the master's task") < log.index("## sub-agent: first (jade-otter)")
    assert "## sub-agent: second (amber-falcon)" in log
    # Each run's own events under its own heading, and the divider above them in
    # neither (the heading has already said whose task it is).
    first = log.index("## sub-agent: first (jade-otter)")
    second = log.index("## sub-agent: second (amber-falcon)")
    assert first < log.index("sub-task one") < second < log.index("sub-task two")
    assert "── task: first ──" not in log[first:]


# == selection is not focus is not live =======================================


async def test_selecting_a_tab_never_touches_the_automations_live_window(
    harness: Harness,
) -> None:
    """The single most important invariant on this surface: looking at a window
    is not driving it. A click on the master tab mid-run must not redirect the
    next paste into the master's chat."""
    view = harness.view
    view.automation.select_live_slot(AgentSlot.SUBAGENT)
    await view.open_session_view(sub_ref())

    view.select_window(MASTER_WINDOW)

    assert view.automation.live_slot is AgentSlot.SUBAGENT
    assert view._focused_window == SUBAGENT_WINDOW


def test_selecting_a_tab_points_the_configuration_surface_at_it(harness: Harness) -> None:
    view = harness.view
    view.select_window(SUBAGENT_WINDOW)
    assert view.automation.calibrating_slot is AgentSlot.SUBAGENT
    view.select_window(MASTER_WINDOW)
    assert view.automation.calibrating_slot is AgentSlot.MASTER


def test_clicking_the_tab_you_are_already_on_still_shows_that_window(
    harness: Harness,
) -> None:
    """Idempotent rather than an early return: after the controller moved the
    view mid-delegation, this is how a user says "show me this window"."""
    harness.flush().clear()
    api_of(harness).window(MASTER_WINDOW)
    assert bar(harness)["selected"] == MASTER_WINDOW


def test_an_unknown_window_is_ignored(harness: Harness) -> None:
    view = harness.view
    api_of(harness).window("m9-s9")
    assert view._selected_window == MASTER_WINDOW


def test_f6_cycles_the_bars_own_order(harness: Harness) -> None:
    """Every master, then the selected master's sub-agent windows - and back
    round (``WindowTabs.order``)."""
    view = harness.view
    api = api_of(harness)
    api.next_window()
    assert view._selected_window == SUBAGENT_WINDOW
    api.next_window()
    assert view._selected_window == MASTER_WINDOW


async def test_the_controller_focusing_a_run_moves_the_view_with_it(
    harness: Harness,
) -> None:
    """``focus_session_view`` is the controller's one reach into the selection:
    a delegation starting pulls the user's eyes to the sub-agent's transcript,
    and its ending hands them back."""
    view = harness.view
    await view.open_session_view(sub_ref())
    assert view._selected_window == SUBAGENT_WINDOW
    view.focus_session_view("master")
    assert view._selected_window == MASTER_WINDOW
    assert view._focused_window == MASTER_WINDOW


# == the sidebar describes the SELECTED window ================================


def test_every_per_window_block_follows_the_selection(harness: Harness) -> None:
    """Picking a tab shows a window's transcript AND points every per-window
    control at that window (``MainScreen._select_window``)."""
    view = harness.view
    view._awaiting_new_session = True
    other = next(key for key in sorted(view._config.services) if key != view._service_for(AgentSlot.MASTER))
    view.automation.set_service(SUBAGENT_WINDOW, other)

    view.select_window(SUBAGENT_WINDOW)
    event = harness.flush().last("sidebar")
    assert event["window"] == SUBAGENT_WINDOW
    assert event["service"] == other
    # The readiness line is the sub-agent window's, and nothing is calibrated
    # here, so it names the gaps rather than claiming delegation is on.
    assert event["slot_note"].startswith("delegation off · need: ")

    view.select_window(MASTER_WINDOW)
    event = harness.flush().last("sidebar")
    assert event["window"] == MASTER_WINDOW
    assert event["slot_note"] == SLOT_NOTE_MASTER


def test_the_detection_heading_still_names_the_live_window_not_the_selected_one(
    harness: Harness,
) -> None:
    """Two pointers, and they part company for the whole of a delegation: the
    DETECTION block reports on what the detectors are watching."""
    view = harness.view
    view.automation.select_live_slot(AgentSlot.SUBAGENT)
    view.select_window(MASTER_WINDOW)
    assert harness.flush().last("sidebar")["detection_title"] == "DETECTION · SUB-AGENT"


def test_the_service_picker_edits_the_selected_window(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agentclip.gui.view.save_active_services",
        lambda master, sub: saved.append((master, sub)),
    )
    view = harness.view
    view._awaiting_new_session = True
    before = view._service_for(AgentSlot.MASTER)
    other = next(key for key in sorted(view._config.services) if key != before)

    view.select_window(SUBAGENT_WINDOW)
    api_of(harness).service(other)

    assert view.automation.service_of(SUBAGENT_WINDOW) == other
    assert view.automation.service_of(MASTER_WINDOW) == before  # untouched
    assert saved and saved[-1] == (before, other)
    # ...and the tab says so: the service key is part of what a tab IS.
    assert tabs(harness)[SUBAGENT_WINDOW]["label"].endswith(f"· {other}")


# == /new keeps the windows and forgets the runs ==============================


async def test_a_reset_forgets_the_runs_and_keeps_both_windows(harness: Harness) -> None:
    """Session/run bookkeeping resets; window calibration does not. The tab
    drops its ``✓`` because the runs are gone, not because the window is."""
    view = harness.view
    view.automation.set_service(SUBAGENT_WINDOW, view._service_for(AgentSlot.MASTER))
    await view.open_session_view(sub_ref())
    await view.finish_session_view("sub-1", "✓ delivered", ok=True)

    await view.clear_transcript()

    event = bar(harness)
    assert [tab["window"] for tab in event["subs"]] == [SUBAGENT_WINDOW]  # type: ignore[union-attr]
    assert tabs(harness)[SUBAGENT_WINDOW]["state"] == "none"
    assert event["selected"] == MASTER_WINDOW and event["focused"] == MASTER_WINDOW
    assert view._sub_runs == []
    assert not view.has_transcript_events()
    # The calibrations and the services are facts about the browser, not about
    # the session that just ended.
    assert view.automation.service_of(SUBAGENT_WINDOW)


# == the session summary =======================================================


def test_e_is_gated_on_a_settled_session_and_says_why_when_it_is_not(
    harness: Harness,
) -> None:
    """``check_action``'s three-way dimming has no equivalent here - there is no
    footer to hide a binding from - so a key that cannot fire toasts why."""
    view = harness.view
    spy = SummarySpy()
    view._controller = spy  # type: ignore[assignment]

    api_of(harness).end_session()
    assert spy.ends == 0
    assert "no session yet" in harness.flush().last("toast")["message"]

    view.render_state(session_view(busy=True))
    api_of(harness).end_session()
    assert spy.ends == 0
    assert "a turn is running" in harness.flush().last("toast")["message"]


@pytest.mark.parametrize("phase", [Phase.AWAITING_REPLY, Phase.DONE])
def test_e_opens_the_summary_once_the_floor_is_back_with_the_user(
    harness: Harness, phase: Phase
) -> None:
    view = harness.view
    spy = SummarySpy()
    view._controller = spy  # type: ignore[assignment]
    view.render_state(session_view(snapshot=snapshot(phase)))
    api_of(harness).end_session()
    assert spy.ends == 1


async def test_the_summary_carries_the_stats_the_text_and_its_keys(
    harness: Harness,
) -> None:
    view = harness.view
    pending = asyncio.ensure_future(
        view.show_summary([("turns", "3"), ("sub-agent runs", "1")], "## done\nall good")
    )
    await settle()
    modal = harness.flush().last("modal")
    assert modal["modal"] == "summary"
    assert modal["title"] == "SESSION SUMMARY"
    assert modal["rows"] == [["turns", "3"], ["sub-agent runs", "1"]]
    assert modal["summary"] == "## done\nall good"
    assert "the model sent no summary" in modal["placeholder"]
    for key in ("u undo last turn", "t new session", "l export chat log", "esc close"):
        assert key in modal["hint"]
    view.answer_prompt(modal["prompt_id"], "close")
    assert await pending == "close"


@pytest.mark.parametrize("answer", ["undo", "new", "export", "close"])
async def test_each_summary_answer_reaches_the_controller_verbatim(
    harness: Harness, answer: str
) -> None:
    """The four sentinels are the controller's vocabulary - it branches on them
    (undo one turn / reset / write the log and re-open / nothing at all)."""
    view = harness.view
    pending = asyncio.ensure_future(view.show_summary([("turns", "1")], ""))
    await settle()
    view.answer_prompt(harness.flush().last("modal")["prompt_id"], answer)
    assert await pending == answer


async def test_an_answer_the_summary_never_offered_is_none_of_the_above(
    harness: Harness,
) -> None:
    """A modal an abort poisoned, or a page that answered with a shape we do not
    know: the controller reads "" as none of the above and leaves the session
    alone, which beats guessing at a reset."""
    view = harness.view
    pending = asyncio.ensure_future(view.show_summary([], ""))
    await settle()
    view.answer_prompt(harness.flush().last("modal")["prompt_id"], "delete-everything")
    assert await pending == ""


def test_the_tab_and_summary_actions_are_on_the_view_the_runner_marshals_to() -> None:
    """The runner is a one-line marshal per method; a name on the bridge's
    protocol and not on the view would be a click that silently did nothing."""
    for name in ("select_window", "next_window", "end_session"):
        assert callable(getattr(GuiView, name, None)), name


def test_the_page_asks_for_them_by_the_names_the_bridge_exposes() -> None:
    for name in ("window", "next_window", "end_session"):
        assert callable(getattr(JsApi, name, None)), name
