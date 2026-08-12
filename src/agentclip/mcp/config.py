"""Reader for the ``mcp`` block of ``opencode.json`` (docs/design/mcp.md 1).

The point of this module is compatibility: a server the user already declared
for OpenCode must mean the same thing here, down to the merge order, the
placeholder syntax and the 30s default timeout. Where OpenCode's docs and its
runtime disagree, the runtime wins (see :data:`DEFAULT_TIMEOUT_MS`).

Stdlib-only, deliberately: ``config.py`` imports this unconditionally, while
the ``mcp`` SDK is an optional extra that a perfectly healthy install may not
have (docs/design/mcp.md 2). Only ``mcp/client.py`` may touch the SDK.

It also does NOT import :mod:`agentclip.config` - that would be circular, since
config.py imports this - which is why :func:`load_mcp_servers` takes the files
to read as an argument. *Deciding* which files those are (the global one
always, a project one only for local sessions, because a `local` server is a
command THIS PC will run and a remote machine must not choose it) belongs to
the caller.

Like :func:`agentclip.config._load_permission_rules`, nothing here raises: a
missing file is silent (most machines have none), a file that exists but
cannot be understood costs exactly one warning, and a single bad entry costs
that entry and nothing else.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from agentclip.mcp.types import (
    DEFAULT_TIMEOUT_MS,
    McpLocalServer,
    McpRemoteServer,
    McpServerConfig,
    McpServers,
)

# OpenCode's two placeholder forms. Matched in one pass over an already-parsed
# string leaf, so a value can embed and repeat them ("Bearer {env:TOKEN}") and
# the substituted text is never re-scanned - a secret that happens to contain
# "{env:...}" stays a secret rather than becoming a lookup.
_PLACEHOLDER_RE = re.compile(r"\{(env|file):([^}]*)\}")


def load_mcp_servers(paths: Sequence[Path], warnings: list[str]) -> McpServers:
    """Read ``mcp`` from each of ``paths`` and return the merged server list.

    ``paths`` are in ASCENDING precedence (global first, project second), and
    same-name entries merge PER FIELD - a project file that sets only
    ``timeout`` keeps the global file's ``command``. The merge happens on the
    raw dicts, before typing, which is what makes OpenCode's bare disable
    (``{"enabled": false}`` with no ``type``) work: it is a patch onto whatever
    an earlier layer declared, not a server of its own.

    Order is merge -> parse -> substitute. Substitution comes last on purpose
    (docs/design/mcp.md 1): OpenCode expands placeholders over the raw text
    before parsing, but doing it post-parse covers every real use (secrets in
    ``headers``/``environment``/``url``) without letting the contents of an
    environment variable or a file rewrite the config's *structure*.
    """
    merged: dict[str, dict] = {}
    # Which file last had a say about each name, so a parse warning can blame a
    # file the user can actually open. Per name, not per file: after the merge
    # an entry may be two files deep, and the later one is the one that made it
    # look the way it does.
    blame: dict[str, Path] = {}
    sources: list[str] = []

    for path in paths:
        block = _read_mcp_block(path, warnings)
        if block is None:
            continue
        contributed = False
        for name, entry in block.items():
            if not isinstance(entry, dict):
                warnings.append(f"config: {path}: mcp.{name} must be a table; ignored")
                continue
            previous = merged.get(name)
            merged[name] = _deep_merge(previous, entry) if previous is not None else dict(entry)
            blame[name] = path
            contributed = True
        if contributed:
            sources.append(str(path))

    servers: list[McpServerConfig] = []
    for name, entry in merged.items():
        ctx = f"config: {blame[name]}: mcp.{name}"
        server = _parse_entry(name, entry, ctx, warnings)
        if server is not None:
            servers.append(_substitute(server, ctx, warnings))
    return McpServers(servers=tuple(servers), source=", ".join(sources))


# -- reading ------------------------------------------------------------------


def _read_mcp_block(path: Path, warnings: list[str]) -> dict | None:
    """The ``mcp`` table of one file, or None when it has nothing to give.

    Triage copied from ``_load_permission_rules``: absent is normal and silent,
    unreadable or unparseable warns once, and a file without the key we care
    about is simply not about us. Only a present-but-wrong ``mcp`` warns, since
    that is a user who meant to configure servers and got nothing.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        warnings.append(f"config: could not read {path}: {exc}")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        warnings.append(f"config: {path} is not valid JSON: {exc}")
        return None
    if not isinstance(data, dict) or "mcp" not in data:
        return None
    block = data["mcp"]
    if not isinstance(block, dict):
        warnings.append(f"config: {path}: mcp must be a table of servers; ignored")
        return None
    return block


