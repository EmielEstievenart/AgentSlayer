"""The SERVICE EDITOR (F2) - parity increment 5.

The contract is ``docs/design/ui-briefs/service-editor.md``: §2 the anatomy, §3
the states (the validity matrix, live-apply vs never-apply-invalid, add-gating,
the capture guard, the discard confirm, the "+ add new" disabled set), §5 what
each action does, §6 the invariants and §7 the terminal machinery that must NOT
be carried over.

Everything here drives :class:`agentclip.shell.webview.service_editor.ServiceEditor`
DIRECTLY - it is a model with no window, no page and no toolkit behind it, which
is the whole reason it was extracted. ``pick_region``/``capture_region`` are
monkeypatched at that module's scope (exactly where the real ones are looked
up), so the capture flow is exercised in full and no child process is ever
spawned; the profile store and ``save_services`` are pointed at ``tmp_path``, so
no run touches the user's captures or their config.toml.

The PAGE-side half of this surface is not here any more. The editor is drawn by
the calibration window now (ui-monitor.md 6.4), so its event, its js_api and its
apply path are pinned in ``tests/shell/monitor_ui/`` - what stays here is
the model both windows would have shared.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    DELIVERY_PASTE,
    DELIVERY_STREAM,
    MATCHER_ANCHORS,
    MATCHER_OPENCV,
    SCROLL_END,
    Config,
    ServicePreset,
    default_services,
    load_config,
    save_services,
)
from agentclip.driver.screen.capture import CaptureError, RegionImage
from agentclip.driver.screen.picker import ScreenPickError
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.profile_store import ProfileStoreError, load_profile, save_template
from agentclip.driver.screen.region import ScreenRegion
from agentclip.shell.webview.service_editor import (
    NEW_SENTINEL,
    OPENCV_MISSING_FROZEN,
    OPENCV_MISSING_SOURCE,
    SIGNAL_UNCAPTURED,
    TEMPLATE_UNSET,
    TEMPLATES_NONE,
    ServiceEditor,
    template_status,
    templates_line,
)

MODULE = "agentclip.shell.webview.service_editor"


# == fixtures =================================================================


def searchable(width: int = 24, height: int = 12) -> RegionImage:
    """A capture ``ServiceProfile.put`` will accept.

    Wider than one anchor (``template.ANCHOR_LEN``) and not flat, so the anchor
    chooser has something to pick - a box too narrow to anchor is refused before
    it ever reaches disk, which is its own test below.
    """
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 9) % 256, (y * 17) % 256, (x * y) % 256, 0))
    return RegionImage(width, height, bytes(pixels))


class Toasts:
    """The model's notify sink: (message, severity) in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, message: str, severity: str) -> None:
        self.sent.append((message, severity))

    @property
    def messages(self) -> list[str]:
        return [message for message, _ in self.sent]

    def saying(self, needle: str) -> bool:
        return any(needle in message for message in self.messages)


class Answers:
    """A confirm dialog that answers from a script and records what it asked."""

    def __init__(self, *answers: bool) -> None:
        self.queued = list(answers)
        self.asked: list[tuple[str, str]] = []

    async def __call__(self, title: str, body: str) -> bool:
        self.asked.append((title, body))
        return self.queued.pop(0) if self.queued else False


@pytest.fixture
def profiles(tmp_path: Path) -> Path:
    root = tmp_path / "profiles"
    root.mkdir()
    return root


@pytest.fixture
def config(project: Path, tmp_path: Path) -> Config:
    return load_config(project, global_config_path=tmp_path / "no-such-global.toml")


@pytest.fixture
def toasts() -> Toasts:
    return Toasts()


@pytest.fixture
def editor(config: Config, profiles: Path, toasts: Toasts) -> ServiceEditor:
    return ServiceEditor(config, profiles, "chatgpt", notify=toasts, opencv=True)


def form_of(editor: ServiceEditor) -> dict[str, str]:
    return dict(editor.state()["form"])


def edit(editor: ServiceEditor, **fields: str) -> None:
    """One keystroke: the page sends the WHOLE candidate, always."""
    current = form_of(editor)
    current.update(fields)
    editor.set_form(current)


# == 1. opening: which service, and what the picker offers ====================


def test_opens_on_the_key_it_was_handed(config: Config, profiles: Path) -> None:
    assert ServiceEditor(config, profiles, "claude").selected_key == "claude"


def test_falls_back_to_the_configured_default_then_alphabetically_first(
    config: Config, profiles: Path
) -> None:
    """A key that named a service since deleted is not a selection."""
    assert ServiceEditor(config, profiles, "no-such-service").selected_key == (
        config.general.service
    )
    stripped = replace(
        config,
        general=replace(config.general, service="gone"),
        services={"zulu": default_services()["claude"], "alpha": default_services()["grok"]},
    )
    assert ServiceEditor(stripped, profiles, None).selected_key == "alpha"


def test_the_picker_lists_every_key_alphabetically_then_the_add_row(
    editor: ServiceEditor,
) -> None:
    rows = editor.state()["services"]
    keys = [row["key"] for row in rows]
    assert keys[:-1] == sorted(keys[:-1])
    assert keys[-1] == NEW_SENTINEL
    builtin = next(row for row in rows if row["key"] == "chatgpt")
    assert builtin["label"] == "chatgpt (builtin)" and builtin["builtin"]


# == 2. the validity matrix (brief §3.1) ======================================


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("label", "   ", "name is required"),
        ("max", "lots", "max input size must be a whole number"),
        ("max", "0", "max input size must be positive"),
        ("total", "many", "total context size must be a whole number"),
        ("total", "-1", "total context size must be positive"),
        ("stable", "soon", "stale seconds must be a number"),
        ("stable", "0.4", "stale seconds must be between 0.5 and 60"),
        ("stable", "60.1", "stale seconds must be between 0.5 and 60"),
        ("delay", "soon", "Enter delay must be a number"),
        ("delay", "-0.1", "Enter delay must be between 0 and 10 seconds"),
        ("delay", "10.1", "Enter delay must be between 0 and 10 seconds"),
    ],
)
def test_each_field_reports_its_own_first_problem(
    editor: ServiceEditor, field: str, value: str, message: str
) -> None:
    edit(editor, **{field: value})
    assert editor.error == message


