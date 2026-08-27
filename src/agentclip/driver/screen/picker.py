"""Spawn a fullscreen overlay in a child process and read its answer back.

The Textual TUI cannot host tkinter (both want an event loop; tkinter also
demands the main thread), so every overlay re-invokes THIS program with a hidden
flag. Works identically frozen (PyInstaller: ``sys.executable`` is whichever
exe asked - the app's or the monitor's) and from source, where the child is
``python -m agentclip.driver.monitor`` (see :data:`CHILD_MODULE`).

Two children, same shape - arguments in, one line of result out:

* ``--pick-region`` (:func:`pick_region`) runs the draw-a-box overlay and prints
  the region wire format on stdout; a cancelled overlay prints nothing, exits 0,
  and parses back as None.
* ``--show-identify`` (:func:`draw_identify_overlay`) is fed the element list as
  JSON on stdin, draws it, and answers nothing at all - it is a read-only
  picture, so the only outcomes are "it was shown" and an error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence

from agentclip.driver.screen.identify import IdentifiedElement, format_payload
from agentclip.driver.screen.region import ScreenRegion, parse_region

# The identify overlay has NO timeout of its own (screen.overlay): it is a
# picture the user asked for and it stays up until they dismiss it, which can be
# minutes. So this parent has no clock either - a cap here would kill the child
# out from under someone still reading it, which is the failure that matters
# more than a child hung before drawing anything.


class ScreenPickError(Exception):
    """The picker child could not run or died - distinct from a user cancel."""


#: The module the from-source child runs. The MONITOR's, not the app's: since
#: docs/design/ui-monitor.md §10.1 the Chat UI draws no overlay and ``cli.py``
#: no longer answers these flags at all, so a checkout's ``-m agentclip`` would
#: be an "unrecognized arguments" error. This module IS the one both frozen
#: binaries reach through ``add_overlay_flags`` / :func:`overlay_child`, and
#: ``agentclip.driver.monitor`` is its entry point one layer up.
CHILD_MODULE = "agentclip.driver.monitor"


def _child_argv(flag: str) -> list[str]:
    """This program again, with one hidden overlay flag."""
    if getattr(sys, "frozen", False):
        return [sys.executable, flag]
    return [sys.executable, "-m", CHILD_MODULE, flag]


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


# == the child side ============================================================
# Both overlays run in a CHILD of whichever program asked (tkinter cannot share
# a webview's process), and the child is ``sys.executable`` again - which in a
# frozen build is the Chat UI's exe OR the Monitor's, depending on who is
# capturing. So both binaries must answer the same two hidden flags, and the
# only place both may import is here, one layer under either entry point
# (tests/test_layering.py: driver/monitor may reach driver/screen; cli.py it
# may not). The parsers add the flags with :func:`add_overlay_flags` and hand
# the namespace to :func:`overlay_child` before choosing any other door.


def add_overlay_flags(parser: argparse.ArgumentParser) -> None:
    """The two hidden re-invocation flags, spelled once for every entry point."""
    parser.add_argument("--pick-region", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pick-prompt", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--show-identify", action="store_true", help=argparse.SUPPRESS)


def overlay_child(args: argparse.Namespace) -> int | None:
    """Run the overlay this invocation asked for; ``None`` when it asked for none."""
    if getattr(args, "pick_region", False):
        return pick_region_child(args.pick_prompt)
    if getattr(args, "show_identify", False):
        return show_identify_child(sys.stdin.read())
    return None


def pick_region_child(prompt: str | None = None) -> int:
    """The --pick-region child: overlay up, region wire format out, exit.

    Cancel is exit 0 with no output (the parent's parse_region yields None);
    a broken environment (no tkinter, no display) is exit 1 with the reason on
    stderr, which :func:`pick_region` surfaces as a ScreenPickError.
    """
    from agentclip.driver.screen.region import format_region

    try:
        from agentclip.driver.screen.overlay import run_overlay

        region = run_overlay(prompt)
    except Exception as exc:  # anything here means "picker unavailable"
        print(f"region picker unavailable: {exc}", file=sys.stderr)
        return 1
    if region is not None:
        print(format_region(region))
    return 0


def show_identify_child(payload: str) -> int:
    """The --show-identify child: boxes up, wait for a dismissal, exit.

    Prints nothing on success - the overlay IS the result. A malformed payload
    or a broken environment (no tkinter, no display) is exit 1 with the reason
    on stderr, which :func:`draw_identify_overlay` surfaces as a
    ScreenPickError; the parent toasts that instead of leaving the user staring
    at a screen where nothing happened.
    """
    from agentclip.driver.screen.identify import parse_payload

    try:
        elements = parse_payload(payload)
    except ValueError as exc:
        print(f"identify overlay got a bad payload: {exc}", file=sys.stderr)
        return 1
    try:
        from agentclip.driver.screen.overlay import run_identify_overlay

        run_identify_overlay(elements)
    except Exception as exc:  # anything here means "overlay unavailable"
        print(f"identify overlay unavailable: {exc}", file=sys.stderr)
        return 1
    return 0
