"""The ELEMENTS column, the region picker and ``/identify`` - parity increment 4.

The contract is ``docs/design/ui-briefs/elements-panel.md``: §2 the anatomy, §3
the states, §4.4 what a GUI must keep of the crop policy, §6 the invariants, §7
the terminal machinery that must NOT be carried over. What is tested here is the
half that lives on the Python side - the three-state row contract, the crop
being the matched rectangle in the byte order a capture really has, the
visibility gate on the encoder - plus the two fullscreen surfaces, at the seam
where they become a child process.

**Nothing here draws an overlay or opens a window.** ``pick_region`` and
``draw_identify_overlay`` are monkeypatched at ``gui/view.py``'s scope, which is
exactly where the real ones are looked up, so the flow around them is exercised
in full and no child process is ever spawned (project rule: no real OS side
effects while the user is present).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.detector import RUNTIME_KINDS, Sighting
from agentclip.driver.screen.identify import IdentifiedElement
from agentclip.driver.screen.picker import ScreenPickError
from agentclip.driver.screen.png import decode_png
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.template import RegionMatch, Template
from agentclip.shell.gui.bridge import JsApi
from agentclip.shell.gui.view import (
    ELEMENT_LABEL,
    ELEMENT_MISSING,
    ELEMENT_ORDER,
    ELEMENT_RESTING,
    STATE_FOUND,
    STATE_MISSING,
    STATE_RESTING,
    ElementCrop,
    GuiView,
    element_crop,
    element_png,
)
from tests.shell.gui.conftest import Harness, settle

ASSETS = Path(__file__).resolve().parents[3] / "src" / "agentclip" / "shell" / "gui" / "assets"

# == fixtures the crops are made of ===========================================


def bgrx(pixels: list[tuple[int, int, int]], width: int, height: int) -> RegionImage:
    """A frame from (blue, green, red) triples - the byte order a capture has.

    The undefined fourth byte is deliberately NOT zero: it is undefined in a
    real capture, and a reader that treated it as alpha would encode a crop as
    transparent (elements-panel.md §6.3). Filling it with garbage is how that
    stays checkable.
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


# == the crop, and the byte order it must survive =============================


def test_a_crop_is_the_matched_rectangle_and_nothing_else() -> None:
    """The cut is the icon, never the chat window (elements-panel.md §4.4).

    A 3x3 scene whose middle pixel is the only distinctive one; the sighting
    claims a 1x1 match at (1, 1), so exactly that pixel must come back.
    """
    scene = bgrx(
        [(0, 0, 0)] * 4 + [(10, 20, 30)] + [(0, 0, 0)] * 4,
        width=3,
        height=3,
    )
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
    """BGRX, not BGRA - the invariant the whole surface hangs on (§6.3).

    The pixel's blue and red channels differ deliberately: an encoder that read
    the buffer as RGB would hand back the mirror image of this assertion, and an
    encoder that read the fourth byte as alpha would hand back something
    invisible. Both are decidable from the bytes, which is why this is asserted
    on the PNG rather than on the crop.
    """
    image = bgrx([(200, 100, 50)], width=1, height=1)  # B=200, G=100, R=50
    uri = element_png(image)
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    # decode_png hands BGRX back, so the round trip is the identity on the three
    # real channels - and the alpha channel it wrote was opaque, or the decode
    # would not have found three channels' worth of colour to return.
    back = decode_png(raw)
    assert (back.width, back.height) == (1, 1)
    assert back.pixels[:3] == bytes((200, 100, 50))


def test_an_unencodable_crop_is_no_picture_rather_than_an_exception() -> None:
    """A truncated buffer is what a poll timer over a moving browser produces;
    the row keeps its verdict and simply has no picture."""
    assert element_png(RegionImage(4, 4, b"\x00" * 8)) == ""
    assert element_png(RegionImage(0, 0, b"")) == ""


# == the three-state row contract =============================================


def test_the_column_lists_every_kind_in_the_detectors_own_order(harness: Harness) -> None:
    """Row order is the detector's report order, so a row can never be mistaken
    for a picture of another row's search (elements-panel.md §2.1)."""
    harness.view.paint_elements({})
    event = harness.flush().last("elements")
    assert [row["kind"] for row in event["rows"]] == [kind.name for kind in RUNTIME_KINDS]
    assert ELEMENT_ORDER == RUNTIME_KINDS
    assert [row["label"] for row in event["rows"]] == [
        ELEMENT_LABEL[kind] for kind in RUNTIME_KINDS
    ]


