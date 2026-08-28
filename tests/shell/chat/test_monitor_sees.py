"""F2's MONITOR SEES block: what the other machine holds, and what it told us.

``docs/design/ui-monitor.md`` §11.4. The block exists because §11.3 emptied this
window: there is no profile store, no click point and no service table on the
brain's side any more, so every "it refused / it never fired / it pasted but did
not send" is a fact about a machine the user is not looking at. Two halves, and
each has its own source:

* the ROWS are ``Watched.captured`` (does that machine hold a picture of this?)
  crossed with the latest tick's ``sightings`` (is it on screen this second?),
* the SETTINGS line is the five fields of ``Watched`` the brain drives from.

Both arrive over the wire and neither is composed from anything local, which is
what these tests are really pinning: stage an answer on the fake monitor, and
the sentence in the sidebar changes.

The third rule is a performance one with a correctness edge: a tick lands about
once a second for the whole of a session, and almost every one of them says
exactly what the last one said. The block is pushed on CHANGE, so a screen that
has not moved sends nothing at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentclip.driver.monitor.protocol import TICK_KINDS
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.chat.view import (
    SEES_CAPTURED,
    SEES_MISSING,
    SEES_NO_MONITOR,
    SEES_ON,
    SUBAGENT_WINDOW,
)
from tests.shell.chat.conftest import Harness

#: The box the MONITOR drew on its own desktop, adopted by this window (§10.5).
CHAT_REGION = ScreenRegion(1050, 340, 812, 540)
#: Where a sighting says a thing was found. Only its presence matters here.
FOUND = ScreenRegion(1100, 800, 240, 40)


async def watching(harness: Harness, *, captured: tuple[TemplateKind, ...], **spec: object) -> str:
    """Point the fake monitor at a drawn window it holds ``captured`` for.

    The one road an appearance takes into the Chat UI since §11.3: the monitor
    is asked what it is watching, and the answer carries the kinds. Nothing is
    written on this side, because there is nowhere on this side to write it.
    """
    target = replace(harness.monitor.specs_for[AgentSlot.MASTER], region=CHAT_REGION, **spec)
    harness.monitor.specs_for = dict.fromkeys(AgentSlot, target)
    harness.monitor.captured[target.service] = captured
    await harness.view._retarget_monitor()
    harness.view._push_sidebar()
    return target.service


def sees(harness: Harness) -> dict[str, object]:
    return harness.flush().last("monitor_sees")


def row_texts(event: dict[str, object]) -> list[str]:
    return [row["text"] for row in event["rows"]]  # type: ignore[index,union-attr]


def state_of(event: dict[str, object], kind: TemplateKind) -> str:
    rows = [row for row in event["rows"] if row["kind"] == kind.name]  # type: ignore[index,union-attr]
    assert len(rows) == 1, f"{kind} is not on exactly one row"
    return rows[0]["state"]


# == the rows =================================================================


async def test_every_kind_the_monitor_could_have_gets_a_row(harness: Harness) -> None:
    """``TICK_KINDS ∪ Watched.captured``, in the order the ELEMENTS panel lists
    them. A kind is on the list whether or not the monitor holds it: "✗ not
    captured" is the answer that names the fix, and a row that vanished when
    there was nothing to say would leave the user counting."""
    await watching(harness, captured=(TemplateKind.COPY,))

    event = sees(harness)
    assert [row["kind"] for row in event["rows"]] == [kind.name for kind in TICK_KINDS]
    assert event["note"] == ""


async def test_a_captured_kind_that_is_on_screen_reads_on_screen(harness: Harness) -> None:
    """The two sources, both consulted: the monitor HOLDS a copy button
    (``captured``) and is looking at one right now (``sightings``)."""
    await watching(harness, captured=(TemplateKind.COPY, TemplateKind.NEW_CHAT))
    harness.monitor.feed(harness.monitor.make_tick(sightings={TemplateKind.COPY: FOUND}))

    event = sees(harness)
    assert state_of(event, TemplateKind.COPY) == "on"
    assert f"{TemplateKind.COPY.label} · {SEES_ON}" in row_texts(event)


async def test_a_captured_kind_that_is_not_on_screen_says_so_quietly(harness: Harness) -> None:
    """The resting state of most rows, most of the time - a new-chat button IS
    captured and simply is not being shown. Its own state rather than a failure:
    nothing is wrong, and a user reading this column is looking for what IS."""
    await watching(harness, captured=(TemplateKind.COPY, TemplateKind.NEW_CHAT))
    harness.monitor.feed(harness.monitor.make_tick(sightings={TemplateKind.NEW_CHAT: None}))

    event = sees(harness)
    assert state_of(event, TemplateKind.NEW_CHAT) == "captured"
    assert f"{TemplateKind.NEW_CHAT.label} · {SEES_CAPTURED}" in row_texts(event)


async def test_a_kind_nobody_captured_is_the_one_the_user_can_act_on(harness: Harness) -> None:
    """§11.0's three reports, in one line of the sidebar. The refusals all say
    "capture one in the Monitor UI" and the user's next question is *which
    ones am I missing* - this is the answer, and before §11.4 the only place it
    existed was the other machine's window."""
    await watching(harness, captured=(TemplateKind.COPY,))

    event = sees(harness)
    assert state_of(event, TemplateKind.NEW_CHAT) == "missing"
    assert f"{TemplateKind.NEW_CHAT.label} · {SEES_MISSING}" in row_texts(event)