def test_the_cross_field_size_rule_is_an_error_not_a_clamp(editor: ServiceEditor) -> None:
    edit(editor, max="900000")
    assert editor.error == "max input size can't exceed total context size"
    # And nothing was clamped: the working copy still holds the last valid pair.
    assert editor.services["chatgpt"].max_paste_chars == default_services()[
        "chatgpt"
    ].max_paste_chars


def test_the_bounds_are_the_ones_the_loader_enforces(editor: ServiceEditor) -> None:
    """A value the editor accepts is never silently rewritten on next start."""
    for accepted in ("0.5", "60", "2.0"):
        edit(editor, stable=accepted)
        assert editor.error == ""
        assert editor.services["chatgpt"].stable_seconds == float(accepted)
    # Same rule for the auto-submit beat, whose floor is a real 0: "tap Enter
    # the moment the paste returns" is a setting, not a blank.
    for accepted in ("0", "1.2", "10"):
        edit(editor, delay=accepted)
        assert editor.error == ""
        assert editor.services["chatgpt"].submit_delay_s == float(accepted)


def test_the_submit_delay_is_shown_and_saved_like_every_other_number(
    editor: ServiceEditor, config: Config
) -> None:
    """§11.8's beat, end to end through this model: the box shows what the
    preset holds, a legal edit writes through live, and what ``close`` hands
    back is what a save would file - the setting is only worth having if the
    number the user typed is the number the monitor waits."""
    assert form_of(editor)["delay"] == str(config.services["chatgpt"].submit_delay_s)

    edit(editor, delay="0")

    assert editor.error == ""
    assert editor.services["chatgpt"].submit_delay_s == 0.0
    assert editor.dirty
    result = asyncio.run(editor.close())
    assert result.edits is not None and result.edits.services is not None
    assert result.edits.services["chatgpt"].submit_delay_s == 0.0


def test_extra_instructions_have_no_validation_at_all(editor: ServiceEditor) -> None:
    edit(editor, extra="x" * 4000)
    assert editor.error == ""
    assert editor.services["chatgpt"].extra_instructions == "x" * 4000
    edit(editor, extra="   \n  ")
    assert editor.services["chatgpt"].extra_instructions == ""


# == 3. live apply, and never applying an invalid candidate (brief §3.2) ======


def test_a_valid_candidate_is_written_straight_into_the_working_copy(
    editor: ServiceEditor,
) -> None:
    edit(
        editor,
        label="ChatGPT, renamed",
        max="7000",
        total="123456",
        stable="3.5",
        delay="0.4",
    )
    preset = editor.services["chatgpt"]
    assert preset.label == "ChatGPT, renamed"
    assert (preset.max_paste_chars, preset.total_context_chars) == (7000, 123456)
    assert preset.stable_seconds == 3.5
    assert preset.submit_delay_s == 0.4
    assert editor.dirty


def test_an_invalid_candidate_leaves_the_last_valid_values_alone(
    editor: ServiceEditor,
) -> None:
    edit(editor, label="Halfway there", max="7000")
    good = editor.services["chatgpt"]
    edit(editor, max="")  # mid-retype: not a number
    assert editor.error == "max input size must be a whole number"
    assert editor.services["chatgpt"] == good
    edit(editor, max="8000")  # and it comes back the moment it parses again
    assert editor.error == ""
    assert editor.services["chatgpt"].max_paste_chars == 8000


def test_the_key_is_immutable_on_an_existing_service(editor: ServiceEditor) -> None:
    assert editor.state()["key_locked"] is True
    # Even if the page sent one anyway, it names nothing: the working copy is
    # keyed by the SELECTION, never by the box.
    edit(editor, key="something-else", label="renamed")
    assert "something-else" not in editor.services
    assert editor.services["chatgpt"].key == "chatgpt"


# == 4. the toggles, the radios and the slider ================================


def detection(editor: ServiceEditor, **overrides: Any) -> None:
    state = editor.state()
    payload: dict[str, Any] = {
        "signals": [row["name"] for row in state["signals"] if row["on"]],
        "hover_scan": state["hover_scan"],
        "require_fenced": state["require_fenced"],
        "stream": state["stream"],
        "auto_submit": state["auto_submit"],
    }
    payload.update(overrides)
    editor.set_detection(**payload)


def test_the_whole_toggle_group_folds_in_at_once(editor: ServiceEditor) -> None:
    detection(editor, signals=["stale", "busy", "nonsense"], hover_scan=True, stream=True)
    preset = editor.services["chatgpt"]
    # Canonical order, unknown values dropped - normalize_finish_signals'.
    assert preset.finish_signals == ("busy", "stale")
    assert preset.hover_scan and preset.delivery == DELIVERY_STREAM
    detection(editor, stream=False, require_fenced=True, auto_submit=True)
    preset = editor.services["chatgpt"]
    assert preset.delivery == DELIVERY_PASTE
    assert preset.require_fenced_reply and preset.auto_submit


def test_edit_by_lines_is_its_own_setter_not_part_of_the_detection_set(
    editor: ServiceEditor,
) -> None:
    """The detection set is the LEFT column's toggles read together; this tick
    lives in the form column and is about how the model EDITS, so it writes
    only its own field (docs/design/ui-briefs/service-editor.md 6)."""
    before = editor.services["chatgpt"]
    editor.set_edit_by_lines(True)
    after = editor.services["chatgpt"]
    assert after.edit_by_lines is True
    assert after == replace(before, edit_by_lines=True)
    assert editor.state()["edit_by_lines"] is True
    assert editor.state()["labels"]["edit_by_lines"]

    editor.set_edit_by_lines(False)
    assert editor.services["chatgpt"].edit_by_lines is False


