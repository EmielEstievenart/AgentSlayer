# AgentClip Architecture Design

Decisive design for codebase structure, config, persistence, sandboxing, and testing. Companion documents: protocol grammar (protocol designer), widget/UX detail (TUI designer). Cross-cutting requirements for them are in §11.

---

## 0. Architectural prime directive (restated as enforced rule)

The **engine is sans-IO with respect to clipboard and UI**: it is a synchronous state machine that consumes *strings* (ingested text, user decisions, user answers) and returns *values* (outbound payload strings, pending actions, results). It performs filesystem and subprocess side effects only through the tool layer, never touches the clipboard, and never imports Textual.

Dependency direction (imports may only point downward; enforced by a lint test, see §8):

```
tui  ──►  clip (watcher/providers)
 │
 ▼
engine  ──►  tools  ──►  sandbox (Workspace)
 │      ──►  store (session, backups)
 ▼
protocol (parser, composer)   ──►  (nothing but stdlib)
 ▲
config (leaf, stdlib-only)  ◄── imported by everyone
```

`clip` is imported **only** by `tui` and `cli`. `protocol` and `config` are leaves. `tools` never imports `engine`. Anything violating this is a bug.

---

## 1. Module layout

```
src/agentclip/
├── __init__.py            # __version__ only
├── __main__.py            # python -m agentclip → cli.main()
├── cli.py                 # argparse (--project, --service, --version); builds Config, wires Engine + TUI
├── config.py              # frozen dataclasses + TOML load/merge/validate (stdlib tomllib)
│
├── protocol/
│   ├── types.py           # wire-level dataclasses (ToolCall, ParsedTurn, ParseIssue, Outbound)
│   ├── parser.py          # tolerant sentinel-block parser: str → ParsedTurn
│   ├── composer.py        # bootstrap / results / chunk payload rendering + budget splitting
│   └── spec.py            # protocol-spec text templates shown to the LLM (incl. per-service variants)
│
├── engine/
│   ├── engine.py          # Engine: the session state machine (the only orchestrator)
│   ├── states.py          # Phase enum + legal-transition table
│   ├── approval.py        # ApprovalPolicy: allowlist matching, session escalation flags
│   └── results.py         # ToolResult + middle-truncation to configured size caps
│
├── tools/
│   ├── registry.py        # ToolRegistry: name → ToolSpec; render_catalog() for the bootstrap prompt
│   ├── sandbox.py         # Workspace: project-root jail, path resolution, exclusion rules
│   ├── fs_tools.py        # read_file, write_file, edit_file, list_dir, glob, grep (pure-Python re scan)
│   ├── shell.py           # run_command: subprocess.run, timeout, combined-output capture
│   └── meta.py            # ask_user, task_done (no side effects; engine interprets)
│
├── store/
│   ├── session.py         # SessionStore: .agentclip/ layout, transcript JSONL append, outbound dumps
│   └── backups.py         # BackupStore: per-turn copy-on-first-touch snapshots, undo, retention
│
├── clip/
│   ├── base.py            # ClipboardProvider Protocol + select_provider()
│   ├── copykitten_provider.py
│   ├── pyperclip_provider.py
│   ├── winseq.py          # ctypes GetClipboardSequenceNumber shim (≤15 lines)
│   ├── watcher.py         # poll loop (plain function, thread-agnostic), self-write suppression
│   └── fake.py            # FakeClipboard + ScriptedClipboard for tests
│
└── tui/
    ├── app.py             # AgentClipApp(App); CSS embedded in class var (PyInstaller, §7)
    ├── messages.py        # ClipboardCaptured, WatcherStateChanged, ... (Textual Message subclasses)
    ├── screens/
    │   ├── main.py        # transcript + pending/diff panel + status bar + task input
    │   ├── approve.py     # ApproveScreen(ModalScreen[Decision]) with y/n/a bindings
    │   └── settings.py    # M3: config editor screen
    └── widgets/
        ├── transcript.py  # VerticalScroll of per-message widgets, .anchor() pinning
        ├── diffview.py    # Static wrapping rich.syntax.Syntax(diff_text, "diff")
        └── statusbar.py   # docked Horizontal: watcher state, budget, service, phase
```

