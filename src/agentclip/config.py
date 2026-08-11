"""Configuration: frozen dataclasses + TOML load/merge/validate.

A leaf (platformdirs, tomli_w, and the two other leaves: permissions.py, shared
with the approval policy, and the Host seam, which is how the project file is
read). Precedence, later wins, per-key shallow merge per table — lists REPLACE,
never concatenate (so a project can tighten the allowlist):

    built-in defaults
    -> <user_config_dir>/agentclip/config.toml
    -> <project root>/.agentclip.toml
    -> CLI flags

The global file is always LOCAL; the project file belongs to the project, so in
a remote session it is read from the remote machine through the Host (hence the
``host`` parameter on :func:`load_config`). Permissions are the deliberate
exception: opencode.json is read from this PC whatever the session is, because
the user's policy must not weaken because of a remote file
(docs/design/remote-ssh.md decision 6).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path

import platformdirs
import tomli_w

from agentclip.hosts.base import Host
from agentclip.permissions import PermissionRule, default_rules, rules_from_config

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

# Which finish detectors a service is allowed to run, in the canonical order the
# poller builds (and posts) them in - the same order tui/screens/main.py relies
# on to know which message closes a tick:
#   "busy"  - the reasoning/stop icon disappearing (needs a BUSY capture)
#   "idle"  - the send icon reappearing (needs an IDLE capture)
#   "stale" - the drawn chat region going still (needs no capture at all)
# A checklist rather than a mode: they reinforce each other, and which of them a
# particular chat UI can be trusted about is a property of the service.
FINISH_SIGNALS: tuple[str, ...] = ("busy", "idle", "stale")
# Staleness alone is what every freshly drawn window can already do, so it is
# what a service ships with. An EMPTY checklist is legal and means "never detect
# a finish for this service" - the user drives the copy button themselves.
DEFAULT_FINISH_SIGNALS: tuple[str, ...] = ("stale",)


# How an outbound payload gets from the clipboard into the chat box once the
# focus click has landed. "paste" is one clipboard write and one synthetic
# Ctrl+V; "stream" walks the payload in through a run of them, so a very large
# message shows visible progress instead of stalling the page on one huge paste.
DELIVERY_PASTE = "paste"
DELIVERY_STREAM = "stream"
DELIVERY_MODES: tuple[str, ...] = (DELIVERY_PASTE, DELIVERY_STREAM)
DEFAULT_DELIVERY = DELIVERY_PASTE


# How the auto-copy flow snaps the transcript to the bottom before hunting for
# the newest copy button:
#   "scroll"    - a mouse-wheel flick over the chat region (the default; needs
#                 no keyboard focus at all).
#   "page_down" - a burst of Page Down taps into the focused chat window.
#   "end"       - one End tap into the focused chat window.
# The keyboard forms exist for pages where a wheel flick does not land (a
# virtualized transcript, a chat that captures wheel events) - but they go to
# whatever has FOCUS, so they only work where the chat scrolls on those keys
# with its input box focused. Per service, because that is a fact about the
# page.
SCROLL_WHEEL = "scroll"
SCROLL_PAGE_DOWN = "page_down"
SCROLL_END = "end"
SCROLL_ACTIONS: tuple[str, ...] = (SCROLL_WHEEL, SCROLL_PAGE_DOWN, SCROLL_END)
DEFAULT_SCROLL_ACTION = SCROLL_WHEEL


# How a service's captured appearances are hunted for on a frame:
#   "anchors" - the built-in fingerprint search (screen.template). No
#               dependency, fast, and blind to a capture whose shades have
#               drifted past a quantisation edge.
#   "opencv"  - an exhaustive correlation sweep (screen.matchers). Needs the
#               optional `agentclip[cv]` extra; falls back to anchors, with a
#               warning in the editor, when it is not installed.
# Only CANDIDATE GENERATION differs: both are verified by the same tolerant
# per-pixel comparison, which is why one `tolerance` below governs both.
#
# Spelled out here rather than imported from screen.matchers: config is a
# stdlib-only leaf that may not import the screen layer (architecture.md 0),
# exactly as VALID_THEMES is spelled out rather than asked of Textual.
# tests/screen/test_matchers.py asserts the two lists agree.
MATCHER_ANCHORS = "anchors"
MATCHER_OPENCV = "opencv"
MATCHERS: tuple[str, ...] = (MATCHER_ANCHORS, MATCHER_OPENCV)
DEFAULT_MATCHER = MATCHER_ANCHORS

# How far a single colour channel may drift before a pixel counts as different.
# Per service because it is a fact about a browser and a theme, not about a
# button: a chat rendered with heavy sub-pixel anti-aliasing, or one whose page
# tints its controls on hover, needs more slack than a flat dark UI.
#
# Deliberately NOT the same knob as `max_diff`, which stays per TemplateKind
# (screen.profile): tolerance is how different one PIXEL may be, max_diff is how
# many pixels may be that different, and the second is a property of the kind of
# control being looked for (an icon sitting on whatever text is behind it needs
# a looser one than a chat box). The value below must match
# screen.template.DEFAULT_TOLERANCE - asserted by tests/screen/test_matchers.py.
DEFAULT_TOLERANCE = 24
TOLERANCE_MIN = 0
TOLERANCE_MAX = 64


def normalize_finish_signals(values: Iterable[str]) -> tuple[str, ...]:
    """Drop unknown entries, dedupe, and return them in :data:`FINISH_SIGNALS`
    order, so a hand-written (or future editor-written) checklist can never make
    the poller build a detector it does not know, nor post two verdicts for one
    detector, nor shuffle the order that decides which message closes a tick."""
    chosen = {value for value in values if value in FINISH_SIGNALS}
    return tuple(name for name in FINISH_SIGNALS if name in chosen)


@dataclass(frozen=True, slots=True)
class ServicePreset:
    key: str
    label: str
    max_paste_chars: int  # budget for a single paste (chunking splits above it)
    total_context_chars: int  # the service's whole conversation window, ~chars (tokens * ~4)
    wrap_blocks_in_fence: bool = True
    attachment_note: bool = True
    stable_seconds: float = DEFAULT_STABLE_SECONDS  # stale detector: stillness = finished
    # Which of FINISH_SIGNALS this service's poller may run (see above).
    finish_signals: tuple[str, ...] = DEFAULT_FINISH_SIGNALS
    # May the auto-copy flow glide the real cursor up the chat region hunting a
    # copy icon that only renders under the pointer? Off by default: it is a
    # visible, slow takeover of the user's mouse, and only some chats (Claude's)
    # need it at all.
    hover_scan: bool = False
    # One of DELIVERY_MODES. "paste" everywhere by default: chunked delivery is
    # slower and leaves a half-written message in the box if it is interrupted,
    # so it is worth it only where a single large paste visibly stalls the page.
    delivery: str = DEFAULT_DELIVERY
    # One of MATCHERS: how this service's appearances are hunted for (see above).
    matcher: str = DEFAULT_MATCHER
    # Per-channel slack in the shared verification, 0..64 (see above).
    tolerance: int = DEFAULT_TOLERANCE
    # One of SCROLL_ACTIONS: how the auto-copy flow reaches the newest reply
    # (see above). "scroll" everywhere by default - the keyboard forms only
    # work on pages that scroll on those keys.
    scroll_action: str = DEFAULT_SCROLL_ACTION
    # May AgentClip press Enter itself right after a successful auto-paste?
    # Off by default: submitting a message is the one act the loop otherwise
    # always leaves to the user, and a synthetic Enter into the wrong widget is
    # a sent half-thought. The send gate still verifies the send either way.
    auto_submit: bool = False
    # May the auto-copy flow's verified copy click ingest a reply that carries
    # NO CLIP blocks at all, showing it in the transcript as prose? Off by
    # default per protocol.md 1.4 tolerance #11 (non-protocol clipboard text is
    # ignored); scoped to the flow's own click - the watcher never loosens, so
    # the user's ordinary copies stay invisible either way.
    capture_prose: bool = False


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
    # Which service the SUB-AGENT window tab starts on (tui.md 1.6). The two
    # browser windows AgentClip drives are independently pointed at a service -
    # a big-context chat for the conversation you steer, a cheap fast one for
    # delegated sub-tasks - so the sub-agent tab needs a preset of its own.
    # Blank means "the same one as the master tab's", which is what makes this
    # key invisible to anybody who does not want two services.
    subagent_service: str = ""
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
class PermissionConfig:
    """Where the OpenCode-style permission ruleset comes from, and whether it is
    consulted at all. Off (or a missing file) leaves the legacy allowlist gate
    in charge - see engine/approval.py."""

    enabled: bool = True
    opencode_config: str = ""  # blank = default_opencode_config_path()


@dataclass(frozen=True, slots=True)
class RemoteTarget:
    """One machine a session can be pointed at (a ``[remote.<name>]`` table).

    ``port``/``root`` may be blank: the port then falls back to ~/.ssh/config
    (and 22), and the root has to arrive on the command line. ``host`` may be an
    ssh_config alias - that is the point of parsing ssh_config at all.
    """

    name: str = ""
    host: str = ""
    user: str = ""
    port: int = 0  # 0 = whatever ~/.ssh/config says, else 22
    root: str = ""  # the project root ON the remote machine (POSIX)


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """Where this session runs, and the saved targets it could have run on.

    ``target``/``root`` come from ``--ssh``/``--remote-root``; the saved
    ``targets`` come from the LOCAL config files only. A remote session is one
    with a target: everything else here describes what a target could mean.
    One session is one machine (design decision 4) - there is no switching.
    """

    target: str = ""  # "" = a local session
    root: str = ""  # --remote-root, overriding the saved target's
    targets: dict[str, RemoteTarget] = field(default_factory=dict)

    def is_remote(self) -> bool:
        return bool(self.target)

    def selected(self) -> RemoteTarget | None:
        """The target this session runs on, or None for a local session.

        A ``--ssh`` value naming a saved target IS that target (with
        ``--remote-root`` overriding its root); anything else is read as
        ``[user@]host[:port]``, which covers both a bare ssh_config alias and a
        fully spelled-out destination.
        """
        if not self.target:
            return None
        saved = self.targets.get(self.target)
        if saved is not None:
            return replace(saved, root=self.root or saved.root)
        user, _, rest = self.target.rpartition("@")
        host, colon, port = rest.partition(":")
        return RemoteTarget(
            name=self.target,
            host=host,
            user=user,
            port=int(port) if colon and port.isdigit() else 0,
            root=self.root,
        )


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
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    # The effective ruleset (built-in defaults first, the user's opencode.json
    # appended). EMPTY means "no ruleset": the legacy allowlist gate stays in
    # charge, which is what every install without an opencode.json gets.
    permission_rules: tuple[PermissionRule, ...] = ()
    permission_source: str = ""  # the file the rules came from, "" when none did
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


def default_opencode_config_path() -> Path:
    """OpenCode's own config file. AgentClip reads the SAME file rather than
    inventing a parallel permission format: a user who has already told OpenCode
    which commands they trust has already told AgentClip.

    Not under platformdirs: OpenCode uses ``~/.config/opencode`` on every
    platform, Windows included, so that is where the file is."""
    return Path.home() / ".config" / "opencode" / "opencode.json"


def default_remote_state_dir(target: str, root: str) -> Path:
    """Where a REMOTE session's ``.agentclip`` tree lives - on this PC.

    Sessions, transcripts and backups are AgentClip's own state, so they stay
    local (docs/design/remote-ssh.md: "backups keep storing locally"); but the
    project root they would normally sit beside is on another machine. One
    directory per (target, root) pair, named for both so a human can find it,
    with a short digest to keep two similar-looking roots apart.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{target}-{root}").strip("-")[:60]
    digest = hashlib.sha256(f"{target}\n{root}".encode()).hexdigest()[:8]
    return Path(platformdirs.user_data_dir("agentclip")) / "remote" / f"{slug or 'target'}-{digest}"