def test_the_after_delivery_pair_is_its_own_setter_too(editor: ServiceEditor) -> None:
    """The TUI's AFTER DELIVERY block, here at last: the switch that stops
    AgentClip stealing the foreground back, and the beep. Read as a PAIR (the
    page sends both boxes on any change) and writing only those two fields."""
    before = editor.services["chatgpt"]
    assert (before.snap_back, before.alert_sound) == (True, False)

    editor.set_after_delivery(snap_back=False, alert_sound=True)

    after = editor.services["chatgpt"]
    assert after == replace(before, snap_back=False, alert_sound=True)
    state = editor.state()
    assert (state["snap_back"], state["alert_sound"]) == (False, True)
    # Worded on this side like every other tick, and each carries the sentence
    # its three words have no room for - these are the two settings a user goes
    # hunting for after something surprised them.
    assert state["labels"]["snap_back"] == "focus back after send"
    assert state["labels"]["alert_sound"] == "beep when it stalls"
    assert "debug aid" in state["titles"]["snap_back"]
    assert "alert" in state["titles"]["alert_sound"]
    assert "0 says it once" in state["titles"]["alert_repeat"]


def test_the_alert_repeat_is_a_validated_form_field(editor: ServiceEditor) -> None:
    """The seconds beside the beep ride the FORM, not the tick pair: it is a
    number like the sizes, with the loader's own bounds (config.py) so a value
    this editor accepts is never silently rewritten on the next start."""
    edit(editor, repeat="30")
    assert editor.error == ""
    assert editor.services["chatgpt"].alert_repeat_seconds == 30

    edit(editor, repeat="")  # cleared: "no repeat", not a refusal to save
    assert editor.error == ""
    assert editor.services["chatgpt"].alert_repeat_seconds == 0

    edit(editor, repeat="soon")
    assert editor.error == "alert repeat must be a whole number of seconds"
    edit(editor, repeat="3601")
    assert editor.error == "alert repeat must be between 0 and 3600 seconds"
    edit(editor, repeat="3600")
    assert editor.error == ""
    assert editor.services["chatgpt"].alert_repeat_seconds == 3600


def test_the_after_delivery_block_shows_what_add_would_create(
    editor: ServiceEditor,
) -> None:
    """"+ Add new" shows the defaults rather than blanks, here as everywhere:
    an unticked "focus back after send" over a preset born with it ON is a lie
    about a setting the user cannot see anywhere else (brief §3.5)."""
    editor.select(NEW_SENTINEL)
    state = editor.state()
    assert (state["snap_back"], state["alert_sound"]) == (True, False)
    assert state["form"]["repeat"] == "0"


def test_the_toggles_apply_even_while_the_form_is_invalid(editor: ServiceEditor) -> None:
    """Independent of the form's validity, exactly as the brief says."""
    edit(editor, max="not a number")
    detection(editor, signals=["idle"])
    assert editor.error != ""
    assert editor.services["chatgpt"].finish_signals == ("idle",)


def test_scroll_and_matcher_write_only_their_own_field(editor: ServiceEditor) -> None:
    before = editor.services["chatgpt"]
    editor.set_scroll(SCROLL_END)
    editor.set_matcher(MATCHER_OPENCV)
    assert editor.services["chatgpt"] == replace(
        before, scroll_action=SCROLL_END, matcher=MATCHER_OPENCV
    )
    editor.set_scroll("teleport")  # not a scroll action
    editor.set_matcher("magic")  # not a matcher
    assert editor.services["chatgpt"].scroll_action == SCROLL_END
    assert editor.services["chatgpt"].matcher == MATCHER_OPENCV


def test_the_tolerance_slider_is_ungated_and_clamped_to_its_own_range(
    editor: ServiceEditor,
) -> None:
    editor.set_tolerance(0)
    assert editor.services["chatgpt"].tolerance == 0
    editor.set_tolerance(64)
    assert editor.services["chatgpt"].tolerance == 64
    editor.set_tolerance(9999)
    assert editor.services["chatgpt"].tolerance == 64
    editor.set_tolerance("nonsense")  # type: ignore[arg-type]
    assert editor.services["chatgpt"].tolerance == 64
    state = editor.state()
    assert (state["tolerance_min"], state["tolerance_max"]) == (0, 64)


# == 5. the OpenCV fallback warning (brief §6) ================================


def test_the_opencv_warning_appears_only_when_it_is_chosen_and_absent(
    config: Config, profiles: Path
) -> None:
    without = ServiceEditor(config, profiles, "chatgpt", opencv=False, frozen=False)
    assert without.state()["matcher_warning"] == ""
    without.set_matcher(MATCHER_OPENCV)
    assert without.state()["matcher_warning"] == OPENCV_MISSING_SOURCE
    # ...and the choice is SAVED anyway: the user may be configuring a machine
    # they are about to install it on.
    assert without.services["chatgpt"].matcher == MATCHER_OPENCV
    without.set_matcher(MATCHER_ANCHORS)
    assert without.state()["matcher_warning"] == ""


def test_a_frozen_build_says_the_other_thing(config: Config, profiles: Path) -> None:
    frozen = ServiceEditor(config, profiles, "chatgpt", opencv=False, frozen=True)
    frozen.set_matcher(MATCHER_OPENCV)
    assert frozen.state()["matcher_warning"] == OPENCV_MISSING_FROZEN
    present = ServiceEditor(config, profiles, "chatgpt", opencv=True, frozen=True)
    present.set_matcher(MATCHER_OPENCV)
    assert present.state()["matcher_warning"] == ""


# == 6. "+ add new": nothing is written until the press (brief §3.5) ==========


def new_candidate(editor: ServiceEditor, key: str = "acme") -> None:
    editor.select(NEW_SENTINEL)
    editor.set_form(
        {"key": key, "label": "Acme chat", "max": "5000", "total": "50000",
         "stable": "2.0", "delay": "1.2", "extra": ""}
    )