### Key signatures

```python
# protocol/types.py ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str                      # unique within a turn (parser enforces)
    tool: str
    args: dict[str, str]         # scalar "key: value" fields
    blocks: dict[str, str]       # heredoc fields (find/replace/content)
    raw: str                     # verbatim block text for transcript/audit

@dataclass(frozen=True, slots=True)
class ParseIssue:
    kind: Literal["missing_end", "bad_header", "duplicate_id",
                  "unterminated_heredoc", "unknown_tool", "truncation_suspected"]
    line: int
    detail: str

@dataclass(frozen=True, slots=True)
class ParsedTurn:
    prose: str                   # everything outside blocks (shown, never executed)
    calls: tuple[ToolCall, ...]
    issues: tuple[ParseIssue, ...]   # non-empty ⇒ engine refuses to execute, requests re-emit

# protocol/parser.py -----------------------------------------------------
PROTOCOL_MARKER = "===CLIP:"
def looks_like_protocol(text: str) -> bool        # cheap watcher pre-filter (substring test)
def parse_reply(text: str) -> ParsedTurn          # tolerates BOM, CRLF, ``` fences, pre/post junk

# protocol/composer.py ---------------------------------------------------
@dataclass(frozen=True, slots=True)
class Outbound:
    kind: Literal["bootstrap", "results", "chunk", "user_answer"]
    chunks: tuple[str, ...]      # each ≤ budget; len > 1 ⇒ chunked send (M3)
    total_chars: int

class Composer:
    def __init__(self, service: ServicePreset, spec_text: str, tool_catalog: str): ...
    def bootstrap(self, task: str, project_summary: str) -> Outbound
    def results(self, turn: int, results: Sequence[ToolResult]) -> Outbound
    def user_answer(self, turn: int, text: str) -> Outbound

# engine/engine.py -------------------------------------------------------
class Phase(Enum):
    IDLE = auto(); AWAITING_REPLY = auto(); REVIEW = auto()
    SENDING_CHUNKS = auto(); AWAITING_USER = auto(); DONE = auto()

class Decision(Enum):
    APPROVE = auto(); REJECT = auto(); APPROVE_ALL_EDITS = auto()  # escalation sticks for session

@dataclass(frozen=True, slots=True)
class PendingAction:
    call: ToolCall
    kind: Literal["edit", "write", "command", "auto"]   # "auto" = no approval needed
    preview: str                  # unified diff for edit/write; command line for command

class Engine:
    """Synchronous, single-threaded. Host (TUI) calls it from exactly one worker thread."""
    def __init__(self, config: Config, registry: ToolRegistry, workspace: Workspace,
                 session: SessionStore, backups: BackupStore, composer: Composer): ...
    phase: Phase
    turn: int
    def start_task(self, task: str) -> Outbound                  # IDLE → AWAITING_REPLY
    def ingest(self, text: str) -> IngestResult                  # AWAITING_REPLY → REVIEW (or noise/error)
    def pending(self) -> tuple[PendingAction, ...]
    def decide(self, call_id: str, decision: Decision) -> None
    def execute(self) -> StepResult                              # REVIEW → AWAITING_REPLY | AWAITING_USER | DONE
    def answer_user(self, text: str) -> Outbound                 # AWAITING_USER → AWAITING_REPLY
    def next_chunk(self) -> Outbound | None                      # M3: chunk ACK advance
    def undo_last_turn(self) -> UndoReport                       # M3 (backups written from M1)
    def status(self) -> StatusSnapshot                           # phase, turn, budget use — for status bar

# IngestResult is a union: NewTurn(parsed) | ChunkAck | Noise | ProtocolError(issues)
# StepResult is a union: Send(outbound) | AskUser(question) | Done(summary)

# tools/registry.py ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    handler: Callable[[Workspace, ToolCall, Limits], ToolResult]
    approval_kind: Literal["auto", "edit", "write", "command"]
    catalog_doc: str             # the description embedded in the bootstrap prompt

class ToolRegistry:
    def get(self, name: str) -> ToolSpec | None
    def render_catalog(self) -> str          # consumed by Composer (data passed, no import)