def test_the_three_states_are_absent_present_none_and_present_crop(harness: Harness) -> None:
    """The distinction the panel exists for: "nothing was ever searched for
    this" and "we looked and it is not there" are opposite readings of the same
    blank space (§3.1), and a kind's presence in the tick's map is what decides
    which one a row is showing (§4.2)."""
    view = harness.view
    view.set_elements_visible(True)
    harness.flush().clear()

    crop = ElementCrop(bgrx([(9, 9, 9)], 1, 1), 0.0123)
    view.paint_elements({TemplateKind.COPY: crop, TemplateKind.BUSY: None})
    rows = rows_of(harness.flush().last("elements"))

    assert rows["COPY"]["state"] == STATE_FOUND
    assert rows["COPY"]["text"] == "found · 1.2%"
    assert rows["COPY"]["png"].startswith("data:image/png;base64,")
    assert rows["BUSY"]["state"] == STATE_MISSING
    assert rows["BUSY"]["text"] == ELEMENT_MISSING
    assert "png" not in rows["BUSY"]
    # Never searched: the kind was absent from the tick's map entirely.
    assert rows["NEW_CHAT"]["state"] == STATE_RESTING
    assert rows["NEW_CHAT"]["text"] == ELEMENT_RESTING


def test_a_kind_absent_from_a_tick_keeps_the_row_it_had(harness: Harness) -> None:
    """A tick that never looked must not blank a row: the detector searches
    every calibrated kind on every frame, so the only reason a tick says nothing
    about one is that the service has no capture of it (§4.2)."""
    view = harness.view
    view.set_elements_visible(True)
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    harness.flush().clear()

    view.paint_elements({TemplateKind.BUSY: None})  # a tick with nothing to say about COPY
    rows = rows_of(harness.flush().last("elements"))
    assert rows["COPY"]["state"] == STATE_FOUND
    assert rows["COPY"]["png"]
    assert rows["BUSY"]["state"] == STATE_MISSING


def test_a_rebuild_sends_every_row_home(harness: Harness) -> None:
    """A crop cut from the old window must never be shown under the new one's
    name, so a detector rebuild clears the column (§3.1)."""
    view = harness.view
    view.set_elements_visible(True)
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    harness.flush().clear()

    view._start_detector_worker()  # the rebuild every calibration change makes
    rows = rows_of(harness.flush().last("elements"))
    assert {row["state"] for row in rows.values()} == {STATE_RESTING}


def test_the_heading_names_the_live_window_not_the_selected_tab(harness: Harness) -> None:
    """Both this column and DETECTION describe the window being DRIVEN, which
    parts company with the tab the user is reading for the whole of a delegation
    (§6.5)."""
    view = harness.view
    view.select_window("m1-s1")  # the user reads the sub-agent's transcript...
    view.paint_elements({})
    assert harness.flush().last("elements")["window"] == "MASTER"  # ...master is live

    view.automation.select_live_slot(AgentSlot.SUBAGENT)
    view.paint_elements({})
    assert harness.flush().last("elements")["window"] == "SUB-AGENT"


# == the visibility gate ======================================================


def test_a_hidden_column_costs_no_encoding_but_keeps_its_state(harness: Harness) -> None:
    """Hiding does not stop the polling: the crops keep arriving so that opening
    the column shows the CURRENT tick rather than a stale one (§3.1). What it
    does stop is the one part that is not free - the PNG per matched
    appearance, twice a second, for a column nobody is looking at."""
    view = harness.view
    assert view._elements_open is False
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    rows = rows_of(harness.flush().last("elements"))
    assert rows["COPY"]["state"] == STATE_FOUND  # the verdict crossed...
    assert "png" not in rows["COPY"]  # ...the picture did not


def test_opening_the_column_paints_the_tick_that_is_already_in(harness: Harness) -> None:
    view = harness.view
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(1, 2, 3)], 1, 1), 0.05)})
    harness.flush().clear()

    view.set_elements_visible(True)
    rows = rows_of(harness.flush().last("elements"))
    assert rows["COPY"]["png"].startswith("data:image/png;base64,")
    # Idempotent: a second "it is open" is not a reason to repaint.
    harness.flush().clear()
    view.set_elements_visible(True)
    assert not harness.flush().of_type("elements")


