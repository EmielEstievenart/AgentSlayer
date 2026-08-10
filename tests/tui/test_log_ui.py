"""Pilot tests for the harness decision log and `/log` (tui.md §3.3b).

The sidebar's STATE rail says WHERE the browser-automation loop is; this log
says how it got there, and that is the question a stuck user actually has. The
rail draws one ``MANUAL_COPY`` box for four different roads into it - the tool
is disarmed, the service has no captured copy button, the button was not found
on screen, the click did not take - and the whole point of the log is that
those four read differently afterwards.

So these tests drive REAL scenarios (a paste with nowhere to paste, a driven
finish, each way the send gate can end, a disarmed harvest) and then read the
rendered screen for the reason that decision should have written. Asserting on
the text rather than on ``_harness_log`` is deliberate: an entry the user cannot
read is not a log entry, and the reasons are the feature.

``BusyProbed`` / ``SendReadyProbed`` are the documented injectable path for the
poller (tui/messages.py, and test_finish_signal_ui.py drives them at length);
posting them is equivalent to a poll completing. Nothing here touches a real
screen, a real clipboard or a real mouse.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

import agentclip.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.clip.fake import FakeClipboard
from agentclip.config import load_config
from agentclip.screen.busy import BusyProbe, BusyState
from agentclip.screen.capture import CaptureError, RegionImage
from agentclip.screen.detector import build_detector
from agentclip.screen.profile import TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.tui.app import AgentClipApp
from agentclip.tui.harness_log import EMPTY_LOG_LINE, HARNESS_LOG_MAX
from agentclip.tui.messages import BusyProbed, ClipboardCaptured, SendReadyProbed
from agentclip.tui.screens.log import LogScreen
from agentclip.tui.screens.main import MainScreen

from .conftest import send_composer

SIZE = (110, 40)
CHAT_REGION = ScreenRegion(1050, 340, 812, 540)

REPLY_TASK_DONE = """All set - nothing else to change.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Nothing left to do.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    project.mkdir()
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
    )
    return app, fake


