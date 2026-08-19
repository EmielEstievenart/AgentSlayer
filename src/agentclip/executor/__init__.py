"""The Executor: everything AgentClip does ON BEHALF OF the agent it hosts.

``tools`` is the catalogue the agent may call, ``permissions`` decides which
calls are allowed, ``hosts`` is the seam every call runs through (local or SSH)
and ``mcp`` bolts external servers onto the same catalogue.
"""
