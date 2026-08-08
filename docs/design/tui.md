# AgentClip TUI Design (Textual 8.2.x)

Prime directive honored throughout: the steady-state loop costs the user **one keypress in the terminal** (`y`) — everything else (ingest, execute reads, compose results, copy to clipboard, bell) is automatic. All key choices below assume no text input is focused on the main screen during the agent loop, so bare letters are safe screen bindings; the moment a text widget gains focus (ask_user answer, reject reason, composer), Textual's focus routing suppresses them automatically.

---

## 1. Screen map

### 1.1 App and screen inventory

```
AgentClipApp(App[None])            # CSS embedded in App.CSS (avoids PyInstaller --add-data)
├── MainScreen(Screen)             # default, always installed; also *starts* sessions (§1.3)
├── ServiceEditorScreen(ModalScreen[dict[str, ServicePreset] | None])   # F2 (§1.4)
├── SummaryScreen(ModalScreen[SummaryAction])
├── ConfirmScreen(ModalScreen[bool])   # generic y/n confirm (undo, end-session, quit-mid-turn, discard-edit)
├── HelpScreen(ModalScreen[None])      # static key/flow cheatsheet
└── TextEntryScreen(ModalScreen[str | None])   # manual paste fallback for force-ingest
```

### 1.2 MainScreen widget tree

```
MainScreen
├── Horizontal                       id=body              # 1fr height
│   ├── Vertical                     id=main-col          # width 1fr: the chat column
│   │   ├── TabbedContent            id=chats             # 1fr height; one pane per session view (§1.6)
│   │   │ └── TabPane "master"       id=tab-master        # + "▶ <title>" / "✓ <title>" per sub-agent run
│   │   ├── TranscriptPanel(VerticalScroll)  id=transcript        # inside tab-master; sub panes get id=tr-sub-N
│   │   │   ├── Markdown                     .ev-user             # user task / follow-ups
│   │   │   ├── Markdown                     .ev-prose            # LLM prose between blocks
│   │   │   ├── Vertical                     .ev-call             # one per tool call
│   │   │   │   ├── Static                                        # "▶ edit_file src/utils.py · 1 hunk · ✓ ok"
│   │   │   │   └── Collapsible(collapsed=True)                    # long payloads only
│   │   │   │       └── Static                                     # Rich Syntax / plain text
│   │   │   ├── Static                       .ev-note / .ev-error / .ev-approval
│   │   │   └── ...
│   │   ├── ActionPanel(Vertical)    id=action            # display:none when idle; max-height:60%
│   │   │   ├── Static               id=action-title      # "APPROVE · call 2/5 · edit_file src/utils.py"
│   │   │   ├── Static               id=queue-strip       # "✓ read_file  ▶ edit_file  • run_command  • task_done"
│   │   │   ├── VerticalScroll       id=action-body       # diff / question / chunk wizard; focused on show
│   │   │   │   └── Static                                # Rich renderable
│   │   │   └── Horizontal           id=action-footer
│   │   │       ├── Static           id=action-hints      # "[y] approve  [n] reject  [a] auto-accept edits"
│   │   │       └── Input            id=reject-reason     # hidden until 'n'
│   │   ├── RunningBar(Static)       id=running           # spinner + "(ctrl+x to cancel)" while a turn executes
│   │   └── ChatComposer(TextArea)   id=composer          # the one text box: task, answers, follow-ups
│   └── Sidebar(Vertical)            id=sidebar           # width 32; F3 hides it (§1.3)
├── StatusBar(Horizontal)            id=statusbar         # full width, height 1 (sits above Footer)
│   └── Static ×6                    .seg                 # see §3.3
└── Footer()                                              # key hints, auto-dimmed via check_action
```

Layout reasoning: the chat column is transcript on top with the ActionPanel as a bottom drawer — a side-by-side split for the *diff* was rejected because diffs and command output need horizontal room, and the drawer keeps the last few transcript events visible above the diff, which is enough context to approve. The sidebar is the exception: it is narrow, static settings chrome (not content), and `F3` collapses it whenever a diff wants the full width back.

Transcript auto-scroll is fit-or-park (`TranscriptPanel._autoscroll`, decided per event after its layout settles — Markdown events are awaited via `Markdown.update` so their real height is known): an event that **fits** the visible area pins the panel to the bottom with the container-level `anchor()` (which releases when the user scrolls up and re-engages when they return to the bottom — NB: anchoring the event widgets themselves, the original approach here, is a silent no-op in Textual 8); an event **taller** than the visible area is parked with its top at the top of the view so the user reads the response from its first line. While parked, follow-up noise (tool calls, notes, outbound) mounts below without moving the view; a new conversational beat (user or assistant message) always re-applies the fit rule, and pinning also resumes once the scroll returns to the bottom. Transcript children are pruned beyond 500 events (oldest unmounted) to bound layout cost.

### 1.3 Session start flow (inline — no modal) and the Sidebar

There is **no launch dialog**. At startup the user sees the finished app: an empty transcript, the docked `ChatComposer` enabled and focused, and the settings sidebar on the right. Typing a task and pressing `Enter` starts the session.

Mechanics: `ChatView.prompt_new_session()` (the port the controller awaits, unchanged) is implemented on MainScreen *inline* — it flips `awaiting_new_session`, switches the composer's border title to `Describe the task · Enter starts the session · Ctrl+J newline`, unlocks the sidebar's service `Select`, focuses the composer and parks on an `asyncio.Future`. The first non-empty send resolves it with `SessionSpec(task, service=<sidebar selection>)`; an empty send just warns. While waiting, the composer text is taken **verbatim** (no slash-command parsing — the same rule as an `ask_user` answer), so a task may start with `/`.

