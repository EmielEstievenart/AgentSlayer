"""Launching the Monitor this Chat UI owns: a child process, not an object.

docs/design/ui-monitor.md §10.1. The Chat UI is a brain, and a brain reaches
pixels **only** over the wire (§2.9). "Local" is therefore not a different kind
of monitor - it is a monitor process this Chat UI started on this machine,
dialled through exactly the same socket, token and ``watched()`` stream a remote
one is. That is the point of the wave: §10.0's two bugs both came from the Chat
UI having a second, in-process way to reach a screen, which disagreed with the
wire about where the chat region and the service key lived.

What is here is the *launch* half and nothing else - a command line, a port, a
token and a handle - so it can be tested without a process. Notably absent:

* **No readiness poll.** The dial IS the readiness check: the Chat UI's redial
  backoff already handles "not listening yet", and a second liveness notion here
  would be a second thing for the link to disagree with.
* **No token on the command line.** The child reads the same
  ``<config dir>/monitor/monitor-token`` file this module reads
  (:func:`~agentclip.driver.monitor.auth.load_or_create_token`), so the secret
  never reaches ``argv``, which is world-readable on the machines this runs on.
* **No** ``--headless``. The child comes up **with its window**, because that
  window is where calibration now happens (§10.2: one door, the Monitor UI).
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentclip.config import MONITOR_LOOPBACK, MonitorTarget

#: The console script the install puts next to ``agentclip`` - and the name of
#: the sibling looked for beside ``sys.executable`` in a frozen build
#: (``scripts/build-exe.ps1`` installs all three binaries into one folder).
MONITOR_EXECUTABLE = "agentclip-monitor"

#: The same program in a checkout, where there is no sibling exe: the Monitor
#: UI's entry point, which owns the windowed door and delegates ``--headless``
#: to the Driver's. ``-m`` rather than a script path, so it works from anywhere.
MONITOR_MODULE = "agentclip.shell.monitor_ui"

#: What a launched monitor is called wherever a target's name is shown. It is a
#: real :class:`MonitorTarget` like any other, so it needs one.
LOCAL_MONITOR_NAME = "local"

#: The extra sentence the DISCONNECTED path adds when the link dropped because
#: the child we started is gone (§10.1). The link's own reason says the socket
#: closed; this says *why* it closed, and what to do about it.
LOCAL_MONITOR_EXITED = "the local monitor exited (code {code}) - relaunch it from the Monitor tab"

#: How long :meth:`SubprocessLauncher.stop` gives a terminate before it kills.
#: Short on purpose: this runs on the way out of ``GuiRunner.stop()`` and the
#: monitor has nothing to flush - its state is the token file and the regions
#: file, both written when they changed rather than at exit.
STOP_GRACE_S = 3.0


@dataclass(frozen=True, slots=True)
class LaunchLocal:
    """``--monitor local`` (and the absent flag): *start one here, then dial it*.

    A sentinel rather than a :class:`MonitorTarget`, because the target does not
    exist yet - its port is chosen at launch. A frozen dataclass rather than a
    bare sentinel object so it type-narrows cleanly out of
    ``MonitorTarget | LaunchLocal | None`` and prints readably in a failure.
    """


@dataclass(frozen=True, slots=True)
class LaunchedMonitor:
    """A monitor process that exists, and the target that reaches it.

    ``process_id`` is carried for the operator, not for control: everything the
    launcher does to the child it does through the handle it kept, and a pid is
    what a Serve panel or a bug report can name.
    """

    target: MonitorTarget
    process_id: int


class ChildProcess(Protocol):
    """The slice of :class:`subprocess.Popen` this module uses.

    Spelled out so the suite can hand in a fake and no unit test ever starts a
    real process - the launcher's interesting behaviour (the command line, the
    port, the stop ladder) has nothing to do with a real one.
    """

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@runtime_checkable
class LocalMonitorLauncher(Protocol):
    """Start one monitor on this machine, stop it, and say whether it lives.

    One launcher owns at most one child at a time: the Chat UI has one local
    monitor or none. ``start`` on a launcher that already holds a child replaces
    it (the old one is stopped first), so **Launch a local monitor** pressed
    twice cannot leak a process.
    """

    def start(
        self, project_root: Path, *, global_config_path: Path | None = None
    ) -> LaunchedMonitor: ...

    def stop(self) -> None: ...

    def alive(self) -> bool: ...

    def exit_code(self) -> int | None: ...


def free_loopback_port() -> int:
    """A port nothing is listening on, found by binding it and letting go.

    Inherently a race - the port is free when we release it, not when the child
    binds it - and the right amount of engineering for the problem. The
    alternative (hand the child ``--port 0`` and read the number back off its
    stdout) would make the launch synchronous on a pipe and give this module the
    readiness poll it deliberately does not have; and a collision is a child that
    exits with a bind error, which is already a case the link has to report.

    ``SO_REUSEADDR`` is deliberately NOT set: it is the ephemeral range's own
    allocator we want, and on some platforms reuse would let us hand back a port
    somebody else is already listening on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((MONITOR_LOOPBACK, 0))
        return int(sock.getsockname()[1])