def test_the_add_row_disables_every_control_but_still_shows_the_defaults(
    editor: ServiceEditor,
) -> None:
    editor.select(NEW_SENTINEL)
    state = editor.state()
    assert state["is_new"] and state["controls_disabled"]
    assert state["key_locked"] is False
    # Not blank: what "Add service" will really create.
    assert [row["name"] for row in state["signals"] if row["on"]] == ["stale"]
    assert state["matcher"] == MATCHER_ANCHORS and state["tolerance"] == 24
    assert state["hover_scan"] is False and state["stream"] is False
    assert all(not kind["can_clear"] for kind in state["kinds"])
    assert state["show_add"] and not state["show_reset"] and not state["show_delete"]


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("", "key is required"),
        ("Acme", "key must be lowercase letters, digits, and hyphens only"),
        ("acme_chat", "key must be lowercase letters, digits, and hyphens only"),
        ("acme--chat", "key must be lowercase letters, digits, and hyphens only"),
        ("-acme", "key must be lowercase letters, digits, and hyphens only"),
        ("chatgpt", "key 'chatgpt' is already in use"),
    ],
)
def test_the_new_key_is_validated_before_anything_can_be_filed_under_it(
    editor: ServiceEditor, key: str, message: str
) -> None:
    new_candidate(editor, key)
    assert editor.error == message
    assert editor.state()["can_add"] is False
    editor.add()  # the button is disabled; pressing it anyway files nothing
    assert key not in editor.services or key == "chatgpt"


def test_add_is_gated_on_the_whole_candidate_validating(editor: ServiceEditor) -> None:
    new_candidate(editor)
    assert editor.error == "" and editor.state()["can_add"]
    edit(editor, max="60000")  # now > total
    assert editor.state()["can_add"] is False
    edit(editor, max="5000")
    assert editor.state()["can_add"]


def test_add_commits_the_candidate_and_selects_it(editor: ServiceEditor, toasts: Toasts) -> None:
    new_candidate(editor)
    editor.add()
    assert editor.selected_key == "acme"
    created = editor.services["acme"]
    # Every field the form does not ask about is the dataclass default, and the
    # form showed exactly those while it was being filled in.
    assert created == ServicePreset(
        key="acme", label="Acme chat", max_paste_chars=5000, total_context_chars=50000
    )
    state = editor.state()
    assert state["reload"] and state["form"]["key"] == "acme" and state["key_locked"]
    assert state["controls_disabled"] is False
    assert state["show_delete"] and not state["show_reset"] and not state["show_add"]
    assert toasts.saying("acme added")


def test_nothing_is_written_before_the_press(editor: ServiceEditor) -> None:
    new_candidate(editor)
    assert "acme" not in editor.services
    assert not editor.dirty
    # And a candidate abandoned by selecting something else leaves no trace.
    editor.select("claude")
    editor.select(NEW_SENTINEL)
    editor.add()
    assert "acme" not in editor.services


# == 7. reset and delete (brief §3.6, §5.8, §5.9) =============================


def test_reset_restores_the_shipped_values_and_touches_no_captures(
    editor: ServiceEditor, profiles: Path
) -> None:
    save_template(profiles, "chatgpt", TemplateKind.BUSY, searchable())
    editor.select("chatgpt")
    edit(editor, label="renamed", max="1")
    editor.set_tolerance(3)
    editor.reset()
    assert editor.services["chatgpt"] == default_services()["chatgpt"]
    assert not editor.dirty
    assert load_profile(profiles, "chatgpt").has(TemplateKind.BUSY)
    assert editor.profiles_changed is False


def test_reset_is_offered_for_every_builtin_and_delete_for_none_of_them(
    editor: ServiceEditor,
) -> None:
    for key in sorted(BUILTIN_SERVICE_KEYS):
        editor.select(key)
        state = editor.state()
        assert state["show_reset"] and not state["show_delete"]
    editor.select("chatgpt")
    editor.delete()  # refused: built-ins are edited or reset, never deleted
    assert "chatgpt" in editor.services


def test_delete_removes_a_custom_key_and_its_captures(
    editor: ServiceEditor, profiles: Path
) -> None:
    new_candidate(editor)
    editor.add()
    save_template(profiles, "acme", TemplateKind.COPY, searchable())
    assert load_profile(profiles, "acme").has(TemplateKind.COPY)
    editor.delete()
    assert "acme" not in editor.services
    assert not load_profile(profiles, "acme").has(TemplateKind.COPY)
    assert editor.profiles_changed
    # ...and the alphabetically-first survivor is selected.
    assert editor.selected_key == sorted(editor.services)[0]


