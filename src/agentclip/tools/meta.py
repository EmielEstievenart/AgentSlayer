"""Meta tools: ask_user and task_done.

These have no filesystem effect. The engine intercepts them BY NAME before
invoking handlers (ask_user pauses payload assembly for the user's answer;
task_done completes the session - though the user may still continue with a
follow-up, which reopens it). The handlers below exist so the registry stays
total - if the engine ever fails to intercept, they are harmless no-ops.
Their catalog_docs still teach the LLM how to use them.
"""

from __future__ import annotations

from agentclip.protocol.types import ToolCall, ToolResult
from agentclip.tools.registry import ToolContext, ToolSpec


def ask_user(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(call_id=call.id, status="ok", body="", tool=call.tool)


def task_done(ctx: ToolContext, call: ToolCall) -> ToolResult:
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
summary <<EOT
Fixed parse_date (src/utils.py line 88); pytest: 5 passed.
EOT
===CLIP:END==="""


ASK_USER_SPEC = ToolSpec("ask_user", "auto", ask_user, None, ASK_USER_DOC)
TASK_DONE_SPEC = ToolSpec("task_done", "auto", task_done, None, TASK_DONE_DOC)
