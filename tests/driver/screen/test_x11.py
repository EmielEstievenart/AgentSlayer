"""The Linux X11 backend, exercised against a fake ``Xlib`` on any platform.

Two halves. The pure one - ZPixmap -> RegionImage conversion, the wheel-button
mapping, the keysym names - is arithmetic and needs nothing. The rest runs the
real functions against ``fake_xlib``'s recorded server, so the assertions are
about the REQUEST (the rectangle asked for, the order of XTest events, the
client message sent to the root) rather than about a screen nobody here has.

Nothing is marked ``real_os``: no test in this file reaches an X server, and on
the Windows machine this suite usually runs on there is not even a python-xlib
to import.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from agentclip.driver.screen import capture, focus, x11
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.region import ScreenRegion
from tests.driver.screen.fake_xlib import (
    BUTTON_PRESS,
    BUTTON_RELEASE,
    KEY_PRESS,
    KEY_RELEASE,
    MOTION_NOTIFY,
    ZPIXMAP,
    FakeDisplay,
    install_fake_xlib,
    keycode_of,
    keycodes_for,
)

REGION = ScreenRegion(120, 240, 32, 24)


def bgrx(*pixels: tuple[int, int, int]) -> bytes:
    """Pack (b, g, r) triples the way a 32bpp LSBFirst server would, with a
    non-zero fourth byte so the conversion has something to prove it zeroes."""
    return b"".join(bytes((b, g, r, 0xAA)) for b, g, r in pixels)


# == pixel conversion =========================================================


def test_row_stride_pads_rows_up_to_the_scanline_unit() -> None:
    """Three 24-bit pixels are nine bytes of colour in a twelve-byte row."""
    assert x11.row_stride(3, 24, 32) == 12
    assert x11.row_stride(4, 24, 32) == 12  # exactly three units, no padding
    assert x11.row_stride(3, 32, 32) == 12  # 32bpp is never padded


def test_a_32bpp_zpixmap_keeps_its_bgr_order_and_loses_the_fourth_byte() -> None:
    """The common case: X's little-endian BGRX and GDI's BGRX are the same
    bytes, so only the undefined X byte is rewritten (capture.py leaves it
    undefined too, and every consumer ignores it)."""
    data = bgrx((1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12))
    pixels = x11.zpixmap_to_bgrx(data, 2, 2, bits_per_pixel=32)
    assert pixels == bytes(
        [1, 2, 3, 0, 4, 5, 6, 0, 7, 8, 9, 0, 10, 11, 12, 0],
    )
    assert len(pixels) == 2 * 2 * 4


def test_row_padding_is_cut_rather_than_carried_into_the_frame() -> None:
    """A 24-bit-deep row of three pixels is nine bytes plus three of padding.
    Carried through, every row after the first would be shifted sideways - the
    failure that looks like a diagonally smeared screenshot."""
    row_one = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9]) + b"\xee\xee\xee"
    row_two = bytes([11, 12, 13, 14, 15, 16, 17, 18, 19]) + b"\xee\xee\xee"
    pixels = x11.zpixmap_to_bgrx(row_one + row_two, 3, 2, bits_per_pixel=24)
    assert pixels == bytes(
        [1, 2, 3, 0, 4, 5, 6, 0, 7, 8, 9, 0, 11, 12, 13, 0, 14, 15, 16, 0, 17, 18, 19, 0]
    )


def test_an_msb_first_server_gets_its_channels_reordered() -> None:
    """MSBFirst hands back X,R,G,B per pixel; RegionImage wants B,G,R,X."""
    data = bytes([0xAA, 3, 2, 1, 0xAA, 6, 5, 4])
    pixels = x11.zpixmap_to_bgrx(data, 2, 1, bits_per_pixel=32, msb_first=True)
    assert pixels == bytes([1, 2, 3, 0, 4, 5, 6, 0])


def test_an_msb_first_24bit_server_gets_its_channels_reordered() -> None:
    data = bytes([3, 2, 1, 6, 5, 4]) + b"\xee\xee"  # two pixels + row padding
    pixels = x11.zpixmap_to_bgrx(data, 2, 1, bits_per_pixel=24, msb_first=True)
    assert pixels == bytes([1, 2, 3, 0, 4, 5, 6, 0])


@pytest.mark.parametrize("bits", [8, 16, 15, 30])
def test_an_unsupported_pixel_format_is_a_capture_error(bits: int) -> None:
    with pytest.raises(CaptureError, match="unsupported X11 pixel format"):
        x11.zpixmap_to_bgrx(b"\x00" * 64, 2, 2, bits_per_pixel=bits)


def test_a_short_buffer_is_a_capture_error_not_a_ragged_frame() -> None:
    with pytest.raises(CaptureError, match="truncated"):
        x11.zpixmap_to_bgrx(bgrx((1, 2, 3)), 2, 2)


def test_an_empty_region_is_refused_before_any_arithmetic() -> None:
    with pytest.raises(CaptureError, match="no area"):
        x11.zpixmap_to_bgrx(b"", 0, 4)


# == capture ==================================================================


def test_capture_asks_for_the_region_where_it_sits_and_returns_a_region_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The region's own virtual-screen coordinates go straight to XGetImage -
    X's root window starts at (0, 0) and spans every monitor, so there is no
    origin to subtract the way Windows' negative virtual screen needs."""
    display = install_fake_xlib(
        monkeypatch, FakeDisplay(image_data=bgrx((1, 2, 3), (4, 5, 6)), image_depth=24)
    )
    image = x11.capture_region(ScreenRegion(37, 11, 2, 1))
    assert display.image_requests == [(37, 11, 2, 1, ZPIXMAP, 0xFFFFFFFF)]
    assert isinstance(image, RegionImage)
    assert (image.width, image.height) == (2, 1)
    assert image.pixels == bytes([1, 2, 3, 0, 4, 5, 6, 0])


