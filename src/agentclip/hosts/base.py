"""The Host seam: the OS primitives every tool is allowed to touch.

A Host is "the machine the project lives on". Today that is always the PC
AgentClip runs on (:class:`~agentclip.hosts.local.LocalHost`); the seam exists
so a remote machine over SSH can take its place without any tool knowing
(docs/design/remote-ssh.md).

The interface is deliberately **primitive-only** - process execution, byte
reads/writes, stat/listdir, realpath. Everything above that (grep's regex scan,
glob's pruning walk, edit_file's matching, tail capping, every cap and limit)
stays above the seam as shared code, so a tool written against these primitives
works on every host by construction. A round trip is not free on a remote host,
so the primitives are shaped to keep the count low: one ``stat`` answers
exists/is-dir/is-file/size, one ``listdir`` entry answers the same for a child.

Process execution is split into ``spawn`` + an :class:`ExecHandle` rather than
one blocking ``exec(...)`` call, because run_command's cooperative cancellation
(poll slices that re-check the user's cancel and the deadline, then kill the
whole process tree) is policy, not OS access: it belongs above the seam, and it
needs a handle it can poll and kill.

Paths are :class:`pathlib.Path`. A remote host is handed paths built from its
own remote root, which is POSIX-flavored; local Windows semantics are untouched.

Error contract: filesystem primitives raise :class:`OSError` (the familiar
subclasses - FileNotFoundError, PermissionError, ...) exactly as the stdlib
would, so handler code keeps catching what it always caught. ``stat``/``lstat``
are the exception: they answer None for "not there" instead of raising, since
every caller asked "does this exist, and what is it?".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExecResult:
    """A finished command: its exit code and its merged stdout+stderr."""

    exit_code: int
    output: str


@dataclass(frozen=True, slots=True)
class FileStat:
    """What a stat call answers; ``size`` is st_size as the OS reports it.

    From ``stat`` the flags describe the symlink TARGET (is_symlink still
    reports whether the path itself was a link); from ``lstat`` they describe
    the link itself, so a symlink has is_dir=is_file=False.
    """

    is_dir: bool
    is_file: bool
    is_symlink: bool
    size: int = 0


@dataclass(frozen=True, slots=True)
class DirEntry:
    """One child of a directory, with its type resolved in the same round trip.

    ``is_dir``/``is_file`` follow symlinks (like :class:`os.DirEntry`), so a
    link to a directory has is_dir=True and is_symlink=True; "a real directory"
    is ``is_dir and not is_symlink``. Unreadable entries degrade to False/0
    rather than raising - the same way the traversal tools already treat them.
    """

    name: str
    is_dir: bool
    is_file: bool
    is_symlink: bool
    size: int = 0


class ExecHandle(Protocol):
    """A running command, driven by a polling loop above the seam.

    Contract:
      - ``wait(timeout)`` returns the :class:`ExecResult` once the command has
        finished, or None if it is still running after ``timeout`` seconds.
        Calling it repeatedly is safe: output buffered during earlier slices
        survives into the final result.
      - ``peek()`` is the merged output SO FAR, as a snapshot: non-blocking,
        safe to call as often as the polling loop likes, and only ever growing
        (what it returned once stays a prefix of what it returns next). This is
        what makes a long command watchable while it runs - run_command diffs
        successive peeks and streams the new characters to the UI
        (``ToolContext.on_output``). A host whose transport cannot hand over
        partial output answers "" until the command finishes, and everything
        above simply shows nothing live: the final result is unaffected.
      - ``kill()`` kills the command AND its children (a shell command's real
        work is a grandchild), best effort, never raising.
      - ``drain(timeout)`` is for after ``kill()``: whatever merged output the
        command managed to emit before it died, best effort.
    """

    def wait(self, timeout: float) -> ExecResult | None: ...

    def peek(self) -> str: ...

    def kill(self) -> None: ...

    def drain(self, timeout: float) -> str: ...


class Host(Protocol):
    """The OS primitives a session's tools may use. See the module docstring."""

    name: str  # for diagnostics / the bootstrap's "on {os}" slot
    # Do path NAMES compare case-sensitively here? A property of the machine the
    # files are on, never of the one AgentClip runs on: glob's matching and the
    # skill-folder ordering ask this, and a Windows operator driving a Linux box
    # must get Linux's answer.
    case_sensitive: bool

    # -- process execution -----------------------------------------------

    def spawn(self, command: str, cwd: Path) -> ExecHandle:
        """Start ``command`` through a shell in ``cwd``, stdout+stderr merged."""
        ...

    # -- files -------------------------------------------------------------

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        """Read a file. ``max_bytes`` reads only a prefix (the binary sniff)."""
        ...

    def write_bytes(self, path: Path, data: bytes, *, append: bool = False) -> None:
        """Write (or append to) a file, creating missing parent directories."""
        ...

    def delete(self, path: Path) -> None:
        """Delete one file (not a directory)."""
        ...

    # -- directories -------------------------------------------------------
    #
    # Only undo needs these (restoring a file whose directory the turn created,
    # then pruning what it emptied), which is why the seam carries no general
    # directory API: no tool may create or remove directories of its own.

    def mkdir(self, path: Path) -> None:
        """Create a directory and its missing parents; an existing one is fine."""
        ...

    def rmdir(self, path: Path) -> None:
        """Remove one EMPTY directory. Raises OSError when it is not empty."""
        ...

    # -- metadata ----------------------------------------------------------

    def stat(self, path: Path) -> FileStat | None:
        """Stat following symlinks; None when the path does not exist."""
        ...

    def lstat(self, path: Path) -> FileStat | None:
        """Stat WITHOUT following symlinks; None when the path does not exist."""
        ...

    def listdir(self, path: Path) -> list[DirEntry]:
        """The children of a directory, unordered. Raises OSError if unreadable."""
        ...

    def realpath(self, path: Path, *, strict: bool = False) -> Path:
        """Resolve symlinks and '..' to an absolute path (the sandbox jail's ruler).

        With ``strict`` the path must exist, as Path.resolve(strict=True) does.
        """
        ...
