"""Profiles on disk: the PNG-per-appearance store (screen/profile_store.py).

Everything runs under tmp_path - the real store lives in the user's config
home, and no test may go near it. The recurring theme is that loading is total:
whatever is (or isn't) on disk, a profile comes back.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path

import pytest

from agentclip.driver.screen import profile_store
from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.profile_store import (
    FORMAT_VERSION,
    MANIFEST_NAME,
    ProfileStoreError,
    delete_profile,
    drop_template,
    drop_variant,
    known_keys,
    load_profile,
    profile_dir,
    save_template,
)

BAD_KEYS = ("../evil", "UPPER", "", "with space", "a/b", ".", "trailing-")


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file in ``directory``, byte for byte."""
    return {entry.name: entry.read_bytes() for entry in sorted(directory.iterdir())}


def rewrite_version(directory: Path, version: object) -> None:
    manifest = directory / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["version"] = version
    manifest.write_text(json.dumps(raw), encoding="utf-8")


def downgrade_to_v1(directory: Path) -> None:
    """Rewrite a freshly saved profile the way the pre-variants build wrote it:
    format 1, one entry per kind rather than a list."""
    manifest = directory / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["version"] = 1
    raw["templates"] = {name: entries[0] for name, entries in raw["templates"].items()}
    manifest.write_text(json.dumps(raw), encoding="utf-8")


def patch(width: int = 20, height: int = 16, shade: int = 0) -> RegionImage:
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(((x * 11 + shade) % 256, (y * 23) % 256, (x * y) % 256, 0))
    return RegionImage(width, height, bytes(pixels))


def bgr(image: RegionImage) -> bytes:
    """The defined channels of a frame, X byte dropped."""
    pixels = image.pixels
    return bytes(b for i, b in enumerate(pixels) if i % 4 != 3)


def test_a_saved_appearance_reloads_pixel_for_pixel(tmp_path: Path) -> None:
    image = patch()
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, image)
    (template,) = load_profile(tmp_path, "chatgpt").variants(TemplateKind.BUSY)
    assert (template.width, template.height) == (20, 16)
    assert bgr(template.image) == bgr(image)


def test_several_appearances_round_trip_together(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=1))
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch(24, 24, shade=2))
    save_template(tmp_path, "chatgpt", TemplateKind.NEW_CHAT, patch(shade=3))
    profile = load_profile(tmp_path, "chatgpt")
    assert profile.key == "chatgpt"
    assert profile.captured == (TemplateKind.BUSY, TemplateKind.COPY, TemplateKind.NEW_CHAT)


def test_the_ready_to_send_button_round_trips_like_any_other_appearance(
    tmp_path: Path,
) -> None:
    """The newest kind needed no manifest change: a kind is a file name.

    ``FORMAT_VERSION`` is about the SHAPE of the manifest, not its contents -
    the templates table is keyed by kind value, so an added appearance is one
    more entry an older build simply skips (``load_profile`` drops names it does
    not recognise) rather than a format nobody can read.
    """
    save_template(tmp_path, "chatgpt", TemplateKind.SEND_READY, patch(28, 28, shade=4))
    raw = json.loads((tmp_path / "chatgpt" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert raw["version"] == FORMAT_VERSION
    assert raw["templates"]["send-ready"][0]["file"] == "send-ready.png"
    (template,) = load_profile(tmp_path, "chatgpt").variants(TemplateKind.SEND_READY)
    assert (template.width, template.height) == (28, 28)


# -- a kind is a stack of images ------------------------------------------------


def test_a_second_save_adds_a_numbered_file_rather_than_overwriting(tmp_path: Path) -> None:
    """The motivating case: the greyed-out send button is a second picture of
    the same control, and losing the first would just move the blind spot."""
    save_template(tmp_path, "chatgpt", TemplateKind.SEND_READY, patch(shade=1))
    save_template(tmp_path, "chatgpt", TemplateKind.SEND_READY, patch(24, 24, shade=2))
    save_template(tmp_path, "chatgpt", TemplateKind.SEND_READY, patch(28, 28, shade=3))
    directory = tmp_path / "chatgpt"
    raw = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [entry["file"] for entry in raw["templates"]["send-ready"]] == [
        "send-ready.png",
        "send-ready-2.png",
        "send-ready-3.png",
    ]
    assert sorted(p.name for p in directory.glob("*.png")) == [
        "send-ready-2.png",
        "send-ready-3.png",
        "send-ready.png",
    ]
    # Capture order round-trips, and it is still one calibrated kind.
    profile = load_profile(tmp_path, "chatgpt")
    assert [
        (t.width, t.height) for t in profile.variants(TemplateKind.SEND_READY)
    ] == [(20, 16), (24, 24), (28, 28)]
    assert profile.describe() == "1/7 captured"


def test_one_corrupt_image_costs_only_itself_not_the_stack(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch(shade=1))
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch(24, 24, shade=2))
    (tmp_path / "chatgpt" / "copy.png").write_bytes(b"not a png at all")
    profile = load_profile(tmp_path, "chatgpt")
    assert [(t.width, t.height) for t in profile.variants(TemplateKind.COPY)] == [(24, 24)]
    assert profile.captured == (TemplateKind.COPY,)


def test_drop_template_removes_every_image_of_its_kind(tmp_path: Path) -> None:
    """What the editor's per-kind "Clear" asks for: half a stack still matches,
    which is indistinguishable from the clear having done nothing."""
    for shade in (1, 2, 3):
        save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=shade))
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch(shade=4))
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    directory = tmp_path / "chatgpt"
    raw = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(raw["templates"]) == {"copy"}
    assert sorted(p.name for p in directory.glob("*.png")) == ["copy.png"]
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


