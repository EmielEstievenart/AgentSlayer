"""Shared value types for the MCP subsystem (docs/design/mcp.md).

Stdlib-only on purpose: config.py imports these unconditionally, and the
optional `mcp` SDK (an extra, like cv) may be absent on a perfectly healthy
install. Only mcp/client.py may import the SDK, and only lazily.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# OpenCode's tool-id sanitizer, ported byte-for-byte (catalog.ts): the composite
# id `sanitize(server) + "_" + sanitize(tool)` must mean the same thing in a
# permission glob here as it does there, or a rule the user already trusts
# would silently gate different calls.
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize(value: str) -> str:
    return _SANITIZE_RE.sub("_", value)


def tool_id(server: str, tool: str) -> str:
    """The composite id the model calls and permission rules match against."""
    return f"{sanitize(server)}_{sanitize(tool)}"


# Milliseconds. OpenCode's docs say 5000; its runtime says 30_000 in both
# places that matter (mcp/index.ts, catalog.ts). Compatibility follows the
# runtime - see docs/design/mcp.md section 1.
DEFAULT_TIMEOUT_MS = 30_000


@dataclass(frozen=True, slots=True)
class McpLocalServer:
    """A `"type": "local"` entry: a stdio server THIS PC spawns.

    `command` is OpenCode's shape - argv[0] plus args in one list. `cwd`
    is kept verbatim (resolved against the project root at spawn time, and
    only for local sessions - the config loader never even reads a remote
    project's file, docs/design/mcp.md section 1).
    """

    name: str
    command: tuple[str, ...]
    cwd: str = ""
    environment: tuple[tuple[str, str], ...] = ()
    enabled: bool = True
    timeout_ms: int = DEFAULT_TIMEOUT_MS


@dataclass(frozen=True, slots=True)
class McpRemoteServer:
    """A `"type": "remote"` entry: Streamable HTTP, SSE fallback.

    `oauth` records whether OpenCode would attempt OAuth for this server
    (i.e. the key is absent or truthy). Phase 1 never performs OAuth; the
    flag exists so a 401 can be reported as needs_auth with an accurate
    hint rather than a generic failure.
    """

    name: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    enabled: bool = True
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    oauth: bool = True


McpServerConfig = McpLocalServer | McpRemoteServer

# The per-server lifecycle, phase 1. `missing_sdk` is a state and not an error
# because an install without the [mcp] extra is healthy by design (the cv
# precedent): configured servers park here and the status line names the fix.
McpServerState = Literal[
    "pending",  # known, nothing attempted yet (connects are lazy)
    "connecting",
    "connected",
    "disabled",  # enabled=false in config
    "failed",  # spawn/connect/handshake error; detail says what
    "needs_auth",  # remote answered 401/403 and phase 1 has no OAuth
    "missing_sdk",  # the [mcp] extra is not installed
]


@dataclass(frozen=True, slots=True)
class McpServerStatus:
    name: str
    state: McpServerState
    detail: str = ""  # one line, human-facing; "" when the state says it all
    tool_count: int = 0


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    """One tool as cached at connect time (no live round-trip afterwards).

    `input_schema_json` is the server's JSON Schema, serialized compactly;
    kept as text because the only consumers re-emit it as text (mcp_schema's
    result) and stdlib json round-trips it without the SDK.
    """

    id: str  # the composite tool_id(server, name)
    server: str
    name: str
    description: str
    input_schema_json: str


@dataclass(frozen=True, slots=True)
class McpConfig:
    """The `[mcp]` table of AgentClip's own config (NOT opencode.json):
    mirrors PermissionConfig - `enabled` kills the subsystem outright,
    `opencode_config` overrides the file path, blank meaning the same
    default_opencode_config_path() the permission reader uses."""

    enabled: bool = True
    opencode_config: str = ""


@dataclass(frozen=True, slots=True)
class McpServers:
    """What the config loader hands the runtime: the parsed server list plus
    where it came from ("" when no file contributed any)."""

    servers: tuple[McpServerConfig, ...] = ()
    source: str = ""

    def enabled_servers(self) -> tuple[McpServerConfig, ...]:
        return tuple(s for s in self.servers if s.enabled)
