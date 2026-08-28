"""§11.9: the ENGINE this window builds is built for the monitor's service.

The Chat UI half of the seam. tests/engine/test_live_preset.py pins what an
engine does with a live preset; this pins where the answer comes from - a
``Watched`` off the wire, turned into a ``ServicePreset`` by
``preset_from_watched``, handed to the engine factory as a QUESTION rather than
a value.

The report was one sentence: "I changed paste size in the monitor and it has no
effect, not even when connecting a new GUI." Both halves are here - a budget
this host's ``[services.*]`` never heard of decides the bootstrap, and a budget
the monitor moves mid-session decides the next turn.
"""

from __future__ import annotations

from dataclasses import replace

from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.engine.link.factory import EngineRequest
from agentclip.shell.app.link import Link, LocalLink
from tests.shell.chat.conftest import Harness

CHAT_REGION = ScreenRegion(1050, 340, 812, 540)

#: A number no built-in preset carries, so an assertion on it can only be
#: satisfied by the monitor's answer having travelled the whole way.
MONITOR_BUDGET = 17_000


async def monitor_says(harness: Harness, **spec: object) -> str:
    """Point the fake monitor at a window described by ``spec`` and let this
    view adopt the answer - the one road a preset takes into the Chat UI."""
    target = replace(harness.monitor.specs_for[AgentSlot.MASTER], region=CHAT_REGION, **spec)
    harness.monitor.specs_for = dict.fromkeys(AgentSlot, target)
    await harness.view._retarget_monitor()
    return target.service


def a_session(harness: Harness) -> Link:
    """One engine, built exactly as the controller builds one for a session."""
    factory = harness.view._controller._engine_factory
    return factory(EngineRequest(service=harness.view._service_for(AgentSlot.MASTER)))


async def test_the_engine_is_built_for_the_budget_the_monitor_reports(
    harness: Harness,
) -> None:
    await monitor_says(harness, max_paste_chars=MONITOR_BUDGET)
    preset = harness.view.engine_preset()
    assert preset is not None and preset.max_paste_chars == MONITOR_BUDGET
    assert harness.view._config.preset().max_paste_chars != MONITOR_BUDGET

    link = a_session(harness)
    assert (await link.status()).budget_chars == MONITOR_BUDGET


async def test_a_budget_changed_mid_session_reaches_the_running_engine(
    harness: Harness,
) -> None:
    """No restart, no ``/new``, no reconnect: the engine asks again every turn,
    so the save in the Monitor UI is in force for the next payload."""
    await monitor_says(harness, max_paste_chars=MONITOR_BUDGET)
    link = a_session(harness)
    assert isinstance(link, LocalLink)
    await link.start_task("do the thing")

    await monitor_says(harness, max_paste_chars=MONITOR_BUDGET * 2)
    assert (await link.status()).budget_chars == MONITOR_BUDGET * 2


async def test_the_rest_of_the_engine_s_preset_travels_too(harness: Harness) -> None:
    """Not just the budget: the fence rule, the extra instructions and the
    ranged-edit catalog are the monitor's answer as well (§10.5's preset half).
    """
    extra = "always put a space between ] and ( in code you send"
    await monitor_says(
        harness,
        max_paste_chars=MONITOR_BUDGET,
        require_fenced_reply=True,
        extra_instructions=extra,
        edit_by_lines=True,
    )
    preset = harness.view.engine_preset()
    assert preset is not None
    assert preset.require_fenced_reply is True
    assert preset.extra_instructions == extra
    assert preset.edit_by_lines is True

    link = a_session(harness)
    payload = (await link.start_task("t")).chunks[0]
    assert "replace_lines(" in payload  # the catalog the bootstrap teaches
    assert extra in payload
    assert (await link.status()).has_extra_instructions is True


async def test_the_alarm_stays_this_machine_s(harness: Harness) -> None:
    """§10.5's exception, unchanged by this wave: the uh-oh sound plays where
    the USER is sitting, so it is read off this host's config and not off the
    wire (``preset_from_watched(alerts=...)``)."""
    await monitor_says(harness, max_paste_chars=MONITOR_BUDGET)
    preset = harness.view.engine_preset()
    assert preset is not None
    assert preset.alert_sound == harness.view._config.preset().alert_sound


async def test_with_no_monitor_the_local_table_is_what_a_session_gets(
    harness: Harness,
) -> None:
    """The fallback, and the reason ``engine_preset`` may answer None at all: an
    idle window (§11.1) reports ``EMPTY_WATCHED``, whose paste budget is zero -
    composing against that would be a session that could never arm."""
    harness.monitor.specs_for = {}
    harness.monitor.spec = None
    await harness.view._retarget_monitor()

    assert harness.view.engine_preset() is None
    link = a_session(harness)
    local = harness.view._config.preset()
    assert (await link.status()).budget_chars == local.max_paste_chars
