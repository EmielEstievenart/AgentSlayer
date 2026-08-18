"""The engine half as a named executable: ``agentclip-engine``.

Deployment (docs/design/remote-executor.md section 2.6) is "the user installs
this package on the target, the master launches ``agentclip-engine`` by name
over an exec channel". That makes the console script a *contract*, not a
convenience: the string in ``[project.scripts]``, the callable it names, and the
exit code that callable returns are all part of how a remote session starts. So
this file pins them from both sides - the target the entry point resolves to,
and, when the dev venv actually has the script installed, the executable itself.

The one hard rule underneath: **stdout is the wire**. A session's stdout carries
frames and nothing else, so a mis-invocation must not print to it. ``--help`` is
the deliberate exception - it is not a session, and argparse's help legitimately
goes to stdout - while a missing ``--project`` must leave stdout empty and say so
on stderr.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agentclip.engine.link.__main__ import main


def test_entry_point_target_is_callable() -> None:
    """``agentclip.engine.link.__main__:main`` - the string pyproject names."""
    assert callable(main)


def test_help_exits_zero_under_the_executable_name(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse ends the process on --help, so SystemExit IS the success here.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr()
    # The name a target's user typed, not the module file behind it.
    assert out.out.startswith("usage: agentclip-engine")
    assert "--project" in out.out


def test_missing_project_exits_nonzero_with_a_clean_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
    out = capsys.readouterr()
    # Nothing on stdout: on a real target that stream is the protocol.
    assert out.out == ""
    assert "--project" in out.err


def test_console_script_runs_when_installed() -> None:
    """The real executable, if this venv has it.

    Probed rather than assumed: a checkout that has not been ``uv sync``ed since
    the script was added has no such command, and a test that fails there would
    be reporting on the environment rather than on the code. The resolved path
    is run directly instead of through ``uv run`` so the subprocess cannot
    re-resolve the environment out from under the suite.
    """
    script = shutil.which("agentclip-engine")
    if script is None:
        pytest.skip("agentclip-engine is not installed in this environment (run `uv sync`)")
    done = subprocess.run([script, "--help"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0
    assert done.stdout.startswith("usage: agentclip-engine")