def default_profile_dir() -> Path:
    """Where per-service appearance profiles live (screen.profile_store).

    Resolved here rather than in the screen layer: that layer takes its root as
    a parameter (it may not import platformdirs), and this is the one place
    that already knows the app's config home.
    """
    return Path(platformdirs.user_config_dir("agentclip")) / "profiles"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value  # scalars AND lists replace
    return out


def _read_toml(path: Path, warnings: list[str], host: Host | None = None) -> dict:
    """Parse one TOML file, from this PC or from ``host`` when one is given."""
    try:
        raw = host.read_bytes(path) if host is not None else path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        warnings.append(f"config: could not read {path}: {exc}")
        return {}
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
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


def _take_finish_signals(
    table: dict, default: tuple[str, ...], ctx: str, warnings: list[str]
) -> tuple[str, ...]:
    """Read a finish-detector checklist, normalized (see
    :func:`normalize_finish_signals`). An empty list is a legal answer - it says
    "no finish detection for this service" - so it is not confused with an
    absent key; entries that name no detector are dropped with a warning, since
    a typo'd checklist silently doing less than the user asked for is exactly
    the failure that leaves auto-copy mysteriously dead."""
    value = table.get("finish_signals")
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        warnings.append(f"config: [{ctx}] finish_signals must be a list of strings; using defaults")
        return default
    signals = normalize_finish_signals(value)
    unknown = sorted({x for x in value if x not in FINISH_SIGNALS})
    if unknown:
        warnings.append(
            f"config: [{ctx}] finish_signals: unknown detector(s) {', '.join(unknown)}; "
            f"known: {', '.join(FINISH_SIGNALS)}"
        )
    return signals


