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

The one crack in the wall is ``extra_read_roots``: directories OUTSIDE the
project root that named READS may reach. It exists for exactly one thing -
the discovered skill folders, whose bundled scripts and references are useless
to a model that cannot open them (executor/tools/skills.py) - and it is drawn
as narrowly as that purpose allows:

- only an ABSOLUTE path can land in one, so a relative path never stops
  meaning "under the project root" and a skill folder can never shadow a
  project file by name;
- containment is checked on the RESOLVED path, exactly as step 3 does, so a
  symlink or a ``..`` inside a skill folder is not a tunnel to the rest of the
  filesystem;
- the HARD_EXCLUDED_NAMES floor applies inside them too;
- writes, edits and deletes are refused there in so many words ("skill folders
  are read-only"): resolve_write never consults the extra roots except to pick
  which refusal the model is told;
- traversal does not extend into them (see :meth:`Workspace.resolve_scan`).

Violations raise SandboxViolation; tool handlers translate it into a
path_outside_workspace error result so the LLM can self-correct.

Steps 2's filesystem questions - realpath, does this exist, is this a symlink -
go through the Host, so the jail is drawn around the machine the project
actually lives on rather than around the one AgentClip runs on. Steps 1, 3 and
4 are pure string/path work and need no host at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
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


def _absolute_shape(rel: str) -> bool:
    """Does this path argument name a LOCATION rather than a place in the tree?

    The four flavors step 1 refuses, factored out of :meth:`Workspace._shape_check`
    because reads now have a second answer for them (an extra read root) and
    "absolute" has to mean the same thing in both places. Pure string/path work:
    safe to ask of a string that has not been NUL-checked yet.
    """
    cleaned = rel.strip()
    normalized = cleaned.replace("\\", "/")
    return (
        PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(cleaned).is_absolute()
        or bool(_DRIVE_RE.match(cleaned))
        or normalized.startswith("//")
    )


class SandboxViolation(Exception):
    """A path argument tried to leave (or touch a forbidden part of) the workspace."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class Workspace:
    """Project-root jail. The root is resolved strictly once at construction."""

    __slots__ = ("root", "excludes", "hard_excludes", "host", "extra_read_roots")

    def __init__(
        self,
        root: Path,
        exclude: Iterable[str],
        *,
        host: Host | None = None,
        hard_exclude: Iterable[str] = HARD_EXCLUDED_NAMES,
        extra_read_roots: Sequence[Path] = (),
    ) -> None:
        self.host: Host = host if host is not None else LocalHost()
        self.root: Path = self.host.realpath(Path(root), strict=True)
        # The hard names are folded in rather than trusted to arrive: a caller
        # that passes a bare `paths.exclude` still gets a jail that seals
        # .agentclip. `exclude` is the union (traversal + writes); the hard set
        # is the narrower one reads are checked against.
        self.hard_excludes: frozenset[str] = frozenset(hard_exclude)
        self.excludes: frozenset[str] = frozenset(exclude) | self.hard_excludes
        # Resolved once, here, for the same reason `root` is: the containment
        # test compares two paths that came through the same normalization, so
        # a symlinked skill folder cannot fail to match itself. A root that
        # cannot be resolved is DROPPED rather than raised on - a skill folder
        # that vanished between discovery and session start is a missing
        # carve-out, not a session that refuses to start.
        self.extra_read_roots: tuple[Path, ...] = self._resolve_extra_roots(extra_read_roots)

    def _resolve_extra_roots(self, roots: Sequence[Path]) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for root in roots:
            try:
                real = self.host.realpath(Path(root), strict=True)
            except (OSError, ValueError):
                continue
            if real not in resolved:
                resolved.append(real)
        return tuple(resolved)

    # -- public API --------------------------------------------------------

    def resolve_read(self, rel: str) -> Path:
        """Resolve a path for reading. The result may not exist (callers check).

        An explicitly named path inside a configured excluded directory is
        ALLOWED here - `.vscode/settings.json` is readable, `node_modules/x/
        package.json` is readable. Only HARD_EXCLUDED_NAMES is refused.

        An ABSOLUTE path is the one way into an extra read root (a discovered
        skill folder), and the only way: everything relative still resolves
        under the project root and nowhere else.
        """
        if _absolute_shape(rel):
            return self._resolve_extra_read(rel)
        return self.resolve_scan(rel)

    def resolve_scan(self, rel: str) -> Path:
        """Resolve the path a TRAVERSAL tool starts from (list_dir/glob/grep).

        :meth:`resolve_read` minus the extra-read-root carve-out, because a
        sweep is not a named read: a skill folder's contents are already
        disclosed by the skill result's own file listing, and a glob or grep
        that could be pointed at one would turn a read-only carve-out into a
        second tree to walk - one that the traversal tools have nothing to
        render their hits relative to.
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
        is damage. The extra read roots are read-only, and this is where that
        is said out loud: the check below only picks WHICH refusal the model
        gets, and no branch here can reach the read resolver.
        """
        if _absolute_shape(rel) and self._inside_extra_root(rel):
            raise SandboxViolation(
                f"skill folders are read-only: {rel!r} (they can be read, never written)"
            )
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

    # -- the extra read roots (skill folders) ---------------------------------

    def _resolve_extra_read(self, rel: str) -> Path:
        """An ABSOLUTE read path, allowed only inside an extra read root.

        Same discipline as the jail itself, in the same order: resolve symlinks
        and ``..`` FIRST, then ask containment of the ANSWER. A skill folder
        holding a link to /etc, or a caller spelling
        `<skill folder>/../../.ssh/id_rsa`, resolves to something no root
        contains and is refused there - the carve-out is a door, not a tunnel.

        With no extra roots configured this is byte-for-byte the refusal step 1
        has always given, so a Workspace built the old way behaves the old way.
        """
        if "\x00" in rel:
            raise SandboxViolation("path contains a NUL byte")
        if not self.extra_read_roots:
            raise SandboxViolation(f"absolute paths are not allowed: {rel!r}")
        candidate = self.host.realpath(Path(rel.strip()))
        for root in self.extra_read_roots:
            if candidate == root or candidate.is_relative_to(root):
                self._check_sealed(candidate.relative_to(root).parts, rel)
                return candidate
        raise SandboxViolation(
            f"absolute paths are allowed only inside a skill folder: {rel!r}"
        )

    def _inside_extra_root(self, rel: str) -> bool:
        """Best-effort "is this absolute path in a skill folder?", for the write
        refusal's wording only. It never grants anything, so a path that cannot
        be resolved simply falls through to the ordinary shape refusal."""
        if not self.extra_read_roots or "\x00" in rel:
            return False
        try:
            candidate = self.host.realpath(Path(rel.strip()))
        except (OSError, ValueError):
            return False
        return any(candidate == root or candidate.is_relative_to(root) for root in self.extra_read_roots)

    # -- the four-step check, steps 1/3/4 ------------------------------------

    def _shape_check(self, rel: str) -> tuple[str, ...]:
        """Step 1: reject absolute/drive/UNC/NUL shapes; return clean components."""
        if "\x00" in rel:
            raise SandboxViolation("path contains a NUL byte")
        if _absolute_shape(rel):
            raise SandboxViolation(f"absolute paths are not allowed: {rel!r}")
        # PurePosixPath drops "." components and empty segments.
        return PurePosixPath(rel.strip().replace("\\", "/")).parts

    def _check_contained(self, candidate: Path, rel: str) -> None:
        """Step 3: the resolved candidate must stay under root."""
        if not (candidate == self.root or candidate.is_relative_to(self.root)):
            raise SandboxViolation(f"path {rel!r} escapes the project root")

    def _check_read_allowed(self, candidate: Path, rel: str) -> None:
        """Step 4, read flavor: only the hard names are off limits."""
        self._check_sealed(candidate.relative_to(self.root).parts, rel)

    def _check_sealed(self, parts: Sequence[str], rel: str) -> None:
        """The hard floor, applied to a path's components relative to whichever
        root contains it - the project's or a skill folder's. Sealed means
        sealed everywhere: .agentclip is where the backups, the transcript and
        the approval rules live, and a skill folder is no more allowed to hold a
        readable one than the project is."""
        for part in parts:
            if part in self.hard_excludes:
                raise SandboxViolation(f"path {rel!r} is under sealed entry {part!r}")

    def _check_write_allowed(self, candidate: Path, rel: str) -> None:
        """Step 4, write flavor: no component may be an excluded name at all."""
        for part in candidate.relative_to(self.root).parts:
            if part in self.excludes:
                raise SandboxViolation(f"path {rel!r} is under excluded entry {part!r}")