def test_a_profile_that_will_not_delete_is_not_reported_as_deleted(
    editor: ServiceEditor, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_candidate(editor)
    editor.add()
    monkeypatch.setattr(
        f"{MODULE}.delete_profile",
        lambda root, key: (_ for _ in ()).throw(ProfileStoreError("locked")),
    )
    editor.delete()
    assert "acme" not in editor.services  # the preset still goes
    assert editor.profiles_changed is False  # the disk did not move


# == 8. appearances: status, thumbnails, clear, forget ========================


def test_the_seven_rows_are_always_all_there(editor: ServiceEditor) -> None:
    kinds = editor.state()["kinds"]
    assert [row["kind"] for row in kinds] == [str(kind) for kind in TemplateKind]
    assert len(kinds) == 7
    assert all(row["status"] == TEMPLATE_UNSET for row in kinds)
    assert editor.state()["templates"] == TEMPLATES_NONE
    assert editor.state()["show_forget"] is False


def row_of(editor: ServiceEditor, kind: TemplateKind) -> dict[str, Any]:
    """One appearance row out of the event the page renders from."""
    return next(row for row in editor.state()["kinds"] if row["kind"] == str(kind))


def stack(profiles: Path, key: str, kind: TemplateKind, *sizes: tuple[int, int]) -> None:
    """A kind captured ``len(sizes)`` times, each variant its own size.

    Distinguishable by size on purpose: the status line names the SHOWN
    variant's dimensions, so which picture a row is on is readable from it.
    """
    for width, height in sizes:
        save_template(profiles, key, kind, searchable(width, height))


def test_a_kind_is_a_stack_and_the_status_line_says_so(profiles: Path) -> None:
    profile = ServiceProfile("chatgpt")
    assert template_status(profile, TemplateKind.BUSY) == TEMPLATE_UNSET
    profile.put(TemplateKind.BUSY, searchable(24, 12))
    assert template_status(profile, TemplateKind.BUSY) == "24×12 · captured"
    profile.put(TemplateKind.BUSY, searchable(30, 9))
    # Above one image the count becomes a POSITION, with the SHOWN variant's
    # size beside it - a second capture ADDS, and the row is a window onto the
    # stack rather than a picture of a slot.
    assert template_status(profile, TemplateKind.BUSY) == "24×12 · 1/2"
    assert template_status(profile, TemplateKind.BUSY, 1) == "30×9 · 2/2"
    # An index from a stack that has since shrunk clamps rather than raising.
    assert template_status(profile, TemplateKind.BUSY, 99) == "30×9 · 2/2"
    assert templates_line(profile) == "appearance: 1/7 captured"


def test_a_captured_kind_carries_a_png_thumbnail_of_the_variant_on_show(
    config: Config, profiles: Path
) -> None:
    save_template(profiles, "chatgpt", TemplateKind.IDLE, searchable())
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    row = row_of(editor, TemplateKind.IDLE)
    assert row["png"].startswith("data:image/png;base64,")
    assert (row["shown"], row["count"]) == (0, 1)
    assert row["can_clear"] is True
    blank = row_of(editor, TemplateKind.BUSY)
    assert blank["png"] == "" and blank["can_clear"] is False
    assert (blank["shown"], blank["count"]) == (0, 0)
    assert editor.state()["show_forget"] is True
    assert editor.state()["templates"] == "appearance: 1/7 captured"


def test_the_arrows_walk_the_stack_and_wrap_at_both_ends(
    config: Config, profiles: Path
) -> None:
    stack(profiles, "chatgpt", TemplateKind.IDLE, (24, 12), (30, 9), (28, 20))
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    thumbs = []
    for expected in ("24×12 · 1/3", "30×9 · 2/3", "28×20 · 3/3"):
        row = row_of(editor, TemplateKind.IDLE)
        assert row["status"] == expected
        thumbs.append(row["png"])
        editor.show_next(TemplateKind.IDLE)
    # Three different pictures, and the fourth press really came back to the
    # first one rather than stopping at the end.
    wrapped = row_of(editor, TemplateKind.IDLE)
    assert len(set(thumbs)) == 3
    assert wrapped["status"] == "24×12 · 1/3" and wrapped["png"] == thumbs[0]
    # ...and left wraps the other way, off the front onto the last.
    editor.show_previous(TemplateKind.IDLE)
    assert row_of(editor, TemplateKind.IDLE)["status"] == "28×20 · 3/3"


def test_a_kind_with_nothing_to_cycle_stays_where_it_is(
    config: Config, profiles: Path
) -> None:
    """The page keeps the arrows disabled below two images; the model does not
    take that on trust."""
    save_template(profiles, "chatgpt", TemplateKind.IDLE, searchable(24, 12))
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    for kind in (TemplateKind.IDLE, TemplateKind.BUSY):
        editor.show_next(kind)
        editor.show_previous(kind)
        assert row_of(editor, kind)["shown"] == 0
    assert row_of(editor, TemplateKind.IDLE)["status"] == "24×12 · captured"


def test_clear_drops_the_variant_on_show_immediately_and_without_a_confirm(
    config: Config, profiles: Path, toasts: Toasts
) -> None:
    stack(profiles, "chatgpt", TemplateKind.IDLE, (24, 12), (30, 9), (28, 20))
    save_template(profiles, "chatgpt", TemplateKind.BUSY, searchable())
    asked = Answers()
    editor = ServiceEditor(
        config, profiles, "chatgpt", notify=toasts, confirm=asked, opencv=True
    )
    editor.show_next(TemplateKind.IDLE)  # the middle one
    editor.clear(TemplateKind.IDLE)
    assert asked.asked == []  # no dialog, by design
    # One image, not the kind: a bad capture does not cost the good ones.
    assert [
        (t.width, t.height) for t in load_profile(profiles, "chatgpt").variants(TemplateKind.IDLE)
    ] == [(24, 12), (28, 20)]
    assert load_profile(profiles, "chatgpt").has(TemplateKind.BUSY)
    assert editor.profiles_changed
    # The index stays put, so what slid into the slot is what is on show.
    assert row_of(editor, TemplateKind.IDLE)["status"] == "28×20 · 2/2"
    assert editor.state()["templates"] == "appearance: 2/7 captured"
    assert toasts.saying("idle indicator cleared for chatgpt")


def test_clearing_the_last_variant_clamps_back_onto_the_new_last_one(
    config: Config, profiles: Path
) -> None:
    stack(profiles, "chatgpt", TemplateKind.IDLE, (24, 12), (30, 9))
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    editor.show_next(TemplateKind.IDLE)
    editor.clear(TemplateKind.IDLE)
    row = row_of(editor, TemplateKind.IDLE)
    assert (row["shown"], row["count"]) == (0, 1)
    assert row["status"] == "24×12 · captured"
    # ...and the last one left empties the row back to "not captured".
    editor.clear(TemplateKind.IDLE)
    row = row_of(editor, TemplateKind.IDLE)
    assert row["status"] == TEMPLATE_UNSET and row["png"] == ""
    assert (row["shown"], row["count"]) == (0, 0) and row["can_clear"] is False
    assert editor.state()["templates"] == TEMPLATES_NONE


def test_a_shown_index_survives_a_profile_re_read(config: Config, profiles: Path) -> None:
    """The folder moves under the editor - another service, a forget, a clear -
    so the index is clamped against what is really there, never trusted."""
    stack(profiles, "chatgpt", TemplateKind.IDLE, (24, 12), (30, 9), (28, 20))
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    editor.show_next(TemplateKind.IDLE)
    editor.show_next(TemplateKind.IDLE)
    assert row_of(editor, TemplateKind.IDLE)["shown"] == 2
    editor.select("claude")  # a service with nothing captured at all
    assert row_of(editor, TemplateKind.IDLE)["shown"] == 0
    editor.select("chatgpt")
    assert row_of(editor, TemplateKind.IDLE)["shown"] == 0
    # And a stack that goes away entirely under a live index is a row that
    # reads as uncaptured, not one pointing past the end of nothing.
    editor.show_next(TemplateKind.IDLE)
    editor.show_next(TemplateKind.IDLE)
    asyncio.run(editor.forget())
    row = row_of(editor, TemplateKind.IDLE)
    assert row["status"] == TEMPLATE_UNSET and row["shown"] == 0


def test_forget_asks_first_and_deletes_the_whole_profile(
    config: Config, profiles: Path
) -> None:
    save_template(profiles, "chatgpt", TemplateKind.IDLE, searchable())
    save_template(profiles, "chatgpt", TemplateKind.BUSY, searchable())
    declined = Answers(False)
    editor = ServiceEditor(config, profiles, "chatgpt", confirm=declined, opencv=True)
    asyncio.run(editor.forget())
    assert declined.asked and "Forget the chatgpt appearance?" in declined.asked[0][0]
    assert load_profile(profiles, "chatgpt").captured  # declining changes nothing
    assert editor.profiles_changed is False

    editor = ServiceEditor(config, profiles, "chatgpt", confirm=Answers(True), opencv=True)
    asyncio.run(editor.forget())
    assert not load_profile(profiles, "chatgpt").captured
    assert editor.profiles_changed
    assert editor.state()["templates"] == TEMPLATES_NONE
    assert editor.state()["show_forget"] is False
    # The PRESET is untouched - forgetting is not deleting.
    assert editor.services["chatgpt"] == default_services()["chatgpt"]


def test_the_ticked_but_not_captured_warning_is_re_derived_from_disk(
    config: Config, profiles: Path
) -> None:
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    detection(editor, signals=["busy", "idle", "stale"])
    warning = editor.state()["signal_warning"]
    assert warning.startswith("busy indicator, idle indicator: ") and SIGNAL_UNCAPTURED in warning
    save_template(profiles, "chatgpt", TemplateKind.BUSY, searchable())
    save_template(profiles, "chatgpt", TemplateKind.IDLE, searchable())
    editor.select("chatgpt")  # re-reads the folder
    assert editor.state()["signal_warning"] == ""
    # "stale" never needs an appearance: the drawn chat region is the detector.
    detection(editor, signals=["stale"])
    assert editor.state()["signal_warning"] == ""


# == 9. capture: the flow, its guard and its refusals (brief §4.4, §5.5) ======


class Picker:
    """``pick_region``'s stand-in: a scripted answer and a record of the prompt."""

    def __init__(self, region: ScreenRegion | None, error: str | None = None) -> None:
        self.region = region
        self.error = error
        self.prompts: list[str] = []

    def __call__(self, prompt: str | None = None) -> ScreenRegion | None:
        self.prompts.append(prompt or "")
        if self.error is not None:
            raise ScreenPickError(self.error)
        return self.region


REGION = ScreenRegion(10, 20, 24, 12)


def wire_capture(
    monkeypatch: pytest.MonkeyPatch,
    picker: Picker,
    image: RegionImage | None = None,
    capture_error: str | None = None,
) -> None:
    monkeypatch.setattr(f"{MODULE}.pick_region", picker)

    def capture(region: ScreenRegion) -> RegionImage:
        if capture_error is not None:
            raise CaptureError(capture_error)
        return image if image is not None else searchable()

    monkeypatch.setattr(f"{MODULE}.capture_region", capture)


def run_capture(editor: ServiceEditor, kind: TemplateKind) -> bool:
    claimed = editor.start_capture(kind)
    if claimed:
        asyncio.run(editor.run_capture(kind))
    return claimed


def test_a_capture_writes_the_png_and_re_derives_the_readouts(
    editor: ServiceEditor, profiles: Path, toasts: Toasts, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker = Picker(REGION)
    wire_capture(monkeypatch, picker)
    assert run_capture(editor, TemplateKind.BUSY)
    # The prompt is the KIND's, not the editor's: what makes a good capture is a
    # fact about the appearance and must read identically wherever it is asked.
    assert picker.prompts == [TemplateKind.BUSY.prompt]
    assert load_profile(profiles, "chatgpt").has(TemplateKind.BUSY)
    assert editor.profiles_changed
    row = next(row for row in editor.state()["kinds"] if row["kind"] == str(TemplateKind.BUSY))
    assert row["status"] == "24×12 · captured" and row["png"] and row["can_clear"]
    assert editor.state()["templates"] == "appearance: 1/7 captured"
    assert editor.capturing is False
    assert toasts.saying("busy indicator captured for chatgpt")


def test_a_second_capture_adds_a_variant_rather_than_replacing(
    editor: ServiceEditor, profiles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_capture(monkeypatch, Picker(REGION), searchable(24, 12))
    run_capture(editor, TemplateKind.COPY)
    first = row_of(editor, TemplateKind.COPY)["png"]
    wire_capture(monkeypatch, Picker(REGION), searchable(30, 9))
    run_capture(editor, TemplateKind.COPY)
    assert len(load_profile(profiles, "chatgpt").variants(TemplateKind.COPY)) == 2
    row = row_of(editor, TemplateKind.COPY)
    # And the row lands ON what was just drawn: a capture that left the older
    # picture up would read as one that did not land.
    assert row["status"] == "30×9 · 2/2" and (row["shown"], row["count"]) == (1, 2)
    assert row["png"] != first
    editor.show_previous(TemplateKind.COPY)
    assert row_of(editor, TemplateKind.COPY)["png"] == first


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"picker": Picker(None)}, "unchanged (selection cancelled)"),
        ({"picker": Picker(None, error="the picker died")}, "the picker died"),
        ({"picker": Picker(REGION), "capture_error": "no screen"}, "could not capture"),
        (
            {"picker": Picker(REGION), "image": RegionImage(4, 4, b"\x00" * 64)},
            "cannot be searched for",
        ),
    ],
)
def test_every_failure_writes_nothing_and_says_why(
    editor: ServiceEditor,
    profiles: Path,
    toasts: Toasts,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    needle: str,
) -> None:
    wire_capture(monkeypatch, **kwargs)
    run_capture(editor, TemplateKind.BUSY)
    assert not load_profile(profiles, "chatgpt").has(TemplateKind.BUSY)
    assert editor.profiles_changed is False
    assert editor.capturing is False  # the guard is released either way
    assert toasts.saying(needle)


