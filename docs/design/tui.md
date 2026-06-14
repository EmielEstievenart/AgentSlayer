# AgentClip TUI Design (Textual 8.2.x)

Prime directive honored throughout: the steady-state loop costs the user **one keypress in the terminal** (`y`) — everything else (ingest, execute reads, compose results, copy to clipboard, bell) is automatic. All key choices below assume no text input is focused on the main screen during the agent loop, so bare letters are safe screen bindings; the moment a text widget gains focus (ask_user answer, reject reason, composer), Textual's focus routing suppresses them automatically.

---

## 1. Screen map

### 1.1 App and screen inventory

```
AgentClipApp(App[None])            # CSS embedded in App.CSS (avoids PyInstaller --add-data)
├── MainScreen(Screen)             # default, always installed
├── NewSessionScreen(ModalScreen[SessionSpec])
├── ConfigScreen(Screen)           # pushed full-screen (forms want room)
├── SummaryScreen(ModalScreen[SummaryAction])
├── ConfirmScreen(ModalScreen[bool])   # generic y/n confirm (undo, end-session, quit-mid-turn)
└── HelpScreen(ModalScreen[None])      # static key/flow cheatsheet
```

### 1.2 MainScreen widget tree

```
MainScreen
├── TranscriptPanel(VerticalScroll)  id=transcript        # 1fr height, full width
│   ├── Markdown                     .ev-user             # user task / follow-ups
│   ├── Markdown                     .ev-prose            # LLM prose between blocks
│   ├── Vertical                     .ev-call             # one per tool call
│   │   ├── Static                                        # "▶ edit_file src/utils.py · 1 hunk · ✓ ok"
│   │   └── Collapsible(collapsed=True)                    # long payloads only
│   │       └── Static                                     # Rich Syntax / plain text
│   ├── Static                       .ev-note / .ev-error / .ev-approval
│   └── ...
├── ActionPanel(Vertical)            id=action            # display:none when idle; max-height:60%
│   ├── Static                       id=action-title      # "APPROVE · call 2/5 · edit_file src/utils.py"
│   ├── Static                       id=queue-strip       # "✓ read_file  ▶ edit_file  • run_command  • task_done"
│   ├── VerticalScroll               id=action-body       # diff / question / chunk wizard; focused on show
│   │   └── Static                                        # Rich renderable
│   └── Horizontal                   id=action-footer
│       ├── Static                   id=action-hints      # "[y] approve  [n] reject  [a] auto-accept edits"
│       ├── Input                    id=reject-reason     # hidden until 'n'
│       └── TextArea                 id=answer            # hidden; ask_user only
├── StatusBar(Horizontal)            id=statusbar         # dock: bottom; height: 1 (sits above Footer)
│   └── Static ×6                    .seg                 # see §3.3
└── Footer()                                              # key hints, auto-dimmed via check_action
```

Layout reasoning: transcript on top at full width, ActionPanel as a bottom drawer. A side-by-side split was rejected because diffs and command output need horizontal room; a 40%-wide column truncates code lines constantly. The drawer keeps the last few transcript events visible above the diff, which is enough context to approve.

Transcript is pinned with `widget.anchor()` on every newly mounted event widget (Textual ≥4 semantics: stays bottom-anchored, releases when the user scrolls up). Transcript children are pruned beyond 500 events (oldest unmounted) to bound layout cost.

### 1.3 Session start flow (NewSessionScreen)

Shown automatically at launch (over MainScreen). Contents:

```
NewSessionScreen(ModalScreen[SessionSpec])
├── Static                 # "New session — working dir: C:\...\AgentClip"
├── Select[str]            # service preset; (label, key) pairs; default from config; uses Select.NULL guard
├── TextArea(soft_wrap=True, tab_behavior="focus")  id=task   # autofocused
└── Static                 # hint line: "ctrl+enter start · F2 settings · F3 calibrate · esc quit"
```

`ctrl+enter` → `dismiss(SessionSpec(task, preset))`. MainScreen then: composes bootstrap prompt (protocol layer), copies it, mounts the task as `.ev-user`, arms the watcher, status flashes `copied bootstrap (1/1, 5.2k chars) — paste into ChatGPT`. If the bootstrap exceeds the preset budget it immediately enters chunk-walk mode (§6).

Rejected alternative: a one-line `Input` for the task — coding tasks are multi-line (pasted tracebacks); `TextArea` with `ctrl+enter` submit (TextArea has no Submitted message, per digest) is the right call.

