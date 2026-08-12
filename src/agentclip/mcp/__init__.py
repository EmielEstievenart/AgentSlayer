"""MCP (Model Context Protocol) support: OpenCode's config, AgentClip's tools.

Layout (docs/design/mcp.md):
    types.py    shared dataclasses + the tool-id convention; stdlib-only
    config.py   reads opencode.json's `mcp` block; stdlib-only
    client.py   the McpManager runtime; the ONLY module that may import the
                optional `mcp` SDK, and only lazily
    tool.py     the mcp / mcp_schema ToolSpecs for the registry
"""
