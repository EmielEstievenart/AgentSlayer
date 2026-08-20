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
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agentclip.config import BudgetCaps, LimitsConfig
from agentclip.executor.hosts.base import Host
from agentclip.executor.hosts.local import LocalHost
from agentclip.executor.tools.sandbox import SandboxViolation, Workspace
from agentclip.protocol.types import ToolCall, ToolResult

if TYPE_CHECKING:
    from agentclip.executor.tools.chunks import CachedChunks
    from agentclip.executor.tools.skills import Skill


@dataclass(slots=True)
class ToolContext:
    """Everything a handler may touch. The engine builds one per session.

    host is the machine the project lives on - the ONLY way a handler may touch
    the filesystem or run a command. Local by default; a remote session swaps in
    a different implementation and every tool follows (docs/design/remote-ssh.md).

    backup_hook(rel_path, abs_path, action) with action in {"write", "delete"}:
    the engine wires this to the BackupStore; mutating handlers MUST call it
    before first touching a file.

    cancel_event: set from ANOTHER thread when the user cancels the running
    batch (Engine.request_cancel). Long-running handlers must poll it via
    ``ctx.cancelled()`` and bail out with a code=cancelled error carrying
    whatever partial output they have; fast handlers can ignore it (the engine
    checks it between calls anyway).

    on_output(call_id, chunk): the live tail. A handler that produces output
    over time (run_command, and only run_command today) hands over the NEW
    characters since it last called - never the whole buffer - so the UI can
    show a build scrolling instead of a frozen spinner. Read by the UI thread's
    side of the fence and therefore the mirror image of cancel_event: this one
    is called FROM the engine's worker thread and must not block. It is a
    courtesy channel, not a result: the model's copy of the output still comes
    back in the ToolResult, so nothing is lost when it is None (the default) or
    when the host cannot stream (ExecHandle.peek).

    numbered_slices maps a planned call's id to the exact text the model was
    SHOWN for the lines that call is about to overwrite - the engine fills it
    while it builds the plan, from its record of the numbered reads that
    survived into the previous payload (engine/numbered.py). replace_lines
    compares it against the file at the instant of the write, which is the only
    way to catch a file mutated by an earlier call in the SAME reply; empty (the
    default) simply means nobody is making that promise, and the tool trusts the
    range it was given.

    chunk_cache is what `fetch_chunk` reads: the full text of the bodies the
    last payload had to truncate, keyed by the id its marker names. It is the
    one field here that is deliberately CROSS-TURN, and the shape follows from
    that - `numbered_slices` is rebuilt per plan and so may be reassigned, but a
    fetch arrives one or more turns after the truncation that filled this, so
    the engine and the context must be looking at the SAME dict. The engine owns
    it, mutates it in place (never rebinds it), and writes the eviction rule
    where the eviction happens; a handler only ever reads.
    """

    workspace: Workspace
    limits: LimitsConfig
    caps: BudgetCaps
    host: Host = field(default_factory=LocalHost)
    backup_hook: Callable[[str, Path, str], None] | None = None
    cancel_event: threading.Event | None = None
    on_output: Callable[[int, str], None] | None = None
    numbered_slices: dict[int, str] = field(default_factory=dict)
    chunk_cache: dict[str, CachedChunks] = field(default_factory=dict)

    def cancelled(self) -> bool:
        """True once the user asked to cancel the batch this call belongs to."""
        return self.cancel_event is not None and self.cancel_event.is_set()

    def emit_output(self, call_id: int, chunk: str) -> None:
        """Push one delta at the live-output hook, if anyone is listening.

        Defensive by contract: the hook crosses into the UI layer, and a view
        that raises (a screen torn down mid-turn, say) must not turn a running
        command into a failed tool call.
        """
        hook = self.on_output
        if hook is None or not chunk:
            return
        try:
            hook(call_id, chunk)
        except Exception:  # noqa: BLE001 - a broken listener is not the command's problem
            # ...and it is not asked again: a listener that failed once will
            # fail five times a second for the rest of a long command.
            self.on_output = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    approval_kind: Literal["auto", "edit", "command"]  # edit = write_file/edit_file/delete_file
    handler: Callable[[ToolContext, ToolCall], ToolResult]
    preview: Callable[[ToolContext, ToolCall], str] | None  # gated tools: diff / command line
    # Bootstrap section-4 entry, normally including a worked example. The one
    # exception is `fetch_chunk`, whose syntax is taught by the truncation marker
    # at the moment it is needed rather than by an example nobody reads until
    # then (executor/tools/chunks.py, FETCH_CHUNK_DOC).
    catalog_doc: str


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
    skills: Sequence[Skill] = (),
    *,
    max_skill_listing_chars: int | None = None,
    role: Literal["master", "subagent"] = "master",
    allow_delegate: bool = False,
    mcp_specs: Sequence[ToolSpec] = (),
    edit_by_lines: bool = False,
) -> ToolRegistry:
    """The built-in tools, in catalog order. When any model-invocable skills are
    discovered, a `skill` tool is inserted (after run_command, before the meta
    tools) so the catalog advertises them and the model can load one on demand.

    `max_skill_listing_chars` bounds the total skill listing so a large skills
    library cannot push the bootstrap past the paste budget (the bootstrap has
    no truncation fallback); callers derive it from the active preset budget.

    `role` and `allow_delegate` gate sub-agent delegation. `delegate` is added
    only for a master whose sub-agent chat is fully calibrated: a sub-agent's
    registry never contains it, so a nested delegation resolves as the ordinary
    unknown_tool error listing the valid tools - nesting is excluded by
    construction rather than by a special case. `role` also selects the
    `task_done` catalog doc (sub-agents are taught the `result` param).

    `mcp_specs` are the already-sized mcp_schema/mcp ToolSpecs (or empty when
    MCP is unconfigured or the paste budget cannot hold them - the caller
    measures, docs/design/mcp.md section 5). They slot in after `skill` and
    before `delegate`/the meta tools: skills and MCP tools are both "extra
    capabilities this environment happens to have", so they read as one group
    behind the built-ins, while delegate/ask_user/task_done stay the catalog's
    closing "how to hand off and finish" block.

    `edit_by_lines` is the per-service ranged-edit mode (config.ServicePreset).
    On, it ADDS `replace_lines` behind `edit_file` - added, not swapped, because
    find/replace is still the better edit wherever the host can carry code
    faithfully - and swaps read_file's catalog entry for the one that teaches
    `numbered`. Off, this function returns exactly what it returned before the
    feature existed, character for character: the gutter is a liability on a
    host that does not need it (it contaminates the find-blocks a model copies
    back), and an unused catalog entry is bootstrap budget spent for nothing.
    """
    # Local imports: fs_tools/shell/meta/skills/chunks import helpers from this module.
    from agentclip.executor.tools import fs_tools, meta, shell
    from agentclip.executor.tools.chunks import FETCH_CHUNK_SPEC
    from agentclip.executor.tools.skills import make_skill_spec

    specs: list[ToolSpec] = [
        fs_tools.READ_FILE_NUMBERED_SPEC if edit_by_lines else fs_tools.READ_FILE_SPEC,
        fs_tools.WRITE_FILE_SPEC,
        fs_tools.EDIT_FILE_SPEC,
        *((fs_tools.REPLACE_LINES_SPEC,) if edit_by_lines else ()),
        fs_tools.DELETE_FILE_SPEC,
        fs_tools.LIST_DIR_SPEC,
        fs_tools.GLOB_SPEC,
        fs_tools.GREP_SPEC,
        shell.RUN_COMMAND_SPEC,
        # Last of the built-ins, and unconditional: it is the recovery path for
        # the tools above it, so it belongs beside them rather than in the
        # closing hand-off block, and no preset or permission can make a body
        # un-truncatable - a registry without it would have failure modes with
        # no way out.
        FETCH_CHUNK_SPEC,
    ]
    listable = [s for s in skills if s.model_invocable]
    if listable:
        specs.append(make_skill_spec(listable, max_listing_chars=max_skill_listing_chars))
    specs.extend(mcp_specs)
    if role == "master" and allow_delegate:
        specs.append(meta.DELEGATE_SPEC)
    specs.extend((meta.ASK_USER_SPEC, meta.task_done_spec(role)))
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
                "use a relative path inside the project root; excluded directories "
                "can be read from but not written to, and .agentclip is off limits.",
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
