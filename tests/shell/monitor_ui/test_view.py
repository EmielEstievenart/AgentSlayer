"""The calibration window's view - phase 4A (docs/design/ui-monitor.md §6.4).

Three surfaces moved in here, and each keeps the contract it had in the chat
shell: the ELEMENTS column (``ui-briefs/elements-panel.md`` §3 the states, §4.2
what a frame that says nothing about a kind means, §4.4 the crop policy, §6.8
the encode memo), the chat-region picker and ``/identify`` (§6.10's one-overlay
rule), and the service editor's ``svc_*`` intents with their save path
(``ui-briefs/service-editor.md`` §5, §7).

What is NEW here, and is therefore what these tests are mostly about:

* the suspend/resume bracket is an ``await monitor.suspend()`` rather than a
  fire-and-forget schedule, and it is per CAPTURE rather than per editor visit,
  because this window keeps the editor open for its whole life;
* the monitor is retargeted by this view itself - there is no
  ``AutomationController`` here to own a live slot;
* closing the editor closes the WINDOW, so the apply path and the exit are one
  door.

**Nothing here draws an overlay, opens a window or writes outside tmp_path.**
``pick_region``, ``capture_region`` and ``draw_identify_overlay`` are
monkeypatched at the scope the real ones are looked up in.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import load_config
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import RUNTIME_KINDS, Sighting
from agentclip.driver.screen.identify import IdentifiedElement
from agentclip.driver.screen.picker import ScreenPickError
from agentclip.driver.screen.png import decode_png
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.template import RegionMatch, Template
from agentclip.shell.monitor_ui.view import (
    ELEMENT_LABEL,
    ELEMENT_MISSING,
    ELEMENT_ORDER,
    ELEMENT_RESTING,
    MASTER,
    STATE_FOUND,
    STATE_MISSING,
    STATE_RESTING,
    SUBAGENT,
    CalibrationView,
    ElementCrop,
    element_crop,
    element_png,
)
from tests.shell.monitor_ui.conftest import CalibHarness

VIEW = "agentclip.shell.monitor_ui.view"
EDITOR = "agentclip.shell.webview.service_editor"


# == fixtures the crops are made of ===========================================


def bgrx(pixels: list[tuple[int, int, int]], width: int, height: int) -> RegionImage:
    """A frame from (blue, green, red) triples - the byte order a capture has.

    The undefined fourth byte is deliberately NOT zero: it is undefined in a
    real capture, and a reader that treated it as alpha would encode a crop as
    transparent (elements-panel.md §6.3).
    """
    buffer = bytearray()
    for blue, green, red in pixels:
        buffer += bytes((blue, green, red, 0x7B))
    assert len(buffer) == width * height * 4
    return RegionImage(width, height, bytes(buffer))


def sighting(kind: TemplateKind, x: int, y: int, width: int, height: int, diff: float) -> Sighting:
    """A verified match, hand-built: the size comes from the template and the
    position from the match, which is what it takes to cut the pixels back out."""
    template = Template(RegionImage(width, height, b"\x00" * (width * height * 4)), ())
    return Sighting(kind=kind, template=template, match=RegionMatch(x=x, y=y, diff=diff), at=0.0)


def rows_of(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["kind"]: row for row in event["rows"]}


def edit(view: CalibrationView, **fields: str) -> None:
    """One keystroke, as the page sends it: the WHOLE candidate, always."""
    editor = view.editor
    assert editor is not None
    current = dict(editor.state()["form"])
    current.update(fields)
    view.svc_form(current)


class PickerSpy:
    """``pick_region``'s stand-in: what it was asked, and what it answers."""

    def __init__(self, answer: ScreenRegion | None = None, error: str = "") -> None:
        self.answer = answer
        self.error = error
        self.prompts: list[str] = []

    def __call__(self, *, prompt: str = "", **_: object) -> ScreenRegion | None:
        self.prompts.append(prompt)
        if self.error:
            raise ScreenPickError(self.error)
        return self.answer


# == the crop, and the byte order it must survive =============================


def test_a_crop_is_the_matched_rectangle_and_nothing_else() -> None:
    """The cut is the icon, never the chat window (elements-panel.md §4.4)."""
    scene = bgrx([(0, 0, 0)] * 4 + [(10, 20, 30)] + [(0, 0, 0)] * 4, width=3, height=3)
    crop = element_crop(scene, sighting(TemplateKind.COPY, 1, 1, 1, 1, 0.012))
    assert crop is not None
    assert (crop.image.width, crop.image.height) == (1, 1)
    assert crop.image.pixels[:3] == bytes((10, 20, 30))
    assert crop.diff == pytest.approx(0.012)


def test_no_sighting_and_a_degenerate_match_are_both_no_picture() -> None:
    scene = bgrx([(1, 2, 3)], width=1, height=1)
    assert element_crop(scene, None) is None
    # A match reported off the edge of the frame: the cut and the source do not
    # overlap at all, which is the same row as "nothing matched".
    assert element_crop(scene, sighting(TemplateKind.COPY, 9, 9, 2, 2, 0.0)) is None


def test_the_png_is_valid_base64_and_keeps_blue_blue() -> None:
    """BGRX, not BGRA - the invariant the whole surface hangs on (§6.3)."""
    image = bgrx([(200, 100, 50)], width=1, height=1)  # B=200, G=100, R=50
    uri = element_png(image)
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    back = decode_png(raw)
    assert (back.width, back.height) == (1, 1)
    assert back.pixels[:3] == bytes((200, 100, 50))


def test_an_unencodable_crop_is_no_picture_rather_than_an_exception() -> None:
    assert element_png(RegionImage(4, 4, b"\x00" * 8)) == ""
    assert element_png(RegionImage(0, 0, b"")) == ""


