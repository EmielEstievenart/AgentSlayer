"""FakeHost: an in-memory host for unit tests.

Every tool reaches the OS through the Host seam, so a test can hand it a
filesystem that lives in a dict and commands whose results are scripted. That
buys two things: tool semantics get tested without touching a real disk, and -
more to the point - a tool that quietly kept a `Path.read_text()` of its own
fails here loudly, which is what keeps the seam honest.

Paths are normalized to forward-slash strings, so tests can write "/project/a"
on any platform. Symlinks are modelled (targets must be absolute) because the
sandbox jail's escape detection is exactly what needs exercising against a
non-local host.
"""

from __future__ import annotations

import errno
import time
from dataclasses import dataclass
from pathlib import Path

from agentclip.hosts.base import DirEntry, ExecResult, FileStat

_MAX_LINK_DEPTH = 40


def _key(path: Path | str) -> str:
    """Normalize any path to the fake filesystem's key form."""
    text = str(path).replace("\\", "/")
    return text.rstrip("/") if len(text) > 1 else text


def _parent(key: str) -> str:
    head, _, _ = key.rpartition("/")
    return head


@dataclass(frozen=True, slots=True)
class FakeCommand:
    """A scripted command result. ``hangs`` never finishes - the cancel/timeout path."""

    exit_code: int = 0
    output: str = ""
    hangs: bool = False


class FakeExec:
    """The ExecHandle over a scripted result; records what the caller did to it."""

    __slots__ = ("command", "script", "killed", "drained")

    def __init__(self, command: str, script: FakeCommand) -> None:
        self.command = command
        self.script = script
        self.killed = False
        self.drained = False

    def wait(self, timeout: float) -> ExecResult | None:
        if self.script.hangs:
            time.sleep(max(0.0, timeout))  # burn the slice, like a real slow command
            return None
        return ExecResult(exit_code=self.script.exit_code, output=self.script.output)

    def kill(self) -> None:
        self.killed = True

    def drain(self, timeout: float) -> str:
        self.drained = True
        return self.script.output


class FakeHost:
    """In-memory files + scripted commands behind the Host protocol."""

    name = "fake"

    def __init__(self, root: Path | str = "/project", *, case_sensitive: bool = True) -> None:
        # A parameter rather than a constant: the case-sensitivity of the machine
        # the files are on changes what glob matches, and both answers need a
        # host to be tested against.
        self.case_sensitive = case_sensitive
        self.root = Path(_key(root))
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = set()
        self._links: dict[str, str] = {}  # link key -> absolute target key
        self.commands: list[tuple[str, Path]] = []  # every spawn, in order
        self.handles: list[FakeExec] = []
        self._scripts: dict[str, FakeCommand] = {}
        self.default_command = FakeCommand()
        self.add_dir(self.root)

    # -- test-side authoring -------------------------------------------------

    def add_dir(self, path: Path | str) -> None:
        key = _key(path)
        while key and key not in self._dirs:
            self._dirs.add(key)
            key = _parent(key)

    def add_file(self, path: Path | str, content: bytes | str = b"") -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        key = _key(path)
        self.add_dir(_parent(key))
        self._files[key] = data

    def add_symlink(self, link: Path | str, target: Path | str) -> None:
        key = _key(link)
        self.add_dir(_parent(key))
        self._links[key] = _key(target)

    def script(
        self, command: str, *, exit_code: int = 0, output: str = "", hangs: bool = False
    ) -> None:
        """Script the result of one exact command string."""
        self._scripts[command] = FakeCommand(exit_code, output, hangs)

    def text(self, path: Path | str) -> str:
        """The current content of a file, for assertions."""
        return self._files[_key(path)].decode("utf-8")

    # -- Host: process execution --------------------------------------------

    def spawn(self, command: str, cwd: Path) -> FakeExec:
        self.commands.append((command, cwd))
        handle = FakeExec(command, self._scripts.get(command, self.default_command))
        self.handles.append(handle)
        return handle

    # -- Host: files ---------------------------------------------------------

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        key = self._resolve(_key(path))
        if key in self._files:
            data = self._files[key]
            return data if max_bytes is None else data[:max_bytes]
        if key in self._dirs:
            raise IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))

    def write_bytes(self, path: Path, data: bytes, *, append: bool = False) -> None:
        key = self._resolve(_key(path))
        if key in self._dirs:
            raise IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
        self.add_dir(_parent(key))
        self._files[key] = self._files.get(key, b"") + data if append else data

    def delete(self, path: Path) -> None:
        key = self._resolve(_key(path))
        if key in self._dirs:
            raise IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
        if key not in self._files:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        del self._files[key]

    # -- Host: directories ---------------------------------------------------

    def mkdir(self, path: Path) -> None:
        key = self._resolve(_key(path))
        if key in self._files:
            raise FileExistsError(errno.EEXIST, "File exists", str(path))
        self.add_dir(key)

    def rmdir(self, path: Path) -> None:
        key = self._resolve(_key(path))
        if key not in self._dirs:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        if any(_parent(child) == key for child in (*self._files, *self._dirs, *self._links)):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        self._dirs.discard(key)

    # -- Host: metadata ------------------------------------------------------

    def stat(self, path: Path) -> FileStat | None:
        return self._describe(self._lresolve(_key(path)), follow=True)

    def lstat(self, path: Path) -> FileStat | None:
        return self._describe(self._lresolve(_key(path)), follow=False)

    def _describe(self, key: str, *, follow: bool) -> FileStat | None:
        is_link = key in self._links
        if is_link and not follow:
            return FileStat(is_dir=False, is_file=False, is_symlink=True)
        target = self._resolve(key) if is_link else key
        if target in self._dirs:
            return FileStat(is_dir=True, is_file=False, is_symlink=is_link)
        if target in self._files:
            return FileStat(
                is_dir=False, is_file=True, is_symlink=is_link, size=len(self._files[target])
            )
        return None

    def listdir(self, path: Path) -> list[DirEntry]:
        key = self._resolve(_key(path))
        if key not in self._dirs:
            if key in self._files:
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        names = {
            child.rpartition("/")[2]
            for child in (*self._files, *self._dirs, *self._links)
            if _parent(child) == key and child != key
        }
        entries: list[DirEntry] = []
        for name in sorted(names):
            child = f"{key}/{name}"
            st = self.stat(Path(child))
            entries.append(
                DirEntry(
                    name=name,
                    is_dir=st is not None and st.is_dir,
                    is_file=st is not None and st.is_file,
                    is_symlink=child in self._links,
                    size=st.size if st is not None and not st.is_dir else 0,
                )
            )
        return entries

    def realpath(self, path: Path, *, strict: bool = False) -> Path:
        key = self._resolve(_key(path))
        if strict and key not in self._dirs and key not in self._files:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        return Path(key)

    # -- symlink resolution --------------------------------------------------

    def _lresolve(self, key: str) -> str:
        """Resolve everything but the last component (the lstat view of a path)."""
        head, sep, name = key.rpartition("/")
        if not sep:
            return key
        return f"{self._resolve(head) if head else ''}/{name}"

    def _resolve(self, key: str, depth: int = 0) -> str:
        """Follow symlinks and '..' component by component, as realpath does."""
        if depth > _MAX_LINK_DEPTH:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", key)
        parts = key.split("/")
        resolved = parts[0]
        for part in parts[1:]:
            if part in ("", "."):
                continue
            if part == "..":
                resolved = _parent(resolved)
                continue
            candidate = f"{resolved}/{part}"
            resolved = (
                self._resolve(self._links[candidate], depth + 1)
                if candidate in self._links
                else candidate
            )
        return resolved or "/"