def test_capture_reads_the_servers_own_pixel_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bits-per-pixel and scanline padding come from the format table for the
    depth the server actually returned, not from an assumption about 32bpp."""
    row = bytes([1, 2, 3, 4, 5, 6]) + b"\xee\xee"
    display = install_fake_xlib(
        monkeypatch,
        FakeDisplay(image_data=row, image_depth=24, bits_per_pixel=24, scanline_pad=32),
    )
    image = x11.capture_region(ScreenRegion(0, 0, 2, 1))
    assert image.pixels == bytes([1, 2, 3, 0, 4, 5, 6, 0])
    assert display.syncs == 0  # a read needs no flush


def test_capture_with_no_area_never_opens_a_display(monkeypatch: pytest.MonkeyPatch) -> None:
    display = install_fake_xlib(monkeypatch)
    with pytest.raises(CaptureError, match="no area"):
        x11.capture_region(ScreenRegion(0, 0, 0, 10))
    assert display.image_requests == []


def test_no_display_is_a_capture_error_naming_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The monitor started outside a graphical session: one clear error per
    call, never a traceback out of a poll tick."""
    monkeypatch.setattr(x11, "_display_state", None)
    monkeypatch.setattr(x11, "_display_failure", None)
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(CaptureError, match="DISPLAY is not set"):
        x11.capture_region(REGION)


