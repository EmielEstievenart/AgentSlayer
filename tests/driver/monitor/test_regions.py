"""Chat regions remembered on the monitor's own machine - §8's open point.

Three layers, and the middle one is the point of the other two. The **store**
(``regions.py``) is a small JSON file that survives being corrupted. The
**monitor** consults it inside ``configure``: a spec that carries a region is
authoritative and is saved, a spec with none falls back to what the operator
drew here last time - which is what makes a rebooted VM keep its calibration
even though the brain that reconnects has no idea where the window is. The
**fake** records the same fact so a brain-side suite can assert the save without
a filesystem.
"""

from __future__ import annotations

from pathlib import Path

from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.driver.monitor.regions import (
    drop_region,
    load_region,
    load_regions,
    regions_path,
    save_region,
)
from agentclip.driver.screen.region import ScreenRegion

from .conftest import spec

# Deliberately negative in one axis: a chat window on a display left of the
# primary one has negative origins, and these are virtual-screen coordinates.
CHAT = ScreenRegion(-120, 40, 800, 600)
OTHER = ScreenRegion(10, 20, 300, 400)


def monitor(regions_dir: Path | None) -> LocalUIMonitor:
    """A monitor that resolves no profile, so ``configure`` starts no thread.

    This file is about the region store and nothing else: a profile-less service
    leaves the monitor configured and idle (§3), which is exactly the shape that
    lets these tests call ``configure`` without a poll loop to tear down.
    """
    return LocalUIMonitor(profile_for=lambda _key: None, regions_dir=regions_dir)


# == the store =================================================================


def test_a_region_round_trips_through_the_file(tmp_path: Path) -> None:
    assert load_region(tmp_path, "svc") is None
    save_region(tmp_path, "svc", CHAT)
    assert load_region(tmp_path, "svc") == CHAT
    assert regions_path(tmp_path).exists()


def test_regions_are_keyed_by_service(tmp_path: Path) -> None:
    save_region(tmp_path, "svc", CHAT)
    save_region(tmp_path, "other", OTHER)
    assert load_regions(tmp_path) == {"svc": CHAT, "other": OTHER}
    save_region(tmp_path, "svc", OTHER)
    assert load_regions(tmp_path) == {"svc": OTHER, "other": OTHER}


def test_dropping_forgets_one_service_and_says_whether_there_was_one(
    tmp_path: Path,
) -> None:
    save_region(tmp_path, "svc", CHAT)
    save_region(tmp_path, "other", OTHER)
    assert drop_region(tmp_path, "svc") is True
    assert drop_region(tmp_path, "svc") is False
    assert load_regions(tmp_path) == {"other": OTHER}


def test_an_unreadable_file_is_no_regions_rather_than_a_crash(tmp_path: Path) -> None:
    """The fallback from "I do not remember where the chat is" is the state the
    monitor is in on its first ever run, and that state works - so a truncated
    file must not stop a VM from polling."""
    path = regions_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "regions": {"svc": ', encoding="utf-8")
    assert load_regions(tmp_path) == {}
    # And it heals: the next save writes a whole document over the wreck.
    save_region(tmp_path, "svc", CHAT)
    assert load_region(tmp_path, "svc") == CHAT


def test_one_bad_entry_costs_one_service_and_not_the_file(tmp_path: Path) -> None:
    save_region(tmp_path, "svc", CHAT)
    path = regions_path(tmp_path)
    document = path.read_text(encoding="utf-8")
    path.write_text(
        document.replace('"regions": {', '"regions": {"broken": {"left": 1}, ', 1),
        encoding="utf-8",
    )
    assert load_regions(tmp_path) == {"svc": CHAT}


# == the monitor ===============================================================


async def test_configuring_with_a_region_remembers_it(tmp_path: Path) -> None:
    live = monitor(tmp_path)
    try:
        await live.configure(spec(region=CHAT, service="svc"))
        assert load_region(tmp_path, "svc") == CHAT
        assert live.saved_region("svc") == CHAT
    finally:
        await live.close()


async def test_a_region_less_spec_falls_back_to_what_was_drawn_here(
    tmp_path: Path,
) -> None:
    """The point of the whole feature: the monitor outlives the brain (§2.8), so
    a reconnecting brain that has no rectangle gets the one this machine has."""
    save_region(tmp_path, "svc", CHAT)
    live = monitor(tmp_path)
    try:
        await live.configure(spec(region=None, service="svc"))
        assert live.spec is not None
        assert live.spec.region == CHAT
        # ...and the brain can ASK for it, which is the whole point over a wire.
        assert await live.configured_region() == CHAT
    finally:
        await live.close()


async def test_the_brain_wins_when_it_has_an_opinion(tmp_path: Path) -> None:
    save_region(tmp_path, "svc", CHAT)
    live = monitor(tmp_path)
    try:
        await live.configure(spec(region=OTHER, service="svc"))
        assert live.spec is not None
        assert live.spec.region == OTHER
        assert load_region(tmp_path, "svc") == OTHER
    finally:
        await live.close()


async def test_a_service_with_nothing_saved_stays_region_less(tmp_path: Path) -> None:
    """"Nothing is calibrated" has to survive the store, or the monitor would
    start watching a rectangle it made up."""
    save_region(tmp_path, "other", OTHER)
    live = monitor(tmp_path)
    try:
        await live.configure(spec(region=None, service="svc"))
        assert live.spec is not None
        assert live.spec.region is None
    finally:
        await live.close()


async def test_a_monitor_with_no_regions_dir_remembers_nothing(tmp_path: Path) -> None:
    """The default, and the old behaviour exactly: no store, no fallback, no
    file - which is what every suite that is not about persistence gets."""
    live = monitor(None)
    try:
        assert live.regions_dir is None
        await live.configure(spec(region=CHAT, service="svc"))
        assert live.saved_region("svc") is None
        assert not regions_path(tmp_path).exists()
    finally:
        await live.close()


# == the fake ==================================================================


async def test_the_fake_records_the_region_it_was_configured_with() -> None:
    """Recorded, not acted on: a suite that hands over ``region=None`` is saying
    "nothing is calibrated", and a double that quietly disagreed would hide the
    branch under test."""
    fake = FakeUIMonitor()
    await fake.configure(spec(region=CHAT, service="svc"))
    assert fake.saved_regions == {"svc": CHAT}
    assert fake.saved_region("svc") == CHAT
    assert fake.saved_region("other") is None
    await fake.configure(spec(region=None, service="svc"))
    assert fake.spec is not None and fake.spec.region is None
    assert fake.saved_region("svc") == CHAT


async def test_the_fake_fills_a_region_less_spec_from_its_store_too() -> None:
    """The double follows the real one's rule, so a shell test can stage 'the
    monitor over there remembers the box' in one assignment."""
    fake = FakeUIMonitor()
    fake.fills_from_store = True
    fake.saved_regions["svc"] = CHAT
    await fake.configure(spec(region=None, service="svc"))
    assert await fake.configured_region() == CHAT
    await fake.configure(spec(region=OTHER, service="svc"))
    assert await fake.configured_region() == OTHER
    assert fake.saved_regions["svc"] == OTHER
