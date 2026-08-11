"""run_command: command execution with merged output and tail capping.

The allowlist/approval decision is NOT made here - the engine gates the call
before the handler runs. This handler just executes:

- ctx.host.spawn(command, cwd=workspace.root), stdout+stderr merged so
  interleaving survives;
- timeout param (seconds, default 60) capped by limits.command_timeout_s;
- output TAIL-capped (build/test verdicts live at the end) to
  caps.command_tail_lines / caps.command_tail_chars;
- first body line is "exit N (X.Xs)"; a timeout becomes an exec_timeout
  error carrying the partial tail.

This is the one handler that can run for minutes, so it is also the one that
can be interrupted: instead of a single blocking wait it waits in short slices,
checking BOTH the deadline and ctx.cancelled() between them. A cancelled
command is killed exactly like a timed out one but reported as code=cancelled,
so the model can tell "your command was too slow" apart from "the user stopped
you".

Everything here is host-agnostic: the spawn/wait/kill/drain handle comes from
ctx.host, so the same polling loop drives a local subprocess or a remote one.
"""

from __future__ import annotations

import time

from agentclip.hosts.base import ExecHandle
from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import (
    ToolContext,
    ToolError,
    ToolSpec,
    int_param,
    require,
    tool_handler,
)

_DEFAULT_TIMEOUT_S = 60
# How long each wait slice is: the deadline and the user's cancel are both
# re-checked this often, so a cancel lands within roughly this long. Repeated
# wait() calls are safe - buffered output survives across them.
_POLL_SLICE_S = 0.2
# How long the post-kill drain waits for the dying process's buffered output.
_DRAIN_S = 5.0


def _tail_cap(text: str, max_lines: int, max_chars: int) -> str:
    """Keep the tail of text within the line and char caps, with an in-band marker."""
    lines = text.replace("\r\n", "\n").splitlines()
    total = len(lines)
    kept = lines[-max_lines:] if total > max_lines else list(lines)
    joined = "\n".join(kept)
    chars_cut = False
    while len(joined) > max_chars and len(kept) > 1:
        kept.pop(0)
        joined = "\n".join(kept)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
        chars_cut = True
    if len(kept) < total or chars_cut:
        return f"{joined}\n[truncated: showing last {len(kept)} of {total} output lines]"
    return joined


def _effective_timeout(ctx: ToolContext, call: ToolCall) -> int:
    requested = int_param(call, "timeout", _DEFAULT_TIMEOUT_S)
    if requested < 1:
        raise ToolError(
            "bad_param", "timeout must be >= 1 second", "resend with a positive timeout."
        )
    return min(requested, ctx.limits.command_timeout_s)


def _kill_and_drain(ctx: ToolContext, handle: ExecHandle, max_chars: int) -> str:
    """Kill the process tree and return the tail of whatever it managed to emit."""
    handle.kill()
    return _tail_cap(handle.drain(_DRAIN_S), ctx.caps.command_tail_lines, max_chars)


@tool_handler
def run_command(ctx: ToolContext, call: ToolCall) -> str:
    (command,) = require(call, "command")
    timeout_s = _effective_timeout(ctx, call)
    max_chars = min(ctx.caps.command_tail_chars, ctx.limits.max_command_output_chars)

    start = time.monotonic()
    deadline = start + timeout_s
    handle = ctx.host.spawn(command, ctx.workspace.root)
    while True:
        slice_s = max(0.0, min(_POLL_SLICE_S, deadline - time.monotonic()))
        finished = handle.wait(slice_s)
        if finished is not None:
            break
        # The user's cancel wins over the deadline: it is the more specific
        # story, and it is the one they are waiting to see.
        if ctx.cancelled():
            partial = _kill_and_drain(ctx, handle, max_chars)
            waited = time.monotonic() - start
            message = (
                f"command cancelled by the user before completion (killed after {waited:.1f}s)"
            )
            if partial:
                message += f"\npartial output (tail):\n{partial}"
            raise ToolError(
                "cancelled",
                message,
                "the user stopped this deliberately - do not re-run it unchanged;"
                " ask what they want instead.",
            ) from None
        if time.monotonic() >= deadline:
            partial = _kill_and_drain(ctx, handle, max_chars)
            message = f"command timed out after {timeout_s}s"
            if partial:
                message += f"\npartial output (tail):\n{partial}"
            raise ToolError(
                "exec_timeout",
                message,
                f"raise timeout (limit {ctx.limits.command_timeout_s}s)"
                " or run a narrower command.",
            ) from None
    elapsed = time.monotonic() - start

    tail = _tail_cap(finished.output, ctx.caps.command_tail_lines, max_chars)
    body = f"exit {finished.exit_code} ({elapsed:.1f}s)"
    if tail:
        body += f"\n{tail}"
    return body


RUN_COMMAND_DOC = """\
run_command(command*, timeout)
  Run a shell command from the project root (timeout in seconds, default
  60). Returns "exit N (X.Xs)" plus merged stdout+stderr, tail-capped (the
  end of the output - where test verdicts live - always survives). A timed
  out command is killed and reported as exec_timeout with the partial tail;
  one the user cancels is killed the same way and reported as cancelled.
  NEVER modify files with commands (no sed/redirects/rm) - use
  write_file/edit_file/delete_file so every change is backed up.
===CLIP:CALL id=1 tool=run_command===
command: pytest tests/ -q
===CLIP:END==="""


def preview_run_command(ctx: ToolContext, call: ToolCall) -> str:
    command = call.params.get("command", "(missing command parameter)")
    return f"{command}\ncwd: {ctx.workspace.root}"


RUN_COMMAND_SPEC = ToolSpec(
    "run_command", "command", run_command, preview_run_command, RUN_COMMAND_DOC
)
