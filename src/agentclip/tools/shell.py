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

The required `reason` param is the one thing here that never touches the
shell: the model states in one line why it wants this command, and the
approval drawer shows it next to the command, so the user is deciding on the
intent and not just on the syntax. It is display-only - never interpolated
into the command line, never logged into it.

This is the one handler that can run for minutes, so it is also the one that
can be interrupted: instead of a single blocking wait it waits in short slices,
checking BOTH the deadline and ctx.cancelled() between them. A cancelled
command is killed exactly like a timed out one but reported as code=cancelled,
so the model can tell "your command was too slow" apart from "the user stopped
you".

Being the one handler that runs for minutes also makes it the one worth
WATCHING, so each slice ends by peeking at the handle's buffer and pushing the
characters that appeared since the last look to ctx.on_output (the TUI's run
panel shows them live). That is a side channel and nothing more: the model's
copy of the output is still the tail-capped body composed at the end, and a
host that cannot stream simply shows nothing until then.

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


def _stream(ctx: ToolContext, call: ToolCall, handle: ExecHandle, sent: int) -> int:
    """Hand the UI the characters that appeared since the last look.

    Returns the new watermark. The whole snapshot is asked for and diffed here
    rather than in the host, because ``peek()`` is deliberately the simplest
    thing a remote transport can implement - "everything so far" - and only one
    caller cares about the difference.
    """
    if ctx.on_output is None:
        return sent
    snapshot = handle.peek()
    if len(snapshot) <= sent:
        return sent
    ctx.emit_output(call.id, snapshot[sent:])
    return len(snapshot)


@tool_handler
def run_command(ctx: ToolContext, call: ToolCall) -> str:
    # `reason` is required but never executed: it exists so the approval gate
    # can show the user WHY this command is being run, in the model's words.
    command, _reason = require(call, "command", "reason")
    timeout_s = _effective_timeout(ctx, call)
    max_chars = min(ctx.caps.command_tail_chars, ctx.limits.max_command_output_chars)

    start = time.monotonic()
    deadline = start + timeout_s
    handle = ctx.host.spawn(command, ctx.workspace.root)
    streamed = 0  # how much of the output the UI has already been shown
    while True:
        slice_s = max(0.0, min(_POLL_SLICE_S, deadline - time.monotonic()))
        finished = handle.wait(slice_s)
        # Every slice, finished or not: the last one carries the tail that
        # arrived while the command was exiting.
        streamed = _stream(ctx, call, handle, streamed)
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
run_command(command*, reason*, timeout)
  Run a shell command from the project root (timeout secs, default 60).
  reason: one line, why this command - the user sees it.
  Returns "exit N (X.Xs)" plus merged stdout+stderr, tail-capped (the end
  of the output - where test verdicts live - always survives). A timed out
  or user-cancelled command is killed and reported as exec_timeout /
  cancelled with the partial tail. NEVER modify files with commands (no
  sed/redirects/rm) - use write_file/edit_file/delete_file so every change
  is backed up.
===CLIP:CALL id=1 tool=run_command===
command: pytest tests/ -q
reason: check the tests pass
===CLIP:END==="""

# The reason is the model's text on a user-facing surface, so the drawer takes
# it one flattened, clipped line at a time - a 200-line "reason" must not push
# the command itself out of view.
_REASON_PREVIEW_CHARS = 200


def reason_line(call: ToolCall) -> str:
    """The ``reason: ...`` line shown at the approval gate, or "" if absent."""
    flat = " ".join(call.params.get("reason", "").split())
    if not flat:
        return ""
    if len(flat) > _REASON_PREVIEW_CHARS:
        flat = flat[: _REASON_PREVIEW_CHARS - 1].rstrip() + "…"
    return f"reason: {flat}"


def preview_run_command(ctx: ToolContext, call: ToolCall) -> str:
    command = call.params.get("command", "(missing command parameter)")
    lines = [command, reason_line(call), f"cwd: {ctx.workspace.root}"]
    return "\n".join(line for line in lines if line)


RUN_COMMAND_SPEC = ToolSpec(
    "run_command", "command", run_command, preview_run_command, RUN_COMMAND_DOC
)