Every "new session" moment reuses that one path: the launch, the budget-exceeded retry, `/new`, and the summary screen's *new session* choice all call `prompt_new_session()` again, which re-arms the same inline surface. There is no second way to start a session.

The controller then composes the bootstrap prompt (protocol layer), copies it, mounts the task as `.ev-user`, arms the watcher, and toasts `bootstrap copied (5.2k chars) — paste into ChatGPT`. If the bootstrap exceeds the preset budget the flow loops back to the inline prompt with an error toast (chunk-walk, §6, lands in M3).

```
Sidebar(Vertical)          id=sidebar        # width 32; F3 toggles display
├── Static "PROJECT" / Static id=side-root          # ~\Dev\AgentClip
├── Static "SERVICE"
├── Select[str]            id=service-select        # "key · 12k" rows; value = config.general.service
├── Static                 id=side-service-label    # "ChatGPT web (attachment OK) · 12,000 chars per paste · 500,000 chars context"
├── Button                 id=edit-services-btn     # "Edit services..." → App.action_settings() (F2)
├── Static "AGENT SLOT"
├── Select[str]            id=slot-select           # "MASTER" / "SUB-AGENT": which slot the buttons below calibrate (§1.6)
├── Static                 id=side-slot-note        # "the main agent's chat window" / "delegation ON" / "delegation off · need: copy button, new-chat button"
├── Static "CHAT WINDOW"
├── Button                 id=set-region-btn        # "Set chat region..." → draw-a-box overlay (§3.4a)
├── Static                 id=side-region           # "not set - ..." / "812×540 at (1050, 340) · chatbot window"
├── Button                 id=set-chatbox-initial-btn   # the input box AS IT SITS IN A FRESH CHAT (centred)
├── Static                 id=side-chatbox-initial
├── Button                 id=set-chatbox-ongoing-btn   # ...and as it sits in an ongoing chat (docked bottom)
├── Static                 id=side-chatbox-ongoing
├── Static "REASONING"
├── Button                 id=set-busy-btn          # "Set busy region..." → draw-a-box overlay, calibrated live
├── Static                 id=side-busy             # "not calibrated - ..." / "● GENERATING · match (diff 1.2%)"
├── Button                 id=set-idle-btn          # "Set idle button..." → the same, calibrated while IDLE
├── Static                 id=side-idle
├── Button                 id=set-stale-btn         # "Set response region..." → the answer text itself, no baseline (§3.4b)
├── Static                 id=side-stale            # "not set - staleness check disabled" / "○ response ready · stale (still ×4)"
├── Static "COPY BUTTON"
├── Button                 id=set-copy-btn          # "Set copy button..." → draw-a-box overlay (§3.4b)
├── Static                 id=side-copy             # "not set - ..." / "24×24 at (1830, 612) · set" / "... · clicked (diff 0.03)"
├── Static "NEW CHAT"
├── Button                 id=set-newchat-btn       # capture the browser's new-chat control (found in the region before every click)
├── Static                 id=side-newchat
├── Button                 id=newchat-btn           # "New browser chat": click it now, in the CALIBRATING slot
└── Static .side-hint                               # "F3 hides this column · F2 settings · F1 help"
(first child, above PROJECT: Static id=side-paste-flash — the blinking ">>> PRESS CTRL+V <<<" / ">>> PRESS ENTER <<<" banner, hidden until an outbound copy; §3.4b)
```

**The finish detectors are presence questions, not fixed rectangles.** "Is the model still generating?" is answered by asking *is the stop button on screen anywhere in the chat?*, not *does this rectangle still look like it did?* — so the user captures what the busy indicator **looks like** once per service ("Capture busy indicator...", `TemplateKind.BUSY`) and `agentclip.screen.presence.PresenceTracker` searches for it inside the drawn chat region on every poll. The prompt warns against boxing an animated spinner, and that warning lives on the enum rather than in the TUI: an animation is a different picture every frame, so it can never be matched back, and that is a fact about the appearance, not about the caller.

The de-bounce is deliberately **asymmetric**. The two mistakes are not equally bad: believing the model is still generating when it has finished costs one poll interval, while believing it has finished when it hasn't harvests a truncated answer and feeds it to the engine as the whole response. So "still generating" is adopted the instant one frame supports it, and "finished" must survive `required_ticks` consecutive frames. "Capture idle indicator..." (`TemplateKind.IDLE`) is the mirror — something on screen only while the chat is **idle**, usually the send button — with `found_is_busy=False`, so `#side-idle` reads the inverse polarity. Both repaint via `Sidebar.update_template(kind, …)` with an unmistakable state string (`● GENERATING · match (diff 1.2%)` / `○ response ready · changed (diff 34.0%)` / `✗ capture failed`).

The third detector has **no button and no capture at all**: the drawn chat region reading unchanged frame to frame *is* a finished response, whatever a particular service's pixel cues do (`agentclip.screen.stale.StaleTracker`, comparing each poll to the previous one rather than to a fixed anchor). Once it has read unchanged (`STALE_MAX_DIFF = 0.002` over up to `STALE_MAX_SAMPLES = 16384` samples) for `required_ticks` consecutive polls it reports STALE. `#side-stale` (`Sidebar.update_stale`) is a readout only: `○ response ready · stale (still ×4)` / `● GENERATING · changing (diff 0.08% · still ×2)` / `✗ capture failed`. Because it rides on the box every slot draws anyway, **every slot has a working finish detector from its first drag** — busy and idle only reinforce it.

