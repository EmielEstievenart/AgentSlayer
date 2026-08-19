"""LocalHost: the Host seam over this PC's subprocess/os/pathlib.

Almost nothing here is new behavior - every method is the exact call the tool
layer used to make inline, moved down one level so the same tool code can run
against a remote machine later. The two platform quirks that used to live in
shell.py travel with it: POSIX children get their own session (so killpg reaps
the whole tree) and Windows kills through ``taskkill /F /T``.

The one thing that is genuinely different is HOW the output is collected.
``LocalExec`` used to wait in ``communicate(timeout=slice)`` calls, which hand
back nothing until the command exits (a timed-out slice only exposes its
half-read buffer through the exception, and only on POSIX). That made a
five-minute build a five-minute silence. Instead a **reader thread** drains the
pipe continuously into a lock-guarded buffer from the moment the process
starts: ``wait()`` just polls the process, ``peek()`` hands out a snapshot of
the buffer (the live tail the UI streams), and both the finished result and the
post-kill ``drain()`` are read out of the same buffer - so there is exactly one
place the output ever lives, and a killed command's partial tail survives for
free instead of via a special case.

Bytes, not text, come off the pipe: partial UTF-8 sequences and split ``\\r\\n``
pairs are the normal state of affairs when reading a pipe in chunks, so
:class:`_StreamDecoder` does incrementally what ``text=True`` would have done in
one go - decode with replacement, and translate newlines universal-style.
"""

from __future__ import annotations

import codecs
import io
import os
import signal
import stat as stat_module
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

from agentclip.executor.hosts.base import DirEntry, ExecResult, FileStat

# How much the reader thread asks for per read. It returns as soon as ANY bytes
# are there (``read1``), so this is a ceiling, not a latency floor.
_READ_CHUNK = 65536
# How long ``wait()`` gives the reader thread to notice EOF after the process
# has exited, so the last line makes it into the result. Bounded rather than
# unbounded (which is what communicate() was): a grandchild holding the pipe
# open must not park the engine's worker thread forever.
_READER_JOIN_S = 5.0


class _StreamDecoder:
    """Incremental UTF-8 + universal-newline translation over chunked reads.

    Two states have to survive a chunk boundary and both are famous for
    corrupting exactly one character when they do not: a multi-byte sequence cut
    in half (the incremental codec's job) and a ``\\r`` that may or may not turn
    out to be the front of a ``\\r\\n`` (held back until the next chunk says).
    Lone ``\\r`` becomes ``\\n``, as ``text=True`` had it - so a progress bar
    that only ever rewrites one line still reads as lines here.
    """

    __slots__ = ("_decoder", "_pending_cr")

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending_cr = False

    def feed(self, data: bytes, *, final: bool = False) -> str:
        text = self._decoder.decode(data, final)
        if self._pending_cr:
            # The previous chunk ended on a CR: one \n now means it was a CRLF.
            text = text[1:] if text.startswith("\n") else text
            text = "\n" + text
            self._pending_cr = False
        if not final and text.endswith("\r"):
            text = text[:-1]
            self._pending_cr = True
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def flush(self) -> str:
        """Whatever the two held-back states still owe, at EOF."""
        return self.feed(b"", final=True)


class LocalExec:
    """A local :class:`subprocess.Popen` behind the ExecHandle contract."""

    __slots__ = ("_proc", "_chunks", "_lock", "_reader", "_result")

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        # The one buffer. Appended to by the reader thread, read by everybody
        # else - hence the lock, which is held only around list mutations.
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        self._result: ExecResult | None = None
        self._reader = threading.Thread(
            target=self._pump, name="agentclip-exec-reader", daemon=True
        )
        self._reader.start()

    def wait(self, timeout: float) -> ExecResult | None:
        if self._result is not None:
            return self._result  # finished earlier: the answer stands
        try:
            code = self._proc.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return None
        # The process is gone; give the reader a moment to see EOF so the last
        # line is in the buffer before the result is frozen around it.
        self._reader.join(_READER_JOIN_S)
        self._result = ExecResult(exit_code=code if code is not None else 0, output=self.peek())
        return self._result

    def peek(self) -> str:
        """The merged output so far - a snapshot, never blocking."""
        with self._lock:
            return "".join(self._chunks)

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
        """After kill(): what the command managed to emit before it died."""
        self._reader.join(max(0.0, timeout))
        return self.peek()

    # -- the reader thread ---------------------------------------------------

    def _pump(self) -> None:
        """Move the pipe into the buffer until EOF. Never raises out of here."""
        # A buffered pipe by construction (spawn asks for stdout=PIPE in binary
        # mode), which is what makes read1 - "give me whatever has arrived" -
        # available; the Popen stubs only promise the IO[bytes] base.
        stream = cast(io.BufferedReader, self._proc.stdout)
        if stream is None:
            return
        decoder = _StreamDecoder()
        try:
            while True:
                data = stream.read1(_READ_CHUNK)  # blocks until SOMETHING is there
                if not data:
                    break
                self._append(decoder.feed(data))
        except (OSError, ValueError):
            pass  # the pipe was closed under us (kill): whatever arrived stands
        finally:
            self._append(decoder.flush())
            with suppress(OSError, ValueError):
                stream.close()

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)


class LocalHost:
    """The Host implementation for the machine AgentClip itself runs on."""

    name = "local"
    # As pathlib itself compares names: case-insensitively on Windows only.
    case_sensitive = os.name != "nt"

    def spawn(self, command: str, cwd: Path) -> LocalExec:
        popen_kwargs: dict = {}
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True  # own process group, so killpg reaps children
        # Binary, deliberately: the reader thread decodes (see _StreamDecoder),
        # because a TextIOWrapper only hands over whole lines and this pipe has
        # to be readable the instant a byte lands on it.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            # A tool's command NEVER inherits this process's stdin. Two reasons,
            # and the second one is a hang:
            #  * a command that reads input would otherwise eat the app's own -
            #    the user's keystrokes in the TUI, the link's frames in the
            #    engine-over-a-wire process (docs/design/remote-executor.md).
            #    With DEVNULL it reads EOF and gets on with it.
            #  * on Windows, an inherited pipe stdin DEADLOCKS the child while
            #    this process has a blocking read pending on that same handle:
            #    handles are synchronous, so the child's startup query of its own
            #    stdin (GetFileType, which every CPython start does) queues
            #    behind our unfinished ReadFile and never returns. That is
            #    exactly the shape of the link server - a reader thread parked on
            #    stdin for the next frame while a worker runs a command - and it
            #    is what tests/shell/app/test_remote_link.py's streaming test
            #    hangs on without this line.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