def _take_delivery(table: dict, default: str, ctx: str, warnings: list[str]) -> str:
    """Read an outbound delivery mode, falling back to ``default`` for anything
    that is not one of :data:`DELIVERY_MODES`. Warned about rather than accepted:
    an unknown mode would otherwise read as "paste" silently, and the whole
    reason a user writes this key is that one big paste does not work for them."""
    value = table.get("delivery", default)
    if not isinstance(value, str) or value not in DELIVERY_MODES:
        warnings.append(
            f"config: [{ctx}] delivery must be one of {', '.join(DELIVERY_MODES)}; "
            f"using {default!r}"
        )
        return default
    return value


def _take_scroll_action(table: dict, default: str, ctx: str, warnings: list[str]) -> str:
    """Read an auto-copy scroll action, falling back to ``default`` for anything
    that is not one of :data:`SCROLL_ACTIONS`. Warned about rather than
    accepted, for the same reason as :func:`_take_delivery`: a user who writes
    this key has a transcript the wheel flick is not reaching, and silently
    flicking the wheel anyway is the one outcome that teaches them nothing."""
    value = table.get("scroll_action", default)
    if not isinstance(value, str) or value not in SCROLL_ACTIONS:
        warnings.append(
            f"config: [{ctx}] scroll_action must be one of {', '.join(SCROLL_ACTIONS)}; "
            f"using {default!r}"
        )
        return default
    return value