All three share one knob and one tick. `required_ticks` comes from the active service preset's `stable_seconds` (§1.4) via `max(1, round(stable_seconds / _BUSY_POLL_S))` for *every* tracker, because "how long is long enough" is a property of the service's streaming cadence, not of which detector is asking. And each tick takes **one** capture of the chat region and hands it to all of them: cheaper, but mainly it makes them judge the same instant instead of three moments of a moving screen, and it makes a failed capture reach all of them as the same ERROR (`PresenceTracker.observe(None)` / `StaleTracker.observe(None)` — streaks intact, so one dropped frame cannot restart the stillness clock on an in-flight finish). Their verdicts drive the auto-copy-click trigger described in §3.4b and nothing else. The drawn region survives `/new` (only the live-slot pointer resets to MASTER, and the poller restarts against the surviving window); the captured appearances survive the process itself.

The `Select` is the session's service ("profile") picker and the only place it is chosen; its value flows into `SessionSpec.service` and from there into the engine and the status bar's service segment. It is **disabled while a session is active** (a session's paste budget is baked into its engine) and re-enabled whenever `prompt_new_session` is waiting. After the services table itself changes (service editor), the sidebar rebuilds its options via `Sidebar.refresh_services(config)`, preserving the current selection when that preset survived.

Rejected alternatives: a one-line `Input` for the task — coding tasks are multi-line (pasted tracebacks), so `ChatComposer` (Enter sends, `ctrl+j` newline, paste keeps newlines) is the right widget. And the original launch modal — it forced a decision (which service?) before the user had seen anything, and duplicated a text box the main screen already owns.

### 1.4 ServiceEditorScreen (shipped M3; scope narrowed from the original ConfigScreen sketch)

The original ConfigScreen sketch covered the whole config surface (allowlist, poll interval, bell/toast switches, backup retention); what actually shipped is narrower and sharper: a `ModalScreen["dict[str, ServicePreset] | None"]` (`src/agentclip/tui/screens/service_editor.py`) dedicated to the things users actually need to tune per chat service — its name, the two size numbers (`max_paste_chars`, `total_context_chars`), and the stale-detector's stillness window (`stable_seconds`, §3.4b). The rest of the config surface is still hand-edited TOML; nothing else needed a form badly enough to justify one yet.

Opened via `F2` (`AgentClipApp.action_settings`) or the sidebar's "Edit services..." button. Both routes are synchronous entry points into an async flow (`push_screen_wait` needs a worker), so `action_settings` is a plain `def` that hands off to `self.run_worker(self._open_service_editor(), group="settings", exclusive=True)` — the same shape as `AgentClipApp._confirm_quit`. Re-entrant guard: if the editor is already the active screen, F2/the button are no-ops.

```
ServiceEditorScreen(ModalScreen)  .modal-box, id=service-editor-box (width 112)
├── Static "SERVICE EDITOR"                     .title
├── Horizontal                    id=svc-body
│   ├── Vertical                  id=svc-list-col   (width 36)
│   │   ├── Static "Services"                   .side-title
│   │   └── Select[str]           id=svc-select      # "key (builtin|custom)" rows + "+ Add new service..."
│   └── Vertical                  id=svc-form-col   (1fr)
│       ├── Input                 id=svc-key         # disabled unless in "add new" mode
│       ├── Input                 id=svc-label
│       ├── Input                 id=svc-max         # max input size (chars per paste)
│       ├── Input                 id=svc-total       # total context size (chars)
│       ├── Input                 id=svc-stable      # stale detector: seconds unchanged = finished (default 2.0, bounds 0.5-60)
│       ├── Static                id=svc-error       # inline validation message, styled $error
│       └── Horizontal            id=svc-actions
│           ├── Button "Add service"    id=svc-add-btn     # visible only in "add new" mode
│           ├── Button "Reset to default" id=svc-reset-btn # visible only for a built-in key
│           └── Button "Delete"         id=svc-delete-btn  # visible only for a non-built-in key
└── Static "escape closes (applies valid edits) · ..."     .hint
```

**Model:** the screen works on an in-memory *working copy* of `config.services`. Selecting an existing preset and editing its name/sizes applies **live** — every keystroke revalidates the whole candidate (key + label + both sizes together, since "max ≤ total" is a cross-field rule) and, only while the candidate is fully valid, writes it straight into the working copy; an invalid candidate is never applied, it just leaves `#svc-error` showing why. Adding a new preset is the one *discrete* action instead of a continuous one: fill in a unique lowercase-hyphen key (validated against `^[a-z0-9]+(-[a-z0-9]+)*$`, immutable once created) plus the other fields, then press "Add service" (enabled only once the candidate validates) — creation is a one-time event best gated behind an explicit action rather than silently committing a half-typed key on every keystroke. The fourth field, `stable_seconds` (`#svc-stable`, "Stale after (seconds unchanged)"), folds into the same candidate and the same live-apply/discrete-add split, but validates on its own (`0.5`–`60`, the exact bounds `config.py`'s `_take_float` enforces on load, so a value the editor accepts is never silently replaced on next start) rather than cross-checked against the two sizes; unlike the blank size fields it is pre-filled with `DEFAULT_STABLE_SECONDS` (`2.0`) when adding a new service, since it has one sensible default most users never need to touch.

**Escape** (`action_close`, same worker hand-off shape as `action_settings` since it awaits a modal): if the currently displayed fields are valid, the screen dismisses with the working `services` dict if it differs from what it opened with, or `None` if nothing changed (so the caller has nothing to persist). If the currently displayed fields are invalid, nothing was ever written to the working copy (invalid values are never committed) — so there's no real edit to lose — but the visible text would still vanish, which is surprising, so escape instead asks via the shared `ConfirmScreen`: *"The current field values are invalid (\<reason\>) and were never applied. Close the service editor anyway?"* Denying returns focus to the editor; confirming closes as if the field had never been touched.

**Delete vs. reset:** the twelve built-in keys (`config.BUILTIN_SERVICE_KEYS`) can be edited and reset but never deleted — `#svc-delete-btn` only appears for a non-built-in key. `#svc-reset-btn` only appears for a built-in key and restores its shipped values (`config.default_services()[key]`) regardless of whether it currently differs (idempotent no-op if it's already default).

**Wiring on close** (`AgentClipApp._open_service_editor`): a non-`None` result is persisted via `config.save_services(result, self._global_config_path)`, which writes a preset's whole `[services.<key>]` table only when it differs at all from its built-in default (an untouched or reset-to-default built-in is omitted entirely) — and, within a written table, adds `stable_seconds` only when *that field itself* still differs from the built-in's, so editing just the size fields on a preset left at the default `2.0` doesn't pin today's value into the file; a service that never customizes it keeps tracking future changes to the shipped default. That result is folded into a fresh `Config` via `dataclasses.replace(self.app_config, services=result)`, and propagated to `self.app_config`, `MainScreen.update_config` (which also updates the `SessionController`'s config so the *next* session and any `/new` see it), and `Sidebar.refresh_services`. Engine construction reads `Config` fresh per session start (`cli.make_engine_factory` takes a `get_config: Callable[[], Config]`, called on every `build()`, not once) — so a saved edit takes effect for the next session in this process without restarting the app, while a session already in flight keeps the `Config` snapshot its `Engine` was built from. Opening/using the editor mid-session never touches `awaiting_new_session`, so the sidebar's service `Select` stays exactly as locked/unlocked as it was before.

### 1.5 End-of-session summary (SummaryScreen)

Pushed on demand via `e` (end session); it is **not** auto-pushed on `task_done` — the user stays in the chat and may follow up (protocol.md §8). Contents: a `Static` rendering a Rich `Table` — turns, calls per tool, files created/modified (paths), commands run, total chars pasted both ways, and `sub-agent runs` when the session delegated at least once — plus the `task_done` summary text from the LLM as `Markdown`. Bindings on the modal: `u` undo entire session (turn-by-turn restore, with ConfirmScreen), `t` new session, `escape` back to main (transcript stays for review), `ctrl+q` quit.

### 1.6 Agent slots and transcript tabs (delegation)

Delegation (protocol.md §3's `delegate` tool) gives the model a second chat window to run a bounded sub-task in. That turns two singletons into pairs, and each got the smallest change that works.

**Slots — one calibration set per browser window.** Everything the user draws (chat region, both chat boxes, the busy/idle/stale detectors, the copy button, the new-chat button) moves off MainScreen and into `SlotCalibration` (`agentclip.screen.slot` — pure data, stdlib only, unit-tested without Textual), one per `AgentSlot`: `MASTER` is the chat the session runs in, `SUBAGENT` the window a delegated sub-agent gets. Two independent pointers ride on top:

- `_calibrating` — which slot the sidebar's buttons write into, moved by the `#slot-select` picker. Never locked by session state: the user must be able to calibrate the sub-agent window while the master chat is mid-turn. Both slots share the picker code, so the SUB-AGENT prompts are prefixed *"SUB-AGENT window · …"*.
- `_live` — which slot the automation drives right now (the focus click, the finish-detector poller, the auto-copy flow). Only `start_browser_chat` / `end_browser_chat` move it.

`SlotCalibration.can_delegate` is the single source of truth for "delegation is available", and it is strict on purpose — a new-chat button *and* somewhere to paste *and* a finish detector *and* a copy button. Without all four a sub-run would strand halfway, so a half-calibrated slot must read as unavailable. `missing()` returns the gaps, which the sidebar shows in `#side-slot-note` and which the controller embeds in the error the *model* gets if it calls `delegate` against an uncalibrated host.

`start_browser_chat(slot)` is deliberately all-or-nothing: it *finds* the new-chat button inside that slot's chat region (`_click_profile_element`), clicks where it actually is, and **only then** retargets `_live`, resets the finish trigger and restarts the poller. A `False` return guarantees nothing was clicked and nothing was retargeted, which is what lets the controller abort a delegation before its first paste — a sub-agent's bootstrap pasted into the master's chat would corrupt that conversation irrecoverably. `end_browser_chat()` is the mirror and is unconditional: it runs in the controller's `finally`, so it must work even after the sub-run blew up.

**Tabs — one transcript per session view.** The transcript becomes a `TabbedContent` with one `TranscriptPanel` per pane. The master's is always there (its widget ids are the pre-tabs ones, so every existing selector still resolves); each delegation adds one, labelled `▶ <task title>` while it runs and `✓ <task title>` once it is done. Nothing is ever removed or disabled short of `/new` — the panels are output-only and the composer always targets the controller's *active* session, so leaving a finished sub-agent's transcript readable costs nothing.

The load-bearing distinction is **focused panel ≠ visible tab**. `MainScreen.transcript` resolves to `_focused_panel`, which only `focus_session_view` moves; clicking a tab (or `F6`) moves what the user *sees* and nothing else. Without that split, a user reading the master tab mid-delegation would silently divert the sub-agent's output into it, which looks exactly like data loss. `render_log` walks every panel — the master's transcript first, then each sub-agent's under a `## sub-agent: <title> (<chat_name>)` heading — so an export carries the whole delegation tree.

**Who is talking.** During a sub-run every piece of state the controller pushes describes the sub-agent, so three places say so: the watcher status segment is rebadged `◆ SUB-AGENT · …` in magenta (`.st-sub`, a colour used nowhere else), the approval drawer's title gains a `SUB-AGENT ‹task title› · ` prefix, and every bell/toast is prefixed `sub-agent: `. The `SessionView` snapshot carries `session_id` / `session_role` / `session_title` for exactly this (additive, master-shaped defaults).

**Known limitation.** The tool catalog is baked into the bootstrap, so whether the model is offered `delegate` at all is decided once, at session start, from `SlotCalibration.can_delegate`. Calibrating the sub-agent slot mid-session notifies *"sub-agent slot ready — /new to give the model the delegate tool"*; it cannot be retro-fitted into a conversation the model has already read.

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
| `delegate` | auto-runs (master only, and only when the sub-agent chat is calibrated): parks the turn and runs a sub-agent in the second window — §3.4c. Never gated, because opening a sub-agent chat is loudly visible and every call the sub-agent makes is gated normally |
| `task_done` | auto-runs; ends the turn and marks the session complete (the user may still follow up to reopen it) |

YOLO mode (§2.6) overrides the whole table for the gated rows: when ON, `run_command`, `write_file`, and `edit_file` all auto-run regardless of allowlist/deny tokens.

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

### 2.6 YOLO mode (auto-approve everything)

`/yolo` (typed in the chat box) flips a session-scoped policy flag that makes **every** tool call auto-run — edits *and* commands — bypassing the allowlist **and** the deny tokens. It is the bigger hammer above auto-accept-edits (`a`), which only covers edits. Use it for trusted/throwaway projects where the round-trip approval cost outweighs the risk.

- Toggle: bare `/yolo` flips it; `/yolo on` / `/yolo off` set it explicitly.
- Scope: the session. A new session (`/new`, or the summary's "new") resets it to the configured default (`[approval] yolo`, default `false`).
- Mechanics: `ApprovalPolicy.verdict()` short-circuits to `auto` for edit/command kinds when `yolo` is set (read-only tools were already auto). The engine never builds a pending gate, so the turn runs unattended end-to-end. Audited per call as `auto: YOLO mode (auto-approve all)` and as a `yolo` session event on toggle.
- Indicator: the status bar `EDITS` segment becomes a red `⚡ YOLO` badge (`.st-yolo`) so the disarmed state is unmissable while the user is staring at the browser.

This is deliberately a **runtime** toggle (not gated behind a confirm): it is opt-in by typing, reversible with `/yolo off`, and every change still lands in the per-turn backup store, so `undo` works as usual.

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

The `EDITS` segment shows `EDITS:ask` (default), `EDITS:auto` (auto-accept-edits ON), or a red `⚡ YOLO` badge (`.st-yolo`) when YOLO mode (§2.6) is armed — YOLO takes display priority over `EDITS:auto`.

### 3.3a Chat commands

The chat box also accepts slash commands (parsed in `SessionController.submit_message`, so any future front-end inherits them; an unknown `/command` is reported, not sent to the model):

- `/yolo [on|off]` — toggle YOLO mode (§2.6).
- `/new` — clear the transcript and start a fresh session (re-arms the inline start flow of §1.3; the service picker unlocks so the next session can use a different preset). Refused mid-turn (answer/finish the current step first); reachable while armed/idle or after `task_done`.
- `/abort` — end the delegated sub-agent run in flight (§3.4c). A no-op with a warning when nothing is delegated.
- `/help` — list the commands in the transcript.

Precedence: while answering an `ask_user` question, the typed text is **always** the answer (commands are not parsed) — so a slash-leading answer like `/etc/hosts` is delivered verbatim, never eaten. A follow-up message that must begin with a literal slash is escaped as `//…` (one slash is stripped and the rest sent as a message).

### 3.4a Screen regions and the focus click (the "hand me back to the browser" nudge)

The user draws **one** box for this — the **chat region** (`#set-region-btn` → `#side-region`), the *window that hosts the AI chatbot* — written into whichever slot `#slot-select` currently points at (§1.6). It describes where the conversation lives: it is where every appearance is searched for, the last-resort click target, and the vertical span of the copy-button search band.

The chat input box is **not** a drawn location. A fresh chat centres its box and an ongoing one docks it at the bottom, and after a new-chat click the layout is the *initial* one — so both layouts are captured once per **service** as appearances (`TemplateKind.CHATBOX_INITIAL` / `CHATBOX_ONGOING`, §1.3's APPEARANCE block) and found *inside* the drawn chat region at click time. `_chatbox_region` captures the live chat region once and hunts both in that one frame (ongoing first: mid-session it is the common case, and the search stops at the first hit), returning the match's absolute rectangle. That inversion is the point of the whole model: the pixels never move, so a browser the user resized or dragged to the other monitor costs one redrawn box instead of six recaptures — and the second slot pointed at the same service costs *nothing*.

`_find(kind, slot=None, scene=None)` is the one primitive underneath: capture the slot's chat region (or reuse a frame the caller already took), search the ACTIVE service profile's template for `kind` at that kind's own `max_diff`, and return the absolute rectangle — or `None` for every way it can come up empty (no region drawn, nothing captured for that kind, the capture failed, or it simply is not on screen).

After **every outbound clipboard copy** — bootstrap, results, user answers, revert notices, re-copies — MainScreen clicks the centre of whichever chat box was found, falling back to the chat region itself when neither is (mid-transition, a dialog over it, or nothing captured yet): clicking the window is recoverable, not clicking at all means the paste never lands. `_click_after_response` returns `bool` — True only when a target was known AND the click landed. Nothing drawn means no click at all. Every one of those reads comes from the **live** slot, so mid-delegation the click goes into the sub-agent's window.

Only when that click landed does `copy_outbound` go one step further: after a 0.15 s settle it sends a synthetic Ctrl+V itself (`screen.focus.send_paste`), dropping the outbound payload straight into the focused input. If no region is drawn — or the click did not land — the paste is never attempted: focus could be on any window, and pasting into an unknown app is the one unforgivable failure mode here, so it stays click-only in that case exactly as before.

- **Drawing**: each button spawns the tkinter overlay as a *child process* (`agentclip --pick-region`, hidden flag) — tkinter cannot share the Textual process (both want an event loop; tkinter wants the main thread). A `_picker_open` flag on MainScreen refuses (with a toast) any picker button press — chat, click, or busy — while an overlay is already up, so only one overlay is ever on screen; an exclusive worker group alone cannot guarantee this, because cancelling the worker does not kill the blocking child overlay process. The overlay is a translucent topmost fullscreen window spanning the whole virtual desktop (multi-monitor, Windows metrics); drag draws a rectangle, `Esc` cancels, a sub-8-px drag is treated as a stray click and ignored. The child prints `left top width height` on stdout; cancel prints nothing. Each caller passes its own prompt ("the window that hosts the AI chatbot" / "the spot to click after each response").
- **Clicking**: `screen.focus.click_region` — Windows-only `SetCursorPos` + `SendInput` via ctypes (stdlib, no new dependency). Both processes force DPI awareness first so overlay coordinates and click coordinates share the physical-pixel space. On non-Windows the click returns False and the user is told once (not per copy) that focus clicks are unsupported.
- **Scope**: the drawn regions are app-run-scoped and live in a `SlotCalibration` on MainScreen (§1.6), show in the sidebar's `#side-region` label, and can be redrawn mid-session (window moved). They describe where the service's windows are, not what one conversation said, so they **survive** `/new` / the summary's *new session* for both slots — a new conversation in the same windows needs no re-drawing (and a sub-agent slot calibrated mid-session actually enables `delegate` on the next `/new`, which is the advertised workflow). Only the pointers (`_calibrating`, `_live`) go home to MASTER, and the detector worker restarts against the surviving live-slot regions. Captured *appearances* are the opposite: they belong to the service, are shared by both slots, and **are** persisted (`screen.profile_store`, one folder of PNGs per service under `config.default_profile_dir()`), so they come back on the next run.
- **Layering**: all of it lives in `agentclip.screen` (region/overlay/picker/focus), an OS side-effect leaf like `clip` that only `tui`/`cli` may import (enforced in test_layering). The controller never knows the feature exists — the click rides inside the view's `copy_outbound`.

### 3.4b The copy button and auto-copy-click

Most chat sites need a click to get a response onto the clipboard at all — there is no keyboard shortcut, only a small icon under each response. This block automates that click once the busy detector (§1.3's REASONING block) says a response has finished, so the user never has to alt-tab and click it themselves.

- **Capturing**: "Capture copy button..." (`#set-copy-btn`) spawns the same draw-a-box overlay as the other regions, prompting (from `TemplateKind.COPY.prompt`) for a **tight** box around **one** copy-button icon — "pick the one under the last response, while the page is idle." Only the *pixels* are kept: they go into the active **service's** profile (`ServiceProfile.put`, which anchors them for search) and are written to disk (`profile_store.save_template`), so the same capture serves both slots and every later run. The drawn rectangle is not stored at all — the icon is found where it actually is, every time. A capture failure is reported and nothing is filed; a *save* failure is reported but the in-memory template is kept (it works for this run, and losing the capture as well would be strictly worse). `#side-copy` shows `"24×24 · captured"`, or `"not captured"`.
- **Arming and firing**: each poller tick takes one capture of the live slot's chat region and posts up to three messages in a **fixed order** — `BusyProbed` → `IdleProbed` → `StaleProbed` (`tui/messages.py`) — skipping whichever tracker it was not built with. `_start_detector_worker` (re)builds the whole set — busy/idle presence trackers if the active service has those appearances, the stale tracker always, nothing at all if no chat region is drawn — whenever an appearance is captured, a region is drawn, or the live slot moves; `_active_detectors` records which of them will post, in that fixed order. Each handler — `on_busy_probed` / `on_idle_probed` / `on_stale_probed` — records its detector's verdict and calls `_evaluate_finish` only when `_finish_tick_closed_by` says its message is the tick's **last**, which is simply `_active_detectors[-1]`: this is what lets `_evaluate_finish` fold the combined verdict exactly once per tick no matter which subset is running, instead of acting on a half-reported tick. `_evaluate_finish` folds every detector that has ever reported (`_seen`) into one verdict: **any** `False` (generating) arms the trigger and resets the streak; only once **every** live verdict reads `True` (finished) does the streak advance, and reaching **2** consecutive all-`True` ticks fires `_auto_copy_flow` and disarms — so with several detectors calibrated, that agreement is the whole point of having more than one. A capture error (`None`) breaks the streak but leaves the arm alone — a single bad frame must not cancel an in-flight finish. Evaluation is suspended for the flow's entire run (`_flow_running`), and **every** live tracker is reset in the flow wrapper's `finally` (`_run_auto_copy_flow`): the flow clicks, scrolls and hover-scans the very window all three detectors watch, so without the suspension and the reset it would read its own mouse work as a fresh generation and re-arm/re-fire itself forever.
- **The flow** (`_auto_copy_flow`, all OS calls off the event loop via `asyncio.to_thread`):
  0. **Nothing to do without a drawn chat region** (or without a captured icon): the search happens *inside* the window the user drew, so with no window there is nowhere to look and the flow returns having clicked and scrolled nothing.
  1. Focus the browser exactly like `copy_outbound` does after every copy — `_click_after_response` (the chat box found in the region wins, the region itself is the fallback) — then a 0.15 s settle.
  2. Scroll the transcript to the bottom fast: `scroll_region(chat_region, -40)`. A 0.4 s pause lets the page render after the flick.
  3. Capture the chat region and hand it to `agentclip.screen.template.find_lowest_in_region(template, scene, max_diff=TemplateKind.COPY.max_diff)`, which returns the bottom-most (i.e. newest) verified match anywhere in it — the icon appears once per response, so multiple matches are normal and only the lowest one matters. There is no band and no width constraint, so the old "search failed" branch is gone with them; a capture failure is reported (`#side-copy` reads `capture failed`) and the flow aborts.
  4. No match in that static frame: the chat may only paint the icon under the pointer (Claude does), so `#side-copy` reads `hover-scanning` and the flow walks the real cursor up the chat region (`screen.hover.hover_scan_points`), re-capturing the region and re-searching after each stop, taking the first frame the icon appears in. Still nothing: toast "copy button not found on screen" (warning) and stop — nothing is clicked. A match: a **verified, retried click** (`_verified_copy_click`) at `match_rect(chat_region, template, match)` — sometimes the cursor lands on the right spot but the hover-rendered button hasn't quite registered the click, so a single unverified click is not trusted. The provider's clipboard text is read once as a baseline before the first attempt; up to **three** attempts fire, each at a small offset from the matched rect's position that stays well inside the ~24 px icon — `(0, 0)`, then `(-3, -3)`, then `(+3, +3)` — each attempt doing `click_region(rect, settle_s=0.05)` (the 50 ms hover settle, same reasoning as the copy-button hover elsewhere) followed by up to six clipboard reads 0.2 s apart, stopping as soon as the text differs from the baseline. If the baseline read itself is unavailable (`ClipboardUnavailable`), the flow falls back to one unverified click instead of retrying blind — there is no signal to retry against.
  6. On a verified (or unverifiable-fallback) click: toast the diff, and snap focus back to AgentClip — a 0.15 s beat (so the browser registers the click), then `focus_window(_own_window)`. `_own_window` is the foreground window handle recorded whenever the user is provably interacting with AgentClip — at mount and on every composer send (`_remember_own_window`) — and is deliberately **not** session-scoped: the terminal outlives `/new`. `focus_window` (screen.focus) taps ALT through SendInput first, the documented input-recency loophole without which Windows refuses `SetForegroundWindow` from a background process. On exhausted retries (the clipboard never changed across all three attempts): toast a warning ("copy click did not take — click the response's copy button yourself") and deliberately **do not** snap focus back — the browser stays focused so the user can click it themselves.
- Every branch also repaints `#side-copy` with the captured size plus a short ASCII status (`captured` / `hover-scanning` / `clicked (diff 0.03)` / `click did not take` / `not found` / `capture failed`).
- **The paste flash and auto-paste**: automation covers browser→AgentClip; AgentClip→browser used to always end in a human Ctrl+V, and now only sometimes does. Right after its focus click, `copy_outbound` checks whether that click actually landed (`_click_after_response` now returns `bool`): if it did, a 0.15 s settle and then a synthetic Ctrl+V (`screen.focus.send_paste`) drops the payload straight into the focused input, and the sidebar banner reads `>>> PRESS ENTER <<<` (`Sidebar.ENTER_FLASH_TEXT`) — the human's only job left is the send keystroke. If the click did not land, or no region was drawn at all, no paste is attempted (pasting into an unknown window is the one unforgivable failure mode) and the banner falls back to its original `>>> PRESS CTRL+V <<<` wording (`PASTE_FLASH_TEXT`). Either way it is the same obnoxious banner at the very top of the sidebar (`#side-paste-flash`, bold, red/yellow, blinking at 0.4 s via a Sidebar-owned timer toggling the `flash-alt` class — pure presentation, so the dumb widget may own it), just with different text; `Sidebar.show_paste_flash(text=...)` takes the copy to show. It hides when the moment has provably passed: a `MATCH` busy probe (the model is chewing — the paste/send landed), a new `ClipboardCaptured` (the conversation moved on without it), or `clear_transcript()`. `Sidebar.show_paste_flash`/`hide_paste_flash` are the only entry points; display on/off (and, now, which text) is the tested contract.
- **Scope and layering**: the captured icon lives in the service profile — shared by both slots, persisted to disk, so it outlives `/new` *and* the process; only the trigger state (`_copy_armed`, `_copy_changed_streak`) is reset by `clear_transcript()`. Nothing is persisted to the config file. The flow itself lives entirely on MainScreen, like the focus click — the controller never knows the feature exists.

### 3.4c A sub-agent run, end to end

When the model calls `delegate`, the engine parks the turn in `AWAITING_SUBAGENT` and hands the controller a `Delegate(task, context)` step. What follows is a *nested session*, not a parallel one: the master's flow coroutine is blocked inside the delegate call for the whole run, which is exactly what lets the single clipboard watcher, the single approval gate, the single `ask_user` future and the single focused transcript be **retargeted** rather than duplicated. At most one chat is live at any instant.

1. **Ask first.** `view.delegation_available()` is checked before anything is built. False ⇒ the model gets `status=error` naming the missing calibrations (`view.delegation_missing()`), the master's turn continues, and no tab is opened. The controller never learns what a "new-chat button" is — the gaps cross the port as data.
2. **Save the master.** The whole per-session context (engine, chat name, preset, stats, glyph strip, last outbound, YOLO mirror) is snapshotted into a local and restored in a `finally`. YOLO deliberately does **not** inherit: `ApprovalPolicy` is per-engine, so a sub-agent starts from the configured default.
3. **Build a sub-agent.** `EngineRequest(role="subagent", allow_delegate=False, parent_chat_name=…)` → its own Engine, its own chat name, its own `SessionStore` (the `session` event records the parent, so the audit trail joins up), the sub-agent bootstrap variant, and a catalog with no `delegate` in it — nesting is excluded by construction, not by a special case.
4. **Open its tab and compose.** `open_session_view(ref)` mounts and focuses the pane; the task (plus `context` under its documented heading) is composed into a bootstrap. A `BudgetExceeded` here is an error result to the master, never a crash.
5. **Open its chat — before any paste.** `start_chat(ref)` searches the SUB-AGENT slot's chat region for the service's captured new-chat button, clicks the match, and only then retargets the automation. Three refusals, three different stories: nothing captured / no window drawn (`NOT_CALIBRATED`), the button is not on screen right now (`MISMATCH` — nothing is clicked, because clicking blind in a browser window is the one thing worse than not clicking), and the OS swallowing the input (`NOT_CLICKED`). **False aborts the delegation with zero paste calls**, because a sub-agent's bootstrap in the master's chat is unrecoverable. This is the single most damaging failure mode in the feature and has its own tests at both layers.
6. **Run the ordinary loop.** Ingest → review → gate → execute, against the sub-agent's engine, on the sub-agent's tab, pasting into the sub-agent's window. Replies are routed by chat name (`peek_chat_name`, a cheap scan of the last sentinel line) **before** the busy check — a sub-agent reply reaching the master's depth-1 queue would never be looked at, since the master is busy for the whole run. A master-chat reply arriving mid-run is dropped with an explanation, never queued: the master's next payload is composed fresh afterwards, so it is stale by definition.
7. **Hand the result back.** `task_done`'s `result` becomes the `delegate` call's result body, verbatim (falling back to `summary`, then to a placeholder — the delegating agent's result body is never empty). The tab is annotated and ticked, `end_chat` returns the automation to the master window, the master's context is restored, its tab is refocused, and the turn resumes at the call *after* `delegate`.

**Waiting and stopping.** There is no wall-clock timeout — the transport is a human alt-tabbing between two browser windows and a bounded sub-task can honestly take twenty minutes. The composer therefore stays enabled for the whole sub-run (its border reads *"Sub-agent running · /abort ends it and tells the model"*) even though the master's flow is busy, because `/abort` is typed there. Two escape hatches, deliberately different:

- **`ctrl+x`** cancels the tool calls running *right now*, in whichever chat is live. The turn still finishes and reports (the killed call plus the skipped ones) into that chat. A delegation survives it.
- **`/abort`** ends the whole run. The master gets `status=error, body="the user aborted the sub-agent run…"`. Where it lands depends on where the run is parked, and all three cases converge on the same `finally`: waiting for a reply ⇒ the reply future raises; at an approval gate ⇒ the gate is rejected (which aborts that turn) and a latched flag ends the run at the next reply park; executing tool calls ⇒ `request_cancel()` on the sub-agent's engine unblocks the worker, that turn ends normally, and the latch ends the run when the loop comes back for a reply. A sub-agent's `ask_user` is **not** abortable this way: while the composer is in answer mode its text is the answer, verbatim (§3.3a's precedence rule), so `/abort` typed there is an answer like any other.

Every failure path — uncalibrated, unverified click, abort, budget, or an exception nobody predicted — comes back to the model as an `error` result on the `delegate` call, and the `finally` always restores the master, drops the live slot back to the master's window and refocuses the master's tab.

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
- **`run_command` gate**: body is the command line in a bordered `Static` plus the cwd and the note `not on allowlist`; hint line adds *"edit .agentclip.toml's [approval] command_allowlist"* (the allowlist itself has no form yet - F2 only edits services, §1.4).

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

**Calibration** (palette `calibrate paste budget`, or from the service editor): copies a numbered marker payload sized to the preset max; user pastes it; model reports the last marker seen; user copies that reply; TUI parses it and toasts a suggested budget with one-key accept (`y`).

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
| `F2` | ServiceEditorScreen | global (App) |
| `F3` | show/hide the settings sidebar | MainScreen (priority: works while the composer has focus) |
| `F4` | SettingsScreen (preferences) | global (App) |
| `F6` | show the next transcript tab — browsing only, it never moves where output lands (§1.6) | MainScreen (priority; `show=False`, only meaningful once a sub-agent tab exists) |
| `enter` | send the composer (task at start, answer, follow-up) | composer focused |
| `ctrl+s` / `ctrl+enter` | send the composer without focusing it | MainScreen (priority) |
| `ctrl+x` | cancel the tool calls running now (`Engine.request_cancel`) — in whichever chat is live; the turn still reports back. `/abort` is the one that ends a delegation (§3.4c) | MainScreen (priority), while the RunningBar is up (`executing`) |
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