# == the three-state row contract =============================================


def test_the_column_lists_every_kind_in_the_detectors_own_order(calib: CalibHarness) -> None:
    """Row order is the detector's report order, so a row can never be mistaken
    for a picture of another row's search (elements-panel.md §2.1)."""
    calib.view.paint_elements({})
    event = calib.flush().last("elements")
    assert [row["kind"] for row in event["rows"]] == [kind.name for kind in RUNTIME_KINDS]
    assert ELEMENT_ORDER == RUNTIME_KINDS
    assert [row["label"] for row in event["rows"]] == [
        ELEMENT_LABEL[kind] for kind in RUNTIME_KINDS
    ]


def test_the_three_states_are_absent_present_none_and_present_crop(calib: CalibHarness) -> None:
    """"Nothing was ever searched for this" and "we looked and it is not there"
    are opposite readings of the same blank space (§3.1), and a kind's presence
    in the frame's map is what decides which one a row is showing (§4.2)."""
    view = calib.view
    crop = ElementCrop(bgrx([(9, 9, 9)], 1, 1), 0.0123)
    view.paint_elements({TemplateKind.COPY: crop, TemplateKind.BUSY: None})
    rows = rows_of(calib.flush().last("elements"))

    assert rows["COPY"]["state"] == STATE_FOUND
    assert rows["COPY"]["text"] == "found · 1.2%"
    assert rows["COPY"]["png"].startswith("data:image/png;base64,")
    assert rows["BUSY"]["state"] == STATE_MISSING
    assert rows["BUSY"]["text"] == ELEMENT_MISSING
    assert "png" not in rows["BUSY"]
    # Never searched: the kind was absent from the frame's map entirely.
    assert rows["NEW_CHAT"]["state"] == STATE_RESTING
    assert rows["NEW_CHAT"]["text"] == ELEMENT_RESTING


def test_a_kind_absent_from_a_frame_keeps_the_row_it_had(calib: CalibHarness) -> None:
    """A frame that never looked must not blank a row: the only reason one says
    nothing about a kind is that the service has no capture of it (§4.2)."""
    view = calib.view
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    calib.flush().clear()

    view.paint_elements({TemplateKind.BUSY: None})
    rows = rows_of(calib.flush().last("elements"))
    assert rows["COPY"]["state"] == STATE_FOUND
    assert rows["COPY"]["png"]
    assert rows["BUSY"]["state"] == STATE_MISSING


def test_a_frame_from_the_monitor_reaches_the_column_as_pictures(calib: CalibHarness) -> None:
    """The whole crop path, driven from the machine end.

    Pixels never ride a ``Tick`` (ui-monitor.md §2.2), so the column is fed by
    the monitor's local-only frame hook, which this view subscribes to itself -
    there is no controller here to route it. Pushing one frame is therefore the
    real check that the wiring the poll thread depends on is connected.
    """
    view = calib.view
    view.start()
    calib.flush().clear()
    scene = bgrx([(0, 0, 0)] * 4 + [(10, 20, 30)] + [(0, 0, 0)] * 4, width=3, height=3)

    calib.monitor.push_frame(
        {
            TemplateKind.COPY: sighting(TemplateKind.COPY, 1, 1, 1, 1, 0.012),
            TemplateKind.BUSY: None,
        },
        scene=scene,
    )

    rows = rows_of(calib.flush().last("elements"))
    assert rows["COPY"]["state"] == STATE_FOUND
    assert rows["COPY"]["png"].startswith("data:image/png;base64,")
    assert rows["BUSY"]["state"] == STATE_MISSING
    assert rows["NEW_CHAT"]["state"] == STATE_RESTING


async def test_closing_the_view_stops_listening_and_ends_the_monitor(
    calib: CalibHarness,
) -> None:
    """The window going away has to take the poll thread with it, and a frame
    that arrives after must reach nobody."""
    view = calib.view
    view.start()
    await view.close()
    calib.flush().clear()

    calib.monitor.push_frame({TemplateKind.COPY: sighting(TemplateKind.COPY, 0, 0, 1, 1, 0.0)})
    assert calib.flush().of_type("elements") == []
    assert calib.monitor.closed is True
    # Idempotent: the pump's return and an explicit close both reach it.
    await view.close()


def test_the_heading_names_the_window_being_calibrated(calib: CalibHarness) -> None:
    view = calib.view
    view.paint_elements({})
    assert calib.flush().last("elements")["window"] == MASTER
    view.select_slot(SUBAGENT)
    assert calib.flush().last("elements")["window"] == SUBAGENT


def test_a_hidden_column_costs_no_encoding_but_keeps_its_state(calib: CalibHarness) -> None:
    """Hiding does not stop the polling: the crops keep arriving so that opening
    the column shows the CURRENT frame (§3.1). What it stops is the one part
    that is not free - a PNG per matched appearance, twice a second."""
    view = calib.view
    view.set_elements_visible(False)
    calib.flush().clear()
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    rows = rows_of(calib.flush().last("elements"))
    assert rows["COPY"]["state"] == STATE_FOUND  # the verdict crossed...
    assert "png" not in rows["COPY"]  # ...the picture did not


def test_opening_the_column_paints_the_frame_that_is_already_in(calib: CalibHarness) -> None:
    view = calib.view
    view.set_elements_visible(False)
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    calib.flush().clear()

    view.set_elements_visible(True)
    rows = rows_of(calib.flush().last("elements"))
    assert rows["COPY"]["png"].startswith("data:image/png;base64,")
    # Idempotent: a second "it is open" is not a reason to repaint.
    calib.flush().clear()
    view.set_elements_visible(True)
    assert not calib.flush().of_type("elements")