def monitor_executable() -> list[str]:
    """How to spell "run agentclip-monitor" from inside THIS install.

    Frozen: the sibling next to ``sys.executable``, by absolute path rather than
    by name - a PATH lookup would find whatever ``agentclip-monitor`` the user
    happens to have installed, which is exactly the version skew the wire
    handshake exists to refuse, and would find nothing at all when the app was
    launched from a folder that is not on PATH.

    From source: ``[sys.executable, "-m", MONITOR_MODULE]``, which is the same
    interpreter and the same checkout the Chat UI is running out of.
    """
    if getattr(sys, "frozen", False):
        suffix = ".exe" if os.name == "nt" else ""
        return [str(Path(sys.executable).resolve().parent / (MONITOR_EXECUTABLE + suffix))]
    return [sys.executable, "-m", MONITOR_MODULE]


def monitor_command(
    port: int, project_root: Path | str, *, global_config_path: Path | None = None
) -> list[str]:
    """The whole command line, and it is short by design.

    ``--bind 127.0.0.1`` is stated rather than left to the Driver's default
    because it is a *promise*, not a preference: a monitor this app started must
    never be reachable off this machine, and a later change to that default must
    not silently open one. ``--global-config`` rides along only when the launch
    had one, so the child otherwise resolves the platform location exactly as it
    would on its own.

    No ``--service``: the service preset is part of the ``MonitorSpec`` the brain
    sends over the wire on the first ``configure`` (§2.10). No ``--token``: see
    the module docstring. No ``--headless``: the window IS the calibration door.
    """
    argv = [
        *monitor_executable(),
        "--port",
        str(port),
        "--bind",
        MONITOR_LOOPBACK,
        "--project",
        str(project_root),
    ]
    if global_config_path is not None:
        argv += ["--global-config", str(global_config_path)]
    return argv


def shared_token() -> str:
    """The token the child will load, read from the file the child reads.

    Imported inside the function on purpose, and that is a layering fact rather
    than a performance one: ``shell.app``'s allowance (tests/test_layering.py)
    does not include the Driver, and a lazy import is the pattern that file
    permits - which keeps the *module graph* of this layer exactly what it was
    while letting the launcher read the one leaf the child also reads.
    ``platformdirs`` riding in behind ``default_monitor_dir`` is the second
    reason: a plain ``import agentclip.shell.app`` should not pay for it.
    """
    from agentclip.driver.monitor.auth import default_monitor_dir, load_or_create_token

    return load_or_create_token(default_monitor_dir())