def test_a_save_failure_files_nothing(
    editor: ServiceEditor, toasts: Toasts, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_capture(monkeypatch, Picker(REGION))
    monkeypatch.setattr(
        f"{MODULE}.save_template",
        lambda root, key, kind, image: (_ for _ in ()).throw(ProfileStoreError("disk full")),
    )
    run_capture(editor, TemplateKind.BUSY)
    assert editor.profiles_changed is False
    assert toasts.saying("could not save the busy indicator: disk full")


def test_only_one_overlay_may_be_in_flight(editor: ServiceEditor, toasts: Toasts) -> None:
    """The claim is synchronous, so the SECOND press is refused, not raced."""
    assert editor.start_capture(TemplateKind.BUSY) is True
    assert editor.start_capture(TemplateKind.IDLE) is False
    assert toasts.saying("a region picker is already open")
    assert editor.state()["capturing"] is True


def test_clear_and_forget_stand_down_while_a_capture_is_up(
    config: Config, profiles: Path
) -> None:
    save_template(profiles, "chatgpt", TemplateKind.IDLE, searchable())
    asked = Answers(True)
    editor = ServiceEditor(config, profiles, "chatgpt", confirm=asked, opencv=True)
    editor.start_capture(TemplateKind.BUSY)
    editor.clear(TemplateKind.IDLE)
    asyncio.run(editor.forget())
    assert asked.asked == []
    assert load_profile(profiles, "chatgpt").has(TemplateKind.IDLE)


def test_capture_is_refused_with_no_key_to_file_it_under(editor: ServiceEditor) -> None:
    editor.select(NEW_SENTINEL)
    assert editor.start_capture(TemplateKind.BUSY) is False
    assert editor.capturing is False


# == 10. closing (brief §3.4, §5.10) ==========================================


def test_closing_an_untouched_editor_hands_back_nothing_at_all(
    editor: ServiceEditor,
) -> None:
    result = asyncio.run(editor.close())
    assert result.closed and result.edits is None


def test_closing_after_an_edit_hands_back_the_whole_table(editor: ServiceEditor) -> None:
    edit(editor, label="renamed")
    result = asyncio.run(editor.close())
    assert result.closed and result.edits is not None
    assert result.edits.services is not None
    assert result.edits.services["chatgpt"].label == "renamed"
    assert result.edits.profiles_changed is False


def test_a_capture_only_visit_still_reports_itself(
    editor: ServiceEditor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``profiles_changed`` is separate because the caller cannot diff for it."""
    wire_capture(monkeypatch, Picker(REGION))
    run_capture(editor, TemplateKind.BUSY)
    result = asyncio.run(editor.close())
    assert result.closed and result.edits is not None
    assert result.edits.services is None and result.edits.profiles_changed is True


def test_an_edit_undone_by_hand_is_not_a_change(editor: ServiceEditor) -> None:
    original = editor.services["chatgpt"].label
    edit(editor, label="renamed")
    edit(editor, label=original)
    result = asyncio.run(editor.close())
    assert result.closed and result.edits is None


def test_closing_over_invalid_text_asks_before_discarding(config: Config, profiles: Path) -> None:
    declined = Answers(False, True)
    editor = ServiceEditor(config, profiles, "chatgpt", confirm=declined, opencv=True)
    edit(editor, label="applied")  # valid: applied live, straight into the copy
    edit(editor, max="nope")  # invalid: never committed, and never lost either
    first = asyncio.run(editor.close())
    assert first.closed is False  # declining returns to the editor
    title, body = declined.asked[0]
    assert title == "Discard the pending edit?"
    assert "max input size must be a whole number" in body
    assert editor.error == "max input size must be a whole number"  # the text survives
    second = asyncio.run(editor.close())
    assert second.closed and second.edits is not None
    # The valid half was applied live and is kept; the invalid half was never
    # committed, so there was nothing to lose.
    assert second.edits.services is not None
    assert second.edits.services["chatgpt"].label == "applied"


def test_closing_is_refused_outright_while_a_capture_is_up(
    editor: ServiceEditor, toasts: Toasts
) -> None:
    editor.start_capture(TemplateKind.BUSY)
    result = asyncio.run(editor.close())
    assert result.closed is False and result.edits is None
    assert toasts.saying("a region picker is open")


# == 11. the minimal write, through the real save_services ====================


def test_the_saved_table_round_trips_and_writes_only_what_differs(
    project: Path, config: Config, profiles: Path, tmp_path: Path
) -> None:
    """The whole save path for real: editor -> ServiceEdits -> save_services ->
    load_config. Minimal-write is ``config.py``'s rule and this is the proof it
    survives the GUI's copy of the working model."""
    target = tmp_path / "config.toml"
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    edit(editor, max="7777")
    editor.select(NEW_SENTINEL)
    editor.set_form(
        {"key": "acme", "label": "Acme chat", "max": "5000", "total": "50000",
         "stable": "2.0", "delay": "1.2", "extra": "mind the ]("}
    )
    editor.add()
    editor.set_tolerance(40)
    result = asyncio.run(editor.close())
    assert result.edits is not None and result.edits.services is not None
    save_services(result.edits.services, target)

    text = target.read_text(encoding="utf-8")
    # The eleven untouched built-ins are simply not in the file.
    assert "[services.claude]" not in text
    assert "[services.chatgpt]" in text and "[services.acme]" in text
    # ...and within a written table, only the fields that really differ.
    assert "tolerance" in text.split("[services.acme]")[1]
    assert "tolerance" not in text.split("[services.chatgpt]")[1].split("[services.acme]")[0]

    reloaded = load_config(project, global_config_path=target)
    assert reloaded.services["chatgpt"].max_paste_chars == 7777
    assert reloaded.services["claude"] == default_services()["claude"]
    acme = reloaded.services["acme"]
    assert acme.tolerance == 40 and acme.extra_instructions == "mind the ]("
    assert acme.max_paste_chars == 5000 and acme.total_context_chars == 50000


def test_the_after_delivery_settings_round_trip_through_the_real_save_path(
    project: Path, config: Config, profiles: Path, tmp_path: Path
) -> None:
    """All three of them out through ``save_services`` and back through
    ``load_config``: the two ticks and the seconds are ordinary preset fields,
    so the GUI's editor gets them onto disk by the same path as every other."""
    target = tmp_path / "config.toml"
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    editor.set_after_delivery(snap_back=False, alert_sound=True)
    edit(editor, repeat="45")
    result = asyncio.run(editor.close())
    assert result.edits is not None and result.edits.services is not None
    save_services(result.edits.services, target)

    reloaded = load_config(project, global_config_path=target).services["chatgpt"]
    assert reloaded.snap_back is False
    assert reloaded.alert_sound is True
    assert reloaded.alert_repeat_seconds == 45
    # ...and a service nobody touched is still absent from the file entirely.
    assert "[services.claude]" not in target.read_text(encoding="utf-8")


def test_a_reset_builtin_disappears_from_the_file(
    project: Path, config: Config, profiles: Path, tmp_path: Path
) -> None:
    target = tmp_path / "config.toml"
    editor = ServiceEditor(config, profiles, "grok", opencv=True)
    edit(editor, label="pinned")
    save_services(editor.services, target)
    assert "[services.grok]" in target.read_text(encoding="utf-8")
    editor.reset()
    save_services(editor.services, target)
    assert "[services.grok]" not in target.read_text(encoding="utf-8")
    assert load_config(project, global_config_path=target).services["grok"] == (
        default_services()["grok"]
    )


# == the click point: which pixel of an appearance gets the click =============


def test_every_row_starts_in_the_middle_of_its_picture(editor: ServiceEditor) -> None:
    """50/50 is where every click landed before the point was adjustable, so it
    is what an untouched profile has to report."""
    for row in editor.state()["kinds"]:
        assert (row["click_x"], row["click_y"]) == (50, 50)
    # The boxes are two bare number fields side by side, so the hover text is
    # the only thing that says which one is which: each has to name its axis.
    labels = editor.state()["click_labels"]
    assert "%" in labels["x"] and "%" in labels["y"]
    assert "horizontal" in labels["x"] and "left" in labels["x"]
    assert "vertical" in labels["y"] and "top" in labels["y"]


def test_a_click_point_is_written_to_the_store_immediately(
    config: Config, profiles: Path, toasts: Toasts
) -> None:
    """A capture-side setting, so it takes the capture side's commit model: the
    folder is the working copy and there is nothing to hand back on close."""
    save_template(profiles, "chatgpt", TemplateKind.CHATBOX_ONGOING, searchable())
    editor = ServiceEditor(config, profiles, "chatgpt", notify=toasts, opencv=True)

    editor.set_click_point(TemplateKind.CHATBOX_ONGOING, 20, 80)

    assert load_profile(profiles, "chatgpt").click_point(TemplateKind.CHATBOX_ONGOING) == (20, 80)
    row = row_of(editor, TemplateKind.CHATBOX_ONGOING)
    assert (row["click_x"], row["click_y"]) == (20, 80)
    assert editor.profiles_changed
    assert not editor.dirty  # the PRESET table did not move


def test_a_click_point_is_clamped_rather_than_refused(editor: ServiceEditor) -> None:
    """The page sends whatever is in a number box, an empty one included."""
    editor.set_click_point(TemplateKind.COPY, "120", "")

    row = row_of(editor, TemplateKind.COPY)
    assert (row["click_x"], row["click_y"]) == (100, 50)


def test_the_add_new_row_has_nothing_to_aim(config: Config, profiles: Path) -> None:
    """No key means no folder to file a click point under - the same disabled
    set the toggles are in (brief §3.5)."""
    editor = ServiceEditor(config, profiles, NEW_SENTINEL, opencv=True)
    editor.select(NEW_SENTINEL)

    editor.set_click_point(TemplateKind.COPY, 10, 10)

    assert editor.state()["controls_disabled"] is True
    assert not editor.profiles_changed
    assert row_of(editor, TemplateKind.COPY)["click_x"] == 50


def test_clearing_the_last_picture_recentres_the_click_point(
    config: Config, profiles: Path
) -> None:
    save_template(profiles, "chatgpt", TemplateKind.COPY, searchable())
    editor = ServiceEditor(config, profiles, "chatgpt", opencv=True)
    editor.set_click_point(TemplateKind.COPY, 10, 90)

    editor.clear(TemplateKind.COPY)

    assert row_of(editor, TemplateKind.COPY)["click_x"] == 50
    assert load_profile(profiles, "chatgpt").click_point(TemplateKind.COPY) == (50, 50)
