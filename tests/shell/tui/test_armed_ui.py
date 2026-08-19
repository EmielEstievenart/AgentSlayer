"""Pilot tests for the global ARMED switch (F5, `/armed`).

DISARMED is one promise - *this app does not touch your machine* - and the only
way to test a promise about the OS is to prove the calls are never made. So
every acting primitive is monkeypatched at its use site
(``main_mod.click_region`` / ``send_paste`` / ``move_cursor`` / ``scroll_region``
/ ``focus_window_verified``) with a fake that appends to a list, and the
assertion is that
the list is still EMPTY. That is the whole shape of this file, and it is the same
patching the suite-wide gate in tests/conftest.py sits underneath: nothing here
may reach the real ``user32``, whether or not a test remembers to patch.

The other half of the promise is what does NOT stop, and it is tested just as
hard: the clipboard still receives the outbound payload (so the user can paste it
themselves), the finish detectors still reach their verdict and still move the
STATE rail, and `i` still ingests a reply the user copied by hand. A switch that
quietly blinded the tool would be a different, worse feature.

``AutomationController.feed_probe`` is the documented injectable path for the
detector poller, used here to drive a genuine finish - a frame that really saw
the busy appearance (``BusyProbe.generating_now``), then two that did not. The
consumption is synchronous; the ``pilot.pause`` after each probe is what lets
the paints it asked for cross back to the sidebar.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

import agentclip.shell.tui.screens.main as main_mod
from agentclip.cli import make_engine_factory
from agentclip.config import load_config
from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.clip.fake import FakeClipboard
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens.main import MainScreen
from agentclip.shell.tui.widgets.sidebar import PASTE_FLASH_TEXT

from .conftest import focus_clicks, send_composer

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
ICON = (24, 24)
SIZE = (110, 40)

# The reply a hand-copied harvest puts on the clipboard - the same canned
# task_done shape test_chat_ui uses, echoing the chat name every engine built by
# these fixtures agrees on.
REPLY_TASK_DONE = """All set - nothing else to change.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Tidied up src/utils.py; nothing else to do.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

UTILS_PY = '''"""Utility helpers."""


def parse_date(s):
    return s
'''


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _make_app(tmp_path: Path, profile_root: Path) -> tuple[AgentClipApp, FakeClipboard]:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "utils.py").write_text(UTILS_PY, encoding="utf-8", newline="")
    config = load_config(project, global_config_path=project / "no-such-global.toml")
    fake = FakeClipboard()
    app = AgentClipApp(
        config=config,
        provider=fake,
        engine_factory=make_engine_factory(lambda: app.app_config, project, "amber-falcon"),
        project_root=project,
        profile_root=profile_root,
    )
    return app, fake


def _service_key(app: AgentClipApp) -> str:
    config = app.app_config
    configured = config.general.service
    return configured if configured in config.services else next(iter(sorted(config.services)))


def _frame(region: ScreenRegion) -> RegionImage:
    return RegionImage(region.width, region.height, b"\x00" * (region.width * region.height * 4))


class _OsCalls:
    """Every synthetic-input call the screen layer can make, recorded not made.

    One object rather than five lists because the assertion is almost always
    about the whole set at once: DISARMED does not mean "fewer clicks", it means
    the OS heard nothing from us at all, and ``assert os_calls.nothing()`` is
    that sentence. The clicks "take" (a real one would land a copy) so an ARMED
    control case can run the same flow to completion.
    """

    def __init__(self, fake: FakeClipboard) -> None:
        self._fake = fake
        self.clicks: list[ScreenRegion] = []
        self.pastes: list[int] = []
        self.moves: list[tuple[int, int]] = []
        self.scrolls: list[tuple[ScreenRegion, int]] = []
        self.focuses: list[int] = []

    def nothing(self) -> bool:
        return not (self.clicks or self.pastes or self.moves or self.scrolls or self.focuses)

    def summary(self) -> str:
        return (
            f"clicks={self.clicks} pastes={self.pastes} moves={self.moves} "
            f"scrolls={self.scrolls} focuses={self.focuses}"
        )