def test_missing_python_xlib_is_a_capture_error_not_an_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A None in sys.modules is what an uninstalled package looks like to the
    import machinery, and the lazy import has to survive it."""
    monkeypatch.setattr(x11, "_display_state", None)
    monkeypatch.setattr(x11, "_display_failure", None)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setitem(sys.modules, "Xlib", None)
    with pytest.raises(CaptureError, match="python-xlib is not installed"):
        x11.capture_region(REGION)


def test_the_failure_is_logged_once_however_many_ticks_ask(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(x11, "_display_state", None)
    monkeypatch.setattr(x11, "_display_failure", None)
    monkeypatch.delenv("DISPLAY", raising=False)
    with caplog.at_level("WARNING", logger="agentclip.driver.screen.x11"):
        for _ in range(5):
            assert x11.move_cursor(1, 1) is False
    assert len(caplog.records) == 1


def test_bounds_come_from_the_root_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_xlib(monkeypatch, FakeDisplay(geometry=(0, 0, 2560, 1440)))
    assert x11.virtual_screen_bounds() == (0, 0, 2560, 1440)


def test_bounds_are_none_without_a_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x11, "_display_state", None)
    monkeypatch.setattr(x11, "_display_failure", None)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert x11.virtual_screen_bounds() is None


# == input ====================================================================


def test_the_scroll_keysyms_match_the_names_the_rest_of_the_app_uses() -> None:
    """Same arrangement focus.py describes for MATCHERS: the names are spelled
    twice (once per backend) and a test asserts they agree."""
    assert set(x11.SCROLL_KEYSYMS) == set(focus.SCROLL_KEYS)


def test_a_keysym_name_resolves_through_the_servers_own_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = install_fake_xlib(monkeypatch, FakeDisplay(keymap=keycodes_for("Return")))
    assert x11._keycode(display, "Return") == keycode_of(display, "Return")
    assert x11._keycode(display, "Page_Down") is None  # unmapped: the server says 0


def test_paste_nests_control_around_v(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four events, in the order that makes them a Ctrl+V rather than two
    unrelated taps: XTest has no burst, so the nesting is ours to get right."""
    display = install_fake_xlib(monkeypatch, FakeDisplay(keymap=keycodes_for("Control_L", "v")))
    ctrl, v = keycode_of(display, "Control_L"), keycode_of(display, "v")
    assert x11.send_paste() is True
    assert [(kind, detail) for kind, detail, _x, _y in display.events] == [
        (KEY_PRESS, ctrl),
        (KEY_PRESS, v),
        (KEY_RELEASE, v),
        (KEY_RELEASE, ctrl),
    ]


def test_an_unmapped_key_sends_nothing_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half a Ctrl+V - a modifier pressed and never released - would leave the
    user's keyboard stuck, so a missing keycode refuses before the first event."""
    display = install_fake_xlib(monkeypatch, FakeDisplay(keymap=keycodes_for("Control_L")))
    assert x11.send_paste() is False
    assert display.events == []


def test_enter_is_one_press_and_one_release(monkeypatch: pytest.MonkeyPatch) -> None:
    display = install_fake_xlib(monkeypatch, FakeDisplay(keymap=keycodes_for("Return")))
    code = keycode_of(display, "Return")
    assert x11.send_enter() is True
    assert [(kind, detail) for kind, detail, _x, _y in display.events] == [
        (KEY_PRESS, code),
        (KEY_RELEASE, code),
    ]


def test_scroll_keys_tap_the_named_key_that_many_times(monkeypatch: pytest.MonkeyPatch) -> None:
    display = install_fake_xlib(monkeypatch, FakeDisplay(keymap=keycodes_for("Page_Down", "End")))
    assert x11.send_scroll_key("page_down", 3) is True
    assert len(display.events) == 6
    assert x11.send_scroll_key("nope") is False
    assert x11.send_scroll_key("end", 0) is False
    assert len(display.events) == 6


def test_the_wheel_buttons_follow_the_callers_sign_not_x11s_numbering() -> None:
    """Positive is UP for both backends, which is Windows' convention; X spells
    that button 4, and down - the flick that snaps a transcript to the bottom -
    button 5."""
    assert x11.scroll_button(3) == x11.BUTTON_WHEEL_UP == 4
    assert x11.scroll_button(-3) == x11.BUTTON_WHEEL_DOWN == 5
    assert x11.scroll_button(0) is None


def test_a_scroll_flick_is_one_move_then_a_detent_per_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = install_fake_xlib(monkeypatch)
    assert x11.scroll_region(REGION, -2) is True
    x, y = REGION.center
    assert display.events == [
        (MOTION_NOTIFY, 0, x, y),
        (BUTTON_PRESS, x11.BUTTON_WHEEL_DOWN, 0, 0),
        (BUTTON_RELEASE, x11.BUTTON_WHEEL_DOWN, 0, 0),
        (BUTTON_PRESS, x11.BUTTON_WHEEL_DOWN, 0, 0),
        (BUTTON_RELEASE, x11.BUTTON_WHEEL_DOWN, 0, 0),
    ]


