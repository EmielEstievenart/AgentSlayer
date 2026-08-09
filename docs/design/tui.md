# AgentClip TUI Design (Textual 8.2.x)

Prime directive honored throughout: the steady-state loop costs the user **one keypress in the terminal** (`y`) — everything else (ingest, execute reads, compose results, copy to clipboard, bell) is automatic. All key choices below assume no text input is focused on the main screen during the agent loop, so bare letters are safe screen bindings; the moment a text widget gains focus (ask_user answer, reject reason, composer), Textual's focus routing suppresses them automatically.

---

## 1. Screen map

### 1.1 App and screen inventory

```
AgentClipApp(App[None])            # CSS embedded in App.CSS (avoids PyInstaller --add-data)
├── MainScreen(Screen)             # default, always installed; also *starts* sessions (§1.3)
├── ServiceEditorScreen(ModalScreen[ServiceEdits | None])   # F2 (§1.4)
├── SettingsScreen(ModalScreen[str | None])    # F4: appearance/theme picker
├── SummaryScreen(ModalScreen[SummaryAction])
├── ConfirmScreen(ModalScreen[bool])   # generic y/n confirm (undo, end-session, quit-mid-turn, discard-edit)
├── HelpScreen(ModalScreen[None])      # F1 key/flow cheatsheet
└── TextEntryScreen(ModalScreen[str | None])   # manual paste fallback for force-ingest
```

`HelpScreen` is a cheatsheet, and a cheatsheet about a moving UI rots silently — the version that shipped through three waves still advertised *"F2 settings (lands in M3)"* after F2 had become the whole service-profile editor, still described a tab per delegation after tabs became browser windows, and carried a **fifth** hand-written copy of the chat-command list. So its command section is not prose at all: `screens/help.py` renders one row per `app.commands.COMMANDS` entry (§3.3a) and a test pins the two together, exactly as one pins the controller's dispatch table. The rest is prose and stays prose — but it is prose about the *shipped* model: two rows of window tabs, one persistent sub-agent transcript with dividers, `▶`/`✓`/`✗`, F2 profiles, F3 sidebar, F4 themes, and what the slash popup does to Enter.

### 1.2 MainScreen widget tree

```
MainScreen
├── Horizontal                       id=body              # 1fr height
│   ├── Vertical                     id=main-col          # width 1fr: the chat column
│   │   ├── WindowTabs(Vertical)     id=chats             # height 2: one tab per browser WINDOW (§1.6)
│   │   │   ├── Horizontal           id=win-row-master    # row 1: the master windows
│   │   │   │   └── WindowTab(Static) id=win-m1           # "MASTER · chatgpt-attach"
│   │   │   └── Horizontal           id=win-row-sub       # row 2: the SELECTED master's sub-agent windows
│   │   │       └── WindowTab(Static) id=win-m1-s1        # "SUB-AGENT · claude" / "▶ …" / "✓ …" / "✗ …"
│   │   ├── Vertical                 id=chat-panels       # 1fr; one panel per window, exactly one displayed
│   │   │   ├── TranscriptPanel(VerticalScroll)  id=transcript    # the master window's (pre-tabs id)
│   │   │   │   ├── Markdown                     .ev-user         # user task / follow-ups
│   │   │   │   ├── Markdown                     .ev-prose        # LLM prose between blocks
│   │   │   │   ├── Vertical                     .ev-call         # one per tool call
│   │   │   │   │   ├── Static                                    # "▶ edit_file src/utils.py · 1 hunk · ✓ ok"
│   │   │   │   │   └── Collapsible(collapsed=True)                # long payloads only
│   │   │   │   │       └── Static                                 # Rich Syntax / plain text
│   │   │   │   ├── Static                       .ev-note / .ev-error / .ev-approval
│   │   │   │   └── ...
│   │   │   └── TranscriptPanel      id=tr-m1-s1          # the sub-agent window's: divider + run, divider + run…
│   │   ├── ActionPanel(Vertical)    id=action            # display:none when idle; max-height:60%
│   │   │   ├── Static               id=action-title      # "APPROVE · call 2/5 · edit_file src/utils.py"
│   │   │   ├── Static               id=queue-strip       # "✓ read_file  ▶ edit_file  • run_command  • task_done"
│   │   │   ├── VerticalScroll       id=action-body       # diff / question / chunk wizard; focused on show
│   │   │   │   └── Static                                # Rich renderable
│   │   │   └── Horizontal           id=action-footer
│   │   │       ├── Static           id=action-hints      # "[y] approve  [n] reject  [a] auto-accept edits"
│   │   │       └── Input            id=reject-reason     # hidden until 'n'
│   │   ├── RunningBar(Static)       id=running           # spinner + "(ctrl+x to cancel)" while a turn executes
│   │   ├── CommandPopup(Static)     id=cmd-popup         # display:none unless a /command is being typed (§3.3a)
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
├── Static                 id=side-profile-note     # read-only: "appearance: 4/6 captured · F2 for captures + detection" (Sidebar.profile_summary)
├── Button                 id=edit-services-btn     # "Edit services..." → App.action_settings() (F2)
├── Static "CHAT WINDOW"                            # the SELECTED window tab's (§1.6)
├── Button                 id=set-region-btn        # "Set chat region..." → draw-a-box overlay (§3.4a)
├── Static                 id=side-region           # "not set - ..." / "812×540 at (1050, 340) · chatbot window"
├── Static                 id=side-slot-note        # "the main agent's chat window" / "delegation ON" / "delegation off · need: copy button, new-chat button"
├── Static                 id=side-detection-title  # "DETECTION · MASTER" / "DETECTION · SUB-AGENT" — the LIVE window, not the selected tab
├── Static                 id=side-tpl-busy         # "busy · no verdict yet" / "busy · ● GENERATING · match (diff 1.2%)" / "busy · ticked but not captured - F2"
├── Static                 id=side-tpl-idle         # "idle · no verdict yet" / "idle · ○ response ready · changed (diff 34.0%)"
├── Static                 id=side-stale            # readout only, no button: the drawn region IS this detector
├── Static                 id=side-tpl-copy         # "copy · no click yet" / "copy · 24×24 · clicked (diff 0.03)"
├── Button                 id=newchat-btn           # "New browser chat": click it now, in the SELECTED tab's window
└── Static .side-hint                               # "F3 hides this column · F2 settings · F1 help"
(first child, above PROJECT: Static id=side-paste-flash — the blinking ">>> PRESS CTRL+V <<<" / ">>> PRESS ENTER <<<" banner, hidden until an outbound copy; §3.4b)
```

The column is **live status plus the two things you steer with**, and holds nothing that has to be *configured*. Capturing what a service looks like, and ticking which finish signals it may run, both live in the service editor (§1.4): they are per-service settings and belong next to the service's other settings — and six capture buttons with six status lines had grown into two thirds of a 32-cell column that is meant to be glanced at, not filled in. What is left is:

- **SERVICE**, which is per *window tab* (§1.6): the picker, its caption and the appearance summary all describe whichever tab is selected, and `Sidebar.show_service(key)` writes that tab's key in when the selection moves — deliberately *without* posting `ServiceChanged`, because the picker is catching up with a choice already made rather than announcing a new one. There is no AGENT SLOT picker any more; the tab bar replaced it.
- **CHAT WINDOW**, the selected tab's window: `#set-region-btn` writes into it, and `Sidebar.show_slot(cal, note)` repaints the block — the drawn rectangle and the readiness note — in one go. Nothing else: the detection lines below are **not** the selected tab's (see §3.4e).
- **DETECTION**, four read-only lines the running automation writes into: `Sidebar.update_template(kind, text)` for the busy/idle probes and the auto-copy flow's last click attempt, `Sidebar.update_stale(text)` for the staleness verdict. Only three `TemplateKind` values have anything to say at runtime (`DETECTOR_LABEL`); the two chat boxes and the new-chat button are found on demand and report by toast, so they have no line and `update_template` ignores them. Each line is *named as it is painted* (`busy · …`, `idle · …`, `copy · …`) — four unlabelled verdicts stacked on each other are not readable — and pinned to `height: 2` (class `side-probe`) so a long verdict cannot walk the button below it up and down the column. **The block is the LIVE window's, not the selected tab's**, its heading says which (`Sidebar.show_detection_window` → *DETECTION · SUB-AGENT*), and only the detector machinery writes into it — the ownership rule is §3.4e.
- One read-only **appearance summary** under the service picker, `#side-profile-note` — *"appearance: 4/6 captured · F2 for captures + detection"* — so *is this service usable at all?* is answerable at a glance without opening the editor. It names F2 for both halves because both live there: the captures and the checklist that decides which of them the poller may use.

`Sidebar.show_profile(profile)` repaints that summary and nothing else. It is called when the service picker moves (`Sidebar.ServiceChanged` — a domain message, because a different service is a different set of appearances, not a display change: MainScreen files the key against the selected window, relabels its tab, reloads the profile, repaints and recomputes the readiness note), when the window tab bar moves, and when `MainScreen.update_config` adopts an editor visit. It deliberately does **not** reset the probe lines any more: those belong to the service the poller is *running*, which is the live window's, so wiping them on a tab click threw away the readout of a sub-agent run the user had switched over to read. Resetting them is the poller's own job (§3.4e). The detector poller is the one thing `ServiceChanged` restarts **conditionally** — only when the tab that changed is the one the automation is driving (`_calibrating is _live`), because re-pointing the sub-agent window mid-session is the normal way to set delegation up and rebuilding the master's poller there would throw away its in-flight streaks for a window nothing is watching. Same rule as a mid-session region redraw (§3.4a).

