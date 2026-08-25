"""Chat regions, remembered on the machine whose screen they are drawn on.

docs/design/ui-monitor.md §8 listed this as open: "a box drawn in the
calibration window reaches a brain through ``on_calibration`` and dies with the
process", and a monitor that outlives every brain (§2.8) is exactly the
deployment where that hurts - the VM reboots, the operator drags the rectangle
again, and nothing on the brain's side could have helped.

**It lives on the monitor**, beside the ``ServiceProfile`` PNGs and for their
reason (§2.10): a rectangle is a fact about THIS machine's desktop - its
monitors, its resolution, where the browser window sits - and a brain that
re-sent one from its own config would push a Windows VM's coordinates onto a
Linux one the first time somebody moved the session. The brain still wins when
it has an opinion: a ``configure`` that CARRIES a region is authoritative and is
what gets saved. The store only answers the spec that carries none.

The file is one JSON object keyed by service key, written whole and replaced
atomically, exactly as ``profile_store`` writes its manifest. Small, rewritten
rarely, and readable by a human debugging a VM over a serial console - which is
worth more here than an incremental format would be.

**A bad file is not a dead monitor.** Every read failure - missing, truncated,
holding something that is not a region - answers ``None``, because the fallback
from "I do not remember where the chat is" is the state the monitor is in on
its first ever run, and that state works.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from agentclip.driver.screen.region import ScreenRegion

_log = logging.getLogger(__name__)

#: The file's name inside the monitor's config directory.
REGIONS_FILE = "regions.json"

#: The document's own version, so a later shape has something to branch on.
REGIONS_VERSION = 1


def regions_path(config_dir: Path) -> Path:
    """Where the regions of ``config_dir`` live - existing or not."""
    return Path(config_dir) / REGIONS_FILE


def load_regions(config_dir: Path) -> dict[str, ScreenRegion]:
    """Every remembered region, by service key. ``{}`` for anything unreadable."""
    path = regions_path(config_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        document = json.loads(raw)
    except ValueError:
        _log.warning("%s is not JSON; ignoring the remembered regions", path)
        return {}
    if not isinstance(document, dict):
        return {}
    entries = document.get("regions")
    if not isinstance(entries, dict):
        return {}
    regions: dict[str, ScreenRegion] = {}
    for service, value in entries.items():
        region = _region(value)
        # One unreadable entry drops one service, not the file: the regions are
        # independent facts and there is no reason a bad "chatgpt" should cost
        # the operator the "claude" box they drew last week.
        if isinstance(service, str) and region is not None:
            regions[service] = region
    return regions


def load_region(config_dir: Path, service: str) -> ScreenRegion | None:
    """The region remembered for ``service``, or ``None``."""
    return load_regions(config_dir).get(service)


def save_region(config_dir: Path, service: str, region: ScreenRegion) -> None:
    """Remember ``region`` as where ``service``'s chat is on this machine.

    Read-modify-write of the whole document: the file holds a handful of
    rectangles and is written when a human drags one, so nothing here is worth
    a merge strategy.
    """
    regions = load_regions(config_dir)
    regions[service] = region
    _write(config_dir, regions)


def drop_region(config_dir: Path, service: str) -> bool:
    """Forget ``service``'s region. True if there was one to forget."""
    regions = load_regions(config_dir)
    if service not in regions:
        return False
    del regions[service]
    _write(config_dir, regions)
    return True


def _region(value: Any) -> ScreenRegion | None:
    if not isinstance(value, dict):
        return None
    try:
        # Signed, like everywhere else on this seam: a chat window on a display
        # left of or above the primary one has negative origins, and these are
        # VIRTUAL-screen coordinates.
        left = int(value["left"])
        top = int(value["top"])
        width = int(value["width"])
        height = int(value["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return ScreenRegion(left, top, width, height)


def _encode(region: ScreenRegion) -> dict[str, int]:
    return {
        "left": region.left,
        "top": region.top,
        "width": region.width,
        "height": region.height,
    }


def _write(config_dir: Path, regions: dict[str, ScreenRegion]) -> None:
    """The whole document, atomically - ``profile_store``'s mkstemp/replace dance.

    Atomic because the calibration window and the poll loop are in one process
    with the operator's hand on the mouse: a save interrupted halfway would
    otherwise leave a truncated JSON file, and the next start would silently
    forget every region rather than the one being changed.
    """
    path = regions_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": REGIONS_VERSION,
        "regions": {service: _encode(region) for service, region in sorted(regions.items())},
    }
    data = json.dumps(document, indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