def test_scrolling_zero_clicks_moves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    display = install_fake_xlib(monkeypatch)
    assert x11.scroll_region(REGION, 0) is False
    assert display.events == []


def test_a_click_moves_to_the_centre_first(monkeypatch: pytest.MonkeyPatch) -> None:
    display = install_fake_xlib(monkeypatch)
    assert x11.click_region(REGION) is True
    x, y = REGION.center
    assert display.events == [
        (MOTION_NOTIFY, 0, x, y),
        (BUTTON_PRESS, x11.BUTTON_LEFT, 0, 0),
        (BUTTON_RELEASE, x11.BUTTON_LEFT, 0, 0),
    ]


def test_the_cursor_move_is_a_motion_event_not_a_warp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warped pointer does not reliably make a browser fire its ``:hover``,
    and making it do exactly that is why the hover scan moves the cursor."""
    display = install_fake_xlib(monkeypatch)
    assert x11.move_cursor(400, 300) is True
    assert display.events == [(MOTION_NOTIFY, 0, 400, 300)]


def test_the_settle_pause_happens_between_the_move_and_the_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hover-rendered targets only exist once the page has processed the move,
    so the pause has to sit after it - not before, and not after the click."""
    display = install_fake_xlib(monkeypatch)
    seen: list[tuple[float, int]] = []
    monkeypatch.setattr(x11.time, "sleep", lambda s: seen.append((s, len(display.events))))
    assert x11.click_region(REGION, settle_s=0.25) is True
    assert seen == [(0.25, 1)]


# == window focus =============================================================


def test_the_foreground_window_is_read_from_net_active_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = install_fake_xlib(monkeypatch, FakeDisplay(active_window=0x400123))
    assert x11.foreground_window() == 0x400123
    assert display.property_reads == [(display.intern_atom("_NET_ACTIVE_WINDOW"), 0)]


def test_no_active_window_reads_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_xlib(monkeypatch, FakeDisplay(active_window=None))
    assert x11.foreground_window() is None


def test_focusing_asks_the_window_manager_and_raises_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = install_fake_xlib(monkeypatch, FakeDisplay(active_window=0x99))
    assert x11.focus_window(0x99) is True
    (message, mask), = display.messages
    assert message.client_type == display.intern_atom("_NET_ACTIVE_WINDOW")
    assert message.data[0] == 32
    assert message.data[1][0] == 2  # source indication: "pager", not "application"
    assert mask  # SubstructureRedirect|Notify - the root's own event mask
    assert display.configures == [(0x99, {"stack_mode": 0})]


def test_focusing_is_false_when_the_window_did_not_come_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request is granted and the answer is still no: focus is verified by
    reading _NET_ACTIVE_WINDOW back, never by the send succeeding."""
    install_fake_xlib(monkeypatch, FakeDisplay(active_window=0x1))
    assert x11.focus_window(0x99) is False


def test_focusing_a_dead_window_is_false_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = install_fake_xlib(
        monkeypatch, FakeDisplay(active_window=0x99, dead_windows=frozenset({0x99}))
    )
    assert x11.focus_window(0x99) is False
    assert display.messages == []


def test_a_zero_handle_never_reaches_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    display = install_fake_xlib(monkeypatch)
    assert x11.focus_window(0) is False
    assert x11.focus_window_verified(0) is False
    assert display.messages == []


def test_verified_focus_retries_until_the_window_really_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race focus.py documents: the browser we just clicked in is still
    activating itself, so one grant can be taken straight back."""
    display = install_fake_xlib(monkeypatch, FakeDisplay(active_window=0x1))
    monkeypatch.setattr(x11.time, "sleep", lambda _s: None)

    attempts: list[int] = []

    def flaky(disp: Any, handle: int) -> bool:
        attempts.append(handle)
        if len(attempts) >= 3:
            display.active_window = handle
        return True

    monkeypatch.setattr(x11, "_activate", flaky)
    assert x11.focus_window_verified(0x99) is True
    assert len(attempts) == 3


