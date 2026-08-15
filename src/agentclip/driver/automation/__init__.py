"""The screen-automation orchestration layer, shared by every UI shell.

What AgentClip does TO the chat window - watch it, click it, paste into it,
harvest the reply - belongs here rather than inside one frontend, because the
Textual TUI is only the first shell to drive it and a pywebview GUI is meant to
drive the same core later.

May import ``agentclip.driver.screen``, ``agentclip.driver.clip`` and
``agentclip.config`` (the OS seams the loop is made of); must never import
``textual``, ``agentclip.shell.app`` or ``agentclip.shell.tui`` - a shell may depend on the
automation, never the other way round (enforced by tests/test_layering.py).
"""

from __future__ import annotations