**The finish detectors are presence questions, not fixed rectangles.** "Is the model still generating?" is answered by asking *is the stop button on screen anywhere in the chat?*, not *does this rectangle still look like it did?* — so the user captures what the busy indicator **looks like** once per service (the editor's "Capture busy indicator...", `TemplateKind.BUSY`) and `agentclip.screen.presence.PresenceTracker` searches for it inside the drawn chat region on every poll. The prompt warns against boxing an animated spinner, and that warning lives on the enum rather than in the TUI: an animation is a different picture every frame, so it can never be matched back, and that is a fact about the appearance, not about the caller.

The de-bounce is deliberately **asymmetric**. The two mistakes are not equally bad: believing the model is still generating when it has finished costs one poll interval, while believing it has finished when it hasn't harvests a truncated answer and feeds it to the engine as the whole response. So "still generating" is adopted the instant one frame supports it, and "finished" must survive `required_ticks` consecutive frames. "Capture idle indicator..." (`TemplateKind.IDLE`) is the mirror — something on screen only while the chat is **idle**, usually the send button — with `found_is_busy=False`, so `#side-tpl-idle` reads the inverse polarity. Both repaint via `Sidebar.update_template(kind, …)` with an unmistakable state string (`● GENERATING · match (diff 1.2%)` / `○ response ready · changed (diff 34.0%)` / `✗ capture failed`).

The third detector has **no button and no capture at all**: the drawn chat region reading unchanged frame to frame *is* a finished response, whatever a particular service's pixel cues do (`agentclip.screen.stale.StaleTracker`, comparing each poll to the previous one rather than to a fixed anchor). Once it has read unchanged (`STALE_MAX_DIFF = 0.002` over up to `STALE_MAX_SAMPLES = 16384` samples) for `required_ticks` consecutive polls it reports STALE. `#side-stale` (`Sidebar.update_stale`) is a readout only: `○ response ready · stale (still ×4)` / `● GENERATING · changing (diff 0.08% · still ×2)` / `✗ capture failed`. Because it rides on the box every window draws anyway, **every window has a working finish detector from its first drag** — busy and idle only reinforce it.

**Which of the three run is the service's decision** (`ServicePreset.finish_signals`, edited in the service editor's DETECTION checklist, §1.4): a checklist over `config.FINISH_SIGNALS = ("busy", "idle", "stale")` — the same canonical order the poller builds and posts in — defaulting to `("stale",)`, the one every drawn window can already do. A ticked busy/idle entry *additionally* needs its appearance captured; a ticked `stale` needs nothing. Staleness is therefore opt-**out**, not unconditional: a chat whose region never settles (an animated avatar, a ticking clock) can untick it and lean on its icons instead — and `#side-stale` then reads `stillness not watched for this service - F2` (`Sidebar.STALE_UNTICKED`) rather than sitting blank while the icons do the work. An empty checklist is legal and means no finish detection at all — no poller is started and `#side-stale` reads `finish detection off - F2 to configure` (`Sidebar.STALE_OFF`), because "the auto-copy will never fire" must be visible somewhere other than in a copy that never arrives, and the door to turning it back on is the only useful thing to say next to it. A ticked busy/idle entry *without* its appearance runs nothing either, so its own line says `ticked but not captured - F2` (`Sidebar.PROBE_UNCAPTURED`) instead of resting forever at "no verdict yet", which is indistinguishable from a detector that simply never finds anything. Unknown entries are dropped (with a warning) when the config loads, so the poller can never be asked for a detector that does not exist.

All of them share one knob and one tick. `required_ticks` comes from the **live window's** service preset's `stable_seconds` (§1.4) via `max(1, round(stable_seconds / _BUSY_POLL_S))` for *every* tracker, because "how long is long enough" is a property of the streaming cadence of the service in the chat being driven — mid-delegation that is the sub-agent tab's, not the master's and not whichever tab is on screen. And each tick takes **one** capture of the chat region and hands it to all of them: cheaper, but mainly it makes them judge the same instant instead of three moments of a moving screen, and it makes a failed capture reach all of them as the same ERROR (`PresenceTracker.observe(None)` / `StaleTracker.observe(None)` — streaks intact, so one dropped frame cannot restart the stillness clock on an in-flight finish). Their verdicts drive the auto-copy-click trigger described in §3.4b and nothing else. The drawn region survives `/new` (only the pointers reset to MASTER, and the poller restarts against the surviving window); the captured appearances survive the process itself.

The `Select` is the only place a service is chosen, and it chooses one **per window tab**: the master tab's value flows into `SessionSpec.service` and from there into the engine and the status bar's service segment, the sub-agent tab's into `SessionSpec.subagent_service` and from there into every delegated run's engine (§1.6). It is **disabled while a session is active** — on *both* tabs, because both were baked in at bootstrap (the master's paste budget into its engine, the sub-agent tab's into the `can_delegate` answer that decided the catalog) — and re-enabled whenever `prompt_new_session` is waiting. After the services table itself changes (service editor), the sidebar rebuilds its options via `Sidebar.refresh_services(config)`, preserving the current selection when that preset survived; `MainScreen.update_config` does the same repair for the tab that is *not* selected, which has no widget to catch a deleted preset.

Rejected alternatives: a one-line `Input` for the task — coding tasks are multi-line (pasted tracebacks), so `ChatComposer` (Enter sends, `ctrl+j` newline, paste keeps newlines) is the right widget. And the original launch modal — it forced a decision (which service?) before the user had seen anything, and duplicated a text box the main screen already owns.

### 1.4 ServiceEditorScreen (shipped M3; scope narrowed from the original ConfigScreen sketch)

The original ConfigScreen sketch covered the whole config surface (allowlist, poll interval, bell/toast switches, backup retention); what actually shipped is narrower and sharper, and is now the **whole per-service profile editor**: a `ModalScreen["ServiceEdits | None"]` (`src/agentclip/tui/screens/service_editor.py`) holding everything that is a property of *one chat service* — its name, the two size numbers (`max_paste_chars`, `total_context_chars`), the stale-detector's stillness window (`stable_seconds`, §3.4b), what it **looks like** (the six captured appearances, §3.4d), and which **finish signals** its poller may run (`finish_signals` + `hover_scan`, §1.3/§3.4b). The rest of the config surface is still hand-edited TOML; nothing else needed a form badly enough to justify one yet.

Opened via `F2` (`AgentClipApp.action_settings`) or the sidebar's "Edit services..." button. Both routes are synchronous entry points into an async flow (`push_screen_wait` needs a worker), so `action_settings` is a plain `def` that hands off to `self.run_worker(self._open_service_editor(), group="settings", exclusive=True)` — the same shape as `AgentClipApp._confirm_quit`. Two guards on the way in: if the editor is already the active screen, F2/the button are no-ops — and if `MainScreen.picker_open` says the chat-region overlay is up, F2 is **refused with a toast**, because the editor has capture overlays of its own behind a separate flag and two fullscreen child processes cannot share a desktop (cancelling a worker cannot kill either of them). The visit also suspends the finish detectors for its whole duration (§3.4e).

```
ServiceEditorScreen(ModalScreen)  .modal-box, id=service-editor-box (width 112, max-height 95%)
├── Static "SERVICE EDITOR"                     .title
├── Horizontal                    id=svc-body   # each column height:auto (Vertical defaults to 1fr, which
│                                               # stretched the box to the max-height cap and pushed the hint off)
│   ├── Vertical                  id=svc-list-col   (width 32)
│   │   ├── Static "Services"                   .side-title
│   │   ├── Select[str]           id=svc-select      # "key (builtin|custom)" rows + "+ Add new service..."
│   │   ├── Static "DETECTION · finished when"   .side-title
│   │   ├── Checkbox              id=svc-signal-busy   # "reasoning icon disappears"  → finish_signals "busy"
│   │   ├── Checkbox              id=svc-signal-idle   # "send icon appears"          → finish_signals "idle"
│   │   ├── Checkbox              id=svc-signal-stale  # "screen stops changing"      → finish_signals "stale"
│   │   ├── Checkbox              id=svc-hover-scan    # "hover-scan for copy icon"   → ServicePreset.hover_scan
│   │   └── Static                id=svc-signal-warning # "busy indicator: ticked but not captured — it will be skipped"
│   ├── Vertical                  id=svc-form-col   (1fr)
│   │   ├── Input                 id=svc-key         # disabled unless in "add new" mode
│   │   ├── Input                 id=svc-label
│   │   ├── Input                 id=svc-max         # max input size (chars per paste)
│   │   ├── Input                 id=svc-total       # total context size (chars)
│   │   ├── Input                 id=svc-stable      # stale detector: seconds unchanged = finished (default 2.0, bounds 0.5-60)
│   │   ├── Static                id=svc-error       # inline validation message, styled $error
│   │   └── Horizontal            id=svc-actions
│   │       ├── Button "Add service"      id=svc-add-btn   # visible only in "add new" mode
│   │       ├── Button "Reset to default" id=svc-reset-btn # visible only for a built-in key
│   │       └── Button "Delete"           id=svc-delete-btn # visible only for a non-built-in key
│   └── Vertical                  id=svc-appearance-col (width 34)
│       ├── Static "APPEARANCE"                 .side-title
│       ├── Button                id=svc-capture-<kind>-btn  # .svc-capture-btn, one per TemplateKind, compact
│       ├── Static                id=svc-tpl-<kind>          # "not captured" / "24×24 · captured"
│       │   …six of those pairs, in TemplateKind declaration order…
│       ├── Static                id=svc-templates   # read-only: "appearance: 2/6 captured (busy indicator, copy button)"
│       └── Button "Forget appearance" id=svc-forget-templates-btn  # visible only when something is captured
└── Static "escape closes (applies valid edits) · ..."     .hint
```