def test_drop_variant_removes_exactly_the_image_it_names(tmp_path: Path) -> None:
    """What the GUI editor's per-variant "Clear" asks for: a bad third capture
    should not cost the two good ones beside it."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(20, 16, shade=1))
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(24, 24, shade=2))
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(28, 12, shade=3))
    drop_variant(tmp_path, "chatgpt", TemplateKind.BUSY, 1)
    directory = tmp_path / "chatgpt"
    raw = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [entry["file"] for entry in raw["templates"]["busy"]] == ["busy.png", "busy-3.png"]
    assert not (directory / "busy-2.png").exists()
    # The stack closes up behind it, in capture order: the survivors are the
    # first and the third, and nothing renamed itself underneath them.
    assert [
        (t.width, t.height) for t in load_profile(tmp_path, "chatgpt").variants(TemplateKind.BUSY)
    ] == [(20, 16), (28, 12)]


def test_dropping_the_last_image_of_a_kind_unlists_the_kind(tmp_path: Path) -> None:
    """An empty stack and an uncaptured kind are the same thing on disk."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    drop_variant(tmp_path, "chatgpt", TemplateKind.BUSY, 0)
    raw = json.loads((tmp_path / "chatgpt" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(raw["templates"]) == {"copy"}
    assert not (tmp_path / "chatgpt" / "busy.png").exists()
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


@pytest.mark.parametrize("index", [-1, 1, 99])
def test_an_index_naming_no_image_leaves_the_folder_exactly_as_it_was(
    tmp_path: Path, index: int
) -> None:
    """A stale row is a no-op, not an error: the caller re-reads the folder
    after every one of these, so all a raise could add is a complaint about a
    picture that was already not there."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    before = snapshot(tmp_path / "chatgpt")
    drop_variant(tmp_path, "chatgpt", TemplateKind.BUSY, index)
    drop_variant(tmp_path, "chatgpt", TemplateKind.COPY, 0)  # a kind with nothing in it
    drop_variant(tmp_path, "no-such-service", TemplateKind.BUSY, 0)  # no folder at all
    assert snapshot(tmp_path / "chatgpt") == before


def test_the_numbering_starts_over_after_a_clear(tmp_path: Path) -> None:
    """It is read off the manifest, so a cleared kind has no names taken."""
    for shade in (1, 2):
        save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=shade))
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=3))
    raw = json.loads((tmp_path / "chatgpt" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [entry["file"] for entry in raw["templates"]["busy"]] == ["busy.png"]


# -- reading what an older build wrote ------------------------------------------


def test_a_format_1_manifest_loads_as_one_image_stacks(tmp_path: Path) -> None:
    """A profile captured before variants keeps working untouched: one entry
    per kind IS a stack of one."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=1))
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch(24, 24, shade=2))
    downgrade_to_v1(tmp_path / "chatgpt")
    profile = load_profile(tmp_path, "chatgpt")
    assert profile.captured == (TemplateKind.BUSY, TemplateKind.COPY)
    (busy,) = profile.variants(TemplateKind.BUSY)
    assert (busy.width, busy.height) == (20, 16)
    assert known_keys(tmp_path) == ("chatgpt",)


def test_saving_into_a_format_1_profile_migrates_it_and_keeps_what_was_there(
    tmp_path: Path,
) -> None:
    """A v1 table normalises into a v2 one without losing anything, so the
    first capture after an upgrade is an addition rather than a reset."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=1))
    downgrade_to_v1(tmp_path / "chatgpt")
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(30, 12, shade=2))
    raw = json.loads((tmp_path / "chatgpt" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert raw["version"] == FORMAT_VERSION
    assert [entry["file"] for entry in raw["templates"]["busy"]] == ["busy.png", "busy-2.png"]
    assert [
        (t.width, t.height) for t in load_profile(tmp_path, "chatgpt").variants(TemplateKind.BUSY)
    ] == [(20, 16), (30, 12)]


def test_clearing_a_format_1_kind_removes_its_file(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    downgrade_to_v1(tmp_path / "chatgpt")
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    assert not (tmp_path / "chatgpt" / "busy.png").exists()
    assert load_profile(tmp_path, "chatgpt").captured == ()


def test_a_kind_whose_entry_is_not_a_list_is_ignored(tmp_path: Path) -> None:
    """Format 2 says list; a hand-edited manifest saying otherwise loses that
    kind, not the profile."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    manifest = tmp_path / "chatgpt" / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["templates"]["busy"] = {"file": "busy.png", "width": 20, "height": 16}
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


def test_a_manifest_may_not_delete_outside_the_profile_folder(tmp_path: Path) -> None:
    """drop_template unlinks what the manifest names, so it validates the name
    exactly as loading does - a manifest is a file anything can write."""
    outsider = tmp_path / "secret.png"
    outsider.write_bytes(b"mine")
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    manifest = tmp_path / "chatgpt" / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["templates"]["busy"][0]["file"] = "../secret.png"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    assert outsider.exists()
    assert load_profile(tmp_path, "chatgpt").captured == ()


def test_profiles_of_different_services_do_not_mix(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=1))
    save_template(tmp_path, "claude-ai", TemplateKind.COPY, patch(shade=2))
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.BUSY,)
    assert load_profile(tmp_path, "claude-ai").captured == (TemplateKind.COPY,)