def test_the_same_pixels_are_encoded_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poller re-cuts the same still icon out of frame after frame; a row
    showing the bytes it already showed is not re-encoded (§6.8)."""
    view = harness.view
    view.set_elements_visible(True)
    encodes: list[int] = []
    monkeypatch.setattr(
        "agentclip.shell.gui.view.element_png",
        lambda image: (encodes.append(1), "data:image/png;base64,AAA")[1],
    )

    image = bgrx([(1, 2, 3)], 1, 1)
    for _ in range(3):
        view.paint_elements({TemplateKind.COPY: ElementCrop(image, 0.05)})
    assert len(encodes) == 1  # three ticks, one encode

    # Pixels that MOVED are encoded again - the memoization is over the bytes,
    # not over the kind.
    view.paint_elements({TemplateKind.COPY: ElementCrop(bgrx([(9, 9, 9)], 1, 1), 0.05)})
    assert len(encodes) == 2
    assert all(
        row["png"] == "data:image/png;base64,AAA"
        for row in harness.flush().last("elements")["rows"]
        if "png" in row
    )


def test_f7_tells_python_which_way_it_went() -> None:
    """The flip never leaves the page (it is show/hide of one element) but the
    encoder has to be told, which is the one thing F3 and F8 do not do."""
    seen: list[bool] = []

    class Calls:
        def set_elements_visible(self, visible: bool) -> None:
            seen.append(visible)

    api = JsApi(Calls())  # type: ignore[arg-type]
    api.elements(True)
    api.elements(False)
    assert seen == [True, False]


# == the region picker ========================================================


class FakeRun:
    """What a started poller leaves behind, minus the thread and the capture."""

    def cancel(self) -> None:
        return None


def stub_rebuilds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record detector rebuilds instead of performing them.

    Every test that draws a region over the LIVE window needs this: the real
    rebuild starts a poller that captures the screen twice a second, and the
    stale detector needs no captured appearance to be worth running - so an
    empty profile is not enough to keep the machine out of it. The stub leaves a
    run behind exactly as the real one does, so ``resume_detectors``' "something
    already restarted it" branch is the one under test rather than an artifact.
    """
    rebuilds: list[str] = []

    def fake(view: GuiView) -> None:
        rebuilds.append("build")
        view._detector_worker = FakeRun()  # type: ignore[assignment]

    monkeypatch.setattr(GuiView, "_start_detector_worker", fake)
    return rebuilds


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


async def test_the_picker_applies_to_the_selected_window_and_rebuilds(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button draws the SELECTED window's box (increment 3's selection
    semantics), the detectors are suspended for the whole visit and the poller
    is rebuilt around what was drawn."""
    view = harness.view
    drawn = ScreenRegion(left=10, top=20, width=800, height=600)
    spy = PickerSpy(drawn)
    monkeypatch.setattr("agentclip.shell.gui.view.pick_region", spy)
    suspends: list[str] = []
    monkeypatch.setattr(
        GuiView, "suspend_detectors", lambda self: suspends.append("suspend"), raising=True
    )
    monkeypatch.setattr(
        GuiView, "resume_detectors", lambda self: suspends.append("resume"), raising=True
    )

    view.select_window("m1-s1")
    await view._pick_chat_region(view.automation.calibrating_slot)

    assert view.automation.calibration(AgentSlot.SUBAGENT).chat_region == drawn
    assert view.automation.calibration(AgentSlot.MASTER).chat_region is None
    assert suspends == ["suspend", "resume"]
    # The sub-agent's prompt says which window is being drawn on.
    assert spy.prompts and spy.prompts[0].startswith("SUB-AGENT window · ")
    recorder = harness.flush()
    assert recorder.last("sidebar")["region"].startswith(drawn.describe())
    assert any("chat region set" in event["message"] for event in recorder.of_type("toast"))
    assert view._picker_open is False


async def test_the_slot_is_frozen_when_the_overlay_opens(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay blocks for as long as the user takes to drag, and the
    selection moves on its own meanwhile (a delegated run's focus selects the
    sub-agent tab). What was selected when the picker opened is what the user
    was answering.

    ``_start_detector_worker`` is stubbed here for the reason every test in this
    file keeps the machine out of it: the drawn window IS the live one, so the
    real one would start a poller that captures the screen twice a second.
    """
    view = harness.view
    drawn = ScreenRegion(left=0, top=0, width=400, height=300)
    rebuilds = stub_rebuilds(monkeypatch)

    def moving(*, prompt: str = "", **_: object) -> ScreenRegion:
        view.select_window("m1-s1")  # the tab moves while the box is being drawn
        return drawn

    monkeypatch.setattr("agentclip.shell.gui.view.pick_region", moving)
    await view._pick_chat_region(AgentSlot.MASTER)

    assert view.automation.calibration(AgentSlot.MASTER).chat_region == drawn
    assert view.automation.calibration(AgentSlot.SUBAGENT).chat_region is None
    # The drawn window is the live one, so the poller is rebuilt around it -
    # once, by the adoption; ``resume_detectors`` finds a run already going.
    assert rebuilds == ["build"]