def spawn_kwargs() -> dict[str, Any]:
    """Keep a console Ctrl+C off the child until :meth:`stop` says so.

    On Windows a console Ctrl+C is delivered to every process in the console's
    process group, so a monitor launched from a terminal would die *before* the
    Chat UI's orderly teardown reached it - the link would drop first and the
    shutdown would report a crash it had caused itself.
    ``CREATE_NEW_PROCESS_GROUP`` puts the child in its own group, and that is
    ALL it does here: the child keeps the parent's console and its inherited
    stdio (no ``DETACHED_PROCESS``, no ``CREATE_NO_WINDOW`` - the first would
    orphan it and the second would hide the startup line that names the port),
    and the only thing that ends it is :meth:`SubprocessLauncher.stop`.

    Nothing is needed on POSIX: the child is in this process's group, and the
    monitor's own SIGINT handling ends it the same way ``stop`` would.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


class SubprocessLauncher:
    """:class:`LocalMonitorLauncher` over a real child process.

    Every seam that would need an OS to exercise is a constructor argument - the
    spawn, the port picker, the token read - so the suite drives the whole class
    without a process, a socket or a config directory. The defaults are the real
    ones, and nothing outside the constructor knows they are seams.
    """

    def __init__(
        self,
        *,
        spawn: Callable[..., ChildProcess] | None = None,
        pick_port: Callable[[], int] | None = None,
        read_token: Callable[[], str] | None = None,
    ) -> None:
        self._spawn: Callable[..., ChildProcess] = spawn if spawn is not None else subprocess.Popen
        self._pick_port: Callable[[], int] = pick_port or free_loopback_port
        self._read_token: Callable[[], str] = read_token or shared_token
        self._process: ChildProcess | None = None
        self._launched: LaunchedMonitor | None = None
        self._last_exit: int | None = None

    @property
    def launched(self) -> LaunchedMonitor | None:
        """The child currently owned - None before the first start, and after a stop."""
        return self._launched

    def start(
        self, project_root: Path, *, global_config_path: Path | None = None
    ) -> LaunchedMonitor:
        """Pick a port, read the shared token, and run the monitor with its window.

        A launcher that already holds a child stops it first: the Chat UI has one
        local monitor, and a second launch must replace it rather than orphan it.
        """
        if self._process is not None:
            self.stop()
        port = self._pick_port()
        token = self._read_token()
        command = monitor_command(port, project_root, global_config_path=global_config_path)
        process = self._spawn(command, **spawn_kwargs())
        self._process = process
        self._last_exit = None
        self._launched = LaunchedMonitor(
            target=MonitorTarget(
                name=LOCAL_MONITOR_NAME, host=MONITOR_LOOPBACK, port=port, token=token
            ),
            process_id=process.pid,
        )
        return self._launched

    def alive(self) -> bool:
        """Is the child we started still running? False before any start."""
        return self._process is not None and self._process.poll() is None

    def exit_code(self) -> int | None:
        """The child's exit status, or None while it runs (and before any start).

        The last status is REMEMBERED across :meth:`stop`, because
        :data:`LOCAL_MONITOR_EXITED` is composed after the handle has been let go.
        """
        if self._process is None:
            return self._last_exit
        code = self._process.poll()
        if code is not None:
            self._last_exit = code
        return code

    def stop(self) -> None:
        """Terminate, wait briefly, then kill. Safe to call twice, and on nothing.

        The ladder rather than a bare ``terminate``: the child owns a window and
        a toolkit event loop, and a monitor that ignored its terminate would keep
        this machine's mouse, keyboard and clipboard behind a port after the app
        that opened it is gone. The grace is :data:`STOP_GRACE_S`, and every wait
        is allowed to fail - a child that vanished between the poll and the signal
        is a stop that already happened.
        """
        process, self._process = self._process, None
        self._launched = None
        if process is None:
            return
        already = process.poll()
        if already is not None:
            self._last_exit = already
            return
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            self._last_exit = process.wait(timeout=STOP_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self._last_exit = process.wait(timeout=STOP_GRACE_S)