def _take_matcher(table: dict, default: str, ctx: str, warnings: list[str]) -> str:
    """Read a candidate-generation backend, falling back to ``default`` for
    anything that is not one of :data:`MATCHERS`. Warned about rather than
    accepted, for the same reason as :func:`_take_delivery`: a user who writes
    this key has a search that is not finding their buttons, and silently
    running the backend they were trying to move off is the one outcome that
    teaches them nothing. (Whether the named backend can actually RUN is a
    different question, answered per machine by screen.matchers and surfaced in
    the editor - an uninstallable name is not a config error.)"""
    value = table.get("matcher", default)
    if not isinstance(value, str) or value not in MATCHERS:
        warnings.append(
            f"config: [{ctx}] matcher must be one of {', '.join(MATCHERS)}; using {default!r}"
        )
        return default
    return value


def _load_permission_rules(
    settings: PermissionConfig, warnings: list[str]
) -> tuple[tuple[PermissionRule, ...], str]:
    """Read opencode.json's top-level ``permission`` block into the effective
    ruleset, defaults first.

    Only that one key is read. OpenCode's ``agent``/``plugin`` blocks describe
    OpenCode agents, which AgentClip has no equivalent of - guessing a mapping
    would silently grant or refuse things the user never decided.

    A missing file is not a problem (most machines have none): it returns an
    empty ruleset, which is the signal for legacy mode. Only a file that EXISTS
    and cannot be understood warns."""
    if not settings.enabled:
        return (), ""
    path = (
        Path(settings.opencode_config).expanduser()
        if settings.opencode_config
        else default_opencode_config_path()
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), ""
    except OSError as exc:
        warnings.append(f"config: could not read {path}: {exc}")
        return (), ""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        warnings.append(f"config: {path} is not valid JSON: {exc}")
        return (), ""
    if not isinstance(data, dict) or "permission" not in data:
        return (), ""
    rules, rule_warnings = rules_from_config(data["permission"])
    warnings.extend(f"config: {path}: {w}" for w in rule_warnings)
    if not rules:
        return (), ""
    return default_rules() + rules, str(path)


