# GUI wave — core + two UIs

Status: binding. Written 2026-08-14, at the start of the GUI wave. This doc records
the decisions; the per-surface behavior contracts live in `docs/design/ui-briefs/`.

## 0. The shape

AgentClip becomes **one core with two UI shells**:

- the existing Textual TUI (`agentclip.tui`) — stays, unchanged in behavior;
- a new desktop GUI built on **pywebview + WebView2** (`agentclip.gui`) — a native
  window rendering an HTML/CSS/JS frontend, Python in-process.

Both shells drive the same two UI-agnostic controllers:

- `SessionController` via the `ChatView` port (`agentclip/app/view.py`) — exists today;
- `AutomationController` via the `AutomationView` port (`agentclip/automation/view.py`)
  — being extracted from `MainScreen` in phase 0 (see §2).

Nothing above the shells may import `textual` or anything GUI-side; the layering is
enforced in `tests/test_layering.py`, twice (generic RULES + named boundary tests),
as it always has been.

### Why pywebview

- The app must run on the user's desktop (clipboard transport, Win32 capture,
  SendInput) — a hosted web UI is impossible, but a local GUI window is fine.
- WebView2 ships with Windows 11: no bundled Chromium, no Node toolchain, PyInstaller
  packaging keeps working, Python stays in-process (no IPC layer, both controllers are
  called directly like the TUI calls them today).
- HTML/CSS gives full design freedom — the original complaint about the TUI.
- Rejected: Electron/Tauri (force a process split and a second toolchain), Flet
  (heavy, maturity risk), textual-web (same look we're leaving), Qt/PySide6 (viable
  fallback, but ~80 MB heavier and modern styling is harder than CSS).

### Parity policy

- The briefs in `docs/design/ui-briefs/` are the parity contract for both shells.
- Features land **core-first**: new behavior goes into the engine/app/automation
  layers, then each shell grows its view of it. A feature that only exists in one
  shell's view layer is a design smell and needs a written exception here.
- The TUI keeps full parity for now. Whether it eventually freezes is a decision for
  after the GUI is proven — explicitly not decided in this wave.
- Known parity carve-outs today:
  - the chunked-send wizard (`tui.md` §6) is designed but not implemented anywhere
    (controller short-circuits multi-chunk; M3). Both shells target current behavior;
    the wizard lands core-first later.
  - SSH connect: the TUI keeps the launch-time CLI-flag flow; the in-app connect
    dialog (§4) is GUI-only. This is a deliberate exception, not a smell — the
    TUI *cannot* prompt before Textual owns the terminal.
  - OSC-52 clipboard fallback is TUI-only (a terminal escape); the GUI uses the real
    clipboard provider. `AutomationController.deliver` takes `clipboard_ok: bool`
    from the shell for exactly this reason.

## 1. The automation package (phase 0)

Decisions ratified from the extraction plan:

- New package `agentclip.automation`, sibling of `app`, holding `AutomationController`
  + the `AutomationView` protocol. Allowed imports: `agentclip.screen`,
  `agentclip.clip`, `agentclip.config`, itself. Banned: `textual`, `agentclip.app`,
  `agentclip.tui`. It is OS-coupled core, shared by both shells — the same flavor of
  layer `screen/` and `clip/` already are.
- **`AutomationView` is paint-only**: `paint_loop_state`, `paint_harness_entry`,
  `paint_detection`, `paint_stale`, `paint_elements`, `paint_armed`,
  paste-flash show/hide, `notify`.
  Same thread contract as `ChatView.call_*`: methods may be called from poller/watcher
  threads, implementations must be non-blocking and thread-safe (the TUI posts a
  message; the GUI enqueues to its JS bridge). No OS primitives on the port — the
  controller imports `agentclip.screen.focus` etc. directly. No scheduling primitive
  on the port — the controller owns its own `threading.Thread`s.
- **`os_armed` moves into `AutomationController`**, reversing the "view-owned" note in
  `app/view.py:202-212`. With two shells, one shared armed flag below both is the only
  non-drifting design. `ChatView.set_os_armed` stays; `MainScreen` passes through.
- Probe ordering: the poller calls the busy→idle→stale decision sequence synchronously
  in one call stack per tick (the "closing message" invariant becomes trivially true —
  no queue between producer and consumer).
- The session/window bookkeeping duplication (`MainScreen._sessions`/`_sub_runs` vs
  `SessionController._active`/`_sub`) is **deferred** — it is transcript-display
  state, not automation. Follow-up ticket, not this wave's phase 0.
