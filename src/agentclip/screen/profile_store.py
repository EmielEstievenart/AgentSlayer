"""Service profiles on disk: one folder per service, one PNG per appearance.

    <root>/chatgpt/profile.json
    <root>/chatgpt/busy.png
    <root>/chatgpt/copy.png

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
harmless, ignored on load - but never a manifest naming a file that isn't there.
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

from agentclip.screen.capture import RegionImage
from agentclip.screen.png import PngError, decode_png, encode_png
from agentclip.screen.profile import ServiceProfile, TemplateKind

FORMAT_VERSION = 1
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


def _is_known_version(document: dict) -> bool:
    """Is this manifest's ``version`` exactly the one this build writes?

    Spelled out rather than ``== FORMAT_VERSION`` because ``True == 1`` in
    Python, and a manifest is a file anything can write: a JSON ``true`` must
    not pass for format 1 and take a profile's whole template table with it.
    """
    version = document.get("version")
    return type(version) is int and version == FORMAT_VERSION


def _entries(document: dict) -> dict[str, dict] | None:
    """The manifest's ``templates`` table, or None if it hasn't got one."""
    templates = document.get("templates")
    if not isinstance(templates, dict):
        return None
    return {name: entry for name, entry in templates.items() if isinstance(entry, dict)}


def _read_manifest(directory: Path) -> dict[str, dict] | None:
    """The manifest's ``templates`` table, or None if it is unusable."""
    document = _read_document(directory)
    if document is None or not _is_known_version(document):
        return None  # a future (or garbage) format: nothing here is trustworthy
    return _entries(document)


def _templates_to_rewrite(directory: Path) -> dict[str, dict]:
    """The table a write is about to replace - refusing to replace the wrong one.

    Reading is total (an unreadable manifest just means "nothing captured"), but
    writing over one is not. A manifest that announces a version this build does
    not know describes files it does not understand: rewriting it as version 1
    would silently orphan every appearance it lists, which is exactly the
    profile the user would then be asked to recapture. Refuse instead.
    """
    document = _read_document(directory)
    if document is None:
        return {}
    if not _is_known_version(document):
        if "version" in document:
            raise ProfileStoreError(
                f"{directory / MANIFEST_NAME} was written by another version of AgentClip"
            )
        return {}  # no version at all: not a manifest, just a file in the way
    return _entries(document) or {}


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


def _write_manifest(directory: Path, key: str, templates: dict[str, dict]) -> None:
    document = {"version": FORMAT_VERSION, "service": key, "templates": templates}
    _write_atomically(
        directory / MANIFEST_NAME,
        json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def _png_name(kind: TemplateKind) -> str:
    return f"{kind.value}.png"


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

    for name, entry in templates.items():
        try:
            kind = TemplateKind(name)
        except ValueError:
            continue  # an appearance this version doesn't know about
        filename = entry.get("file")
        # Only ever a bare filename inside the profile folder: a manifest is a
        # file on disk like any other, and must not be able to name /etc/shadow.
        # The directory names are spelled out because pathlib does not treat
        # them as anything special - ``Path("..").name`` is ``".."``.
        if not isinstance(filename, str) or filename in ("", ".", ".."):
            continue
        if Path(filename).name != filename:
            continue
        try:
            image = decode_png((directory / filename).read_bytes())
            profile.put(kind, image)
        except (OSError, PngError, ValueError):
            continue  # this one appearance is lost; the others still load
    return profile


def save_template(root: Path, key: str, kind: TemplateKind, image: RegionImage) -> None:
    """Persist one captured appearance (PNG first, then the manifest).

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
        _write_atomically(directory / _png_name(kind), data)
        templates[kind.value] = {
            "file": _png_name(kind),
            "width": image.width,
            "height": image.height,
            "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        _write_manifest(directory, key, templates)
    except OSError as exc:
        raise ProfileStoreError(f"could not write the profile for {key!r}: {exc}") from exc


def drop_template(root: Path, key: str, kind: TemplateKind) -> None:
    """Forget one appearance: unlist it, then delete its file.

    The reverse order of :func:`save_template`, for the same reason - the
    manifest must never name a file that is gone.
    """
    directory = profile_dir(root, key)
    templates = _templates_to_rewrite(directory)
    if kind.value in templates:
        del templates[kind.value]
        try:
            _write_manifest(directory, key, templates)
        except OSError as exc:
            raise ProfileStoreError(f"could not update the profile for {key!r}: {exc}") from exc
    try:
        (directory / _png_name(kind)).unlink(missing_ok=True)
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