### 1.4 ConfigScreen

Full `Screen` with a `VerticalScroll > Grid` form; `escape` saves-and-pops (explicit Save button rejected — every field validates on change, nothing to batch):

- `Select[str]` default service preset
- `Input(validators=[Number(minimum=500)])` paste budget override (chars); blank = preset value
- `Input(validators=[Number(minimum=200, maximum=2000)])` watcher poll interval ms (default 300)
- `TextArea` command allowlist, one glob/regex pattern per line (e.g. `pytest *`, `ruff *`, `git status`)
- `Switch` terminal bell on events; `Switch` toasts (`notify`) on events
- `Switch` "wrap protocol blocks in one code fence" (forced ON for Copilot/Gemini presets — see contracts)
- `Input` backup retention (turns to keep, default 50)

### 1.5 End-of-session summary (SummaryScreen)

Pushed on demand via `e` (end session); it is **not** auto-pushed on `task_done` — the user stays in the chat and may follow up (protocol.md §8). Contents: a `Static` rendering a Rich `Table` — turns, calls per tool, files created/modified (paths), commands run, total chars pasted both ways — plus the `task_done` summary text from the LLM as `Markdown`. Bindings on the modal: `u` undo entire session (turn-by-turn restore, with ConfirmScreen), `t` new session, `escape` back to main (transcript stays for review), `ctrl+q` quit.

---

## 2. Approval flow state machine

### 2.1 Turn lifecycle

```
IDLE ──copy bootstrap──▶ ARMED ──reply parsed──▶ EXECUTING ──▶ COMPOSING ──▶ ARMED ...
                                                    │                          │
                                                    │ task_done                └─▶ CHUNKING (§6) ─▶ ARMED
                                                    ▼
                                                  DONE ──follow-up / undo──▶ ARMED   (non-terminal; summary on demand via e)
```

`EXECUTING` runs in a single async worker: `self.run_worker(self._run_turn(reply), exclusive=True, group="executor")`. Calls execute **strictly sequentially in the LLM's given order** (later calls assume earlier effects — edit-then-test).

### 2.2 Per-call classification

| Tool | Behavior |
|---|---|
| `read_file`, `list_dir`, `glob`, `grep` | auto-run, never gated |
| `run_command` | auto-run if allowlist matches (transcript shows matched rule: `auto: matched "pytest *"`); else **gated** |
| `write_file`, `edit_file` | **gated**, unless session auto-accept-edits is ON |
| `ask_user` | pauses for typed answer (not an approval; §9) |
| `task_done` | auto-runs; ends the turn and marks the session complete (the user may still follow up to reopen it) |

### 2.3 The gate

When the executor hits a gated call it builds the renderable (diff for edits, the literal command line for commands), shows the ActionPanel, focuses `#action-body` (so arrows scroll the diff immediately), bells/notifies (§8), sets `pending_approval: reactive[bool] = reactive(False, bindings=True)`, and awaits an `asyncio.Future[Approval]`:

```python
async def _gate(self, call: ToolCall, body: RenderableType) -> Approval:
    self._approval_future = asyncio.get_running_loop().create_future()
    self.show_action_panel(call, body)
    self.pending_approval = True
    try:
        return await self._approval_future       # resolved by action_approve/reject/auto
    finally:
        self.pending_approval = False
```

Approval lives in the ActionPanel, **not** a ModalScreen: the user needs the transcript visible behind the diff for context, and `check_action` gating gives the same binding safety. (`push_screen_wait` + ModalScreen rejected for the main gate; it is used for the rarer Confirm dialogs where blocking is the point.)

Queue strip (`#queue-strip`) renders every call in the turn with status glyphs (BMP-only per the Windows brief): `✓` done, `✗` failed/rejected, `▶` current, `•` queued, `−` skipped.

### 2.4 Keys at the gate

- **`y`** — approve current call; executor resumes; gate closes (or moves to next gated call).
- **`a`** — enable auto-accept-all-edits for the session **and** approve the current call if it is a file write/edit. From idle, `a` toggles the mode off/on. Status bar shows `EDITS:auto` while ON. Does **not** apply to commands (decision made by user spec: commands stay allowlist-or-prompt).
- **`n`** — reject. The hidden `Input#reject-reason` appears in the action footer and takes focus, placeholder *"optional reason — enter to send, esc to cancel"*. `enter` (even empty) confirms rejection; `escape` cancels and returns to the pending gate. On rejection: **all remaining calls in the turn are skipped** (they presumed the rejected effect). The queue strip flips them to `−`. Rejected-but-continue was rejected as a mode: it produces incoherent state (tests run against unedited files) and costs an extra decision per call.