def _deep_merge(base: dict, override: dict) -> dict:
    """OpenCode's deep merge, and ``agentclip.config._deep_merge``'s semantics:
    nested tables (``environment``, ``headers``) merge per key, while scalars
    and lists replace wholesale. A ``command`` list is a single decision, not a
    set of parts to interleave."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# -- parsing ------------------------------------------------------------------


def _parse_entry(
    name: str, entry: dict, ctx: str, warnings: list[str]
) -> McpServerConfig | None:
    """One merged entry as a typed server, or None (with a warning, unless the
    entry is a bare disable) when it cannot be trusted.

    Unknown keys inside an entry are ignored in silence: OpenCode's schema
    grows, and warning about a key this version has not learned yet would train
    users to ignore warnings.
    """
    kind = entry.get("type")
    if kind is None:
        # A bare disable: `{"enabled": false}` and no type. It is legal in
        # OpenCode as a way to switch off a server declared in another config
        # layer - and if such a layer existed, the raw-dict merge already
        # applied it there, so a type-less entry reaching here means nobody
        # declared the server at all. Nothing to disable, nothing to warn
        # about: the user got exactly what they asked for.
        if entry.get("enabled") is False:
            return None
        warnings.append(f'{ctx}: type must be "local" or "remote"; ignored')
        return None
    if kind == "local":
        return _parse_local(name, entry, ctx, warnings)
    if kind == "remote":
        return _parse_remote(name, entry, ctx, warnings)
    warnings.append(f'{ctx}: unknown type {kind!r}; expected "local" or "remote"; ignored')
    return None


def _parse_local(
    name: str, entry: dict, ctx: str, warnings: list[str]
) -> McpLocalServer | None:
    command = entry.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(x, str) for x in command)
    ):
        # The whole server dies with its command, rather than defaulting to
        # something plausible: this is the argv THIS PC will execute, and a
        # guess is the one failure mode with teeth (docs/design/mcp.md 1).
        warnings.append(f"{ctx}: command must be a non-empty list of strings; server ignored")
        return None
    return McpLocalServer(
        name=name,
        command=tuple(command),
        cwd=_take_str(entry, "cwd", ctx, warnings),
        environment=_take_str_table(entry, "environment", ctx, warnings),
        enabled=_take_enabled(entry, ctx, warnings),
        timeout_ms=_take_timeout(entry, ctx, warnings),
    )


def _parse_remote(
    name: str, entry: dict, ctx: str, warnings: list[str]
) -> McpRemoteServer | None:
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        warnings.append(f"{ctx}: url must be a non-empty string; server ignored")
        return None
    return McpRemoteServer(
        name=name,
        url=url,
        headers=_take_str_table(entry, "headers", ctx, warnings),
        enabled=_take_enabled(entry, ctx, warnings),
        timeout_ms=_take_timeout(entry, ctx, warnings),
        # Absent means OpenCode would try OAuth; present is read for truthiness
        # so `false` switches it off the way OpenCode's own config does. The
        # flag only records the intent - phase 1 performs no OAuth, it just
        # wants an accurate hint to attach to a 401 (McpRemoteServer docstring).
        oauth=True if "oauth" not in entry else bool(entry["oauth"]),
    )


def _take_str(entry: dict, key: str, ctx: str, warnings: list[str]) -> str:
    value = entry.get(key, "")
    if not isinstance(value, str):
        warnings.append(f"{ctx}: {key} must be a string; using ''")
        return ""
    return value


def _take_str_table(
    entry: dict, key: str, ctx: str, warnings: list[str]
) -> tuple[tuple[str, str], ...]:
    """``environment``/``headers``: a table of strings, kept as ordered pairs.

    A bad value drops only its own pair - half a header set is still worth
    sending, and naming the key tells the user which secret went missing."""
    value = entry.get(key)
    if value is None:
        return ()
    if not isinstance(value, dict):
        warnings.append(f"{ctx}: {key} must be a table of strings; ignored")
        return ()
    pairs: list[tuple[str, str]] = []
    for item_key, item in value.items():
        if not isinstance(item, str):
            warnings.append(f"{ctx}: {key}.{item_key} must be a string; dropped")
            continue
        pairs.append((str(item_key), item))
    return tuple(pairs)


def _take_enabled(entry: dict, ctx: str, warnings: list[str]) -> bool:
    value = entry.get("enabled", True)
    if not isinstance(value, bool):
        warnings.append(f"{ctx}: enabled must be true/false; using True")
        return True
    return value


def _take_timeout(entry: dict, ctx: str, warnings: list[str]) -> int:
    """``timeout`` in MILLISECONDS, kept verbatim - no unit conversion anywhere,
    because OpenCode's number and ours must be the same number.

    ``isinstance(True, int)`` is true in Python, so the bool test comes first
    (the ``_take_int`` pitfall): ``"timeout": true`` is a mistake, not a 1ms
    deadline. The default is 30000 rather than the 5000 OpenCode's docs claim -
    its runtime says 30s, and behaviour beats documentation when the point is
    compatibility (docs/design/mcp.md 1)."""
    value = entry.get("timeout", DEFAULT_TIMEOUT_MS)
    if isinstance(value, bool) or not isinstance(value, int):
        warnings.append(
            f"{ctx}: timeout must be an integer number of milliseconds; "
            f"using {DEFAULT_TIMEOUT_MS}"
        )
        return DEFAULT_TIMEOUT_MS
    if value < 1:
        warnings.append(f"{ctx}: timeout={value} must be at least 1 ms; using {DEFAULT_TIMEOUT_MS}")
        return DEFAULT_TIMEOUT_MS
    return value


# -- placeholder substitution -------------------------------------------------


def _substitute(server: McpServerConfig, ctx: str, warnings: list[str]) -> McpServerConfig:
    """Expand ``{env:VAR}`` / ``{file:path}`` on every string leaf of a parsed
    server: command elements, cwd, environment values, url, header values.

    Never on server names and never on table keys - those are identity, and an
    id that changes with the environment would make permission rules and status
    lines mean different things on different days.
    """
    if isinstance(server, McpLocalServer):
        return replace(
            server,
            command=tuple(_expand(part, ctx, warnings) for part in server.command),
            cwd=_expand(server.cwd, ctx, warnings),
            environment=tuple(
                (key, _expand(value, ctx, warnings)) for key, value in server.environment
            ),
        )
    return replace(
        server,
        url=_expand(server.url, ctx, warnings),
        headers=tuple((key, _expand(value, ctx, warnings)) for key, value in server.headers),
    )


def _expand(value: str, ctx: str, warnings: list[str]) -> str:
    if "{" not in value:  # the overwhelmingly common case; skip the regex
        return value

    def _one(match: re.Match[str]) -> str:
        kind, arg = match.group(1), match.group(2)
        if kind == "env":
            # Unset substitutes EMPTY rather than failing or leaving the
            # placeholder in place, matching OpenCode: the server then fails to
            # authenticate with a message from the server itself, which is a
            # better story than a literal "{env:TOKEN}" travelling as a token.
            return os.environ.get(arg, "")
        try:
            text = Path(arg).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"{ctx}: could not read {arg} for {{file:...}}; using '': {exc}")
            return ""
        # Stripped because secret files end in a newline, and a newline inside
        # "Bearer {file:token}" would corrupt the header it is joined into.
        return text.strip()

    return _PLACEHOLDER_RE.sub(_one, value)