def _patch_os(monkeypatch: pytest.MonkeyPatch, fake: FakeClipboard) -> _OsCalls:
    """Record (never perform) the five acting primitives, plus the capture.

    The capture is faked too, but it is NOT one of the five: reading the screen
    is exactly what stays live while disarmed, and a test that could not capture
    would be testing the wrong thing.
    """
    calls = _OsCalls(fake)

    def fake_click(region: ScreenRegion, *, settle_s: float = 0.0) -> bool:
        calls.clicks.append(region)
        fake.write_text(f"copied {len(calls.clicks)}")  # a real click lands a copy
        return True

    monkeypatch.setattr(main_mod, "click_region", fake_click)
    monkeypatch.setattr(main_mod, "send_paste", lambda: bool(calls.pastes.append(1)) or True)
    monkeypatch.setattr(main_mod, "move_cursor", lambda x, y: bool(calls.moves.append((x, y))) or True)
    monkeypatch.setattr(
        main_mod, "scroll_region", lambda region, n: bool(calls.scrolls.append((region, n))) or True
    )
    monkeypatch.setattr(main_mod, "focus_window_verified", lambda h: bool(calls.focuses.append(h)) or True)
    monkeypatch.setattr(main_mod, "capture_region", _frame)
    return calls


@pytest.fixture(autouse=True)
def _no_detector_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the poller: the finish tests inject their own verdicts, and a live
    one would interleave stale verdicts with them. ``_active_detectors`` is what
    stands in for it - see ``_fire``."""
    monkeypatch.setattr(MainScreen, "_start_detector_worker", lambda self: None)


def _toasts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    notes: list[str] = []
    monkeypatch.setattr(
        MainScreen, "notify", lambda self, message, *a, **kw: notes.append(str(message))
    )
    return notes


def _said(notes: list[str], fragment: str) -> bool:
    return any(fragment in note for note in notes)


def _badge(main: MainScreen) -> Static:
    return main.status_bar.query_one("#seg-armed", Static)


def _banner(main: MainScreen) -> Static:
    return main.sidebar.query_one("#side-armed-banner", Static)


def _disarmed_on_screen(main: MainScreen) -> bool:
    """Both indicators up: the status badge and the sidebar's standing banner."""
    return _badge(main).display and _banner(main).display


def _armed_on_screen(main: MainScreen) -> bool:
    """Armed says nothing anywhere - the badge is an alarm, not furniture."""
    return not _badge(main).display and not _banner(main).display


