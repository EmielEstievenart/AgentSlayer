"""MCP (Model Context Protocol) support: OpenCode's config shape, AgentClip's tools.

Layout (docs/design/mcp.md):
    types.py    shared dataclasses + the tool-id convention; stdlib-only
    config.py   reads permissions.json's `mcp` block; stdlib-only
    client.py   the McpManager runtime; the ONLY module that may import the
                optional `mcp` SDK, and only lazily

The mcp / mcp_schema ToolSpecs live in agentclip/executor/tools/mcp_tools.py, not
here: this package is a leaf below config (test_layering.py), and a spec
module would have to import the tools layer, closing a cycle.
"""