- New no-Textual test suite `tests/automation/` (FakeAutomationView, mirroring
  `tests/app/`); all existing `tests/tui/*_ui.py` Pilot tests are **kept** as the
  wiring/integration check. Probe-injection test seam:
  `AutomationController.feed_probe(...)` replaces posting `BusyProbed(...)` messages,
  converted in the same commit that removes the message path.

Slices (each one commit, suite green, layering test run first):
1. move `loop_state`/`harness_log` (pure vocabulary) + layering rules
2. controller/view skeleton + slot-calibration pointers + `os_armed`
3. clipboard watcher thread ownership
4. detector poller threads + generation stamping — **producer only**: probes
   still cross to the UI thread as today's messages, every handler untouched.
   (Re-cut from the original plan: moving probe bookkeeping ahead of
   `_evaluate_finish` would have split reader and writer across threads — a
   race today's single-threaded handlers don't have.)
5. the whole consumer — split in two, because moving the code and moving the
   thread are separable and only one of them can change behavior:
   - **5a (code)**: probe bookkeeping, painting, both gates, the loop-state
     writer + harness log and finish-evaluation become controller methods,
     still invoked from the UI-thread message handlers they always ran on.
     The view grows the paint family (`paint_loop_state`, `paint_detection`,
     `paint_stale`, `paint_elements`, the paste-flash pair) with the port's
     thread contract, which the TUI implements as synchronous widget writes
     for now. The fire path stays a callback (`on_fire`), and so does the one
     question the fold cannot answer itself (`has_appearance`).
   - **5b (thread)**: the poller calls `consume_*` directly, the paint
     implementations become `post_message`, and the `feed_probe` test seam
     replaces message injection in the same commit
6. auto-copy flow + click/hover/scroll sequences + start/end browser chat
7. delivery path (`deliver(...)` async; OSC-52 stays TUI-side)
8. cleanup: dead code, doc sync (`architecture.md`, `tui.md` drift), module table

## 2. The GUI shell

- Package `agentclip.gui`; entry `agentclip --gui` (same binary; TUI stays the
  default until the GUI reaches parity — flipping the default is a later decision).
- **No Node toolchain.** The frontend is hand-written ES modules + CSS shipped as
  package data; no build step, no npm. If we ever outgrow this, that's a new decision
  here first.
- Bridge: Python→JS via pywebview `evaluate_js` fed from a thread-safe queue (the
  GUI's implementation of the view-port thread contract); JS→Python via pywebview's
  `js_api` object whose methods call the same controller methods the TUI calls.
- Images (elements panel, service-editor thumbnails): PNG data URIs per crop. The
  BGRX→RGB rule and crop-not-whole-frame policy carry over from `tui/graphics.py`;
  the sixel/half-block machinery does not.
- The `/identify` and region-picker overlays keep the existing tkinter child-process
  mechanism unchanged — the GUI is in-process Python and shells out the same way.
- Startup: the window mounts immediately; everything slow (SSH connect, MCP starts)
  happens after first paint with visible progress. The sixel terminal probe does not
  exist on this path.
- WebView2 runtime missing (old/stripped Windows): detect at launch, show a plain
  dialog pointing at the Evergreen runtime installer; do not bundle it.

## 3. Increment order (after phase 0)

Serial, one implementer per increment, both shells launching + tests green at each
step: shell + transcript/composer (proves the bridge) → approval gate → run panel →
sidebar/status/state rail → window tabs/delegation/summary → elements panel →
service editor → settings/help/modals → SSH connect dialog.

## 4. SSH connect dialog (GUI-only surface)

Contract and flow: `docs/design/ui-briefs/ssh-connect.md`. The 8-step connect
sequence and its semantics (`cli.py:471-551`) are preserved exactly; only the UX
changes. Ratified answers to that brief's open questions:

1. Successful manual connect offers "save as `[remote.<name>]`" — global config only.
2. Passwords are in-memory only, forever. No keychain, no disk, no config.
3. `~/.ssh/config` alias listing hides wildcard and `Match` entries.
4. Keyboard-interactive/2FA prompts get the same 3-attempt shape as passwords.
5. Reconnect stays lazy, but the status indicator includes a manual "reconnect now"
   button.
6. "Target owns its policy" gets a connect-time banner **and** a persistent marker in
   the project block — the footgun stays visible, not one-time.

## 5. Packaging (deferred, tracked)

The 78 MB onefile exe self-extracting on every launch is the dominant startup cost
and is independent of UI framework. Decision deferred to after the shell increment:
likely onedir + installer, or a slimmed onefile without the `cv` extra. The README's
"~15 MB" claim is stale either way. GUI assets (html/css/js) ship as package data and
must be added to the PyInstaller spec when `agentclip.gui` lands.