async def _at_the_task_prompt(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


async def _start_session(app: AgentClipApp, pilot: Pilot, task: str = "Say hello.") -> MainScreen:
    """A session, started the only way there is: type the task, press Enter.

    With no chat window calibrated the bootstrap's insert attempt refuses to
    click, which is itself one of the decisions under test here.
    """
    main = await _at_the_task_prompt(app, pilot)
    await send_composer(app, pilot, task)
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


async def _read_log(app: AgentClipApp, pilot: Pilot) -> str:
    """Type `/log`, read what the screen actually renders, dismiss it."""
    await send_composer(app, pilot, "/log")
    await _wait_for(pilot, lambda: isinstance(app.screen, LogScreen), "the log screen")
    text = str(app.screen.query_one("#log-entries", Static).render())
    await pilot.press("escape")
    await _wait_for(pilot, lambda: not isinstance(app.screen, LogScreen), "the log dismissed")
    return text


def _detectors(main: MainScreen, *names: str) -> None:
    """Which detectors the (unstarted) poller would be posting - the seam that
    says which message closes a tick. See test_finish_signal_ui.py."""
    main._active_detectors = names


async def _busy(main: MainScreen, pilot: Pilot, state: BusyState) -> None:
    """One busy-appearance probe, as ``PresenceTracker.observe`` would post it.
    MATCH is the reasoning icon on screen; ``generating_now`` is the honest
    per-frame reading of that."""
    main.post_message(
        BusyProbed(BusyProbe(state, 0.2, state is BusyState.MATCH), main._detector_generation)
    )
    await pilot.pause()


async def _send_ready(main: MainScreen, pilot: Pilot, found: bool | None) -> None:
    main.post_message(SendReadyProbed(found, main._detector_generation))
    await pilot.pause()


async def _finishes(main: MainScreen, pilot: Pilot) -> None:
    """The busy sequence that arms and then fires: generating, then two quiet ticks."""
    await _busy(main, pilot, BusyState.MATCH)
    await _busy(main, pilot, BusyState.CHANGED)
    await _busy(main, pilot, BusyState.CHANGED)


def _seed_and_reload(
    main: MainScreen, seed: Callable[..., None], *kinds: TemplateKind
) -> None:
    """Give the live service those appearances, as a capture would have."""
    seed(main._selected_service(), *kinds, size=(24, 24))
    main._profiles.clear()
    main.update_config(main._config)


# -- the screen itself --------------------------------------------------------


async def test_the_log_opens_with_no_session_at_all_and_says_it_is_empty(
    tmp_path: Path,
) -> None:
    """`/identify`'s rule: no session gate, because the log is most wanted when
    a run has wedged or ended. At the task prompt there is nothing in it yet,
    and the screen says so rather than showing a blank box the user has to
    interpret - and the prompt is still waiting afterwards."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)

        assert EMPTY_LOG_LINE in await _read_log(app, pilot)
        assert main.awaiting_new_session  # the prompt never resolved...
        assert not main.session_active  # ...and no session was started


async def test_the_log_screen_scrolls_instead_of_clipping_its_tail(tmp_path: Path) -> None:
    """The reason the body is a VerticalScroll: ``.modal-box`` caps its height
    and sets no overflow rule, so a long log in a bare Vertical would lose its
    newest entries silently - which are the ones the user opened it for."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        for index in range(120):
            main._log_harness("state", f"filler entry {index}")

        await send_composer(app, pilot, "/log")
        await _wait_for(pilot, lambda: isinstance(app.screen, LogScreen), "the log screen")
        body = app.screen.query_one("#log-body")
        assert body.max_scroll_y > 0  # there is more log than box
        assert body.scroll_offset.y == body.max_scroll_y  # ...and it opened at the end


async def test_the_log_is_bounded_and_keeps_the_newest_entries(tmp_path: Path) -> None:
    """A debugging tail, not an archive - the same 500 the transcript prunes to.
    What must survive the bound is the RECENT end, since that is where the
    decision the user is asking about lives."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        for index in range(HARNESS_LOG_MAX + 20):
            main._log_harness("state", f"entry {index}")

        assert len(main._harness_log) == HARNESS_LOG_MAX
        text = await _read_log(app, pilot)
        assert f"entry {HARNESS_LOG_MAX + 19}" in text
        assert "entry 0 " not in text and "entry 5\n" not in text


# -- the reasons, from real scenarios -----------------------------------------


async def test_a_paste_with_nowhere_to_paste_logs_why_it_asked_for_a_manual_one(
    tmp_path: Path,
) -> None:
    """The first decision of every run, and the one users meet first: the
    bootstrap is copied, the tool tries to insert it itself, and with no chat
    box drawn the click is refused. The rail shows MANUAL_INSERT; the log says
    which of its causes this was."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await _start_session(app, pilot)

        text = await _read_log(app, pilot)
        assert "IDLE → AUTO_INSERT — an outbound payload is ready" in text
        assert "AUTO_INSERT → MANUAL_INSERT — the focus click did not land" in text
        assert "session started" in text


async def test_a_disarmed_paste_is_logged_as_suppressed_rather_than_failed(
    tmp_path: Path,
) -> None:
    """The switch the user threw themselves must not read as a fault. Same
    MANUAL_INSERT box on the rail, a different sentence underneath it - and the
    toggle itself is logged, so the two lines explain each other."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        main.set_os_armed(False)
        await _start_session(app, pilot)

        text = await _read_log(app, pilot)
        assert "DISARMED - watching only" in text
        assert "auto-insert suppressed: disarmed" in text
        assert "the focus click did not land" not in text  # nothing was attempted


async def test_a_clipboard_capture_logs_its_size_and_the_move_to_interpreting(
    tmp_path: Path,
) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)

        main.post_message(ClipboardCaptured(REPLY_TASK_DONE))
        await _wait_for(pilot, lambda: main.phase_name == "DONE", "task marked done")
        await _wait_for(pilot, lambda: not main.busy, "done flow settled")

        text = await _read_log(app, pilot)
        assert f"clipboard capture came in ({len(REPLY_TASK_DONE)} chars)" in text
        assert "→ INTERPRETING — the reply arrived on the clipboard" in text
        # ...and the turn handing the floor back is its own, differently-worded
        # road to IDLE - never confusable with a /new reset.
        assert "INTERPRETING → IDLE — the turn finished" in text


async def test_a_driven_finish_logs_the_evidence_that_armed_it_and_the_fire(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-copy trigger is the least visible decision in the app: it arms
    on one kind of evidence and fires on another, and until now neither was
    reported anywhere. A reasoning icon is proof; a run of large frame deltas is
    inference, and only the second can be fooled by a video the user has open -
    so WHICH one armed it is the entry."""

    async def fake_flow(self: MainScreen) -> None:
        return None

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _seed_and_reload(main, seed_templates, TemplateKind.COPY)
        main._open_reply_gate()
        _detectors(main, "busy")

        await _finishes(main, pilot)

        text = await _read_log(app, pilot)
        assert "auto-copy trigger armed: a busy/idle icon shows the model generating" in text
        assert "→ WAIT_GENERATE — a busy/idle icon shows the model generating" in text
        assert "WAIT_GENERATE → AUTO_COPY — every live detector said the model stopped" in text


async def test_a_finish_with_no_captured_copy_button_says_there_was_nothing_to_click(
    tmp_path: Path,
) -> None:
    """One of the four roads to MANUAL_COPY, and the one with the clearest fix:
    capture the button in F2. The rail cannot say that; this does."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        main._open_reply_gate()
        _detectors(main, "busy")

        await _finishes(main, pilot)

        text = await _read_log(app, pilot)
        assert "WAIT_GENERATE → MANUAL_COPY — no copy button is captured for this service" in text


async def test_a_disarmed_finish_says_the_auto_copy_was_suppressed(
    tmp_path: Path, seed_templates: Callable[..., None]
) -> None:
    """Same MANUAL_COPY box, a wholly different cause: the finish is real and
    everything about it stayed true - the tool simply may not click. The entry
    has to name the switch, or the user goes looking for a broken capture."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _seed_and_reload(main, seed_templates, TemplateKind.COPY)
        main.set_os_armed(False)
        main._open_reply_gate()
        _detectors(main, "busy")

        await _finishes(main, pilot)

        text = await _read_log(app, pilot)
        assert "WAIT_GENERATE → MANUAL_COPY — auto-copy suppressed: disarmed" in text


# -- the auto-copy flow's own failures ----------------------------------------


def _patch_flow_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every OS call the auto-copy flow makes, stubbed at its use site.

    Nothing here touches a real screen, mouse or window. ``move_cursor``
    refusing is what keeps the hover scan out of these tests - it is
    test_hover_copy_ui.py's subject, and a static miss is what this file is
    about.
    """
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)
    monkeypatch.setattr(main_mod, "capture_region", lambda region: _blank(region))
    monkeypatch.setattr(main_mod, "click_region", lambda region, **kw: True)
    monkeypatch.setattr(main_mod, "scroll_region", lambda region, n: True)
    monkeypatch.setattr(main_mod, "move_cursor", lambda x, y: False)
    monkeypatch.setattr(main_mod, "focus_window", lambda handle: True)


def _blank(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


async def _fire_the_flow(
    app: AgentClipApp, pilot: Pilot, seed: Callable[..., None]
) -> MainScreen:
    main = await _at_the_task_prompt(app, pilot)
    _seed_and_reload(main, seed, TemplateKind.COPY)
    main._chat_region = CHAT_REGION
    main._open_reply_gate()
    _detectors(main, "busy")
    await _finishes(main, pilot)
    return main


async def test_a_near_miss_on_the_copy_button_logs_how_close_it_came(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question the whole search extension exists for. "Copy button not
    found" has two causes needing opposite fixes - it is not on screen, or the
    capture has stopped matching it - and only a number tells them apart. A near
    miss means recapture it in F2."""
    _patch_flow_io(monkeypatch)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (None, 0.21))
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _fire_the_flow(app, pilot, seed_templates)
        await _wait_for(
            pilot, lambda: main._loop_state.name == "MANUAL_COPY", "the flow gave up"
        )

        text = await _read_log(app, pilot)
        needed = f"{TemplateKind.COPY.max_diff:.2f}"
        assert f"copy button not found (best candidate diff 0.21, needs ≤ {needed})" in text
        assert "AUTO_COPY → MANUAL_COPY — the copy button was not found on screen" in text


