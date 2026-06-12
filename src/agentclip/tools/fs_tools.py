"""Filesystem tools: read_file, write_file, edit_file, delete_file, list_dir, glob, grep.

Semantics are normative per docs/design/protocol.md section 3. Mutating
handlers call ctx.backup_hook(rel, abs_path, action) BEFORE first touching the
file. Preview functions share the same compute paths as the handlers
(_apply_edit, _planned_write) so a preview can never diverge from execution.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import (
    ToolContext,
    ToolError,
    ToolSpec,
    int_param,
    require,
    tool_handler,
)
from agentclip.tools.sandbox import SandboxViolation

_BINARY_SNIFF_BYTES = 8192


# -- small shared helpers ------------------------------------------------------


def _is_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(_BINARY_SNIFF_BYTES)


def _read_norm(path: Path) -> tuple[str, str]:
    """Read text normalized to LF; return (text, original newline style)."""
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write_norm(path: Path, text: str, newline: str) -> None:
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _require_text_file(path: Path, disp: str) -> None:
    if not path.exists():
        raise ToolError(
            "file_not_found",
            f"file not found: {disp}",
            "check the path with list_dir or glob, then resend.",
        )
    if path.is_dir():
        raise ToolError(
            "bad_param",
            f"{disp} is a directory, not a file",
            "use list_dir for directories.",
        )
    if _is_binary(path):
        raise ToolError(
            "binary_file",
            f"{disp} is a binary file",
            "binary files cannot be read or edited; work with text files only.",
        )


def _rel_display(ctx: ToolContext, abs_path: Path) -> str:
    return abs_path.relative_to(ctx.workspace.root).as_posix()


def _human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(n)} B"  # unreachable


def _unified_diff(old: str, new: str, rel: str) -> str:
    lines = difflib.unified_diff(
        old.split("\n"), new.split("\n"), fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3, lineterm=""
    )
    return "\n".join(lines)


# -- read_file -----------------------------------------------------------------


@tool_handler
def read_file(ctx: ToolContext, call: ToolCall) -> str:
    (path_param,) = require(call, "path")
    abs_path = ctx.workspace.resolve_read(path_param)
    _require_text_file(abs_path, path_param)

    text, _ = _read_norm(abs_path)
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return f"{path_param} lines 0-0 of 0\n(empty file)"

    explicit = "start" in call.params or "end" in call.params
    start = int_param(call, "start", 1)
    end = int_param(call, "end", total if explicit else min(total, ctx.caps.read_file_span_lines))

    eff_start = min(max(start, 1), total)
    eff_end = min(max(end, eff_start), total)
    notes: list[str] = []
    if explicit and (eff_start, eff_end) != (start, end):
        notes.append(f"[note: requested lines {start}-{end} clamped to {eff_start}-{eff_end}]")
    elif not explicit and eff_end < total:
        notes.append(
            f"[truncated: showing lines 1-{eff_end} of {total}"
            " - request further ranges with start/end]"
        )

    selected = lines[eff_start - 1 : eff_end]
    char_cap = ctx.limits.max_file_read_chars
    if sum(len(line) + 1 for line in selected) > char_cap:
        kept: list[str] = []
        used = 0
        for line in selected:
            if used + len(line) + 1 > char_cap:
                break
            kept.append(line)
            used += len(line) + 1
        if not kept:  # single line longer than the cap
            kept = [selected[0][:char_cap]]
        new_end = eff_start + len(kept) - 1
        notes = [
            f"[truncated: showing lines {eff_start}-{new_end} of {total}"
            f" ({char_cap} char cap) - request narrower ranges]"
        ]
        selected, eff_end = kept, new_end

    body = f"{path_param} lines {eff_start}-{eff_end} of {total}\n" + "\n".join(selected)
    if notes:
        body += "\n" + "\n".join(notes)
    return body


# -- write_file ------------------------------------------------------------------


_WRITE_MODES = ("overwrite", "create", "append")


@dataclass(frozen=True, slots=True)
class _PlannedWrite:
    abs_path: Path
    rel: str
    mode: str
    existed: bool
    content: str


def _planned_write(ctx: ToolContext, call: ToolCall) -> _PlannedWrite:
    """Validation shared by the write_file handler and its preview."""
    path_param, content = require(call, "path", "content")
    mode = call.params.get("mode", "overwrite").strip().lower()
    if mode not in _WRITE_MODES:
        raise ToolError(
            "bad_param",
            f"mode must be one of overwrite|create|append, got {mode!r}",
            "resend with a valid mode (omit it for overwrite).",
        )
    abs_path = ctx.workspace.resolve_write(path_param)
    if abs_path.is_dir():
        raise ToolError(
            "bad_param",
            f"{path_param} is a directory",
            "write_file targets files; pick a file path.",
        )
    existed = abs_path.exists()
    if mode == "create" and existed:
        raise ToolError(
            "bad_param",
            f"file already exists: {path_param}",
            "use mode: overwrite to replace it, or mode: append to extend it.",
        )
    return _PlannedWrite(abs_path, _rel_display(ctx, abs_path), mode, existed, content)


@tool_handler
def write_file(ctx: ToolContext, call: ToolCall) -> str:
    plan = _planned_write(ctx, call)
    if ctx.backup_hook is not None:
        ctx.backup_hook(plan.rel, plan.abs_path, "write")
    plan.abs_path.parent.mkdir(parents=True, exist_ok=True)
    open_mode = "a" if plan.mode == "append" else "w"
    with open(plan.abs_path, open_mode, encoding="utf-8", newline="") as f:
        f.write(plan.content)
    word = "appended" if plan.mode == "append" else ("overwritten" if plan.existed else "created")
    n_lines = len(plan.content.splitlines())
    return f"wrote {n_lines} lines ({len(plan.content)} chars) to {plan.rel} ({word})"


def preview_write_file(ctx: ToolContext, call: ToolCall) -> str:
    try:
        plan = _planned_write(ctx, call)
        if not plan.existed:
            n_lines = len(plan.content.splitlines())
            return f"NEW FILE {plan.rel} ({n_lines} lines)\n{plan.content}"
        if _is_binary(plan.abs_path):
            return f"(cannot preview: {plan.rel} is binary and would be {plan.mode}d)"
        old, _ = _read_norm(plan.abs_path)
        new = old + plan.content if plan.mode == "append" else plan.content
        return _unified_diff(old, new, plan.rel)
    except (ToolError, SandboxViolation) as exc:
        return f"(write will fail: {exc})"
    except OSError as exc:
        return f"(cannot preview: {exc})"


# -- edit_file -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EditOutcome:
    new_text: str
    summary: str


def _literal_spans(text: str, find: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while (idx := text.find(find, pos)) != -1:
        spans.append((idx, idx + len(find)))
        pos = idx + len(find)
    return spans


def _ws_fallback_spans(text: str, find: str) -> list[tuple[int, int]]:
    """One fallback pass ignoring per-line trailing whitespace on both sides."""
    if not find.strip():
        return []
    pattern = r"\n".join(re.escape(line.rstrip()) + r"[ \t]*" for line in find.split("\n"))
    return [m.span() for m in re.finditer(pattern, text)]


def _near_miss(text: str, find: str) -> tuple[int, int, list[str]] | None:
    """Closest near-miss region (<=20 lines) via difflib.SequenceMatcher."""
    content_lines = text.split("\n")
    window = min(max(len(find.split("\n")), 1), 20)
    target = "\n".join(line.rstrip() for line in find.split("\n"))
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)
    best_ratio, best_start = 0.0, -1
    for i in range(max(1, len(content_lines) - window + 1)):
        matcher.set_seq1("\n".join(line.rstrip() for line in content_lines[i : i + window]))
        if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i
    if best_start < 0 or best_ratio < 0.4:
        return None
    # Pad up to 2 context lines on each side (region stays capped at 20 lines).
    pad = min(2, (20 - window) // 2)
    lo = max(0, best_start - pad)
    hi = min(len(content_lines), best_start + window + pad)
    region = content_lines[lo:hi]
    return lo + 1, lo + len(region), region


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _apply_edit(text: str, call: ToolCall, disp: str) -> _EditOutcome:
    """Shared by the edit_file handler and its preview. Raises ToolError."""
    _, find, replace = require(call, "path", "find", "replace")
    if find == "":
        raise ToolError(
            "bad_param",
            "find must not be empty",
            "resend with the exact text to replace in find.",
        )
    spans = _literal_spans(text, find)
    fallback = False
    if not spans:
        spans = _ws_fallback_spans(text, find)
        fallback = bool(spans)
    if not spans:
        near = _near_miss(text, find)
        if near is None:
            message = f"find-block not found in {disp}.\nNo similar region found."
            hint = "re-read the file with read_file; its content may differ from what you expect."
        else:
            lo, hi, region = near
            message = (
                f"find-block not found in {disp}.\n"
                f"Closest near-miss at lines {lo}-{hi}:\n" + "\n".join(region)
            )
            hint = f"re-read lines {lo}-{hi} with read_file and resend the exact text."
        raise ToolError("match_not_found", message, hint)

    occurrence = call.params.get("occurrence")
    if occurrence is None:
        if len(spans) > 1:
            line_list = ", ".join(str(_line_of(text, s)) for s, _ in spans)
            raise ToolError(
                "multiple_matches",
                f"find-block matches {len(spans)} times in {disp} at lines {line_list}.",
                "add surrounding lines to make it unique, or set occurrence: N|first|all.",
            )
        chosen = spans
    else:
        occ = occurrence.strip().lower()
        if occ == "first":
            chosen = spans[:1]
        elif occ == "all":
            chosen = spans
        elif occ.isdigit() and int(occ) >= 1:
            n = int(occ)
            if n > len(spans):
                raise ToolError(
                    "bad_param",
                    f"occurrence {n} requested but only {len(spans)} match(es) in {disp}",
                    f"use an occurrence between 1 and {len(spans)}, or 'all'.",
                )
            chosen = [spans[n - 1]]
        else:
            raise ToolError(
                "bad_param",
                f"occurrence must be a positive number, 'first', or 'all'; got {occurrence!r}",
                "resend with a valid occurrence value.",
            )

    new_text = text
    for s, e in reversed(chosen):
        new_text = new_text[:s] + replace + new_text[e:]
    line_list = ", ".join(str(_line_of(text, s)) for s, _ in chosen)
    plural = "s" if len(chosen) != 1 else ""
    summary = f"replaced {len(chosen)} occurrence{plural} at line{plural} {line_list}"
    if fallback:
        summary += " (matched ignoring trailing whitespace)"
    return _EditOutcome(new_text, summary)


@tool_handler
def edit_file(ctx: ToolContext, call: ToolCall) -> str:
    (path_param,) = require(call, "path")
    abs_path = ctx.workspace.resolve_write(path_param)
    _require_text_file(abs_path, path_param)
    text, newline = _read_norm(abs_path)
    outcome = _apply_edit(text, call, path_param)
    rel = _rel_display(ctx, abs_path)
    if ctx.backup_hook is not None:
        ctx.backup_hook(rel, abs_path, "write")
    _write_norm(abs_path, outcome.new_text, newline)
    return outcome.summary


def preview_edit_file(ctx: ToolContext, call: ToolCall) -> str:
    try:
        (path_param,) = require(call, "path")
        abs_path = ctx.workspace.resolve_write(path_param)
        _require_text_file(abs_path, path_param)
        text, _ = _read_norm(abs_path)
        outcome = _apply_edit(text, call, path_param)
        return _unified_diff(text, outcome.new_text, _rel_display(ctx, abs_path))
    except ToolError as exc:
        return f"(edit will fail: {exc.code})\n{exc.message}"
    except SandboxViolation as exc:
        return f"(edit will fail: path_outside_workspace)\n{exc.detail}"
    except OSError as exc:
        return f"(cannot preview: {exc})"


# -- delete_file -----------------------------------------------------------------


@tool_handler
def delete_file(ctx: ToolContext, call: ToolCall) -> str:
    (path_param,) = require(call, "path")
    abs_path = ctx.workspace.resolve_write(path_param)
    if not abs_path.exists():
        raise ToolError(
            "file_not_found",
            f"file not found: {path_param}",
            "check the path with list_dir or glob; it may already be gone.",
        )
    if abs_path.is_dir():
        raise ToolError(
            "bad_param",
            f"{path_param} is a directory",
            "delete_file only deletes single files.",
        )
    rel = _rel_display(ctx, abs_path)
    if ctx.backup_hook is not None:
        ctx.backup_hook(rel, abs_path, "delete")
    abs_path.unlink()
    return f"deleted {rel} (backed up)"


def preview_delete_file(ctx: ToolContext, call: ToolCall) -> str:
    try:
        (path_param,) = require(call, "path")
        abs_path = ctx.workspace.resolve_write(path_param)
        if not abs_path.is_file():
            return f"(delete will fail: file not found: {path_param})"
        rel = _rel_display(ctx, abs_path)
        if _is_binary(abs_path):
            return f"DELETE {rel} (binary, {_human_size(abs_path.stat().st_size)})"
        text, _ = _read_norm(abs_path)
        return f"DELETE {rel} ({len(text.splitlines())} lines)"
    except (ToolError, SandboxViolation, OSError) as exc:
        return f"(delete will fail: {exc})"


# -- list_dir --------------------------------------------------------------------


@tool_handler
def list_dir(ctx: ToolContext, call: ToolCall) -> str:
    path_param = call.params.get("path", ".")
    depth = int_param(call, "depth", 1)
    clamped_depth = min(max(depth, 1), 3)
    base = ctx.workspace.resolve_read(path_param)
    if not base.exists():
        raise ToolError(
            "file_not_found",
            f"directory not found: {path_param}",
            "check the path with glob or a shallower list_dir.",
        )
    if not base.is_dir():
        raise ToolError(
            "bad_param",
            f"{path_param} is a file, not a directory",
            "use read_file for files.",
        )

    cap = ctx.caps.listing_max_entries
    lines: list[str] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if len(lines) >= cap:
                truncated = True
                return
            indent = "  " * level
            if child.is_dir():
                if ctx.workspace.is_excluded(child):
                    lines.append(f"{indent}{child.name}/ (excluded, not listed)")
                    continue
                lines.append(f"{indent}{child.name}/")
                if level + 1 < clamped_depth:
                    walk(child, level + 1)
            else:
                if ctx.workspace.is_excluded(child):
                    continue
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{indent}{child.name} ({_human_size(size)})")

    walk(base, 0)
    if not lines:
        return f"{path_param}: (empty)"
    notes: list[str] = []
    if depth != clamped_depth:
        notes.append(f"[note: depth {depth} clamped to {clamped_depth} (max 3)]")
    if truncated:
        notes.append(
            f"[truncated: listing capped at {cap} entries - list subdirectories directly]"
        )
    return "\n".join(lines + notes)


# -- glob ------------------------------------------------------------------------


@tool_handler
def glob(ctx: ToolContext, call: ToolCall) -> str:
    (pattern,) = require(call, "pattern")
    root_param = call.params.get("root", ".")
    base = ctx.workspace.resolve_read(root_param)
    if not base.is_dir():
        raise ToolError(
            "file_not_found" if not base.exists() else "bad_param",
            f"glob root is not a directory: {root_param}",
            "pass a directory (or omit root for the project root).",
        )

    norm = pattern.strip().replace("\\", "/")
    if norm.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", norm) or "\x00" in norm:
        raise ToolError(
            "bad_param",
            f"pattern must be relative: {pattern!r}",
            "use a pattern relative to root, e.g. src/**/*.py.",
        )
    if ".." in norm.split("/"):
        raise ToolError(
            "bad_param",
            f"pattern may not contain '..': {pattern!r}",
            "glob only searches inside the project root.",
        )
    try:
        found = [
            p
            for p in base.glob(norm)
            if p.is_relative_to(ctx.workspace.root) and not ctx.workspace.is_excluded(p)
        ]
    except (ValueError, NotImplementedError) as exc:
        raise ToolError(
            "bad_param", f"invalid glob pattern {pattern!r}: {exc}", "fix the pattern and resend."
        ) from None

    found.sort(key=lambda p: p.as_posix())
    cap = ctx.caps.listing_max_entries
    shown = found[:cap]
    lines = [
        _rel_display(ctx, p) + ("/" if p.is_dir() else "") for p in shown
    ]
    if len(found) > cap:
        lines.append(
            f"[truncated: showing first {cap} of {len(found)} matches - narrow the pattern]"
        )
    lines.append(f"{len(found)} matches")
    return "\n".join(lines)


# -- grep ------------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in ("yes", "true", "1", "on")


def _grep_files(ctx: ToolContext, target: Path, name_glob: str | None) -> list[Path]:
    if target.is_file():
        return [target]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(target):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not ctx.workspace.is_excluded(here / d))
        for name in sorted(filenames):
            if ctx.workspace.is_excluded(here / name):
                continue
            if name_glob and not fnmatch.fnmatch(name, name_glob):
                continue
            files.append(here / name)
    return files


@tool_handler
def grep(ctx: ToolContext, call: ToolCall) -> str:
    (pattern,) = require(call, "pattern")
    flags = re.IGNORECASE if _truthy(call.params.get("ignore_case")) else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(
            "bad_param", f"invalid regex: {exc}", "fix the regular expression and resend."
        ) from None

    context = int_param(call, "context", 0)
    if context < 0:
        raise ToolError("bad_param", "context must be >= 0", "resend with context: 0 or higher.")
    cap = min(ctx.limits.max_grep_matches, ctx.caps.grep_max_hits)
    if "max" in call.params:
        max_p = int_param(call, "max", cap)
        if max_p < 1:
            raise ToolError("bad_param", "max must be >= 1", "resend with a positive max.")
        cap = min(cap, max_p)

    path_param = call.params.get("path", ".")
    target = ctx.workspace.resolve_read(path_param)
    if not target.exists():
        raise ToolError(
            "file_not_found",
            f"path not found: {path_param}",
            "check the path with list_dir or glob.",
        )

    out: list[str] = []
    remaining = cap
    truncated = False
    for fp in _grep_files(ctx, target, call.params.get("glob")):
        try:
            if _is_binary(fp):
                continue
            text, _ = _read_norm(fp)
        except OSError:
            continue
        lines = text.splitlines()
        hit_lines = [i + 1 for i, line in enumerate(lines) if rx.search(line)]
        if not hit_lines:
            continue
        if remaining == 0:
            truncated = True
            break
        take = hit_lines[:remaining]
        if len(hit_lines) > len(take):
            truncated = True
        rel = _rel_display(ctx, fp)
        hit_set = set(take)
        show: set[int] = set()
        for hit in take:
            show.update(range(max(1, hit - context), min(len(lines), hit + context) + 1))
        for ln in sorted(show):
            sep = ":" if ln in hit_set else "-"
            out.append(f"{rel}:{ln}{sep} {lines[ln - 1]}")
        remaining -= len(take)

    if not out:
        return "no matches"
    if truncated:
        out.append(
            f"[truncated: showing first {cap} matches"
            " - narrow the pattern, filter with glob, or set max]"
        )
    return "\n".join(out)


# -- catalog docs + specs ---------------------------------------------------------

READ_FILE_DOC = """\
read_file(path*, start, end)
  Read a text file. start/end are 1-based inclusive line numbers; omit them
  for the default span. The first result line is "<path> lines A-B of N";
  the content has no line-number gutter - get line numbers from grep.
  Out-of-range values are clamped with a note; long files are truncated with
  an in-band [truncated: ...] line - re-request narrower ranges. Binary
  files are refused (error binary_file).
