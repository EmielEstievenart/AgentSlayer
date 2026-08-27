"""The local monitor is a child process (docs/design/ui-monitor.md §10.1).

Every seam that would need an OS is a constructor argument, so the whole class
is driven here without a process, a socket or a config directory: a fake Popen
records what it was asked to run and answers ``poll``/``wait`` from a script.
The one thing that DOES touch the OS is :func:`free_loopback_port`, which is a
bind on 127.0.0.1 and back - read-only as far as the desktop is concerned.

What is deliberately NOT tested here, because it deliberately does not exist:
readiness. Nothing in this module polls the child - the dial is the readiness
check, and the link reports a child that dies (§10.1).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentclip.config import MONITOR_LOOPBACK, MonitorTarget
from agentclip.shell.app.monitor_launch import (
    LOCAL_MONITOR_EXITED,
    MONITOR_EXECUTABLE,
    MONITOR_MODULE,
    LaunchedMonitor,
    LaunchLocal,
    LocalMonitorLauncher,
    SubprocessLauncher,
    free_loopback_port,
    monitor_command,
    spawn_kwargs,
)

TOKEN = "a" * 32


class FakePopen:
    """The slice of :class:`subprocess.Popen` the launcher uses, scripted.

    ``exits_after`` is how many ``poll`` calls the child survives (None = it
    never exits on its own); ``ignores_terminate`` is the child that has to be
    killed, which is the one branch of ``stop`` a well-behaved fake never
    reaches.
    """

    def __init__(
        self,
        command: list[str],
        *,
        exits_after: int | None = None,
        ignores_terminate: bool = False,
        **kwargs: object,
    ) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242
        self.polls = 0
        self.terminated = 0
        self.killed = 0
        self.waits: list[float | None] = []
        self._exits_after = exits_after
        self._ignores_terminate = ignores_terminate
        self._code: int | None = None

    def poll(self) -> int | None:
        self.polls += 1
        if self._code is None and self._exits_after is not None and self.polls > self._exits_after:
            self._code = 7
        return self._code

    def terminate(self) -> None:
        self.terminated += 1
        if not self._ignores_terminate:
            self._code = -15

    def kill(self) -> None:
        self.killed += 1
        self._code = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self._code is None:
            raise subprocess.TimeoutExpired(self.command, timeout or 0)
        return self._code


def make_launcher(**popen: object) -> tuple[SubprocessLauncher, list[FakePopen]]:
    """A launcher whose spawn records every child, and whose port is fixed."""
    spawned: list[FakePopen] = []

    def spawn(command: list[str], **kwargs: object) -> FakePopen:
        child = FakePopen(command, **popen, **kwargs)  # type: ignore[arg-type]
        spawned.append(child)
        return child

    launcher = SubprocessLauncher(spawn=spawn, pick_port=lambda: 51234, read_token=lambda: TOKEN)
    return launcher, spawned


# -- the sentinel and the protocol ---------------------------------------------


def test_launch_local_is_a_value_with_no_fields_and_compares_equal() -> None:
    """It stands for "start one here" and carries nothing, because the port it
    would carry does not exist until the launch happens."""
    assert LaunchLocal() == LaunchLocal()
    assert LaunchLocal() != None  # noqa: E711 - the OTHER --monitor answer


def test_the_subprocess_launcher_satisfies_the_protocol() -> None:
    """The Chat UI is handed the Protocol, never this class - so a test double
    in §10.2 needs only these four names."""
    launcher, _ = make_launcher()
    assert isinstance(launcher, LocalMonitorLauncher)


# -- the command line ----------------------------------------------------------


def test_the_command_names_the_port_the_loopback_bind_and_the_project() -> None:
    command = monitor_command(7777, Path("/srv/app"))
    assert command[-6:] == [
        "--port",
        "7777",
        "--bind",
        "127.0.0.1",
        "--project",
        str(Path("/srv/app")),
    ]
    # The window is the point of a local monitor (§10.2: calibration lives in
    # the Monitor UI), so the headless door is never taken.
    assert "--headless" not in command


def test_the_global_config_rides_along_only_when_the_launch_had_one() -> None:
    assert "--global-config" not in monitor_command(1, "/p")
    command = monitor_command(1, "/p", global_config_path=Path("/etc/agentclip.toml"))
    assert command[-2:] == ["--global-config", str(Path("/etc/agentclip.toml"))]


def test_the_token_never_reaches_the_command_line() -> None:
    """argv is world-readable on the machines this runs on; the child reads the
    same token FILE the launcher reads."""
    launcher, spawned = make_launcher()
    launcher.start(Path("/srv/app"))
    assert TOKEN not in " ".join(spawned[0].command)
    assert "--token" not in spawned[0].command


def test_a_checkout_runs_the_monitor_ui_module_on_this_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert monitor_command(1, "/p")[:3] == [sys.executable, "-m", MONITOR_MODULE]


def test_a_frozen_build_runs_the_sibling_beside_the_running_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """By absolute path, not by name: a PATH lookup would find whatever
    agentclip-monitor the user happens to have installed, which is the version
    skew the wire handshake exists to refuse."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "agentclip.exe"))
    argv0 = monitor_command(1, "/p")[0]
    suffix = ".exe" if os.name == "nt" else ""
    assert argv0 == str(tmp_path.resolve() / (MONITOR_EXECUTABLE + suffix))
    assert Path(argv0).is_absolute()


