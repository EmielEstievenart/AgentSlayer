"""Service profiles on disk: one folder per service, one PNG per captured image.

    <root>/chatgpt/profile.json
    <root>/chatgpt/busy.png
    <root>/chatgpt/copy.png
    <root>/chatgpt/send-ready.png
    <root>/chatgpt/send-ready-2.png     # the same button, greyed out

Deliberately not one opaque blob. A profile is the only part of AgentClip's
state a user cannot re-derive by reading a config file - if the copy button
stops being found, the fix starts with *looking at* what was captured, and a
folder of PNGs makes that a double-click. It also makes a broken template a
local failure: one unreadable file costs one appearance, not the profile.

Nothing here resolves a location. ``root`` is a parameter on every function
(config.default_profile_dir supplies the real one) so tests, and any future
per-project profile set, point it wherever they like - and so this module stays
inside the screen layer's stdlib-only import budget.

Loading NEVER raises. A profile is read at startup on a path where the only
useful reaction to corruption is to ask the user to recapture, so every failure
mode - missing folder, unknown format version, truncated JSON, a PNG someone
overwrote with a text file - degrades to "that appearance is not captured".
Writing is the opposite: an explicit user action that must report failure -
including its refusal to touch a manifest written by a version this build does
not know, where carrying on would orphan every appearance the manifest lists.

Writes are atomic and ordered PNGs-first, manifest-last (config.save_services'
mkstemp/os.replace dance), so a crash mid-save can leave an unreferenced file -
harmless, ignored on load, and its name reusable by the next save - but never a
manifest naming a file that isn't there.

Format 2 is what a kind holding a STACK of images (screen.profile) looks like
on disk: the manifest's ``templates`` table maps a kind to a LIST of entries
rather than to one. Format 1 - one entry per kind - is still read, as a
one-variant stack, so a profile captured before variants keeps working and
migrates the first time it is written to; only format 2 is ever written.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.png import PngError, decode_png, encode_png
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind

FORMAT_VERSION = 2
# Every manifest shape this build understands well enough to read AND to
# rewrite. Reading an older one is the whole point; rewriting it is safe for
# the same reason - a v1 table is a v2 table with one entry per list.
READABLE_VERSIONS = (1, FORMAT_VERSION)
MANIFEST_NAME = "profile.json"
# Service keys become directory names, so they are checked before they ever
# touch the filesystem: lowercase slug only - no separators, no traversal, no
# case-folding surprises between Windows and everything else.
_KEY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class ProfileStoreError(Exception):
    """A profile could not be written (bad service key, or an I/O failure)."""


def profile_dir(root: Path, key: str) -> Path:
    """The folder holding ``key``'s profile. Raises on a key we won't create."""
    if not _KEY_RE.fullmatch(key):
        raise ProfileStoreError(f"invalid service key {key!r}")
    return root / key


def _read_document(directory: Path) -> dict | None:
    """The manifest as a JSON object, or None if there isn't a readable one."""
    try:
        raw = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _known_version(document: dict) -> int | None:
    """This manifest's format, or None if it is not one we read.

    ``type(version) is int`` rather than a plain ``in`` test because ``True ==
    1`` in Python, and a manifest is a file anything can write: a JSON ``true``
    must not pass for format 1 and take a profile's whole template table with
    it.
    """
    version = document.get("version")
    if type(version) is not int or version not in READABLE_VERSIONS:
        return None
    return version


def _entries(document: dict, version: int) -> dict[str, list[dict]] | None:
    """The manifest's ``templates`` table as stacks, or None if it hasn't got one.

    The one place the two formats differ, so everything downstream only ever
    sees a list per kind: a v1 entry is that kind's single variant.
    """
    templates = document.get("templates")
    if not isinstance(templates, dict):
        return None
    if version == 1:
        return {name: [entry] for name, entry in templates.items() if isinstance(entry, dict)}
    stacks = {
        name: [entry for entry in entries if isinstance(entry, dict)]
        for name, entries in templates.items()
        if isinstance(entries, list)
    }
    return {name: entries for name, entries in stacks.items() if entries}


