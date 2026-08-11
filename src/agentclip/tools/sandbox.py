"""Workspace sandbox: the project-root path jail (architecture.md section 3).

Every path argument arriving from the LLM is an untrusted string. The
four-step check, in order:

1. shape: reject absolute paths (in BOTH the POSIX and Windows flavors),
   drive designators, UNC prefixes, and NUL bytes before touching the
   filesystem;
2. resolution: resolve symlinks - writes resolve the deepest EXISTING
   ancestor with strict=True and refuse ".."/symlink components in the
   non-existent tail (so a write can never tunnel out through a symlinked
   directory);
3. containment: the resolved candidate must stay under the workspace root;
4. exclusion: no component may name an excluded entry. Callers pass the
   merged set from config.Config.excluded_names(), which always contains
   .agentclip and .agentclip.toml.

Violations raise SandboxViolation; tool handlers translate it into a
path_outside_workspace error result so the LLM can self-correct.

Steps 2's filesystem questions - realpath, does this exist, is this a symlink -
go through the Host, so the jail is drawn around the machine the project
actually lives on rather than around the one AgentClip runs on. Steps 1, 3 and
4 are pure string/path work and need no host at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentclip.hosts.base import Host
from agentclip.hosts.local import LocalHost

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class SandboxViolation(Exception):
    """A path argument tried to leave (or touch a forbidden part of) the workspace."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Workspace:
    """Project-root jail. The root is resolved strictly once at construction."""

    __slots__ = ("root", "excludes", "host")

    def __init__(self, root: Path, exclude: Iterable[str], *, host: Host | None = None) -> None:
        self.host: Host = host if host is not None else LocalHost()
        self.root: Path = self.host.realpath(Path(root), strict=True)
        self.excludes: frozenset[str] = frozenset(exclude)

    # -- public API --------------------------------------------------------

    def resolve_read(self, rel: str) -> Path:
        """Resolve a path for reading. The result may not exist (callers check)."""
        parts = self._shape_check(rel)
        candidate = self.host.realpath(self.root.joinpath(*parts))
        self._check_contained(candidate, rel)
        self._check_excluded(candidate, rel)
        return candidate

    def resolve_write(self, rel: str) -> Path:
        """Resolve a path for writing/deleting. The file itself may not exist yet."""
        parts = self._shape_check(rel)
        if not parts:
            raise SandboxViolation(f"path {rel!r} resolves to the project root, not a file")

        # Walk down the EXISTING portion lexically; stat() follows symlinks, so
        # a broken symlink terminates the walk and is rejected explicitly.
        cur = self.root
        i = 0
        while i < len(parts):
            nxt = cur / parts[i]
            if self.host.stat(nxt) is not None:
                cur = nxt
                i += 1
                continue
            link = self.host.lstat(nxt)
            if link is not None and link.is_symlink:
                raise SandboxViolation(f"broken symlink in path: {parts[i]!r}")
            break

        ancestor = self.host.realpath(cur, strict=True)
        tail = parts[i:]
        for part in tail:
            if part == "..":
                raise SandboxViolation(
                    f"'..' not allowed in the non-existent part of a write path: {rel!r}"
                )

        if not (ancestor == self.root or ancestor.is_relative_to(self.root)):
            raise SandboxViolation(f"path {rel!r} escapes the project root (symlink or '..')")

        candidate = ancestor.joinpath(*tail)
        self._check_contained(candidate, rel)
        self._check_excluded(candidate, rel)
        return candidate

    def is_excluded(self, p: Path) -> bool:
        """True when p (a path under root, resolved or lexical) hits the exclusion list.

        Traversal tools (list_dir/glob/grep) use this to silently skip excluded
        entries instead of erroring. Paths outside root count as excluded.
        """
        try:
            rel = p.relative_to(self.root)
        except ValueError:
            return True
        return any(part in self.excludes for part in rel.parts)

    # -- the four-step check, steps 1/3/4 ------------------------------------

    def _shape_check(self, rel: str) -> tuple[str, ...]:
        """Step 1: reject absolute/drive/UNC/NUL shapes; return clean components."""
        if "\x00" in rel:
            raise SandboxViolation("path contains a NUL byte")
        cleaned = rel.strip()
        normalized = cleaned.replace("\\", "/")
        if (
            PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(cleaned).is_absolute()
            or _DRIVE_RE.match(cleaned)
            or normalized.startswith("//")
        ):
            raise SandboxViolation(f"absolute paths are not allowed: {rel!r}")
        # PurePosixPath drops "." components and empty segments.
        return PurePosixPath(normalized).parts

    def _check_contained(self, candidate: Path, rel: str) -> None:
        """Step 3: the resolved candidate must stay under root."""
        if not (candidate == self.root or candidate.is_relative_to(self.root)):
            raise SandboxViolation(f"path {rel!r} escapes the project root")

    def _check_excluded(self, candidate: Path, rel: str) -> None:
        """Step 4: no component of the in-root path may be an excluded name."""
        for part in candidate.relative_to(self.root).parts:
            if part in self.excludes:
                raise SandboxViolation(f"path {rel!r} is under excluded entry {part!r}")
