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
    clipboard provider. Two things follow, and both shipped in slice 7:
    `AutomationHost.park_off_clipboard(text)` is where a payload the provider
    refused crosses back to a shell (the TUI writes the escape; the GUI does
    nothing), and `AutomationController.deliver` takes `clipboard_ok: bool` so
    the delivery is TOLD how the payload got parked rather than re-reading a
    clipboard that may not be where it went - which is what makes the streamed
    mode fall back to a single burst.

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
     replaces message injection in the same commit. **Shipped, plus two things
     the plan did not name.** (a) Two threads now write the consumer's state, so
     `AutomationController._tick_lock` (an `RLock`) is held for exactly one
     probe's consumption and by the UI-thread callers that mutate the same
     bookkeeping — the grain the message pump used to give this code for free.
     (b) `post_message` does **not** preserve order across threads: Textual
     routes a cross-thread post through `call_soon_threadsafe`, so an outgoing
     run's last paint can be delivered after the UI thread's rebuild reset the
     block. The generation ghost filter therefore gains a paint-side twin —
     `MainScreen._paint_epoch`, stamped into `PaintDetection`/`PaintStale`/
     `PaintElements` — and the two paints with a re-readable truth
     (`PaintLoopState`, `PaintArmed`) are drawn from the controller rather than
     from their payload, while the harness-log entries ride an ordered queue on
     the screen with the message as the nudge that drains it.
6. auto-copy flow + click/hover/scroll sequences + start/end browser chat.
   **Shipped, with two seams the plan did not name.** The sequences are async
   methods on the controller (the `SessionController._engine_call` precedent);
   the shell keeps only the SCHEDULING (`run_worker(..., group="copyflow",
   exclusive=True)`) and hands the body in, because that name is what the Pilot
   suites stub. (a) The OS primitives stay off the view port as decided, but
   they are reached through one substitutable object,
   `automation/ops.py:ScreenOps`, whose default implementation *is* the direct
   `agentclip.screen` call — the Textual suites monkeypatch those names at
   `tui/screens/main.py`'s scope, so the shell hands in a subclass that resolves
   its own module's names per call (slice 4's `_poll_capture`, generalised).
   (b) What the sequences still have to ASK a shell is a second port,
   `automation/host.py:AutomationHost` — the live preset/profile, `find_all`,
   the verified copy click, the prose ingest (the session is `agentclip.app`,
   above this layer), the detector rebuild. It is deliberately NOT folded into
   `AutomationView`: that port's contract is "callable from a poller thread,
   never blocking", and a host is only ever called from the event loop. Paints
   the flow needed grew no new port methods — the copy-status line is
   `paint_detection(COPY, …)` and the harvest's crop is `paint_elements`, so
   both now carry the paint epoch. `_own_window` became `set_own_window` on the
   controller (OS state, both shells snap back to it) with a read-only
   compatibility property on `MainScreen`.
7. delivery path (`deliver(...)` async; OSC-52 stays TUI-side). **Shipped, with
   one seam the plan did not name and one clarification of the carve-out.**
   `deliver(text, *, clipboard_ok)` is the OS half exactly as designed, and the
   whole flow around it came with it: `copy_outbound`, `park_outbound`,
   `retry_insert`, the `_pending_insert` bookkeeping, the burst-or-stream choice
   and the banner's four words. The clipboard WRITE moved down too - the
   controller holds the provider *and* the self-write set now, so one object is
   both the watcher's filter and the writer's register - which left the OSC-52
   fallback needing a way back up from *inside* the delivery (the partial-stream
   restore is below `deliver`, not above it). So `AutomationHost` grew one
   method, `park_off_clipboard(text)`: the controller writes, and hands a shell
   the payload it could not place. `clipboard_ok` stays exactly what §0 carved
   out - the answer to "how did this get parked", which only the STREAM path
   reads. The banner's words moved into `automation/delivery.py` with the beats,
   for the port's own "text, not decisions" rule; the sidebar re-exports them.
   The two synthetic keystrokes and the delivery's three beats and one chunk
   size joined `ScreenOps`, which is what keeps the Pilot suites' patches biting
   at `main.py`'s scope. Only the SCHEDULING stayed in the shell (`run_worker`
   for the retry button and for `c`'s second tap), the slice-6 arrangement
   unchanged; `redeliver_outbound`'s two refusals are the controller's
   `may_redeliver`.