def _read_manifest(directory: Path) -> dict[str, list[dict]] | None:
    """The manifest's ``templates`` table, or None if it is unusable."""
    document = _read_document(directory)
    if document is None:
        return None
    version = _known_version(document)
    if version is None:
        return None  # a future (or garbage) format: nothing here is trustworthy
    return _entries(document, version)


def _templates_to_rewrite(directory: Path) -> dict[str, list[dict]]:
    """The table a write is about to replace - refusing to replace the wrong one.

    Reading is total (an unreadable manifest just means "nothing captured"), but
    writing over one is not. A manifest that announces a version this build does
    not know describes files it does not understand: rewriting it as format 2
    would silently orphan every appearance it lists, which is exactly the
    profile the user would then be asked to recapture. Refuse instead.

    A format we DO know is fair game whether or not it is the one we write: a
    v1 table normalises into a v2 one without losing anything, so the first
    save into an old profile quietly migrates it.
    """
    document = _read_document(directory)
    if document is None:
        return {}
    version = _known_version(document)
    if version is None:
        if "version" in document:
            raise ProfileStoreError(
                f"{directory / MANIFEST_NAME} was written by another version of AgentClip"
            )
        return {}  # no version at all: not a manifest, just a file in the way
    return _entries(document, version) or {}


def _write_atomically(target: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            # On disk, not merely in the page cache, before the rename makes it
            # the real file: a crash in between is how an atomic replace still
            # leaves a zero-length profile behind.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise


def _write_manifest(directory: Path, key: str, templates: dict[str, list[dict]]) -> None:
    document = {"version": FORMAT_VERSION, "service": key, "templates": templates}
    _write_atomically(
        directory / MANIFEST_NAME,
        json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def _png_name(kind: TemplateKind, index: int) -> str:
    """Where a kind's ``index``-th variant lives (1-based).

    The first one keeps the bare ``<kind>.png`` a format-1 profile used, so an
    old folder needs no renaming and a folder of one-image kinds still reads
    like the filenames somebody would have chosen by hand.
    """
    return f"{kind.value}.png" if index <= 1 else f"{kind.value}-{index}.png"


def _next_png_name(kind: TemplateKind, taken: set[object]) -> str:
    """The lowest-numbered file ``kind`` does not already list.

    Numbered off the MANIFEST rather than off the directory, so a PNG orphaned
    by a crash between the two writes is simply overwritten - it is referenced
    by nothing, and leaving gaps in the numbering to preserve it would be
    preserving litter.
    """
    index = 1
    while _png_name(kind, index) in taken:
        index += 1
    return _png_name(kind, index)


def _safe_name(filename: object) -> str | None:
    """A manifest's ``file`` field as a bare name inside the profile folder.

    None for anything else. A manifest is a file on disk like any other and
    must not be able to name /etc/shadow - neither to be read from nor, in
    :func:`drop_template`, to be deleted. The directory names are spelled out
    because pathlib does not treat them as anything special - ``Path("..").name``
    is ``".."``.
    """
    if not isinstance(filename, str) or filename in ("", ".", ".."):
        return None
    if Path(filename).name != filename:
        return None
    return filename


def load_profile(root: Path, key: str) -> ServiceProfile:
    """``key``'s profile, or an empty one. Never raises - see the module docstring."""
    profile = ServiceProfile(key)
    try:
        directory = profile_dir(root, key)
    except ProfileStoreError:
        return profile
    templates = _read_manifest(directory)
    if not templates:
        return profile

    for name, entries in templates.items():
        try:
            kind = TemplateKind(name)
        except ValueError:
            continue  # an appearance this version doesn't know about
        for entry in entries:
            filename = _safe_name(entry.get("file"))
            if filename is None:
                continue
            try:
                image = decode_png((directory / filename).read_bytes())
                profile.put(kind, image)
            except (OSError, PngError, ValueError):
                continue  # this one image is lost; the rest of the stack loads
    return profile


def save_template(root: Path, key: str, kind: TemplateKind, image: RegionImage) -> None:
    """ADD one captured appearance to ``kind`` (PNG first, then the manifest).

    Added, not replaced: a kind is a stack (screen.profile), and a second
    capture is the user teaching it a second way the same control is drawn.
    "Clear" (:func:`drop_template`) is how a stack is emptied.

    The manifest is *read* before anything is written, so a profile this build
    must not touch is left exactly as it was found - PNGs included.
    """
    directory = profile_dir(root, key)
    try:
        data = encode_png(image)
    except PngError as exc:
        raise ProfileStoreError(f"cannot store the {kind.label}: {exc}") from exc
    try:
        directory.mkdir(parents=True, exist_ok=True)
        templates = _templates_to_rewrite(directory)
        entries = templates.get(kind.value, [])
        filename = _next_png_name(kind, {entry.get("file") for entry in entries})
        _write_atomically(directory / filename, data)
        templates[kind.value] = [
            *entries,
            {
                "file": filename,
                "width": image.width,
                "height": image.height,
                "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            },
        ]
        _write_manifest(directory, key, templates)
    except OSError as exc:
        raise ProfileStoreError(f"could not write the profile for {key!r}: {exc}") from exc


def drop_template(root: Path, key: str, kind: TemplateKind) -> None:
    """Forget one appearance ENTIRELY: unlist the stack, then delete its files.

    The reverse order of :func:`save_template`, for the same reason - the
    manifest must never name a file that is gone.

    Every image, because that is the question the editor's per-kind "Clear"
    asks: half a stack is a kind that still matches, which is indistinguishable
    from the clear having done nothing.
    """
    directory = profile_dir(root, key)
    templates = _templates_to_rewrite(directory)
    entries = templates.pop(kind.value, [])
    if entries:
        try:
            _write_manifest(directory, key, templates)
        except OSError as exc:
            raise ProfileStoreError(f"could not update the profile for {key!r}: {exc}") from exc
    for entry in entries:
        filename = _safe_name(entry.get("file"))
        if filename is None:
            continue
        try:
            (directory / filename).unlink(missing_ok=True)
        except OSError as exc:
            raise ProfileStoreError(f"could not remove the {kind.label}: {exc}") from exc


def drop_variant(root: Path, key: str, kind: TemplateKind, index: int) -> None:
    """Forget ONE image of ``kind`` - the one a row is currently showing.

    :func:`drop_template`'s ordering and posture over a single entry: unlist it
    first, unlink it after, so a crash in between leaves an unreferenced PNG
    (harmless, ignored on load, its name reusable) rather than a manifest naming
    a file that is gone. The stack closes up behind it - capture order is the
    only order a variant has - and a kind whose last image goes is unlisted
    entirely, because an empty stack and an uncaptured kind are the same thing.

    An index naming nothing - a row read before another press moved the stack, a
    kind with nothing in it - is a no-op rather than an error. The caller
    re-reads the folder after every one of these anyway, so all an exception
    could add is a complaint about a picture that was already not there.
    """
    directory = profile_dir(root, key)
    templates = _templates_to_rewrite(directory)
    entries = templates.get(kind.value, [])
    if not 0 <= index < len(entries):
        return
    entry = entries.pop(index)
    if entries:
        templates[kind.value] = entries
    else:
        templates.pop(kind.value, None)
    try:
        _write_manifest(directory, key, templates)
    except OSError as exc:
        raise ProfileStoreError(f"could not update the profile for {key!r}: {exc}") from exc
    filename = _safe_name(entry.get("file"))
    if filename is None:
        return
    try:
        (directory / filename).unlink(missing_ok=True)
    except OSError as exc:
        raise ProfileStoreError(f"could not remove the {kind.label}: {exc}") from exc


def delete_profile(root: Path, key: str) -> None:
    """Remove a service's whole profile folder.

    The folder is ours end to end - we create it and nothing else writes there -
    so it goes as a unit rather than file by file, which would otherwise leave
    behind whatever a partial write or a hand-dropped file put in it.
    """
    directory = profile_dir(root, key)
    shutil.rmtree(directory, ignore_errors=True)


def known_keys(root: Path) -> tuple[str, ...]:
    """Service keys with a readable profile on disk, sorted."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    return tuple(
        entry.name
        for entry in entries
        if entry.is_dir() and _KEY_RE.fullmatch(entry.name) and _read_manifest(entry) is not None
    )