async def test_a_tick_cannot_promote_a_kind_the_monitor_never_captured(
    harness: Harness,
) -> None:
    """``captured`` is the authority on capture and the tick is the authority on
    the screen; a sighting for a kind that is in neither is not evidence of a
    picture. (The real monitor never searches what it has not captured, so this
    is a disagreement that cannot happen - and the row states which of the two
    answers wins when it does.)"""
    await watching(harness, captured=())
    harness.monitor.feed(harness.monitor.make_tick(sightings={TemplateKind.BUSY: None}))

    assert state_of(sees(harness), TemplateKind.BUSY) == "missing"


async def test_the_rows_describe_the_window_the_user_selected(harness: Harness) -> None:
    """Like every other per-window block in the sidebar. The two windows can be
    pointed at two services with two different sets of pictures, and the column
    describes the tab that is open - not the one the automation is driving."""
    await watching(harness, captured=(TemplateKind.COPY,))
    sub = replace(harness.view._watched[AgentSlot.MASTER], captured=(TemplateKind.NEW_CHAT,))
    harness.view._adopt_watched(AgentSlot.SUBAGENT, sub)

    harness.view._select_window(SUBAGENT_WINDOW)

    event = sees(harness)
    assert state_of(event, TemplateKind.NEW_CHAT) == "captured"
    assert state_of(event, TemplateKind.COPY) == "missing"


# == the settings the brain drives from =======================================


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("auto_submit", True, "auto-submit on"),
        ("auto_submit", False, "auto-submit off"),
        ("delivery", "clipboard", "delivery clipboard"),
        ("max_paste_chars", 12_000, "paste ≤ 12,000 chars"),
        ("hover_scan", True, "hover scan on"),
        ("snap_back", False, "snap back off"),
        ("submit_delay_s", 2.5, "submit delay 2.5s"),
        ("submit_delay_s", 0.0, "submit delay 0.0s"),
    ],
)
async def test_the_settings_line_reports_what_the_monitor_sent(
    harness: Harness, field: str, value: object, expected: str
) -> None:
    """Six settings, and each one is the answer to a support question a user
    cannot otherwise ask: why was my prompt not sent, why did the paste arrive
    in pieces, why did the window scroll, why did the Enter go out before the
    composer had swallowed the paste. All six are the MONITOR's to configure
    (§10.5) and reach this window inside ``Watched``, so this is the only place
    the user can read them at all."""
    await watching(harness, captured=(), **{field: value})

    assert expected in sees(harness)["settings"]  # type: ignore[operator]


# == the change filter ========================================================


async def test_a_tick_that_says_nothing_new_repaints_nothing(harness: Harness) -> None:
    """A tick a second, for hours. Nothing about the block changes between two
    identical observations, so nothing is sent: the filter is what makes it
    affordable to feed this block from the tick stream at all."""
    await watching(harness, captured=(TemplateKind.COPY,))
    sighting = {TemplateKind.COPY: FOUND}
    harness.monitor.feed(harness.monitor.make_tick(sightings=sighting))
    harness.flush().clear()

    for _ in range(5):
        harness.monitor.feed(harness.monitor.make_tick(sightings=sighting))

    assert harness.flush().of_type("monitor_sees") == []


async def test_the_copy_button_appearing_does_repaint(harness: Harness) -> None:
    """The other half of the same rule, and the reason it is a comparison rather
    than a timer: the moment the block exists for is the one where a row
    changes, and it must land on the next tick, not the next repaint of the
    sidebar."""
    await watching(harness, captured=(TemplateKind.COPY,))
    harness.monitor.feed(harness.monitor.make_tick(sightings={TemplateKind.COPY: None}))
    harness.flush().clear()

    harness.monitor.feed(harness.monitor.make_tick(sightings={TemplateKind.COPY: FOUND}))

    assert state_of(sees(harness), TemplateKind.COPY) == "on"


async def test_the_settings_changing_repaints_even_with_the_screen_still(
    harness: Harness,
) -> None:
    """The settings ride on ``Watched``, not on the tick, so they change when
    the user edits the service in the MONITOR's window - with nothing on this
    machine's screen moving at all."""
    await watching(harness, captured=(), auto_submit=False)
    harness.flush().clear()

    await watching(harness, captured=(), auto_submit=True)

    assert "auto-submit on" in sees(harness)["settings"]  # type: ignore[operator]


# == no monitor at all ========================================================


async def test_with_nothing_attached_the_block_says_so(harness: Harness) -> None:
    """A column of "not captured" would blame the far machine for a link that
    was never made. One sentence instead, and no rows: the honest answer to
    "what does the monitor see" when there is no monitor."""
    await harness.view._detach_monitor()

    event = sees(harness)
    assert event["rows"] == []
    assert event["settings"] == ""
    assert event["note"] == SEES_NO_MONITOR