8. cleanup: dead code, doc sync (`architecture.md`, `tui.md` drift), module table.
   **Shipped, plus the one race the extraction surfaced.** Moving the poller off
   the message pump (slice 4) put the UI thread's `reset_trackers` alongside a
   poller thread inside `detector.observe`, and a tracker reads its streak,
   spends a template search, then writes the streak back - so an in-place
   `reset()` was read-modify-written away a frame later and the frames the paste
   or the flow produced stayed in history. `_tick_lock` cannot close that at its
   own grain (the expensive halves of a tick are deliberately outside it), so the
   answer is **swap, not clear**: `PresenceTracker.fresh()` / `StaleTracker.fresh()`
   hand back a tracker of the same calibration with no history, and
   `reset_trackers` installs it in the controller AND in the live detector - which
   is why the controller now remembers the run's `ScreenDetector` (the poller
   reads its trackers through that object, so a swap that stopped at the
   controller would change nothing). The poll still in flight folds its frame into
   an object nobody reads again.

**Phase 0 is complete** — `5e419cb` (slice 1) through this commit (slice 8) on
`master`, i.e. `git log 5e419cb^..` filtered to "UI split phase 0": the
automation brain lives below the shells, `architecture.md` §0/§1 carry
`agentclip.automation` and its three seams, and `tui.md` points at where each
moved name went. What is left on `MainScreen` is the shell: tabs, transcripts,
sidebar routing, the paint handlers, the `run_worker` scheduling, and the
compatibility seams the Pilot suites patch.

## 2. The GUI shell

- Package `agentclip.gui`; entry `agentclip --gui` (same binary; TUI stays the
  default until the GUI reaches parity — flipping the default is a later decision).
- **No Node toolchain.** The frontend is hand-written JS + CSS shipped as
  package data; no build step, no npm. If we ever outgrow this, that's a new decision
  here first.