async def _at_the_task_prompt(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = app.main_screen
    assert main is not None
    await _wait_for(pilot, lambda: main.awaiting_new_session, "composer armed for a task")
    return main


async def _start_session(app: AgentClipApp, pilot: Pilot) -> MainScreen:
    main = await _at_the_task_prompt(app, pilot)
    main.composer.load_text("Tidy up src/utils.py.")
    await pilot.press("enter")
    await _wait_for(pilot, lambda: main.session_active, "session armed")
    await _wait_for(pilot, lambda: main.phase_name == "AWAITING_REPLY", "armed for a reply")
    await _wait_for(pilot, lambda: not main.busy, "session flow settled")
    return main


def _calibrate(main: MainScreen) -> None:
    """A drawn chat window, written straight into the slot (the picker itself is
    test_chat_region_ui's subject)."""
    main._chat_region = CHAT_REGION


async def _fire(main: MainScreen, pilot: Pilot) -> None:
    """A genuine finish: MATCH-that-really-saw-the-icon, then two CHANGED.

    ``_active_detectors`` declares which detectors the poller would be posting
    (verdicts from any other are dropped as a cancelled loop's leftovers), and
    the reply gate is opened the way ``copy_outbound`` opens it - a verdict may
    only reach for the mouse while a reply is genuinely outstanding.
    """
    main._active_detectors = ("busy",)
    main._open_reply_gate()
    for state in (BusyState.MATCH, BusyState.CHANGED, BusyState.CHANGED):
        main._automation.feed_probe("busy", BusyProbe(state, 0.2, state is BusyState.MATCH))
        await pilot.pause()


# == the switch itself =========================================================


async def test_f5_toggles_the_switch_and_paints_both_indicators(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two indicators are not decoration: a user who disarmed an hour ago
    and came back must be able to tell at a glance, from either the status bar
    they read every turn or the column they watch the loop in.

    Both paint synchronously on the toggle - no repaint from a status push is
    coming, because disarming touches no session at all."""
    app, _ = _make_app(tmp_path, profile_root)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        assert main._os_armed is True  # armed is the default, i.e. every old release
        assert _armed_on_screen(main)

        await pilot.press("f5")
        await pilot.pause()
        assert main._os_armed is False
        assert _disarmed_on_screen(main)
        assert "DISARMED" in str(_badge(main).render())
        assert _said(notes, "DISARMED - watching only")
        # The toast has to say what stopped, or "disarmed" reads as "broken".
        assert _said(notes, "no clicks, no paste, no clipboard watch")

        await pilot.press("f5")
        await pilot.pause()
        assert main._os_armed is True
        assert _armed_on_screen(main)
        assert _said(notes, "ARMED - automation restored")


async def test_f5_works_before_any_session_exists_and_while_one_runs(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding hangs off no ``check_action`` on purpose. A switch that only
    worked in a healthy session would be missing in exactly the state a user
    reaches for it - and the launch screen, before anything is armed, is one of
    those states."""
    app, _ = _make_app(tmp_path, profile_root)
    _toasts(monkeypatch)
    _patch_os(monkeypatch, FakeClipboard())
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        assert not main.session_active

        await pilot.press("f5")  # no session at all
        await pilot.pause()
        assert main._os_armed is False

        await pilot.press("f5")
        await pilot.pause()
        await _start_session(app, pilot)

        await pilot.press("f5")  # ...and mid-session, with the composer focused
        await pilot.pause()
        assert main._os_armed is False
        assert _disarmed_on_screen(main)


@pytest.mark.parametrize(
    ("typed", "wanted"),
    [("/armed", False), ("/armed off", False), ("/armed on", True)],
)
async def test_the_armed_command_needs_no_session(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    typed: str,
    wanted: bool,
) -> None:
    """`/identify`'s rule, for the same reason: typed at the task prompt it runs
    rather than becoming the opening message to the model. ``/armed on`` from an
    already-armed app is a no-op that still confirms itself."""
    app, _ = _make_app(tmp_path, profile_root)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)

        await send_composer(app, pilot, typed)
        await _wait_for(pilot, lambda: main._os_armed is wanted, f"{typed} took effect")

        assert main.awaiting_new_session  # the prompt never resolved...
        assert not main.session_active  # ...so nothing was sent to the model
        assert not any(typed in entry for entry in main.transcript.entries)
        assert _said(notes, "ARMED" if wanted else "DISARMED")


# == chokepoint 1: the paste path ==============================================


async def test_a_disarmed_outbound_still_reaches_the_clipboard_but_nothing_is_clicked(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heart of the feature. The payload MUST still land on the clipboard -
    otherwise disarming would not stop the automation, it would break the app -
    and the click and the synthetic Ctrl+V must not happen. What is left is the
    pre-existing manual path: MANUAL_INSERT on the rail and the Ctrl+V nag."""
    app, fake = _make_app(tmp_path, profile_root)
    calls = _patch_os(monkeypatch, fake)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)
        main.set_os_armed(False)
        calls.clicks.clear()

        await main.copy_outbound("PAYLOAD-FOR-THE-USER")
        await pilot.pause()  # the banner goes up through the paint queue now

        assert fake.read_text() == "PAYLOAD-FOR-THE-USER"  # the write still happened
        assert calls.nothing(), calls.summary()
        assert main._loop_state is LoopState.MANUAL_INSERT
        # ...and the existing nag is what tells the user their half of the deal.
        flash = main.sidebar.query_one("#side-paste-flash", Static)
        assert flash.display
        assert str(flash.render()) == PASTE_FLASH_TEXT
        assert _said(notes, "press Ctrl+V yourself")


async def test_the_same_outbound_armed_does_click_and_paste(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case, without which the test above proves nothing: the very
    same call on an armed app really does reach for the mouse and the keyboard.
    """
    app, fake = _make_app(tmp_path, profile_root)
    calls = _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)

        await main.copy_outbound("PAYLOAD-FOR-THE-USER")

        assert calls.clicks == focus_clicks(CHAT_REGION)  # the chat box, else the drawn region
        assert calls.pastes == [1]
        assert main._loop_state is LoopState.WAIT_SEND


# == chokepoint 2: the find-then-click primitive ===============================


async def test_disarmed_new_opens_no_browser_chat_but_still_resets_the_tool_side(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """`/new` is the command that drives the browser, and disarmed the browser
    half is refused entire: nothing captured, nothing searched, nothing clicked
    or focused. What the switch does NOT cover is the tool's own session - the
    user asked for a new conversation and that half costs the machine nothing -
    so it starts, and the toast names the switch as the reason the chat on
    screen is still the old one.

    The new-chat button is captured and the window drawn, so nothing but the
    switch itself can be doing the refusing."""
    app, fake = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.NEW_CHAT, size=ICON)
    calls = _patch_os(monkeypatch, fake)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        _calibrate(main)
        assert main._active_profile().has(TemplateKind.NEW_CHAT)
        main.set_os_armed(False)
        calls.clicks.clear()
        notes.clear()

        await send_composer(app, pilot, "/new")
        await _wait_for(pilot, lambda: _said(notes, "disarmed"), "the refusal toast")
        await _wait_for(pilot, lambda: main.awaiting_new_session, "the fresh session")

        assert calls.nothing(), calls.summary()
        assert _said(notes, "no new chat was opened")
        assert _said(notes, "open a new browser chat yourself")
        assert not main.transcript.entries  # the tool side really did start over


async def test_a_disarmed_delegation_is_refused_before_anything_is_pasted(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The other caller of the same primitive. ``start_browser_chat`` is
    all-or-nothing by contract - a False return must mean nothing was clicked
    and nothing retargeted - and the switch has to honour that too, because a
    sub-agent's bootstrap pasted into the master's chat corrupts it for good."""
    app, fake = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.NEW_CHAT, size=ICON)
    calls = _patch_os(monkeypatch, fake)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)
        main.set_os_armed(False)
        live_before = main._live

        started = await main.start_browser_chat(main_mod.AgentSlot.SUBAGENT)

        assert started is False
        assert calls.nothing(), calls.summary()
        assert main._live is live_before  # no retarget: the master is still live
        assert _said(notes, "nothing was delegated")


# == chokepoint 3: the finish decision =========================================


async def test_a_disarmed_finish_lands_on_manual_copy_and_launches_no_flow(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Detection is emphatically NOT what disarming turns off. The probes land,
    the verdicts are folded, the decision is reached - and then the one step
    that would have driven the mouse is replaced by "the harvest is yours".

    The copy button IS captured here, so the MANUAL_COPY landing is the switch's
    doing and not the pre-existing no-appearance path."""
    app, fake = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.COPY, size=ICON)
    calls = _patch_os(monkeypatch, fake)
    notes = _toasts(monkeypatch)
    flows: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        flows.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)
        assert main._active_profile().has(TemplateKind.COPY)
        main.set_os_armed(False)

        await _fire(main, pilot)
        await pilot.pause(0.1)

        assert flows == []  # the flow never even started
        assert main._flow_running is False
        assert main._loop_state is LoopState.MANUAL_COPY
        assert calls.nothing(), calls.summary()
        # The detector bookkeeping is untouched, so the sidebar keeps telling the
        # truth about a turn that really did finish.
        assert main._copy_armed is True
        # ...and the hint must not promise an ingest that cannot happen: the
        # watcher is off too, so the user is told both halves of their job.
        assert _said(notes, "copy it yourself")
        assert _said(notes, "press i to ingest it")