async def test_drawing_a_window_the_poller_is_not_watching_leaves_it_alone(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drawing the sub-agent's window mid-session is the normal way to reach
    delegation, and rebuilding around it would re-aim a poller at a window the
    automation is not driving."""
    view = harness.view
    rebuilds = stub_rebuilds(monkeypatch)
    monkeypatch.setattr(
        "agentclip.shell.gui.view.pick_region", PickerSpy(ScreenRegion(0, 0, 400, 300))
    )
    await view._pick_chat_region(AgentSlot.SUBAGENT)
    # The suspension's ``resume`` is the only rebuild, and it is the one that
    # puts back exactly the run that was already there.
    assert rebuilds == ["build"]
    assert view.automation.calibration(AgentSlot.MASTER).chat_region is None


async def test_a_cancelled_pick_changes_nothing_and_says_so(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = harness.view
    monkeypatch.setattr("agentclip.shell.gui.view.pick_region", PickerSpy(None))
    await view._pick_chat_region(AgentSlot.MASTER)
    assert view.automation.calibration(AgentSlot.MASTER).chat_region is None
    assert "cancelled" in harness.flush().last("toast")["message"]


async def test_a_failed_picker_is_an_error_toast_not_a_crash(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = harness.view
    monkeypatch.setattr("agentclip.shell.gui.view.pick_region", PickerSpy(error="the picker died"))
    await view._pick_chat_region(AgentSlot.MASTER)
    toast = harness.flush().last("toast")
    assert toast["severity"] == "error" and "picker died" in toast["message"]
    assert view._picker_open is False


def test_only_one_fullscreen_child_at_a_time(harness: Harness) -> None:
    """``/identify``, the region picker and (later) the service editor's capture
    overlays share one flag: a second request while one is open is refused, not
    queued, because cancelling a task cannot kill a blocking child process
    (§6.10)."""
    view = harness.view
    view.set_chat_region()
    assert len(harness.scheduled) == 1
    view.show_identify_overlay()
    assert len(harness.scheduled) == 1  # nothing new was started
    assert "already open" in harness.flush().last("toast")["message"]


# == /identify ================================================================


async def test_identify_refuses_when_no_window_is_drawn(harness: Harness) -> None:
    """The gating condition is the TUI's: a region is what there is to identify
    INSIDE, and without one there is nothing to put an overlay over (§3.2)."""
    view = harness.view
    view._picker_open = True  # as show_identify_overlay leaves it
    await view._identify_live_window()
    toast = harness.flush().last("toast")
    assert toast["severity"] == "warning" and "Set chat region" in toast["message"]
    assert view._picker_open is False


async def test_identify_captures_first_then_draws_and_summarises(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture happens BEFORE any overlay exists (a frame taken with it up
    would identify the overlay as part of the chat window), the detectors are
    suspended around the drawing, and the summary lands after the overlay is
    down so it is readable rather than painted behind it."""
    view = harness.view
    region = ScreenRegion(left=0, top=0, width=40, height=30)
    view.automation.set_calibration(AgentSlot.MASTER, region)
    order: list[str] = []

    monkeypatch.setattr(
        "agentclip.shell.gui.view.capture_region",
        lambda _region: (order.append("capture"), bgrx([(0, 0, 0)] * 1200, 40, 30))[1],
    )
    found = [
        IdentifiedElement("chat region", region),
        IdentifiedElement("copy", ScreenRegion(left=5, top=5, width=10, height=10), 0.01),
    ]
    monkeypatch.setattr(
        "agentclip.shell.gui.view.identify_elements",
        lambda *args, **kwargs: (order.append("identify"), found)[1],
    )
    monkeypatch.setattr(
        "agentclip.shell.gui.view.draw_identify_overlay",
        lambda elements: order.append(f"draw:{len(elements)}"),
    )
    monkeypatch.setattr(GuiView, "suspend_detectors", lambda self: order.append("suspend"))
    monkeypatch.setattr(GuiView, "resume_detectors", lambda self: order.append("resume"))

    view._picker_open = True
    await view._identify_live_window()

    assert order == ["capture", "identify", "suspend", "draw:2", "resume"]
    assert "identified 1 elements: copy×1" in harness.flush().last("toast")["message"]
    assert view._picker_open is False


async def test_identify_searches_with_the_pollers_own_settings(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay that searched with different settings from the poller would
    answer a question nobody asked (§4.5)."""
    view = harness.view
    region = ScreenRegion(left=0, top=0, width=4, height=4)
    view.automation.set_calibration(AgentSlot.MASTER, region)
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        "agentclip.shell.gui.view.capture_region", lambda _r: bgrx([(0, 0, 0)] * 16, 4, 4)
    )

    def spy_identify(*args: Any, **kwargs: Any) -> list[IdentifiedElement]:
        seen.update(kwargs)
        return [IdentifiedElement("chat region", region)]

    monkeypatch.setattr("agentclip.shell.gui.view.identify_elements", spy_identify)
    monkeypatch.setattr("agentclip.shell.gui.view.draw_identify_overlay", lambda elements: None)
    monkeypatch.setattr(GuiView, "suspend_detectors", lambda self: None)
    monkeypatch.setattr(GuiView, "resume_detectors", lambda self: None)

    view._picker_open = True
    await view._identify_live_window()
    tolerance, matcher = view.automation.live_search()
    assert seen["tolerance"] == tolerance
    assert seen["matcher"] == matcher
    # Nothing but the region matched: the toast says so rather than counting it.
    assert "identified nothing" in harness.flush().last("toast")["message"]


async def test_a_failed_overlay_resumes_the_detectors_anyway(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = harness.view
    view.automation.set_calibration(AgentSlot.MASTER, ScreenRegion(0, 0, 4, 4))
    monkeypatch.setattr(
        "agentclip.shell.gui.view.capture_region", lambda _r: bgrx([(0, 0, 0)] * 16, 4, 4)
    )
    monkeypatch.setattr(
        "agentclip.shell.gui.view.identify_elements",
        lambda *a, **k: [IdentifiedElement("chat region", ScreenRegion(0, 0, 4, 4))],
    )
    resumed: list[str] = []
    monkeypatch.setattr(GuiView, "suspend_detectors", lambda self: None)
    monkeypatch.setattr(GuiView, "resume_detectors", lambda self: resumed.append("resume"))

    def boom(_elements: object) -> None:
        raise ScreenPickError("the overlay could not start")

    monkeypatch.setattr("agentclip.shell.gui.view.draw_identify_overlay", boom)
    view._picker_open = True
    await view._identify_live_window()

    assert resumed == ["resume"]
    assert "could not start" in harness.flush().last("toast")["message"]
    assert view._picker_open is False


async def test_slash_identify_reaches_the_view_through_the_controller(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command path is the TUI's and needed nothing here: ``/identify`` has
    no session gate, so it is dispatched even from the task prompt."""
    view = harness.view
    calls: list[str] = []
    monkeypatch.setattr(GuiView, "show_identify_overlay", lambda self: calls.append("identify"))
    pending = asyncio.ensure_future(view.prompt_new_session())
    await settle()
    view.submit_text("/identify")
    await settle()
    assert calls == ["identify"]
    pending.cancel()


# == the page's own half ======================================================


def test_the_key_and_the_button_are_on_the_view_the_runner_marshals_to() -> None:
    """The runner is a one-line marshal per method; a name on the bridge's
    protocol and not on the view would be a click that silently did nothing."""
    for name in ("set_chat_region", "set_elements_visible"):
        assert callable(getattr(GuiView, name, None)), name


def test_the_page_draws_the_column_and_binds_f7() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    # A sibling of the sidebar, not nested inside it (elements-panel.md §2.1).
    assert '<aside class="elements" id="elements" hidden>' in html
    assert 'id="el-rows"' in html
    assert 'id="elements-title"' in html
    # One <img> per row, sized by CSS, and the F7 flip that tells Python.
    assert 'createElement("img")' in js
    # F7 is a row of the one key table the dispatcher and the help sheet share
    # (parity increment 6), not a branch of a switch any more.
    assert 'on: ["F7"]' in js and "toggleElements" in js
    assert 'api("elements", !el.elements.hidden)' in js
    assert 'api("set_region")' in js
    assert ".el-crop img" in css


def test_a_data_uri_is_not_a_network_reach() -> None:
    """The asset guard next door forbids ``http://``/``https://`` anywhere in
    the page, and the crops arrive as ``data:`` URIs built in Python - so the
    guard needed no allowlist and the assets still contain no URL at all. Pinned
    because "just widen the check" would be the wrong fix if it ever fires.
    """
    for name in ("index.html", "app.js", "app.css"):
        text = (ASSETS / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text
    assert element_png(bgrx([(1, 2, 3)], 1, 1)).startswith("data:")
