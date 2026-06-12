"""run_command: subprocess execution with merged output and tail capping.

The allowlist/approval decision is NOT made here - the engine gates the call
before the handler runs. This handler just executes:

- subprocess.run(shell=True, cwd=workspace.root), stdout+stderr merged so
  interleaving survives;
- timeout param (seconds, default 60) capped by limits.command_timeout_s;
- output TAIL-capped (build/test verdicts live at the end) to
  caps.command_tail_lines / caps.command_tail_chars;
- first body line is "exit N (X.Xs)"; a timeout becomes an exec_timeout
  error carrying the partial tail.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

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


def _coerce_output(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


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


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the shell AND its children (shell=True makes the real work a grandchild)."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()


@tool_handler
def run_command(ctx: ToolContext, call: ToolCall) -> str:
    (command,) = require(call, "command")
    timeout_s = _effective_timeout(ctx, call)
    max_chars = min(ctx.caps.command_tail_chars, ctx.limits.max_command_output_chars)

    popen_kwargs: dict = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True  # own process group, so killpg reaps children

    start = time.monotonic()
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=str(ctx.workspace.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        **popen_kwargs,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        try:  # collect whatever was buffered before the kill
            out, _ = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out = _coerce_output(exc.output)
        partial = _tail_cap(_coerce_output(out), ctx.caps.command_tail_lines, max_chars)
        message = f"command timed out after {timeout_s}s"
        if partial:
            message += f"\npartial output (tail):\n{partial}"
        raise ToolError(
            "exec_timeout",
            message,
            f"raise timeout (limit {ctx.limits.command_timeout_s}s) or run a narrower command.",
        ) from None
    elapsed = time.monotonic() - start

    tail = _tail_cap(out or "", ctx.caps.command_tail_lines, max_chars)
    body = f"exit {proc.returncode} ({elapsed:.1f}s)"
    if tail:
        body += f"\n{tail}"
    return body


RUN_COMMAND_DOC = """\
run_command(command*, timeout)
  Run a shell command from the project root (timeout in seconds, default
  60). Returns "exit N (X.Xs)" plus merged stdout+stderr, tail-capped (the
  end of the output - where test verdicts live - always survives). A timed
  out command is killed and reported as exec_timeout with the partial tail.
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
