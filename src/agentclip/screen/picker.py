"""Spawn the overlay in a child process and read the drawn region back.

The Textual TUI cannot host tkinter (both want an event loop; tkinter also
demands the main thread), so the picker re-invokes THIS program with the hidden
``--pick-region`` flag. The child runs only the overlay and prints the region
wire format on stdout; a cancelled overlay prints nothing and exits 0. Works
identically frozen (PyInstaller: ``sys.executable`` is agentclip.exe) and from
source (``python -m agentclip``).
"""

from __future__ import annotations

import subprocess
import sys

from agentclip.screen.region import ScreenRegion, parse_region


class ScreenPickError(Exception):
    """The picker child could not run or died - distinct from a user cancel."""


def _command(prompt: str | None) -> list[str]:
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "--pick-region"]
    else:
        argv = [sys.executable, "-m", "agentclip", "--pick-region"]
    if prompt:
        argv += ["--pick-prompt", prompt]
    return argv


def pick_region(timeout_s: float = 300.0, prompt: str | None = None) -> ScreenRegion | None:
    """Run the draw-a-box overlay; the drawn region, or None if cancelled.

    ``prompt`` overrides the instruction the overlay shows (what to draw around).
    Blocks for as long as the overlay is up - call from a worker thread.
    """
    try:
        proc = subprocess.run(
            _command(prompt), capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        raise ScreenPickError("the region picker timed out - it was closed for you") from None
    except OSError as exc:
        raise ScreenPickError(f"could not launch the region picker: {exc}") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else f"picker exited with code {proc.returncode}"
        raise ScreenPickError(detail)
    return parse_region(proc.stdout)