### 2.5 What goes back to the LLM

The results payload (composed immediately after the last call resolves) carries one entry per call with status `ok | error | rejected | skipped` plus the user's reason on the rejected one, e.g. conceptually:

```
call 2 edit_file → rejected: "wrong function — fix parse_date, not format_date"
call 3 run_command → skipped (turn aborted after rejection of call 2)
```

Exact wire grammar is the protocol designer's lane; the status enum + `user_note` field is a contract (§11). Errors during auto-run calls (file not found, command exit≠0) do **not** gate or abort: the result entry carries the error/output and execution continues — the LLM is the error handler.

---

## 3. Clipboard watcher integration

### 3.1 Worker pattern

Started at session arm, per the research digest:

```python
self.run_worker(self._clipboard_loop, thread=True, exclusive=True,
                group="clipwatch", exit_on_error=False)

def _clipboard_loop(self) -> None:                     # runs in thread
    worker = get_current_worker()
    while not worker.is_cancelled:
        time.sleep(self._interval)                     # 300 ms default
        text = self._provider.poll()                   # Win: seqnum shim → read only on change
        if text is None:
            continue
        h = blake2b_hash(text)
        if h in self._self_written or h in self._recently_seen:
            continue
        self._recently_seen.append(h)                  # deque(maxlen=8)
        if "===CLIP:" not in text:
            continue                                   # cheap pre-filter; 5 MB junk costs ~ms
        result = parse_reply(text)                     # parse in thread; it's pure CPU, fast
        self.post_message(ReplyCandidate(result))      # thread-safe per digest
```

Messages (all `textual.message.Message` subclasses, posted from the thread): `ReplyCandidate(parsed | ParseError)`, `WatcherStateChanged(state)`, `ClipboardFault(detail)`. All clipboard **writes** also route through this one thread via a small queue, per the digest's single-clipboard-thread recommendation; the write path records the payload hash into `_self_written` *before* writing (suppresses self-detection race-free).

### 3.2 Dedup and stale-reply guard

- Content hash dedup (`_recently_seen`, last 8) absorbs the user copying the same reply twice.
- Turn-id guard: parsed replies carry the echoed turn number (protocol contract §11). A reply with `turn <= last_completed_turn` is dropped with a toast *"stale reply (turn 3) ignored — current turn is 5"*. No turn id present → hash dedup only.
- Reply arriving while `EXECUTING`: queued (depth 1, newest wins), status segment shows `+1 queued`, toast warns. Processed when the turn composes its results — almost always this is the user re-copying; the dedup/turn guard then discards it silently.

### 3.3 Status bar (the "armed/ingested/waiting" indicator)

Six `Static` segments, words not emoji, colored via CSS classes, driven by reactives:

```
● ARMED │ ChatGPT 4.0k │ out 3.4k/4.0k (1/1) │ turn 5 │ EDITS:auto │ ~\Dev\AgentClip
```

Watcher segment states: `● ARMED` (green, polling), `◍ EXECUTING` (yellow), `◍ APPROVAL?` (yellow, blinking class), `◍ PART 2/3` (chunk mode), `○ PAUSED` (dim), `✗ CLIP ERR` (red — provider fault, manual mode active), `○ IDLE`, `✓ DONE`.

### 3.4 Manual fallback and copy-again

- **`i`** — *ingest now*: one forced read of the clipboard, **bypassing** hash dedup and the `===CLIP:` pre-filter result caching (still must parse). This is the hotkey fallback when polling is paused/broken or the watcher mis-deduped.
- **`c`** — *copy again*: re-copies the current outbound payload (bootstrap, results, or — in chunk mode — the current part). Status flashes `re-copied part 2/3 (11,990 chars)`. This is the recovery for "user copied something else and clobbered the clipboard".
- **`w`** — pause/resume the watcher (auto-paste-detection off; `i` still works).
- Provider death (both copykitten and pyperclip failing): watcher posts `ClipboardFault`; status shows `✗ CLIP ERR`; `c` falls back to OSC-52 via `App.copy_to_clipboard()` (write-only, fine for outbound); inbound becomes a modal with instructions to use `i` after fixing, plus the payload shown selectable (Textual 8.2 native text selection) as a last resort.

---

## 4. Transcript design

