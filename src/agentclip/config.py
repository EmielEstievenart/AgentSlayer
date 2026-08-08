"""Configuration: frozen dataclasses + TOML load/merge/validate.

Stdlib-only leaf (plus platformdirs). Precedence, later wins, per-key shallow
merge per table — lists REPLACE, never concatenate (so a project can tighten
the allowlist):

    built-in defaults
    -> <user_config_dir>/agentclip/config.toml
    -> <project root>/.agentclip.toml
    -> CLI flags
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs
import tomli_w

# Always excluded from file tools, not configurable: the LLM must never read
# backups/transcripts or tamper with its own approval rules.
HARD_EXCLUDED_NAMES = frozenset({".agentclip", ".agentclip.toml"})

DEFAULT_EXCLUDES = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
)

DEFAULT_ALLOWLIST = (
    "pytest*",
    "python -m pytest*",
    "python -m unittest*",
    "uv run pytest*",
    "ruff check*",
    "ruff format --check*",
    "mypy*",
    "npm test*",
    "npm run test*",
    "npx tsc --noEmit*",
    "cargo check*",
    "cargo test*",
    "go test*",
    "go vet*",
    "git status",
    "git diff*",
    "git log*",
    "ls*",
    "dir*",
)

DEFAULT_DENY_TOKENS = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n")

# Theme names selectable in Settings > Appearance (F3). Two are Textual
# built-ins; "claude-warm"/"claude-dark" are registered by AgentClipApp on
# mount (tui/app.py) - config.py stays a stdlib-only leaf, so it validates
# against this literal set rather than importing textual just to ask it.
VALID_THEMES = frozenset({"textual-light", "textual-dark", "claude-warm", "claude-dark"})
DEFAULT_THEME = "textual-dark"


# How long the response region must sit unchanged before the stale finish
# detector calls the response done. Per service because streaming cadence
# differs: a service that pauses mid-answer needs a longer stillness window.
DEFAULT_STABLE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ServicePreset:
    key: str
    label: str
    max_paste_chars: int  # budget for a single paste (chunking splits above it)
    total_context_chars: int  # the service's whole conversation window, ~chars (tokens * ~4)
    wrap_blocks_in_fence: bool = True
    attachment_note: bool = True
    stable_seconds: float = DEFAULT_STABLE_SECONDS  # stale detector: stillness = finished


def default_services() -> dict[str, ServicePreset]:
    """The twelve built-in presets, keyed by their preset key.

    ``total_context_chars`` is a conservative estimate of the service's whole
    context window (roughly tokens * 4 chars/token) - it bounds the editor's
    validation (max input size must fit inside it) and is informational
    elsewhere; ``max_paste_chars`` (a single paste/message budget) is what the
    engine actually enforces per turn.
    """
    presets = [
        ServicePreset("chatgpt", "ChatGPT web (inline-safe)", 4_000, 500_000),
        ServicePreset("chatgpt-attach", "ChatGPT web (attachment OK)", 12_000, 500_000),
        ServicePreset("copilot-work", "M365 Copilot Chat - work tab (licensed)", 96_000, 400_000),
        ServicePreset("copilot-web", "M365 Copilot Chat - web tab", 12_000, 150_000),
        ServicePreset("copilot-free", "Copilot (unlicensed / consumer)", 6_000, 128_000),
        ServicePreset("claude", "Claude.ai", 24_000, 700_000),
        ServicePreset("gemini", "Gemini", 24_000, 800_000),
        ServicePreset("perplexity", "Perplexity", 6_000, 100_000),
        ServicePreset("deepseek", "DeepSeek", 12_000, 250_000),
        ServicePreset("grok", "Grok", 100_000, 400_000),
        ServicePreset("unknown", "Unknown service (conservative)", 6_000, 100_000),
        ServicePreset("paranoid", "Unknown service (paranoid)", 4_000, 50_000),
    ]
    return {p.key: p for p in presets}


# Keys that ship built-in and therefore can't be deleted (only edited/reset) by the
# service editor. Computed once - default_services() never varies at runtime.
BUILTIN_SERVICE_KEYS: frozenset[str] = frozenset(default_services())


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    """Per-tool result caps derived from the active paste budget (protocol §5.3)."""

    read_file_span_lines: int
    grep_max_hits: int
    command_tail_lines: int
    command_tail_chars: int
    listing_max_entries: int
    advised_max_calls: int


def caps_for_budget(budget_chars: int) -> BudgetCaps:
    if budget_chars <= 4_000:
        return BudgetCaps(120, 25, 60, 3_000, 100, 3)
    if budget_chars <= 8_000:
        return BudgetCaps(250, 50, 120, 6_000, 200, 5)
    if budget_chars <= 32_000:
        return BudgetCaps(600, 100, 250, 12_000, 400, 8)
    return BudgetCaps(1_500, 200, 500, 24_000, 1_000, 10)


@dataclass(frozen=True, slots=True)
class GeneralConfig:
    service: str = "chatgpt-attach"
    chars_per_token: int = 3  # code-like payloads tokenize at ~3 chars/token
    theme: str = DEFAULT_THEME


@dataclass(frozen=True, slots=True)
class ClipboardConfig:
    provider: str = "auto"  # auto | copykitten | pyperclip | manual
    poll_interval_ms: int = 300


@dataclass(frozen=True, slots=True)
class ApprovalConfig:
    auto_accept_edits: bool = False
    # YOLO mode: auto-approve EVERY tool call - edits AND commands - bypassing the
    # allowlist and the deny tokens entirely. Off by default; the /yolo chat command
    # toggles it live for the session. Setting it true here arms a session in YOLO.
    yolo: bool = False
    command_allowlist: tuple[str, ...] = DEFAULT_ALLOWLIST
    command_deny_tokens: tuple[str, ...] = DEFAULT_DENY_TOKENS


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    max_file_read_chars: int = 20_000
    max_command_output_chars: int = 8_000
    max_result_chars: int = 6_000
    max_grep_matches: int = 200
    command_timeout_s: int = 120


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    bell: bool = True
    toast: bool = True


@dataclass(frozen=True, slots=True)
class BackupConfig:
    keep_sessions: int = 5


@dataclass(frozen=True, slots=True)
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    clipboard: ClipboardConfig = field(default_factory=ClipboardConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    services: dict[str, ServicePreset] = field(default_factory=default_services)
    warnings: tuple[str, ...] = ()  # non-fatal validation complaints, for the TUI to surface

    def preset(self) -> ServicePreset:
        try:
            return self.services[self.general.service]
        except KeyError:
            return self.services["unknown"]

    def caps(self) -> BudgetCaps:
        return caps_for_budget(self.preset().max_paste_chars)

    def excluded_names(self) -> frozenset[str]:
        return frozenset(self.exclude) | HARD_EXCLUDED_NAMES


def default_global_config_path() -> Path:
    return Path(platformdirs.user_config_dir("agentclip")) / "config.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value  # scalars AND lists replace
    return out


def _read_toml(path: Path, warnings: list[str]) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        warnings.append(f"config: could not read {path}: {exc}")
        return {}


def _take_int(table: dict, key: str, default: int, lo: int, hi: int, ctx: str, warnings: list[str]) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        warnings.append(f"config: [{ctx}] {key} must be an integer; using {default}")
        return default
    if not (lo <= value <= hi):
        warnings.append(f"config: [{ctx}] {key}={value} outside {lo}..{hi}; using {default}")
        return default
    return value


def _take_float(table: dict, key: str, default: float, lo: float, hi: float, ctx: str, warnings: list[str]) -> float:
    value = table.get(key, default)
    # TOML users will write `2` as readily as `2.0`; both are numbers here.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"config: [{ctx}] {key} must be a number; using {default}")
        return default
    if not (lo <= value <= hi):
        warnings.append(f"config: [{ctx}] {key}={value} outside {lo}..{hi}; using {default}")
        return default
    return float(value)


def _take_bool(table: dict, key: str, default: bool, ctx: str, warnings: list[str]) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        warnings.append(f"config: [{ctx}] {key} must be true/false; using {default}")
        return default
    return value


def _take_str(table: dict, key: str, default: str, ctx: str, warnings: list[str]) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        warnings.append(f"config: [{ctx}] {key} must be a string; using {default!r}")
        return default
    return value


def _take_str_list(table: dict, key: str, default: tuple[str, ...], ctx: str, warnings: list[str]) -> tuple[str, ...]:
    value = table.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        warnings.append(f"config: [{ctx}] {key} must be a list of strings; using defaults")
        return default
    return tuple(value)


def load_config(
    project_root: Path,
    *,
    service_override: str | None = None,
    global_config_path: Path | None = None,
) -> Config:
    """Load, merge, and validate configuration. Never raises on bad user config;
    problems become Config.warnings and defaults win."""
    warnings: list[str] = []
    global_path = global_config_path if global_config_path is not None else default_global_config_path()

    merged: dict = {}
    for path in (global_path, project_root / ".agentclip.toml"):
        merged = _deep_merge(merged, _read_toml(path, warnings))

    general_t = merged.get("general", {})
    clipboard_t = merged.get("clipboard", {})
    approval_t = merged.get("approval", {})
    limits_t = merged.get("limits", {})
    notify_t = merged.get("notify", {})
    backup_t = merged.get("backup", {})
    paths_t = merged.get("paths", {})

    services = default_services()
    for key, table in merged.get("services", {}).items():
        if not isinstance(table, dict):
            warnings.append(f"config: [services.{key}] must be a table; ignored")
            continue
        base = services.get(key)
        ctx = f"services.{key}"
        preset = ServicePreset(
            key=key,
            label=_take_str(table, "label", base.label if base else key, ctx, warnings),
            max_paste_chars=_take_int(
                table, "max_paste_chars", base.max_paste_chars if base else 6_000, 500, 2_000_000, ctx, warnings
            ),
            total_context_chars=_take_int(
                table,
                "total_context_chars",
                base.total_context_chars if base else 100_000,
                500,
                10_000_000,
                ctx,
                warnings,
            ),
            wrap_blocks_in_fence=_take_bool(
                table, "wrap_blocks_in_fence", base.wrap_blocks_in_fence if base else True, ctx, warnings
            ),
            attachment_note=_take_bool(
                table, "attachment_note", base.attachment_note if base else True, ctx, warnings
            ),
            stable_seconds=_take_float(
                table,
                "stable_seconds",
                base.stable_seconds if base else DEFAULT_STABLE_SECONDS,
                0.5,
                60.0,
                ctx,
                warnings,
            ),
        )
        if preset.max_paste_chars > preset.total_context_chars:
            warnings.append(
                f"config: [{ctx}] max_paste_chars ({preset.max_paste_chars:,}) exceeds "
                f"total_context_chars ({preset.total_context_chars:,})"
            )
        services[key] = preset

    service = service_override or _take_str(general_t, "service", "chatgpt-attach", "general", warnings)
    if service not in services:
        warnings.append(f"config: unknown service preset {service!r}; using 'unknown'")
        service = "unknown"

    provider = _take_str(clipboard_t, "provider", "auto", "clipboard", warnings)
    if provider not in ("auto", "copykitten", "pyperclip", "manual"):
        warnings.append(f"config: unknown clipboard provider {provider!r}; using 'auto'")
        provider = "auto"

    theme = _take_str(general_t, "theme", DEFAULT_THEME, "general", warnings)
    if theme not in VALID_THEMES:
        warnings.append(f"config: unknown theme {theme!r}; using {DEFAULT_THEME!r}")
        theme = DEFAULT_THEME

    return Config(
        general=GeneralConfig(
            service=service,
            chars_per_token=_take_int(general_t, "chars_per_token", 3, 1, 10, "general", warnings),
            theme=theme,
        ),
        clipboard=ClipboardConfig(
            provider=provider,
            poll_interval_ms=_take_int(clipboard_t, "poll_interval_ms", 300, 100, 5_000, "clipboard", warnings),
        ),
        approval=ApprovalConfig(
            auto_accept_edits=_take_bool(approval_t, "auto_accept_edits", False, "approval", warnings),
            yolo=_take_bool(approval_t, "yolo", False, "approval", warnings),
            command_allowlist=_take_str_list(
                approval_t, "command_allowlist", DEFAULT_ALLOWLIST, "approval", warnings
            ),
            command_deny_tokens=_take_str_list(
                approval_t, "command_deny_tokens", DEFAULT_DENY_TOKENS, "approval", warnings
            ),
        ),
        limits=LimitsConfig(
            max_file_read_chars=_take_int(limits_t, "max_file_read_chars", 20_000, 500, 10_000_000, "limits", warnings),
            max_command_output_chars=_take_int(
                limits_t, "max_command_output_chars", 8_000, 500, 10_000_000, "limits", warnings
            ),
            max_result_chars=_take_int(limits_t, "max_result_chars", 6_000, 200, 10_000_000, "limits", warnings),
            max_grep_matches=_take_int(limits_t, "max_grep_matches", 200, 1, 100_000, "limits", warnings),
            command_timeout_s=_take_int(limits_t, "command_timeout_s", 120, 1, 86_400, "limits", warnings),
        ),
        notify=NotifyConfig(
            bell=_take_bool(notify_t, "bell", True, "notify", warnings),
            toast=_take_bool(notify_t, "toast", True, "notify", warnings),
        ),
        backup=BackupConfig(
            keep_sessions=_take_int(backup_t, "keep_sessions", 5, 1, 1_000, "backup", warnings),
        ),
        exclude=_take_str_list(paths_t, "exclude", DEFAULT_EXCLUDES, "paths", warnings),
        services=services,
        warnings=tuple(warnings),
    )


def save_services(services: dict[str, ServicePreset], path: Path | None = None) -> None:
    """Persist ``services`` (the complete desired preset table) into the global
    config.toml at ``path`` (default: :func:`default_global_config_path`).

    Only presets that differ from the built-in defaults are written as
    ``[services.<key>]`` tables; a preset equal to its built-in (including one
    just "reset to default") is omitted, so the file stays minimal and future
    tweaks to the shipped defaults keep applying to it. A built-in key that is
    simply absent from ``services`` is treated the same as "reset to default"
    (deletion is only ever offered by the editor for non-built-in keys, but
    this function itself doesn't special-case that - it just writes what it's
    given). Every other top-level table/key already in the file (``general``,
    ``approval``, a user's hand-written comments' *content* if not comments
    themselves, etc.) is preserved verbatim; TOML comments are NOT preserved
    (``tomllib`` doesn't retain them - acceptable per the design brief).

    Writes atomically: the new content is written to a temp file in the same
    directory, then swapped into place with :func:`os.replace`, so a crash or
    power loss mid-write can never leave a truncated/corrupt config.toml.

    Round-trips with :func:`load_config`: calling this then loading again
    (with the same ``global_config_path``) reproduces the same presets.
    """
    target = path if path is not None else default_global_config_path()
    discard_warnings: list[str] = []
    data = _read_toml(target, discard_warnings)
    defaults = default_services()

    services_table: dict[str, dict[str, object]] = {}
    for key, preset in services.items():
        if key in defaults and preset == defaults[key]:
            continue  # untouched (or reset) built-in: don't dump it
        services_table[key] = {
            "label": preset.label,
            "max_paste_chars": preset.max_paste_chars,
            "total_context_chars": preset.total_context_chars,
            "wrap_blocks_in_fence": preset.wrap_blocks_in_fence,
            "attachment_note": preset.attachment_note,
        }
        # Written only when it actually differs from the built-in (or, for a
        # custom key, from the dataclass default): the field arrived after the
        # five above, and a file whose user never touched the stale knob should
        # stay byte-for-byte what earlier versions wrote.
        base_stable = defaults[key].stable_seconds if key in defaults else DEFAULT_STABLE_SECONDS
        if preset.stable_seconds != base_stable:
            services_table[key]["stable_seconds"] = preset.stable_seconds

    data = dict(data)
    if services_table:
        data["services"] = services_table
    else:
        data.pop("services", None)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp_name, target)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise


def save_theme(theme: str, path: Path | None = None) -> None:
    """Persist ``theme`` as ``[general] theme`` into the global config.toml at
    ``path`` (default: :func:`default_global_config_path`).

    Mirrors :func:`save_services`'s atomic-write behaviour: the new content is
    written to a temp file in the same directory then swapped into place with
    :func:`os.replace`, so a crash mid-write can never corrupt config.toml.

    Only the ``theme`` key under ``[general]`` is touched - any other key
    already in ``[general]`` (e.g. ``service``, ``chars_per_token``) and every
    other top-level table are preserved verbatim. ``path`` is a parameter
    (rather than always resolving :func:`default_global_config_path`) so
    tests can point it at a tmp file instead of the user's real config.
    """
    target = path if path is not None else default_global_config_path()
    discard_warnings: list[str] = []
    data = _read_toml(target, discard_warnings)

    general_table = dict(data.get("general", {}))
    general_table["theme"] = theme

    data = dict(data)
    data["general"] = general_table

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp_name, target)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp_name)
        raise
