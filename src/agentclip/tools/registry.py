"""Tool registry: ToolSpec/ToolContext, the name->spec map, and the catalog text.

Also home to the small helpers every handler shares (error/ok result
construction, required-param checks, the guard decorator) so fs_tools, shell,
and meta cannot drift apart in how they report failures:

- every error body ends with a "hint: <next action>" line;
- SandboxViolation always maps to code=path_outside_workspace;
- a missing required param is always code=missing_param naming the param.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentclip.config import BudgetCaps, LimitsConfig
from agentclip.protocol.types import ToolCall, ToolResult
from agentclip.tools.sandbox import SandboxViolation, Workspace

if TYPE_CHECKING:
    from agentclip.tools.skills import Skill


@dataclass(slots=True)
class ToolContext:
    """Everything a handler may touch. The engine builds one per session.

    backup_hook(rel_path, abs_path, action) with action in {"write", "delete"}:
    the engine wires this to the BackupStore; mutating handlers MUST call it
    before first touching a file.
    """

    workspace: Workspace
    limits: LimitsConfig
    caps: BudgetCaps
    backup_hook: Callable[[str, Path, str], None] | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    approval_kind: Literal["auto", "edit", "command"]  # edit = write_file/edit_file/delete_file
    handler: Callable[[ToolContext, ToolCall], ToolResult]
    preview: Callable[[ToolContext, ToolCall], str] | None  # gated tools: diff / command line
    catalog_doc: str  # bootstrap section-4 entry incl. worked example


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"duplicate tool name: {spec.name}")
            self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def render_catalog(self) -> str:
        """Bootstrap section 4 body: the catalog_docs joined, ~4200 chars total."""
        return "\n\n".join(spec.catalog_doc for spec in self._specs.values())


def default_registry(
    skills: Sequence[Skill] = (), *, max_skill_listing_chars: int | None = None
) -> ToolRegistry:
    """The built-in tools, in catalog order. When any model-invocable skills are
    discovered, a `skill` tool is inserted (after run_command, before the meta
    tools) so the catalog advertises them and the model can load one on demand.

    `max_skill_listing_chars` bounds the total skill listing so a large skills
    library cannot push the bootstrap past the paste budget (the bootstrap has
    no truncation fallback); callers derive it from the active preset budget.
    """
    # Local imports: fs_tools/shell/meta/skills import helpers from this module.
    from agentclip.tools import fs_tools, meta, shell
    from agentclip.tools.skills import make_skill_spec

    specs: list[ToolSpec] = [
        fs_tools.READ_FILE_SPEC,
        fs_tools.WRITE_FILE_SPEC,
        fs_tools.EDIT_FILE_SPEC,
        fs_tools.DELETE_FILE_SPEC,
        fs_tools.LIST_DIR_SPEC,
        fs_tools.GLOB_SPEC,
        fs_tools.GREP_SPEC,
        shell.RUN_COMMAND_SPEC,
    ]
    listable = [s for s in skills if s.model_invocable]
    if listable:
        specs.append(make_skill_spec(listable, max_listing_chars=max_skill_listing_chars))
    specs.extend((meta.ASK_USER_SPEC, meta.TASK_DONE_SPEC))
    return ToolRegistry(specs)


# -- shared handler plumbing -------------------------------------------------


class ToolError(Exception):
    """Raised inside handlers; the guard decorator turns it into an error result."""

    def __init__(self, code: str, message: str, hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def ok_result(call: ToolCall, body: str) -> ToolResult:
    return ToolResult(call_id=call.id, status="ok", body=body, tool=call.tool)


def error_result(call: ToolCall, code: str, message: str, hint: str) -> ToolResult:
    body = f"{message.rstrip()}\nhint: {hint}"
    return ToolResult(call_id=call.id, status="error", body=body, tool=call.tool, code=code)


def tool_handler(
    fn: Callable[[ToolContext, ToolCall], str],
) -> Callable[[ToolContext, ToolCall], ToolResult]:
    """Wrap an implementation returning a body string into a full handler.

    Catches the failure modes every tool shares and maps them onto the closed
    error-code set.
    """

    @functools.wraps(fn)
    def wrapper(ctx: ToolContext, call: ToolCall) -> ToolResult:
        try:
            return ok_result(call, fn(ctx, call))
        except ToolError as exc:
            return error_result(call, exc.code, exc.message, exc.hint)
        except SandboxViolation as exc:
            return error_result(
                call,
                "path_outside_workspace",
                f"path not allowed: {exc.detail}",
                "use a relative path inside the project root and avoid excluded directories.",
            )
        except OSError as exc:
            return error_result(
                call,
                "bad_param",
                f"OS error: {exc}",
                "check the path/arguments and resend the call.",
            )

    return wrapper


def require(call: ToolCall, *names: str) -> tuple[str, ...]:
    """Return the named params; raise missing_param naming the first absent one."""
    values: list[str] = []
    for name in names:
        if name not in call.params:
            raise ToolError(
                "missing_param",
                f"missing required parameter: {name}",
                f"resend the call with all required parameters: {', '.join(names)}.",
            )
        values.append(call.params[name])
    return tuple(values)


def int_param(call: ToolCall, name: str, default: int) -> int:
    raw = call.params.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ToolError(
            "bad_param",
            f"parameter {name!r} must be an integer, got {raw!r}",
            f"resend with a numeric {name}.",
        ) from None
