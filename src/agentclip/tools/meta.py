"""Meta tools: ask_user, task_done and delegate.

These have no filesystem effect. The engine intercepts them BY NAME before
invoking handlers (ask_user pauses payload assembly for the user's answer;
delegate parks the whole turn while a sub-agent runs; task_done completes the
session - though the user may still continue with a follow-up, which reopens
it). The handlers below exist so the registry stays total - if the engine ever
fails to intercept, they are harmless no-ops. Their catalog_docs still teach
the LLM how to use them.

`delegate` and the `result` param of `task_done` are role-dependent: only a
master agent is ever offered `delegate` (and only when the sub-agent chat is
calibrated), and only a sub-agent is taught `task_done`'s `result` param,
because only a sub-agent has anything to hand back.
"""

from __future__ import annotations

from typing import Literal

from agentclip.protocol.types import ToolCall, ToolResult
from agentclip.tools.registry import ToolContext, ToolSpec

Role = Literal["master", "subagent"]


def ask_user(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, status="ok", body="", tool=call.tool)


def task_done(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, status="ok", body="", tool=call.tool)


def delegate(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, status="ok", body="", tool=call.tool)


ASK_USER_DOC = """\
ask_user(question*)
  Ask the user a question; the results payload is not sent until they answer
  in the terminal, and the result body is their answer verbatim. Use
  sparingly - prefer acting autonomously over asking.
===CLIP:CALL id=1 tool=ask_user===
question: Should I also update the changelog?
===CLIP:END==="""

TASK_DONE_DOC = """\
task_done(summary)
  Send when the task is complete and verified; it ends the session. Put what
  changed and how you verified it in summary. Do not emit further calls after
  task_done.
===CLIP:CALL id=1 tool=task_done===
summary << EOT
Fixed parse_date (src/utils.py line 88); pytest: 5 passed.
EOT
===CLIP:END==="""

TASK_DONE_DOC_SUBAGENT = """\
task_done(summary, result*)
  Send when the delegated task is complete. `result` (heredoc) is what is
  handed back to the agent that delegated to you - it is the ONLY thing they
  see, so it must stand alone: findings, file paths, exact quotes, whatever was
  asked for. `summary` is a short line for the human's log.
===CLIP:CALL id=1 tool=task_done===
summary: surveyed the screen package
result << EOT
capture.grab_region() -> RegionImage, handed to matcher.find_template().
EOT
===CLIP:END==="""

DELEGATE_DOC = """\
delegate(task*, context)
  Hand a self-contained sub-task to a FRESH sub-agent in its own chat with its
  own context window. It sees none of this conversation - put everything it
  needs into task/context and state the exact deliverable you want back. The
  result body is the sub-agent's final answer, verbatim. One delegation runs at
  a time, it may take a while, and a finished sub-agent cannot be reopened. Use
  it for bounded chunks (surveys, large-file reading, self-contained edits) that
  would otherwise flood your own context.
===CLIP:CALL id=1 tool=delegate===
task << EOT
Read every file under src/agentclip/screen/ and report, in <=25 lines, how a
region capture reaches the template matcher. Quote exact function names.
EOT
===CLIP:END==="""


ASK_USER_SPEC = ToolSpec("ask_user", "auto", ask_user, None, ASK_USER_DOC)
# Approval mode is "auto": the delegation itself is loudly user-visible (a new
# chat tab opens and the user carries every paste), so a per-call gate on top
# would add friction without adding oversight.
DELEGATE_SPEC = ToolSpec("delegate", "auto", delegate, None, DELEGATE_DOC)


def task_done_spec(role: Role = "master") -> ToolSpec:
    """Role-specific catalog doc: sub-agents are taught the `result` param.

    The engine reads `result` unconditionally (a master that sends one simply
    has nobody to hand it to), so only the advertised documentation differs.
    """
    doc = TASK_DONE_DOC_SUBAGENT if role == "subagent" else TASK_DONE_DOC
    return ToolSpec("task_done", "auto", task_done, None, doc)


TASK_DONE_SPEC = task_done_spec("master")