def test_the_same_pixels_are_encoded_once(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row showing the bytes it already showed is not re-encoded (§6.8)."""
    view = calib.view
    encodes: list[int] = []
    monkeypatch.setattr(
        f"{VIEW}.element_png",
        lambda image: (encodes.append(1), "data:image/png;base64,AAA")[1],
    )

    image = bgrx([(1, 2, 3)], 1, 1)
    for _ in range(3):
        view.paint_elements({TemplateKind.COPY: ElementCrop(image, 0.05)})
    assert len(encodes) == 1  # three frames, one encode

    # Pixels that MOVED are encoded again - the memo is over the bytes, not
    # over the kind.
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(9, 9, 9)], 1, 1), 0.05)})
    assert len(encodes) == 2


# == the monitor's target =====================================================


async def test_start_subscribes_once_and_configures_the_monitor(calib: CalibHarness) -> None:
    """The frame hook FIRST, so no tick can land in the gap between "the window
    exists" and "the column is listening"."""
    view = calib.view
    view.start()
    assert calib.scheduled  # the configure went on the loop
    view.start()  # a second ``loaded`` must not double-subscribe
    calib.flush().clear()
    calib.monitor.push_frame({TemplateKind.COPY: None})
    assert len(calib.flush().of_type("elements")) == 1


async def test_the_spec_names_the_selected_windows_service_and_rectangle(
    calib: CalibHarness,
) -> None:
    """The payload is §2.10's, read fresh: the service KEY (never the profile),
    the drawn rectangle, and ``stable_seconds`` RAW - converting it into a tick
    count belongs to whatever is doing the ticking."""
    view = calib.view
    drawn = ScreenRegion(left=1, top=2, width=30, height=40)
    view.set_region(AgentSlot.MASTER, drawn)
    await view._configure()

    spec = calib.monitor.spec
    assert spec is not None
    assert spec.region == drawn
    assert spec.service == view.config.general.service
    assert spec.stable_seconds == view.config.preset().stable_seconds


async def test_switching_windows_repoints_the_monitor_and_empties_the_column(
    calib: CalibHarness,
) -> None:
    """A crop cut from the other window under this one's name is a lie."""
    view = calib.view
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    calib.flush().clear()

    view.select_slot(SUBAGENT)
    assert view.slot is AgentSlot.SUBAGENT
    rows = rows_of(calib.flush().last("elements"))
    assert {row["state"] for row in rows.values()} == {STATE_RESTING}


def test_an_adopted_region_is_not_echoed_back_to_the_brain(calib: CalibHarness) -> None:
    """``set_region`` is the way IN (the chat GUI hands its rectangle over at
    hand-off); echoing it would send the brain its own answer."""
    drawn = ScreenRegion(0, 0, 10, 10)
    calib.view.set_region(AgentSlot.MASTER, drawn)
    assert calib.calibrations == []
    assert calib.flush().last("calib")["region"] == drawn.describe()


# == the region picker ========================================================


async def test_the_picker_applies_to_the_selected_window_and_tells_the_brain(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detectors are suspended for the whole visit - a fullscreen overlay
    over the browser they watch is the sustained large delta that arms a finish
    trigger on staleness alone - and the drawn rectangle leaves this window the
    only way it can: through the injected callback."""
    view = calib.view
    drawn = ScreenRegion(left=10, top=20, width=800, height=600)
    spy = PickerSpy(drawn)
    monkeypatch.setattr(f"{VIEW}.pick_region", spy)

    await view._pick_chat_region(AgentSlot.MASTER)

    assert view.region == drawn
    assert calib.calibrations == [(AgentSlot.MASTER, drawn)]
    assert (calib.monitor.suspends, calib.monitor.resumes) == (1, 1)
    assert spy.prompts and not spy.prompts[0].startswith("SUB-AGENT")
    recorder = calib.flush()
    assert recorder.last("calib")["region"].startswith(drawn.describe())
    assert any("chat region set" in event["message"] for event in recorder.of_type("toast"))
    assert view._picker_open is False


async def test_the_slot_is_frozen_when_the_overlay_opens(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay blocks for as long as the user takes to drag, and the picker
    on the page can move meanwhile. What was selected when it opened is what the
    user was answering - and the sub-agent's prompt says which window it is."""
    view = calib.view
    drawn = ScreenRegion(left=0, top=0, width=400, height=300)

    def moving(*, prompt: str = "", **_: object) -> ScreenRegion:
        view.select_slot(MASTER)  # the header moves while the box is being drawn
        return drawn

    monkeypatch.setattr(f"{VIEW}.pick_region", moving)
    view.select_slot(SUBAGENT)
    await view._pick_chat_region(AgentSlot.SUBAGENT)

    assert calib.calibrations == [(AgentSlot.SUBAGENT, drawn)]
    # ...and the MASTER slot, which the page moved to mid-drag, is untouched.
    assert view.region is None


async def test_the_sub_agents_prompt_says_which_window(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = PickerSpy(ScreenRegion(0, 0, 4, 4))
    monkeypatch.setattr(f"{VIEW}.pick_region", spy)
    await calib.view._pick_chat_region(AgentSlot.SUBAGENT)
    assert spy.prompts[0].startswith("SUB-AGENT window · ")


async def test_a_cancelled_pick_changes_nothing_and_says_so(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{VIEW}.pick_region", PickerSpy(None))
    await calib.view._pick_chat_region(AgentSlot.MASTER)
    assert calib.view.region is None
    assert calib.calibrations == []
    assert "cancelled" in calib.flush().last("toast")["message"]
    # The suspension is still put back: the ``finally`` runs on every exit.
    assert calib.monitor.resumes == 1


async def test_a_failed_picker_is_an_error_toast_not_a_crash(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{VIEW}.pick_region", PickerSpy(error="the picker died"))
    await calib.view._pick_chat_region(AgentSlot.MASTER)
    toast = calib.flush().last("toast")
    assert toast["severity"] == "error" and "picker died" in toast["message"]
    assert calib.view._picker_open is False
    assert calib.monitor.resumes == 1


def test_only_one_fullscreen_child_at_a_time(calib: CalibHarness) -> None:
    """The picker, ``/identify`` and the editor's Capture share one flag: a
    second request while one is open is refused, not queued, because cancelling
    a task cannot kill a blocking child process (§6.10)."""
    view = calib.view
    view.set_chat_region()
    assert len(calib.scheduled) == 1
    view.show_identify_overlay()
    assert len(calib.scheduled) == 1  # nothing new was started
    assert "already open" in calib.flush().last("toast")["message"]


# == /identify ================================================================


async def test_identify_refuses_when_no_window_is_drawn(calib: CalibHarness) -> None:
    view = calib.view
    view._picker_open = True  # as show_identify_overlay leaves it
    await view._identify_window()
    toast = calib.flush().last("toast")
    assert toast["severity"] == "warning" and "Set chat region" in toast["message"]
    assert view._picker_open is False


async def test_identify_captures_first_then_draws_and_summarises(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture happens BEFORE any overlay exists (a frame taken with it up
    would identify the overlay as part of the chat window), it goes through the
    MONITOR's own capture seam, the detectors are suspended around the drawing,
    and the summary lands after the overlay is down."""
    view = calib.view
    region = ScreenRegion(left=0, top=0, width=40, height=30)
    view.set_region(AgentSlot.MASTER, region)
    order: list[str] = []

    calib.monitor.frames = [bgrx([(0, 0, 0)] * 1200, 40, 30)]
    real_capture = calib.monitor.capture
    monkeypatch.setattr(
        calib.monitor,
        "capture",
        lambda given: (order.append("capture"), real_capture(given))[1],
    )
    found = [
        IdentifiedElement("chat region", region),
        IdentifiedElement("copy", ScreenRegion(left=5, top=5, width=10, height=10), 0.01),
    ]
    monkeypatch.setattr(
        f"{VIEW}.identify_elements", lambda *a, **k: (order.append("identify"), found)[1]
    )
    monkeypatch.setattr(
        f"{VIEW}.draw_identify_overlay",
        lambda elements: order.append(f"draw:{len(elements)}"),
    )
    real_suspend, real_resume = calib.monitor.suspend, calib.monitor.resume

    async def suspend() -> None:
        order.append("suspend")
        await real_suspend()

    async def resume() -> None:
        order.append("resume")
        await real_resume()

    monkeypatch.setattr(calib.monitor, "suspend", suspend)
    monkeypatch.setattr(calib.monitor, "resume", resume)

    view._picker_open = True
    await view._identify_window()

    assert order == ["capture", "identify", "suspend", "draw:2", "resume"]
    assert "identified 1 elements: copy×1" in calib.flush().last("toast")["message"]
    assert view._picker_open is False


async def test_identify_searches_with_the_pollers_own_settings(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay that searched with different settings from the poller would
    answer a question nobody asked (elements-panel.md §4.5)."""
    view = calib.view
    region = ScreenRegion(left=0, top=0, width=4, height=4)
    view.set_region(AgentSlot.MASTER, region)
    calib.monitor.frames = [bgrx([(0, 0, 0)] * 16, 4, 4)]
    seen: dict[str, Any] = {}

    def spy_identify(*args: Any, **kwargs: Any) -> list[IdentifiedElement]:
        seen.update(kwargs)
        return [IdentifiedElement("chat region", region)]

    monkeypatch.setattr(f"{VIEW}.identify_elements", spy_identify)
    monkeypatch.setattr(f"{VIEW}.draw_identify_overlay", lambda elements: None)

    view._picker_open = True
    await view._identify_window()
    assert seen["tolerance"] == view.config.preset().tolerance
    assert seen["matcher"] is not None
    # Nothing but the region matched: the toast says so rather than counting it.
    assert "identified nothing" in calib.flush().last("toast")["message"]


async def test_a_failed_overlay_resumes_the_detectors_anyway(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = calib.view
    view.set_region(AgentSlot.MASTER, ScreenRegion(0, 0, 4, 4))
    calib.monitor.frames = [bgrx([(0, 0, 0)] * 16, 4, 4)]
    monkeypatch.setattr(
        f"{VIEW}.identify_elements",
        lambda *a, **k: [IdentifiedElement("chat region", ScreenRegion(0, 0, 4, 4))],
    )

    def boom(_elements: object) -> None:
        raise ScreenPickError("the overlay could not start")

    monkeypatch.setattr(f"{VIEW}.draw_identify_overlay", boom)
    view._picker_open = True
    await view._identify_window()

    assert calib.monitor.resumes == 1
    assert "could not start" in calib.flush().last("toast")["message"]
    assert view._picker_open is False


async def test_an_unreadable_screen_is_an_error_toast(calib: CalibHarness) -> None:
    """``FakeUIMonitor.capture`` raises ``CaptureError`` with nothing scripted,
    which is exactly what a real one does when the region has gone."""
    view = calib.view
    view.set_region(AgentSlot.MASTER, ScreenRegion(0, 0, 4, 4))
    view._picker_open = True
    await view._identify_window()
    toast = calib.flush().last("toast")
    assert toast["severity"] == "error" and "could not capture" in toast["message"]


# == the service editor =======================================================


def test_the_editor_is_the_window_and_opens_with_it(calib: CalibHarness) -> None:
    """Not a modal here: this window IS the editor, so one is built at start and
    lives for the window's life."""
    view = calib.view
    view.start()
    event = calib.flush().last("editor")
    assert event["open"] is True
    assert event["selected"] == view.config.general.service
    # Idempotent - a second ``loaded`` must not throw the working copy away.
    editor = view.editor
    view.open_service_editor()
    assert view.editor is editor


def test_every_svc_intent_drives_the_model_and_repaints(calib: CalibHarness) -> None:
    """One call on the model, one ``editor`` event back - the model owns every
    refusal and toasts it itself."""
    view = calib.view
    view.start()
    calib.flush().clear()
    edit(view, label="Renamed")
    assert calib.flush().last("editor")["form"]["label"] == "Renamed"
    assert view.editor is not None
    assert view.editor.services[view.config.general.service].label == "Renamed"

    view.svc_tolerance(31)
    assert calib.flush().last("editor")["tolerance"] == 31
    view.svc_detection({"signals": ["stale"], "hover_scan": True, "require_fenced": False,
                        "stream": False, "auto_submit": False})
    assert calib.flush().last("editor")["hover_scan"] is True
    view.svc_after_delivery({"snap_back": False, "alert_sound": False})
    assert calib.flush().last("editor")["snap_back"] is False
    view.svc_edit_by_lines(True)
    assert calib.flush().last("editor")["edit_by_lines"] is True


def test_an_svc_intent_before_the_editor_exists_is_a_no_op(calib: CalibHarness) -> None:
    """The page can be a repaint behind the window closing; nothing may raise."""
    view = calib.view
    view.svc_select("chatgpt")
    view.svc_tolerance(4)
    view.svc_prev("copy")
    view.svc_click_point("copy", 10, 10)
    assert calib.flush().of_type("editor") == []


async def test_a_capture_suspends_only_for_the_capture(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bracket is per CAPTURE here, not per visit as it is in the chat GUI's
    modal: this window keeps the editor open for its whole life, and suspending
    for that long would freeze the ELEMENTS column - the surface the user is
    calibrating AGAINST (ui-monitor.md §6.4)."""
    view = calib.view
    view.start()
    assert calib.monitor.suspends == 0  # opening the editor suspends NOTHING

    monkeypatch.setattr(f"{EDITOR}.pick_region", lambda **_: None)  # cancelled
    view.svc_capture(TemplateKind.COPY.value)
    assert view._picker_open is True  # claimed synchronously, before the task
    await view._svc_capture(TemplateKind.COPY)

    assert (calib.monitor.suspends, calib.monitor.resumes) == (1, 1)
    assert view._picker_open is False


def test_a_capture_claims_the_overlay_before_the_task_runs(calib: CalibHarness) -> None:
    """Two presses marshal onto the loop as two callbacks; if the flag were
    taken inside the coroutine neither would have seen the other's."""
    view = calib.view
    view.start()
    view.svc_capture(TemplateKind.COPY.value)
    scheduled = len(calib.scheduled)
    view.svc_capture(TemplateKind.BUSY.value)
    assert len(calib.scheduled) == scheduled  # the second press started nothing


def test_a_setter_saves_and_retargets_without_waiting_for_the_close(
    calib: CalibHarness,
) -> None:
    """§11.10. This window is the monitor's own face and stays open for the
    process's life, so a setting that applied on close applied never as far as
    an attached Chat UI was concerned."""
    view = calib.view
    view.start()
    key = view.config.general.service
    calib.scheduled.clear()
    calib.flush().clear()

    view.svc_tolerance(31)

    # ...on disk, in the file this window loads at startup...
    saved = load_config(Path.cwd(), global_config_path=calib.global_config_path)
    assert saved.services[key].tolerance == 31
    # ...adopted here...
    assert view.config.services[key].tolerance == 31
    assert calib.configs and calib.configs[-1].services[key].tolerance == 31
    # ...and on the poller, which is what bumps the generation an attached brain
    # re-reads ``watched()`` on.
    assert calib.scheduled == ["CalibrationView._configure"]
    # A keystroke is not an event worth a toast.
    assert calib.flush().of_type("toast") == []


def test_a_setter_that_lands_nothing_legal_writes_nothing(calib: CalibHarness) -> None:
    """An invalid candidate is never committed to the working copy, so the
    comparison ``_apply_edits`` guards on finds nothing to write."""
    view = calib.view
    view.start()
    calib.scheduled.clear()

    edit(view, max="not a number")

    assert not calib.global_config_path.exists()
    assert calib.scheduled == []
    assert calib.configs == []


def test_a_setter_re_picked_at_its_current_value_is_not_a_write(
    calib: CalibHarness,
) -> None:
    view = calib.view
    view.start()
    view.svc_tolerance(31)
    calib.scheduled.clear()
    calib.configs.clear()

    view.svc_tolerance(31)

    assert calib.scheduled == []
    assert calib.configs == []


async def test_closing_after_a_setter_writes_nothing_new(calib: CalibHarness) -> None:
    """The presets are already on disk and already on the poller, so the
    ordinary close is a close and nothing else."""
    view = calib.view
    view.start()
    edit(view, label="Edited")
    calib.scheduled.clear()
    calib.configs.clear()
    stamp = calib.global_config_path.read_bytes()

    await view._svc_close()

    assert calib.exits == 1
    assert calib.global_config_path.read_bytes() == stamp
    assert calib.configs == []
    assert calib.scheduled == []


async def test_closing_saves_the_presets_hands_them_on_and_closes_the_window(
    calib: CalibHarness,
) -> None:
    """The one path that parts company with the chat GUI's modal: the editor
    closing IS the window closing.

    Since §11.10 the apply happened at the keystroke rather than here, so what
    this pins is the END STATE a close leaves behind - saved, handed on, gone -
    which is the promise, not which of the two moments wrote it."""
    view = calib.view
    view.start()
    edit(view, label="Edited")
    calib.flush().clear()

    await view._svc_close()

    assert calib.exits == 1
    assert view.editor is None
    assert calib.flush().last("editor")["open"] is False
    # Written to the global config.toml this window was pointed at...
    assert calib.global_config_path.exists()
    key = view.config.general.service
    saved = load_config(Path.cwd(), global_config_path=calib.global_config_path)
    assert saved.services[key].label == "Edited"
    # ...and handed to whoever built the engine factory, which is how a chat GUI
    # hosting this window learns about the edit (phase 4B).
    assert calib.configs and calib.configs[-1].services[key].label == "Edited"
    assert view.config.services[key].label == "Edited"


async def test_closing_with_nothing_changed_writes_nothing(calib: CalibHarness) -> None:
    view = calib.view
    view.start()
    await view._svc_close()
    assert calib.exits == 1
    assert calib.configs == []
    assert not calib.global_config_path.exists()


async def test_a_refused_close_keeps_the_window(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture is up: Escape belongs to the overlay, and closing out from
    under it would strand the flow that still has to write the PNG."""
    view = calib.view
    view.start()
    editor = view.editor
    assert editor is not None
    assert editor.start_capture(TemplateKind.COPY) is True
    calib.flush().clear()

    await view._svc_close()
    assert calib.exits == 0
    assert view.editor is editor
    assert calib.flush().last("editor")["open"] is True


async def test_an_unwritable_config_still_adopts_the_edit(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's edit is real for this process even when the file it should
    outlive it in could not be written."""
    view = calib.view
    view.start()
    monkeypatch.setattr(
        f"{VIEW}.save_services",
        lambda services, path: (_ for _ in ()).throw(OSError("read-only")),
    )
    calib.flush().clear()

    edit(view, label="Edited")
    assert view.config.services[view.config.general.service].label == "Edited"
    assert any(
        event["severity"] == "error" and "could not save" in event["message"]
        for event in calib.flush().of_type("toast")
    )


async def test_an_unwritable_config_is_one_complaint_not_one_per_keystroke(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11.10: applying on change means the failing write happens per character,
    and a toast per character would bury every other message the window has."""
    view = calib.view
    view.start()
    monkeypatch.setattr(
        f"{VIEW}.save_services",
        lambda services, path: (_ for _ in ()).throw(OSError("read-only")),
    )
    calib.flush().clear()

    edit(view, label="E")
    edit(view, label="Ed")
    edit(view, label="Edi")

    errors = [
        event for event in calib.flush().of_type("toast") if event["severity"] == "error"
    ]
    assert len(errors) == 1


def test_the_close_button_takes_the_editors_door(calib: CalibHarness) -> None:
    """The window's own close is routed through the editor's apply path, never
    a shortcut past it."""
    view = calib.view
    view.start()
    calib.scheduled.clear()
    view.request_close()
    assert calib.scheduled == ["CalibrationView._svc_close"]
    assert calib.exits == 0  # not until the model says it may


def test_a_close_with_no_editor_left_just_leaves(calib: CalibHarness) -> None:
    view = calib.view
    view.svc_close()
    assert calib.exits == 1


# == the header block =========================================================


def test_the_header_names_the_window_its_service_and_its_rectangle(
    calib: CalibHarness,
) -> None:
    view = calib.view
    view.start()
    event = calib.flush().last("calib")
    assert event["slot"] == MASTER
    assert event["slots"] == [MASTER, SUBAGENT]
    assert event["service"] == view.config.general.service
    assert event["region"] == "not set"


def test_every_slot_resolves_to_a_service_that_exists(calib: CalibHarness) -> None:
    """A window pointed at a preset that is not in the table would silently
    calibrate against ``Config.preset()``'s fallback, which is a different
    service from the one the header names."""
    view = calib.view
    for name in (SUBAGENT, MASTER):
        view.select_slot(name)
        assert view._service_key() in view.config.services
        assert calib.flush().last("calib")["service"] in view.config.services
    # ...and a slot that is already current is not a repaint at all.
    calib.flush().clear()
    view.select_slot(MASTER)
    assert calib.flush().of_type("calib") == []


# == what this machine already remembers ======================================
# ui-monitor.md §8 / ``driver/monitor/regions.py``: a rectangle is a fact about
# THIS desktop, so the monitor keeps it. The window opening over a monitor that
# remembers one and saying "not set" was a header disagreeing with the very
# store ``configure`` fills its spec from - and it left Identify refusing a
# window it could see perfectly well.


def test_a_remembered_region_fills_the_header_at_start(calib: CalibHarness) -> None:
    view = calib.view
    remembered = ScreenRegion(left=7, top=9, width=300, height=200)
    calib.monitor.saved_regions[view.config.general.service] = remembered

    view.start()

    assert view.region == remembered
    assert calib.flush().last("calib")["region"] == remembered.describe()


async def test_a_remembered_region_is_a_region_identify_may_search(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is about "nothing is drawn", not about "nobody dragged in
    THIS run" - so a box off the store is one Identify works from."""
    view = calib.view
    remembered = ScreenRegion(left=0, top=0, width=4, height=4)
    calib.monitor.saved_regions[view.config.general.service] = remembered
    view.start()
    calib.monitor.frames = [bgrx([(0, 0, 0)] * 16, 4, 4)]
    searched: list[ScreenRegion] = []
    monkeypatch.setattr(
        f"{VIEW}.identify_elements",
        lambda region, *a, **k: (searched.append(region), [IdentifiedElement("copy", region)])[1],
    )
    monkeypatch.setattr(f"{VIEW}.draw_identify_overlay", lambda elements: None)

    view._picker_open = True
    await view._identify_window()

    assert searched == [remembered]
    toast = calib.flush().last("toast")
    assert toast["severity"] == "information"
    assert "Set chat region" not in toast["message"]


def test_a_drawn_region_beats_the_remembered_one(calib: CalibHarness) -> None:
    """The seed only ever fills a hole: re-reading the store on a slot switch or
    a config edit must never undo a drag."""
    view = calib.view
    drawn = ScreenRegion(left=1, top=1, width=50, height=50)
    calib.monitor.saved_regions[view.config.general.service] = ScreenRegion(0, 0, 999, 999)
    view.set_region(AgentSlot.MASTER, drawn)

    view.start()
    view.select_slot(SUBAGENT)
    view.select_slot(MASTER)

    assert view.region == drawn
    assert calib.flush().last("calib")["region"] == drawn.describe()


def test_switching_windows_picks_up_what_that_window_forgot(calib: CalibHarness) -> None:
    """A slot arrives with nothing drawn; the store is asked under that window's
    own service key."""
    view = calib.view
    view.start()
    view.select_slot(SUBAGENT)
    assert view.region is None
    remembered = ScreenRegion(left=3, top=4, width=120, height=90)
    calib.monitor.saved_regions[view._service_key()] = remembered

    view.select_slot(MASTER)
    view.select_slot(SUBAGENT)

    assert view.region == remembered
    assert calib.flush().last("calib")["region"] == remembered.describe()


async def test_the_seeded_region_is_what_the_monitor_is_pointed_at(
    calib: CalibHarness,
) -> None:
    """The point of the seed: the spec carries the rectangle the header names."""
    view = calib.view
    remembered = ScreenRegion(left=11, top=12, width=13, height=14)
    calib.monitor.saved_regions[view.config.general.service] = remembered
    view.start()

    await view._configure()

    spec = calib.monitor.spec
    assert spec is not None and spec.region == remembered


def test_a_monitor_that_remembers_nothing_still_says_not_set(calib: CalibHarness) -> None:
    """Every monitor the Chat UI opens this window over: those regions are the
    session's, so the store answers None and the header is honest."""
    calib.view.start()
    assert calib.view.region is None
    assert calib.flush().last("calib")["region"] == "not set"


# == the window IS the monitor's configuration (§10.5) ========================
# Wave 3's inversion: a brain never sends a service, a preset or a spec. It names
# a WINDOW over the wire and the monitor answers - and on a machine with a
# Monitor UI up, the monitor's answer has to be THIS window's selection, because
# the config file does not know which service the operator just picked or which
# box they just drew. That is one state behind two surfaces, which is the
# disagreement the wave exists to end.


def test_the_view_installs_itself_as_the_monitors_spec_source(
    calib: CalibHarness,
) -> None:
    """At construction, not at ``start``: a ``watch`` can arrive over the wire
    before the page has finished loading, and it must still be answered with
    this window's selection rather than with the config file's."""
    assert calib.monitor.spec_for == calib.view.spec_for


async def test_a_watch_over_the_wire_runs_this_windows_spec(
    calib: CalibHarness,
) -> None:
    """What a remote brain's ``watch(MASTER)`` actually gets: the region drawn
    here, under the service key this window resolved."""
    view = calib.view
    drawn = ScreenRegion(left=5, top=6, width=70, height=80)
    view.start()
    view.set_region(AgentSlot.MASTER, drawn)

    watched = await calib.monitor.watch(AgentSlot.MASTER)

    assert watched.service == view._service_key()
    assert watched.region == drawn


async def test_a_watch_for_the_other_window_switches_this_one_and_repaints(
    calib: CalibHarness,
) -> None:
    """A delegation starting is exactly when the sub-agent's window is worth
    looking at - so the header, the service line and the ELEMENTS column follow
    the brain rather than describing a window nobody is watching."""
    view = calib.view
    view.start()
    assert view.slot is AgentSlot.MASTER
    calib.flush().clear()

    watched = await calib.monitor.watch(AgentSlot.SUBAGENT)

    assert view.slot is AgentSlot.SUBAGENT
    assert watched.service == view._service_key()
    events = calib.flush()
    assert events.last("calib")["slot"] == SUBAGENT
    assert events.last("calib")["service"] == view._service_key()
    # The column is blanked too: a crop cut from the master's window under the
    # sub-agent's name is a straightforward lie.
    assert events.last("elements")["window"] == SUBAGENT
    rows = rows_of(events.last("elements"))
    assert {row["state"] for row in rows.values()} == {STATE_RESTING}


async def test_a_watch_for_the_window_already_shown_repaints_nothing(
    calib: CalibHarness,
) -> None:
    """A brain re-reading its own slot on every redial must not make the window
    flicker through a slot switch it never left."""
    view = calib.view
    view.start()
    calib.flush().clear()

    await calib.monitor.watch(AgentSlot.MASTER)

    assert view.slot is AgentSlot.MASTER
    assert calib.flush().of_type("calib") == []


async def test_a_watch_configures_the_monitor_exactly_once(
    calib: CalibHarness,
) -> None:
    """The slot adoption may repaint, but it may not retarget: the retarget is
    the very call being answered, and a second one would be this window
    configuring the monitor behind the brain's back."""
    view = calib.view
    view.start()
    before = calib.monitor.generations
    scheduled = len(calib.scheduled)

    await calib.monitor.watch(AgentSlot.SUBAGENT)

    assert calib.monitor.generations == before + 1
    assert len(calib.scheduled) == scheduled, "the slot switch scheduled a second retarget"
    assert view.slot is AgentSlot.SUBAGENT


def test_the_spec_carries_the_whole_preset_the_brain_acts_on(
    calib: CalibHarness,
) -> None:
    """§10.5: the paste budget, the fences and the instructions all reach the
    brain through ``Watched``, so they have to be on the spec that builds it."""
    view = calib.view
    view.start()
    preset = view.config.services[view._service_key()]
    built = view.spec_for(AgentSlot.MASTER)

    assert built.label == preset.label
    assert built.max_paste_chars == preset.max_paste_chars
    assert built.total_context_chars == preset.total_context_chars
    assert built.wrap_blocks_in_fence == preset.wrap_blocks_in_fence
    assert built.attachment_note == preset.attachment_note
    assert built.require_fenced_reply == preset.require_fenced_reply
    assert built.extra_instructions == preset.extra_instructions
    assert built.delivery == preset.delivery
    assert built.auto_submit == preset.auto_submit


# == the ONE service selection (§11.6) ========================================
# The bug: the Services dropdown moved the EDITOR's selection and nothing else,
# while ``_service_key`` - what ``spec_for``/``watch`` answer with - kept
# reading ``[general] service`` off the config file. So a Monitor UI showing
# 'chatgpt-attach' served a Chat UI that was driving 'zai'. The service is
# entirely the monitor's domain and there is exactly one selection: the
# dropdown IS the service this window watches for the tab it is showing.


def test_the_dropdown_is_the_service_this_window_watches(calib: CalibHarness) -> None:
    """One control. Picking in it moves what the monitor is pointed at, not
    merely which preset the editor draws."""
    view = calib.view
    view.start()
    assert view._service_key() == view.config.general.service
    calib.scheduled.clear()

    view.svc_select("claude")

    assert view._service_key() == "claude"
    assert view._spec().service == "claude"
    assert calib.flush().last("editor")["selected"] == "claude"
    assert calib.flush().last("calib")["service"] == "claude"
    # ...and the running poller was repointed rather than left on the old one.
    assert "CalibrationView._configure" in calib.scheduled


async def test_a_watch_answers_the_dropdown_and_bumps_the_generation(
    calib: CalibHarness,
) -> None:
    """What the attached brain gets back. The generation is how it learns: the
    retarget bumps it, and a tick carrying a stamp it has not seen makes it
    re-read ``watched()`` - so a service picked here reaches the far Chat UI
    without anybody pressing anything over there."""
    view = calib.view
    view.start()
    view.svc_select("claude")
    before = calib.monitor.generations

    watched = await calib.monitor.watch(AgentSlot.MASTER)

    assert watched.service == "claude"
    assert calib.monitor.generations == before + 1


def test_picking_a_service_is_remembered_for_the_next_launch(
    calib: CalibHarness, project: Path
) -> None:
    """Persisted to the GLOBAL config, through the same writer the sidebar
    picker used before that door left the Chat UI."""
    view = calib.view
    view.start()

    view.svc_select("claude")

    assert calib.global_config_path.exists()
    reloaded = load_config(project, global_config_path=calib.global_config_path)
    assert reloaded.general.service == "claude"


def test_each_window_keeps_its_own_service_and_the_tab_repaints_it(
    calib: CalibHarness, project: Path
) -> None:
    """Two windows, two selections, one control: switching tab shows that
    window's service in the very dropdown that sets it."""
    view = calib.view
    view.start()
    view.svc_select("claude")

    view.select_slot(SUBAGENT)
    # Its own selection, seeded from the file and untouched by the master's
    # pick: two windows are two services, which is the whole point of the tab.
    assert view._service_key() == view.config.general.service
    assert calib.flush().last("editor")["selected"] == view.config.general.service
    view.svc_select("gemini")
    assert view._service_key() == "gemini"
    assert calib.flush().last("editor")["selected"] == "gemini"

    view.select_slot(MASTER)
    assert view._service_key() == "claude"
    events = calib.flush()
    assert events.last("editor")["selected"] == "claude"
    assert events.last("calib")["service"] == "claude"

    reloaded = load_config(project, global_config_path=calib.global_config_path)
    assert (reloaded.general.service, reloaded.general.subagent_service) == ("claude", "gemini")


def test_the_config_file_is_only_where_the_selection_starts(calib: CalibHarness) -> None:
    """After the first pick nothing reads ``[general]`` again for this run - the
    file is the initial value, and the two-readers arrangement it replaced is
    what let the window and the monitor answer differently."""
    view = calib.view
    view.start()
    started_on = view.config.general.service

    view.svc_select("claude")

    assert view.config.general.service == started_on  # untouched in memory
    assert view._service_key() == "claude"
    assert view.spec_for(AgentSlot.MASTER).service == "claude"


def test_an_unwritable_config_still_switches_the_watched_service(
    calib: CalibHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pick is real for this run even when the file it should outlive it in
    could not be written - the same bargain the preset save makes."""
    view = calib.view
    view.start()
    monkeypatch.setattr(
        f"{VIEW}.save_active_services",
        lambda service, subagent_service, path: (_ for _ in ()).throw(OSError("read-only")),
    )
    calib.flush().clear()

    view.svc_select("claude")

    assert view._service_key() == "claude"
    assert any(
        event["severity"] == "error" and "could not save" in event["message"]
        for event in calib.flush().of_type("toast")
    )


def test_the_same_service_again_is_not_a_retarget(calib: CalibHarness) -> None:
    """The page repaints the dropdown from every ``editor`` event, so a redraw
    must not look like a pick: a retarget per paint would bump the generation
    once a keystroke and have every attached brain re-reading ``watched()``."""
    view = calib.view
    view.start()
    calib.scheduled.clear()

    view.svc_select(view._service_key())

    assert calib.scheduled == []
    assert not calib.global_config_path.exists()