def test_only_windows_gets_its_own_process_group() -> None:
    """A console Ctrl+C must not kill the child before ``stop()`` does - and
    nothing more than that is asked for: no detach, no hidden window."""
    kwargs = spawn_kwargs()
    if os.name == "nt":
        assert kwargs == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kwargs == {}


def test_the_spawn_is_given_those_flags() -> None:
    launcher, spawned = make_launcher()
    launcher.start(Path("/srv/app"))
    assert spawned[0].kwargs == spawn_kwargs()


# -- the port ------------------------------------------------------------------


def test_a_free_port_is_in_range_and_is_not_the_same_one_twice() -> None:
    """Bind-then-release: the OS's ephemeral allocator picks, and it does not
    hand the same number straight back."""
    first = free_loopback_port()
    assert 1 <= first <= 65535
    assert free_loopback_port() != first


def test_the_chosen_port_reaches_both_the_command_and_the_target() -> None:
    launcher, spawned = make_launcher()
    launched = launcher.start(Path("/srv/app"))
    assert "51234" in spawned[0].command
    assert launched.target.port == 51234


# -- what a start hands back ---------------------------------------------------


def test_a_launch_is_a_loopback_target_named_local_carrying_the_shared_token() -> None:
    launcher, _ = make_launcher()
    launched = launcher.start(Path("/srv/app"))
    assert launched == LaunchedMonitor(
        target=MonitorTarget(name="local", host=MONITOR_LOOPBACK, port=51234, token=TOKEN),
        process_id=4242,
    )
    assert launcher.launched == launched


def test_a_second_start_replaces_the_first_child_rather_than_orphaning_it() -> None:
    """The Chat UI has one local monitor or none, so a second Launch press must
    not leak a process."""
    launcher, spawned = make_launcher()
    launcher.start(Path("/srv/app"))
    launcher.start(Path("/srv/app"))
    assert len(spawned) == 2
    assert spawned[0].terminated == 1
    assert spawned[1].terminated == 0


# -- alive / exit_code ---------------------------------------------------------


def test_nothing_is_alive_before_a_start_and_there_is_no_exit_code_either() -> None:
    launcher, _ = make_launcher()
    assert launcher.alive() is False
    assert launcher.exit_code() is None


def test_a_running_child_is_alive_and_a_dead_one_reports_its_code() -> None:
    launcher, _ = make_launcher(exits_after=1)
    launcher.start(Path("/srv/app"))
    assert launcher.alive() is True  # first poll
    assert launcher.alive() is False  # the scripted exit
    assert launcher.exit_code() == 7


def test_the_exit_code_survives_the_handle_being_let_go() -> None:
    """LOCAL_MONITOR_EXITED is composed after ``stop`` has dropped the child, so
    the last status has to be remembered rather than re-read."""
    launcher, _ = make_launcher(exits_after=0)
    launcher.start(Path("/srv/app"))
    launcher.stop()
    assert launcher.exit_code() == 7
    assert LOCAL_MONITOR_EXITED.format(code=launcher.exit_code()) == (
        "the local monitor exited (code 7) - relaunch it from the Monitor tab"
    )


# -- stop ----------------------------------------------------------------------


def test_stop_terminates_and_waits_and_never_kills_a_child_that_went() -> None:
    launcher, spawned = make_launcher()
    launcher.start(Path("/srv/app"))
    launcher.stop()
    child = spawned[0]
    assert (child.terminated, child.killed) == (1, 0)
    assert child.waits == [pytest.approx(3.0)]
    assert launcher.alive() is False
    assert launcher.launched is None


def test_a_child_that_ignores_terminate_is_killed() -> None:
    """It holds this machine's mouse, keyboard and clipboard behind a port; it
    does not get to outlive the app that opened it."""
    launcher, spawned = make_launcher(ignores_terminate=True)
    launcher.start(Path("/srv/app"))
    launcher.stop()
    child = spawned[0]
    assert (child.terminated, child.killed) == (1, 1)
    assert len(child.waits) == 2
    assert launcher.exit_code() == -9


def test_stop_on_nothing_and_stop_twice_are_both_no_ops() -> None:
    launcher, spawned = make_launcher()
    launcher.stop()
    launcher.start(Path("/srv/app"))
    launcher.stop()
    launcher.stop()
    assert spawned[0].terminated == 1


def test_a_child_already_gone_is_not_signalled_at_all() -> None:
    launcher, spawned = make_launcher(exits_after=0)
    launcher.start(Path("/srv/app"))
    launcher.stop()
    assert (spawned[0].terminated, spawned[0].killed) == (0, 0)