# tools/sandbox.py -------------------------------------------------------
class Workspace:
    root: Path                               # Path(root).resolve(strict=True) at startup
    excludes: frozenset[str]
    def resolve_read(self, rel: str) -> Path      # raises SandboxViolation
    def resolve_write(self, rel: str) -> Path     # parent-resolving variant (file may not exist)
    def is_excluded(self, p: Path) -> bool

# engine/approval.py -----------------------------------------------------
class ApprovalPolicy:
    auto_accept_edits: bool = False          # flipped by Decision.APPROVE_ALL_EDITS
    yolo: bool = False                       # auto-approve EVERYTHING; toggled live by /yolo
    def verdict(self, spec: ToolSpec, call: ToolCall) -> Literal["auto", "needs_approval"]
    def command_auto_allowed(self, command: str) -> bool   # glob allowlist + deny-token check

# store/backups.py -------------------------------------------------------
class BackupStore:
    def begin_turn(self, turn: int) -> None
    def snapshot_before_write(self, rel: str, abs_path: Path) -> None  # copy-on-first-touch
    def finish_turn(self) -> None                                      # writes manifest.json
    def undo_turn(self, turn: int) -> UndoReport
    def prune(self, keep_sessions: int) -> None

# clip/base.py -----------------------------------------------------------
class ClipboardProvider(Protocol):
    name: str
    def read_text(self) -> str | None        # None = non-text / empty / transient failure
    def write_text(self, text: str) -> None
    def healthcheck(self) -> bool

def select_provider(prefer: str = "auto") -> ClipboardProvider
# order: Windows → copykitten (+winseq); Linux → copykitten, else pyperclip; none → ManualOnly sentinel

# clip/watcher.py ---------------------------------------------------------
def watch(provider: ClipboardProvider, interval_ms: int,
          should_stop: Callable[[], bool],
          on_capture: Callable[[str], None],
          self_writes: SelfWriteSet) -> None