async def test_a_failed_harvest_also_says_what_the_poller_has_seen(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second half of the same diagnosis, and what the always-running
    detector (§3.4f) is read for.

    "How close did THIS frame come" separates a drifted capture from an absent
    icon; "has the poller ever seen one in this window" separates a capture that
    matches nothing ever from an icon that was there a moment ago and is simply
    not drawn on this response. The flow still never CLICKS anything it
    remembers - it re-searches - so this is a sentence in a log and nothing else.
    """
    _patch_flow_io(monkeypatch)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (None, 0.21))
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _seed_and_reload(main, seed_templates, TemplateKind.COPY)
        main._chat_region = CHAT_REGION
        # A detector of the kind the poller builds, and one frame with the
        # captured icon actually in it - so the memory has something in it.
        main._detector = build_detector(
            CHAT_REGION, main._live_profile(), signals=(), required_ticks=4
        )
        assert (
            main._copy_last_seen_note() == "; the poller has never seen it in this window either"
        )

        icon = main._live_profile().variants(TemplateKind.COPY)[0].image
        scene = bytearray(_blank(CHAT_REGION).pixels)
        row = icon.width * 4
        for line in range(icon.height):
            start = ((40 + line) * CHAT_REGION.width + 30) * 4
            scene[start : start + row] = icon.pixels[line * row : (line + 1) * row]
        main._detector.observe(RegionImage(CHAT_REGION.width, CHAT_REGION.height, bytes(scene)))
        assert "the poller last saw one 0s ago" in main._copy_last_seen_note()

        main._open_reply_gate()
        _detectors(main, "busy")
        await _finishes(main, pilot)
        await _wait_for(pilot, lambda: main._loop_state.name == "MANUAL_COPY", "the flow gave up")

        text = await _read_log(app, pilot)
        assert "best candidate diff 0.21" in text
        assert "the poller last saw one" in text


async def test_a_search_with_nothing_even_shaped_like_it_says_so_in_words(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other cause: no candidate was judged at all, so there is no number to
    print and a fabricated one would be worse than none."""
    _patch_flow_io(monkeypatch)
    monkeypatch.setattr(main_mod, "find_lowest_with_best_miss", lambda t, s, **kw: (None, None))
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _fire_the_flow(app, pilot, seed_templates)
        await _wait_for(
            pilot, lambda: main._loop_state.name == "MANUAL_COPY", "the flow gave up"
        )

        text = await _read_log(app, pilot)
        assert "copy button not found (no candidate cleared the first-stage sniff test)" in text


async def test_a_failed_capture_of_the_chat_region_is_logged_as_such(
    tmp_path: Path, seed_templates: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third road to the same MANUAL_COPY box, and one the user can do
    something about (the browser was minimised, the region is off-screen)."""
    _patch_flow_io(monkeypatch)

    def _explode(region: ScreenRegion) -> RegionImage:
        raise CaptureError("the window is minimised")

    monkeypatch.setattr(main_mod, "capture_region", _explode)
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _fire_the_flow(app, pilot, seed_templates)
        await _wait_for(
            pilot, lambda: main._loop_state.name == "MANUAL_COPY", "the flow gave up"
        )

        text = await _read_log(app, pilot)
        assert "could not capture the chat region: the window is minimised" in text
        assert "AUTO_COPY → MANUAL_COPY — the chat region could not be captured" in text


# -- the ready-to-send gate ---------------------------------------------------


async def test_the_send_gate_logs_the_hold_the_sighting_and_the_release(
    tmp_path: Path, seed_templates: Callable[..., None]
) -> None:
    """The gate is entirely invisible unless you know the sidebar's send line by
    heart, and it is the thing most likely to be blamed for "nothing happened
    after I pasted". All three of its ordinary steps get an entry."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _seed_and_reload(main, seed_templates, TemplateKind.SEND_READY)
        main._open_reply_gate()

        await _send_ready(main, pilot, True)  # the button is up: unsent text
        await _send_ready(main, pilot, False)  # ...and gone: that was the Enter

        text = await _read_log(app, pilot)
        assert "holding finish detection until the send is seen" in text
        assert "the ready-to-send button is on screen" in text
        assert "finish detection is released" in text
        assert "→ WAIT_GENERATE — the ready-to-send button went away, which is your Enter" in text


async def test_the_send_gate_logs_an_override_as_better_evidence_not_a_failure(
    tmp_path: Path, seed_templates: Callable[..., None]
) -> None:
    """A first message in a fresh chat can show the send button and never yield
    a clean not-found frame, so the icon releases the gate instead. That is a
    release on better evidence, and the entry says so - it is not a timeout."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _seed_and_reload(main, seed_templates, TemplateKind.SEND_READY, TemplateKind.COPY)
        main._open_reply_gate()
        _detectors(main, "busy")

        await _send_ready(main, pilot, True)
        await _busy(main, pilot, BusyState.MATCH)  # a reasoning icon, on this frame

        text = await _read_log(app, pilot)
        assert "gate overridden by better evidence" in text
        assert "timed out" not in text


async def test_the_send_gate_logs_a_timeout_with_the_case_that_applies(
    tmp_path: Path, seed_templates: Callable[..., None]
) -> None:
    """The two timeout cases need opposite fixes from the user - one capture
    never matches, the other never stops - so the toast names them apart and the
    log keeps that sentence after the toast has gone."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _seed_and_reload(main, seed_templates, TemplateKind.SEND_READY)
        main._open_reply_gate()

        for _ in range(main_mod.SEND_GATE_TIMEOUT_TICKS):
            await _send_ready(main, pilot, False)  # never appears at all

        text = await _read_log(app, pilot)
        assert "gate timed out: the ready-to-send button never appeared after the paste" in text


# -- the session boundary -----------------------------------------------------


async def test_new_logs_the_reset_and_the_log_survives_it(
    tmp_path: Path, new_chat_click_lands: None
) -> None:
    """The rule the whole feature turns on: a wedged user's first move is `/new`,
    and clearing the log there would destroy the evidence they are about to go
    looking for. The reset writes its own entry instead - worded as a reset, not
    as a transition - and everything above it stays readable."""
    app, _ = _make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)

        await send_composer(app, pilot, "/new")
        await _wait_for(pilot, lambda: not main.session_active, "the session was torn down")

        text = await _read_log(app, pilot)
        assert "session reset: the transcript is cleared" in text
        assert "→ IDLE — session reset" in text
        # ...and the run that led here is still there to read.
        assert "AUTO_INSERT → MANUAL_INSERT — the focus click did not land" in text
        assert main.transcript.entries == []  # the transcript, in contrast, went
