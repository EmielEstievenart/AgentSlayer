"""The slider widget: what its two gestures do, and what it refuses to do.

Textual ships no slider, so this one is ours and the cases here are the
contract the service editor leans on: the keyboard walks it, a click on the
track jumps to that position, the ends clamp, and ``Changed`` is posted for
real movement and for nothing else. That last one is not fussiness - the editor
writes its whole preset back on every Changed, and a held arrow key at the
maximum would otherwise rewrite it dozens of times a second.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from agentclip.tui.widgets.slider import HANDLE_CHAR, Slider


class SliderApp(App[None]):
    """One slider, wide enough that its track has real resolution."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self._kwargs = kwargs
        self.changes: list[int] = []

    def compose(self) -> ComposeResult:
        yield Slider(id="s", **self._kwargs)  # type: ignore[arg-type]

    def on_slider_changed(self, event: Slider.Changed) -> None:
        self.changes.append(event.value)


def _slider(app: SliderApp) -> Slider:
    return app.query_one("#s", Slider)


async def test_the_arrow_keys_walk_it_by_one_and_shift_by_the_big_step() -> None:
    app = SliderApp(value=24, minimum=0, maximum=64, big_step=8)
    async with app.run_test(size=(40, 5)) as pilot:
        _slider(app).focus()
        await pilot.press("right")
        assert _slider(app).value == 25
        await pilot.press("left", "left")
        assert _slider(app).value == 23
        await pilot.press("shift+right")
        assert _slider(app).value == 31
        await pilot.press("shift+left", "shift+left")
        assert _slider(app).value == 15
        assert app.changes == [25, 24, 23, 31, 23, 15]


async def test_home_and_end_go_to_the_ends() -> None:
    app = SliderApp(value=24, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)) as pilot:
        _slider(app).focus()
        await pilot.press("end")
        assert _slider(app).value == 64
        await pilot.press("home")
        assert _slider(app).value == 0


async def test_it_clamps_at_both_ends_and_says_nothing_when_it_cannot_move() -> None:
    """A held arrow key at the end is a run of no-op sets. The widget must not
    announce them: the editor writes a preset back on every Changed."""
    app = SliderApp(value=63, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)) as pilot:
        _slider(app).focus()
        await pilot.press("right", "right", "right", "right")
        assert _slider(app).value == 64
        assert app.changes == [64]  # one real move, three that could not happen


async def test_a_value_out_of_range_is_clamped_at_construction() -> None:
    app = SliderApp(value=500, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)):
        assert _slider(app).value == 64
        assert app.changes == []  # construction is not a gesture


async def test_a_click_on_the_track_jumps_the_handle_there() -> None:
    app = SliderApp(value=0, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)) as pilot:
        slider = _slider(app)
        track = slider._track_width
        assert track > 4
        await pilot.click(slider, offset=(track - 1, 0))
        assert slider.value == 64
        await pilot.click(slider, offset=(0, 0))
        assert slider.value == 0
        assert app.changes == [64, 0]


async def test_a_click_on_the_number_beside_the_track_is_not_a_gesture() -> None:
    """The number is a readout. Reaching for it to read it must not set the
    slider to its maximum, which is what treating the whole widget as track
    would do."""
    app = SliderApp(value=10, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)) as pilot:
        slider = _slider(app)
        await pilot.click(slider, offset=(slider._track_width + 1, 0))
        assert slider.value == 10
        assert app.changes == []


async def test_setting_it_without_notifying_fills_a_form_in_silently() -> None:
    """What a screen loading a service needs: the control shows the value
    without the load reading back as the user having moved it."""
    app = SliderApp(value=10, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)) as pilot:
        slider = _slider(app)
        slider.set_value(40, notify=False)
        await pilot.pause()
        assert slider.value == 40
        assert app.changes == []
        slider.set_value(41)
        await pilot.pause()
        assert app.changes == [41]


async def test_the_handle_and_the_number_are_both_drawn() -> None:
    """The number earns its cells: a slider whose value cannot be read off
    cannot be reported in a bug report or compared against the default."""
    app = SliderApp(value=64, minimum=0, maximum=64)
    async with app.run_test(size=(40, 5)) as pilot:
        slider = _slider(app)
        line = slider.render_line(0).text
        assert HANDLE_CHAR in line
        assert line.strip().endswith("64")
        # At the maximum the handle is the LAST cell of the track, whatever the
        # rounding in between does - "is it at the end?" is the question a
        # slider is worst at answering by eye.
        assert line.index(HANDLE_CHAR) == slider._track_width - 1
        slider.set_value(0, notify=False)
        await pilot.pause()
        assert slider.render_line(0).text.index(HANDLE_CHAR) == 0