def test_verified_focus_gives_up_rather_than_hanging_the_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_xlib(monkeypatch, FakeDisplay(active_window=0x1))
    monkeypatch.setattr(x11.time, "sleep", lambda _s: None)
    assert x11.focus_window_verified(0x99, attempts=2) is False


# == the dispatch from the Windows-first modules ==============================


class _Spy:
    """Records what the platform switch handed over."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def make(self, name: str, answer: Any) -> Any:
        def recorder(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return answer

        return recorder


@pytest.fixture
def on_linux(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    """``sys.platform`` says linux, and every x11 entry point is a spy."""
    monkeypatch.setattr(sys, "platform", "linux")
    spy = _Spy()
    for name, answer in (
        ("capture_region", RegionImage(1, 1, b"\x00\x00\x00\x00")),
        ("click_region", True),
        ("move_cursor", True),
        ("scroll_region", True),
        ("send_paste", True),
        ("send_enter", True),
        ("send_scroll_key", True),
        ("virtual_screen_bounds", (0, 0, 800, 600)),
        ("foreground_window", 0x77),
        ("focus_window", True),
        ("focus_window_verified", True),
    ):
        monkeypatch.setattr(x11, name, spy.make(name, answer))
    return spy


def test_capture_dispatches_to_x11_on_linux(on_linux: _Spy) -> None:
    image = capture.capture_region(REGION)
    assert (image.width, image.height) == (1, 1)
    assert on_linux.calls == [("capture_region", (REGION,), {})]


def test_the_click_carries_its_settle_across_the_dispatch(on_linux: _Spy) -> None:
    assert focus.click_region(REGION, settle_s=0.4) is True
    assert on_linux.calls == [("click_region", (REGION,), {"settle_s": 0.4})]


def test_every_focus_entry_point_dispatches_on_linux(on_linux: _Spy) -> None:
    assert focus.move_cursor(5, 6) is True
    assert focus.scroll_region(REGION, -2) is True
    assert focus.send_paste() is True
    assert focus.send_enter() is True
    assert focus.send_scroll_key("end", 2) is True
    assert focus.virtual_screen_bounds() == (0, 0, 800, 600)
    assert focus.foreground_window() == 0x77
    assert focus.focus_window(0x77) is True
    assert focus.focus_window_verified(0x77) is True
    assert [name for name, _args, _kwargs in on_linux.calls] == [
        "move_cursor",
        "scroll_region",
        "send_paste",
        "send_enter",
        "send_scroll_key",
        "virtual_screen_bounds",
        "foreground_window",
        "focus_window",
        "focus_window_verified",
    ]


def test_the_guards_that_refuse_before_the_platform_switch_still_do(on_linux: _Spy) -> None:
    """Cheap refusals stay on the near side of the hand-off: a zero-detent
    scroll, an unknown scroll key and a zero handle are answered without a
    round trip to any backend."""
    assert focus.scroll_region(REGION, 0) is False
    assert focus.send_scroll_key("sideways") is False
    assert focus.focus_window(0) is False
    assert focus.focus_window_verified(0) is False
    assert on_linux.calls == []


@pytest.mark.parametrize("platform", ["darwin", "freebsd13"])
def test_other_platforms_keep_refusing_outright(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """The X11 backend is Linux's answer, not everyone's: macOS keeps the
    documented False/None so its caller still tells the user once."""
    monkeypatch.setattr(sys, "platform", platform)
    assert focus._x11() is None
    assert focus.click_region(REGION) is False
    assert focus.move_cursor(1, 2) is False
    assert focus.send_paste() is False
    assert focus.foreground_window() is None
    assert focus.virtual_screen_bounds() is None
    with pytest.raises(CaptureError, match="needs Windows or a Linux X11 desktop"):
        capture.capture_region(REGION)


def test_windows_never_reaches_the_x11_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the dispatch: on win32 the switch is not even
    consulted, so the GDI/SendInput path is exactly what it was."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert focus._x11() is None