One mounted widget per event in `TranscriptPanel(VerticalScroll)`; each new widget gets `.anchor()`. Rendering per event type:

| Event | Widget | Rendering |
|---|---|---|
| User task / follow-up / ask_user answer | `Markdown` `.ev-user` | left accent border, "you ▸" label line |
| LLM prose (text outside CLIP blocks) | `Markdown` `.ev-prose` | streamed not needed (arrives whole); plain `Markdown(prose)` |
| Tool call + result | `Vertical .ev-call`: summary `Static` + optional `Collapsible` | summary: `▶ run_command pytest -q · exit 1 · 74 lines` → glyph flips to `✓`/`✗` on completion. Body > 8 lines goes in `Collapsible(title="output (74 lines)", collapsed=True)` containing a `Static` with `rich.syntax.Syntax` (lexer by content: `diff`, file extension, or plain) |
| Approval / rejection | `Static .ev-approval` | `✓ approved edit_file src/utils.py` / `✗ rejected: "wrong function" · 2 calls skipped` |
| Outbound copy | `Static .ev-note` | `→ results copied (3,412 chars, 1 part)` |
| Backup notice | `Static .ev-note` | `▣ backup turn 5 (2 files)` |
| Parse/clipboard errors | `Static .ev-error` | red, full reason, remedy hint (`press c to re-copy`, `copy the full reply`) |

Expand interaction: `Collapsible` is focusable; `tab`/arrows reach it, `enter` toggles (native). Additionally `x` toggles the **most recent** collapsible in the transcript — covers the common "what did that command actually print" glance without focus navigation.

The `RichLog` single-widget transcript was rejected: no per-message collapse/expand, which this design leans on.

---

## 5. Diff presentation

All diffs render as `rich.syntax.Syntax(..., theme="ansi_dark", word_wrap=False)` inside a `Static` in `#action-body` (`VerticalScroll`, so long diffs scroll with arrows/PgUp/PgDn; `word_wrap=False` keeps hunks readable, horizontal overflow clips). No `textual[syntax]` extra — pygments via Rich, per the digest.

- **`edit_file` (find/replace)**: compute the post-edit file in memory, `difflib.unified_diff(old, new, n=3)` restricted to affected hunks, render with the `diff` lexer. Title line: `edit_file src/utils.py · 1 hunk · −1/+1`. If `find` matches zero or >1 locations, that is an executor *error result*, never a gate — nothing to approve.
- **`write_file`, file exists**: same unified-diff path, title `write_file (overwrite) src/config.py · −12/+40`.
- **`write_file`, new file**: full content as `Syntax(content, lexer_from_extension, line_numbers=True)` under a green banner `NEW FILE src/cli.py (84 lines)`. An all-`+` unified diff was rejected: `+` gutters add noise and lose language highlighting on brand-new code.
- **`run_command` gate**: body is the command line in a bordered `Static` plus the cwd and the note `not on allowlist`; hint line adds *"edit allowlist in F2 settings"*.

---

## 6. Chunked-send UX (chunk-walk mode)

When an outbound payload exceeds the preset budget, the protocol layer splits it; the TUI walks the user:

1. Part 1 is copied automatically. ActionPanel shows the wizard:
   ```
   CHUNKED SEND · part 1/3 on clipboard (11,990 chars)
   1. Alt-tab to the chat, paste, send.
   2. The model replies "ACK 1/3" — click its Copy button.
   3. Alt-tab back; the next part is copied automatically.
   [space] skip ACK & arm next part   [c] re-copy this part   [esc] abort send
   ```
   Status segment: `◍ PART 1/3`.
2. Watcher sees the ACK block (protocol marker w/ part number + length echo): on **match** → auto-copy part 2, bell, wizard advances. On **NACK / length mismatch** → re-copy the *same* part, red toast *"part 1 arrived truncated (got 9,400/11,990) — re-copied, paste again"*.
3. Final part sent → model's substantive reply comes back through the normal ingest path; wizard closes, state `ARMED`.

`space` exists because ACK round-trips cost a copy per chunk and some users will trust their service; it advances without verification. Inbound chunking (model output too big) is the protocol designer's problem; the TUI just renders however many ingests arrive against one turn.

**Calibration** (`F3` from NewSessionScreen, or palette `calibrate paste budget`): copies a numbered marker payload sized to the preset max; user pastes it; model reports the last marker seen; user copies that reply; TUI parses it and toasts a suggested budget with one-key accept (`y`).

