"""LocalHost: the Host seam over this PC's subprocess/os/pathlib.

Nothing here is new behavior - every method is the exact call the tool layer
used to make inline, moved down one level so the same tool code can run against
a remote machine later. The two platform quirks that used to live in shell.py
travel with it: POSIX children get their own session (so killpg reaps the whole
tree) and Windows kills through ``taskkill /F /T``.
"""

from __future__ import annotations

import os
import signal
import stat as stat_module
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from agentclip.hosts.base import DirEntry, ExecResult, FileStat


def _coerce_output(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


class LocalExec:
    """A local :class:`subprocess.Popen` behind the ExecHandle contract."""

    __slots__ = ("_proc", "_partial")

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        # Whatever communicate() had buffered when it last timed out: the
        # fallback if the post-kill drain cannot collect anything.
        self._partial = ""

    def wait(self, timeout: float) -> ExecResult | None:
        try:
            out, _ = self._proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self._partial = _coerce_output(exc.output)
            return None
        code = self._proc.returncode  # always set once communicate() has returned
        return ExecResult(exit_code=code if code is not None else 0, output=_coerce_output(out))

    def kill(self) -> None:
        """Kill the shell AND its children (shell=True makes the real work a grandchild)."""
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                self._proc.kill()

    def drain(self, timeout: float) -> str:
        try:  # collect whatever was buffered before the kill
            out, _ = self._proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return self._partial
        return _coerce_output(out)


class LocalHost:
    """The Host implementation for the machine AgentClip itself runs on."""

    name = "local"
    # As pathlib itself compares names: case-insensitively on Windows only.
    case_sensitive = os.name != "nt"

    def spawn(self, command: str, cwd: Path) -> LocalExec:
        popen_kwargs: dict = {}
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True  # own process group, so killpg reaps children
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            **popen_kwargs,
        )
        return LocalExec(proc)

    # -- files ---------------------------------------------------------------

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        with open(path, "rb") as f:
            return f.read(max_bytes)  # read(None) reads the whole file

    def write_bytes(self, path: Path, data: bytes, *, append: bool = False) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab" if append else "wb") as f:
            f.write(data)

    def delete(self, path: Path) -> None:
        Path(path).unlink()

    # -- directories ---------------------------------------------------------

    def mkdir(self, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def rmdir(self, path: Path) -> None:
        Path(path).rmdir()  # raises OSError when not empty: undo's stop condition

    # -- metadata ------------------------------------------------------------

    def stat(self, path: Path) -> FileStat | None:
        return _describe(path, follow=True)

    def lstat(self, path: Path) -> FileStat | None:
        return _describe(path, follow=False)

    def listdir(self, path: Path) -> list[DirEntry]:
        entries: list[DirEntry] = []
        with os.scandir(path) as it:
            for entry in it:
                is_dir = _entry_flag(entry.is_dir)
                entries.append(
                    DirEntry(
                        name=entry.name,
                        is_dir=is_dir,
                        is_file=_entry_flag(entry.is_file),
                        is_symlink=_entry_flag(entry.is_symlink),
                        # Only non-directories are ever shown with a size, and
                        # the stat is free from scandir's cached data.
                        size=0 if is_dir else _entry_size(entry),
                    )
                )
        return entries

    def realpath(self, path: Path, *, strict: bool = False) -> Path:
        return Path(path).resolve(strict=strict)


def _describe(path: Path, *, follow: bool) -> FileStat | None:
    """One FileStat, or None when the path is not there (as Path.exists() reads it)."""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return None
    is_symlink = stat_module.S_ISLNK(st.st_mode)
    if follow and is_symlink:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return None  # broken link: nothing at the end of it
    return FileStat(
        is_dir=stat_module.S_ISDIR(st.st_mode),
        is_file=stat_module.S_ISREG(st.st_mode),
        is_symlink=is_symlink,
        size=st.st_size,
    )


def _entry_flag(probe: Callable[[], bool]) -> bool:
    """os.DirEntry type probes raise on races/permission trouble; those are False."""
    try:
        return bool(probe())
    except OSError:
        return False


def _entry_size(entry: os.DirEntry[str]) -> int:
    try:
        return entry.stat().st_size
    except OSError:
        return 0
