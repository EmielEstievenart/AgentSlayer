"""The overlay children: their command lines, and what they do with their input.

Two hidden flags, one shape - ``--pick-region`` (drag a box, print the region)
and ``--show-identify`` (read a rectangle list off stdin, draw it). Neither
overlay is ever run here: the drawing is stubbed at its own use site, on top of
the suite-wide block in tests/conftest.py.
"""

from __future__ import annotations

import pytest

from agentclip.cli import _show_identify_child
from agentclip.screen import picker
from agentclip.screen.identify import IdentifiedElement, format_payload
from agentclip.screen.region import ScreenRegion

# Bound at import, before the suite-wide OS gate swaps the module attribute for
# its loud stub (tests/conftest.py): these tests drive the REAL parent function
# with ``subprocess.run`` itself stubbed, so no child is ever spawned.
REAL_DRAW_IDENTIFY = picker.draw_identify_overlay


def test_command_without_a_prompt_is_unchanged() -> None:
    argv = picker._command(None)
    assert argv[-1] == "--pick-region"
    assert "--pick-prompt" not in argv


def test_command_passes_the_prompt_through() -> None:
    argv = picker._command("Draw around the stop button")
    assert argv[-2:] == ["--pick-prompt", "Draw around the stop button"]
    assert "--pick-region" in argv


def test_frozen_command_invokes_the_exe_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """PyInstaller: sys.executable IS agentclip.exe, so no ``-m agentclip``."""
    monkeypatch.setattr(picker.sys, "frozen", True, raising=False)
    argv = picker._command("hello")
    assert "-m" not in argv
    assert argv[-2:] == ["--pick-prompt", "hello"]


def test_the_identify_child_is_the_same_program_with_its_own_flag() -> None:
    """No arguments beyond the flag: the rectangle list is far too big for a
    command line, so it travels on stdin."""
    argv = picker._child_argv("--show-identify")
    assert argv[-1] == "--show-identify"
    assert argv[:3] == [picker.sys.executable, "-m", "agentclip"]


def test_frozen_identify_child_invokes_the_exe_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(picker.sys, "frozen", True, raising=False)
    assert picker._child_argv("--show-identify") == [picker.sys.executable, "--show-identify"]


# -- the identify child, driven directly (the drawing stubbed) -----------------


def test_the_identify_child_draws_exactly_what_it_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elements = [
        IdentifiedElement("chat region", ScreenRegion(10, 20, 300, 200)),
        IdentifiedElement("copy", ScreenRegion(-40, 60, 24, 24), 0.0125),
    ]
    drawn: list[list[IdentifiedElement]] = []
    monkeypatch.setattr(
        "agentclip.screen.overlay.run_identify_overlay",
        lambda parsed, *args, **kwargs: drawn.append(list(parsed)),
    )
    assert _show_identify_child(format_payload(elements)) == 0
    assert drawn == [elements]


def test_the_identify_child_rejects_a_bad_payload_before_drawing_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 with the reason on stderr, which screen.picker turns into a toast -
    better than a fullscreen window with nothing on it."""
    monkeypatch.setattr(
        "agentclip.screen.overlay.run_identify_overlay",
        lambda *args, **kwargs: pytest.fail("drew a malformed payload"),
    )
    assert _show_identify_child("{not json") == 1
    assert "bad payload" in capsys.readouterr().err


def test_the_identify_child_survives_an_environment_with_no_overlay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No tkinter, no display: exit 1 with the reason, never a traceback."""

    def boom(*args: object, **kwargs: object) -> None:
        raise ImportError("No module named 'tkinter'")

    monkeypatch.setattr("agentclip.screen.overlay.run_identify_overlay", boom)
    assert _show_identify_child(format_payload([])) == 1
    assert "identify overlay unavailable" in capsys.readouterr().err


# -- the identify parent: no clock (the user's own eyes end the overlay) -------


def _run_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Stand in for subprocess.run, recording how the child was invoked."""
    calls: list[dict[str, object]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv: object, **kwargs: object) -> _Proc:
        calls.append({"argv": argv, **kwargs})
        return _Proc()

    monkeypatch.setattr(picker.subprocess, "run", fake_run)
    return calls


def test_the_identify_overlay_child_runs_without_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/identify` draws a picture the user ASKED for, and reading it takes as
    long as it takes. A finite timeout here would kill the overlay out from under
    someone still looking at it - the failure that timing out was meant to avoid
    (a fullscreen window over an empty desk) is the smaller one, and the child no
    longer self-destructs either.
    """
    calls = _run_recorder(monkeypatch)
    elements = [IdentifiedElement("copy", ScreenRegion(10, 20, 24, 24), 0.01)]

    REAL_DRAW_IDENTIFY(elements)

    assert len(calls) == 1
    assert calls[0].get("timeout") is None  # not passed, and not defaulted to one
    assert calls[0]["input"] == format_payload(elements)


def test_a_child_that_dies_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the clock does not mean losing the failure path: an overlay that
    could not be launched, or that exited non-zero, is still a ScreenPickError
    with the child's own last word in it."""

    class _Dead:
        returncode = 1
        stdout = ""
        stderr = "no display available\n"

    monkeypatch.setattr(picker.subprocess, "run", lambda *a, **kw: _Dead())
    with pytest.raises(picker.ScreenPickError, match="no display available"):
        REAL_DRAW_IDENTIFY([])

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("nothing to exec")

    monkeypatch.setattr(picker.subprocess, "run", boom)
    with pytest.raises(picker.ScreenPickError, match="could not launch"):
        REAL_DRAW_IDENTIFY([])