---

## 7. Undo UX

- Before the first file mutation of each turn, the executor snapshots every to-be-touched file (and records created-file paths) under `.agentclip/backups/turn-NNN/` (storage layout = architecture lane). The transcript gets `▣ backup turn 5 (2 files)` so the user knows the safety net exists.
- **`u`** (dynamic: enabled when ≥1 undoable turn exists and state is `IDLE`/`ARMED`/`DONE`, never mid-`EXECUTING`) → `ConfirmScreen` listing exactly what restores: *"Restore 2 files modified in turn 5 (src/utils.py, tests/test_utils.py); delete 1 created file (src/new.py). Commands run in this turn are NOT undone."* Plus a `Checkbox` *"copy a revert notice for the LLM"*, default **ON** when a session is armed.
- On confirm: files restored, transcript line `↩ undid turn 5 (2 restored, 1 deleted)`, and — if the checkbox was on — a notice payload is composed and copied: a plain protocol note telling the model *"the user reverted all file changes from turn 5; file state is as before that turn"* (grammar = protocol contract). Without the notice the model's mental file state diverges and its next edit_file `find` anchors miss — so it defaults on; OFF exists for post-session cleanup.
- Repeated `u` walks back turn by turn (5, then 4, …). Whole-session undo lives on SummaryScreen (`u` there loops it).

---

## 8. Notifications

The user is staring at the browser; the terminal must call them back:

