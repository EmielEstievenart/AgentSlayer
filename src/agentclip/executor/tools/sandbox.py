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
4. exclusion, which is TWO policies rather than one, because the exclusion
   list answers two different questions:
   - WRITES (and deletes) are refused under any excluded name at all - the
     configured ones (``paths.exclude``: .git, node_modules, .venv, .vscode,
     …) and the hard ones alike. Nothing the model does may edit a
     dependency tree, a VCS directory or a build output.
   - READS of an EXPLICITLY NAMED path are refused only under
     HARD_EXCLUDED_NAMES. The configured list exists for budget hygiene -
     keeping node_modules out of listings and sweeps - not for secrecy, and
     a model that is told "read .vscode/settings.json" should be able to.
     Traversal (``is_excluded``, used by list_dir/glob/grep) still skips the
     whole merged set, so an excluded tree never floods a result: it has to
     be asked for by name.
   HARD_EXCLUDED_NAMES (.agentclip, .agentclip.toml) is sealed in every
   direction - it holds the backups, transcripts and approval rules the model
   must neither read nor rewrite - and Workspace folds it into ``excludes``
   itself, so no caller can construct a jail without it.

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

from agentclip.executor.hosts.base import Host
from agentclip.executor.hosts.local import LocalHost

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Excluded from the file tools in EVERY direction and not configurable: the LLM
# must never read backups/transcripts or tamper with its own approval rules.
# It lives here, one layer below config.py (which re-exports it), because the
# Workspace is what enforces it - the jail cannot be handed its own hard floor
# by the caller it is jailing. Everything ELSE in the exclusion list is the
# user's `paths.exclude`, and reads inside those are allowed (see the module
# docstring).
HARD_EXCLUDED_NAMES = frozenset({".agentclip", ".agentclip.toml"})


class SandboxViolation(Exception):
    """A path argument tried to leave (or touch a forbidden part of) the workspace."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Workspace:
    """Project-root jail. The root is resolved strictly once at construction."""

    __slots__ = ("root", "excludes", "hard_excludes", "host")

    def __init__(
        self,
        root: Path,
        exclude: Iterable[str],
        *,
        host: Host | None = None,
        hard_exclude: Iterable[str] = HARD_EXCLUDED_NAMES,
    ) -> None:
        self.host: Host = host if host is not None else LocalHost()
        self.root: Path = self.host.realpath(Path(root), strict=True)
        # The hard names are folded in rather than trusted to arrive: a caller
        # that passes a bare `paths.exclude` still gets a jail that seals
        # .agentclip. `exclude` is the union (traversal + writes); the hard set
        # is the narrower one reads are checked against.
        self.hard_excludes: frozenset[str] = frozenset(hard_exclude)
        self.excludes: frozenset[str] = frozenset(exclude) | self.hard_excludes

    # -- public API --------------------------------------------------------

    def resolve_read(self, rel: str) -> Path:
        """Resolve a path for reading. The result may not exist (callers check).

        An explicitly named path inside a configured excluded directory is
        ALLOWED here - `.vscode/settings.json` is readable, `node_modules/x/
        package.json` is readable. Only HARD_EXCLUDED_NAMES is refused.
        """
        parts = self._shape_check(rel)
        candidate = self.host.realpath(self.root.joinpath(*parts))
        self._check_contained(candidate, rel)
        self._check_read_allowed(candidate, rel)
        return candidate

    def resolve_write(self, rel: str) -> Path:
        """Resolve a path for writing/deleting. The file itself may not exist yet.

        Stricter than :meth:`resolve_read`: every excluded name, configured or
        hard, is refused - reading a dependency tree is research, editing one
        is damage.
        """
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
        self._check_write_allowed(candidate, rel)
        return candidate

    def is_excluded(self, p: Path) -> bool:
        """True when p (a path under root, resolved or lexical) hits the exclusion list.

        Traversal tools (list_dir/glob/grep) use this to silently skip excluded
        entries instead of erroring, and they check the FULL merged set: a
        listing or a sweep must never fill up with node_modules even though a
        named file inside it could be read. Paths outside root count as
        excluded.
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

    def _check_read_allowed(self, candidate: Path, rel: str) -> None:
        """Step 4, read flavor: only the hard names are off limits."""
        for part in candidate.relative_to(self.root).parts:
            if part in self.hard_excludes:
                raise SandboxViolation(f"path {rel!r} is under sealed entry {part!r}")

    def _check_write_allowed(self, candidate: Path, rel: str) -> None:
        """Step 4, write flavor: no component may be an excluded name at all."""
        for part in candidate.relative_to(self.root).parts:
            if part in self.excludes:
                raise SandboxViolation(f"path {rel!r} is under excluded entry {part!r}")