def test_the_manifest_describes_what_it_stored(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    raw = json.loads((tmp_path / "chatgpt" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert raw["version"] == FORMAT_VERSION
    assert raw["service"] == "chatgpt"
    (entry,) = raw["templates"]["busy"]
    assert entry["file"] == "busy.png"
    assert (entry["width"], entry["height"]) == (20, 16)
    assert entry["captured_at"].endswith("+00:00")
    assert (tmp_path / "chatgpt" / "busy.png").exists()


def test_saving_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.IDLE, patch())
    assert list((tmp_path / "chatgpt").glob("*.tmp")) == []
    assert sorted(p.name for p in (tmp_path / "chatgpt").iterdir()) == [
        "busy.png",
        "idle.png",
        MANIFEST_NAME,
    ]


def test_loading_a_service_that_was_never_captured_is_empty(tmp_path: Path) -> None:
    assert load_profile(tmp_path / "nothing-here", "chatgpt").captured == ()
    assert load_profile(tmp_path, "chatgpt").describe() == "0/7 captured"


def test_one_corrupt_png_costs_only_its_own_appearance(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    (tmp_path / "chatgpt" / "busy.png").write_bytes(b"not a png at all")
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


def test_a_manifest_naming_a_missing_file_is_survivable(tmp_path: Path) -> None:
    """A half-written profile must not take the readable half down with it."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    (tmp_path / "chatgpt" / "copy.png").unlink()
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.BUSY,)


def test_a_manifest_from_another_version_loads_as_empty(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    rewrite_version(tmp_path / "chatgpt", FORMAT_VERSION + 1)
    assert load_profile(tmp_path, "chatgpt").captured == ()


def test_a_json_true_version_is_not_version_one(tmp_path: Path) -> None:
    """``True == 1`` in Python, and a manifest is a file anything can write."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    rewrite_version(tmp_path / "chatgpt", True)
    assert load_profile(tmp_path, "chatgpt").captured == ()


def test_saving_into_a_profile_from_another_version_changes_nothing(tmp_path: Path) -> None:
    """Rewriting a manifest this build cannot read would unlist every
    appearance in it and leave the PNGs behind as litter - so it refuses, and
    leaves the folder byte for byte as it found it."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch(24, 24))
    directory = tmp_path / "chatgpt"
    rewrite_version(directory, 99)
    before = snapshot(directory)
    with pytest.raises(ProfileStoreError):
        save_template(tmp_path, "chatgpt", TemplateKind.IDLE, patch(shade=5))
    with pytest.raises(ProfileStoreError):
        save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch(shade=7))
    assert snapshot(directory) == before


def test_dropping_from_a_profile_from_another_version_changes_nothing(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    directory = tmp_path / "chatgpt"
    rewrite_version(directory, 99)
    before = snapshot(directory)
    with pytest.raises(ProfileStoreError):
        drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    assert snapshot(directory) == before


def test_a_corrupt_manifest_loads_as_empty(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    (tmp_path / "chatgpt" / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert load_profile(tmp_path, "chatgpt").captured == ()


def test_unknown_manifest_entries_and_stray_files_are_ignored(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    manifest = tmp_path / "chatgpt" / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["templates"]["from-the-future"] = [{"file": "future.png", "width": 1, "height": 1}]
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "chatgpt" / "notes.txt").write_text("mine now", encoding="utf-8")
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.BUSY,)


def test_a_manifest_may_not_point_outside_the_profile_folder(tmp_path: Path) -> None:
    """The manifest is just a file on disk; it must not be able to name one
    anywhere else."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    manifest = tmp_path / "chatgpt" / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["templates"]["busy"][0]["file"] = "../../secret.png"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert load_profile(tmp_path, "chatgpt").captured == ()


@pytest.mark.parametrize("filename", ["..", ".", "", "sub/busy.png"])
def test_a_manifest_may_not_name_a_directory_or_a_path(tmp_path: Path, filename: str) -> None:
    """``Path("..").name`` is ``".."``, so the bare-filename check alone lets a
    directory through - and reading one is an OSError, not a template."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    manifest = tmp_path / "chatgpt" / MANIFEST_NAME
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["templates"]["busy"][0]["file"] = filename
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert load_profile(tmp_path, "chatgpt").captured == ()


def test_a_bomb_shaped_png_costs_only_its_own_appearance(tmp_path: Path) -> None:
    """Loading is total even against a file built to break the decoder."""
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    bomb = zlib.compress(b"\x00" * (16 << 20))
    header = struct.pack(">IIBBBBB", 30_000, 30_000, 8, 6, 0, 0, 0)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    (tmp_path / "chatgpt" / "busy.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", bomb) + chunk(b"IEND", b"")
    )
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


def test_a_failed_write_reports_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """The other half of "writing must report failure": the half-written temp
    file goes with the error, or the next save inherits a folder full of them."""
    real_replace = os.replace

    def fail_on_the_png(src: object, dst: object, **kwargs: object) -> None:
        if str(dst).endswith(".png"):
            raise OSError("no space left on device")
        real_replace(src, dst, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(profile_store.os, "replace", fail_on_the_png)
        with pytest.raises(ProfileStoreError):
            save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    assert [entry.name for entry in (tmp_path / "chatgpt").iterdir()] == []


def test_an_invalid_service_key_never_reaches_the_filesystem(tmp_path: Path) -> None:
    for key in BAD_KEYS:
        with pytest.raises(ProfileStoreError):
            profile_dir(tmp_path, key)
        with pytest.raises(ProfileStoreError):
            save_template(tmp_path, key, TemplateKind.BUSY, patch())
        with pytest.raises(ProfileStoreError):
            delete_profile(tmp_path, key)
        assert load_profile(tmp_path, key).captured == ()  # loading stays total
    assert list(tmp_path.iterdir()) == []


def test_profile_dir_accepts_a_slug_key(tmp_path: Path) -> None:
    assert profile_dir(tmp_path, "copilot-work") == tmp_path / "copilot-work"


def test_drop_template_unlists_the_kind_and_deletes_its_file(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    raw = json.loads((tmp_path / "chatgpt" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(raw["templates"]) == {"copy"}
    assert not (tmp_path / "chatgpt" / "busy.png").exists()
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


def test_dropping_what_was_never_captured_is_not_an_error(tmp_path: Path) -> None:
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    save_template(tmp_path, "chatgpt", TemplateKind.COPY, patch())
    drop_template(tmp_path, "chatgpt", TemplateKind.BUSY)
    assert load_profile(tmp_path, "chatgpt").captured == (TemplateKind.COPY,)


def test_delete_profile_removes_the_whole_folder(tmp_path: Path) -> None:
    save_template(tmp_path, "chatgpt", TemplateKind.BUSY, patch())
    save_template(tmp_path, "claude-ai", TemplateKind.BUSY, patch())
    delete_profile(tmp_path, "chatgpt")
    assert not (tmp_path / "chatgpt").exists()
    assert (tmp_path / "claude-ai").exists()
    delete_profile(tmp_path, "chatgpt")  # deleting again is a no-op


def test_known_keys_lists_readable_profiles_sorted(tmp_path: Path) -> None:
    for key in ("gemini", "chatgpt", "claude-ai"):
        save_template(tmp_path, key, TemplateKind.BUSY, patch())
    (tmp_path / "not-a-profile").mkdir()  # no manifest
    (tmp_path / "loose.png").write_bytes(b"")
    assert known_keys(tmp_path) == ("chatgpt", "claude-ai", "gemini")


def test_known_keys_of_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert known_keys(tmp_path / "never-created") == ()
