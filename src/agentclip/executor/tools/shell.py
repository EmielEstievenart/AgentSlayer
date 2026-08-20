"""run_command: command execution with merged output, kept whole.

The approval decision is NOT made here - the engine gates the call
before the handler runs. This handler just executes:

- ctx.host.spawn(command, cwd=workspace.root), stdout+stderr merged so
  interleaving survives;
- timeout param (seconds, default 60) capped by limits.command_timeout_s;
- the WHOLE output, bounded only by the memory guard
  (limits.max_command_output_chars, :func:`retain_output`);
- first body line is "exit N (X.Xs)"; a timeout becomes an exec_timeout
  error carrying the partial tail.

Why the whole output, when a paste budget obviously cannot carry it
--------------------------------------------------------------------
Because this handler is no longer the last thing that sees it. The engine
truncates for display AND caches what it cut, so a body that arrives here whole
reaches the model as head-plus-tail with a `fetch_chunk` marker naming the rest
(executor/tools/chunks.py). A handler that tail-capped first would be cutting
BEFORE the cache is filled: the cache would then faithfully hold the tail it was
handed, part 1 would start in the middle, and the head would not exist anywhere -
which is exactly the failure fetch_chunk was built to end. Truncating in two
places cannot be half-right; it has to happen in the one place that can also
remember. The guard that remains is a memory bound and nothing else, set to the
same number the cache stops at, so it can only ever drop text no fetch could have
reached anyway.

The one exception is the kill/drain path: a command that had to be killed leaves
a buffer, not a result, and nothing will ever chunk-fetch it - so that tail stays
tail-capped to the budget's caps, where a bounded emergency drain belongs.

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
copy of the output is still the body composed at the end, and a host that cannot
stream simply shows nothing until then.

Everything here is host-agnostic: the spawn/wait/kill/drain handle comes from
ctx.host, so the same polling loop drives a local subprocess or a remote one.
"""

from __future__ import annotations

import time

from agentclip.executor.hosts.base import ExecHandle
from agentclip.executor.tools.registry import (
    ToolContext,
    ToolError,
    ToolSpec,
    int_param,
    require,
    tool_handler,
)
from agentclip.protocol.types import ToolCall

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


def retain_output(text: str, max_chars: int) -> str:
    """``text`` whole, unless holding it whole is itself the problem.

    THE memory guard, shared by run_command and the MCP handler (both produce
    output nobody can re-derive by asking again with a narrower range). Under the
    bound this is the identity function, which is the entire point: display
    truncation belongs to the engine, which caches what it cuts, and a handler
    that anticipated it would destroy the head of the very body the cache is
    about to be filled from.

    Over the bound - a build that printed a megabyte, an MCP server that answered
    with a database - the tail is kept and the ordinary in-band marker says how
    much was dropped. It stays the HONEST marker (rather than a fetch_chunk one)
    because this is the one truncation that really is unrecoverable: the bound is
    the cache's own per-body cap, so there is nothing past it left to fetch. Only
    the char dimension bites; the line cap is handed the text's own line count,
    because "you may have 512k but only 500 lines of it" would be the display
    policy this function exists to stay out of.

    The two normalisations it does make are the ones `_tail_cap` always did, kept
    so a body's SHAPE is unchanged by this fix: CRLF becomes LF (payloads are
    LF-only, and a remote host's output is not), and the one trailing newline
    every command ends with is dropped, because the heredoc the body is rendered
    into supplies its own.
    """
    text = text.replace("\r\n", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    if len(text) <= max_chars:
        return text
    return _tail_cap(text, len(text.splitlines()), max_chars)


def _effective_timeout(ctx: ToolContext, call: ToolCall) -> int:
    requested = int_param(call, "timeout", _DEFAULT_TIMEOUT_S)
    if requested < 1:
        raise ToolError(
            "bad_param", "timeout must be >= 1 second", "resend with a positive timeout."
        )
    return min(requested, ctx.limits.command_timeout_s)


def _kill_and_drain(ctx: ToolContext, handle: ExecHandle, max_chars: int) -> str:
    """Kill the process tree and return the tail of whatever it managed to emit.

    The one place a run_command body is still tail-capped to the budget's caps,
    and deliberately: what a killed process left in its buffer is an emergency
    drain, not a result. It rides inside an error message rather than as a body,
    the model is being told to stop rather than to read, and nothing will ever
    hand it a fetch_chunk id - so bounding it small is the kindness here, where
    bounding a finished command's output small was the bug.
    """
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
    # For the DRAIN paths below only (see _kill_and_drain): the user's explicit
    # cap still bounds it, but the budget's own tail cap is what normally wins.
    drain_chars = min(ctx.caps.command_tail_chars, ctx.limits.max_command_output_chars)

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
            partial = _kill_and_drain(ctx, handle, drain_chars)
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
            partial = _kill_and_drain(ctx, handle, drain_chars)
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

    output = retain_output(finished.output, ctx.limits.max_command_output_chars)
    body = f"exit {finished.exit_code} ({elapsed:.1f}s)"
    if output:
        body += f"\n{output}"
    return body


RUN_COMMAND_DOC = """\
run_command(command*, reason*, timeout)
  Run a shell command from the project root (timeout secs, default 60).
  reason: one line, why this command - the user sees it.
  Returns "exit N (X.Xs)" plus merged stdout+stderr (a body too big for
  one paste is cut, and its marker says how to get the rest). A timed out
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
