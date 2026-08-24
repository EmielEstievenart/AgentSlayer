# GUI wave — core + two UIs

Status: binding. Written 2026-08-14, at the start of the GUI wave. This doc records
the decisions; the per-surface behavior contracts live in `docs/design/ui-briefs/`.

> **There is one shell now (2026-08-24, `ui-monitor.md` §6.6).** The Textual TUI
> — `agentclip.shell.tui`, its Pilot suites and the `textual` dependency — was
> **deleted**; `--tui` survives one release as a stub that prints "the Textual
> TUI was removed in this release; plain agentclip opens the GUI" and exits 2.
> This document is unchanged below except for the parity policy, which §2.12
> already amended, and it is still binding for the GUI: every decision it
> records about the core, the ports and the window is the code. Read every "the
> TUI does X" in it as **history** — the sentence that made a decision, kept
> because deleting it would delete the reason. Nothing below describes code that
> still exists on that side of the comparison.

## 0. The shape

AgentClip became **one core with two UI shells** (and, since `ui-monitor.md`
§6.6, one core with one — the core survived the shell it was extracted from,
which is what the two ports were for):

- the existing Textual TUI (`agentclip.shell.tui`) — stayed, unchanged in
  behavior, until phase 6 deleted it;
- a new desktop GUI built on **pywebview + WebView2** (`agentclip.shell.gui`) — a native
  window rendering an HTML/CSS/JS frontend, Python in-process.

Both shells drove the same two UI-agnostic controllers:

- `SessionController` via the `ChatView` port (`agentclip/shell/app/view.py`) — exists today;
- `AutomationController` via the `AutomationView` port
  (`agentclip/driver/automation/view.py`)
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

> **Amended 2026-08-24 by `ui-monitor.md` §2.12 (its phase 0), and closed by its
> §6.6 the same day.** The question this section left open — "whether the TUI
> eventually freezes" — was answered: it froze, and then phase 6 **deleted** it.
> There is no parity contract left to keep, because there is nothing to keep
> parity with. What follows replaces the original two-shell contract.

- The briefs in `docs/design/ui-briefs/` are the behavior contract for **the GUI**,
  full stop. They were a two-shell parity contract; the second shell is gone.
- Features still land **core-first**: new behavior goes into the engine, `shell/app`
  and the Driver, then the GUI grows its view of it. That rule outlived its
  original justification and is kept on its own merits — it is what let one of
  the two shells be deleted without the core noticing. A feature that exists only
  in the GUI's view layer is **not a design smell** and needs no written exception.
- Known carve-outs — the *core* ones; the rows that were only "the TUI does
  X, the GUI does Y" went with the parity contract that made them matter:
  - the chunked-send wizard (`tui.md` §6) is designed but not implemented anywhere
    (controller short-circuits multi-chunk; M3). The GUI targets current behavior;
    the wizard lands core-first later.
  - the two core seams the TUI's OSC-52 clipboard fallback bought stay, because
    they are core API and not shell trivia: `AutomationHost.park_off_clipboard(text)`
    is where a payload the clipboard provider refused crosses back to a shell (the
    GUI does nothing with it), and `AutomationController.deliver` takes
    `clipboard_ok: bool` so the delivery is TOLD how the payload got parked rather
    than re-reading a clipboard that may not be where it went — which is what makes
    the streamed mode fall back to a single burst.

## 1. The automation package — the Driver's core (phase 0)

> **Amended 2026-08-24 by `ui-monitor.md` §6.1 (its phase 1).** Half of what
> this section put in `driver/automation` moved one layer down into the new
> `driver/monitor`: `ScreenOps` is `driver/monitor/ops.py`, the delivery beats
> are `driver/monitor/beats.py` (re-exported by `driver/automation/delivery.py`
> under their old names), and the poller thread, the trackers, the generation
> stamp and the clipboard watcher are one object's now,
> `driver/monitor/local.py:LocalUIMonitor`. The controller *consumes* a
> `UIMonitor` instead of owning any of that. Nothing about the decisions below
> is reversed — `AutomationView` and `AutomationHost` are untouched, and the
> paint contract still holds — but read a `driver/automation/ops.py` or a
> thread-ownership claim below as history. The GUI's own `_BUSY_POLL_S`,
> `build_detector` call and detector-poller mirror in `view.py` are what
> `ui-monitor.md` §6.1's shell rewire deletes; until that lands they are still
> there, and §6.1's status note is the ledger of what is owed.

Decisions ratified from the extraction plan:

- New package `agentclip.driver.automation`, sibling of `shell/app`, holding
  `AutomationController` + the `AutomationView` protocol. Allowed imports:
  `agentclip.driver.screen`, `agentclip.driver.clip`, `agentclip.config`, itself.
  Banned: `textual`, `agentclip.shell.app`, `agentclip.shell.tui`. It is OS-coupled
  core, shared by both shells — the same flavor of layer `driver/screen` and
  `driver/clip` already are, and with them it is what architecture.md §0 now names
  the **Driver**.
- **`AutomationView` is paint-only**: `paint_loop_state`, `paint_harness_entry`,
  `paint_detection`, `paint_stale`, `paint_elements`, `paint_armed`,
  paste-flash show/hide, `notify`.
  Same thread contract as `ChatView.call_*`: methods may be called from poller/watcher
  threads, implementations must be non-blocking and thread-safe (the TUI posts a
  message; the GUI enqueues to its JS bridge). No OS primitives on the port — the
  controller imports `agentclip.driver.screen.focus` etc. directly. No scheduling primitive
  on the port — the controller owns its own `threading.Thread`s.
- **`os_armed` moves into `AutomationController`**, reversing the "view-owned" note in
  `shell/app/view.py:202-212`. With two shells, one shared armed flag below both is the only
  non-drifting design. `ChatView.set_os_armed` stays; `MainScreen` passes through.
- Probe ordering: the poller calls the busy→idle→stale decision sequence synchronously
  in one call stack per tick (the "closing message" invariant becomes trivially true —
  no queue between producer and consumer).
- The session/window bookkeeping duplication (`MainScreen._sessions`/`_sub_runs` vs
  `SessionController._active`/`_sub`) is **deferred** — it is transcript-display
  state, not automation. Follow-up ticket, not this wave's phase 0.