**Model:** the screen works on an in-memory *working copy* of `config.services`. Selecting an existing preset and editing its name/sizes applies **live** — every keystroke revalidates the whole candidate (key + label + both sizes together, since "max ≤ total" is a cross-field rule) and, only while the candidate is fully valid, writes it straight into the working copy; an invalid candidate is never applied, it just leaves `#svc-error` showing why. Adding a new preset is the one *discrete* action instead of a continuous one: fill in a unique lowercase-hyphen key (validated against `^[a-z0-9]+(-[a-z0-9]+)*$`, immutable once created) plus the other fields, then press "Add service" (enabled only once the candidate validates) — creation is a one-time event best gated behind an explicit action rather than silently committing a half-typed key on every keystroke. The fourth field, `stable_seconds` (`#svc-stable`, "Stale after (seconds unchanged)"), folds into the same candidate and the same live-apply/discrete-add split, but validates on its own (`0.5`–`60`, the exact bounds `config.py`'s `_take_float` enforces on load, so a value the editor accepts is never silently replaced on next start) rather than cross-checked against the two sizes; unlike the blank size fields it is pre-filled with `DEFAULT_STABLE_SECONDS` (`2.0`) when adding a new service, since it has one sensible default most users never need to touch.

**Escape** (`action_close`, same worker hand-off shape as `action_settings` since it awaits a modal): if the currently displayed fields are valid, the screen dismisses with the working `services` dict if it differs from what it opened with, or `None` if nothing changed (so the caller has nothing to persist). If the currently displayed fields are invalid, nothing was ever written to the working copy (invalid values are never committed) — so there's no real edit to lose — but the visible text would still vanish, which is surprising, so escape instead asks via the shared `ConfirmScreen`: *"The current field values are invalid (\<reason\>) and were never applied. Close the service editor anyway?"* Denying returns focus to the editor; confirming closes as if the field had never been touched.

**APPEARANCE** (`#svc-appearance-col`): one "Capture \<thing\>..." button per `TemplateKind` in declaration order, each above its own status line, plus the `#svc-templates` summary and the "Forget appearance" escape hatch. The block is *generated* from the enum with the kind encoded in each button's id (`svc-capture-<kind>-btn`, class `svc-capture-btn`), so **one** `@on(Button.Pressed, ".svc-capture-btn")` handler serves all six — a seventh appearance is an enum member and nothing else. A press runs the same full-screen draw-a-box overlay the chat region uses (`screen.picker.pick_region` with `TemplateKind.prompt`, then `screen.capture.capture_region`, both imported *into* this module so tests monkeypatch them at `agentclip.tui.screens.service_editor`); the modal stays open and `_show_appearance` re-derives every readout from `profile_store.load_profile(profile_root, key)` when the worker returns.

Captures are the one thing here that does **not** wait for escape: `save_template` writes the PNG immediately, exactly as "Forget appearance" deletes immediately, because the editor holds no `ServiceProfile` of its own to hand back — the store *is* the working copy, and `ServiceEdits.profiles_changed` is what tells the caller its cache is stale. Consequently a save failure files nothing at all (unlike the old main-screen path, there is no in-memory template left to be useful) and is reported as an error toast. The box is still anchored first (`ServiceProfile.put` on a throwaway profile) so an unsearchable sliver is refused before anything reaches disk.

**One overlay at a time, and escape belongs to it.** Cancelling an exclusive worker cannot kill the blocking child overlay process it spawned, so `_capturing` is held for the whole method (pick → capture → anchor → save → repaint), and while it is held a second capture press *and* `action_close` are both refused with a toast — closing the editor out from under an in-flight capture would strand the worker that still has to write the PNG. Same shape as MainScreen's `_picker_open` guard on the chat region.

**DETECTION** (`#svc-signal-*`, `#svc-hover-scan`): the finish-signal checklist and the hover-scan opt-in, labelled in *user* terms — "reasoning icon disappears" / "send icon appears" / "screen stops changing" — because the TOML names (`busy`/`idle`/`stale`) describe how a detector works and these describe what the user will see, which is the only thing they can check against their own chat window. `Checkbox.Changed` folds **all four** boxes back into the preset at once (`normalize_finish_signals` over `config.FINISH_SIGNALS`, then `replace`) rather than toggling one entry: the checklist has one canonical order anyway, and reading the set makes the handler immune to the echo Textual fires when `_load_service` writes the values in — that echo writes the freshly loaded service's own values straight back, which is a no-op. A ticked `busy`/`idle` whose appearance is not captured runs *nothing* (§3.4d: the checklist and the profile are ANDed), which is otherwise invisible until an auto-copy never fires, so `#svc-signal-warning` says so inline — *"busy indicator: ticked but not captured — it will be skipped"* — and clears the moment the capture lands.

**Nothing to file it under yet.** While the `+ Add new service...` sentinel is selected and "Add service" has not been pressed, there is no key, so every capture button and every checkbox is `disabled` (disabled rather than hidden: the column must not reflow while the user fills the form in). They come alive on the `_refresh_select_options` that follows the add. Disabled, but **not blank**: the checkboxes show the `ServicePreset` dataclass defaults (`_NEW_PRESET_DEFAULTS` — *screen stops changing* ticked, hover off), which is exactly what "Add service" is about to create. An all-unticked form that silently produced a stale-ticked preset misrepresented the one setting this form is the only place to see.

Because the editor writes *and* deletes profiles out from under MainScreen's per-run cache, `MainScreen.update_config` clears that cache, repaints the sidebar summary and restarts the detector poller — which is what makes a busy/idle capture take effect without a restart.

**Delete vs. reset:** the twelve built-in keys (`config.BUILTIN_SERVICE_KEYS`) can be edited and reset but never deleted — `#svc-delete-btn` only appears for a non-built-in key. `#svc-reset-btn` only appears for a built-in key and restores its shipped values (`config.default_services()[key]`) regardless of whether it currently differs (idempotent no-op if it's already default).

**Wiring on close** (`AgentClipApp._open_service_editor`): a non-`None` result is persisted via `config.save_services(result, self._global_config_path)`, which writes a preset's whole `[services.<key>]` table only when it differs at all from its built-in default (an untouched or reset-to-default built-in is omitted entirely) — and, within a written table, adds `stable_seconds` only when *that field itself* still differs from the built-in's, so editing just the size fields on a preset left at the default `2.0` doesn't pin today's value into the file; a service that never customizes it keeps tracking future changes to the shipped default. That result is folded into a fresh `Config` via `dataclasses.replace(self.app_config, services=result)`, and propagated to `self.app_config`, `MainScreen.update_config` (which also updates the `SessionController`'s config so the *next* session and any `/new` see it), and `Sidebar.refresh_services`. Engine construction reads `Config` fresh per session start (`cli.make_engine_factory` takes a `get_config: Callable[[], Config]`, called on every `build()`, not once) — so a saved edit takes effect for the next session in this process without restarting the app, while a session already in flight keeps the `Config` snapshot its `Engine` was built from. Opening/using the editor mid-session never touches `awaiting_new_session`, so the sidebar's service `Select` stays exactly as locked/unlocked as it was before.

### 1.5 End-of-session summary (SummaryScreen)

Pushed on demand via `e` (end session); it is **not** auto-pushed on `task_done` — the user stays in the chat and may follow up (protocol.md §8). Contents: a `Static` rendering a Rich `Table` — turns, calls per tool, files created/modified (paths), commands run, total chars pasted both ways, and `sub-agent runs` when the session delegated at least once — plus the `task_done` summary text from the LLM as `Markdown`. Bindings on the modal: `u` undo entire session (turn-by-turn restore, with ConfirmScreen), `t` new session, `escape` back to main (transcript stays for review), `ctrl+q` quit.

### 1.6 Window tabs, agent slots and delegation

Delegation (protocol.md §3's `delegate` tool) gives the model a second chat window to run a bounded sub-task in. **A tab is one of those browser windows.** Not a transcript, not a session view: a window is open before any session exists, keeps its own service, its own drawn rectangle and its own accumulating transcript, and is still there after `/new`. Everything else in this section follows from taking that literally.

**The bar.** `WindowTabs` (`tui/widgets/window_tabs.py`) is two rows: row 1 the master windows, row 2 the sub-agent windows of whichever master is selected. Today that is exactly one of each — `m1` and `m1-s1`, tab ids `win-m1` / `win-m1-s1` — so it reads as two short title lines, and there are no add/remove controls. The ids, the `WindowSpec` tree, the `WindowSelected` message and the sub-row rebuild are shaped for N × N anyway, because they are the entire difference between one-of-each and many, and retrofitting them would mean re-teaching every caller what a tab is.

Each tab's label is *what the window is · what it runs on*, plus live state: `MASTER · chatgpt-attach`, `SUB-AGENT · claude` before anything has run there, `▶ SUB-AGENT · claude` while a delegated run is in flight, and otherwise the **last** run's outcome: `✓ …` when it handed a result back, `✗ …` when it ended without one (a refused chat, a bootstrap over budget, an abort, a crash). The last run, because the tab is a status light for the window and what a user wants at a glance is how the most recent attempt went; the earlier runs are still readable in the transcript below it. The service is on the tab because it is per window and the sidebar only ever shows the selected one's — otherwise *"which chat is the sub-agent going to open?"* would be a question you answer by clicking around.

Textual's `Tabs` is deliberately not used. Two rows would be two independent `Tabs.active` values fighting over one selection, and a row whose only tab is already active swallows a click without posting anything (`_activate_tab` assigns the same value, the reactive never fires) — precisely the 1 × 1 case here, where clicking either row would do nothing at all. Both rows also auto-activate their first tab on mount, so a naive handler would select whichever row mounted last. A strip that owns **one** selection across both rows is a dozen lines and has neither problem; it is also not focusable, so a tab click never steals the composer's focus.

**Selecting a tab is what the AGENT SLOT picker used to be**, plus the transcript. `_select_window` moves `_calibrating` (so "Set chat region..." and the service picker write into that window), repaints the whole sidebar column from that window's state, and displays its transcript panel. `F6` cycles the same funnel. What it pointedly does **not** touch is `_live`: looking at a window is not driving it, and a click here while a sub-agent is mid-run must not send the next paste into the master's chat.

**Slots — one drawn box per browser window.** Underneath the bar, the storage is unchanged: `_WINDOW_SLOTS` maps a window id to an `AgentSlot`, and that dict is the whole seam an N-window bar plugs into. A slot was six calibrations; it is now a single rectangle, because the two questions it conflated have different owners. *What does this thing look like?* (the stop button, the copy icon, both chat-box layouts, the new-chat control) is a property of the **service** — identical in every window it is ever opened in — so it lives in a `ServiceProfile` (`agentclip.screen.profile`): captured once, persisted as PNGs, reloaded next run, shared by every window pointed at that service. *Where should I look for it?* is genuinely per window, and is all `SlotCalibration` (`agentclip.screen.slot` — pure data, stdlib only, unit-tested without Textual) still holds: `chat_region`, one per `AgentSlot`.

The headline is the setup cost. **A sub-agent window costs exactly one drag** when it is on the same service as the master, because it inherits every capture already made; and a browser the user moved, resized or dragged to the other monitor costs one *re*-drag instead of six recaptures. Two independent pointers ride on top:

- `_calibrating` — the selected tab's slot: what the sidebar configures. Never locked by session state (the *service* picker is; the region button is not), because the user must be able to calibrate and watch the sub-agent window while the master chat is mid-turn. Both windows share the picker code, so the SUB-AGENT prompts are prefixed *"SUB-AGENT window · …"*.
- `_live` — which slot the automation drives right now (the focus click, the finish-detector poller, the auto-copy flow). Only `start_browser_chat` / `end_browser_chat` move it.

**A service per window.** Each tab carries its own key (`MainScreen._services`), so the conversation the user steers can run on a big-context chat while delegated sub-tasks go to something cheap and fast. Consequently *"which preset / which appearances / how long is stillness / may I hover-scan?"* always names a slot, and mixing the two up is the bug the split exists to prevent:

- `_live_preset()` / `_live_profile()` — the window the automation is driving. The detector poller, the auto-copy flow, `_chatbox_region`, and every `_find_all` without an explicit slot read these.
- `_preset_for(slot)` / `_profile_for(slot)` — a named window. `_click_profile_element(slot, kind)` and `start_browser_chat(slot)` use it, so a sub-agent's new-chat click is verified against the *sub-agent chat's* button even though `_live` has not moved yet.
- `_active_preset()` / `_active_profile()` — the selected tab, i.e. the sidebar's appearance summary and readiness note, and nothing the automation does.

Both keys leave the view exactly once, in `SessionSpec(task, service, subagent_service)` at bootstrap, because both pickers are locked for the session's life: the master's paste budget is baked into its Engine and the sub-agent tab's decided the delegate catalog. `SessionController` stores `subagent_service` and builds every sub-agent Engine from it (§3.4c). The starting values come from `[general] service` and `[general] subagent_service`, the latter blank-means-*the same as the master's* so the key is invisible to anyone running one service.

Readiness is therefore a function of the **pair** (window, that window's service profile), not a property of either half, so the rules are module-level functions taking `(cal, profile)`: `can_paste`, `can_finish`, `can_copy`, `can_delegate`, `missing`. `can_paste` and `can_finish` are the drawn window alone — the input box is found inside it, and a rectangle that stops changing *is* the staleness finish detector, so **one drag makes a window both pasteable and finishable**. `can_copy` adds `profile.has(COPY)`; `can_delegate` adds `profile.has(NEW_CHAT)` on top of all of it, and is asked against the **SUB tab's** profile: the buttons a run will click are the ones in the chat it is going to open, and the master tab having captured its own says nothing about them. It is strict on purpose: without all three pieces a sub-run would strand halfway. `missing()` returns the gaps in calibration order — `MISSING_CHAT_REGION`, `MISSING_COPY`, `MISSING_NEWCHAT` — which the sidebar shows in `#side-slot-note` and which the controller embeds in the error the *model* gets if it calls `delegate` against an uncalibrated host. Losing the window lists all three, which is honest rather than noisy: with nowhere to search, neither button can be found, and one drag closes all three gaps.

Because half the answer is the profile, the "sub-agent window ready" seam (`_after_calibration`) has to fire on **captures and service switches as well as region draws** — capturing a copy button, or re-pointing the sub-agent tab at a service that has none, flips delegation without any box being drawn. Since captures moved into the editor (§1.4) that half arrives through `MainScreen.update_config`, which drops the profile cache and re-runs `_after_calibration` for exactly this reason.

`start_browser_chat(slot)` is deliberately all-or-nothing: it *finds* the new-chat button inside that slot's chat region (`_click_profile_element`, against that window's service), clicks where it actually is, and **only then** retargets `_live`, resets the finish trigger and restarts the poller. A `False` return guarantees nothing was clicked and nothing was retargeted, which is what lets the controller abort a delegation before its first paste — a sub-agent's bootstrap pasted into the master's chat would corrupt that conversation irrecoverably. `end_browser_chat()` is the mirror and is unconditional: it runs in the controller's `finally`, so it must work even after the sub-run blew up.

**One transcript per window, appended.** Each window's `TranscriptPanel` is mounted once and kept (`#chat-panels` shows exactly one); the master's keeps the pre-tabs id `#transcript`, so every existing selector still resolves. A delegation does not mint a pane: `open_session_view` appends a divider — `── task: <short title> ──` — and the run's transcript follows it, so the sub-agent window reads as one scroll of everything that ever ran in it, in order, with the divider marking where each sub-task began. `finish_session_view(session_id, note, ok)` adds its note and re-badges the tab `✓`/`✗`; nothing is removed or disabled, because the panels are output-only and the composer always targets the controller's *active* session. `ok` is a **parameter**, not something the view can infer: every ending — delivered, refused, over budget, aborted, crashed — arrives through the controller's one `finally`, so a view left to guess printed *"the result above was handed back"* directly under the error explaining that nothing ran, and badged the tab `✓`. The controller decides what happened (`_FINISHED_NOTE` / `_FAILED_NOTE`); the view decides what a failure looks like.

The load-bearing distinction is **focused panel ≠ selected tab**. `MainScreen.transcript` resolves to `_focused_panel`, which only `focus_session_view` moves; clicking a tab (or `F6`) moves what the user *sees* and what the sidebar configures, and nothing else. Without that split, a user reading the master's tab mid-delegation would silently divert the sub-agent's output into it, which looks exactly like data loss.

`render_log` is still **per run**, not per window: one `## sub-agent: <title> (<chat_name>)` heading over five unrelated sub-tasks is a wall, so `MainScreen._sub_runs` remembers each run's start and end index in the sub-agent panel's (unpruned) `event_log` and `TranscriptPanel.render_events(start, end)` slices them back apart. `/new` (`_remove_session_views` + `clear_transcript`) empties both transcripts and forgets the runs, and **keeps both tabs, both drawn rectangles and both services**: the browser windows have not gone anywhere.

**Who is talking.** During a sub-run every piece of state the controller pushes describes the sub-agent, so three places say so: the watcher status segment is rebadged `◆ SUB-AGENT · …` in magenta (`.st-sub`, a colour used nowhere else), the approval drawer's title gains a `SUB-AGENT ‹task title› · ` prefix, and every bell/toast is prefixed `sub-agent: `. The `SessionView` snapshot carries `session_id` / `session_role` / `session_title` for exactly this (additive, master-shaped defaults).

**Known limitation.** The tool catalog is baked into the bootstrap, so whether the model is offered `delegate` at all is decided once, at session start, from `can_delegate` against the sub-agent tab. Calibrating that window mid-session notifies *"sub-agent slot ready — /new to give the model the delegate tool"*; it cannot be retro-fitted into a conversation the model has already read.

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

In registry order, which is also popup order (see **Ordering** below):

- `/help` — list the commands in the transcript. Aliases `/commands` and `/?` dispatch here but are never offered by the popup below — one obvious spelling per command.
- `/new` — clear the transcript and start a fresh session (re-arms the inline start flow of §1.3; the service picker unlocks so the next session can use a different preset). Refused mid-turn (answer/finish the current step first); reachable while armed/idle or after `task_done`.
- `/abort` — end the delegated sub-agent run in flight (§3.4c). A no-op with a warning when nothing is delegated.
- `/yolo [on|off]` — toggle YOLO mode (§2.6).

**One registry.** The list above is data, not prose repeated per consumer: `app/commands.py` holds a frozen `ChatCommand` (name, arg hint, one-line summary, aliases) per entry, and the controller's dispatch table, the `/help` note, the "unknown command — try `/help`, `/new`, `/abort`, or `/yolo`" hint, the F1 help screen's command section and the autocomplete popup all render from that one tuple. `SessionController._command_handlers()` is the join between a registry name and what it does, and a test pins the two sets equal, so a command cannot ship as an undiscoverable row (or a hidden feature). It sits in `app`, not `tui`, because the dispatch does — plain string work, no Textual import, which is exactly what lets a `tui` widget read the same table.

**Ordering is a safety property.** The popup lists the tuple as written, so whatever sits at the top is what a stray keystroke is nearest to — and `/yolo` disables every approval gate in the app. It is therefore **last**, behind the harmless and the reversible, with `/help` first because it is what a lost user is reaching for. Together with the no-preselect rule below, that is two independent locks on the same door: nothing destructive is at the top of the list, *and* the top of the list is not reachable without an explicit choice.

Precedence: while answering an `ask_user` question, the typed text is **always** the answer (commands are not parsed) — so a slash-leading answer like `/etc/hosts` is delivered verbatim, never eaten. A follow-up message that must begin with a literal slash is escaped as `//…` (one slash is stripped and the rest sent as a message).

**Autocomplete (`CommandPopup`, §1.2).** Commands are otherwise findable only by already knowing `/help` exists, so a compact list pops up **directly above the composer** the moment one is being typed, and narrows with every character.

- *Trigger* (`commands.match_prefix`, shared with the dispatcher so the two cannot drift): the box's text is a single bare token starting with exactly one slash. `/` offers everything; `/y` narrows to `/yolo`; `/yolo` still matches itself. Each of the three ways a line stops being a command in progress closes the popup on its own — `//escaped` is the literal-slash hatch, `/yolo ` has committed and is typing its argument, and `/xyz` matches nothing. Deleting the slash closes it too.
- *Nothing is pre-selected until the user narrows the list.* A bare `/` shows every command with **no** highlight; one typed letter (which filters) or one arrow press arms a row. This is a safety rule, not a cosmetic one: with the top row always highlighted, `/` + `Enter` + `Enter` *ran* `COMMANDS[0]` — two keystrokes past a character typed by accident, at a box whose whole job is sending text. The composer decides it (`preselect=len(text) > 1`) because it is the only thing that knows what was typed; the popup keeps `index = None` and answers `highlighted = None`.
- *Keys*, intercepted by `ChatComposer` and **only** while the popup is up; every other key keeps editing (and re-filters the list underneath):
  - `↑`/`↓` move the highlight, wrapping at both ends; from *no* highlight, `↓` arms the first row and `↑` the last — exactly where wrapping from either edge would have landed.
  - `Enter` or `Tab` **complete**: the highlighted command replaces the line as `/name ` — with the trailing space where its argument goes. Enter does *not* send here; because the trailing space closes the popup, the **next** Enter is an ordinary send. `Tab` does not move focus. With no row highlighted there is nothing to complete, and the key is still swallowed — so a bare slash can neither run a command nor be sent as a message; it simply waits to be narrowed.
  - `Esc` dismisses the list and touches neither the text nor the focus. With no popup up, `Esc` keeps its old job (blur to command mode, §1.2).
- *Focus* never moves: the popup is a `Static` painting one Rich `Text` and is driven entirely by method calls from the composer, so it cannot steal focus from the box — or from the Approve button at a gate, where a disabled composer closes it anyway. One command is always exactly one row (`text-wrap: nowrap; text-overflow: ellipsis` — a summary wider than the chat column is cut), so the popup's height is its match count and the highlight lines up with what it points at; a wrapped row would push the last command out from under `max-height` instead.
- *Suppression.* No popup while the box's next send is consumed **verbatim**, because a leading slash there is text and offering to complete it would misrepresent what Enter does. That is exactly two modes, and both are already-known state rather than a new signal: `MainScreen.awaiting_new_session` (the task that starts a session, §1.3) and `awaiting_answer` from the `SessionView` push (the `ask_user` gate above). `_update_composer` hands both to the composer as one `verbatim` flag alongside the enable/disable decision, and a disabled box never shows a popup either.

### 3.4a Screen regions and the focus click (the "hand me back to the browser" nudge)

The user draws **one** box for this — the **chat region** (`#set-region-btn` → `#side-region`), the *window that hosts the AI chatbot* — written into the window tab that was selected when the **picker opened** (§1.6), which is passed to `_pick_chat_region(slot)` rather than re-read afterwards. The overlay blocks for as long as the user takes to drag a box, and `_calibrating` moves on its own in that window: a delegation starting mid-drag focuses the sub-agent run's transcript and selects its tab, and a slot read *after* the await therefore filed the box drawn around the master's window as the sub-agent's — and skipped the poller restart the master needed. The same slot decides both the write and the restart; the sidebar's `#side-region` is only repainted when that slot is still the one on screen. It describes where the conversation lives: it is where every appearance is searched for, the last-resort click target, the scroll target of the auto-copy flow, and — all by itself — the staleness finish detector.

The chat input box is **not** a drawn location. A fresh chat centres its box and an ongoing one docks it at the bottom, and after a new-chat click the layout is the *initial* one — so both layouts are captured once per **service** as appearances (`TemplateKind.CHATBOX_INITIAL` / `CHATBOX_ONGOING`, the editor's APPEARANCE column, §1.4) and found *inside* the drawn chat region at click time. `_chatbox_region` captures the live chat region once and hunts both in that one frame (ongoing first: mid-session it is the common case, and the search stops at the first hit), returning the match's absolute rectangle. That inversion is the point of the whole model: the pixels never move, so a browser the user resized or dragged to the other monitor costs one redrawn box instead of six recaptures — and a second window pointed at the same service costs *nothing*.

`_find_all(kind, slot=None, scene=None)` is the one primitive underneath: capture the slot's chat region (or reuse a frame the caller already took), search THAT WINDOW's service profile for `kind` at that kind's own `max_diff`, and return the absolute rectangles of every distinct place it verifies — an empty list for every way it can come up empty (no region drawn, nothing captured for that kind, the capture failed, or it simply is not on screen). Returning *all* of them is what makes ambiguity detectable: two windows of the same service inside one drawn region resolve the same appearance twice, and anything about to aim a real click (`_click_profile_element` → `ElementClick.AMBIGUOUS`, refused with a redraw-the-window message; `_chatbox_region` → fall back to the drawn region rather than paste-click a maybe-wrong box) must refuse rather than guess — near-duplicate hits of the same physical element are merged first, so "two" always means two *elements*.

After **every outbound clipboard copy** — bootstrap, results, user answers, revert notices, re-copies — MainScreen clicks the centre of whichever chat box was found, falling back to the chat region itself when neither is (mid-transition, a dialog over it, or nothing captured yet): clicking the window is recoverable, not clicking at all means the paste never lands. `_click_after_response` returns `bool` — True only when a target was known AND the click landed. Nothing drawn means no click at all. Every one of those reads comes from the **live** slot, so mid-delegation the click goes into the sub-agent's window.

Only when that click landed does `copy_outbound` go one step further: after a 0.15 s settle it sends a synthetic Ctrl+V itself (`screen.focus.send_paste`), dropping the outbound payload straight into the focused input. If no region is drawn — or the click did not land — the paste is never attempted: focus could be on any window, and pasting into an unknown app is the one unforgivable failure mode here, so it stays click-only in that case exactly as before.

- **Drawing**: each button spawns the tkinter overlay as a *child process* (`agentclip --pick-region`, hidden flag) — tkinter cannot share the Textual process (both want an event loop; tkinter wants the main thread). Only one overlay may ever be on screen, and an exclusive worker group alone cannot guarantee it, because cancelling the worker does not kill the blocking child overlay process — so each of the two surfaces that can raise one holds a flag for the whole operation and refuses (with a toast) any further press while it is held: `MainScreen._picker_open` for the chat region, `ServiceEditorScreen._capturing` for the six appearance captures (which additionally refuses `escape`, §1.4). The two surfaces cannot be reached at the same time: the editor is a modal over the main screen. The overlay is a translucent topmost fullscreen window spanning the whole virtual desktop (multi-monitor, Windows metrics); drag draws a rectangle, `Esc` cancels, a sub-8-px drag is treated as a stray click and ignored. The child prints `left top width height` on stdout; cancel prints nothing. Each caller passes its own prompt ("the window that hosts the AI chatbot" / "the spot to click after each response").
- **Clicking**: `screen.focus.click_region` — Windows-only `SetCursorPos` + `SendInput` via ctypes (stdlib, no new dependency). Both processes force DPI awareness first so overlay coordinates and click coordinates share the physical-pixel space. On non-Windows the click returns False and the user is told once (not per copy) that focus clicks are unsupported.
- **Scope**: the drawn regions are app-run-scoped and live in a `SlotCalibration` on MainScreen (§1.6), show in the sidebar's `#side-region` label, and can be redrawn mid-session (window moved). They describe where the service's windows are, not what one conversation said, so they **survive** `/new` / the summary's *new session* for both windows — a new conversation in the same windows needs no re-drawing (and a sub-agent window calibrated mid-session actually enables `delegate` on the next `/new`, which is the advertised workflow). Only the pointers (`_calibrating`, `_live`) go home to MASTER, and the detector worker restarts against the surviving live window. Captured *appearances* are the opposite: they belong to the service, are shared by every window pointed at it, and **are** persisted (`screen.profile_store`, one folder of PNGs per service under `config.default_profile_dir()`), so they come back on the next run.
- **Layering**: all of it lives in `agentclip.screen` (region/overlay/picker/focus), an OS side-effect leaf like `clip` that only `tui`/`cli` may import (enforced in test_layering). The controller never knows the feature exists — the click rides inside the view's `copy_outbound`.

### 3.4b The copy button and auto-copy-click

Most chat sites need a click to get a response onto the clipboard at all — there is no keyboard shortcut, only a small icon under each response. This block automates that click once the busy detector (§1.3's REASONING block) says a response has finished, so the user never has to alt-tab and click it themselves.

- **Capturing**: the service editor's "Capture copy button..." (`#svc-capture-copy-btn`, §1.4) spawns the same draw-a-box overlay as the chat region, prompting (from `TemplateKind.COPY.prompt`) for a **tight** box around **one** copy-button icon — "pick the one under the last response, while the page is idle." Only the *pixels* are kept: they are anchored for search (`ServiceProfile.put`) and written to disk under the **service** (`profile_store.save_template`), so the same capture serves every window pointed at that service and every later run. The drawn rectangle is not stored at all — the icon is found where it actually is, every time. A capture failure is reported and nothing is filed; so is a *save* failure, since the editor keeps no in-memory profile for the run to fall back on. The sidebar shows only whether it is captured at all (`#side-profile-note`, "appearance: 4/6 captured"); the per-kind size lives on `#svc-tpl-copy` in the editor.
- **Arming and firing**: each poller tick takes one capture of the live slot's chat region and posts up to three messages in a **fixed order** — `BusyProbed` → `IdleProbed` → `StaleProbed` (`tui/messages.py`) — skipping whichever tracker it was not built with. `_start_detector_worker` (re)builds the whole set — busy/idle presence trackers if the LIVE window's service `finish_signals` ticks them *and* it has those appearances, the stale tracker if `finish_signals` ticks it, nothing at all if no chat region is drawn or the checklist leaves nothing runnable (§1.3) — whenever an appearance is captured, the live window's region is drawn or its service re-picked, the config is adopted, or the live slot moves; `_active_detectors` records which of them will post, in that fixed order, and the run gets a fresh `_detector_generation` — a monotonic int stamped into every probe message it posts. Each handler — `on_busy_probed` / `on_idle_probed` / `on_stale_probed` — first drops **ghosts** (`_ghost`), then records its detector's verdict and calls `_evaluate_finish` only when `_finish_tick_closed_by` says its message is the tick's **last**, which is simply `_active_detectors[-1]`: this is what lets `_evaluate_finish` fold the combined verdict exactly once per tick no matter which subset is running, instead of acting on a half-reported tick. `_evaluate_finish` folds every detector that has ever reported (`_seen`) into one verdict: **any** `False` (generating) resets the streak; only once **every** live verdict reads `True` (finished) does the streak advance, and reaching **2** consecutive all-`True` ticks fires `_auto_copy_flow` and disarms — so with several detectors calibrated, that agreement is the whole point of having more than one. A capture error (`None`) breaks the streak but leaves the arm alone — a single bad frame must not cancel an in-flight finish. Evaluation is suspended for the flow's entire run (`_flow_running`), and **every** live tracker is reset in the flow wrapper's `finally` (`_run_auto_copy_flow`): the flow clicks, scrolls and hover-scans the very window all three detectors watch, so without the suspension and the reset it would read its own mouse work as a fresh generation and re-arm/re-fire itself forever.
- **A verdict is a reading of one window, and it says which.** Cancelling a poll thread only raises a flag: the loop it interrupts still finishes its tick and posts. `_ghost(detector, generation)` drops anything from a run that is no longer live, and the **generation stamp** is the load-bearing half of that test. The poller is restarted precisely when the automation changes windows (`start_browser_chat` / `end_browser_chat`, §3.4c), so an in-flight probe routinely lands *after* the retarget — describing the window AgentClip has just stopped driving. Filtering by detector name alone let it through, because both windows run a stale detector: `/abort` during a generating sub-run handed the master the live slot, the sub window's last "still generating" then armed the trigger, and two quiet ticks fired the copy flow at the **master's** chat. The name check remains as the second half, for the case the stamp cannot see: when the new detector set is *smaller* (a forgotten appearance, an unticked signal) a leftover "generating" would re-arm the trigger on every later tick and wedge auto-copy shut for good. Dropping a verdict is always safe — a detector that is still running is refreshed one poll interval later.
- **What may arm it is not what may break the streak.** A `False` from **busy or idle** arms the trigger on the spot: the reasoning indicator being on screen (or the send button being gone) is evidence only a real generation produces. A `False` from the **stale** detector is not — frame-to-frame change is produced by a blinking caret and a drifting mouse as readily as by a model, and AgentClip's own paste leaves the user staring at a composer for as long as it takes them to press Enter. Arming on that noise, then reading the still, reply-less pre-Enter screen as a finished response, fired the auto-copy at a chat containing nothing. So staleness arms only on a **sustained large delta**: `StaleProbe.diff ≥ SEND_ARM_MIN_DIFF` (`0.02` — two orders of magnitude above caret/hover noise, far below a prompt landing plus the reasoning UI unfolding) on `SEND_ARM_TICKS` (`3`, ~1.5 s at the 0.5 s cadence) **consecutive** stale probes; any smaller probe (and any STALE or ERROR) restarts that run. A small-diff `CHANGING` still resets the finished-streak — it is "not finished" evidence whatever else it is — it simply may not claim the send happened. `Sidebar.hide_paste_flash()` moves with the arm rather than with the verdict, so the `>>> PRESS ENTER <<<` banner comes down exactly when the send is provably detected.
- **The flow** (`_auto_copy_flow`, all OS calls off the event loop via `asyncio.to_thread`):
  0. **Nothing to do without a drawn chat region** (or without a captured icon): the search happens *inside* the window the user drew, so with no window there is nowhere to look and the flow returns having clicked and scrolled nothing.
  1. Focus the browser exactly like `copy_outbound` does after every copy — `_click_after_response` (the chat box found in the region wins, the region itself is the fallback) — then a 0.15 s settle.
  2. Scroll the transcript to the bottom fast: `scroll_region(chat_region, -40)`. A 0.4 s pause lets the page render after the flick.
  3. Capture the chat region and hand it to `agentclip.screen.template.find_lowest_in_region(template, scene, max_diff=TemplateKind.COPY.max_diff)`, which returns the bottom-most (i.e. newest) verified match anywhere in it — the icon appears once per response, so multiple matches are normal and only the lowest one matters. There is no band and no width constraint, so the old "search failed" branch is gone with them; a capture failure is reported (`#side-tpl-copy` reads `capture failed`) and the flow aborts.
  4. No match in that static frame, **and the active service's `ServicePreset.hover_scan` is on**: the chat may only paint the icon under the pointer (Claude does), so `#side-tpl-copy` reads `hover-scanning` and the flow walks the real cursor up the chat region (`screen.hover.hover_scan_points`), re-capturing the region and re-searching after each stop, taking the first frame the icon appears in. The scan is opt-in per service and **off by default** because it is a slow, visible takeover of the user's mouse: where it is the only way to find the icon it is worth that, and everywhere else a static miss simply means the icon is not there. With it off, a static miss goes straight to the not-found branch and the cursor is never touched. Still nothing: toast "copy button not found on screen" (warning) and stop — nothing is clicked. A match: a **verified, retried click** (`_verified_copy_click`) at `match_rect(chat_region, template, match)` — sometimes the cursor lands on the right spot but the hover-rendered button hasn't quite registered the click, so a single unverified click is not trusted. The provider's clipboard text is read once as a baseline before the first attempt; up to **three** attempts fire, each at a small offset from the matched rect's position that stays well inside the ~24 px icon — `(0, 0)`, then `(-3, -3)`, then `(+3, +3)` — each attempt doing `click_region(rect, settle_s=0.05)` (the 50 ms hover settle, same reasoning as the copy-button hover elsewhere) followed by up to six clipboard reads 0.2 s apart, stopping as soon as the text differs from the baseline. If the baseline read itself is unavailable (`ClipboardUnavailable`), the flow falls back to one unverified click instead of retrying blind — there is no signal to retry against.
  6. On a verified (or unverifiable-fallback) click: toast the diff, and snap focus back to AgentClip — a 0.15 s beat (so the browser registers the click), then `focus_window(_own_window)`. `_own_window` is the foreground window handle recorded whenever the user is provably interacting with AgentClip — at mount and on every composer send (`_remember_own_window`) — and is deliberately **not** session-scoped: the terminal outlives `/new`. `focus_window` (screen.focus) taps ALT through SendInput first, the documented input-recency loophole without which Windows refuses `SetForegroundWindow` from a background process. On exhausted retries (the clipboard never changed across all three attempts): toast a warning ("copy click did not take — click the response's copy button yourself") and deliberately **do not** snap focus back — the browser stays focused so the user can click it themselves.
- Every branch also repaints the sidebar's `#side-tpl-copy` line with the captured size plus a short ASCII status (`hover-scanning` / `clicked (diff 0.03)` / `click did not take` / `not found` / `capture failed`), which `Sidebar.update_template` prefixes `copy · `; it rests at `copy · no click yet`.
- **The paste flash and auto-paste**: automation covers browser→AgentClip; AgentClip→browser used to always end in a human Ctrl+V, and now only sometimes does. Right after its focus click, `copy_outbound` checks whether that click actually landed (`_click_after_response` now returns `bool`): if it did, a 0.15 s settle and then a synthetic Ctrl+V (`screen.focus.send_paste`) drops the payload straight into the focused input, and the sidebar banner reads `>>> PRESS ENTER <<<` (`Sidebar.ENTER_FLASH_TEXT`) — the human's only job left is the send keystroke. If the click did not land, or no region was drawn at all, no paste is attempted (pasting into an unknown window is the one unforgivable failure mode) and the banner falls back to its original `>>> PRESS CTRL+V <<<` wording (`PASTE_FLASH_TEXT`). Either way it is the same obnoxious banner at the very top of the sidebar (`#side-paste-flash`, bold, red/yellow, blinking at 0.4 s via a Sidebar-owned timer toggling the `flash-alt` class — pure presentation, so the dumb widget may own it), just with different text; `Sidebar.show_paste_flash(text=...)` takes the copy to show. It hides when the moment has provably passed: a `MATCH` busy probe (the model is chewing — the paste/send landed), a new `ClipboardCaptured` (the conversation moved on without it), or `clear_transcript()`. `Sidebar.show_paste_flash`/`hide_paste_flash` are the only entry points; display on/off (and, now, which text) is the tested contract.
- **Scope and layering**: the captured icon lives in the service profile — shared by every window on that service, persisted to disk, so it outlives `/new` *and* the process; only the trigger state (`_copy_armed`, `_copy_changed_streak`, `_stale_arm_streak`) is reset by `clear_transcript()`. Nothing is persisted to the config file. The flow itself lives entirely on MainScreen, like the focus click — the controller never knows the feature exists.

### 3.4c A sub-agent run, end to end

When the model calls `delegate`, the engine parks the turn in `AWAITING_SUBAGENT` and hands the controller a `Delegate(task, context)` step. What follows is a *nested session*, not a parallel one: the master's flow coroutine is blocked inside the delegate call for the whole run, which is exactly what lets the single clipboard watcher, the single approval gate, the single `ask_user` future and the single focused transcript be **retargeted** rather than duplicated. At most one chat is live at any instant.

1. **Ask first.** `view.delegation_available()` is checked before anything is built. False ⇒ the model gets `status=error` naming the missing calibrations (`view.delegation_missing()`), the master's turn continues, and no tab is opened. The controller never learns what a "new-chat button" is — the gaps cross the port as data. The same refusal covers a second way the ground can move: `_subagent_service` is frozen at bootstrap (both pickers lock for the session's life) but the **service editor is not**, so F2 mid-session can delete the very preset that key names. A frozen key that is no longer in `config.services` refuses the delegation outright — building the engine anyway would fall through `cli.build`'s unknown-preset fallback to `[general]`, giving the sub-run neither the budget readiness advertised nor the one its window is pointed at.
2. **Save the master.** The whole per-session context (engine, chat name, preset, stats, glyph strip, last outbound, YOLO mirror) is snapshotted into a local and restored in a `finally`. YOLO deliberately does **not** inherit: `ApprovalPolicy` is per-engine, so a sub-agent starts from the configured default.
3. **Build a sub-agent, on the sub-agent window's service.** `EngineRequest(service=self._subagent_service, role="subagent", allow_delegate=False, parent_chat_name=…)` → its own Engine, its own chat name, its own `SessionStore` (the `session` event records the parent, so the audit trail joins up), the sub-agent bootstrap variant, and a catalog with no `delegate` in it — nesting is excluded by construction, not by a special case. `_subagent_service` is the service the SUB tab was on when the session armed, carried across the port in `SessionSpec.subagent_service` and frozen there (§1.6): its paste budget is the budget of the chat this run is actually going to be pasted into, and composing against the master's would silently overrun a smaller one.
4. **Start its transcript and compose.** `open_session_view(ref)` appends the `── task: <title> ──` divider to the sub-agent window's (persistent) panel, records where this run's events begin, badges the tab `▶`, and focuses that window; the task (plus `context` under its documented heading) is composed into a bootstrap. A `BudgetExceeded` here is an error result to the master, never a crash.
5. **Open its chat — before any paste.** `start_chat(ref)` searches the SUB-AGENT window's chat region for THAT tab's service's captured new-chat button, clicks the match, and only then retargets the automation. Three refusals, three different stories: nothing captured / no window drawn (`NOT_CALIBRATED`), the button is not on screen right now (`MISMATCH` — nothing is clicked, because clicking blind in a browser window is the one thing worse than not clicking), and the OS swallowing the input (`NOT_CLICKED`). **False aborts the delegation with zero paste calls**, because a sub-agent's bootstrap in the master's chat is unrecoverable. This is the single most damaging failure mode in the feature and has its own tests at both layers.
6. **Run the ordinary loop.** Ingest → review → gate → execute, against the sub-agent's engine, into the sub-agent window's transcript, pasting into the sub-agent's window and polling it with ITS service's finish detectors. Replies are routed by chat name (`peek_chat_name`, a cheap scan of the last sentinel line) **before** the busy check — a sub-agent reply reaching the master's depth-1 queue would never be looked at, since the master is busy for the whole run. A master-chat reply arriving mid-run is dropped with an explanation, never queued: the master's next payload is composed fresh afterwards, so it is stale by definition.
7. **Hand the result back.** `task_done`'s `result` becomes the `delegate` call's result body, verbatim (falling back to `summary`, then to a placeholder — the delegating agent's result body is never empty). The run is annotated, its slice of the transcript is closed and the sub-agent tab drops its `▶` for a `✓` (or, on any of the failure paths below, a `✗` and an honest note); `end_chat` returns the automation to the master window, the master's context is restored, its window is refocused, and the turn resumes at the call *after* `delegate`. The next delegation appends under its own divider in the same panel — the window's transcript is one scroll, and `render_log` slices it back apart per run.

**Waiting and stopping.** There is no wall-clock timeout — the transport is a human alt-tabbing between two browser windows and a bounded sub-task can honestly take twenty minutes. The composer therefore stays enabled for the whole sub-run (its border reads *"Sub-agent running · /abort ends it and tells the model"*) even though the master's flow is busy, because `/abort` is typed there. Two escape hatches, deliberately different:

- **`ctrl+x`** cancels the tool calls running *right now*, in whichever chat is live. The turn still finishes and reports (the killed call plus the skipped ones) into that chat. A delegation survives it.
- **`/abort`** ends the whole run. The master gets `status=error, body="the user aborted the sub-agent run…"`. Where it lands depends on where the run is parked, and all three cases converge on the same `finally`: waiting for a reply ⇒ the reply future raises; at an approval gate ⇒ the gate is rejected (which aborts that turn) and a latched flag ends the run at the next reply park; executing tool calls ⇒ `request_cancel()` on the sub-agent's engine unblocks the worker, that turn ends normally, and the latch ends the run when the loop comes back for a reply. A sub-agent's `ask_user` is **not** abortable this way: while the composer is in answer mode its text is the answer, verbatim (§3.3a's precedence rule), so `/abort` typed there is an answer like any other.

Every failure path — uncalibrated, unverified click, abort, budget, or an exception nobody predicted — comes back to the model as an `error` result on the `delegate` call, and the `finally` always restores the master, drops the live slot back to the master's window and refocuses the master's window.

### 3.4d Service appearance profiles

Everything §3.4a–c searches for is an **appearance**: a small picture of what one of a service's controls looks like, captured once and recognised wherever it happens to be. This section is the one place the whole model is written down.

**The six kinds** (`agentclip.screen.profile.TemplateKind`, in declaration order — the service editor's APPEARANCE column is generated from it, §1.4): `BUSY` (on screen only while the model generates — the stop button, not a spinner: an animation is a different picture every frame and can never be matched back), `IDLE` (its mirror: on screen only while the chat is idle, usually the send button), `CHATBOX_INITIAL` and `CHATBOX_ONGOING` (the input box in its fresh-chat and mid-conversation layouts, captured while empty), `COPY` (one response's copy icon), and `NEW_CHAT` (the browser's new-chat control). Each kind carries its own overlay `prompt` and its own match threshold (`max_diff`: 0.08 for the three crisp icons, 0.10 for the hover-tinted new-chat button, 0.20 for the big mostly-flat chat boxes) — properties of the *appearance*, so they live on the enum, not in the TUI. `ServiceProfile` is the per-service bag of captured kinds; readiness questions (§1.6) and every search go through it.

**The search** (`agentclip.screen.template`): pure Python cannot afford a naive 2D scan (a 1920×1080 region is ~2M candidate offsets), so candidate generation is pushed into C-level `bytes` operations. At capture time, `Template.build` quantises the image's blue plane (`pixels[0::4].translate(>>5 table)`, 8 buckets — a bucket comfortably exceeds the tolerance-24 noise band) and picks 8 eight-byte **anchors** at the most varied windows on distinct rows. Per search, the scene's plane is built the same way once and each anchor's needle is swept **bottom-up** with `bytes.rfind` — when the per-anchor candidate cap (512, counting only deduped, in-bounds candidates) does bind on pathologically repetitive content, the survivors are the bottom-most ones, which is where the answers live (the newest response's copy button, the chat's action area). Each surviving candidate origin passes a 16-point grid probe before the full 1024-sample strided verification (per-channel tolerance 24 on B/G/R, the undefined X byte skipped, the stride bumped to be **coprime with the template width** — a stride sharing a factor with the width collapses the samples onto a few columns and can score a mostly-different image as a perfect match) produces the diff that `max_diff` judges; a global `MAX_VERIFICATIONS` cap bounds the worst case. Quantisation has a boundary hazard — a pixel drifting across a bucket edge breaks an exact `find` — and the mitigation is 8 anchors preferring **distinct rows and distinct needles**: one surviving anchor is enough, and a genuinely static element keeps most of them. The tiled worst case (a 2560×1440 scene covered in copies of the template) measures ~95 ms; the typical busy-path probe is ~10–30 ms — comfortable fractions of the 0.5 s poll tick on a worker thread. `find_all_in_region` returns every verified occurrence (ambiguity is the caller's question, §3.4a), `find_lowest_in_region` answers "the newest response's copy button" (max y), and `match_rect` converts a scene-local hit back to absolute screen coordinates for the click. The same coprime-stride rule guards `busy.diff_fraction`, which the staleness detector scores frames with — there the aliasing was not a nuisance but a harvest-mid-generation bug.

**Persistence** (`agentclip.screen.profile_store` + `agentclip.screen.png`): one folder per service key under `config.default_profile_dir()` (a `profiles/` sibling of config.toml), holding one PNG per captured kind plus a `profile.json` manifest (`FORMAT_VERSION = 1`, per-kind file name, dimensions, capture timestamp). PNG — hand-rolled on stdlib `zlib`/`struct`, colour type 6, filter 0 — earns its ~70 lines over raw bytes because *"did I capture the right button?"* is the single most common calibration question and a PNG answers it with a double-click. Writes are atomic (`mkstemp` + `os.replace`, mirroring `config.save_services`) and ordered PNGs-first-manifest-last, so a crash mid-save leaves a consistent, possibly stale profile — never a manifest naming a missing file. `load_profile` never raises: a missing directory is an empty profile, an unknown version is an empty profile, one corrupt PNG costs only that kind — a damaged config dir must not stop the app booting. Service keys are validated against the editor's own `^[a-z0-9]+(-[a-z0-9]+)*$` before touching the filesystem, so a hand-written config key can never become a path. The store takes its root as a parameter and never imports `platformdirs` — the layering split (config owns the *where*, screen owns the *what*) is enforced by test_layering.

**A capture is a capability, not an instruction.** Having `BUSY`/`IDLE` in the profile only means the poller *could* use them; whether it does is the service preset's `finish_signals` checklist (§1.3, §3.4b), and the two are ANDed — an appearance nobody ticked is dead weight, a ticked entry with nothing captured runs nothing. The same split governs the hover scan: the `COPY` capture says what the icon looks like, `ServicePreset.hover_scan` (default **off**) says whether the flow may go hunting for it with the user's real cursor. Both live in the config file rather than the profile because they are *policy about* a service, not a picture of it — and both are edited two columns away from the capture buttons in the same modal (§1.4), which is why the editor can warn inline when a tick and a capture disagree.

**Invalidation is manual by design.** A profile is per service *and per theme*: switch the chat's light/dark theme or hit a site redesign, and the captures stop matching — the fix is re-pressing that one capture button in the editor (the new capture overwrites the old), or "Forget appearance" to start clean. Nothing expires automatically: a template that stops matching degrades to "not found" toasts, and the stale detector still finishes the turn wherever the checklist has it ticked (the shipped default) — annoying, visible, and safe. The failure mode automation must never have is clicking somewhere wrong quietly.

### 3.4e Who owns the sidebar's DETECTION lines

Four lines (`#side-tpl-busy`, `#side-tpl-idle`, `#side-stale`, `#side-tpl-copy`, §1.3) report what the finish detectors are seeing. They belong to the **live** window — the one the automation is driving — and everything above them in the column belongs to the **selected tab**. Those two pointers are the same almost always and are apart for exactly the length of a delegation, which is when the readout matters most, so the split has to be stated rather than inferred:

- **Only the detector machinery writes them.** `_start_detector_worker` repaints the whole block (`_paint_detection`) on *every* exit, including the two that start nothing, and the probe handlers overwrite it tick by tick. `Sidebar.show_slot` no longer touches the stale line and `Sidebar.show_profile` no longer resets the probe lines. Both used to: `show_slot` wrote a flat `watching the chat region` whenever the *selected* tab had a region, which silently overwrote `finish detection off` (§1.3) with a promise of an auto-copy that would never come, and froze on a stale claim as the detector set changed underneath it; `show_profile` reset the probe lines on every tab click, throwing away the live readout of the very sub-agent run the user had switched over to read.
- **A tab click that changes nothing does nothing.** `WindowTabs` re-posts `WindowSelected` for a click on the already-selected tab, so `_select_window` early-returns when the window is already selected. `_selected_window` and the displayed panel move together, so there is never a stale view to correct — only widgets to rebuild from state that did not change, which can only lose something.
- **The block names its window.** `Sidebar.show_detection_window(name)` titles it *DETECTION · MASTER* / *DETECTION · SUB-AGENT*, painted by the same repaint. Without the name, a mid-delegation readout of the sub-agent's window reads as the master tab's, which is a worse error than showing nothing.
- **Its resting states explain their own silence.** `STALE_CALIBRATED` while stale runs, `STALE_UNTICKED` when the service unticked it but icons still run, `STALE_OFF` when nothing runs at all, `STALE_UNSET` with no region drawn; `PROBE_UNCAPTURED` on a busy/idle line whose signal is ticked with no appearance behind it (§1.3). A line resting forever at "no verdict yet" is indistinguishable from a detector that simply never finds anything.
- **A modal that draws on the screen suspends them.** The service editor's capture buttons throw the same fullscreen overlay over the browser the detectors watch, and an overlay appearing and vanishing is exactly the sustained large delta that arms the trigger on staleness alone (§3.4b). `AgentClipApp._open_service_editor` therefore calls `MainScreen.suspend_detectors()` before pushing it and `resume_detectors()` in a `finally` — the common exit is "closed with no changes", which propagates nothing and would otherwise never restart the poller. F2 is refused outright while `MainScreen.picker_open`: two one-overlay-at-a-time guards on two screens can both be satisfied at once, and cancelling a worker cannot kill a child process.

---

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

`Binding(...)` on MainScreen unless noted. Dynamic = gated via `check_action` + `reactive(..., bindings=True)` (`pending_approval`, `awaiting_answer`, `busy`, `executing`, `session_active`, `awaiting_new_session`, `phase_name`, `watch_paused`, `reject_open`, `has_outbound`, `sub_running`); `None` returns show dimmed keys in `Footer` for discoverability.

Only keys that are actually bound are listed. Chunk-walk mode (§6) has none yet — its `space` "skip the ACK, arm the next part" binding lands with the feature, not before it, so the footer cannot advertise a key that does nothing.

| Key | Action | Context (check_action) |
|---|---|---|
| `y` | approve pending call | pending_approval |
| `n` | reject pending call (opens reason Input) | pending_approval |
| `a` | approve + auto-accept edits for the session | pending_approval and the gate is an `edit` |
| `u` | undo last turn (ConfirmScreen) | session active, not busy, AWAITING_REPLY/DONE |
| `c` | re-copy current outbound / current part | has_outbound |
| `i` | force-ingest clipboard now | session active, not busy, AWAITING_REPLY |
| `w` | pause/resume watcher | session active (never in `manual` clipboard mode) |
| `t` | jump to the chat box (it is docked, not modal — §1.3) | session active |
| `e` | end session → SummaryScreen | session active, not busy, AWAITING_REPLY/DONE |
| `l` | export the whole chat log to a file — the master's transcript, then each delegated RUN under its own heading (§1.6) | session active |
| `x` | toggle most recent transcript collapsible | always (main; `show=False`) |
| `enter` | toggle focused Collapsible | native, when focused |
| `pageup`/`pagedown` | scroll transcript | always (main) |
| arrows / `pgup`/`pgdn` | scroll focused panel (diff body autofocused at gate) | native |
| `escape` | close the slash-command popup / cancel reject-reason / blur the composer to command mode / dismiss modal | contextual, in that order |
| `F1` / `?` | HelpScreen — its command section renders from `app/commands.py`, so it cannot drift from what `/help` says | global (App) |
| `F2` | ServiceEditorScreen: sizes, appearances, the finish-signal checklist (§1.4). Refused while the chat-region overlay is up — two fullscreen child processes cannot share a desktop | global (App) |
| `F3` | show/hide the settings sidebar | MainScreen (priority: works while the composer has focus) |
| `F4` | SettingsScreen (preferences) | global (App) |
| `F6` | select the next window tab — shows that window and points the sidebar at it, never moves where output lands or which window the automation drives (§1.6) | MainScreen (priority; `show=False`) |
| `enter` | with the slash-command popup up and a row highlighted, **completes** it to `/name ` (the trailing space closes the popup, so the next Enter sends); with the popup up and nothing highlighted, does nothing at all; otherwise sends the composer (task at start, answer, follow-up) | composer focused (§3.3a) |
| `tab` | completes the highlighted command exactly like Enter, and does not move focus | composer focused, popup open with a highlight |
| `↑` / `↓` | move the command-popup highlight, wrapping; from *no* highlight, `↓` arms the first row and `↑` the last | composer focused, popup open |
| `ctrl+j` | insert a literal newline in the composer | composer focused |
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