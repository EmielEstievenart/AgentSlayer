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
to read as an argument. *Deciding* which files those are, and which machine
they are on (:class:`McpTarget`), belongs to the caller.

Like :func:`agentclip.config._load_permission_rules`, nothing here raises: a
missing file is silent (most machines have none), a file that exists but
cannot be understood costs exactly one warning, and a single bad entry costs
that entry and nothing else.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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


def _read_local(path: Path) -> bytes:
    return path.read_bytes()


def _expand_local(raw: str) -> Path:
    return Path(raw).expanduser()


@dataclass(frozen=True, slots=True)
class McpTarget:
    """The machine the config files are on, as the three things reading them
    needs: how to read a file over there, what its environment holds, and what
    ``~`` means there.

    Three plain callables and a mapping rather than the Host object that has
    them, because this module is a stdlib-only leaf allowed to import nothing
    but its own package (tests/test_layering.py): it takes the capabilities it
    uses instead of the seam that supplies them. ``expanduser`` travels as one
    of them so ``~`` means the same thing here as it does for the permission
    ruleset read off the same file - config.py hands both the same rule rather
    than keeping two copies of it.

    The defaults are THIS PC, and they are what every local session gets: the
    reader behaves exactly as it did before remote sessions could reach it.
    """

    read_bytes: Callable[[Path], bytes] = _read_local
    # os.environ itself, not a copy: a caller that exports a variable after
    # load time (and the test suite's monkeypatch.setenv) must still be read.
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    expanduser: Callable[[str], Path] = _expand_local


def load_mcp_servers(
    paths: Sequence[Path], warnings: list[str], target: McpTarget | None = None
) -> McpServers:
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

    ``target`` says which machine ``paths`` (and the files and variables they
    point at) live on; omitted, it is this PC.
    """
    target = target if target is not None else McpTarget()
    merged: dict[str, dict] = {}
    # Which file last had a say about each name, so a parse warning can blame a
    # file the user can actually open. Per name, not per file: after the merge
    # an entry may be two files deep, and the later one is the one that made it
    # look the way it does.
    blame: dict[str, Path] = {}
    sources: list[str] = []

    for path in paths:
        block = _read_mcp_block(path, warnings, target)
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
            # The blame file's directory anchors relative {file:...} paths -
            # OpenCode resolves them against the config file being parsed, and
            # "the secret sits next to the config that names it" is the layout
            # that implies. (When two layers both touched a server, the LAST
            # file anchors: per-field origins are gone after the raw merge,
            # and the later file is the one whose author saw the final shape.)
            servers.append(_substitute(server, ctx, blame[name].parent, warnings, target))
    return McpServers(servers=tuple(servers), source=", ".join(sources))


# -- reading ------------------------------------------------------------------


def _read_mcp_block(path: Path, warnings: list[str], target: McpTarget) -> dict | None:
    """The ``mcp`` table of one file, or None when it has nothing to give.

    Triage copied from ``_load_permission_rules``: absent is normal and silent,
    unreadable or unparseable warns once, and a file without the key we care
    about is simply not about us. Only a present-but-wrong ``mcp`` warns, since
    that is a user who meant to configure servers and got nothing.

    Bytes rather than text because the Host seam speaks bytes; json.loads does
    its own UTF-8 decoding and raises a ValueError subclass when that fails, so
    a mojibake config becomes the same one warning as any other unreadable one.
    """
    try:
        raw = target.read_bytes(path)
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
        # OpenCode's schema is Union([OAuth struct, Literal(false)]): ONLY a
        # literal `false` disables OAuth. Everything else - absent, an oauth
        # object with fields, or `{}` (every OAuth field is optional, so an
        # empty object is legal and means "attempt via auto-discovery") - keeps
        # it on. `bool(entry["oauth"])` was wrong precisely for `{}`, which is
        # falsy in Python and would have flipped the meaning. The flag only
        # records the intent - phase 1 performs no OAuth, it just wants an
        # accurate hint to attach to a 401 (McpRemoteServer docstring).
        oauth=entry.get("oauth", True) is not False,
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


def _substitute(
    server: McpServerConfig, ctx: str, base_dir: Path, warnings: list[str], target: McpTarget
) -> McpServerConfig:
    """Expand ``{env:VAR}`` / ``{file:path}`` on every string leaf of a parsed
    server: command elements, cwd, environment values, url, header values.

    Never on server names and never on table keys - those are identity, and an
    id that changes with the environment would make permission rules and status
    lines mean different things on different days.
    """
    if isinstance(server, McpLocalServer):
        return replace(
            server,
            command=tuple(
                _expand(part, ctx, base_dir, warnings, target) for part in server.command
            ),
            cwd=_expand(server.cwd, ctx, base_dir, warnings, target),
            environment=tuple(
                (key, _expand(value, ctx, base_dir, warnings, target))
                for key, value in server.environment
            ),
        )
    return replace(
        server,
        url=_expand(server.url, ctx, base_dir, warnings, target),
        headers=tuple(
            (key, _expand(value, ctx, base_dir, warnings, target)) for key, value in server.headers
        ),
    )


def _expand(value: str, ctx: str, base_dir: Path, warnings: list[str], target: McpTarget) -> str:
    if "{" not in value:  # the overwhelmingly common case; skip the regex
        return value

    def _one(match: re.Match[str]) -> str:
        kind, arg = match.group(1), match.group(2)
        if kind == "env":
            # Unset substitutes EMPTY rather than failing or leaving the
            # placeholder in place, matching OpenCode: the server then fails to
            # authenticate with a message from the server itself, which is a
            # better story than a literal "{env:TOKEN}" travelling as a token.
            #
            # The environment is the TARGET's in a remote session: the person
            # who wrote {env:API_TOKEN} into a file on that box exported it on
            # that box, and this PC's variable of the same name is a different
            # secret (docs/design/remote-ssh.md, "the target owns its policy").
            return target.environ.get(arg, "")
        try:
            # Relative paths anchor to the CONFIG FILE's directory, as OpenCode
            # resolves them - never to this process's cwd, which is wherever
            # AgentClip was launched from and would send "{file:./token.txt}"
            # hunting through the user's project instead of ~/.config/opencode.
            secret = target.expanduser(arg)
            if not secret.is_absolute():
                secret = base_dir / secret
            text = target.read_bytes(secret).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # A file that is not text is as unusable as one that is not there,
            # and both are the author's mistake to see rather than a traceback
            # out of a config load that promises never to raise.
            warnings.append(f"{ctx}: could not read {arg} for {{file:...}}; using '': {exc}")
            return ""
        # Stripped because secret files end in a newline, and a newline inside
        # "Bearer {file:token}" would corrupt the header it is joined into.
        return text.strip()

    return _PLACEHOLDER_RE.sub(_one, value)