===CLIP:CALL id=1 tool=read_file===
path: src/utils.py
start: 80
end: 140
===CLIP:END==="""

WRITE_FILE_DOC = """\
write_file(path*, content*, mode)
  Write a whole file. mode: overwrite (default) | create (errors if the file
  exists) | append (adds to the end - the escape hatch for files too large
  for one reply: send the first part with mode: create, the rest with
  mode: append). Parent directories are created automatically. content must
  be a heredoc. Result: "wrote N lines (M chars) to <path> (created)".
===CLIP:CALL id=1 tool=write_file===
path: src/new.py
mode: create
content <<EOT
print("hello")
EOT
===CLIP:END==="""

EDIT_FILE_DOC = """\
edit_file(path*, find*, replace*, occurrence)
  Replace find with replace, both heredocs copied VERBATIM (exact
  indentation; trailing whitespace is forgiven). By default find must match
  exactly once: on multiple_matches you get the line numbers back - add
  surrounding lines or set occurrence: N|first|all; on match_not_found you
  get the closest near-miss region - re-read it, then resend exact text.
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return parse(s, OLD_FMT)
EOT
replace <<EOT
    return parse(s, NEW_FMT)
EOT
===CLIP:END==="""

DELETE_FILE_DOC = """\
delete_file(path*)
  Delete one file (it is backed up first, so this is reversible). Never
  delete via run_command.