# Thread-agnostic loop per research digest: Windows seqnum fast path; elsewhere len+blake2b compare;
# only invokes on_capture when looks_like_protocol(text) is True; skips hashes in self_writes.
```

The TUI wraps the engine: clipboard watcher thread → `post_message(ClipboardCaptured(text))` → a `@work(thread=True)` handler calls `engine.ingest(...)` / `engine.execute(...)` and `push_screen_wait(ApproveScreen(...))` for each `PendingAction`. The engine never blocks the event loop because the TUI never calls it from the event loop.

---

## 2. Config system

**Format:** TOML. **Files and precedence** (later wins, per-key shallow merge per table; lists *replace*, never concatenate — concatenation makes allowlists impossible to tighten per-project):

1. Built-in defaults (in `config.py`, the table below)
2. Global: `platformdirs.user_config_dir("agentclip")/config.toml` (`~/.config/agentclip/config.toml` on Linux, `%APPDATA%\agentclip\config.toml` on Windows)
3. Project: `<root>/.agentclip.toml`
4. CLI flags (`--service`, `--project`)

**Allowlist matching: glob (`fnmatch.fnmatchcase`) against the full command string.** Rejected regex: users will write allowlists by hand; glob is auditable at a glance and can't catastrophically backtrack. Safety backstop: if a command contains any *deny token* (`;`, `&&`, `||`, `|`, backtick, `$(`, `>`, `<`, newline), it **always requires approval** even when a glob matches — this prevents `pytest tests; rm -rf ~` from riding the `pytest *` pattern.

**Full default config** (this exact content ships as the built-in default and as a commented `config.toml` written on first run):

```toml
# AgentClip configuration. Project file .agentclip.toml overrides these per key.

[general]
service = "chatgpt"            # key into [services.*]
chars_per_token = 3            # code-like payloads tokenize ~3 chars/token (budget math)

[clipboard]
provider = "auto"              # auto | copykitten | pyperclip | manual
poll_interval_ms = 300         # 200–500 sensible range

[approval]
auto_accept_edits = false      # session escalation always starts off
yolo = false                   # auto-approve EVERYTHING (edits + commands); /yolo toggles live
command_allowlist = [
  "pytest*", "python -m pytest*", "python -m unittest*",
  "ruff check*", "ruff format --check*", "mypy*",
  "npm test*", "npm run test*", "npx tsc --noEmit*",
  "cargo check*", "cargo test*", "go test*", "go vet*",
  "git status", "git diff*", "git log*",   # read-only git; AgentClip itself never uses git
  "ls*", "dir*",
]
command_deny_tokens = [";", "&&", "||", "|", "`", "$(", ">", "<"]

[limits]
max_file_read_chars = 20000    # read_file hard cap per call (LLM asks for ranges beyond this)
max_command_output_chars = 8000
max_result_chars = 6000        # per-tool-result cap inside the outbound payload
max_grep_matches = 200
command_timeout_s = 120

[paths]
# .agentclip and .agentclip.toml are ALWAYS excluded (hard-coded) so the LLM
# cannot read/tamper with backups, transcripts, or its own approval rules.
exclude = [
  ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
  "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
  "dist", "build", ".idea", ".vscode",
]

[backup]
keep_sessions = 5              # prune older session dirs (incl. their backups) at startup

# ── Service presets ─────────────────────────────────────────────────────
# max_paste_chars: outbound budget per single paste (chunking splits above it).
# wrap_blocks_in_fence: bootstrap instructs LLM to emit all CLIP blocks inside ONE
#   ``` fence → the per-code-block copy button is lossless on services whose
#   reply-copy strips markdown (Copilot, Gemini).
# attachment_note: bootstrap warns the model that user messages may arrive as an
#   attached pasted-text file it must read fully. Cheap; on everywhere.

[services.chatgpt]
label = "ChatGPT web (inline-safe)"
max_paste_chars = 4000          # stays under ~5k paste-to-attachment threshold
wrap_blocks_in_fence = false
attachment_note = true

[services.chatgpt-attach]
label = "ChatGPT web (attachment OK)"
max_paste_chars = 12000
wrap_blocks_in_fence = false
attachment_note = true

[services.copilot-work]
label = "M365 Copilot Chat — work tab (licensed)"
max_paste_chars = 96000         # 128k counter with headroom; counter hard-stops (truncation risk)
wrap_blocks_in_fence = true     # Copilot reply-copy plain flavor strips markdown
attachment_note = true

[services.copilot-web]
label = "M365 Copilot Chat — web tab"
max_paste_chars = 12000         # 16k reported, 25% headroom
wrap_blocks_in_fence = true
attachment_note = true

[services.copilot-free]
label = "Copilot (unlicensed / consumer)"
max_paste_chars = 6000          # ~8k floor with headroom
wrap_blocks_in_fence = true
attachment_note = true

[services.claude]
label = "Claude.ai"
max_paste_chars = 24000         # attachment conversion is safe (full-context pasted text)
wrap_blocks_in_fence = false
attachment_note = true

[services.gemini]
label = "Gemini"
max_paste_chars = 24000         # ~30k hard limit with headroom
wrap_blocks_in_fence = true     # Gemini reply-copy is lossy like Copilot
attachment_note = true

[services.perplexity]
label = "Perplexity"
max_paste_chars = 6000          # ~8k-token paste.txt conversion; also appends citation tail
wrap_blocks_in_fence = false
attachment_note = true

[services.deepseek]
label = "DeepSeek"
max_paste_chars = 12000
wrap_blocks_in_fence = false
attachment_note = true

[services.grok]
label = "Grok"
max_paste_chars = 100000
wrap_blocks_in_fence = false
attachment_note = true

[services.unknown]
label = "Unknown service (conservative)"
max_paste_chars = 6000
wrap_blocks_in_fence = true
attachment_note = true

[services.paranoid]
label = "Unknown service (paranoid)"
max_paste_chars = 4000
wrap_blocks_in_fence = true
attachment_note = true
```

Config is loaded into frozen dataclasses with manual validation (type + range checks, unknown-key warnings). Unknown `[services.*]` tables are accepted as user-defined presets.

---

## 3. Working-directory sandboxing (the check, exactly)

`Workspace.root = Path(project_root).resolve(strict=True)` once at startup. Every tool path argument is a string `rel` from the LLM, checked as:

1. **Reject early on shape:** `PurePosixPath(rel).is_absolute()` or `PureWindowsPath(rel).is_absolute()` or `rel` contains a drive designator (`re.match(r"^[A-Za-z]:", rel)`) or starts with `\\`/`//` (UNC) or contains a NUL byte → `SandboxViolation`. Checking *both* flavors closes the "POSIX-absolute path on Windows" and "Windows path on Linux" holes.
2. **Resolve with symlink following:**
   - Reads (`resolve_read`): `candidate = (root / rel).resolve()` — non-strict resolve in 3.11+ resolves all existing symlink components; a symlink pointing outside root produces a path that fails step 3.
   - Writes (`resolve_write`): the file may not exist, and non-strict resolve does not chase symlinks in a non-existent tail. So: find the deepest **existing** ancestor of `root / rel`, `resolve(strict=True)` it, verify *it* passes step 3, then append the remaining (non-existent) components after rejecting any `..` or symlink-named component among them. Refuse to write *through* a symlinked directory whose target escapes root.
3. **Containment:** `candidate == root or candidate.is_relative_to(root)` else `SandboxViolation`. (Case-insensitive comparison hazards on Windows are avoided because both sides come from the same `resolve()` normalization.)
4. **Exclusion:** if any path component ∈ `paths.exclude` ∪ `{".agentclip", ".agentclip.toml"}` → refused for read *and* write (`.git` may hold credentials in remote URLs; `.agentclip` holds the backups the LLM must not touch). Traversal tools (`list_dir`, `glob`, `grep`) silently skip excluded directories instead of erroring.

`SandboxViolation` is reported back to the LLM as a tool error result (`error: path outside project root`), not hidden — the model can self-correct. `run_command` is *not* path-sandboxed (it runs with `cwd=root`; the allowlist + approval gate is its control) — document this honestly rather than pretending subprocesses are containable.

---

## 4. Session persistence — `.agentclip/` layout

```
<project root>/
├── .agentclip.toml                  # optional per-project config (committed by user if desired)
└── .agentclip/                      # data dir; AgentClip writes "*" to .agentclip/.gitignore on creation
    └── sessions/
        ├── LATEST                   # text file containing the most recent session id (no symlinks: Windows)
        └── 20260612-143015-7f3a/    # session id = local timestamp + 4 hex rand
            ├── meta.json            # {schema: 1, started, service, agentclip_version, root}
            ├── transcript.jsonl     # append-only audit log (below)
            ├── outbound/
            │   └── turn-0003.txt    # exact last-composed payload per turn (chunks concatenated
            │                        #   with "\n␞\n" separators) — manual re-copy / postmortem
            └── backups/
                └── turn-0003/
                    ├── manifest.json
                    └── files/src/utils.py        # mirrored relative paths, pre-change bytes
```

**Transcript JSONL** — one event per line, `{"t": <type>, "ts": <iso8601>, ...}` with types: `task`, `outbound` (kind, turn, total_chars, chunk count), `inbound` (raw text), `parsed` (call ids/tools, issues), `decision` (call_id, verdict, source: user|allowlist|auto_edits), `result` (call_id, ok, truncated, chars), `undo`, `error`. Raw inbound is stored verbatim — it is the audit trail for "what did the LLM actually say".

**Resume after restart: NOT supported in MVP.** Decision: a half-finished conversation lives in the chat UI's context, which AgentClip cannot reconstruct reliably; faking resume invites state divergence. On restart you start a new session/task. What *is* supported after restart: backups remain on disk for manual recovery, and M3's `undo` can target the latest session's turns by reading manifests from disk (no in-memory state needed). Transcript is audit-only.

---

## 5. Undo/backup store

**Copy-on-first-touch per turn.** `Engine.execute()` calls `backups.begin_turn(n)` before running any approved call. Before the first mutation of each file in that turn, the tool layer calls `snapshot_before_write(rel, abs_path)`:

- File exists → copy bytes to `backups/turn-NNNN/files/<rel>` (with `shutil.copy2` to keep mtime/mode), manifest entry `{path, action: "modified", backup: "files/<rel>", sha256_before}`.
- File does not exist → no copy, manifest entry `{path, action: "created", backup: null}`.
- Second+ write to the same file in the same turn → no-op (first snapshot is the turn baseline).

`finish_turn()` writes `manifest.json` atomically (write `manifest.json.tmp`, `os.replace`).

**Restore semantics for `undo_turn(n)`** (turns must be undone newest-first; engine enforces):

- `modified` → copy backup bytes over current file. If current content differs from what AgentClip wrote (sha mismatch vs. post-write hash recorded in the result event), warn in `UndoReport` but proceed — the user asked.
- `created` → delete the file; remove now-empty parent dirs that the turn created.
- `deleted` → restore from backup. (No delete tool exists in MVP, but `edit_file`/`write_file` never delete either, so this branch is dormant — kept in the manifest schema so adding a delete tool later doesn't migrate data.)
- Honest limitation, surfaced in the TUI: **undo covers file-tool changes only**; `run_command` side effects (installed packages, files written by scripts) are outside the manifest.

**Retention:** at startup, `prune(keep_sessions=5)` deletes the oldest session dirs beyond the configured count. No per-turn pruning within a session (a session's backups are small — only touched files — and the whole point).

---

## 6. Dependencies (exact, minimal)

**Runtime:**

| Package | Pin | Why |
|---|---|---|
| `textual` | `>=8.2,<9` | the TUI; brings `rich`, `platformdirs`, `markdown-it-py` transitively |
| `copykitten` | `>=2.0,<3` | primary clipboard (Rust/arboard, abi3 wheel, no subprocess at 300 ms polling) |
| `pyperclip` | `>=1.11,<2` | fallback provider (pure Python, Wayland-without-XWayland path) |
| `platformdirs` | `>=4` | config dir resolution — declared explicitly even though textual carries it (don't depend on transitive deps) |

**Dev (PEP 735 `[dependency-groups]`, uv-native):** `pytest`, `pytest-asyncio`, `pytest-textual-snapshot`, `textual-dev`, `ruff`, `mypy`.

**Deliberately NOT added:**

- **pydantic** — config is ~40 keys; frozen dataclasses + 50 lines of validation beat a 10 MB PyInstaller payload and a Rust core dep.
- **click/typer** — one entry point, three flags; `argparse` suffices.
- **`textual[syntax]`** — diff coloring uses pygments via `rich.Syntax`; tree-sitter native libs complicate onefile builds.
- **tomli-w** — deferred to M3 (settings screen needs TOML *writing*; until then config is hand-edited). Tiny and pure-Python when it arrives.
- **GitPython / dulwich** — undo is explicitly non-git (user decision).
- **watchdog, requests, ripgrep bindings** — no FS watching (we poll the clipboard, not files), no network, `grep` is a pure-Python `re` scan with excludes and match caps.

---

## 7. pyproject.toml shape

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "agentclip"
version = "0.1.0"
description = "Use any web-chat LLM as a coding agent over the clipboard"
requires-python = ">=3.11"
license = "MIT"
dependencies = [
  "textual>=8.2,<9",
  "copykitten>=2.0,<3",
  "pyperclip>=1.11,<2",
  "platformdirs>=4",
]

[project.scripts]
agentclip = "agentclip.cli:main"

[dependency-groups]                  # PEP 735; `uv sync` picks this up
dev = [
  "pytest>=8", "pytest-asyncio>=0.25", "pytest-textual-snapshot",
  "textual-dev", "ruff", "mypy",
]

[tool.hatch.build.targets.wheel]
packages = ["src/agentclip"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**PyInstaller milestone (M4) implications baked in now:**

- Textual CSS lives in the `CSS` class variable of `AgentClipApp`, **not** a `.tcss` file → zero `--add-data` and no `CSS_PATH` resolution against an extraction dir.
- Ship `packaging/hook-agentclip.py` (M4) with `hiddenimports = collect_submodules("textual.widgets")` — Textual lazy-loads widgets via module `__getattr__` and PyInstaller misses them.
- The protocol-spec templates in `protocol/spec.py` are Python string constants, not data files, for the same reason.
- copykitten's abi3 `.pyd`/`.so` is auto-collected; pyperclip is pure Python. No other binary deps exist by construction.

---

## 8. Test strategy

```
tests/
├── conftest.py                      # tmp workspace fixture, default Config fixture, ScriptedLLM helper
├── test_layering.py                 # imports each module, asserts dependency direction (no tui/clip
│                                    #   imports inside engine/protocol/tools/store) — enforces §0
├── protocol/
│   ├── golden/                      # pairs: NNN-name.input.txt + NNN-name.expected.json
│   │   ├── 001-two-calls.input.txt
│   │   ├── 010-fenced-blocks.input.txt        # whole reply inside ``` fence
│   │   ├── 011-crlf.input.txt
│   │   ├── 012-bom.input.txt
│   │   ├── 013-perplexity-citation-tail.input.txt
│   │   ├── 014-copilot-said-prefix.input.txt
│   │   ├── 020-missing-end.input.txt          # expected: issue missing_end, zero executable calls
│   │   ├── 021-unterminated-heredoc.input.txt
│   │   ├── 022-rewrapped-header-line.input.txt # editor soft-wrap split the sentinel line
│   │   └── 023-truncated-mid-block.input.txt   # silent-truncation simulation
│   ├── test_parser_golden.py        # parametrized over golden/; compares ParsedTurn as JSON
│   └── test_composer.py             # budget math (3 chars/token), chunk split boundaries, fence wrap
├── engine/
│   ├── test_state_machine.py        # legal/illegal phase transitions, decide/execute ordering
│   ├── test_roundtrip.py            # full headless loop: start_task → ScriptedLLM reply → approve →
│   │                                #   execute → results payload → ... → task_done; asserts files on disk
│   └── test_approval.py             # glob allowlist, deny-token override, APPROVE_ALL_EDITS stickiness
├── tools/
│   ├── test_sandbox.py              # ../escape, absolute POSIX + C:\ + UNC, drive letter, NUL,
│   │                                #   symlink-out-of-root (skipif Windows without symlink privilege),
│   │                                #   write-through-symlink-dir, excluded dirs (.git, .agentclip)
│   ├── test_fs_tools.py             # edit_file uniqueness/no-match errors, read ranges, truncation caps
│   └── test_shell.py                # timeout kill, output cap, cwd=root
├── store/
│   └── test_backups.py              # copy-on-first-touch idempotence, undo created/modified, prune,
│                                    #   undo-from-disk-after-new-BackupStore (restart scenario)
├── clip/
│   └── test_watcher.py              # FakeClipboard: change detection, self-write suppression,
│                                    #   non-protocol noise ignored, None (non-text) tolerated
└── tui/
    └── test_smoke.py                # ONE Pilot test: app boots with FakeClipboard injected, post
                                     #   ClipboardCaptured(reply), approval modal appears, press "y",
                                     #   transcript shows result, status bar shows AWAITING_REPLY
```

Principles: the **engine round-trip never touches a real clipboard** — `ScriptedLLM` maps outbound payloads to canned reply strings, proving the loop headless (prime directive a). The watcher is tested against `FakeClipboard` (a `ClipboardProvider` with a settable buffer and change counter). Exactly one Textual Pilot test in MVP — TUI behavior beyond boot/approve/render is the TUI designer's snapshot-test territory. Golden files are byte-exact (committed with `* -text` in `.gitattributes` so CRLF fixtures survive checkout).

---

## 9. MVP cut — milestones

**M1 — Headless engine + protocol (the product's brain, zero UI):**
`config.py` (defaults + TOML merge), `protocol/` (parser with all tolerances, composer **single-payload only** — over-budget raises `BudgetExceeded`, no chunking), `tools/` (all ten tools + sandbox), `engine/` (full state machine, approval policy, APPROVE_ALL_EDITS flag), `store/session.py` (transcript JSONL), `store/backups.py` **write path only** (snapshots + manifests; no undo command yet — backups are safety-critical from the first file edit). Exit criterion: `test_roundtrip.py` green — a scripted multi-turn task that edits files and "runs" a command end-to-end.

**M2 — TUI happy path:**
`clip/` providers + watcher thread, `tui/` main screen (transcript via `VerticalScroll`+`anchor()`, diff panel, status bar, task input, approve modal with y/n/a), manual "read clipboard now" hotkey fallback, copy-outbound-to-clipboard. One service preset chosen via config file. Exit criterion: a human completes a real task against ChatGPT web.

**M3 — Chunking, undo, settings:**
Chunk protocol + ACK ingestion (`next_chunk`), `undo_turn` + retention pruning + TUI undo command, settings screen (`tomli-w` dependency added here), per-service fence-wrap behavior wired into composer, structured "resync/re-emit" payload on parse failure.

**M4 — Polish + distribution:**
PyInstaller onefile (hook file, smoke test of frozen binary on Win11 + Ubuntu), paste-budget **calibrate** command (numbered test payload, model reports last visible marker), Wayland fallback UX (provider healthcheck messaging), preset refinements, docs.

**Explicitly cut from MVP (M1+M2), revisit only on demand:** session resume; chunking (M3); undo command (M3 — write path is M1); settings screen; delete-file tool; any git integration; clipboard HTML-flavor parsing; OSC-52; plugin/extension system for tools; concurrent sessions; macOS testing (should work via copykitten, untested); telemetry of any kind (never).

---

## 10. Worked example (removes ambiguity about engine I/O)

```python
engine = Engine(cfg, default_registry(), Workspace(root, cfg.paths.exclude),
                SessionStore(root), BackupStore(root, session_id), composer)

out = engine.start_task("Fix the date parsing bug in src/utils.py")
clipboard.write_text(out.chunks[0])                  # TUI's job; engine never sees the clipboard

result = engine.ingest(reply_text)                   # text from watcher / manual hotkey
assert isinstance(result, NewTurn)
for action in engine.pending():                      # e.g. edit_file → kind="edit", preview=unified diff
    engine.decide(action.call.id, Decision.APPROVE)  # TUI gets this from ApproveScreen
step = engine.execute()                              # snapshots file, applies edit, runs pytest
assert isinstance(step, Send)
clipboard.write_text(step.outbound.chunks[0])        # results payload back to the LLM
```

---

## 11. Contracts for other designers

**Protocol designer must honor:**
1. Grammar must be **line-anchored plain text**: no backticks/asterisks/headings as syntax (survives markdown-stripping copy on Copilot/Gemini). Sentinel `===CLIP:` is the watcher's cheap pre-filter — keep it as the literal prefix of every block-opening line.
2. Parser tolerances I committed to in §1/§8 are requirements on the *grammar*, not just the parser: blocks must remain unambiguous when wrapped in ``` fences, prefixed with "Copilot said:", suffixed with Perplexity citations, CRLF'd, or BOM'd.
3. Define the **heredoc escaping rule** for content containing the closing marker (e.g. `>>>` on its own line inside `replace`) — my `ToolCall.blocks` assumes exact byte fidelity is recoverable.
4. Chunk headers must carry `part i/n` **and a length field** (e.g. `len=11990`) with NACK-on-mismatch semantics — silent truncation exists in the wild.
5. Bootstrap text must include the **attachment note** ("the user's message may arrive as a file named pasted-text/paste.txt; read it entirely") and, when the preset sets `wrap_blocks_in_fence`, the instruction to emit all blocks inside one fenced code block.
6. Call `id`s unique per turn; results payloads reference them. Parse issues ⇒ the whole turn is non-executable (no partial execution of a half-parsed reply).

**TUI designer must honor:**
1. The engine API in §1 is the **complete** surface — no reaching into `tools/`, `store/`, or `protocol/` from `tui/`. Status bar reads `engine.status()` only.
2. The engine is synchronous and **not thread-safe**: call it from exactly one `@work(thread=True)` worker; never from the event loop (`execute()` runs subprocesses for minutes).
3. The watcher is a plain function (`clip/watcher.py`) — you own wrapping it in a thread worker and bridging via `post_message`; inject `FakeClipboard` in tests.
4. Every outbound write must go through `SelfWriteSet.note(text)` before `provider.write_text` (self-detection suppression), and reads/writes should share one clipboard thread.
5. Approval UX maps to exactly three `Decision` values (approve / reject / approve-all-edits-this-session); diff text arrives precomputed in `PendingAction.preview` — do not re-diff in the TUI.
6. ASCII/BMP-only status chrome (`●`, `▶`, `✓`), no multi-codepoint emoji (Windows Terminal/conhost width bugs).