- pywebview is the optional extra **`gui`** (`uv sync --extra gui`, `pip install
  agentclip[gui]`), the same shape as `cv` and `mcp`: `gui/shell.py` imports
  `webview` inside its functions and `cli.main` imports the shell only on `--gui`,
  so a TUI launch never pays for it and an install without the extra still runs —
  it exits 2 naming the extra. The extra is in the `dev` group too, so the suite
  can exercise the real toolkit. `tests/test_layering.py` gives `agentclip.gui` its
  own RULES entry (the TUI's reach minus Textual, plus `webview`), adds it to
  `CLIP_SCREEN_IMPORTERS`, keeps it OUT of the textual exemption, and names two
  boundary tests: `test_gui_never_imports_textual`,
  `test_pywebview_only_in_the_gui_shell`. Slice 2 added `agentclip.engine` to
  that entry, and only for its VALUE types: `Decision` is what an approval
  answer IS (the same call the TUI makes), `PendingAction` is what a gate is
  handed, `Engine` is what the factory `cli.py` builds returns. A shell that
  could not name them would have to re-declare vocabulary its own controller
  already speaks — which is the drift the ports exist to prevent — and
  `agentclip.app` already depends on that layer, so the direction is unchanged.
- The assets are located with `importlib.resources` (`files("agentclip.gui") /
  "assets"`, materialized by `as_file` for the lifetime of the window loop), never
  `__file__` — so a wheel, a source checkout and the PyInstaller extraction all
  resolve the same way.
- The window loads a **`file://` URL**, not a path: pywebview starts a local Bottle
  HTTP server for a bare local path, and this page has nothing to serve. The price
  is that a `file://` origin cannot load ES module scripts (Chromium blocks them
  from an opaque origin), so the frontend is classic `<script>` files. If the JS
  ever needs real modules, the switch is to pass the path and let pywebview serve
  it on 127.0.0.1 — a change of one line and of this paragraph. The app version
  reaches the page as a URL fragment (`#v=…`) until the bridge exists.
- Bridge: Python→JS via pywebview `evaluate_js` fed from a thread-safe queue (the
  GUI's implementation of the view-port thread contract); JS→Python via pywebview's
  `js_api` object whose methods call the same controller methods the TUI calls.
  **Shipped in slice 2 (`agentclip/gui/bridge.py`), with the pywebview facts the
  plan had assumed rather than checked.** `Window.evaluate_js` *is* safe to call
  from any thread — the WebView2 backend marshals the script onto the WinForms
  UI thread with `Control.Invoke` — but it then **blocks the caller** on a
  semaphore until the script has run (`webview/platforms/edgechromium.py`). So
  the queue is not there for safety, it is there for the two things safety does
  not buy: a paint raised on the detector poller must not stall that tick behind
  a UI round trip, and two threads calling `evaluate_js` concurrently interleave
  in whatever order the scheduler picks — which is exactly the hazard phase 0
  slice 5b paid for with the paint-epoch filter. **One FIFO, one drainer thread,
  never interleaved**, so ordering is structural rather than re-proved per event
  family. Two consequences worth naming: the drainer inherits page-readiness for
  free (`evaluate_js` waits on pywebview's `_pywebviewready`, so events queued
  before first paint are delivered late and in order rather than lost), and the
  sink is a plain `Callable[[str], None]`, so the entire bridge — ordering
  included, under real threads — is testable with a list. Going the other way,
  pywebview runs each `js_api` method on a **fresh thread per call**
  (`webview/util.py:js_bridge_call`), so every one of them is a one-line marshal
  onto the GUI's loop and the page never touches controller state.

### The GUI's concurrency model (slice 2, `agentclip/gui/runner.py`)

`SessionController` is asyncio to the bone and the TUI hands it Textual's loop.
pywebview has none to hand: `webview.start()` runs a native message pump on the
MAIN thread and blocks there. So the GUI brings its own, and this is the shape
every later increment is built on:

- **one dedicated thread runs one asyncio loop** for the whole app run. Every
  session flow, blocking prompt and OS-acting sequence lives on it;
  `GuiView.spawn` is `GuiRunner.schedule`, which checks the calling thread
  (`create_task` on the loop, `run_coroutine_threadsafe` off it) because a flow
  spawning another flow is already on the loop;
- the **main thread does nothing but `webview.start()`**;
- the **bridge's drainer** is the only caller of `evaluate_js`; the **js_api
  threads** only marshal;
- the **watcher and detector threads are unchanged** — the AutomationController
  has always owned them, and this shell starts/stops them exactly as
  `MainScreen` does;
- **shutdown is the TUI's quit path in the same order**: stop what touches the
  machine (watcher, poller, by name), cancel every task on the loop (the
  equivalent of Textual cancelling a screen's workers on unmount), stop the
  loop, join, then let the bridge flush what it still owes the page. Every wait
  is bounded. It hangs off `webview.start()` RETURNING rather than off the
  window's `closing` event, deliberately: `closing` runs on the window's own
  thread and the drainer parks inside `evaluate_js` waiting on that very thread,
  so tearing down from there would make the two wait on each other.

### Two GUI-side decisions the TUI has no equivalent for

- **Enter sends, Shift+Enter is a newline.** The TUI uses `ctrl+j` for the
  newline because Enter is its send key inside a Textual `TextArea`; the GUI
  uses the web-native convention every chat composer has. A deliberate
  shell-idiom difference, recorded here so it is not read as drift.
- **`AutomationHost.park_off_clipboard` has no OSC-52 to fall back to.** The
  TUI writes the terminal escape (§0); a WebView2 window has nothing like it,
  and writing back through the page's own clipboard would be the same refused
  write one layer up. The GUI's honest equivalent is to **show** the payload in
  a selectable `<pre>` block with a toast saying the copy is theirs to make (and
  naming `.agentclip/sessions/<id>/outbound/` as the other way to it).

### Slice 2's reduced-scope port methods (the parity backlog)

Everything a *turn* passes through is the real thing — transcript, gate,
delivery, clipboard watcher, blocking prompts, `render_state`. These four are
implemented smaller than the TUI's, each saying so at its own definition and in
a toast where a user could otherwise be left staring at nothing:

1. ~~**Window tabs / session views.**~~ **Done in parity increment 3** —
   `open_session_view` / `focus_session_view` / `finish_session_view` are the
   real implementations now: one persistent transcript per browser window, a
   two-row tab bar over them, sub-run dividers and the derived `▶`/`✓`/`✗`
   state. The reduced single-transcript version (a divider plus a
   `· sub-agent ‹title›` speaker label) is gone.
2. ~~**`toggle_harness_log`.**~~ **Done in parity increment 2** — the pane
   landed with the state rail, exactly as this line said it would.
3. **`show_identify_overlay`.** The tkinter child-process mechanism carries over
   unchanged, but `/identify` needs a *drawn* chat window and this shell has no
   calibration surface yet. Toasts rather than putting an empty overlay up.
4. **`paint_elements`.** Routes the KINDS a tick recognised, not their pictures:
   no `crop_elements` is wired in, so PNG data URIs per crop land with the
   elements panel.

One duplication is tracked with them: `gui/view.py:_distinct_rects` and
`find_all` are `MainScreen`'s, spelled again because the two shells may not
import each other. They move down into `agentclip.automation` when the GUI grows
calibration and there are two real callers.
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
step:

1. ✅ shell + transcript/composer (proves the bridge) — slices 1 and 2 below.
2. ✅ approval gate — **parity increment 1**, with (3).
3. ✅ run panel — **parity increment 1**, with (2).
4. ✅ sidebar / status / state rail — **parity increment 2**, with the harness
   log pane and the keys those surfaces report on.
5. ✅ window tabs / delegation / summary — **parity increment 3**.
6. ⬜ elements panel
7. ⬜ service editor
8. ⬜ settings / help / modals
9. ⬜ SSH connect dialog

Shipped so far: **slice 1** (`63e3c76`) — the window over the packaged page, no
bridge. **Slice 2** — the bridge, the runner's loop, `GuiView` against all three
ports, and with them the first three items above at working-minimum: a task typed
in the composer runs a whole turn (transcript, approval gate with y/n + a reject
note, run rows with live output, the outbound parked on the clipboard, the
watcher's ingest, `task_done`), `ask_user` answers on the composer, and the four
blocking prompts are ugly-but-correct modals. `cli.py` builds the clipboard
provider, the MCP runtime and the engine factory ABOVE the shell fork now, so
both frontends are handed the same objects.

**Parity increment 1** — the MAIN CHAT surface is whole in both shells: items 2
and 3 above, against `docs/design/ui-briefs/main-chat.md`'s ActionPanel and
RunPanel sections. Nothing new crossed the ports; what changed is that the
*decisions* the TUI's two widgets make now cross with the data, so the page
renders instead of sniffing:

- the gate event carries a `preview_kind` (`diff` / `new_file` / `command` /
  `mcp` / `text`) with `preview_head`/`preview_body`/`reason`/`note`/`timeout`,
  plus the keys `hint`. Those are `ActionPanel.preview_renderable`'s branches,
  taken on the Python side because "an mcp call carrying a decoy `command:`
  must not repaint the gate as a harmless shell line" is a safety rule, not a
  rendering style. The page owns only the colouring: the unified diff is
  coloured **by hand** in `app.js` (`+`/`-`/`@@`/file-header line classes, HTML
  escaped first, long lines never wrapped), a new file gets the NEW FILE banner
  and CSS-counter line numbers, and the queue strip becomes one chip per call in
  the run panel's glyph alphabet. The middle "stop asking" button is unchanged
  in *when* it appears (`always_label`, slice 2) and now says in the hint which
  of the two things pressing it buys.
- `call_started` carries `streams`, so a row the plan never mentioned becomes
  expandable on exactly the terms `RunPanel.call_started` gives it.
- the run panel grew the spinner (a CSS animation, not braille frames), the
  glyph-coded rows, the `ctrl+x` cancel hint and the tail behind `ctrl+o` — the
  brief's rules kept exactly: only the *currently streaming* call, collapsed the
  instant it finishes, deltas accumulated page-side and bounded per call, and
  nothing painted while the pane is closed. Row **windowing is deliberately not
  ported** (`_MAX_ROWS = 8` is terminal real estate; this panel scrolls), per
  main-chat.md §7.
- keyboard: `y`/`n`/`a`/`x` are inert while a text box has focus — the brief's
  one safety mechanism — and `ctrl+x`/`ctrl+o` fire regardless, which is what
  the TUI buys with `priority=True`.

Still deferred after increment 1, and named so nobody reads them as done: the
composer's slash-command popup, the transcript's 500-event prune, and every
`MainScreen` binding that is not on this surface (`u`/`i`/`c`/`e`/`l`/`t`,
`shift+tab`) — they land with increment 4, which is where the rail that shows
their state is.

**Parity increment 2** — the SIDEBAR, the STATUS BAR and the HARNESS LOG, against
`docs/design/ui-briefs/sidebar-status-log.md`, plus the keys whose state those
three surfaces display (`modals-keys-esc.md` §5.1). Nothing new crossed the
ports and no core change was needed; what changed is that four more families of
*decision* cross with the data:

- `{type: "rail"}` carries the eight `LoopState` rows with `LOOP_TRANSITIONS`
  already applied (`active` / `legal` / `dim`). The table is the automation's
  vocabulary and the page has no business holding a copy of it; it stays
  display-only on both sides, as `loop_state.py`'s header requires.
- `{type: "status"}` carries the ten segments **composed and in order**, because
  every one of them is a rule rather than a style: the watch segment's
  nine-branch precedence (including `awaiting_new_session` masking `busy`), YOLO
  winning the `edits` slot over `auto`, `armed` keeping its own slot so a
  disarmed YOLO session is visible as the pair it is, and the `◆ SUB-AGENT`
  rebadge. A segment that must hide is **absent from the list**, which is how
  the TUI hides `armed`/`instr`/`mcp` too — by not being drawn.
- `{type: "sidebar"}` / `{type: "mcp"}` / `{type: "detection"}` carry the PROJECT,
  SERVICE, CHAT WINDOW, MCP and DETECTION blocks already worded. The DETECTION
  block keeps its exclusive owner: `GuiView._paint_detection` is the only writer
  and every exit of `_start_detector_worker` — including the two that start
  nothing — leaves the five lines saying what just became true.
- `{type: "harness"}` gained the rendered `line`, so the fixed-width kind column
  is `HarnessEntry.line`'s decision on both sides. The pane keeps its own tail
  bounded at `HARNESS_LOG_MAX`, follows the bottom only while it is already
  there, survives `/new`, and is flipped by F8 or by `/log` (which arrives as
  `{type: "toggle", what: "log"}` — one implementation, two doors).

Keys: F3 (sidebar) and F8 (log) never leave the page; F5/`/armed`, `shift+tab`,
`w`, `c`, `i`, `r`, the service picker and the retry button each get one typed
`js_api` method that marshals onto the loop and lands on the same controller
call the TUI's binding makes. `c`'s double tap needed nothing here —
`SessionController.recopy` owns the 1.5s window on both sides.

Three deliberate divergences, recorded rather than smuggled:

- **The paste flash stays a full-width banner** instead of moving into the
  sidebar where the TUI keeps it. A 300px column in a 1200px window is the
  quietest place on screen and this banner's whole job is to be seen — and F3
  must not be able to hide the thing asking for a keystroke. It blinks (a CSS
  animation, not a 0.4s timer) and the `Retry insert` button rides with it,
  shown only under the Ctrl+V variant, exactly as `retry=True` says.
- **Three sidebar lines drop their "F2"** (the appearance summary, `STALE_OFF`,
  `PROBE_UNCAPTURED`). The diagnosis is verbatim; the key is not, because this
  shell has no service editor behind F2 until increment 7 and naming a key that
  does nothing is worse than naming none.
- **`w`/`i`/`r` refuse out loud.** `check_action`'s three-way footer dimming
  (§6.6 of the keys brief) has no equivalent here — there is no footer to hide a
  binding from — so a key that cannot fire toasts why instead of being silently
  absent. The three states are preserved as *messages*, not as a collapsed
  "disabled".

Two things landed alongside because the increment is unusable without them: the
composer's **two-stage Esc** (clear-with-undo, then blur — stages 2 and 3 of the
Esc machine), since the bare-letter keys are unreachable while the composer holds
focus; and the **service picker driving the real config path**
(`AutomationController.set_service` + `save_active_services` + a detector
rebuild), locked while a session owns the preset. The picker edits the MASTER
window only — there is no tab bar to select another one until increment 5, which
is also where the CHAT WINDOW block grows the sub-agent's readiness note. The
"Set chat region..." button toasts: calibration lands with the elements panel.
(Both halves of that sentence were made good by increment 3: the picker edits
whichever window the tab bar has selected, and the CHAT WINDOW block carries the
sub-agent's readiness note.)

**Parity increment 3** — the WINDOW TABS, the DELEGATION VIEWS and the SESSION
SUMMARY, against `docs/design/ui-briefs/tabs-delegation-summary.md`. Nothing new
crossed the ports and no core change was needed; the three reduced session-view
methods became the real ones, and one more family of *decisions* crosses with
the data:

- `{type: "tabs"}` carries both rows (masters, then the selected master's
  sub-agent windows), the one selection that spans them, and per tab the
  composed `label`, the `service` and the `state`. The state is **derived from
  the window's run history, never stored** — none / `running` / `ok` / `failed`,
  last run only — because "how did the last run in this window go" is one rule
  and two copies of it would drift. The glyphs (`▶`/`✓`/`✗`) ride in the label
  for parity of wording; the page also colours from `state`, which is the half
  the brief actually requires (§7: a GUI may badge this however it likes).
- **A tab is a browser WINDOW, not a session view.** It exists before any
  session, keeps its own service, and `/new` keeps both tabs and both
  calibrations while forgetting the runs — the sub-agent tab drops its `✓`
  because the runs are gone, not because the window is.
- **Three pointers, not one**, and this is the whole of the surface's
  correctness: `_selected_window` (what is shown, what the sidebar configures),
  `_focused_window` (where `add_*` lands, moved only by `focus_session_view`),
  and the automation's live slot (what is driven, never moved by a tab). Every
  `transcript` event now carries its `window`, so output keeps landing in the
  sub-agent's panel while the user reads the master's.
- The **sidebar's SERVICE and CHAT WINDOW blocks now describe the SELECTED
  window** — the picker writes into that window's slot, the appearance summary
  and region are its, and the readiness line is `slot_note`'s two-input answer
  (the window's box, and what *that tab's* service looks like). DETECTION keeps
  naming the LIVE window: a different pointer, and they part company for the
  whole of a delegation.
- The **summary** is the existing modal grown up: the stats rows, the
  `task_done` markdown (with the "no summary" placeholder), four buttons and the
  four single-letter keys. `e` is gated exactly as `check_action` gates it
  (session active, not busy, `AWAITING_REPLY`/`DONE`) and refuses out loud, per
  increment 2's divergence. The code's behaviour is the contract, not `tui.md`
  §1.5's older prose: `u` undoes **one** turn behind a confirm and returns to
  the chat; `l` writes the log and re-opens the summary (a loop); `escape` is
  "back", not "end".

Two small things landed with it, both consequences rather than additions: the
export is per RUN again (`render_log` slices `_sub_runs` out of the sub-agent
window's log under one `## sub-agent: <title> (<chat>)` heading each), and F6
is kept even though a DOM tab strip needs no hotkey — the composer holds focus
for most of a session. Deferred deliberately: the top-level `u`/`l`/`t`
bindings (they belong to `modals-keys-esc.md`, increment 8), and any N-window
chrome — one master and one sub-agent is the current real contract (brief §4 of
the ambiguities).

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