===CLIP:CALL id=1 tool=delete_file===
path: src/old.py
===CLIP:END==="""

LIST_DIR_DOC = """\
list_dir(path, depth)
  Directory tree (path defaults to the project root; depth default 1, max 3).
  Dirs end with /, files show sizes. Excluded dirs (.git, node_modules, ...)
  are skipped with a note.
===CLIP:CALL id=1 tool=list_dir===
path: src
depth: 2
===CLIP:END==="""

GLOB_DOC = """\
glob(pattern*, root)
  Find files by shell pattern (** allowed), relative to root (default:
  project root). One path per line plus an "N matches" footer; long listings
  are capped with an in-band note.
===CLIP:CALL id=1 tool=glob===
pattern: src/**/*.py
===CLIP:END==="""

GREP_DOC = """\
grep(pattern*, path, glob, ignore_case, context, max)
  Regex search in path (file or directory, default project root); glob
  filters file names, ignore_case: yes, context: N extra lines, max caps
  hits. Hits print as path:lineno: text (context lines use - after the
  number). This is how you learn line numbers for ranged reads and edits.
===CLIP:CALL id=1 tool=grep===
pattern: def parse_date
glob: *.py
context: 2
===CLIP:END==="""


READ_FILE_SPEC = ToolSpec("read_file", "auto", read_file, None, READ_FILE_DOC)
WRITE_FILE_SPEC = ToolSpec("write_file", "edit", write_file, preview_write_file, WRITE_FILE_DOC)
EDIT_FILE_SPEC = ToolSpec("edit_file", "edit", edit_file, preview_edit_file, EDIT_FILE_DOC)
DELETE_FILE_SPEC = ToolSpec(
    "delete_file", "edit", delete_file, preview_delete_file, DELETE_FILE_DOC
)
LIST_DIR_SPEC = ToolSpec("list_dir", "auto", list_dir, None, LIST_DIR_DOC)
GLOB_SPEC = ToolSpec("glob", "auto", glob, None, GLOB_DOC)
GREP_SPEC = ToolSpec("grep", "auto", grep, None, GREP_DOC)