async def test_the_same_finish_armed_does_fire_the_flow(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """The control case for the finish gate: the identical probe sequence on an
    armed app reaches the auto-copy flow."""
    app, fake = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.COPY, size=ICON)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    flows: list[None] = []

    async def fake_flow(self: MainScreen) -> None:
        flows.append(None)

    monkeypatch.setattr(MainScreen, "_auto_copy_flow", fake_flow)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)

        await _fire(main, pilot)
        await _wait_for(pilot, lambda: len(flows) == 1, "the auto-copy flow fired")


async def test_disarming_mid_turn_leaves_the_detectors_bookkeeping_alone(
    tmp_path: Path,
    profile_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_templates: Callable[..., None],
) -> None:
    """Flipping the switch is not a reset. The outstanding-reply flag, the send
    gate and the finish trigger are all fed by live detection, so clearing them
    would only make the sidebar lie about a turn that is genuinely in flight."""
    app, fake = _make_app(tmp_path, profile_root)
    seed_templates(_service_key(app), TemplateKind.COPY, size=ICON)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)
        main._active_detectors = ("busy",)
        main._open_reply_gate()
        main._automation.feed_probe("busy", BusyProbe(BusyState.MATCH, 0.2, True))
        await pilot.pause()
        assert main._copy_armed is True
        assert main._awaiting_pasted_reply is True

        main.set_os_armed(False)
        await pilot.pause()

        assert main._copy_armed is True
        assert main._awaiting_pasted_reply is True


# == chokepoint 4: the clipboard watcher =======================================


