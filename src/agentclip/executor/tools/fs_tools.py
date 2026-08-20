"""Filesystem tools: read_file, write_file, edit_file, replace_lines, delete_file,
list_dir, glob, grep.

Semantics are normative per docs/design/protocol.md section 3. Mutating
handlers call ctx.backup_hook(rel, abs_path, action) BEFORE first touching the
file. Preview functions share the same compute paths as the handlers
(_apply_edit, _apply_replace_lines, _planned_write) so a preview can never
diverge from execution.

read_file's `numbered` gutter and `replace_lines` are the two halves of ONE
feature, the opt-in ranged-edit mode of ServicePreset.edit_by_lines: a model on
a host that cannot echo code verbatim reads with the gutter and then edits by
line range, so the file's own text never has to survive the round trip. The
tools here are the mechanism only - the guarantee that a range was actually
READ before it is written is the engine's (engine/numbered.py), because only
the engine knows what the previous payload really contained.

Two resolvers, not one: read_file asks the Workspace for `resolve_read`, which
also reaches the discovered skill folders when handed an absolute path, while
list_dir/glob/grep ask for `resolve_scan`, which never does. A named read of a
side file is the whole point of that carve-out (executor/tools/skills.py); a
sweep pointed at a skill folder is not, and `_rel_display` below would have
nothing to render its hits relative to.

Every byte read, byte written and directory listed goes through ctx.host - the
tools themselves know nothing about which machine they run on. What stays here
is the part that is the same everywhere: decoding, the caps, the diff, the edit
matcher, glob's pruning walk and grep's regex scan (docs/design/remote-ssh.md).
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from agentclip.executor.hosts.base import DirEntry
from agentclip.executor.tools.registry import (
    ToolContext,
    ToolError,
    ToolSpec,
    int_param,
    require,
    tool_handler,
)
from agentclip.executor.tools.sandbox import SandboxViolation
from agentclip.protocol.types import ToolCall

_BINARY_SNIFF_BYTES = 8192

# What separates a `numbered` read's line number from the line itself. Public
# because the engine matches on it when it works out which numbered lines
# survived truncation into the payload the model was actually handed
# (engine/numbered.py). The trailing space is part of it: it keeps an empty
# source line from gluing the number to the next character of anything, and it
# is what makes the anchored gutter pattern unambiguous even when the file's
# own content looks like a gutter.
GUTTER_SEP = "| "


# -- small shared helpers ------------------------------------------------------


def _is_binary(ctx: ToolContext, path: Path) -> bool:
    return b"\x00" in ctx.host.read_bytes(path, max_bytes=_BINARY_SNIFF_BYTES)


def _read_norm(ctx: ToolContext, path: Path) -> tuple[str, str]:
    """Read text normalized to LF; return (text, original newline style)."""
    raw = ctx.host.read_bytes(path).decode("utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def _write_norm(ctx: ToolContext, path: Path, text: str, newline: str) -> None:
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    ctx.host.write_bytes(path, text.encode("utf-8"))


def split_lines(text: str) -> tuple[list[str], bool]:
    """LF-normalised text as its numbered lines, plus "did it end with a newline".

    Deliberately ``split("\\n")`` rather than ``splitlines()``. read_file's
    gutter and replace_lines' ranges have to agree, to the line, on what "line
    40" is: splitlines() ALSO breaks on form feeds, \\x1c-\\x1e and the unicode
    separators, none of which any editor - or the model counting the gutter -
    treats as a line. One rule, one place, both tools.

    The trailing flag is how the final newline survives a ranged edit: it is not
    a line, so it must not be counted as one, and it must not be lost either.
    """
    if text == "":
        return [], False
    parts = text.split("\n")
    trailing = len(parts) > 1 and parts[-1] == ""
    return (parts[:-1] if trailing else parts), trailing


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in ("yes", "true", "1", "on")


def numbered_requested(call: ToolCall) -> bool:
    """Did this read ask for the line-number gutter?

    Public because the engine asks the same question of the same call when it
    records what it served (engine/numbered.py), and "what counts as yes" must
    not be answered twice.
    """
    return call.tool == "read_file" and _truthy(call.params.get("numbered"))


def _require_text_file(ctx: ToolContext, path: Path, disp: str) -> None:
    st = ctx.host.stat(path)
    if st is None:
        raise ToolError(
            "file_not_found",
            f"file not found: {disp}",
            "check the path with list_dir or glob, then resend.",
        )
    if st.is_dir:
        raise ToolError(
            "bad_param",
            f"{disp} is a directory, not a file",
            "use list_dir for directories.",
        )
    if _is_binary(ctx, path):
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


def _scan(ctx: ToolContext, directory: Path) -> list[DirEntry]:
    """Children of a directory, or none if it cannot be read (pathlib skips those too)."""
    try:
        return ctx.host.listdir(directory)
    except OSError:
        return []


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
    _require_text_file(ctx, abs_path, path_param)

    text, _ = _read_norm(ctx, abs_path)
    lines, _ = split_lines(text)
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
    if _truthy(call.params.get("numbered")):
        # Applied to the SLICE and before the char cap, so the cap accounts for
        # what actually goes on the wire and the numbers survive truncation
        # attached to their own line - which is what lets the engine work out,
        # from the delivered payload alone, which lines the model really saw.
        width = len(str(eff_end))
        selected = [f"{eff_start + i:>{width}}{GUTTER_SEP}{line}" for i, line in enumerate(selected)]
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
    st = ctx.host.stat(abs_path)
    if st is not None and st.is_dir:
        raise ToolError(
            "bad_param",
            f"{path_param} is a directory",
            "write_file targets files; pick a file path.",
        )
    existed = st is not None
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
    # write_bytes creates the missing parent directories.
    ctx.host.write_bytes(
        plan.abs_path, plan.content.encode("utf-8"), append=plan.mode == "append"
    )
    word = "appended" if plan.mode == "append" else ("overwritten" if plan.existed else "created")
    n_lines = len(plan.content.splitlines())
    return f"wrote {n_lines} lines ({len(plan.content)} chars) to {plan.rel} ({word})"


def preview_write_file(ctx: ToolContext, call: ToolCall) -> str:
    try:
        plan = _planned_write(ctx, call)
        if not plan.existed:
            n_lines = len(plan.content.splitlines())
            return f"NEW FILE {plan.rel} ({n_lines} lines)\n{plan.content}"
        if _is_binary(ctx, plan.abs_path):
            return f"(cannot preview: {plan.rel} is binary and would be {plan.mode}d)"
        old, _ = _read_norm(ctx, plan.abs_path)
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
    _require_text_file(ctx, abs_path, path_param)
    text, newline = _read_norm(ctx, abs_path)
    outcome = _apply_edit(text, call, path_param)
    rel = _rel_display(ctx, abs_path)
    if ctx.backup_hook is not None:
        ctx.backup_hook(rel, abs_path, "write")
    _write_norm(ctx, abs_path, outcome.new_text, newline)
    return outcome.summary


def preview_edit_file(ctx: ToolContext, call: ToolCall) -> str:
    try:
        (path_param,) = require(call, "path")
        abs_path = ctx.workspace.resolve_write(path_param)
        _require_text_file(ctx, abs_path, path_param)
        text, _ = _read_norm(ctx, abs_path)
        outcome = _apply_edit(text, call, path_param)
        return _unified_diff(text, outcome.new_text, _rel_display(ctx, abs_path))
    except ToolError as exc:
        return f"(edit will fail: {exc.code})\n{exc.message}"
    except SandboxViolation as exc:
        return f"(edit will fail: path_outside_workspace)\n{exc.detail}"
    except OSError as exc:
        return f"(cannot preview: {exc})"


# -- replace_lines ---------------------------------------------------------------
#
# The other half of the `numbered` read. Everything this tool needs from the
# model is a path, two integers and the NEW code - no find-block, so nothing
# that came out of the file has to come back through a chat that mangles it.
#
# The safety of that trade lives in TWO places, and only one of them is here.
# The engine refuses a call whose range was not in the payload it just sent
# (engine/numbered.py); this file enforces what the engine cannot see from
# outside - that the bytes on disk at the moment of the write are still the
# bytes that were served. ctx.numbered_slices is how the engine hands that
# expectation down, and both the handler and the preview read it, so the diff
# in the approval drawer can never be of an edit that will not happen.


@dataclass(frozen=True, slots=True)
class _RangeEdit:
    new_text: str
    summary: str


def _apply_replace_lines(ctx: ToolContext, text: str, call: ToolCall, disp: str) -> _RangeEdit:
    """Shared by the replace_lines handler and its preview. Raises ToolError."""
    _, _, _, replacement = require(call, "path", "start", "end", "replace")
    start = int_param(call, "start", 1)
    end = int_param(call, "end", start)
    lines, trailing = split_lines(text)
    total = len(lines)
    if total == 0:
        raise ToolError(
            "bad_param",
            f"{disp} is empty - there are no lines to replace",
            "use write_file to put content in an empty file.",
        )
    if start < 1 or end < start or end > total:
        raise ToolError(
            "bad_param",
            f"lines {start}-{end} are not a range in {disp} ({total} lines)",
            f"start and end are 1-based and inclusive; use 1 <= start <= end <= {total}.",
        )
    expected = ctx.numbered_slices.get(call.id)
    if expected is not None and "\n".join(lines[start - 1 : end]) != expected:
        # The file moved under a range that WAS legitimately read - a
        # run_command earlier in this same reply, most likely. Loud, because the
        # alternative is writing the model's new code over lines it never saw.
        raise ToolError(
            "stale_read",
            f"{disp} changed since you read it: lines {start}-{end} are no longer"
            " the lines you were shown.",
            "read_file that range again with numbered: yes, then resend the edit.",
        )
    # An empty replace DELETES the range: dropping lines is the common ranged
    # edit, and a heredoc cannot express "one blank line" distinctly anyway
    # (its body is joined without a trailing newline, so blank and empty arrive
    # identical). Widen the range by a neighbour to write a blank line.
    new_lines = [] if replacement == "" else replacement.split("\n")
    merged = lines[: start - 1] + new_lines + lines[end:]
    new_text = "\n".join(merged)
    if trailing and merged:
        new_text += "\n"
    span = end - start + 1
    return _RangeEdit(
        new_text,
        f"replaced lines {start}-{end} of {disp} ({span} lines -> {len(new_lines)} lines)",
    )


@tool_handler
def replace_lines(ctx: ToolContext, call: ToolCall) -> str:
    (path_param,) = require(call, "path")
    abs_path = ctx.workspace.resolve_write(path_param)
    _require_text_file(ctx, abs_path, path_param)
    text, newline = _read_norm(ctx, abs_path)
    outcome = _apply_replace_lines(ctx, text, call, path_param)
    rel = _rel_display(ctx, abs_path)
    if ctx.backup_hook is not None:
        ctx.backup_hook(rel, abs_path, "write")
    _write_norm(ctx, abs_path, outcome.new_text, newline)
    return outcome.summary


def preview_replace_lines(ctx: ToolContext, call: ToolCall) -> str:
    try:
        (path_param,) = require(call, "path")
        abs_path = ctx.workspace.resolve_write(path_param)
        _require_text_file(ctx, abs_path, path_param)
        text, _ = _read_norm(ctx, abs_path)
        outcome = _apply_replace_lines(ctx, text, call, path_param)
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
    st = ctx.host.stat(abs_path)
    if st is None:
        raise ToolError(
            "file_not_found",
            f"file not found: {path_param}",
            "check the path with list_dir or glob; it may already be gone.",
        )
    if st.is_dir:
        raise ToolError(
            "bad_param",
            f"{path_param} is a directory",
            "delete_file only deletes single files.",
        )
    rel = _rel_display(ctx, abs_path)
    if ctx.backup_hook is not None:
        ctx.backup_hook(rel, abs_path, "delete")
    ctx.host.delete(abs_path)
    return f"deleted {rel} (backed up)"


def preview_delete_file(ctx: ToolContext, call: ToolCall) -> str:
    try:
        (path_param,) = require(call, "path")
        abs_path = ctx.workspace.resolve_write(path_param)
        st = ctx.host.stat(abs_path)
        if st is None or not st.is_file:
            return f"(delete will fail: file not found: {path_param})"
        rel = _rel_display(ctx, abs_path)
        if _is_binary(ctx, abs_path):
            return f"DELETE {rel} (binary, {_human_size(st.size)})"
        text, _ = _read_norm(ctx, abs_path)
        return f"DELETE {rel} ({len(text.splitlines())} lines)"
    except (ToolError, SandboxViolation, OSError) as exc:
        return f"(delete will fail: {exc})"


# -- list_dir --------------------------------------------------------------------


@tool_handler
def list_dir(ctx: ToolContext, call: ToolCall) -> str:
    path_param = call.params.get("path", ".")
    depth = int_param(call, "depth", 1)
    clamped_depth = min(max(depth, 1), 3)
    base = ctx.workspace.resolve_scan(path_param)
    base_stat = ctx.host.stat(base)
    if base_stat is None:
        raise ToolError(
            "file_not_found",
            f"directory not found: {path_param}",
            "check the path with glob or a shallower list_dir.",
        )
    if not base_stat.is_dir:
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
        children = sorted(_scan(ctx, directory), key=lambda e: (not e.is_dir, e.name.lower()))
        for entry in children:
            if len(lines) >= cap:
                truncated = True
                return
            indent = "  " * level
            child = directory / entry.name
            if entry.is_dir:
                if ctx.workspace.is_excluded(child):
                    lines.append(f"{indent}{entry.name}/ (excluded, not listed)")
                    continue
                lines.append(f"{indent}{entry.name}/")
                if level + 1 < clamped_depth:
                    walk(child, level + 1)
            else:
                if ctx.workspace.is_excluded(child):
                    continue
                lines.append(f"{indent}{entry.name} ({_human_size(entry.size)})")

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
#
# The pattern language is pathlib's, but the traversal is ours, because
# Path.glob offers no way to say "do not go in there". It walks everything the
# pattern reaches and hands back the lot; excluding .venv/.git/build afterwards
# still pays for reading them, and in a real project those ARE the tree - a
# single .venv is thousands of files, so `**/README*` spent seconds on
# directories whose every hit was destined for the bin. So, like _grep_files
# below, we prune: an excluded directory is never descended into at all.
#
# What follows therefore has to reproduce pathlib's matching rules exactly:
# '**' spans zero or more directories (so `**/README*` still finds a top-level
# README), a trailing '**' or '/' selects directories only, '**' beside other
# characters is an error, and each component is fnmatch against one name -
# case-insensitively on Windows, as pathlib is. The parity test in
# tests/executor/tools/test_fs_tools.py pins that agreement to Path.glob itself.
#
# Which of those two rules applies is a fact about the machine the FILES are on
# (ctx.host.case_sensitive), not about the one AgentClip runs on: a Windows
# operator globbing a Linux box must get Linux's answer, and Path.glob's local
# rule would tell them `*.PY` matches `main.py`.


def _glob_parts(norm: str) -> tuple[list[str], bool]:
    """Split a pattern into components; report whether it asked for directories only.

    Raises ValueError - in pathlib's own wording - for the patterns pathlib
    itself refuses, so the error the LLM sees is unchanged. The one deviation is
    a pattern that survives to nothing at all ("." or "./"), which pathlib
    answers with a bare IndexError; here it joins the other rejects.
    """
    dir_only = norm.endswith("/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"Unacceptable pattern: {norm!r}")
    for part in parts:
        if "**" in part and part != "**":
            raise ValueError("Invalid pattern: '**' can only be an entire path component")
    # Runs of '**' select the same directories as a single one; collapsing them
    # keeps the same file from arriving twice by two different splits.
    collapsed = [p for i, p in enumerate(parts) if p != "**" or i == 0 or parts[i - 1] != "**"]
    return collapsed, dir_only


def _glob_select(ctx: ToolContext, base: Path, parts: list[str], dir_only: bool) -> list[Path]:
    """Every path under base matching the parsed pattern, excluded subtrees unvisited."""
    # One compiled matcher per component, built once rather than per directory
    # entry. fnmatch.translate anchors its output, so .match is a full match.
    flags = 0 if ctx.host.case_sensitive else re.IGNORECASE
    matchers = [re.compile(fnmatch.translate(part), flags).match for part in parts]
    found: list[Path] = []

    def starting_points(directory: Path) -> list[Path]:
        """The directory itself plus every included directory beneath it - the span of '**'."""
        points = [directory]
        for entry in _scan(ctx, directory):
            # '**' recursion does not follow symlinked directories, as pathlib's
            # does not: a link back up the tree would never terminate.
            if not entry.is_dir or entry.is_symlink:
                continue
            child = directory / entry.name
            if ctx.workspace.is_excluded(child):
                continue  # the prune: nothing under here is ever read
            points.extend(starting_points(child))
        return points

    def select(directory: Path, index: int) -> None:
        last = index == len(parts) - 1
        if parts[index] == "**":
            for start in starting_points(directory):
                if last:
                    # A pattern ending in '**' selects directories, not files.
                    found.append(start)
                else:
                    select(start, index + 1)
            return
        match = matchers[index]
        for entry in _scan(ctx, directory):
            if not match(entry.name):
                continue
            child = directory / entry.name
            # Excluded FILES are skipped as well as excluded directories: an
            # entry named in the exclusion list is invisible whatever it is.
            if ctx.workspace.is_excluded(child):
                continue
            # Non-recursive components do follow symlinks, so src/*.py works
            # through a symlinked src.
            is_dir = entry.is_dir
            if last:
                if is_dir or not dir_only:
                    found.append(child)
            elif is_dir:
                select(child, index + 1)

    select(base, 0)
    # A path can be reachable by two different splits of a pattern with several
    # '**'s; pathlib reports it once, so do we.
    return list(dict.fromkeys(found))


def _dir_suffix(ctx: ToolContext, path: Path) -> str:
    """The trailing '/' a directory is listed with."""
    st = ctx.host.stat(path)
    return "/" if st is not None and st.is_dir else ""


@tool_handler
def glob(ctx: ToolContext, call: ToolCall) -> str:
    (pattern,) = require(call, "pattern")
    root_param = call.params.get("root", ".")
    base = ctx.workspace.resolve_scan(root_param)
    base_stat = ctx.host.stat(base)
    if base_stat is None or not base_stat.is_dir:
        raise ToolError(
            "file_not_found" if base_stat is None else "bad_param",
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
        parts, dir_only = _glob_parts(norm)
        found = [
            p
            for p in _glob_select(ctx, base, parts, dir_only)
            if p.is_relative_to(ctx.workspace.root)
        ]
    except (ValueError, NotImplementedError) as exc:
        raise ToolError(
            "bad_param", f"invalid glob pattern {pattern!r}: {exc}", "fix the pattern and resend."
        ) from None

    found.sort(key=lambda p: p.as_posix())
    cap = ctx.caps.listing_max_entries
    shown = found[:cap]
    lines = [_rel_display(ctx, p) + _dir_suffix(ctx, p) for p in shown]
    if len(found) > cap:
        lines.append(
            f"[truncated: showing first {cap} of {len(found)} matches - narrow the pattern]"
        )
    lines.append(f"{len(found)} matches")
    return "\n".join(lines)


# -- grep ------------------------------------------------------------------------


def _grep_files(ctx: ToolContext, target: Path, name_glob: str | None) -> list[Path]:
    """Every file to scan, in os.walk's order: a directory's own files (sorted)
    before its subdirectories (sorted), excluded subtrees never entered."""
    target_stat = ctx.host.stat(target)
    if target_stat is not None and target_stat.is_file:
        return [target]
    files: list[Path] = []

    def walk(directory: Path) -> None:
        subdirs: list[Path] = []
        for entry in sorted(_scan(ctx, directory), key=lambda e: e.name):
            child = directory / entry.name
            if ctx.workspace.is_excluded(child):
                continue
            if entry.is_dir:
                # A symlinked directory is listed but not descended into, as
                # os.walk(followlinks=False) has it: no loops back up the tree.
                if not entry.is_symlink:
                    subdirs.append(child)
                continue
            if name_glob and not fnmatch.fnmatch(entry.name, name_glob):
                continue
            files.append(child)
        for subdir in subdirs:
            walk(subdir)

    walk(target)
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
    target = ctx.workspace.resolve_scan(path_param)
    if ctx.host.stat(target) is None:
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
            if _is_binary(ctx, fp):
                continue
            text, _ = _read_norm(ctx, fp)
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

# The `numbered` variant, shown only when the service has edit_by_lines on
# (registry.default_registry). Two docs rather than one paragraph with an "if":
# with the toggle off the catalog must be byte-identical to a build that never
# heard of ranged edits, and a model that has no replace_lines has no reason to
# be told about a gutter that would only contaminate its find-blocks.
READ_FILE_NUMBERED_DOC = """\
read_file(path*, start, end, numbered)
  Read a text file. start/end are 1-based inclusive line numbers; omit them
  for the default span. The first result line is "<path> lines A-B of N".
  numbered: yes prefixes each line with its number and "| ". Read that way
  to edit with replace_lines; never copy a gutter into a call.
  Out-of-range values are clamped with a note; long files are truncated with
  an in-band [truncated: ...] line - re-request narrower ranges. Binary
  files are refused (error binary_file).