- New no-Textual test suite `tests/driver/automation/` (FakeAutomationView, mirroring
  `tests/shell/app/`); all existing `tests/shell/tui/*_ui.py` Pilot tests are **kept** as the
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
   `driver/automation/ops.py:ScreenOps` (since `ui-monitor.md` phase 1:
   `driver/monitor/ops.py`, reached as `monitor.ops`), whose default
   implementation *is* the direct `agentclip.driver.screen` call — the Textual
   suites monkeypatch those names at `shell/tui/screens/main.py`'s scope, so
   the shell hands in a subclass that resolves its own module's names per call
   (slice 4's `_poll_capture`, generalised).
   (b) What the sequences still have to ASK a shell is a second port,
   `driver/automation/host.py:AutomationHost` — the live preset/profile,
   `find_all`, the verified copy click, the prose ingest (the session is
   `agentclip.shell.app`, above this layer), the detector rebuild. It is
   deliberately NOT folded into `AutomationView`: that port's contract is
   "callable from a poller thread, never blocking", and a host is only ever
   called from the event loop. Paints
   the flow needed grew no new port methods — the copy-status line is
   `paint_detection(COPY, …)` and the harvest's crop is `paint_elements`, so
   both now carry the paint epoch. `_own_window` became `set_own_window` on the
   controller (OS state, both shells snap back to it) with a read-only
   compatibility property on `MainScreen`. The snapping back became the
   controller's whole and only too: `snap_back_after_click()` is the
   `SNAP_BACK_SETTLE_S` beat plus the verified `snap_focus_back()`, no-opping
   without a handle, and `MainScreen` dropped its one-line wrapper - there was no
   shell decision left inside it. **That closed a parity gap the GUI had been
   carrying**: `_new_browser_chat` here never handed the focus back, so a
   `/new` in the GUI left the user staring at a fresh browser chat while the tool
   window sat behind it. Both shells now make the same call on a click that
   LANDED, and neither makes it on one that did not - a refused new-chat click
   leaves the browser focused, because that is where the user has to finish the
   job (tui.md §3.3a).
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
   reads. The banner's words moved into `driver/automation/delivery.py` with the beats,
   for the port's own "text, not decisions" rule; the sidebar re-exports them.
   The two synthetic keystrokes and the delivery's beats and one chunk
   size joined `ScreenOps`, which is what keeps the Pilot suites' patches biting
   at `main.py`'s scope - and so did the one READ the delivery needs,
   `foreground_window()`, when the blind settle in front of the Ctrl+V grew an
   activation poll in front of *it* (`_await_browser_activation`;
   `ACTIVATION_ATTEMPTS`/`ACTIVATION_POLL_S`/the raised `PASTE_SETTLE_DELAY` are
   all `ScreenOps` calls for the same reason as the rest, tui.md §3.4b). Only the
   SCHEDULING stayed in the shell (`run_worker`
   for the retry button and for `c`'s second tap), the slice-6 arrangement
   unchanged; `redeliver_outbound`'s two refusals are the controller's
   `may_redeliver`. `deliver` also owns where the FOCUS ends up, which is a
   decision and therefore below both shells: it snaps back to the tool window on
   the auto-sent outcome alone, and leaves the browser focused on the two whose
   banner is asking the user for a keystroke there.
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
`agentclip.driver.automation` and its three seams, and `tui.md` points at where each
moved name went. What is left on `MainScreen` is the shell: tabs, transcripts,
sidebar routing, the paint handlers, the `run_worker` scheduling, and the
compatibility seams the Pilot suites patch.

## 2. The GUI shell

- Package `agentclip.shell.gui`; entry `agentclip --gui` (same binary; TUI stays the
  default until the GUI reaches parity — flipping the default is a later decision).
- **No Node toolchain.** The frontend is hand-written JS + CSS shipped as
  package data; no build step, no npm. If we ever outgrow this, that's a new decision
  here first.
- pywebview is the optional extra **`gui`** (`uv sync --extra gui`, `pip install
  agentclip[gui]`), the same shape as `cv` and `mcp`: `shell/gui/shell.py` imports
  `webview` inside its functions and `cli.main` imports the shell only on `--gui`,
  so a TUI launch never pays for it and an install without the extra still runs —
  it exits 2 naming the extra. The extra is in the `dev` group too, so the suite
  can exercise the real toolkit. `tests/test_layering.py` gives `agentclip.shell.gui` its
  own RULES entry (the TUI's reach minus Textual, plus `webview`), adds it to
  `CLIP_SCREEN_IMPORTERS`, keeps it OUT of the textual exemption, and names two
  boundary tests: `test_gui_never_imports_textual`,
  `test_pywebview_only_in_the_gui_shell`. Slice 2 added `agentclip.engine` to
  that entry, and only for its VALUE types: `Decision` is what an approval
  answer IS (the same call the TUI makes), `PendingAction` is what a gate is
  handed, `Engine` is what the factory `cli.py` builds returns. A shell that
  could not name them would have to re-declare vocabulary its own controller
  already speaks — which is the drift the ports exist to prevent — and
  `agentclip.shell.app` already depends on that layer, so the direction is unchanged.
- The assets are located with `importlib.resources` (`files("agentclip.shell.gui") /
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
  **Shipped in slice 2 (`agentclip/shell/gui/bridge.py`), with the pywebview facts the
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

### The GUI's concurrency model (slice 2, `agentclip/shell/gui/runner.py`)

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

### Three GUI-side decisions the TUI has no equivalent for

- **Enter sends, Shift+Enter is a newline — and so is `ctrl+j`.** The TUI uses
  `ctrl+j` because Enter is its send key inside a Textual `TextArea`; the GUI
  leads with the web-native convention every chat composer has, but honors
  `ctrl+j` too so the muscle memory transfers between shells. The hint strings
  name only Shift+Enter (one chord per hint; `ctrl+j` is the help sheet's job).
- **`AutomationHost.park_off_clipboard` has no OSC-52 to fall back to.** The
  TUI writes the terminal escape (§0); a WebView2 window has nothing like it,
  and writing back through the page's own clipboard would be the same refused
  write one layer up. The GUI's honest equivalent is to **show** the payload in
  a selectable `<pre>` block with a toast saying the copy is theirs to make (and
  naming `.agentclip/sessions/<id>/outbound/` as the other way to it).
- **Text is selectable, and the chrome opts out.** A terminal's selection
  belongs to the terminal emulator; a window's belongs to us, and pywebview's
  default is to switch it off by injecting `body { user-select: none }` after
  the stylesheet has loaded — which no rule in `app.css` can outrank. So
  `create_window` is passed `text_select=True` (`shell.WINDOW_TEXT_SELECT`, and
  `tests/shell/gui/test_shell.py` pins it on a real window), and the page
  answers the question per element: everything the user *reads* — transcripts,
  code blocks, the gate preview, run output, the log pane, sidebar status,
  modal bodies — can be dragged over and copied with Ctrl+C, and only *click
  targets* (the titlebar, tabs, buttons, the key-hint strip, the run panel's
  toggle line, checkbox labels) turn selection off again for themselves. The
  two handlers that would eat a selection anyway are guarded: the run panel's
  click-to-toggle ignores the click that ended a drag, and the state push that
  puts the caret back in the composer skips it while a selection is live.

### How big the window may be

The window is freely resizable and the layout owes it a usable page at any size
it can reach. The policy, in `shell/gui/shell.py` and `assets/app.css`:

- **Minimum `400×300`.** Chosen so the window can take *half of a small
  portrait monitor* — half of a 900×1400 panel is 450×1400 split vertically and
  900×700 split horizontally, and both must be legal. A minimum that forbids
  those forbids a layout the stylesheet already knows how to draw.
- **The `1200×800` default is clamped into the primary screen** before
  `create_window` sees it (`initial_window_size`, less `SCREEN_MARGIN` for the
  taskbar and the frame), so a 1200-wide default never opens a third of itself
  off a 900-wide panel. A screen pywebview cannot enumerate — headless, an
  unfamiliar toolkit — leaves that axis at the default rather than failing.
- **Two breakpoints do the reshaping**, and nothing above them changes. The
  side columns (F3, F7) trade their fixed widths for a `clamp()` share of the
  window, and **below 640px they stop being columns**: they float over the chat
  right-anchored, still shown and hidden by the same toggles, because they are
  still only `[hidden]` to `app.js` — no script was needed. The wide dialogs
  stack their lanes into one scrolling column instead of squeezing them (the
  service editor below 860px, the connect dialog below 560px).

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
3. ~~**`show_identify_overlay`.**~~ **Done in parity increment 4** — the same
   `--show-identify` child process the TUI shells out to, gated on the live
   window having a region and refusing out loud when it does not.
4. ~~**`paint_elements`.**~~ **Done in parity increment 4** — `crop_elements` is
   wired in, and what crosses is one row per kind with a PNG data URI per
   matched crop.

**The list is empty.** Every `ChatView`/`AutomationView` method this shell
implements is now the real one; what is left of the parity backlog is one whole
SURFACE (the SSH dialog), not methods implemented smaller than their contract.

~~One duplication is tracked with them: `shell/gui/view.py:_distinct_rects` and
`find_all`.~~ **Done, in its own commit.** The fold is
`driver/automation/flow.py:distinct_rects`, the union around it is `flow.element_rects`
(the search passed in as `ScreenOps.all_matches`, the way `lowest_match_scored`
already took `ScreenOps.lowest_match`), and the whole sequence — refusals,
calibration lookup, capture, union, fold — is
`AutomationController.find_all`. Both shells' `AutomationHost.find_all` is a
one-liner onto it. The HOST method stays: it is the seam the Textual suites stub
to put an appearance on an imaginary screen, so the sequences keep asking
through `self._host` rather than calling the controller's own method.
- Images (elements panel, service-editor thumbnails): PNG data URIs per crop.
  The crop-not-whole-frame policy and the BGRX rule carry over; the
  sixel/half-block machinery does not. The encoder is `driver/screen/png.py` (stdlib
  zlib, already in the layer, and it already writes the capture's undefined
  fourth byte as opaque alpha), so the GUI needs neither Pillow nor anything out
  of `shell/tui/graphics.py`.
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
6. ✅ elements panel — **parity increment 4**, with the chat-region picker and
   `/identify` (the two surfaces the panel is useless without: nothing is
   searched until a window is drawn).
7. ✅ service editor — **parity increment 5**.
8. ✅ settings / help / modals — **parity increment 6**, with the slash popup,
   the whole key chain and the quit gate.
9. ✅ SSH connect dialog — **parity increment 7**.

**The wave is complete.** Every surface in `docs/design/ui-briefs/` now exists in
both shells, except the one that deliberately exists in only one (§0's carve-out,
and item 9 above is it).

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
their state is. *(Made good: `i`/`c`/`shift+tab` in increment 2, `e` in
increment 3, and the popup, the prune and `u`/`l`/`t` in increment 6 — the
keys brief, not the rail, turned out to be where the last three belonged.)*

**Parity increment 2** — the SIDEBAR, the STATUS BAR and the HARNESS LOG, against
`docs/design/ui-briefs/sidebar-status-log.md`, plus the keys whose state those
three surfaces display (`modals-keys-esc.md` §5.1). Nothing new crossed the
ports and no core change was needed; what changed is that four more families of
*decision* cross with the data:

- `{type: "rail"}` carries the eight `LoopState` rows with `LOOP_TRANSITIONS`
  already applied (`active` / `legal` / `dim`). The table is the automation's
  vocabulary and the page has no business holding a copy of it; it stays
  display-only on both sides, as `loop_state.py`'s header requires.
- `{type: "status"}` carries the eleven segments **composed and in order**, because
  every one of them is a rule rather than a style: the watch segment's
  nine-branch precedence (including `awaiting_new_session` masking `busy`), YOLO
  winning the `edits` slot over `auto`, `armed` and `unattended` keeping slots of
  their own so a disarmed YOLO session — or an auto-approving and auto-denying
  one — is visible as the pair it is, and the `◆ SUB-AGENT` rebadge. A segment
  that must hide is **absent from the list**, which is how the TUI hides
  `armed`/`instr`/`unattended`/`mcp` too — by not being drawn.
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
  sidebar where the TUI keeps it. That column is the quietest place on screen —
  a quarter of the window at best, and on a narrow window it is not even in the
  flow (it floats over the chat) — while this banner's whole job is to be seen;
  and F3 must not be able to hide the thing asking for a keystroke. It blinks (a CSS
  animation, not a 0.4s timer) and the `Retry insert` button rides with it,
  shown only under the Ctrl+V variant, exactly as `retry=True` says. It is an
  **overlay hanging off the titlebar's bottom edge**, not a row in the flow:
  it lives inside `<header class="titlebar">` and is positioned `top: 100%`
  across the full width, so the height it hangs at is the titlebar's own and
  no number in the stylesheet, and raising or dropping it — which happens
  repeatedly inside one turn — reflows nothing. In the flow it shrank `.body`
  on every appearance and walked the transcript, the run panel and the composer
  up and down with it, which is exactly what a banner asking for a keystroke
  must not do. A shadow (`--shadow`) says "above the page"; its z-index (15)
  sits over the columns and under the toasts and every scrim.
- **Three sidebar lines drop their "F2"** (the appearance summary, `STALE_OFF`,
  `PROBE_UNCAPTURED`). The diagnosis is verbatim; the key is not, because this
  shell has no service editor behind F2 yet and naming a key that does nothing
  is worse than naming none. *(Reversed in increment 5, which is that editor.)*
- **`w`/`i`/`r` refuse out loud**, *and* there is now a footer to dim them in.
  When this increment landed there was none, so `check_action`'s three-way
  dimming (§6.6 of the keys brief) survived only as *messages*: a key that could
  not fire toasted why instead of being silently absent. The toasts stay — they
  are the only thing that answers the press itself — and the **KEY HINT strip**
  above the status bar now carries the three states as the brief requires:
  - **normal** — the key fires now.
  - **dimmed** — it does not, but it will: `u`/`e` mid-turn, `i` outside
    `AWAITING_REPLY`, `c` with nothing outbound yet, `l`/`r` with no session,
    `ctrl+x` with nothing running, `y`/`n`/`a` with no gate up. Dimmed, never
    dropped, so the strip's rows never move under the eye.
  - **hidden** — it never can, in this mode: `w` while disarmed or in
    manual-clipboard mode, which is the same `False` the TUI returns.
  The strip is painted from the one `KEYS` table the dispatcher reads and F1's
  sheet renders (`foot` is the TUI's `show=`, `avail` is `check_action`'s
  three-way answer), out of the `state`/`status`/`run` pushes the page already
  receives — nothing new crosses the bridge for it. The one gate this side
  cannot see is `r`'s *hidden* branch: whether the active service carries extra
  instructions is not on any push, so the key is shown and the refusal stays
  Python's toast. A row is dimmed for a second reason the TUI has no need of:
  while a text box holds the caret the bare-letter keys are inert (they are
  going into the sentence being typed), and the strip says so on focus in and
  out. Nothing in it is focusable or clickable, and it is drawn at all times so
  its height is reserved — a hint bar that came and went would be the flash
  banner's mistake at the other end of the window.

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
sub-agent's readiness note. The region button stopped toasting in increment 4 —
it runs the real `--pick-region` child now.)

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
bindings (they belong to `modals-keys-esc.md`, increment 8 — and landed there),
and any N-window chrome — one master and one sub-agent is the current real
contract (brief §4 of the ambiguities).

**Parity increment 4** — the ELEMENTS COLUMN, the CHAT-REGION PICKER and
`/identify`, against `docs/design/ui-briefs/elements-panel.md`. The three land
together because the column is a picture of searches that only happen inside a
drawn window, and until this increment the GUI could not draw one.

- `{type: "elements"}` carries **one row per `TemplateKind`**, in
  `RUNTIME_KINDS` order, each with its `label`, its `state`
  (`resting`/`missing`/`found`), the verdict `text` and — on a found row — a
  `png` **data URI**. The three states are the brief's and are not
  interchangeable: a kind ABSENT from a tick's map has never been searched (its
  service has no capture of it) and keeps whatever its row said; present-and-null
  was searched and is not on screen; present-and-crop carries the diff and the
  picture. All seven rows are searched every tick regardless of which finish
  signals are ticked — the column is a picture of what the tool can SEE, not of
  what the automation decides from — and `window` names the LIVE window, which
  parts company with the selected tab for the whole of a delegation.
- **PNG data URIs replace the entire sixel/half-block machinery** (brief §7).
  What carries over is the crop policy (the matched rectangle only, cut on the
  poller thread that captured the frame, via the `crop_elements` seam) and the
  **BGRX-not-BGRA** rule. The GUI needs no Pillow for either: `driver/screen/png.py`
  already encodes a capture as RGBA with the undefined fourth byte written as
  OPAQUE alpha, which is exactly the rule — read as alpha, that byte is zero and
  every crop encodes invisible. No mode readout line exists here, deliberately
  (a recorded divergence: it described sixel vs half-block, and a page can
  always show real pixels).
- **F7 is the one page-side toggle that tells Python.** F3 and F8 are pure
  show/hide; this one is too, but the encoder is gated on it — a PNG per matched
  appearance twice a second for a column nobody is looking at is the only part
  of this surface that is not free. The crops keep being cut and kept while it is
  hidden, so opening the column paints the CURRENT tick rather than the next one,
  and a crop whose bytes are unchanged is not re-encoded.
- **The picker and `/identify` reuse the child-process overlays unchanged**
  (`driver/screen/picker.py`: `--pick-region`, and `--show-identify` fed JSON on stdin).
  Both brackets are the TUI's: the detectors are suspended for the whole visit
  (a fullscreen window over the browser they watch is the sustained delta that
  arms the finish trigger on staleness alone), only one such child may be up at
  a time, the target slot is frozen when the overlay opens rather than re-read
  after it closes, and the poller is rebuilt only when the window just drawn is
  the one it is watching. `/identify` searches with the poller's own tolerance
  and matcher, captures BEFORE the overlay exists, and toasts its summary after
  it is down.
- **The GUI window is NOT minimised around either overlay** — tried without
  first, as planned, and kept: the overlay is fullscreen-topmost across the whole
  virtual desktop, so it is over this window either way (the TUI leaves its
  terminal up for the same reason), and a restore would fight the user for focus
  right after they drew a box around their browser. `window.minimize()` stays
  unused.

One **core change** was needed and it is the smallest one available:
`crop(image, x, y, w, h)` — the cutter, a pure function over a captured buffer —
moved from `shell/tui/pixels.py` to `driver/screen/capture.py`, which owns
`RegionImage`. `shell.tui.pixels` re-exports it, so every existing caller and test
is untouched; `driver/screen` gained no new dependency (the move is stdlib-only,
and no Pillow came with it), so `tests/test_layering.py` needed no allowance. `region_to_pil` and
the rest of `shell/tui/graphics.py` did NOT move: they are the sixel path's, and this
shell has no use for them.

Two things landed alongside because the picker made them reachable: the
sub-agent's **one-shot "slot ready" toast** (`MainScreen._after_calibration`'s
half that a repaint cannot carry — the delegate tool is baked in at bootstrap,
so a window that just became ready reaches the model on the next `/new`), and
the sidebar's "Set chat region..." button becoming real, which was increment 2's
last standing toast. Deferred deliberately: the service editor behind F2 (which
landed as increment 5) — the appearance-summary lines still name the door rather
than the key — and any GUI-native overlay, which the brief's open question 1
leaves to the child process on purpose.

**Parity increment 5** — the SERVICE EDITOR (F2), against
`docs/design/ui-briefs/service-editor.md`. The first surface whose MODEL was
extracted rather than re-implemented against widgets: `shell/gui/service_editor.py`
holds the working copy, the validation, the commit models and the capture
orchestration with no window, no page and no toolkit in it, and `shell/gui/view.py`
plus `app.js` hold only the two things a model cannot do (schedule the capture
coroutine, and draw). It is why the editor's ~600 no-window tests exist at all —
the TUI's equivalent needs a Pilot and a 120×45 terminal for the same
assertions.

- `{type: "editor"}` carries the WHOLE surface in one event, `open: false` being
  the closed state rather than a second type. Two fields are the page-side
  contract. **`reload`** is true only after a form RELOAD (a selection, an add,
  a reset, a delete) and false after a keystroke: the model owns the form's
  values, but repainting a text box from it on every input event would fight the
  caret. **`controls_disabled`** is the "+ add new" state (brief §3.5) — the
  toggles, radios and slider are DISABLED, not blank, and keep showing what
  "Add service" would create.
- The two commit models are the TUI's, exactly: an existing preset applies
  **live** on every change that validates as a whole candidate (`max <= total`
  is a cross-field rule, so per-field validity does not exist), an invalid
  candidate is **never** committed and the working copy keeps its last-valid
  values, and a new preset waits for the one discrete "Add service" press
  because a key is immutable once created. The toggles/radios/slider write
  through with no validation gate at all — none of them can express an illegal
  value.
- The seven APPEARANCE rows are real PNG data URIs (`driver/screen/png.py`, the same
  BGRX-as-opaque-alpha rule the elements column needs), encoded only when the
  profile folder is re-read — a selection, a capture, a clear, a forget — never
  per keystroke. Captures run the same `pick_region` child process increment 4
  wired, write to the store immediately, ADD a variant rather than replacing
  one, and the second press is refused **out loud** rather than raced (the claim
  is synchronous, before the coroutine is scheduled, because two js_api presses
  marshal onto the loop as two callbacks).
- Each row is a **window onto that kind's stack**, not a picture of a slot: an
  arrow either side of the thumbnail walks the variants (wrapping, disabled
  below two), the status line names the position (`"24×12 · 2/3"`) with the
  SHOWN variant's dimensions, and "Clear" drops **that one image** rather than
  the kind — the stack is how one control drawn several ways is recognised in
  all of them, and a bad third capture should not cost the two good ones.
  "Forget appearance" is still the whole-service door. Which variant is showing
  is the Python model's state (`ServiceEditor._shown`, crossing as `shown` /
  `count` per row) rather than the page's, like everything else in this modal:
  it is clamped against the folder on every re-read, so a stack that shrank
  under a stale index shows its new last variant instead of a hole. A capture
  lands ON the variant it just drew. This diverges from the TUI — see below.
- The save is `AgentClipApp._open_service_editor`'s, step for step:
  `save_services` minimal-write into the shell's own `global_config_path`,
  `replace(config, services=...)`, `SessionController.update_config`, the
  per-run profile cache dropped, any window pointed at a deleted service
  re-pointed, and the detector poller rebuilt — with the detectors suspended for
  the **whole visit** and resumed in a `finally`, because a capture throws a
  fullscreen overlay over the browser they watch.

Four divergences, recorded rather than smuggled:

- **The appearance row walks its kind's stack, and "Clear" drops one image.**
  The TUI's row shows `variants[0]` and its Clear calls `drop_template`, which
  wipes the kind; this shell's row has an arrow either side of the thumbnail,
  names the shown position in the status line, and clears the shown variant
  through a new `profile_store.drop_variant`. The reason is that a stack is a
  set of pictures the user has to be able to LOOK at to debug a match, and a
  frontend that can only show the first one makes the other variants
  unreviewable and unfixable except by recapturing all of them. Recorded in
  the brief itself (§2.7, §3.6, §5.5, §6), because it is a behavior change and
  not a rendering one — the TUI is free to follow later.
- **The tolerance control is a real `<input type="range">`.** The TUI's is a
  bespoke track+handle widget with arrow-key nudging, because Textual ships no
  slider; brief §7 says to use the platform's own, and this is it. Only the
  semantics carry over (0-64, default 24, live-apply, the number beside it).
  Trivially, and named here only because §7 asked for the swap to be recorded.
- **Increment 2's "three sidebar lines drop their F2" divergence is
  REVERSED.** There is a service editor behind that key now, so the appearance
  summary, `STALE_OFF`, `STALE_UNTICKED` and `PROBE_UNCAPTURED` are the TUI's
  words again, and the sidebar grew the "Edit services..." button the TUI's
  has.
- **`cli.py` hands the GUI a config CELL, not a config.** The TUI's engine
  factory closure reads `app.app_config`, an attribute its editor reassigns;
  the GUI's window does not exist at the line where the factory is built, so
  the cell lives in `main()` and the shell writes it back through
  `run_gui(..., on_config_change=...)` → `GuiView._adopt_config`. Both shells
  therefore build the NEXT session's Engine from whatever the editor last
  saved, and neither touches a session already in flight. The previous
  `lambda: config` was correct only while this shell had nothing that could
  rebind one.

One thing is deliberately smaller than the TUI's and says so here: a
`save_services` that raises `OSError` **toasts and still applies in memory**
(the TUI lets it propagate). It is the same trade `_persist_services` already
makes for the service picker — remembering a pick is a convenience, never the
point of the press.

**Parity increment 6** — HELP (F1), SETTINGS (F4), the COMPOSER'S SLASH POPUP,
the whole KEY table and the six-stage ESC chain, and the quit-mid-turn gate,
against `docs/design/ui-briefs/modals-keys-esc.md`. Two more families cross
(`commands` and `settings`, both once per page load) and one core file changed —
`config.py`, for a `[gui]` table; everything else is view and page.

- **One table feeds the key handler and the help sheet.** `app.js`'s `KEYS` is
  the dispatcher's only door and the sheet's key rows are rendered from it, so a
  binding cannot exist undocumented or be documented without existing; a test
  asserts the absence of any second dispatch path rather than the presence of a
  string. The table is **this shell's**, recorded divergences included
  (Shift+Enter is the newline, F1/F4 open page screens, `t` never leaves the
  page). The COMMAND rows come the other way: `{type: "commands"}` carries
  `agentclip.shell.app.commands.COMMANDS` verbatim, so the popup and the help sheet
  read the same registry `/help` and the controller's dispatch do. The filtering
  rules are page-side — a round trip per keystroke would be latency for string
  work — the data never is.
- **The Esc chain is the brief's, in the brief's order**, split across the two
  dispatchers that own it: stages 1-3 (popup / clear-with-undo / blur) on the
  composer, stages 4-7 (reject note / modal-local / dismiss a pending `ask_user` /
  no-op) on the document. Stage
  5 is *checked* first in the document handler and that is not a reordering — a
  GUI modal traps focus behind its scrim, so stages 1-3 cannot be live beside
  one, and Textual's screen stack gives the TUI the same guarantee. **No stage is
  skipped**: every surface the brief names exists here. The two handlers are
  named functions rather than inline closures precisely so the order is
  readable and assertable. `ev.defaultPrevented` is what stops a composer Esc
  from also firing stage 4 on the way up.
- **Stage 6 dismisses a pending `ask_user`** (`modals-keys-esc.md` §3.3, `tui.md`
  §3.3e) — a GUI where the only exit from a question is typing something the
  model will read as an answer is a trap. It is last before the no-op on purpose
  — the composer is auto-focused while a question is open, so stage 3 spends the
  first press letting go of the box and the press that dismisses is never the
  press that meant to. It **sends nothing**: `dismiss_pending_question()` leaves
  the answer future open and the engine parked in `AWAITING_USER` (its one exit
  is `answer_user`, so poisoning would strand it — that is safe only for `/new`,
  which always resets the session behind it), drops `awaiting_answer` so the
  banner and answer mode go, and lets the next ordinary message resolve the park
  with the declined-prefix in front of it. Both shells have this stage now; the
  TUI's sits one place earlier in its own chain (dismiss before blur), because
  its composer is not auto-blurred by anything.
- **The `ask_user` question gets a panel of its own** (`#ask-banner`), pinned
  above the composer, the approval gate's structural twin: a question is a stop,
  and a transcript note scrolls away while a stop must not. **A GUI/TUI
  asymmetry, recorded**: the TUI stays as it is (the `"? …"` note plus the
  composer's `■ ANSWER NEEDED` mode). Deliberately no new `ChatView` method for
  it — a port method only one shell implements is a port that lies about what a
  chat view is — so `GuiView.add_note` reads the question off the controller's
  `"? "` note and the existing `state` push carries it, keyed off
  `awaiting_answer` so the banner cannot outlive the park.
- **`↑`/`↓` in the composer walk this run's sends** — landed after this
  increment, filed here because it is one more claimant on the composer's key
  chain and reads as nothing else. The priority order is the TUI's exactly
  (`docs/design/tui.md` §3.3d): the slash popup gets the keys
  first while it is open, then the textarea keeps them unless the caret is at an
  edge — `↑` recalls only when nothing before `selectionStart` is a newline, `↓`
  only when nothing after `selectionEnd` is, and a live (non-collapsed)
  selection is excluded outright, because there the arrows are how a selection
  is grown. `↓` past the newest entry restores the draft. The list is
  session-local, in memory, capped at 50, blanks skipped and consecutive
  duplicates collapsed, grown in **`send()`** — the one send door, so the button
  and Enter agree — before the box is cleared. The `input` listener that already
  re-decides the popup also ends the walk, which is the page's version of
  `ChatComposer._text_changed`. Rule for rule the same as the TUI's
  `SendHistory` on purpose: these arrows are muscle memory, and two shells that
  disagreed about them would be worse than one that lacked them.
- **`t`, `u` and `l` land**, the last of increment 3's deferrals. `u` and `e`
  share one gate (`check_action` has one clause for both), `l` is gated on the
  session alone because the export is a read-only snapshot that never touches
  the engine, and all three refuse out loud — increment 2's divergence, kept.
  `t` is wholly page-side: putting the caret back in the chat box is a fact
  about this window, and its gate is the composer's own `disabled` flag, which
  Python already composed from the brief's precedence table.
- **Closing the window mid-turn asks first.** pywebview's `closing` is a
  *locking* event — handlers run synchronously on the window's own thread and a
  handler returning `False` sets `args.Cancel` (`webview/event.py:Event.set` ->
  `winforms.py:on_closing`, verified in 5.4). That thread is the one the bridge
  drainer parks against inside `evaluate_js`, and `destroy_window` reaches it
  through a blocking `Control.Invoke`, so `GuiRunner.window_closing` does
  **only** two things: read `GuiView.mid_turn` (`action_quit`'s own formula,
  `awaiting_new_session` carve-out included) and, if a turn would be lost, post
  the confirm onto the loop and return `False`. The dialog is the ordinary
  `ChatView.confirm` with the TUI's two sentences verbatim; the answer comes back
  through the ordinary bridge path and `window.destroy()` is called from the loop
  thread. A `_quit_ok` flag is set before every programmatic close, so the
  `closing` that `destroy()` itself raises sails through rather than re-asking.
  With nothing in flight the close proceeds exactly as it always has, and the
  teardown still hangs off `webview.start()` returning. `ctrl+q` reaches the same
  decision through `GuiView.request_quit`.
- **The transcript prunes at 500 events per window** (`TranscriptPanel.MAX_EVENTS`),
  deferred since increment 1. Per window because the cap is about how much DOM
  one scroll carries; the Python-side event list is untouched, so `l` still
  exports everything that ever happened.
- Help, settings, the user guide, the payload block and every blocking prompt
  reuse the **one**
  modal element. A parked flow always wins it — F1/F4 are inert while a prompt
  is up, which is the same "a modal owns all key input" rule the TUI gets from
  its screen stack.
- **The user guide is a fourth rider on that element, and the titlebar's only
  button.** `docs/commands.md` and `docs/configuration.md` — the user-facing
  manual — are read at run time (`shell/gui/docs.py`) and pushed whole on a
  `docs` event, `commands`' twin: once per page load, because it is something
  the page needs to HAVE. The files stay the source of truth; nothing copies
  them into `assets/` and nothing pre-renders them, so the two documents and
  what the window shows cannot drift. The viewer is the modal in a `docs`
  class — a switcher (Commands / Configuration) that stays put over a body that
  scrolls — and Esc closes it through the same `closePageModal` every page
  screen uses. A BUTTON and no binding: F1–F8 are all taken and a bare letter
  belongs to the main screen's session keys, so the affordance is the button
  plus a "User guide" action on F1's sheet. The F1 sheet stays what it was —
  the cheatsheet drawn from the page's own `KEYS` table and the command
  registry; this is the prose neither of those can carry.
  - `app.js`'s markdown renderer grew what the guide uses and the transcripts
    never did: pipe tables (GFM's escaped `\|`, resolved by the table before any
    inline rule, so `[on\|off]` inside a code span in a cell works), code spans
    by backtick RUN (the configuration page writes a literal backtick as a
    double-tick span), backslash escapes (`\<name\>` in a heading), block quotes
    and links. HTML is still escaped FIRST and never unescaped — both documents
    are full of literal `<project>` and `<key>` — and the whole renderer stays
    pure, which is what lets one of them serve a transcript block and a
    reference manual.
  - Packaging follows the assets' rule one package up: the files are collected
    to `agentclip/docs`, which is where `files("agentclip") / "docs"` looks —
    `packaging/agentclip.spec` for the frozen exe (§5) and a `force-include` in
    `pyproject.toml` for a wheel. A source checkout needs neither: the reader
    walks up to the repo's own `docs/`. Nowhere at all is not an error — the
    viewer shows a note saying where the guide lives — which is exactly why
    `--gui-smoke` reads it back out of a build and fails there instead.

Two decisions recorded rather than smuggled:

- **Settings persist in a new `[gui]` table, not `[general] theme`.** That key
  names a *Textual* theme and is validated against `VALID_THEMES`; the GUI's are
  CSS palettes, so writing `"dark"` there would make every TUI launch warn and
  reset it. The two tables overlap by two names and that is deliberate, not a
  leak: `claude-warm` and `claude-dark` exist in both vocabularies so that
  `/theme claude-dark` means the same thing whichever shell it is typed in — the
  same name, each shell rendering it in its own medium (a `Theme` object in
  `shell/tui/app.py`, a `body.theme-claude-dark` block in `assets/app.css`,
  role-for-role the same colours). `dark`/`light` remain this shell's alone and
  `textual-light`/`textual-dark` remain the TUI's, so neither table can be
  pasted into the other. `config.py` gains
  `GuiConfig`/`VALID_GUI_THEMES` and `save_gui_theme`, the last a clone of
  `save_theme` one table over (same atomic write, same "only this key is
  touched" contract). It is the minimum a shell-specific setting can cost, and
  it is the only core change in this increment.
- **F4 is a theme picker and nothing else**, because the TUI's SettingsScreen is
  (§2.2 of the brief) — one "Appearance" tab, and it does not touch
  `[notify] bell/toast`, which is file-only in both shells. A **real light
  theme** ships rather than the "dark (light theme planned)" fallback: every
  colour below `:root` is a token now, so a theme is a palette rather than a
  second stylesheet, and a test fails on any hard-coded hex that escapes it.
  That is also what made the Claude pair cost one CSS block each: `applyTheme`
  validates the name against the list the `settings` event brought (falling back
  to the default, never throwing), strips whatever `theme-*` class was worn and
  adds the new one — and the *default* wears no class at all, because `:root`
  already is that palette. Adding a fifth is one `THEME_CHOICES` row, one
  `VALID_GUI_THEMES` name and one block; the page needs no edit. The
  one divergence from the TUI's model: the pick applies **and saves** at once
  rather than staging behind Save/Cancel. The TUI stages because its preview is
  an app-wide reactive that Escape must revert; a class on `<body>` costs nothing
  to try, and there is no Save button to make "revert" mean anything.

  F4 is one of two doors onto this setting: `/theme [name]` is the other, and it
  is the same list, the same write and the same repaint (`GuiView._persist_theme`,
  reached from the port's `apply_theme`). The page paints only what the
  `settings` event says, so a theme changed from Python re-pushes that event and
  the body class follows — which is what makes a *chat command* able to retheme a
  page that has no palette to run one from. Neither shell has a command palette:
  this one never had, and the TUI's Textual default is switched off
  (`ENABLE_COMMAND_PALETTE = False`), so the composer's slash commands are the
  single command surface in both.

**Parity increment 7** — the SSH CONNECT DIALOG, against
`docs/design/ui-briefs/ssh-connect.md` and the six rulings in §4 below. The last
increment of the wave, the only GUI-only surface, and the only one that needed a
change on BOTH sides of the shell boundary — because the thing it is a UI for
was written into `cli.remote_launch`'s body.

- **The eight-step sequence moved down, whole, into
  `agentclip/executor/hosts/connect.py`** (`connect_remote`), with exactly two things
  injectable: who answers a question (`ConnectPrompts`) and who is told what is
  happening (`on_step`). Everything else — the order, which steps are fatal,
  every message, the close-on-failure — belongs to the sequence, because a
  second copy of any of it is the drift the extraction exists to prevent.
  `cli.remote_launch` is now a wrapper that supplies `getpass`/`input` and
  prints the same notes to the same streams; **its stderr wording and its
  exit-2s are unchanged**, and `tests/test_launch_remote.py` passes untouched.
  The six steps are `resolve → connect+auth → probe → root → env → config`:
  brief §3.4's five, with the target RESOLUTION given its own row rather than
  hidden in the first tick (it is the step that fails most often, on a missing
  `--remote-root`, and it fails before anything is dialled).
- **`executor/hosts/connect.py` is the one module in that package that may import
  `config`**, and it has its own `RULES` entry saying so. Steps 1 and 6 ARE
  config loads — the local config names the target, the remote one is read back
  through the host — so a module that could not name that layer would have to
  hand both halves back to its caller, which is exactly the shape that let the
  GUI and the CLI drift. Nothing in `executor/hosts/__init__.py` imports it, so the
  direction never closes into a cycle and the seam still costs no paramiko.
- **One core change, and it is small: `SessionController.rebind(config,
  engine_factory, project_root)`.** The three things a session is assembled from
  change TOGETHER when the machine changes, so they cross together; it is
  refused while a session is live, which is "host-hopping = new session"
  (remote-ssh.md decision 4) expressed as a precondition. No new controller is
  built — the live one is parked on `prompt_new_session` and reads all three
  when it BUILDS, which has not happened yet. What `/new` keeps, this keeps:
  both window tabs, their services and their calibrations, because the browser
  did not move.
- **`--gui --ssh` no longer blocks the launch.** `cli.main` defers it: the
  window opens on this PC and the dialog auto-opens pre-filled and runs the
  identical sequence with a checklist. A launch with a connect pending builds
  **no MCP runtime** — those servers would be this PC's, read from this PC's
  `permissions.json`, for a session about to belong to another machine. The TUI's
  launch-time flow is untouched, which is the §0 carve-out: it cannot prompt
  once Textual owns the terminal.
- `{type: "connect"}` carries the whole surface in one event, `open: false`
  being the closed state — the service editor's shape, and its model lives in
  `shell/gui/remote.py` with no window in it for the same reason. All six checklist
  rows cross on every push whatever has happened, because a stage after a
  failure must stay **pending** rather than skipped-with-a-checkmark. The three
  questions a dial can ask ride the ORDINARY `modal` family
  (`connect_password` / `connect_hostkey` / `connect_keyboard`): each is a flow
  parked on an answer, and one modal implementation is the rule.
- **The prompts hop threads in the one direction nothing else in this shell
  does.** `connect_remote` blocks, so it runs on a worker thread — which puts
  its three callbacks there too. Each opens its modal ON the loop
  (`run_coroutine_threadsafe`) and the worker parks on the answer. Anything that
  fails returns `None`, which is `PasswordPrompt`'s own "give up" signal, so a
  window closing under an open prompt ends the attempt instead of wedging a
  thread inside paramiko.

Two things are deliberately smaller than the brief's proposal, and say so:

- **Retry always re-runs the whole sequence.** §3.8 imagined a failed root check
  retrying from stage 4 on the live connection; `_abort` closes the host on
  every failure, so there is never a connection to reuse. One honest path beats
  one path plus a claim.
- **Keyboard-interactive is plumbed, not wired.** `SshHost` takes a
  `keyboard_prompt`, `ConnectPrompts` carries it, the dialog exists and its
  contract is tested — but `_authenticate` still lets paramiko's own
  `auth_interactive_dumb` (which reads stdin) have that path, and a TODO there
  says so. Routing it means bypassing `client.connect` for
  `transport.auth_interactive`, i.e. rebuilding the auth flow, and no target in
  this suite can prove the result. The seam is whole so the day it is wired
  nothing above it changes.

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

Two of the six met reality on the way in, and the deviation is in the
implementation rather than in the ruling:

- **Ruling 3 could not be implemented the way brief §6 proposed.**
  `paramiko.SSHConfig.get_hostnames()` does `entry["host"]` over every parsed
  block, and a `Match` block has no `host` key — so it raises `KeyError` on any
  `~/.ssh/config` that contains one (paramiko 4.0 `config.py:325`, verified).
  The parse is still paramiko's; only the accessor is ours, reading past a block
  that has no hostnames instead of tripping over it. Wildcards, `!` negations
  and `Match` blocks are all hidden, which is what the ruling asked for.
- **Ruling 5's manual button is `SshHost.reconnect()`**, a two-line method whose
  whole body is the `_ensure()` the next operation would have called. That is
  what keeps the model lazy: the button spends the re-dial early, it does not
  introduce a second dial path with its own failure modes. The indicator itself
  is rendered from the two facts that already crossed (`connected`,
  `reconnects`) and is repainted at turn boundaries — nothing polls, because a
  poll would be the app dialling to keep a light green.

## 5. Packaging

**The exe ships both shells.** `packaging/agentclip.spec` and
`scripts/build-exe.ps1` carry the `gui` extra now: the build syncs
`--extra cv --extra gui` and refuses to build if either is unimportable, and the
spec names what a lazy import hides. Four findings, because only one of them was
the thing this section predicted:

- **The page assets were the real gap, and they were the only one.** They are
  collected to `agentclip/shell/gui/assets` — the PACKAGE-relative path, not the
  bundle root — because `shell/gui/shell.py` resolves them with
  `importlib.resources` (§2), and PyInstaller's `FrozenImporter` answers that by
  looking under `sys._MEIPASS` at exactly that layout. Verified against a real
  onefile build rather than assumed: the spec before this change produced an exe
  that imported pywebview fine and then could not open `index.html`.
- **pywebview needed no `datas` help.** pywebview and pythonnet each ship a
  PyInstaller hook through the `pyinstaller40` entry point
  (`webview/__pyinstaller/`, `pythonnet/_pyinstaller/`), which PyInstaller
  discovers automatically; `pyinstaller-hooks-contrib` adds `hook-clr` and
  `hook-clr_loader`. Between them the WebView2 interop DLLs, `webview/js/`,
  `Python.Runtime.dll` and clr_loader's ffi DLLs all ride along. Nothing here
  had to be written by hand, and onefile + pythonnet is not the incompatibility
  it is sometimes reported to be — `interop_dll_path` has a `sys._MEIPASS`
  branch, and the package-relative one it finds first works too.
- **What the spec does add is REACHABILITY**, the same shape as `cv2` and
  `copykitten`: `webview.platforms.winforms` is chosen per platform inside
  `webview/guilib.py` at import time, and `edgechromium`/`mshtml` are chosen
  below it off the EdgeUpdate registry keys — so the build box's own WebView2
  state must not decide what a user's machine can render, and both are named.
  **The list is per-`sys.platform`**, because that same argument applies one
  level up: `guilib.initialize()` picks winforms on Windows, gtk-then-qt on
  Linux (flipped by `KDE_FULL_SESSION` or `PYWEBVIEW_GUI`, both read at *run*
  time, so both are named) and cocoa on Darwin. A spec hardcoding the Windows
  answer produced a Linux binary whose `--gui` reported that neither toolkit was
  installed — a lie about the user's machine. What the Linux branch cannot fix
  is that gtk and qt bind to *system* libraries which are not dependencies of
  the `gui` extra; naming them guarantees that whatever the build box has gets
  collected instead of skipped, and the per-distro question stays open
  (`docs/design/remote-executor.md` §5).
- **The user guide is collected the same way**, and it is the only `datas` of
  ours that is not already package data in a checkout: `docs/commands.md` and
  `docs/configuration.md` live at the REPO ROOT, not under `src/`. They go to
  `agentclip/docs` because that is where `files("agentclip") / "docs"` looks
  (`shell/gui/docs.py`), and `pyproject.toml` force-includes them at the same
  path so a wheel answers the reader identically. Losing them does not crash
  anything — the GUI's "docs" button opens a note saying the build carries no
  guide — which is precisely why the check below reads them too.
- **`--gui-smoke` is what keeps this honest** (`cli.py`, hidden, beside
  `--pick-region`). It imports pywebview, READS all three assets and both guide
  pages back through
  `importlib.resources`, runs `webview2_missing()` and prints
  `gui-smoke: ok renderer=<edgechromium|missing|n/a>`. A build box without the
  WebView2 runtime still exits 0 — the check is on the FREEZE, not the machine —
  and `scripts/build-exe.ps1` runs it against the exe beside `--version` and
  `--list-matchers`. `scripts/build-exe.sh` runs the same three on Linux/macOS;
  no display is needed there either, because `_gui_smoke` imports `webview`
  (whose `__init__` does not call `guilib.initialize()`, so no toolkit loads)
  and short-circuits the renderer to `n/a` off `platform.system()`. A failure
  that is nonetheless display- or toolkit-shaped is treated as the environment
  surprising the check rather than as a broken freeze: loud warning, build
  continues, GUI shell marked unverified.

Cost: **+49 KB** (77.5 MB), because pywebview and the .NET runtime were already
being collected — `cli.py`'s `--gui` branch imports the shell inside a function
and PyInstaller reads bytecode, so the graph already reached them. The assets are
the whole delta.

**Still open, unchanged:** the 78 MB onefile self-extracting on every launch is
the dominant startup cost, is independent of UI framework, and the onedir +
installer / slimmed-onefile decision has not been taken. The README's stale
"~15 MB" claim is fixed.