async def test_disarming_stops_the_watcher_and_re_arming_starts_it_again(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watching a clipboard the user has not offered us is an action too - it is
    the one that ingests, and ingesting drives a whole turn. So it stops."""
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher running")

        main.set_os_armed(False)
        await pilot.pause()
        assert main._watch_worker is None
        assert main.watch_paused is True

        main.set_os_armed(True)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher restored")
        assert main.watch_paused is False


async def test_re_arming_restores_the_watcher_the_user_had_not_the_one_they_paused(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `w`-reconciliation rule, which is the only interesting choice here:
    the disarm remembers what the watcher was doing and the re-arm puts THAT
    back. A user who paused it themselves, disarmed, and re-armed does not get
    handed back a watcher they had switched off - re-arming undoes the disarm,
    nothing more."""
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher running")

        main.action_toggle_watch()  # the user's own `w`
        await pilot.pause()
        assert main._watch_worker is None and main.watch_paused is True

        main.set_os_armed(False)
        await pilot.pause()
        main.set_os_armed(True)
        await pilot.pause(0.1)

        assert main._watch_worker is None  # still paused, as the user left it
        assert main.watch_paused is True


async def test_disarming_twice_does_not_lose_the_watcher_on_re_arm(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression. `/armed off` typed twice (or F5 raced by a command) must not
    let the second call re-read the already-stopped worker and remember "it was
    off" - that would silently swallow the watcher the first call took away, and
    the user would re-arm into an app that never ingests again. The watcher
    bookkeeping moves on transitions only; the paint and the toast do not."""
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher running")

        main.set_os_armed(False)
        main.set_os_armed(False)  # the second one must be inert for the watcher
        await pilot.pause()
        assert main._watch_worker is None
        assert notes.count(notes[-1]) == 2  # ...but it still confirmed itself

        main.set_os_armed(True)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher restored")


async def test_the_watcher_key_is_refused_and_hidden_while_disarmed(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no state in which a disarmed app may poll the clipboard, so `w`
    is hidden the way it is in manual-clipboard mode rather than dimmed - and
    the action itself refuses, for the command-palette door into it."""
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    notes = _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        assert main.check_action("toggle_watch", ()) is True

        main.set_os_armed(False)
        await pilot.pause()
        assert main.check_action("toggle_watch", ()) is False

        notes.clear()
        main.action_toggle_watch()
        await pilot.pause()
        assert main._watch_worker is None
        assert _said(notes, "the clipboard watcher stays off until F5")


async def test_a_session_started_while_disarmed_gets_its_watcher_on_re_arm(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session asked for a watcher and was refused; re-arming is when that
    request is finally honoured. Without this the user would have to press F5
    and then `w` to get back to a normal app."""
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        main.set_os_armed(False)
        await _start_session(app, pilot)
        await pilot.pause(0.1)
        assert main._watch_worker is None  # the session never got one

        main.set_os_armed(True)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher started on re-arm")


async def test_quitting_stops_the_watcher(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown, which is a promise about the OS just like DISARMED is: after the
    app is gone, nothing of ours is still reading the user's clipboard.

    It used to come for free - Textual cancels the workers a node started when it
    unmounts, and the watcher was one of them. The thread belongs to the
    AutomationController now, so the screen asks for it by name on unmount, and
    this is the test that would notice if that line disappeared.
    """
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _start_session(app, pilot)
        await _wait_for(pilot, lambda: main._watch_worker is not None, "watcher running")
        thread = main._watch_worker
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive(), "the watcher thread outlived the app"


# == what keeps working ========================================================


async def test_force_ingest_still_reads_the_clipboard_while_disarmed(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`i` is the user handing us their clipboard once, on purpose - not a poll
    of it - and it is what makes the whole manual path completable: with the
    watcher stopped, this is the only way a hand-copied reply gets in.

    So the loop really does close while disarmed: the outbound went out via the
    clipboard, the user pasted and copied by hand, and `i` finishes the turn."""
    app, fake = _make_app(tmp_path, profile_root)
    calls = _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        main.set_os_armed(False)
        await _start_session(app, pilot)
        # `i` is still offered - it is not an action on the world.
        assert main.check_action("force_ingest", ()) is True

        fake.write_text(REPLY_TASK_DONE)  # as if the user copied the reply themselves
        main.action_force_ingest()
        await _wait_for(pilot, lambda: main.phase_name == "DONE", "the hand-copied reply landed")

        assert calls.nothing(), calls.summary()
        assert main._os_armed is False  # ...and a completed turn does not re-arm us


async def test_detection_and_the_state_rail_stay_live_while_disarmed(
    tmp_path: Path, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instruments are the whole reason to disarm rather than quit: a user
    watching the tool misbehave needs to keep seeing what it thinks it sees."""
    app, fake = _make_app(tmp_path, profile_root)
    _patch_os(monkeypatch, fake)
    _toasts(monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        main = await _at_the_task_prompt(app, pilot)
        _calibrate(main)
        main.set_os_armed(False)
        main._active_detectors = ("busy",)
        main._open_reply_gate()

        main._automation.feed_probe("busy", BusyProbe(BusyState.MATCH, 0.2, True))
        await pilot.pause()

        # The verdict reached the sidebar's DETECTION block...
        assert "match" in str(main.sidebar.query_one("#side-tpl-busy", Static).render()).lower()
        # ...and the rail moved to "the model is generating", off live evidence.
        assert main._loop_state is LoopState.WAIT_GENERATE