===CLIP:CALL id=1 tool=read_file===
path: src/utils.py
start: 80
end: 140
numbered: yes
===CLIP:END==="""

REPLACE_LINES_DOC = """\
replace_lines(path*, start*, end*, replace*)
  Replace lines start..end (1-based, inclusive) with replace, a heredoc of
  the NEW text only - the old text is never sent back, so this survives a
  host that mangles code. Empty replace deletes the range.
  The range must lie inside a `read_file numbered: yes` of that file in the
  results you were JUST handed, and the file must not have changed since.
  Several ranges in one file in one reply: BOTTOM TO TOP (highest start
  first), never overlapping. Refused? Re-read numbered and resend.
===CLIP:CALL id=1 tool=replace_lines===
path: src/utils.py
start: 88
end: 90
replace << EOT
def parse(s):
    return parse_iso(s)
EOT
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
content << EOT
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
find << EOT
    return parse(s, OLD_FMT)
EOT
replace << EOT
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
READ_FILE_NUMBERED_SPEC = ToolSpec("read_file", "auto", read_file, None, READ_FILE_NUMBERED_DOC)
REPLACE_LINES_SPEC = ToolSpec(
    "replace_lines", "edit", replace_lines, preview_replace_lines, REPLACE_LINES_DOC
)
WRITE_FILE_SPEC = ToolSpec("write_file", "edit", write_file, preview_write_file, WRITE_FILE_DOC)
EDIT_FILE_SPEC = ToolSpec("edit_file", "edit", edit_file, preview_edit_file, EDIT_FILE_DOC)
DELETE_FILE_SPEC = ToolSpec(
    "delete_file", "edit", delete_file, preview_delete_file, DELETE_FILE_DOC
)
LIST_DIR_SPEC = ToolSpec("list_dir", "auto", list_dir, None, LIST_DIR_DOC)
GLOB_SPEC = ToolSpec("glob", "auto", glob, None, GLOB_DOC)
GREP_SPEC = ToolSpec("grep", "auto", grep, None, GREP_DOC)