def _load_remote(
    table: dict, target: str, root: str, warnings: list[str]
) -> RemoteConfig:
    """Read the ``[remote.<name>]`` saved targets and the session's selection."""
    targets: dict[str, RemoteTarget] = {}
    for key, entry in table.items():
        ctx = f"remote.{key}"
        if not isinstance(entry, dict):
            warnings.append(f"config: [{ctx}] must be a table; ignored")
            continue
        host = _take_str(entry, "host", key, ctx, warnings)
        targets[key] = RemoteTarget(
            name=key,
            host=host or key,  # a target named for its host need not repeat it
            user=_take_str(entry, "user", "", ctx, warnings),
            port=_take_int(entry, "port", 0, 0, 65_535, ctx, warnings),
            root=_take_str(entry, "root", "", ctx, warnings),
        )
    if target and target not in targets and "@" not in target and ":" not in target:
        # Not a saved name and not a spelled-out destination: it can still be an
        # ~/.ssh/config alias, which is legitimate - say so rather than fail.
        warnings.append(
            f"config: --ssh {target!r} names no [remote.{target}] target; "
            "treating it as a host name or ~/.ssh/config alias"
        )
    return RemoteConfig(target=target, root=root, targets=targets)


def load_config(
    project_root: Path,
    *,
    service_override: str | None = None,
    global_config_path: Path | None = None,
    remote_target: str | None = None,
    remote_root: str | None = None,
    host: Host | None = None,
) -> Config:
    """Load, merge, and validate configuration. Never raises on bad user config;
    problems become Config.warnings and defaults win.

    ``host`` is the machine the PROJECT is on: its ``.agentclip.toml`` is read
    through it, so a remote session honors the remote project's settings. The
    global file is read from this PC either way.
    """
    warnings: list[str] = []
    global_path = global_config_path if global_config_path is not None else default_global_config_path()

    merged: dict = _read_toml(global_path, warnings)
    merged = _deep_merge(merged, _read_toml(project_root / ".agentclip.toml", warnings, host))

    general_t = merged.get("general", {})
    clipboard_t = merged.get("clipboard", {})
    approval_t = merged.get("approval", {})
    limits_t = merged.get("limits", {})
    notify_t = merged.get("notify", {})
    backup_t = merged.get("backup", {})
    paths_t = merged.get("paths", {})
    permission_t = merged.get("permission", {})
    remote_t = merged.get("remote", {})

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
            finish_signals=_take_finish_signals(
                table,
                base.finish_signals if base else DEFAULT_FINISH_SIGNALS,
                ctx,
                warnings,
            ),
            hover_scan=_take_bool(table, "hover_scan", base.hover_scan if base else False, ctx, warnings),
            delivery=_take_delivery(
                table, base.delivery if base else DEFAULT_DELIVERY, ctx, warnings
            ),
            matcher=_take_matcher(table, base.matcher if base else DEFAULT_MATCHER, ctx, warnings),
            tolerance=_take_int(
                table,
                "tolerance",
                base.tolerance if base else DEFAULT_TOLERANCE,
                TOLERANCE_MIN,
                TOLERANCE_MAX,
                ctx,
                warnings,
            ),
            scroll_action=_take_scroll_action(
                table, base.scroll_action if base else DEFAULT_SCROLL_ACTION, ctx, warnings
            ),
            auto_submit=_take_bool(
                table, "auto_submit", base.auto_submit if base else False, ctx, warnings
            ),
            capture_prose=_take_bool(
                table, "capture_prose", base.capture_prose if base else False, ctx, warnings
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

    # The sub-agent window's service. Blank (the default) is not an error - it
    # means "whatever the master tab is on", so the key can simply be absent.
    subagent_service = _take_str(general_t, "subagent_service", "", "general", warnings)
    if subagent_service and subagent_service not in services:
        warnings.append(
            f"config: unknown subagent_service preset {subagent_service!r}; "
            "using the master's service"
        )
        subagent_service = ""

    provider = _take_str(clipboard_t, "provider", "auto", "clipboard", warnings)
    if provider not in ("auto", "copykitten", "pyperclip", "manual"):
        warnings.append(f"config: unknown clipboard provider {provider!r}; using 'auto'")
        provider = "auto"

    theme = _take_str(general_t, "theme", DEFAULT_THEME, "general", warnings)
    if theme not in VALID_THEMES:
        warnings.append(f"config: unknown theme {theme!r}; using {DEFAULT_THEME!r}")
        theme = DEFAULT_THEME

    permission = PermissionConfig(
        enabled=_take_bool(permission_t, "enabled", True, "permission", warnings),
        opencode_config=_take_str(permission_t, "opencode_config", "", "permission", warnings),
    )
    permission_rules, permission_source = _load_permission_rules(permission, warnings)

    return Config(
        general=GeneralConfig(
            service=service,
            subagent_service=subagent_service,
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
        permission=permission,
        permission_rules=permission_rules,
        permission_source=permission_source,
        remote=_load_remote(
            remote_t if isinstance(remote_t, dict) else {},
            remote_target or "",
            remote_root or "",
            warnings,
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
        base = defaults.get(key)
        base_stable = base.stable_seconds if base else DEFAULT_STABLE_SECONDS
        if preset.stable_seconds != base_stable:
            services_table[key]["stable_seconds"] = preset.stable_seconds
        # Same rule for the detection knobs, which arrived later still: written
        # only when the user has actually moved them off the built-in. An empty
        # checklist is a real setting ("detect nothing here"), so it is written
        # as `finish_signals = []` rather than omitted.
        base_signals = base.finish_signals if base else DEFAULT_FINISH_SIGNALS
        if preset.finish_signals != base_signals:
            services_table[key]["finish_signals"] = list(preset.finish_signals)
        if preset.hover_scan != (base.hover_scan if base else False):
            services_table[key]["hover_scan"] = preset.hover_scan
        if preset.delivery != (base.delivery if base else DEFAULT_DELIVERY):
            services_table[key]["delivery"] = preset.delivery
        # The matching knobs, newest of all, under the same rule: a user who has
        # never opened the MATCHING block gets a file that does not mention it.
        if preset.matcher != (base.matcher if base else DEFAULT_MATCHER):
            services_table[key]["matcher"] = preset.matcher
        if preset.tolerance != (base.tolerance if base else DEFAULT_TOLERANCE):
            services_table[key]["tolerance"] = preset.tolerance
        # The automation knobs that arrived with the scroll/auto-submit wave,
        # under the same write-only-when-moved rule as everything above.
        if preset.scroll_action != (base.scroll_action if base else DEFAULT_SCROLL_ACTION):
            services_table[key]["scroll_action"] = preset.scroll_action
        if preset.auto_submit != (base.auto_submit if base else False):
            services_table[key]["auto_submit"] = preset.auto_submit
        if preset.capture_prose != (base.capture_prose if base else False):
            services_table[key]["capture_prose"] = preset.capture_prose

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