- `self.app.bell()` (BEL → Windows Terminal taskbar flash / audible per WT settings) **and** `self.notify(...)` toast, both individually switchable in config.
- Fired on: approval needed (`severity="warning"`, this is the big one), ask_user question, parse error / partial copy, chunk ACK ok (next part armed) and NACK, task_done (`severity="information"`), clipboard provider fault (`severity="error"`).
- Not fired on: routine auto-run completions, outbound copies the user just triggered (they're looking at the terminal already).
- The `◍ APPROVAL?` status segment gets a CSS blink class so a glance at the taskbar-restored terminal lands on it.

---

## 9. Edge cases

- **Partial copy** (`===CLIP:CALL` without `===CLIP:END===`, or chunk header without body): parser returns `ParseError(kind="truncated")` → red transcript line + toast *"partial protocol block — click the reply's Copy button and try again"* + bell. State stays `ARMED`; the bad content's hash is remembered so it doesn't re-toast every tick.
- **Prose-only reply** (user copied the right reply but the model emitted no blocks — pre-filter fails): nothing auto-ingests. `i` on such content ingests it as LLM prose into the transcript and toasts *"no tool calls found — reply shown in transcript"*; the user can then send a follow-up with `t`. This also covers "model forgot the protocol" — follow-up nudges it.
- **Two different replies copied quickly**: depth-1 queue per §3.2; second waits for turn completion; dedup discards if identical/stale.
- **5 MB unrelated clipboard**: Windows pays nothing until seqnum changes; one read + substring scan + hash ≈ ms; no protocol marker → ignored. A hard cap (8 MB) skips parsing entirely with a one-time dim toast.
- **Terminal resize**: Textual reflows; ActionPanel `max-height: 60%; min-height: 8;` so the transcript never fully disappears; status segments have CSS `text-overflow: ellipsis` with the watcher segment first (highest priority). No "too small" overlay — degrade silently.
- **`ask_user`**: ActionPanel switches to question mode — question as `Markdown` in `#action-body`, `TextArea#answer` revealed and focused (multi-line answers: tracebacks, choices). `ctrl+enter` submits; the answer is recorded as an `ask_user` result and the turn continues (remaining calls run), then composes/copies as usual. While the TextArea is focused, all letter bindings are naturally inert. `escape` blurs to let the user scroll the transcript; re-`tab` to resume typing.
- **User quits mid-turn** (`ctrl+q` during `EXECUTING`/gate): ConfirmScreen warns the turn is incomplete and results were never sent; backups for the turn are kept.
- **LLM emits unknown tool / malformed call body**: that single call gets an `error: unknown tool 'foo'` result entry (LLM self-corrects next turn); the rest of the turn proceeds.

---

## 10. Key binding table

`Binding(...)` on MainScreen unless noted. Dynamic = gated via `check_action` + `reactive(..., bindings=True)` (`pending_approval`, `session_state`, `chunk_mode`, `has_undo`, `has_outbound`); `None` returns show dimmed keys in `Footer` for discoverability.

| Key | Action | Context (check_action) |
|---|---|---|
| `y` | approve pending call | pending_approval |
| `n` | reject pending call (opens reason Input) | pending_approval |
| `a` | auto-accept edits: enable+approve / toggle | pending file gate, or idle-in-session |
| `u` | undo last turn (ConfirmScreen) | has_undo and not EXECUTING |
| `c` | re-copy current outbound / current part | has_outbound |
| `i` | force-ingest clipboard now | session active |
| `w` | pause/resume watcher | session active |
| `space` | chunk mode: skip ACK, arm next part | chunk_mode |
| `t` | follow-up message to LLM (composer modal, ctrl+enter sends) | session active and not EXECUTING |
| `e` | end session → SummaryScreen | session active and IDLE/ARMED |
| `x` | toggle most recent transcript collapsible | always (main) |
| `enter` | toggle focused Collapsible | native, when focused |
| `pageup`/`pagedown` | scroll transcript | always (main) |
| arrows / `pgup`/`pgdn` | scroll focused panel (diff body autofocused at gate) | native |
| `escape` | cancel reject-reason / abort chunk send / dismiss modal | contextual |
| `F1` / `?` | HelpScreen | global (App) |
| `F2` | ConfigScreen | global (App) |
| `F3` | calibrate paste budget | NewSessionScreen / palette |
| `ctrl+enter` | submit TextArea (task / answer / follow-up) | on the TextArea's screen |
| `ctrl+p` | command palette (every action mirrored here) | global, Textual default |
| `ctrl+q` | quit (Confirm if mid-turn) | global, Textual default |
| SummaryScreen: `u` undo session, `t` new session, `esc` close | | modal-local BINDINGS |

No `priority=True` letters anywhere — focus-based suppression is the safety mechanism for text inputs, and modals (Confirm/Help/Summary) isolate their own bindings by being ModalScreens.

---

## 11. Contracts for other designers

**Protocol designer must provide:**
1. Per-call result status enum `{ok, error, rejected, skipped}` + optional `user_note` string (rejection reasons) in the results grammar.
2. A monotonically increasing **turn id** in outbound payloads that the model echoes in its reply header — the TUI's stale/duplicate-reply guard depends on it (falls back to hash dedup if absent).
3. Chunk grammar: part header `k/n` with `len=` char count; ACK/NACK blocks that survive markdown-stripping copy (sentinel lines only, no backticks/asterisks — same rule as all markers); NACK carries observed length.
4. `ask_user` call shape (question text) and `task_done` with a summary field (rendered on SummaryScreen).
5. Bootstrap text must include: (a) "user's message may arrive as an attached pasted-text file — read it entirely"; (b) for Copilot/Gemini presets, "emit all CLIP blocks inside ONE fenced code block"; (c) ACK instructions for chunked input. A `CALIB` block grammar for the calibration flow.
6. Revert-notice block ("user reverted turn N's file changes") for the undo flow.
7. Parser must tolerate: fences wrapped/stripped, leading "Copilot said:", trailing Perplexity citations — and report `truncated` vs `malformed` distinctly (TUI gives different remedies).

**Architecture designer must provide:**
1. `ClipboardProvider` protocol: `read_text() -> str|None`, `write_text(str)`, `name`, `healthcheck()`; Windows variant exposes `changed() -> bool` (GetClipboardSequenceNumber shim) so the watcher can skip reads. Provider selection at startup; active provider name surfaced for the status bar.
2. Single clipboard thread owning reads and writes (write queue), with the self-written-hash registration happening inside that thread before the write.
3. Backup store: `snapshot_turn(turn, paths, created) -> BackupId`, `restore_turn(turn) -> RestoreReport(restored, deleted)`, retention pruning; synchronous and fast (TUI calls from executor worker).
4. Executor as an async service: `execute(call) -> CallResult`, cancellable (quit mid-turn), `run_command` via subprocess without blocking the event loop; allowlist matcher returns the matched rule string (displayed in transcript).
5. Preset table struct with **two numbers per service** (`inline_safe`, `max`) + flags (`wrap_in_fence`, `attachment_ok`); user-editable, persisted via `platformdirs`.
6. Command output larger than budget: executor returns full output (TUI shows it all locally in the Collapsible); the payload composer truncates per budget with the protocol's truncation marker.
7. Testability: watcher injectable; tests post `ReplyCandidate` directly and drive with `pilot.press("y")` per `run_test` (`pytest-asyncio`, `pilot.pause()` after posts).