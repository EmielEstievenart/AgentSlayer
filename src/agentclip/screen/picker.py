"""Spawn a fullscreen overlay in a child process and read its answer back.

The Textual TUI cannot host tkinter (both want an event loop; tkinter also
demands the main thread), so every overlay re-invokes THIS program with a hidden
flag. Works identically frozen (PyInstaller: ``sys.executable`` is
agentclip.exe) and from source (``python -m agentclip``).

Two children, same shape - arguments in, one line of result out:

* ``--pick-region`` (:func:`pick_region`) runs the draw-a-box overlay and prints
  the region wire format on stdout; a cancelled overlay prints nothing, exits 0,
  and parses back as None.
* ``--show-identify`` (:func:`draw_identify_overlay`) is fed the element list as
  JSON on stdin, draws it, and answers nothing at all - it is a read-only
  picture, so the only outcomes are "it was shown" and an error.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

from agentclip.screen.identify import IdentifiedElement, format_payload
from agentclip.screen.region import ScreenRegion, parse_region

# The identify overlay has NO timeout of its own (screen.overlay): it is a
# picture the user asked for and it stays up until they dismiss it, which can be
# minutes. So this parent has no clock either - a cap here would kill the child
# out from under someone still reading it, which is the failure that matters
# more than a child hung before drawing anything.


class ScreenPickError(Exception):
    """The picker child could not run or died - distinct from a user cancel."""


def _child_argv(flag: str) -> list[str]:
    """This program again, with one hidden overlay flag."""
    if getattr(sys, "frozen", False):
        return [sys.executable, flag]
    return [sys.executable, "-m", "agentclip", flag]


def _command(prompt: str | None) -> list[str]:
    argv = _child_argv("--pick-region")
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
        raise ScreenPickError(_detail(proc.stderr, proc.returncode, "picker"))
    return parse_region(proc.stdout)


def draw_identify_overlay(elements: Sequence[IdentifiedElement]) -> None:
    """Show `/identify`'s boxes on the real screen; block until the user dismisses them.

    ``elements`` travels as JSON on the child's stdin rather than argv: a whole
    profile's worth of rectangles is well past what a command line should carry,
    and the picker child has already established stdout as the direction results
    come back in.

    Blocks with no deadline, for as long as the user leaves the overlay up - call
    from a worker thread, and suspend the finish detectors around it (a
    fullscreen window appearing over the browser they watch is exactly the
    sustained delta that arms them).
    """
    try:
        proc = subprocess.run(
            _child_argv("--show-identify"),
            input=format_payload(elements),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ScreenPickError(f"could not launch the identify overlay: {exc}") from exc
    if proc.returncode != 0:
        raise ScreenPickError(_detail(proc.stderr, proc.returncode, "identify overlay"))


def _detail(stderr: str | None, returncode: int, what: str) -> str:
    """The child's last stderr line, or a bare exit code when it said nothing."""
    lines = (stderr or "").strip().splitlines()
    return lines[-1] if lines else f"{what} exited with code {returncode}"
