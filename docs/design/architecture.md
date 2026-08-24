# AgentClip Architecture Design

Decisive design for codebase structure, config, persistence, sandboxing, and testing. Companion documents: protocol grammar (protocol designer), widget/UX detail (**GUI designer** — `gui.md` and the per-surface briefs in `docs/design/ui-briefs/`; the Textual shell's own `tui.md` is frozen, `ui-monitor.md` §2.12). Cross-cutting requirements for them are in §11.

---

## 0. Architectural prime directive (restated as enforced rule)

The **engine is sans-IO with respect to clipboard and UI**: it is a synchronous state machine that consumes *strings* (ingested text, user decisions, user answers) and returns *values* (outbound payload strings, pending actions, results). It performs filesystem and subprocess side effects only through the tool layer, never touches the clipboard, and never imports Textual.

### The three named layers

Above the engine the codebase is three packages, and they are the vocabulary the rest of these documents use:

- **Shell** (`shell/`: `app/`, `tui/`, `gui/`) — the user-facing surfaces. `tui` is the Textual terminal app, `gui` the pywebview desktop window, and `app` is the UI-agnostic session controller both of them drive through the `ChatView` port. Two shells, one behaviour: neither may own anything the other cannot have.
- **Driver** (`driver/`: `automation/`, `monitor/`, `screen/`, `clip/`) — everything AgentClip does **to the desktop chat app it operates**. `automation` is the loop that watches, clicks, pastes and harvests the reply; `monitor` is the machine it watches *through* — the poll loop, the trackers, the mouse, the keyboard and the clipboard behind one `UIMonitor` object, destined for a process of its own (docs/design/ui-monitor.md §3); `screen` and `clip` are the OS seams `monitor` is made of (capture, focus click, detection; clipboard providers and the watcher).
- **Executor** (`executor/`: `tools/`, `hosts/`, `mcp/`, `permissions.py`) — permission-gated execution **on behalf of the agent**, reaching the machine only through the **Host seam**. `tools` is the catalogue the model may call, `permissions` decides which calls are allowed, `hosts` is the one route to files and commands (local or over SSH), and `mcp` bolts external servers onto the same catalogue.

Below them sit `engine/` (the state machine and its `store/`), `protocol/` (the shared wire vocabulary) and `config.py` (the leaf everyone reads). A Shell may reach down into the Driver, the Executor and the engine; nothing below may reach back up.

Dependency direction (imports may only point downward; enforced by a lint test, see §8):

```
SHELL  ──►  DRIVER, ENGINE           the UIs: shell/tui (Textual) | shell/gui (pywebview)
 ├──►  shell/app  ──►  engine        (UI-agnostic session orchestration)
 └──►  driver/automation             (UI-agnostic screen automation)
 │
 ▼
DRIVER ──►  driver/automation  ──►  driver/monitor  (the UI monitor: poll loop, trackers,
 │            (recipes, policy)          │              mouse, keyboard, clipboard — ui-monitor.md §3)
 │                                       ▼
 │                                      driver/clip (watcher/providers), driver/screen (capture, click, detect)
 │
 ▼
ENGINE ──►  EXECUTOR: executor/tools  ──►  sandbox (Workspace)
 │      ──►  engine/store (session, backups)
 ▼
protocol (parser, composer)   ──►  (nothing but stdlib)
 ▲
config (leaf)  ◄── imported by everyone;  ──►  executor/hosts (reads the project's .agentclip.toml)
EXECUTOR leaves: executor/hosts        ◄── config, tools, engine/store, engine: the OS seam
                 executor/permissions  ◄── config, engine/approval: one rule model, two readers
                 executor/mcp          ◄── config (the servers), tools (the client runtime)
```

`driver/clip` and `driver/screen` (the OS side-effect layers: clipboard, screen overlay + focus click) are imported **only** by `shell/tui`, `shell/gui`, `cli`, `driver/automation` and `driver/monitor`. `protocol`, `config`, `executor/hosts`, `executor/permissions` and `executor/mcp` are leaves. `executor/tools` never imports `engine`. Anything violating this is a bug.

**`driver/automation` is the second UI-agnostic layer**, added when the desktop GUI shell was decided (docs/design/gui.md §1) and extracted from `MainScreen` over phase 0. It is `shell/app`'s sibling and its opposite number: `app` drives the *engine* through the `ChatView` port, `automation` drives the *screen* through the `AutomationView` port, and a UI shell is what plugs into both. Allowed imports: `agentclip.driver.monitor`, `agentclip.driver.screen`, `agentclip.driver.clip`, `agentclip.config`, itself (plus `agentclip.engine.states`, for `describe.py` alone — tests/test_layering.py). Banned: `textual`, `agentclip.shell.app`, `agentclip.shell.tui` — a shell depends on the Driver, never the other way round.

That inverts what used to be true of the acting primitives, and the note in `shell/app/view.py` that said so was reversed in the same wave: mouse clicks, synthetic keystrokes, focus snap-back, screen capture, the detector poll threads, the clipboard watcher AND the clipboard write are **not** called from a view layer any more. They are called from `AutomationController`, below every shell, so that two frontends cannot drift into two different automations. `os_armed` — the standing "do not touch the world" switch — lives there for the same reason; `ChatView.set_os_armed` only forwards the intent. What a shell still owns is scheduling (Textual's `run_worker`) and painting.

Four seams carry the split, and they are deliberately not one object (docs/design/gui.md §1):

| seam | direction | thread contract |
|---|---|---|
| `driver/automation/view.py:AutomationView` | controller → shell | paint-only, callable from the poller/watcher threads, must be non-blocking and thread-safe (the TUI posts a message; a GUI enqueues to its JS bridge) |
| `driver/automation/host.py:AutomationHost` | controller → shell | the handful of answers only a shell has (live preset/profile, `find_all`, the verified copy click, prose ingest, detector rebuild, the OSC-52 park-off-clipboard). Event-loop thread only — which is exactly why it is not folded into `AutomationView` |
| `driver/monitor/protocol.py:UIMonitor` | controller → the machine that can see the chat | the whole machine behind one object: `configure`/`suspend`/`resume`, the `Tick` stream (`latest`, `observe`, `subscribe`), the clipboard (`watch_clipboard`, `on_clip`, read/write) and every OS action, each a coroutine. In-process today (`driver/monitor/local.py:LocalUIMonitor`, which owns the poll thread); a TCP client tomorrow (docs/design/ui-monitor.md §3, §6.5). Awaited from the event-loop thread; the `subscribe`/`on_clip` hooks fire on the monitor's own thread and must not block |
| `driver/monitor/ops.py:ScreenOps` | monitor → OS | `agentclip.driver.screen` behind one substitutable object, so the OS primitives stay off the paint port and a shell's test suite can patch them at its own module scope. It is a tier *inside* the monitor now (reached as `monitor.ops`), not a seam a shell wires up — it moved out of `driver/automation` in ui-monitor.md phase 1 |

The `ScreenOps` seam **deepens** into `UIMonitor` (docs/design/ui-monitor.md §3): the OS adapter is one tier inside the object that also owns the poll loop, the trackers, the ghost stamp and the clipboard watcher, and the controller reaches the machine only through it. `AutomationView` and `AutomationHost` are untouched by that split.

`executor/hosts` is the **OS seam** the whole Executor is built on: filesystem and command execution reach the machine only through a `Host` (`spawn`/`read_bytes`/`write_bytes`/`delete`/`mkdir`/`rmdir`/`stat`/`lstat`/`listdir`/`realpath`, plus the `case_sensitive` fact), carried on `ToolContext`. `LocalHost` is this PC; `SshHost` is a machine over SSH (docs/design/remote-ssh.md), chosen once at launch in `cli.py` and shared by the workspace jail, the tool context, the backup store and the engine — one session is one machine. Rule for new tools: write against Host primitives and it works everywhere. Process execution is `spawn` + an `ExecHandle` the caller polls (`wait`/`peek`/`kill`/`drain`), because cancellation, timeouts and the live output tail are policy above the seam, not OS access.

---

## 1. Module layout

```
src/agentclip/
├── __init__.py            # __version__ only
├── __main__.py            # python -m agentclip → cli.main()
├── cli.py                 # argparse (--project, --service, --ssh, --remote-root, --version); connects
│                          #   the host, builds Config, wires Engine + the chosen shell
├── config.py              # frozen dataclasses + TOML load/merge/validate (stdlib tomllib)
│
├── protocol/
│   ├── types.py           # wire-level dataclasses (ToolCall, ParsedTurn, ParseIssue, Outbound)
│   ├── parser.py          # tolerant sentinel-block parser: str → ParsedTurn
│   ├── composer.py        # bootstrap / results / chunk payload rendering + budget splitting
│   └── spec.py            # protocol-spec text templates shown to the LLM (incl. per-service variants)
│
├── engine/                # the session state machine and the session's own persistence
│   ├── engine.py          # Engine: the session state machine (the only orchestrator)
│   ├── states.py          # Phase enum + legal-transition table
│   ├── approval.py        # ApprovalPolicy: the per-mode permission rules + the yolo/unattended toggles
│   ├── results.py         # ToolResult + middle-truncation to configured size caps
│   ├── link/              # the ENGINE half of the Shell↔Engine link (remote-executor.md §2.2);
│   │   │                  #   never imports shell/driver — it is what runs on the target
│   │   └── factory.py     # EngineRequest + make_engine_builder: one EngineRequest → one Engine
│   │                      #   (registry sizing, workspace jail, stores, composer). cli's
│   │                      #   make_engine_factory is this behind a LocalLink
│   └── store/
│       ├── session.py     # SessionStore: .agentclip/ layout, transcript JSONL append, outbound dumps
│       └── backups.py     # BackupStore: per-turn copy-on-first-touch snapshots, undo, retention
│
├── executor/              # THE EXECUTOR: permission-gated execution, reaching the machine
│   │                      #   only through the Host seam. Never imports engine or any shell
│   ├── permissions.py     # OpenCode's allow/ask/deny rule model: wildcard matcher, evaluate(), always_pattern()
│   ├── hosts/             # the OS seam: every file byte and every command goes through a Host
│   │   ├── base.py        # Host + ExecHandle Protocols (wait/peek/kill/drain), FileStat/DirEntry/ExecResult
│   │   ├── local.py       # LocalHost: subprocess/os/pathlib, kill-tree per platform, reader thread → peek()
│   │   ├── ssh.py         # SshHost: one Paramiko connection, exec channels + SFTP, lazy reconnect
│   │   ├── connect.py     # the remote CONNECT SEQUENCE both shells drive: resolve → dial+auth →
│   │   │                  #   probe → root → home/env → remote config. Prompts and progress are
│   │   │                  #   injected (getpass+stderr for the CLI, modals+a checklist for the GUI);
│   │   │                  #   the only module here that may import config (steps 1 and 6 are loads)
│   │   └── fake.py        # FakeHost: in-memory filesystem + scripted commands, for tests
│   ├── mcp/               # external MCP servers behind the same catalogue (docs/design/mcp.md)
│   └── tools/
│       ├── registry.py    # ToolRegistry: name → ToolSpec; render_catalog() for the bootstrap prompt
│       ├── sandbox.py     # Workspace: project-root jail, host-resolved paths, exclusion rules
│       ├── fs_tools.py    # read_file, write_file, edit_file, replace_lines, list_dir, glob, grep (pure-Python re scan)
│       ├── shell.py       # run_command: host.spawn + poll slices, timeout/cancel kill, combined output,
│       │                  # per-slice peek() diff → ctx.on_output (the shells' live tail)
│       └── meta.py        # ask_user, task_done (no side effects; engine interprets)
│
├── driver/                # THE DRIVER: what AgentClip does TO the desktop chat app it
│   │                      #   operates. Imports config and NOTHING above (gui.md §1)
│   ├── automation/        # UI-agnostic screen automation: shell/app's sibling, shared by
│   │   │                  #   both UI shells. Imports screen/clip/config only
│   │   ├── controller.py  # AutomationController: the armed switch, the slot pointers, the
│   │   │                  #   clipboard watcher + detector poller threads, the probe consumer,
│   │   │                  #   the finish decision, the OS-acting sequences, the delivery path
│   │   ├── view.py        # AutomationView Protocol: the PAINT-only port (paint_loop_state,
│   │   │                  #   paint_detection/stale/elements/armed, paste flash, notify).
│   │   │                  #   Callable from the poller/watcher threads: never blocking
│   │   ├── host.py        # AutomationHost Protocol: what only a shell can answer (live
│   │   │                  #   preset/profile, find_all, verified copy click, prose ingest,
│   │   │                  #   detector rebuild, park_off_clipboard). Event-loop thread only
│   │   ├── ops.py         # ElementClick: what one find-then-click can come to (ScreenOps
│   │   │                  #   itself is the monitor's now, ui-monitor.md §6.1)
│   │   ├── describe.py    # describe(Phase, LoopState) -> the one state label both shells show
│   │   ├── finish.py      # the finish vocabulary: SendGate, the verdict folds, the phrasing
│   │   ├── flow.py        # the auto-copy flow's geometry: lowest_match, above_chatbox, snap
│   │   ├── delivery.py    # the paste banner's four wordings and the delivery beats
│   │   ├── loop_state.py  # LoopState: where the browser-automation loop is (the STATE rail)
│   │   └── harness_log.py # HarnessEntry + the /log renderer: the decision log's vocabulary
│   ├── monitor/           # THE UI MONITOR (docs/design/ui-monitor.md): everything that watches
│   │   │                  #   and touches the machine showing the chat. Imports screen/clip/
│   │   │                  #   config only — never automation
│   │   ├── protocol.py    # UIMonitor Protocol + Tick (booleans, locations, counts — never
│   │   │                  #   pixels) + MonitorSpec (what watching one chat window needs)
│   │   ├── local.py       # LocalUIMonitor: the poll thread, the detector + its trackers, the
│   │   │                  #   generation stamp, the clipboard watcher, POLL_SECONDS
│   │   ├── fake.py        # FakeUIMonitor: ticks pushed by hand, for the brain's suites
│   │   ├── ops.py         # ScreenOps: agentclip.driver.screen behind one substitutable object
│   │   └── beats.py       # the cadence every OS-acting sequence paces itself by
│   ├── clip/
│   │   ├── base.py        # ClipboardProvider Protocol + select_provider()
│   │   ├── copykitten_provider.py
│   │   ├── pyperclip_provider.py
│   │   ├── winseq.py      # ctypes GetClipboardSequenceNumber shim (≤15 lines)
│   │   ├── watcher.py     # poll loop (plain function, thread-agnostic), self-write suppression
│   │   └── fake.py        # FakeClipboard + ScriptedClipboard for tests
│   └── screen/            # OS screen layer (like clip: imported ONLY by the shells, cli and
│       │                  # automation; stdlib-only AT MODULE LEVEL — cv2/numpy are lazy,
│       │                  # function-body imports, see matchers.py)
│       ├── region.py      # ScreenRegion dataclass + the "left top width height" wire format
│       ├── overlay.py     # draw-a-box tkinter overlay; runs in a CHILD process (--pick-region)
│       ├── picker.py      # spawns the child (works frozen and from source), parses its stdout
│       ├── capture.py     # GDI BitBlt/GetDIBits screen-region grab (ctypes) -> RegionImage,
│       │                  #   plus crop(): the matched rectangle back out of a frame (both shells)
│       ├── focus.py       # Windows SetCursorPos+SendInput click/scroll into a region; window focus snap-back (ctypes)
│       ├── hover.py       # cursor-stop geometry for the hover scan (icons that only render under the pointer)
│       ├── busy.py        # diff_fraction + the BusyState/BusyProbe vocabulary every detector answers in
│       ├── stale.py       # StaleTracker: frame-to-frame stability of a region -> StaleState
│       ├── presence.py    # PresenceTracker: is this appearance on screen? de-bounced -> BusyProbe
│       ├── png.py         # stdlib zlib/struct PNG encode/decode, for persisting templates
│       ├── profile.py     # TemplateKind + ServiceProfile: what a SERVICE looks like (not where)
│       ├── profile_store.py # one folder of PNGs + a manifest per service; load never raises
│       ├── slot.py        # AgentSlot (MASTER/SUBAGENT) + SlotCalibration: the drawn window per slot (= per tab)
│       ├── matchers.py    # the pluggable HALF of a search: anchors | opencv candidate generation.
│       │                  # cv2/numpy imported inside the function; falls back to anchors when absent
│       └── template.py    # 2D search for an appearance: dual-ruler anchors + the SHARED verification
│                          # every backend is judged by (tui.md §3.4g)
│
└── shell/                 # THE SHELL: the two UIs and the controller they share
    ├── app/               # UI-agnostic orchestration: drives the engine, never imports tui/clip/screen
    │   ├── controller.py  # SessionController: flows, gate/ask futures, delegation (nested sessions)
    │   ├── link.py        # Link Protocol + LocalLink: the Shell↔Engine seam
    │   │                  #   (docs/design/remote-executor.md §2.2)
    │   ├── remote_link.py # the same seam over the wire: RemoteLinkClient (one connection, over any
    │   │                  #   pair of text streams) + RemoteLink (a Link whose engine is elsewhere)
    │   │                  #   (remote-executor.md §2.11)
    │   ├── commands.py    # the chat slash-command registry: dispatch, /help and the popup read one tuple
    │   ├── view.py        # ChatView Protocol (the one UI seam) + SessionView snapshot + RunCall rows
    │   └── types.py       # SessionSpec, SessionRef, SessionStats (EngineRequest is engine-side:
    │                      #   engine/link/factory.py, so a remote engine can decode one)
    ├── gui/               # the pywebview desktop shell: a native window over hand-written
    │                      #   HTML/CSS/JS in assets/, Python in the same process (gui.md §2)
    └── tui/
        ├── app.py         # AgentClipApp(App); CSS embedded in class var (PyInstaller, §7)
        ├── messages.py    # ClipboardCaptured, CallStarted/CallFinished/CallOutput (the engine worker
        │                  # thread's bridge to the run panel), McpStatusChanged, and the PAINT family:
        │                  # one message per AutomationView method (the Driver's threads asking
        │                  # for a repaint), plus AutoCopyRequested. No probe messages: the poller
        │                  # feeds AutomationController.consume_* in its own call stack
        ├── graphics.py    # can this terminal draw sixels? probed ONCE from cli.main, before Textual (tui.md §1.7)
        ├── pixels.py      # the half-block renderer: pure functions over RegionImage, the no-sixel
        │                  # fallback; re-exports driver.screen.capture.crop, which used to live here
        ├── screens/
        │   ├── main.py    # the ChatView + AutomationView + AutomationHost adapter: tabs,
        │   │              #   transcripts, sidebar routing, and the scheduling the Driver
        │   │              #   hands back (run_worker); the automation itself is below it
        │   ├── service_editor.py # F2: the whole per-service PROFILE editor (tui.md §1.4)
        │   ├── settings.py # F4: appearance/theme picker
        │   ├── help.py    # F1 cheatsheet; its command section renders from shell/app/commands.py
        │   ├── summary.py # end-of-session stats + what next (tui.md §1.5)
        │   ├── confirm.py # ConfirmScreen(ModalScreen[bool]): quit mid-turn, undo, forget captures
        │   └── text_entry.py # TextEntryScreen(ModalScreen[str | None]): one-line prompts
        └── widgets/
            ├── transcript.py  # VerticalScroll of per-message widgets, .anchor() pinning
            ├── window_tabs.py # the two-row tab bar whose tabs ARE browser windows (tui.md §1.6)
            ├── composer.py    # the docked chat box: Enter sends, and it drives the popup below
            ├── command_popup.py # the slash-command list above the composer (tui.md §3.3a)
            ├── action_panel.py  # the approval gate: title, diff/command body, buttons, reject input
            ├── run_panel.py    # the RUN PANEL: one row per call while a turn executes + the running
            │                    # command's live output, ctrl+o (tui.md §8a)
            ├── running_bar.py   # the "Working... ctrl+x cancels" spinner line, the run panel's header
            ├── sidebar.py     # the settings column: service, chat window, DETECTION (tui.md §1.3)
            ├── elements.py    # the ELEMENTS column: the crops the detectors matched (tui.md §1.7)
            ├── log_pane.py    # the full-width live harness decision log, /log + F8 (tui.md §3.3b)
            └── statusbar.py   # docked Horizontal: watcher state, budget, service, phase
```

### Key signatures

```python
# executor/hosts/base.py ----------------------------------------------------------
class ExecHandle(Protocol):
    """One running command, driven by run_command's polling loop above the seam."""
    def wait(self, timeout: float) -> ExecResult | None   # None = still running
    def peek(self) -> str        # merged output SO FAR: non-blocking, only ever grows
    def kill(self) -> None       # the process AND its children; never raises
    def drain(self, timeout: float) -> str                # after kill(): what it managed to emit

# peek() is what makes a long command watchable. LocalExec drains the pipe on a
# reader thread from the moment it spawns (bytes + an incremental UTF-8 /
# universal-newline decoder), so wait() only polls the process and BOTH the
# final result and the post-kill drain read out of that one buffer. SshExec
# answers with whatever its poll loop has pumped off the channel. A transport
# that cannot expose partial output may answer "" forever: nothing above breaks,
# the live tail is simply empty until the result lands.

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
    SENDING_CHUNKS = auto(); AWAITING_USER = auto()
    AWAITING_SUBAGENT = auto()   # a `delegate` call parked mid-turn; see below
    DONE = auto()

class Decision(Enum):
    APPROVE = auto(); REJECT = auto(); APPROVE_ALL_EDITS = auto()  # escalation sticks for session
    APPROVE_ALWAYS = auto()       # remember a permission rule for the rest of the session

@dataclass(frozen=True, slots=True)
class PendingAction:
    call: ToolCall
    kind: Literal["edit", "command", "auto"]            # "auto" = no approval needed
    preview: str                  # unified diff for an edit; command line for a command
    auto_reason: str | None       # why it needed no gate (transcript/audit text)
    always_pattern: str | None    # what APPROVE_ALWAYS would remember, e.g. "git commit *"

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
    def execute(self) -> StepResult                              # REVIEW → AWAITING_REPLY | AWAITING_USER | AWAITING_SUBAGENT | DONE
    def answer_user(self, text: str) -> Outbound                 # AWAITING_USER → AWAITING_REPLY
    def deliver_delegate_result(self, text: str, *, status: ResultStatus = "ok",
                                code: str | None = None) -> StepResult   # AWAITING_SUBAGENT → resume the turn
    def request_cancel(self) -> None                             # THREAD-SAFE (sets an Event); no-op when idle
    # Watching a turn execute (the TUI's run panel, tui.md §8a). Wired once by
    # whoever owns the engine, exactly like the backup hook; both fire FROM THE
    # WORKER THREAD that is inside execute(), so an implementation must not
    # block and must be thread-safe. Both are courtesies - a hook that raises is
    # dropped, hook and all, and the turn carries on.
    def set_progress_hook(self, hook: Callable[[CallProgress], None] | None) -> None
    def set_output_hook(self, hook: Callable[[int, str], None] | None) -> None   # (call_id, delta)
    def next_chunk(self) -> Outbound | None                      # M3: chunk ACK advance
    def undo_last_turn(self) -> UndoReport                       # M3 (backups written from M1)
    def status(self) -> StatusSnapshot                           # phase, turn, budget use — for status bar
    def set_yolo(self, enabled: bool) -> bool                    # live, any phase; audited
    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode  # live, any phase; audited
    role: Literal["master", "subagent"]                          # immutable; picks the bootstrap variant
    chat_name: str                                               # immutable; stamped on every outbound

@dataclass(frozen=True, slots=True)
class CallProgress:
    """One step of a turn's execution, reported AS IT HAPPENS (set_progress_hook)."""
    call_id: int
    tool: str
    phase: Literal["running", "done"]   # "running" = about to enter the handler
    status: str = ""                    # the ToolResult status once phase is "done"

# Every call resolves exactly once, including the ones that never run (denied by
# a rule or the mode, skipped after a rejection or a cancel, pre-resolved parse
# errors) - so a row on screen can never be left pending. Only calls that
# actually enter a handler emit "running" first.

# StatusSnapshot: phase, turn, service_key, budget_chars, auto_accept_edits, yolo,
# mode (PermissionMode), session_dir, last_outbound_chars.

# IngestResult is a union: NewTurn(parsed) | ChunkAck | Noise | ProtocolError(issues)
# StepResult is a union: Send(outbound) | AskUser(question) | Delegate(task, context) | Done(summary, outbound, result)
#
# Delegate is the exact mirror of AskUser: the engine parks mid-plan, hands the
# host something it cannot do itself, and resumes when the host hands a body
# back. AskUser asks a human; Delegate asks a whole second agent. Both resume
# methods write an ordinary ToolResult for the parked call, so from the model's
# side a delegation is just a tool call that took a while — and every failure of
# the run (uncalibrated chat, unverified new-chat click, user abort, crash) comes
# back through deliver_delegate_result as status="error" with a code, never as a
# dropped call. Done.result carries task_done's `result` param (a sub-agent's
# deliverable) and is empty for an ordinary master session.

# shell/app/types.py -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionSpec:
    """What the New-Session prompt returns: the task plus a service PER ROLE."""
    task: str
    service: str                        # the master window tab's service
    subagent_service: str = ""          # the sub-agent window tab's; "" → same as the master's

# engine/link/factory.py --------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EngineRequest:
    """What the controller asks the engine factory to build."""
    service: str
    role: Literal["master", "subagent"] = "master"
    allow_delegate: bool = False        # `delegate` in the catalog at all (master + calibrated only)
    chat_name: str | None = None        # None → the factory draws a fresh one
    parent_chat_name: str | None = None # recorded in the session log for the audit trail

# back to shell/app/types.py -----------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionRef:
    id: str                             # "master", "sub-1", "sub-2", ...
    role: Literal["master", "subagent"]
    title: str                          # short task label: the run's transcript divider
    chat_name: str                      # routes pastes to the right session

# A request object rather than a bare service key because role, catalog gating
# and chat naming have to travel as plain data: the assembly lives in
# `engine/link/factory.py` (it needs the tool/store/composer wiring, and a remote
# engine has to be able to build from a request with no shell in the process)
# while the decision to spawn a sub-agent is made in `shell/app`, which must not
# import the Driver or `shell/tui` to make it. `cli.make_engine_factory` is that
# builder behind a LocalLink (docs/design/remote-executor.md §2.2).
#
# SessionSpec is the same seam for the OTHER half of "which service?". AgentClip
# drives two browser windows and the user picks a service per window tab
# (tui.md 1.6), so a delegation's Engine must be built from the SUB tab's
# preset, not the master's. Rather than a port call the controller would have to
# make mid-run, both keys travel in the spec at bootstrap and the controller
# keeps `_subagent_service` for the session's life - which is exactly as long as
# they are true for, since both pickers lock while a session runs. The app layer
# therefore still learns nothing about tabs, windows or slots.

# executor/tools/registry.py ------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    handler: Callable[[Workspace, ToolCall, Limits], ToolResult]
    approval_kind: Literal["auto", "edit", "command"]
    catalog_doc: str             # the description embedded in the bootstrap prompt

class ToolRegistry:
    def get(self, name: str) -> ToolSpec | None
    def render_catalog(self) -> str          # consumed by Composer (data passed, no import)

@dataclass(slots=True)
class ToolContext:
    """Everything a handler may touch; the engine builds one per session."""
    workspace: Workspace; limits: LimitsConfig; caps: BudgetCaps
    host: Host                                          # the ONLY route to files/commands
    backup_hook: Callable[[str, Path, str], None] | None    # (rel, abs, "write"|"delete")
    cancel_event: threading.Event | None                # set from another thread; ctx.cancelled()
    on_output: Callable[[int, str], None] | None        # (call_id, NEW chars) - the live tail
    def emit_output(self, call_id: int, chunk: str) -> None  # defensive: a broken listener is dropped

# on_output is cancel_event's mirror image. That one is written by the UI thread
# and read here; this one is called FROM here, on the engine's worker thread,
# and read by the UI. It is a courtesy channel only: the model's copy of a
# command's output is still the composed ToolResult, so a None hook, a
# non-streaming host and a listener that raises all cost the model nothing.

# executor/tools/sandbox.py -------------------------------------------------------
HARD_EXCLUDED_NAMES = frozenset({".agentclip", ".agentclip.toml"})  # sealed both ways
class Workspace:
    root: Path                               # Path(root).resolve(strict=True) at startup
    excludes: frozenset[str]                 # paths.exclude | hard_excludes: traversal + writes
    hard_excludes: frozenset[str]            # the sealed names: reads are checked against ONLY these
    extra_read_roots: tuple[Path, ...]       # skill folders: absolute READS only, never writes
    def resolve_read(self, rel: str) -> Path      # raises SandboxViolation; hard names only
    def resolve_scan(self, rel: str) -> Path      # resolve_read minus the carve-out: traversal's base
    def resolve_write(self, rel: str) -> Path     # parent-resolving variant (file may not exist)
    def is_excluded(self, p: Path) -> bool        # traversal predicate: the full merged set

# engine/approval.py -----------------------------------------------------
class ApprovalPolicy:
    auto_accept_edits: bool = False          # shorthand for one remembered rule: edit["*"]="allow"
    yolo: bool = False                       # auto-approve every ASK; toggled live by /yolo
    unattended: bool = False                 # auto-DENY every ask; toggled live by /unattended
    mode: PermissionMode = "build"           # build | plan: which RULESET is in force; /mode, shift+tab
    session_rules: list[PermissionRule]      # "always allow" answers; evaluated last
    rules: tuple[PermissionRule, ...]        # the active mode's ruleset (ModeRules.for_mode)
    def verdict(self, spec: ToolSpec, call: ToolCall) -> Verdict
    def rule_for(self, spec: ToolSpec, call: ToolCall) -> PermissionRule
    def always_rule(self, spec: ToolSpec, call: ToolCall) -> PermissionRule
    def remember(self, rule: PermissionRule) -> None

Verdict = Literal["auto", "needs_approval", "deny", "deny_plan", "deny_unattended"]
DENY_VERDICTS = frozenset({"deny", "deny_plan", "deny_unattended"})   # "was this refused?"
# PermissionMode / PERMISSION_MODES / normalize_mode are re-exported here: they are
# DEFINED in executor/permissions.py (config.py reads them and cannot import this
# module), and shell/app/ + shell/tui/ take them from here rather than reaching
# into the rule model.

# executor/permissions.py (stdlib leaf, shared by config.py and approval.py)
@dataclass(frozen=True, slots=True)
class PermissionRule:
    permission: str; pattern: str; action: Literal["allow", "ask", "deny"]

PermissionMode = Literal["build", "plan"]                    # OpenCode's two primary agents
PERMISSION_MODES = ("build", "plan")                         # also the shift+tab cycle order
def normalize_mode(value: object) -> PermissionMode | None   # None = not a mode

DEFAULT_CONFIG: dict[str, object]                            # THE defaults; what /config writes
MODE_PERMISSIONS: dict[PermissionMode, dict[str, object]]    # the built-in per-mode overlay
@dataclass(frozen=True, slots=True)
class ModeRules:                                             # one effective ruleset per mode
    build: tuple[PermissionRule, ...]; plan: tuple[PermissionRule, ...]
    def for_mode(self, mode: PermissionMode) -> tuple[PermissionRule, ...]

def wildcard_match(text: str, pattern: str) -> bool          # OpenCode's Wildcard.match
def evaluate(permission, pattern, *rulesets) -> PermissionRule   # LAST match wins; no match = "ask"
def rules_from_config(obj) -> tuple[rules, warnings]         # OpenCode's fromConfig
def build_mode_rules(shared, per_mode) -> ModeRules          # the five-layer merge (see §2)
def permission_target(tool, params, approval_kind) -> tuple[str, str]   # tool -> (key, resource)
def always_pattern(key: str, resource: str) -> str           # "git commit *" / "*"

# engine/store/backups.py -------------------------------------------------------
class BackupStore:
    def begin_turn(self, turn: int) -> None
    def snapshot_before_write(self, rel: str, abs_path: Path) -> None  # copy-on-first-touch
    def finish_turn(self) -> None                                      # writes manifest.json
    def undo_turn(self, turn: int) -> UndoReport
    def prune(self, keep_sessions: int) -> None

# driver/clip/base.py -----------------------------------------------------------
class ClipboardProvider(Protocol):
    name: str
    def read_text(self) -> str | None        # None = non-text / empty / transient failure
    def write_text(self, text: str) -> None
    def healthcheck(self) -> bool

def select_provider(prefer: str = "auto") -> ClipboardProvider
# order: Windows → copykitten (+winseq); Linux → copykitten, else pyperclip; none → ManualOnly sentinel

# driver/clip/watcher.py ---------------------------------------------------------
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

**Format:** TOML. **Files and precedence** (later wins, per-key shallow merge per table; lists *replace*, never concatenate — concatenation makes a list impossible to tighten per-project):

1. Built-in defaults (in `config.py`, the table below)
2. Global: `platformdirs.user_config_dir("agentclip")/config.toml` (`~/.config/agentclip/config.toml` on Linux, `%APPDATA%\agentclip\config.toml` on Windows)
3. Project: `<root>/.agentclip.toml`
4. CLI flags (`--service`, `--project`)

**In a remote session** the project layer is read from the target through the Host seam (and so are the permission ruleset and the MCP blocks), while the global `config.toml` is the operator's. **No table is exempt from the merge** — `[approval]` (mode, yolo, unattended, deny tokens) was pinned to this PC by the `/config` wave and no longer is: policy belongs to the machine the engine runs on (docs/design/remote-executor.md §2.5, superseding remote-ssh.md's "the host owns the gate"). What stays local is the gate's *UI*, not its rules.

**Commands are gated by the ruleset below, not by a list in `config.toml`.** The `[approval] command_allowlist` key is gone: one mechanism decides every tool, and a `bash` rule says the same thing in the same file as everything else. What survives it is the *deny-token* backstop: if a command contains any of `;`, `&&`, `||`, `|`, backtick, `$(`, `>`, `<`, newline, it **always requires approval** even when a rule allows it — this prevents `pytest tests; rm -rf ~` from riding an `allow` on `pytest *`.

### Permission rules: AgentClip's `permissions.json`

Rules live in AgentClip's own JSON file, in two layers, later winning: `platformdirs.user_config_dir("agentclip")/permissions.json` (beside `config.toml`; `~/.config/agentclip/permissions.json` on Linux) then `<project>/.agentclip/permissions.json` — `[permission] permissions_config` overrides the global path, `[permission] enabled = false` switches the whole thing off. The **shape** is `opencode.json`'s, deliberately (the rule model below is OpenCode's, ported), so a ruleset written for that tool reads correctly here when copied across — but nothing falls back to another tool's file: no `opencode.json` is opened, anywhere. `/config [global|local]` creates the file (with `DEFAULT_CONFIG` spelled out, plus an empty `mcp` block — the rules that were already in force, so creating it changes nothing) and puts its path on the clipboard; `/config [global|local] reset` puts that same document *back* over a file edited into a state where nothing runs, keeping only its top-level `"mcp"` block. It is read **once at launch**, so an edit — or a reset — needs a restart; not even `/new` re-reads it (`cli.main` builds one `Config` per process). Two top-level keys are read: `"permission"` and `"agent"`, the latter restricted to `agent.build` / `agent.plan` (OpenCode's `plugin` block and its other agents have no AgentClip equivalent, and guessing a mapping would grant or refuse things the user never decided). The model can't reach the file either way — it lives outside the workspace.

A rule is `(permission key, resource pattern, action)` with `action ∈ {allow, ask, deny}`. `executor/permissions.py` (a stdlib leaf, shared by `config.py` and `engine/approval.py`) ports OpenCode's semantics verbatim:

- **Wildcard matching**, not glob and not regex: `*` crosses spaces *and* slashes, `?` is exactly one character, backslashes normalize to `/` on both sides, matching is whole-string anchored and case-insensitive on Windows. A pattern ending in `" *"` makes the arguments optional, so `ls *` matches a bare `ls`.
- **Positional precedence**: the LAST matching rule wins — no specificity sorting. That is what lets a config say `"*": "ask"` and carve exceptions under it. No rule matching at all is an implicit `ask`.
- **Effective ruleset, per mode** (`build_mode_rules`, later winning): `DEFAULT_CONFIG["permission"]` → the user's shared `permission` block → the mode's built-in overlay (`MODE_PERMISSIONS`) → `DEFAULT_CONFIG["agent"][mode]["permission"]` → the user's `agent.<mode>.permission` block. Two things fall out of that order: a config with no `permissions.json` anywhere evaluates *identically* to one whose file `/config` just created (the same blocks arrive twice in a row, and last-match-wins cannot notice), and the overlay outranks a block written for every mode while still being outrankable by one written *for* that mode — so loosening plan mode is possible, but only where it is unmistakably meant. OpenCode merges its agent overlay earlier, which lets a shared `{"edit": "allow"}` switch plan's denials off by accident.

Tools map onto permission keys: `read_file→read`, `write_file`/`edit_file`/`replace_lines`/`delete_file→edit`, `list_dir→list`, `glob→glob`, `grep→grep`, `run_command→bash`, `skill→skill`, `delegate→task`, the MCP invoker→`mcp` (`mcp_schema` keeps its own name). The resource is the file path (workspace-relative, forward slashes), the pattern/name parameter, the composite MCP tool id, or the full command line. `ask_user`/`task_done` are AgentClip's own control flow and are never gated.

**The ruleset IS the permission system** — there is no second gate behind it and no "legacy mode" beside it. An install with no `permissions.json` runs on `DEFAULT_CONFIG` in full, so read-only tools, edits and commands are all judged by the same table:

| | what decides |
|---|---|
| read-only tools | the rules (the shipped defaults allow `list`/`glob`/`grep`/`skill` and `read` except dotenv) |
| edits | the rules (the shipped default is `ask`); `auto_accept_edits` is one remembered `edit["*"]="allow"` |
| commands | the rules + the deny-token backstop (shipped: `ask`, with read-only git and `ls`/`dir` allowed) |
| `/yolo` | auto-approves every *ask*; a `deny` still denies, and the deny-token backstop still gates |
| `/unattended` | auto-*denies* every ask (`deny_unattended`); allow rules still run |
| third gate button | **Always: `<pattern>`** (every gated call), `Decision.APPROVE_ALWAYS` |

A `deny` verdict never opens a gate: the call is pre-resolved as a `denied` result carrying OpenCode's `DeniedError` text (protocol.md §4) and the turn *continues* — only an interactive rejection aborts the rest of it. The decision is audited with source `rule`, as are rule-allowed calls (`allowed by rule bash["git status*"]`).

### Permission modes: two rulesets, not a dial

`ApprovalPolicy.mode` picks **which ruleset is in force**, and nothing else: `PermissionMode = Literal["build", "plan"]` — OpenCode's two primary agents under their own names — lives in `executor/permissions.py` (the leaf `config.py` reads and `approval.py` applies) and is re-exported from `engine/approval.py`, which is where `shell/app`, `shell/tui` and `shell/gui` import it from. A mode is no longer a dial *above* the rules; it *is* a ruleset, and `ModeRules` holds both so switching is one field.

| mode | its ruleset |
|---|---|
| `build` (default) | the user's rules exactly as written — the overlay adds nothing |
| `plan` | the same, plus a built-in overlay denying `edit`, `bash`, `mcp` and `task`: everything that could change something. Reads, listings, globs, greps and skills behave exactly as in `build`, and a user can buy back specific commands under `agent.plan` (the shipped file does, for `git status`/`git diff`) |

Beside the mode sit **two live toggles**, both booleans on `ApprovalPolicy` and both statements about the *user* rather than about the rules: `yolo` answers every would-be gate "yes", `unattended` answers it "no".

Verdict order: **① the rule that wins** (`evaluate`, last match) — `deny` → `deny_plan` if plan's overlay is the only reason `build` would not have denied it, else `deny` → **② `allow`** → `auto`, unless it is a `bash` resource carrying a deny token, which falls to the gate → **③ everything else (an explicit or implicit `ask`)** → `auto` if YOLO, else the gate → **④ the gate** → `deny_unattended` if unattended, else `needs_approval`.

- **YOLO vs `unattended`** is decided by ③ running before ④: YOLO wins, because it is the user's explicit "approve everything for me" and outranks "I stepped away". The one thing YOLO does not answer is the **deny-token backstop**, so a chained command riding an allow rule reaches ④ — and is `deny_unattended` rather than a gate nobody is at.
- The three refusals are **distinct verdicts**, not one: `deny_plan` and `deny_unattended` get their own model-facing bodies (protocol.md §4) and their own audit source (`plan` / `unattended`), while a rule `deny` keeps OpenCode's wording byte-for-byte. A model can only pick a different route if it is told which door was shut.
- **Not retroactive**, exactly like `set_yolo`: a gate already pending stays pending, and the new mode governs every verdict computed after it.
- Set live by `/mode [build|plan]` (bare `/mode` reports) and by `SessionController.cycle_permission_mode()` (`build → plan → build`, what `shift+tab` calls); the first value comes from `[approval] mode`, which falls back to `build` with a config warning if it is not one of the two. Audited as a `permission_mode` session event. `unattended` is set by `/unattended [on|off]` (bare toggles) through `Engine.set_unattended`, its first value comes from `[approval] unattended`, and both shells paint a badge for as long as it is on.
- **The mode is the user's, not a session's**, and this is where it parts company with YOLO — the two look alike in `ApprovalPolicy` and are scoped oppositely on purpose. YOLO answers a question about **one conversation** ("approve everything here"), so it is per-engine, dies with the session and reverts to its configured default on a reset. The mode is a statement about **the user** ("I am only exploring"), so `SessionController` owns it and every engine in the app run obeys it — and `unattended` ("I am not at my desk") is scoped with the mode, not with YOLO, for exactly that reason:
  - it works with **no session at all** — `set_permission_mode`/`cycle_permission_mode` at the start prompt skip only the engine call; the mirror, the transcript note and the status repaint all happen;
  - it **survives `/new`** and every other `_reset_session`, because "I am only exploring today" is still true after a new chat and a mode that silently reverted would hand the next session's first edit to a user who thought they had turned changes off;
  - it **reaches sub-agents**. `delegate` is never gated (it is AgentClip's own control flow), so a mode stopping at the master would let a model in plan mode make every change it liked by delegating it, and would park an absent `unattended` user on a sub-agent's gate — the denial bodies of both promise neither can happen.
  Neither can become hidden state: the mode segment shows both modes and never hides, and `unattended` has a badge of its own that is up whenever it is on.
- **Every engine is armed before it can compute a verdict.** `_session_flow` (master) and `_sub_run` (sub-agent) both call `Engine.set_permission_mode(self._mode)` on the engine they just built, **before `start_task`** — so the first verdict of the first turn already obeys it, and the audit line is at the top of that session's log. Chosen over threading the mode through `EngineRequest`/`ApprovalConfig` because it keeps one carrier for "the mode is now X" instead of two, and the audit event falls out of it.
- **Across a delegation swap.** Only one engine is reachable at a time (`_apply_mode` writes to whatever `self._engine` currently is), so a cycle made while a sub-agent runs lands on the *sub-agent's* policy — right, since that is the conversation running and the one the user is watching. The mode is therefore the one field `_SessionContext` does **not** restore: `_adopt_ctx` leaves the mirror alone and `_restore_ctx` reconciles towards it, never towards the snapshot. `_SessionContext.engine_mode` records only what the master's *policy* was left at, so `_rearm_master_mode` can give the master a change it slept through — and say nothing when the mode never moved, since re-sending it would arm a spurious note.
- The model is told at the **next results payload** via the notes channel (protocol.md §4), never in the bootstrap — §2's budget headroom has no room for prose about a mode that may never be used, and each denial body explains itself anyway. `set_permission_mode` arms that note **only when the engine is past `IDLE`**: before `start_task` there is no conversation to interrupt, so a pre-session choice is simply the mode the session started in, and announcing it as a change in the first results payload would be describing something that never happened.

**"Always allow"** appends `Rule(key, always_pattern, "allow")` to an in-memory session list evaluated *last*, so it outranks the file (OpenCode's `approved` array works the same way) and is forgotten on restart. `always_pattern` keeps the first N words of a command per a small arity table (`git commit -m "wip"` → `git commit *`, `npm run build` → `npm run build *`) and is `*` for every other key — remembering an edit means remembering all edits, which is exactly what `APPROVE_ALL_EDITS` already meant. Answering it re-evaluates the other pending calls in the turn and auto-approves the ones the new rule covers.

**Deviation from OpenCode: the deny-token backstop.** OpenCode splits a shell script with tree-sitter and judges every command node separately, so `git status && rm -rf /` is also judged as `rm -rf /`. AgentClip has no shell parser, so instead a command containing any configured deny token (`;`, `&&`, `||`, `|`, backtick, `$(`, `>`, `<`, newline) can never *silently* auto-run: `allow` is downgraded to a gate, and YOLO does not answer that one for you. A `deny` still wins outright. This is why `command_deny_tokens` is the one command key left in `[approval]`.

**Full default config** (this exact content ships as the built-in default and as a commented `config.toml` written on first run):

```toml
# AgentClip configuration. Project file .agentclip.toml overrides these per key.

[general]
service = "chatgpt"            # key into [services.*]: the MASTER window tab starts here
subagent_service = ""          # the SUB-AGENT window tab's (tui.md §1.6); "" = same as the master's
chars_per_token = 3            # code-like payloads tokenize ~3 chars/token (budget math)

[clipboard]
provider = "auto"              # auto | copykitten | pyperclip | manual
poll_interval_ms = 300         # 200–500 sensible range

[approval]
auto_accept_edits = false      # session escalation always starts off
yolo = false                   # auto-approve EVERYTHING (edits + commands); /yolo toggles live
mode = "build"                 # build (your rules decide) | plan (nothing that changes anything); /mode, shift+tab
unattended = false             # auto-DENY whatever would ask you: nobody is at the keyboard; /unattended toggles live
command_deny_tokens = [";", "&&", "||", "|", "`", "$(", ">", "<"]

[permission]
enabled = true                 # read permissions.json (see above); false = the shipped defaults only
permissions_config = ""        # "" = <user_config_dir>/agentclip/permissions.json

[limits]
max_file_read_chars = 20000    # read_file hard cap per call (LLM asks for ranges beyond this)
max_command_output_chars = 0   # 0 = auto: how much raw output a handler RETAINS (512k, the chunk cache's own cap)
max_result_chars = 0           # 0 = auto: per-tool-result cap in the payload = max_paste_chars // 2
max_grep_matches = 200
command_timeout_s = 120

[paths]
# Skipped by list_dir/glob/grep and refused for writes; a file inside one can
# still be READ when named explicitly. .agentclip and .agentclip.toml are ALWAYS
# excluded (hard-coded) and sealed both ways, so the LLM cannot read or tamper
# with backups, transcripts, or its own approval rules.
exclude = [
  ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
  "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
  "dist", "build", ".idea", ".vscode",
]

[backup]
keep_sessions = 5              # prune older session dirs (incl. their backups) at startup

# ── Service presets ─────────────────────────────────────────────────────
# max_paste_chars: outbound budget per single paste (chunking splits above it).
# total_context_chars: the service's whole conversation window, ~chars (roughly
#   tokens × 4 chars/token, kept conservative) - bounds the service editor's
#   validation (max_paste_chars must fit inside it) and informs preset choice;
#   the engine itself only ever enforces max_paste_chars per turn.
# wrap_blocks_in_fence: bootstrap instructs LLM to emit all CLIP blocks inside ONE
#   ``` fence → the per-code-block copy button is lossless on services whose
#   reply-copy strips markdown (Copilot, Gemini).
# attachment_note: bootstrap warns the model that user messages may arrive as an
#   attached pasted-text file it must read fully. Cheap; on everywhere.
#
# The three detection knobs below are per service because they describe that
# chat's UI, not AgentClip. All three are edited in the service editor (F2,
# tui.md §1.4) and written back here only when they differ from the built-in, so
# a file whose user never touched them stays byte-for-byte what it was.
#
# stable_seconds: how long the drawn chat region must sit UNCHANGED before the
#   stale finish detector calls the response done (0.5–60, default 2.0). Per
#   service because streaming cadence differs - a chat that pauses mid-answer
#   needs a longer stillness window, or auto-copy fires into the gap.
# finish_signals: which finish detectors this service's poller may run, from
#   ["busy", "idle", "stale"] (default ["stale"]). A checklist, not a mode: they
#   reinforce each other, and the trigger fires only when EVERY one that is
#   running agrees. "busy"/"idle" additionally need that appearance captured -
#   ticked without one, the detector is skipped and the sidebar says so. An
#   EMPTY list is legal and means "never detect a finish here"; the user drives
#   the copy button themselves. Unknown entries are dropped with a warning.
# hover_scan: may the auto-copy flow glide the REAL cursor up the chat region
#   hunting a copy icon that only renders under the pointer (default false)?
#   Opt-in because it is a visible, slow takeover of the user's mouse, and only
#   some chats (Claude's) need it at all.

[services.chatgpt]
label = "ChatGPT web (inline-safe)"
max_paste_chars = 4000          # stays under ~5k paste-to-attachment threshold
total_context_chars = 500000    # ~128k-token context
wrap_blocks_in_fence = false
attachment_note = true
stable_seconds = 2.0            # stale detector: this much stillness = finished
finish_signals = ["stale"]      # busy | idle | stale; [] = no finish detection
hover_scan = false              # walk the real cursor to reveal the copy icon?

[services.chatgpt-attach]
label = "ChatGPT web (attachment OK)"
max_paste_chars = 12000
total_context_chars = 500000
wrap_blocks_in_fence = false
attachment_note = true

[services.copilot-work]
label = "M365 Copilot Chat — work tab (licensed)"
max_paste_chars = 96000         # 128k counter with headroom; counter hard-stops (truncation risk)
total_context_chars = 400000
wrap_blocks_in_fence = true     # Copilot reply-copy plain flavor strips markdown
attachment_note = true

[services.copilot-web]
label = "M365 Copilot Chat — web tab"
max_paste_chars = 12000         # 16k reported, 25% headroom
total_context_chars = 150000
wrap_blocks_in_fence = true
attachment_note = true

[services.copilot-free]
label = "Copilot (unlicensed / consumer)"
max_paste_chars = 6000          # ~8k floor with headroom
total_context_chars = 128000
wrap_blocks_in_fence = true
attachment_note = true

[services.claude]
label = "Claude.ai"
max_paste_chars = 24000         # attachment conversion is safe (full-context pasted text)
total_context_chars = 700000    # ~200k-token context
wrap_blocks_in_fence = false
attachment_note = true

[services.gemini]
label = "Gemini"
max_paste_chars = 24000         # ~30k hard limit with headroom
total_context_chars = 800000    # large (~1M-token) context, kept conservative
wrap_blocks_in_fence = true     # Gemini reply-copy is lossy like Copilot
attachment_note = true

[services.perplexity]
label = "Perplexity"
max_paste_chars = 6000          # ~8k-token paste.txt conversion; also appends citation tail
total_context_chars = 100000
wrap_blocks_in_fence = false
attachment_note = true

[services.deepseek]
label = "DeepSeek"
max_paste_chars = 12000
total_context_chars = 250000
wrap_blocks_in_fence = false
attachment_note = true

[services.grok]
label = "Grok"
max_paste_chars = 100000
total_context_chars = 400000
wrap_blocks_in_fence = false
attachment_note = true

[services.unknown]
label = "Unknown service (conservative)"
max_paste_chars = 6000
total_context_chars = 100000
wrap_blocks_in_fence = true
attachment_note = true

[services.paranoid]
label = "Unknown service (paranoid)"
max_paste_chars = 4000
total_context_chars = 50000
wrap_blocks_in_fence = true
attachment_note = true
```

Config is loaded into frozen dataclasses with manual validation (type + range checks, unknown-key warnings). Unknown `[services.*]` tables are accepted as user-defined presets.

**Writing services back** (the service editor, M3): `config.save_services(services: dict[str, ServicePreset], path: Path | None = None) -> None` persists the *complete* desired services table into the global `config.toml` (default path: `default_global_config_path()`). It reads the existing file with `tomllib` first, then replaces only the `[services.*]` tables in memory - a preset equal to its built-in default (`config.default_services()`) is omitted so an untouched or reset-to-default built-in never gets written, keeping the file minimal and future built-in tweaks still apply to it. Every other top-level table (`[general]`, `[approval]`, …) is preserved verbatim (comments are not - `tomllib` doesn't retain them). The write is atomic: a temp file in the same directory, then `os.replace`. `BUILTIN_SERVICE_KEYS` (a module-level `frozenset` of the twelve shipped keys) is the source of truth the editor uses to decide what can be deleted (only non-built-in keys) versus only edited/reset.

---

## 3. Working-directory sandboxing (the check, exactly)

`Workspace.root = Path(project_root).resolve(strict=True)` once at startup. Every tool path argument is a string `rel` from the LLM, checked as:

1. **Reject early on shape:** `PurePosixPath(rel).is_absolute()` or `PureWindowsPath(rel).is_absolute()` or `rel` contains a drive designator (`re.match(r"^[A-Za-z]:", rel)`) or starts with `\\`/`//` (UNC) or contains a NUL byte → `SandboxViolation`. Checking *both* flavors closes the "POSIX-absolute path on Windows" and "Windows path on Linux" holes.
2. **Resolve with symlink following:**
   - Reads (`resolve_read`): `candidate = (root / rel).resolve()` — non-strict resolve in 3.11+ resolves all existing symlink components; a symlink pointing outside root produces a path that fails step 3.
   - Writes (`resolve_write`): the file may not exist, and non-strict resolve does not chase symlinks in a non-existent tail. So: find the deepest **existing** ancestor of `root / rel`, `resolve(strict=True)` it, verify *it* passes step 3, then append the remaining (non-existent) components after rejecting any `..` or symlink-named component among them. Refuse to write *through* a symlinked directory whose target escapes root.
3. **Containment:** `candidate == root or candidate.is_relative_to(root)` else `SandboxViolation`. (Case-insensitive comparison hazards on Windows are avoided because both sides come from the same `resolve()` normalization.)
4. **Exclusion — two policies, not one**, because the list answers two different questions:
   - **Writes** (and deletes): refused if any path component ∈ `paths.exclude` ∪ `HARD_EXCLUDED_NAMES`. Nothing the model does may edit a dependency tree, a VCS directory or a build output.
   - **Reads**: refused only if a component ∈ `HARD_EXCLUDED_NAMES` = `{".agentclip", ".agentclip.toml"}` — the backups, transcripts and approval rules the LLM must never see or rewrite. An *explicitly named* path inside a configured excluded directory (`.vscode/settings.json`, `node_modules/left-pad/index.js`) **is readable**: `paths.exclude` exists for budget hygiene, not secrecy, and refusing it stranded users whose own project files lived there.
   - **Traversal** (`list_dir`, `glob`, `grep`) silently skips the *full merged* set instead of erroring, so an excluded tree never floods a listing or a sweep — it has to be asked for by name.

   `HARD_EXCLUDED_NAMES` is defined in `executor/tools/sandbox.py`, next to the `Workspace` that enforces it, and folded into `excludes` at construction — a caller passing a bare `paths.exclude` still gets a jail that seals `.agentclip`. `config.Config.excluded_names()` therefore returns exactly what the user configured.

**The one carve-out: `extra_read_roots`.** The session's discovered skill folders (`docs/design/skills.md`) are handed to the `Workspace` at construction, and an **absolute** path resolving inside one is readable. Only absolute, so a relative path never stops meaning "under the project root"; containment is asked of the *resolved* path exactly as step 3 does, so a `..` or a symlink inside a skill folder is not a tunnel; the `HARD_EXCLUDED_NAMES` floor applies there too; writes/edits/deletes are refused with *"skill folders are read-only"* (`resolve_write` consults the roots only to pick which refusal the model is told, and can never reach the read resolver); and traversal does not extend into them — `list_dir`/`glob`/`grep` resolve their base through `resolve_scan`, which knows no carve-out, because the skill tool's own `files:` listing is the discovery surface. For permissions, such a path reaches the matcher **as written**: it has no workspace-relative form, and `*` crosses slashes, so `read: {"*.env": "ask"}` still asks.

`SandboxViolation` is reported back to the LLM as a tool error result (`error: path outside project root`), not hidden — the model can self-correct. `run_command` is *not* path-sandboxed (it runs with `cwd=root`; the `bash` rules + the approval gate are its control) — document this honestly rather than pretending subprocesses are containable.

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

**Transcript JSONL** — one event per line, `{"t": <type>, "ts": <iso8601>, ...}` with types: `task`, `outbound` (kind, turn, total_chars, chunk count), `inbound` (raw text), `parsed` (call ids/tools, issues), `decision` (call_id, verdict, source: user|rule|yolo|plan|unattended), `permission_mode` (mode), `yolo`/`unattended` (enabled), `result` (call_id, ok, truncated, chars), `undo`, `error`. Raw inbound is stored verbatim — it is the audit trail for "what did the LLM actually say".

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
| `tomli-w` | `>=1.0` | TOML *writing* for the service editor's `config.save_services` (M3, landed with it — see below); tiny, pure-Python, no longer deferred |
| `pillow` | `>=11,<12` | scaling and encoding the ELEMENTS column's crops for sixel (tui.md §1.7); a compiled dep, accepted after half-block close-ups were rejected on quality — PyInstaller has a built-in hook for it |
| `textual-image` | `>=0.13,<1` | the sixel renderer and the Textual widget that can inject sixel data into a composited screen. Used **explicitly** (`textual_image.widget.sixel.Image`), never through its auto-detecting alias — see tui.md §1.7 for why that distinction is load-bearing |

**Optional extras (`[project.optional-dependencies]`):**

| Extra | Packages | Status |
|---|---|---|
| `cv` | `opencv-python-headless>=4.10`, `numpy>=2` | the OpenCV matcher backend (tui.md §3.4g), opt in per service in the editor's MATCHING block. **Optional from source; BUNDLED in the shipped exe** |

It earns its place because the alternative is worse than its weight: an exhaustive correlation sweep is the only thing that covers the anchors' residual quantisation blind spot, and that blind spot is a real, reproduced failure on a real chat UI. It is an *extra* rather than a hard runtime dependency because a from-source install must not be taxed ~40 MB for a feature most users leave off, and the built-in anchor search needs nothing at all. Three properties make it safe to be absent: `cv2`/`numpy` are imported **inside the function** (`driver/screen/matchers.py`), so §0's stdlib-only rule for the screen layer still holds at module level and `tests/test_layering.py` passes unchanged (its AST checker explicitly permits function-body imports, the same allowance `driver/clip/copykitten_provider.py` uses); selecting the backend without it installed **falls back to anchors and says so** in the editor rather than crashing or silently searching nothing; and the tests that exercise it skip cleanly. Install with `pip install agentclip[cv]` (or `uv sync --extra cv`).

**The frozen exe bundles it, and this supersedes the earlier "keep the exe lean" reasoning.** That argument was made in the abstract and lost on contact with a user: the editor correctly reported *"OpenCV is not installed — install it with `pip install agentclip[cv]`"* inside `agentclip.exe`, where there is no environment to install into and the advice is unactionable. A knob that cannot be turned is not a lean build, it is a broken one — so the exe now carries the extra and the whole feature works out of the box. Consequences, all of them enforced rather than remembered:

- **The build environment needs the extra.** `scripts/build-exe.ps1` syncs `uv sync --group build --extra cv` (as does `scripts/build-exe.sh`, the Linux/macOS script, which builds the full app *and* `agentclip-engine` — and whose `--engine-only` mode deliberately drops `--extra cv --extra gui`, because the engine binary excludes both). Leaving the flag off does not merely skip it — `uv sync` prunes to exactly what was asked for, so it would *uninstall* opencv/numpy from the shared `.venv` and then build a lean exe without a word.
- **Reachability is named, collection is not.** `packaging/agentclip.spec` lists `cv2`/`numpy` in `hiddenimports` for the same reason it lists `copykitten` and `tkinter`: the import is lazy and `try`-guarded, so missing it produces no error, just a silent fallback. The binaries themselves need no help — PyInstaller ships `hook-numpy` and pyinstaller-hooks-contrib ships `hook-cv2`.
- **The 29 MB FFmpeg video-I/O plugin is dropped** (`a.binaries` filter in the spec). AgentClip decodes no video: the backend calls exactly `matchTemplate` and `dilate`, both core imgproc, and cv2 loads that plugin lazily only for `VideoCapture`. Verified, not assumed — with the DLL removed, cv2 still imports and both calls return identical results.
- **The build proves it, twice.** It refuses to start if `import cv2` fails in the build env, and after building it runs the frozen exe's own `--list-matchers`, which imports each backend and reports what happened. That second check is the one that matters: "the file is in the archive" is a weaker claim than "a onefile extraction can load its DLLs".
- **Cost, measured:** 24.8 MiB → 64.1 MiB (+39.3 MiB). `cv2.pyd` is the bulk of it; numpy's OpenBLAS DLL (19.5 MB) is kept because numpy imports `linalg` eagerly.

**Dev (PEP 735 `[dependency-groups]`, uv-native):** `pytest`, `pytest-asyncio`, `pytest-textual-snapshot`, `textual-dev`, `ruff`, `mypy`.

**Deliberately NOT added:**

- **pydantic** — config is ~40 keys; frozen dataclasses + 50 lines of validation beat a 10 MB PyInstaller payload and a Rust core dep.
- **click/typer** — one entry point, three flags; `argparse` suffices.
- **`textual[syntax]`** — diff coloring uses pygments via `rich.Syntax`; tree-sitter native libs complicate onefile builds.
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
  "tomli-w>=1.2.0",
  "pillow>=11,<12",
  "textual-image>=0.13,<1",
]

[project.optional-dependencies]
cv = ["opencv-python-headless>=4.10", "numpy>=2"]   # the OpenCV matcher backend (tui.md §3.4g)

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
- copykitten's abi3 `.pyd`/`.so` is auto-collected; pyperclip is pure Python. Pillow is the one further binary dep, and PyInstaller ships a hook for it.
- `textual_image` is named in `hiddenimports` (minus its bundled demo, which pulls the excluded `click`): `shell/tui/graphics.py` reaches it only through lazy, guarded imports, so static analysis sees nothing and a missed collection fails **soft** — the probe would answer "no sixel" and the frozen exe would quietly draw half-blocks.

---

## 8. Test strategy

```
tests/                               # mirrors src/agentclip: one directory per layer
├── conftest.py                      # tmp workspace fixture, default Config fixture, ScriptedLLM helper
├── test_layering.py                 # imports each module, asserts dependency direction (no driver or
│                                    #   shell imports inside engine/protocol/executor; no textual or
│                                    #   shell inside the driver) — enforces §0, generically and by name
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
│   ├── test_approval.py             # rule verdicts/deny/always, the deny-token backstop, yolo vs unattended
│   └── store/
│       └── test_backups.py          # copy-on-first-touch idempotence, undo created/modified, prune,
│                                    #   undo-from-disk-after-new-BackupStore (restart scenario)
├── executor/
│   ├── hosts/
│   │   ├── test_local_host.py       # real files + real subprocesses: stat/listdir/realpath, kill+drain
│   │   ├── test_fake_host.py        # the in-memory twin behaves like a filesystem (symlinks included)
│   │   ├── fake_paramiko.py         # an SSH server that is a dict: client/transport/channel/sftp
│   │   ├── test_ssh_host.py         # posix paths, reconnect, unknown-outcome, auth ladder, sftp
│   │   ├── test_connect.py          # the six-step connect sequence both shells drive: order, which
│   │   │                            #   step each failure lands on, close-on-failure, the two
│   │   │                            #   non-fatal steps, the picker's two target sources
│   │   └── test_ssh_real.py         # @real_ssh: AGENTCLIP_SSH_TESTS=1 + AGENTCLIP_SSH_TARGET only
│   ├── mcp/                         # the permissions.json mcp reader and the client runtime (mcp.md)
│   └── tools/
│       ├── test_excluded_reads.py   # the read/write/traversal split on paths.exclude, through
│       │                            #   the tools: .vscode reads, .vscode writes refused,
│       │                            #   .agentclip sealed, sweeps still skip
│       ├── test_sandbox.py          # ../escape, absolute POSIX + C:\ + UNC, drive letter, NUL,
│       │                            #   symlink-out-of-root (skipif Windows without symlink privilege),
│       │                            #   write-through-symlink-dir, the read/write exclusion split
│       ├── test_fs_tools.py         # edit_file uniqueness/no-match errors, read ranges, truncation caps
│       ├── test_fs_tools_fake_host.py # the same tools + jail over FakeHost: nothing bypasses the seam
│       └── test_shell.py            # timeout kill, output cap, cwd=root; scripted-host cancel/timeout
├── driver/
│   ├── clip/
│   │   └── test_watcher.py          # FakeClipboard: change detection, self-write suppression,
│   │                                #   non-protocol noise ignored, None (non-text) tolerated
│   ├── screen/                      # the OS screen seam without a screen: capture, the detectors,
│   │                                #   the matchers and the template search, on fixture pixels
│   ├── monitor/                     # LocalUIMonitor with its poll thread REAL and its ops faked:
│   │                                #   ghosts, tracker swaps, observe()'s freshness, the watcher
│   └── automation/                  # the Driver's core, driven through FakeAutomationView and
│                                    #   FakeUIMonitor: no Textual, no terminal, no mouse, and
│                                    #   since ui-monitor.md §6.1 no threads either — ticks are
│                                    #   pushed by hand through the tick_feed seam
└── shell/
    ├── app/                         # the session controller against a fake ChatView: no UI at all
    ├── gui/                         # the pywebview shell's Python side, driven without a window
    └── tui/
        └── test_smoke.py            # ONE Pilot test: app boots with FakeClipboard injected, post
                                     #   ClipboardCaptured(reply), approval modal appears, press "y",
                                     #   transcript shows result, status bar shows AWAITING_REPLY
```

Principles: the **engine round-trip never touches a real clipboard** — `ScriptedLLM` maps outbound payloads to canned reply strings, proving the loop headless (prime directive a). The watcher is tested against `FakeClipboard` (a `ClipboardProvider` with a settable buffer and change counter). Exactly one Textual Pilot test in MVP — shell behavior beyond boot/approve/render is the GUI designer's territory (the GUI's Python side is driven windowless, under `tests/shell/gui/`; the frozen TUI keeps only this smoke test). Golden files are byte-exact (committed with `* -text` in `.gitattributes` so CRLF fixtures survive checkout).

---

## 9. MVP cut — milestones

**M1 — Headless engine + protocol (the product's brain, zero UI):**
`config.py` (defaults + TOML merge), `protocol/` (parser with all tolerances, composer **single-payload only** — over-budget raises `BudgetExceeded`, no chunking), `executor/tools/` (all ten tools + sandbox), `engine/` (full state machine, approval policy, APPROVE_ALL_EDITS flag), `engine/store/session.py` (transcript JSONL), `engine/store/backups.py` **write path only** (snapshots + manifests; no undo command yet — backups are safety-critical from the first file edit). Exit criterion: `test_roundtrip.py` green — a scripted multi-turn task that edits files and "runs" a command end-to-end.

**M2 — TUI happy path:**
`driver/clip/` providers + watcher thread, `shell/tui/` main screen (transcript via `VerticalScroll`+`anchor()`, diff panel, status bar, task input, approve modal with y/n/a), manual "read clipboard now" hotkey fallback, copy-outbound-to-clipboard. One service preset chosen via config file. Exit criterion: a human completes a real task against ChatGPT web.

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

**GUI designer must honor** (and the frozen TUI still does):
1. The engine API in §1 is the **complete** surface — no reaching into `executor/tools/`, `engine/store/`, or `protocol/` from a shell (`shell/gui/`, `shell/tui/`). The status line reads `engine.status()` only.
2. The engine is synchronous and **not thread-safe**: call it from exactly one worker off the UI thread — the GUI's `asyncio.to_thread` on `GuiRunner`'s loop thread, the TUI's single `@work(thread=True)` worker — never from the event loop (`execute()` runs subprocesses for minutes). The single exception is `request_cancel()` — it only sets a `threading.Event`, and is *meant* to be called from the UI thread while that worker is inside `execute()`. Cancelling is not an abort: the interrupted call gets a `code=cancelled` error result (with its partial output), the calls after it get `cancelled` skip results, and the turn finishes through the normal `Send` path so the model is told what happened.
3. The watcher is a plain function (`driver/clip/watcher.py`) — you own wrapping it in a thread and bridging back to your UI (the GUI's bridge queue, the TUI's `post_message`); inject `FakeClipboard` in tests.
4. Every outbound write must go through `SelfWriteSet.note(text)` before `provider.write_text` (self-detection suppression), and reads/writes should share one clipboard thread.
5. Approval UX maps to exactly three `Decision` values (approve / reject / approve-all-edits-this-session); diff text arrives precomputed in `PendingAction.preview` — do not re-diff in the shell.
6. Status chrome stays ASCII/BMP in the **TUI** (`●`, `▶`, `✓`), where multi-codepoint emoji trip Windows Terminal/conhost width bugs. The GUI paints into a WebView and has no such limit.