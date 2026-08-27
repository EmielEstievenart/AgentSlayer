"""``watch(slot)``: the monitor's own service, answered whole - ui-monitor.md §10.5.

The inversion this file pins. Before wave 3 the brain composed a ``MonitorSpec``
and sent it, and the two halves then disagreed about where the chat region and
the service key lived - every "it works locally but not split" report was that
disagreement. Now the brain names a WINDOW and the monitor answers with the
service: its key, its label, the box it actually watches, whether this machine
has appearances for it, the generation its ticks will carry, and the eleven
preset fields a brain acts on.

Three layers again, the way ``test_regions.py`` has them. The **spec source**
(``spec_from_preset`` and the headless ``spec_for_config``) is where a
``[services.*]`` row becomes a target. The **monitor** runs it and folds the
region store in on the way through. The **fake** answers the same shape without
a filesystem, so a brain-side suite can stage "the monitor over there is running
zai" in one assignment.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentclip.config import ServicePreset, load_config
from agentclip.driver.monitor.__main__ import build_monitor, service_key, spec_for_config
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.protocol import (
    EMPTY_WATCHED,
    MonitorSpec,
    spec_from_preset,
    watched_from,
)
from agentclip.driver.monitor.regions import save_region
from agentclip.driver.screen.profile import ServiceProfile
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot

from .conftest import spec

CHAT = ScreenRegion(-120, 40, 800, 600)
OTHER = ScreenRegion(10, 20, 300, 400)

# A preset with nothing left at its default, so a field dropped anywhere between
# the config row and the brain's answer fails rather than accidentally agreeing.
PRESET = ServicePreset(
    key="zai",
    label="Z.ai chat",
    max_paste_chars=9_000,
    total_context_chars=250_000,
    wrap_blocks_in_fence=False,
    attachment_note=False,
    stable_seconds=3.5,
    finish_signals=("busy", "stale"),
    hover_scan=True,
    delivery="stream",
    matcher="opencv",
    tolerance=12,
    scroll_action="end",
    auto_submit=True,
    require_fenced_reply=True,
    extra_instructions="mind the ]( sequences",
    snap_back=False,
)


def monitor(
    regions_dir: Path | None = None,
    *,
    spec_for: object = None,
    profiled: bool = False,
) -> LocalUIMonitor:
    """A monitor with no appearances, so nothing here starts a poll thread.

    A profile-less service leaves the monitor configured and idle (§3), which is
    what lets every test below call ``watch`` and have nothing to tear down.
    ``profiled=True`` hands back an empty profile object instead of None - which
    is what "this machine HAS captures for that key" looks like to ``watched``.
    """
    return LocalUIMonitor(
        profile_for=lambda key: ServiceProfile(key) if profiled else None,
        regions_dir=regions_dir,
        spec_for=spec_for,  # type: ignore[arg-type]
    )


# == a config row becomes a target =============================================


def test_a_preset_becomes_a_spec_field_for_field() -> None:
    """The one place the two doors share, so the headless monitor and the
    Monitor UI cannot come to different answers about the same row."""
    built = spec_from_preset(PRESET, CHAT)
    assert built.service == "zai"
    assert built.label == "Z.ai chat"
    assert built.region == CHAT
    assert built.finish_signals == ("busy", "stale")
    assert built.stable_seconds == 3.5
    assert built.tolerance == 12
    assert built.matcher == "opencv"
    assert built.hover_scan is True
    assert built.scroll_action == "end"
    assert built.snap_back is False
    assert built.delivery == "stream"
    assert built.auto_submit is True
    assert built.max_paste_chars == 9_000
    assert built.total_context_chars == 250_000
    assert built.wrap_blocks_in_fence is False
    assert built.attachment_note is False
    assert built.require_fenced_reply is True
    assert built.extra_instructions == "mind the ]( sequences"


def test_a_spec_carries_no_region_of_its_own_by_default() -> None:
    """The rectangle is not in the preset at all - it is a fact about this
    desktop - so the honest default is "let the store fill it"."""
    assert spec_from_preset(PRESET).region is None


def test_the_key_may_be_overridden_when_the_preset_is_a_fallback() -> None:
    """``general.service`` naming a row that does not exist: the caller falls
    back to a default PRESET but keeps the KEY, because the profile store and
    the region store are both indexed by the key."""
    assert spec_from_preset(PRESET, service="missing").service == "missing"


def test_watched_is_built_from_the_spec_and_says_which_run_it_describes() -> None:
    built = watched_from(spec_from_preset(PRESET, CHAT), profiled=True, generation=7)
    assert (built.service, built.label, built.region) == ("zai", "Z.ai chat", CHAT)
    assert (built.profiled, built.generation) == (True, 7)
    assert built.max_paste_chars == 9_000
    assert built.extra_instructions == "mind the ]( sequences"


# == the headless monitor's own spec_for =======================================


def config_at(tmp_path: Path, **general: str) -> object:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    body = "\n".join(f'{key} = "{value}"' for key, value in general.items())
    (tmp_path / "global.toml").write_text(f"[general]\n{body}\n", encoding="utf-8")
    return load_config(root, global_config_path=tmp_path / "global.toml")


def test_each_window_gets_the_service_the_config_names_for_it(tmp_path: Path) -> None:
    config = config_at(tmp_path, service="chatgpt", subagent_service="claude")
    spec_for = spec_for_config(config)  # type: ignore[arg-type]
    assert spec_for(AgentSlot.MASTER).service == "chatgpt"
    assert spec_for(AgentSlot.SUBAGENT).service == "claude"


def test_a_subagent_service_that_names_nothing_falls_back_to_the_master(
    tmp_path: Path,
) -> None:
    """The same fallback the Monitor UI's header does, said once so the poller
    and the panel cannot disagree about which row a window is."""
    config = config_at(tmp_path, service="chatgpt", subagent_service="no-such-service")
    assert service_key(config, AgentSlot.SUBAGENT) == "chatgpt"  # type: ignore[arg-type]


def test_the_headless_spec_leaves_the_region_to_the_store(tmp_path: Path) -> None:
    config = config_at(tmp_path, service="chatgpt")
    assert spec_for_config(config)(AgentSlot.MASTER).region is None  # type: ignore[arg-type]


async def test_build_monitor_hands_the_config_over_as_the_monitors_own_spec_for(
    tmp_path: Path,
) -> None:
    """The headless door's whole half of §10.5: a process with no window still
    answers ``watch`` out of its own configuration."""
    config = config_at(tmp_path, service="chatgpt", subagent_service="claude")
    args = argparse.Namespace(
        project=str(tmp_path / "project"),
        service=None,
        global_config=None,
        profile_root=str(tmp_path / "profiles"),
        config_dir=str(tmp_path / "monitor"),
    )
    live = build_monitor(args, config)  # type: ignore[arg-type]
    try:
        assert (await live.watch(AgentSlot.SUBAGENT)).service == "claude"
    finally:
        await live.close()


# == the monitor runs it =======================================================


async def test_watch_configures_from_the_monitors_own_spec_for() -> None:
    """The brain names a window; everything else is answered on this side."""
    asked: list[AgentSlot] = []

    def spec_for(slot: AgentSlot) -> MonitorSpec:
        asked.append(slot)
        return spec_from_preset(PRESET, CHAT)

    live = monitor(spec_for=spec_for, profiled=True)
    try:
        watched = await live.watch(AgentSlot.SUBAGENT)
        assert asked == [AgentSlot.SUBAGENT]
        assert watched.service == "zai"
        assert watched.region == CHAT
        assert watched.profiled is True
        assert watched.generation == live.generation == 1
        assert watched.max_paste_chars == 9_000
        assert watched.delivery == "stream"
        # ...and the same answer is re-readable without a retarget, which is how
        # a brain re-reads after a tick carrying a generation it has not seen.
        assert await live.watched() == watched
    finally:
        await live.close()


async def test_watch_fills_the_region_from_this_machines_store(tmp_path: Path) -> None:
    """§10.5 meets §8: the preset carries no rectangle, and the box the operator
    drew on THIS desktop is what the brain is told it is watching."""
    save_region(tmp_path, "zai", CHAT)
    live = monitor(tmp_path, spec_for=lambda _slot: spec_from_preset(PRESET))
    try:
        assert (await live.watch(AgentSlot.MASTER)).region == CHAT
    finally:
        await live.close()


async def test_watch_with_no_configuration_retargets_nothing() -> None:
    """ "There is nothing over here to watch" is an answer, not a failure - the
    same shape a service with no profile gets."""
    live = monitor()
    try:
        assert await live.watch(AgentSlot.MASTER) == EMPTY_WATCHED
        assert live.generation == 0, "an empty watch bumped the generation anyway"
    finally:
        await live.close()


async def test_the_window_may_replace_the_spec_source_after_construction() -> None:
    """The knot the setter exists for: the Monitor UI's view is built OVER the
    monitor, so the monitor has to exist first and be told second."""
    live = monitor()
    try:
        live.set_spec_for(lambda _slot: spec_from_preset(PRESET, OTHER))
        assert (await live.watch(AgentSlot.MASTER)).region == OTHER
        live.set_spec_for(None)
        assert (await live.watch(AgentSlot.MASTER)).region == OTHER, "the answer was forgotten"
        assert live.generation == 1, "a forgotten spec source retargeted anyway"
    finally:
        await live.close()


async def test_an_unprofiled_service_says_so_rather_than_going_quiet() -> None:
    """The split-mode trap made visible: a brain driving a service this machine
    has no captures for gets NOT_CALIBRATED on every click, and this is the only
    field that says why."""
    live = monitor(spec_for=lambda _slot: spec_from_preset(PRESET, CHAT))
    try:
        assert (await live.watch(AgentSlot.MASTER)).profiled is False
    finally:
        await live.close()


# == the fake ==================================================================


async def test_the_fake_watches_a_spec_per_slot() -> None:
    fake = FakeUIMonitor()
    fake.specs_for[AgentSlot.MASTER] = spec(service="chatgpt", region=CHAT)
    fake.specs_for[AgentSlot.SUBAGENT] = spec(service="claude", region=OTHER)

    assert (await fake.watch(AgentSlot.SUBAGENT)).service == "claude"
    assert (await fake.watch(AgentSlot.MASTER)).service == "chatgpt"
    assert fake.watches == [AgentSlot.SUBAGENT, AgentSlot.MASTER]
    assert [name for name, _args in fake.calls if name == "watch"] == ["watch", "watch"]


async def test_the_fake_answers_a_service_called_fake_until_a_suite_says_otherwise() -> None:
    fake = FakeUIMonitor()
    watched = await fake.watch(AgentSlot.MASTER)
    assert watched.service == "fake"
    assert watched.region is None, "a double should start uncalibrated"
    assert watched.profiled is True


async def test_the_fakes_watched_carries_the_preset_and_the_generation() -> None:
    """A double whose ``watched`` dropped a preset field would be a brain-side
    suite passing against nothing."""
    fake = FakeUIMonitor()
    fake.specs_for[AgentSlot.MASTER] = spec_from_preset(PRESET, CHAT)
    fake.profiled = False

    watched = await fake.watch(AgentSlot.MASTER)
    assert watched.label == "Z.ai chat"
    assert watched.max_paste_chars == 9_000
    assert watched.require_fenced_reply is True
    assert watched.profiled is False
    assert watched.generation == fake.generations == 1


async def test_an_installed_spec_for_wins_over_the_fakes_dict() -> None:
    """A window driving the monitor is the LIVE answer; the dict is the resting
    one. This is the seam the Monitor UI's view hangs its ``_spec`` on."""
    fake = FakeUIMonitor()
    fake.specs_for[AgentSlot.MASTER] = spec(service="resting")
    fake.set_spec_for(lambda _slot: spec(service="live"))
    assert (await fake.watch(AgentSlot.MASTER)).service == "live"


async def test_the_fake_with_nothing_configured_for_a_slot_answers_the_empty_one() -> None:
    fake = FakeUIMonitor()
    fake.specs_for.clear()
    assert await fake.watch(AgentSlot.SUBAGENT) == EMPTY_WATCHED


@pytest.mark.parametrize("slot", list(AgentSlot))
def test_every_slot_has_a_default_spec(slot: AgentSlot) -> None:
    """Both windows, because a story that only configured one would make the
    other's ``watch`` mean "nothing here" by accident."""
    fake = FakeUIMonitor()
    assert slot in fake.specs_for
