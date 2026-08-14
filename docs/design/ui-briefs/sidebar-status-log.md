# UI Brief: Sidebar + Status Bar + Harness Log

Audience: engineers building a second front-end (pywebview/HTML/JS) that must reach
feature parity with the Textual TUI on this surface, and maintainers keeping both UIs
in sync. Describes BEHAVIOR framework-neutrally; Textual specifics are called out
separately in section 7 so they are not copied by accident.

Primary sources: `docs/design/tui.md` §1.2, §1.3, §3.3, §3.3a (`/log`), §3.3b, §3.4a–h,
§3.5; `src/agentclip/tui/widgets/sidebar.py`; `src/agentclip/tui/widgets/statusbar.py`;
`src/agentclip/tui/widgets/log_pane.py`; `src/agentclip/tui/loop_state.py`;
`src/agentclip/tui/harness_log.py`; `src/agentclip/tui/screens/main.py`;
`src/agentclip/app/view.py` (the `ChatView` protocol).

---

## 1. Purpose

Three widgets, one job each, sharing one theme: **make the automation's internal
state legible while it is happening**, not after the fact.

- **Sidebar** — the right-hand settings/state column. It is "live status plus the two
  things you steer with" (sidebar.py:24), holding nothing that has to be *configured*
  (that moved to the F2 service editor). It answers, at a glance: where is the
  browser-automation loop right now, which service/window is selected, is the chat
  region drawn, and what are the finish detectors seeing on the live window.
- **Status bar** — one docked row, ten segments, answering "what does the app want
  from me, and what session-wide facts govern the next turn" (permission mode,
  ARMED/DISARMED, service+budget, outbound size, turn number, extra-instructions
  arm, edits/YOLO policy, MCP connection count, project root).
- **Harness log** — the decision log. The STATE rail says *where* the loop is; this
  says *how it got there*. One `LoopState` box (e.g. `MANUAL_COPY`) can be reached by
  four different roads (disarmed, no captured copy button, button not found on
  screen, click did not take) and the log is the only place that distinction survives
  longer than a toast (tui.md:613, harness_log.py:1-16).

These three are deliberately split from the STATUS BAR / TRANSCRIPT / ACTION PANEL
surfaces: this brief covers only the right-hand column, the bottom bar, and the F8
log pane.

---

## 2. Anatomy

### 2.1 Sidebar (top to bottom; tui.md §1.3, sidebar.py:412-485)

1. **STATE rail** — title `"STATE"`, then 8 rows, one per `LoopState`, in loop order
   (not enum declaration order): idle, auto insert, manual insert, wait send, wait
   generate, auto copy, manual copy, interpreting (loop_state.py:26-33,
   sidebar.py:114-133). Row id: `state_row_id(state)` → `side-state-{name.lower()}`.
2. **Paste-flash banner** (`side-paste-flash`) — one of four texts (§3 below);
   `display:none` when idle.
3. **Retry-insert button** (`retry-insert-btn`, label "Retry insert") — mounted
   directly under the flash; shown only when the flash is the Ctrl+V variant with
   `retry=True`.
4. **DISARMED banner** (`side-armed-banner`) — text `"⛔ DISARMED\nwatching only - F5
   arms"` (`DISARMED_BANNER_TEXT`); `display:none` while armed. Sits directly under
   the STATE rail, above PROJECT.
5. **PROJECT** — title, then the short project root (`~\...`).
6. **MCP block** (optional) — title `"MCP"` + one row per configured server, in
   config order, id `side-mcp-{index}`. Composed only when the app has an
   `McpManager`; otherwise this whole block (heading included) does not exist.
7. **SERVICE** — title, then a `Select` (`service-select`) of `"key · Nk"` rows, a
   caption line (`side-service-label`: label + max-paste-chars + total-context-chars),
   a read-only appearance-summary line (`side-profile-note`: `"appearance: N/7
   captured · F2 for captures + detection"`), and an "Edit services..." button
   (`edit-services-btn`).
8. **CHAT WINDOW** — title, a "Set chat region..." button (`set-region-btn`), the
   drawn-region readout (`side-region`), and a readiness note (`side-slot-note`:
   `"the main agent's chat window"` / `"delegation ON"` / `"delegation off · need:
   ..."`).
9. **DETECTION** — title that names the LIVE window (`side-detection-title`:
   `"DETECTION"` / `"DETECTION · MASTER"` / `"DETECTION · SUB-AGENT"`), then five
   read-only lines, each named as it is painted:
   - `side-tpl-send-ready` — `"send · ..."` (the send gate, §3.4b)
   - `side-tpl-busy` — `"busy · ..."`
   - `side-tpl-idle` — `"idle · ..."`
   - `side-stale` — no label prefix; the staleness detector's readout
   - `side-tpl-copy` — `"copy · ..."` (the auto-copy flow's last click attempt)
10. **"New browser chat" button** (`newchat-btn`).
11. **Hint line** — `"F3 hides this column · F7 elements · F5 armed · F2 settings ·
    F1 help"`.

Only 4 of the 7 `TemplateKind` values (`SEND_READY`, `BUSY`, `IDLE`, `COPY`) ever
produce a verdict line here; `CHATBOX_INITIAL`, `CHATBOX_ONGOING`, `NEW_CHAT` are
searched every tick like the rest but decide nothing and have no line — their
pictures live in the separate ELEMENTS column (F7, out of this brief's scope).

### 2.2 Status bar (10 segments, in order; statusbar.py:35, tui.md §3.3)

```
mode | watch | armed | service | out | turn | instr | edits | mcp | root
```

| id | shows | hides when |
|---|---|---|
| `seg-mode` | `MODE:ask` / `MODE:plan` / `MODE:unattended` | never |
| `seg-watch` | one of 8 watcher-state renderings (§3.2) | never |
| `seg-armed` | `⛔ DISARMED` | app is ARMED (the common case) |
| `seg-service` | `"{service_key} {budget}k"` or `"no session"` | never |
| `seg-out` | `"out {last_outbound_k}/{budget_k} (1/1)"` or `"out -"` | never |
| `seg-turn` | `"turn {n}"` or `"turn -"` | never |
| `seg-instr` | `✎ INSTR` | the extra-instructions reminder is not armed |
| `seg-edits` | `EDITS:ask` / `EDITS:auto` / `⚡ YOLO` | never |
| `seg-mcp` | `"mcp {connected}/{enabled_total}"` | app has no `McpManager` configured |
| `seg-root` | short project root | never |

Segments hide by *not being drawn* (zero width, no padding left behind), not by
going blank — see statusbar.py:66-87 (`armed_seg.display = bool(armed)`, same for
`instr_seg`/`mcp_seg`).

### 2.3 Harness log pane (F8; log_pane.py, harness_log.py)

- Border title `"HARNESS DECISION LOG"`, subtitle `"newest last · F8 hides"`.
- Full-width strip between the three columns and the status bar (sibling of `#body`
  in the vertical layout, not a fourth column) — `height: 30%; min-height: 6;
  max-height: 14`.
- One entry per line: `HH:MM:SS  {kind:<8}  {text}` (`HarnessEntry.line`,
  harness_log.py:94-96). Seven kinds, printed as a fixed-width column:
  `state`, `gate`, `trigger`, `copy`, `clipboard`, `session`, `armed`
  (harness_log.py:53-69).
  - `state` entries read `"{OLD} → {NEW} — {reason}"` (`state_text`, harness_log.py:118-120).
- Empty-log placeholder: `"nothing logged yet - the harness writes here as it moves
  through the loop (paste, send, generate, copy)."` (`EMPTY_LOG_LINE`).

---

## 3. States

### 3.1 LoopState — the STATE rail (loop_state.py)

Eight values, in loop order:

```
IDLE → AUTO_INSERT → (MANUAL_INSERT) → WAIT_SEND → WAIT_GENERATE
     → AUTO_COPY → (MANUAL_COPY) → INTERPRETING → IDLE | AUTO_INSERT
```

| state | meaning |
|---|---|
| `IDLE` | nothing outstanding; the user's next chat message starts the loop |
| `AUTO_INSERT` | outbound copied; clicking the chat box and pasting it in |
| `MANUAL_INSERT` | the click/paste did not land; the user Ctrl+Vs it themselves |
| `WAIT_SEND` | the payload is in the chat box; waiting for the user's Enter |
| `WAIT_GENERATE` | sent, and the model is visibly generating |
| `AUTO_COPY` | generation stopped; hunting for the reply's copy button |
| `MANUAL_COPY` | no copy button to click; the user copies the reply themselves |
| `INTERPRETING` | the reply is on the clipboard; parsing and acting on it |

`LOOP_TRANSITIONS` (loop_state.py:36-58) — the legal-next table the rail's styling
reads:

| from | legal next |
|---|---|
| `IDLE` | `{AUTO_INSERT}` |
| `AUTO_INSERT` | `{WAIT_SEND, MANUAL_INSERT}` |
| `MANUAL_INSERT` | `{WAIT_SEND, WAIT_GENERATE}` |
| `WAIT_SEND` | `{WAIT_GENERATE}` |
| `WAIT_GENERATE` | `{AUTO_COPY, MANUAL_COPY}` |
| `AUTO_COPY` | `{INTERPRETING, MANUAL_COPY}` |
| `MANUAL_COPY` | `{INTERPRETING}` |
| `INTERPRETING` | `{AUTO_INSERT, IDLE}` |

This is forward motion only. Session teardown (`/new`) sends every state home to
`IDLE`, and that is explicitly **a reset, not a transition** — it is logged with the
reason `"session reset"`, not drawn as though `IDLE` were reachable from everywhere
(loop_state.py:11-15, main.py:1419-1428). `LOOP_TRANSITIONS` is display-only: nothing
reads it to *decide* anything, and nothing enforces that `_set_loop_state` only moves
along a legal edge — it is the rail's brightness table, not a state-machine guard
(sidebar.py "the states are display only" doc; tui.md:166 "nothing reads the enum
back to make a decision").

Rail rendering rule (sidebar.py:493-508): active row gets a `▶ ` marker + bold/reverse
style; every row in `LOOP_TRANSITIONS[active]` reads at normal brightness; everything
else is dim.

`MainScreen._set_loop_state(state, reason)` (main.py:1645) is the **sole writer**.
`reason` is a required argument — every transition is forced through the harness log
(`KIND_STATE`) at the same call site, and a no-op call (same state twice) neither
repaints nor logs (main.py:1663-1664).

### 3.2 Status bar segment variants

- **`mode`** (statusbar.py `mode_class`): `MODE:ask` → `st-dim`; `MODE:plan` →
  `st-plan` (blue/bold); `MODE:unattended` → `st-unattended` (warning/bold). Never
  red — red is reserved for the two "something is off" badges (`⚡ YOLO`,
  `⛔ DISARMED`). Never hides (tui.md:531).
- **`watch`** — 8 renderings (`_watch_segment`/`_base_watch_segment`,
  main.py:4942-4975), evaluated in this priority order:
  1. `phase_name == "DONE"` → `"✓ done - reply to continue"` (`st-done`)
  2. `pending_approval` → `"■ APPROVE NEEDED"` (`st-attn`)
  3. `awaiting_answer` → `"■ ANSWER NEEDED"` (`st-attn`)
  4. `awaiting_new_session` → `"○ idle"` (`st-dim`) — even though the session
     worker is technically busy (parked on the inline start prompt); `● working...`
     would be a lie the user has no way to resolve
  5. `busy` → `"● working..."` (`st-busy`)
  6. provider is `"manual"` → `"✗ manual paste"` (`st-err`)
  7. `watch_paused` → `"○ paused"` (`st-dim`)
  8. `session_active and phase == AWAITING_REPLY` → `"● ready - paste the reply"`
     (`st-armed`)
  9. else → `"○ idle"` (`st-dim`)

  (tui.md:535 also documents `◍ EXECUTING`, `◍ APPROVAL?` blinking, `◍ PART n/m`
  chunk mode, and `✗ CLIP ERR` provider-fault as additional renderings layered on
  by the clipboard-watcher integration, not all of which are literally
  branches of `_base_watch_segment` above — treat the two lists together as the
  full enumeration.)
- **`edits`** — priority order: `yolo` set → `"⚡ YOLO"` (`st-yolo`, red) **overrides**
  `auto_accept_edits`; else `auto_accept_edits` → `"EDITS:auto"` (no special class);
  else `"EDITS:ask"` (main.py:4999-5004). YOLO always wins the display even though
  the two flags are logically independent.
- **`armed`** segment (status bar) — empty/hidden while armed; `"⛔ DISARMED"` while
  disarmed. Its own slot, not folded into `edits`, because a disarmed YOLO session
  is a real, must-see combination (tui.md:533, statusbar.py:10-16).
- **Sub-agent rebadging** — during a delegation every field on the bar (mode,
  service, out, turn, edits, instr) describes the **sub-agent's** engine, not the
  master's, because `SessionView.snapshot` is whichever engine the controller
  currently has focused (`app/view.py` `SessionView.session_role`). The status bar
  itself carries no separate "which session" badge in this segment set — the
  sub-agent context is signaled elsewhere (gate title prefix, panel focus), not by
  a status-bar segment. (Verify against the live app if a second front end wants an
  explicit sub-agent status-bar cue — see Ambiguities.)

### 3.3 Sidebar banner show/hide triggers

**Paste flash** (`side-paste-flash`) — four possible texts, one call site chooses
which (sidebar.py:140-153, `Sidebar.show_paste_flash`):
- `PASTE_FLASH_TEXT` = `">>> PRESS CTRL+V <<<\nin the chat, then send"` — the
  synthetic click/paste did not land; shown with `retry=True` (retry button visible).
- `ENTER_FLASH_TEXT` = `">>> PRESS ENTER <<<\nreply pasted - just send it"` — the
  paste landed; only the human Enter is left.
- `AUTO_SEND_FLASH_TEXT` = `">>> AUTO-SENT <<<\nEnter was tapped for you"` — the
  service has `auto_submit` on and a synthetic Enter was sent; stays up until the
  send gate proves the send landed (a tap can silently fail).
- `stream_flash_text(index, total)` = `">>> STREAMING <<<\nchunk {i}/{n} - don't
  type"` — a streamed delivery is mid-flight.

Hide triggers (`hide_paste_flash`, called from `MainScreen`): a `MATCH` busy probe
(model started chewing → paste/send proven), a new `ClipboardCaptured` (conversation
moved on without it), or `clear_transcript()` (session reset). Hiding also
unconditionally hides the retry button (tui.md §3.4b "The paste flash and
auto-paste").

**DISARMED banner** (`side-armed-banner`) — one call, `show_armed_state(armed:
bool)`, mirrors `MainScreen._os_armed` directly; no separate "moment passed" case
like the flash (sidebar.py:749-757). Deliberately never blinks — it is a standing
fact meant to survive being looked at for an hour, unlike the flash which is asking
for a keystroke *right now* (sidebar.py:16-22, tui.md:134-138).

---

## 4. Inputs from core

This is the section that matters most for the AutomationView-port split. Two
distinct data sources feed this surface:

**A. `ChatView` port calls** (core-driven, protocol in `src/agentclip/app/view.py`) —
these WILL be part of the shared `SessionController` core and must be implemented by
any new front end:

| method | drives |
|---|---|
| `render_state(view: SessionView)` | Status bar (all of it, via `_paint_status`), composer enable/prompt, sidebar service-lock (`_sync_sidebar` → `Sidebar.set_locked`), and **one slice of the STATE rail**: `render_state` forces `LoopState.IDLE` whenever `not view.session_active` (reason `"no session is running"`) or whenever the loop is `INTERPRETING` and the turn has settled back to waiting on the user (reason `"the turn finished and the floor is back with you"`) — main.py:1796-1805. |
| `notify` / `alert` | toasts referenced throughout (paste failures, gate results, /new outcomes) — not literally part of this surface's anatomy but the harness log's reasons are frequently lifted verbatim from these strings (harness_log.py:11-15). |
| `set_os_armed(target)` | the DISARMED banner + `seg-armed` (indirectly, via `_paint_armed`/`_paint_status` which `MainScreen` calls from inside its own `set_os_armed` implementation) |
| `toggle_harness_log()` | show/hide the F8 pane |
| `clear_transcript()` | resets STATE rail to `IDLE` ("session reset"), hides paste flash, clears `_pending_insert`, restarts the detector worker — but does **not** clear the harness log (harness_log.py:26-29, main.py:1409-1434) |

`StatusSnapshot` (on `SessionView.snapshot`, from `agentclip.engine.engine`) is the
authority for `mode`, `service_key`, `budget_chars`, `last_outbound_chars`, `turn`,
`yolo`, `auto_accept_edits`, `instructions_armed`, `has_extra_instructions`. Before a
session exists, `mode` falls back to `SessionController.permission_mode` (a live,
settable value even pre-session) — main.py:4990.

**B. TUI-internal automation state** (currently NOT behind any port; this is the part
slated for extraction behind the new `AutomationView` port) — driven directly by
`MainScreen`'s own screen-automation machinery (`agentclip.screen.*`: the poller,
`PresenceTracker`, `StaleTracker`, the send gate, the auto-copy flow, the clipboard
focus/paste primitives) and NOT reachable through `ChatView`:

| element | fed by |
|---|---|
| STATE rail (all 7 non-IDLE-via-render_state transitions) | `_set_loop_state` calls scattered through the paste path, the send gate, the finish-detector evaluation, the auto-copy flow, and `ClipboardCaptured`/force-ingest handling — main.py grep hits at lines ~2111, 2131-2143, 2216, 2639, 3558, 3728, 3752-3775, 3949, 4209, 4263, 4326, 4363, 4399, 4814 |
| Paste-flash banner | `copy_outbound`'s click/paste outcome, the send gate's phase, `_stream_outbound`'s chunk progress (tui.md §3.4a/§3.4b) |
| SERVICE / CHAT WINDOW blocks | the window-tab selection (`_select_window`), `SlotCalibration`, `Sidebar.show_slot`/`show_service`/`show_profile` |
| DETECTION block (all 5 lines) + its heading | **only** the detector machinery: `_start_detector_worker`'s rebuild, and the per-tick handlers `on_busy_probed`/`on_idle_probed`/`on_stale_probed`/`on_send_ready_probed` (tui.md §3.4e — "only the detector machinery writes them"; `Sidebar.show_slot` and `Sidebar.show_profile` are explicitly forbidden from touching these lines) |
| Harness log entries of kind `gate`, `trigger`, `copy`, `clipboard` | the send gate, the auto-copy trigger arm, the auto-copy flow's steps/failures, clipboard capture ingestion |
| Harness log entries of kind `armed` | `MainScreen.set_os_armed` (itself a `ChatView` port method, but the *reason text* it logs is TUI-internal wording) |
| Harness log entries of kind `session` | `render_state`'s session-boundary detection (`_logged_session_active`) — this one **is** core-driven, via `render_state` |
| MCP block + `seg-mcp` | `McpManager.statuses()`, via `_mcp_status_hook` → `McpStatusChanged` message → `_paint_mcp`; layered independently of `ChatView` (own manager object, own hook) |

**Net for a second front end**: the status bar and the session-boundary/idle-reset
half of the STATE rail can be built purely against `ChatView`/`SessionController`.
The paste flash, the SERVICE/CHAT WINDOW/DETECTION sidebar blocks, and 7 of the 8
`LoopState` transitions require the screen-automation layer's own event stream —
i.e. they need whatever the `AutomationView` port ends up exposing. The harness log
straddles both: its `session` entries are core-driven, everything else is
automation-driven, but the deque and the pane are one undifferentiated store today
(`MainScreen._harness_log`) with no seam between the two sources.

---

## 5. User actions out

| action | key / control | invokes |
|---|---|---|
| Toggle sidebar | `F3` (priority binding, works with composer focused) | `MainScreen.action_toggle_sidebar` → flips `sidebar.display` |
| Toggle harness log pane | `F8` (priority, `show=False`) or `/log` | `ChatView.toggle_harness_log()` → same call both ways; toggles `display`, refills from the deque on reveal only if entries arrived while hidden |
| Toggle ARMED/DISARMED | `F5` (app-level binding) or `/armed [on|off]` (bare toggles) | `AgentClipApp.action_toggle_armed` → `MainScreen.set_os_armed(None)`; `/armed` has **no session gate** |
| Cycle permission mode | `shift+tab` (priority, overrides Textual's own binding; never gated, works mid-turn/mid-delegation) | `MainScreen.action_cycle_permission_mode` → `SessionController.cycle_permission_mode()` — also settable via `/mode [plan|ask|unattended]`; bare `/mode` reports rather than cycles |
| Region picker button | `#set-region-btn` in CHAT WINDOW block | draws the chat-region overlay for the **selected tab's** window at the moment the picker opened (slot captured once, not re-read after the await) |
| "New browser chat" button | `#newchat-btn` | same flow as `/new`, but targets whichever tab is **selected** (not pinned to master like `/new`); on the master tab a landed click also ends the session (`SessionController.request_new_session()`) |
| "Retry insert" button | `#retry-insert-btn` (shown only under the Ctrl+V flash) | `MainScreen.retry_insert()` — re-runs `_insert_outbound`, the same method the auto flow uses; refuses (toast) if nothing was copied, if DISARMED, or if an auto-copy flow is mid-sequence |
| Service picker | `#service-select` in SERVICE block | posts `Sidebar.ServiceChanged(key)` **only** when the change is a genuine user pick (not a tab-catching-up write via `show_service`, not the two-step reset `refresh_services` does internally); locked whenever a session is active |
| "Edit services..." button | `#edit-services-btn` | `AgentClipApp.action_settings()` (same as `F2`) |
| Extra-instructions re-inject | `r` | `SessionController.reinstruct()` — arms/disarms `✎ INSTR`; hidden from the footer entirely (not merely dimmed) when the live preset carries no instructions |
| Toggle clipboard watcher | `w` | `MainScreen.action_toggle_watch` — refused (toast) while DISARMED |
| Force-ingest | `i` | sets `LoopState.INTERPRETING` directly, then `SessionController.force_ingest()` — the one place a key press moves the rail without going through the detector machinery |
| Re-copy / re-deliver | `c` (double-tap within 1.5s escalates) | first press: `ChatView.park_outbound` (clipboard write only, no `_set_loop_state` move); second press within the window: `redeliver_outbound` (full `copy_outbound` re-run) |

`/log`, `/identify`, `/armed` are the three commands with **no session gate** — they
answer "what is this thing doing to my screen, and why," which is exactly what a
wedged, session-less user needs (tui.md:568-570).

---

## 6. Invariants & edge cases

- **Harness log: single writer, required reason.** `MainScreen._set_loop_state(state,
  reason)` is the sole writer of `LoopState`; `reason` is a required positional
  argument, so a rail move cannot happen without a log entry (main.py:1645-1667,
  harness_log.py "MainScreen is its only writer"). All other log entries go through
  `_log_harness(kind, text)` (main.py:1670-1685), the one append site that owns the
  timestamp and the bound.
- **`deque(maxlen=HARNESS_LOG_MAX)`, `HARNESS_LOG_MAX = 500`** (harness_log.py:48) —
  same bound as `TranscriptPanel`'s prune limit; a debugging tail, not an archive.
  Nothing is written to disk (harness_log.py:34-38).
- **The harness log survives `/new`.** `clear_transcript()` resets the STATE rail to
  `IDLE` with reason `"session reset"` and appends a `KIND_SESSION` entry worded as a
  reset, not naming `/new` by name (because the summary screen's "new session" and
  the budget-retry path reach the same teardown and did not literally type `/new`) —
  main.py:1414-1428, tui.md:620. The log itself is never cleared by any in-app action.
- **UI-thread-only appends, no lock.** Every `_log_harness`/`_set_loop_state` call
  site is on the event loop; worker threads reach the screen only via
  `post_message`, and async flows log after their `await`s return — so the deque is
  never touched concurrently (harness_log.py:30-32).
- **Pane vs. deque: two matching bounds.** The `RichLog` widget's `max_lines` is set
  to the same `HARNESS_LOG_MAX`; with `wrap=False` one entry is exactly one line, so
  the widget and the deque prune in lockstep during a long run with the pane open
  (log_pane.py:29-32, 57-64).
- **Follow/freeze scroll behavior is a property, not a mode.** `following` ==
  `is_vertical_scroll_end`, read fresh on every append — there is no boolean flag
  that can disagree with where the scroll actually is. At the tail, a new entry
  scrolls the view; scrolled up, it lands below the fold and the view does not move.
  Scrolling back to the bottom (or native `End`) resumes following with nothing
  having to notice (log_pane.py:74-101).
- **Hidden pane paints nothing; reveal is one full refill.** While `display` is
  false, `append()` only sets `_behind = True` — no render cost. `reveal(entries)`
  does a single `refill` from the live deque so a reopened pane shows *now*, not a
  poll interval ago (log_pane.py:86-120). This is the opposite tradeoff from the
  ELEMENTS column, which keeps painting into a hidden widget.
- **State-rail transition legality is display-only.** `LOOP_TRANSITIONS` governs the
  rail's dim/normal/active styling; it is not consulted by `_set_loop_state` to
  reject an illegal move, and nothing reads `LoopState` back to make a decision
  elsewhere. A road that skips a state (e.g. a manual paste-and-send the send gate
  never observed) simply never lights that row — it is not an error condition
  (loop_state.py header comment, sidebar.py `show_loop` docstring).
- **DETECTION block ownership is exclusive.** Only the detector machinery
  (`_start_detector_worker` and its per-tick handlers) may write the five DETECTION
  lines or the heading. `Sidebar.show_slot` and `Sidebar.show_profile` are
  *explicitly* forbidden from touching them — this used to be a bug (a tab switch
  silently overwrote "finish detection off" with a stale "watching the chat region"
  promise) and is now a stated rule (tui.md §3.4e, sidebar.py:606-613, 640-646).
- **DETECTION/STATE describe the LIVE window, everything else describes the
  SELECTED tab.** These two pointers are almost always the same window and diverge
  for exactly the length of a delegation — precisely when the readout matters most.
  The DETECTION heading and the STATE rail therefore have no per-tab identity; a tab
  click never repaints them (tui.md §3.4e).
- **Resting-state lines explain their own silence**, so a line stuck at "no verdict
  yet" is never confused with "detector found nothing forever": `STALE_UNSET` (no
  region drawn) / `STALE_UNTICKED` (stillness unticked, icons still run) /
  `STALE_OFF` (nothing runs at all) / `STALE_CALIBRATED`; `PROBE_UNCAPTURED`
  ("ticked but not captured - F2") on a busy/idle line whose signal is ticked but has
  no appearance behind it (sidebar.py:219-254).
- **Segments hide by not being drawn**, never by going blank — `armed`, `instr`,
  `mcp` reserve no padding when hidden, so an install with no MCP servers gets
  exactly the bar it always had (statusbar.py:66-87).
- **YOLO always wins the `edits` segment's display over `EDITS:auto`**, even though
  the two booleans are independent and both can theoretically matter — there is no
  combined "auto+YOLO" rendering (main.py:4999-5004).
- **`DISARMED` and `YOLO` are independent axes with separate slots.** A disarmed
  YOLO session is a real, must-be-visible combination — this is why `armed` is its
  own status-bar segment rather than sharing `edits`'s slot (statusbar.py:10-16,
  tui.md §3.5 "Terminology").
- **`awaiting_new_session` masks `busy` in the watch segment.** The session worker
  is technically busy (parked on the inline start-session prompt) but the bar reads
  `"○ idle"` rather than `"● working..."`, because there is no turn in flight for the
  user to wait on (main.py:4962-4966).
- **The paste flash and the DISARMED banner are structural siblings with opposite
  temperaments.** Same mount position (directly under the STATE rail), same widget
  shape; the flash blinks (asking for a keystroke *now*, while the user is looking
  at the browser) and the DISARMED banner never does (a standing fact that must
  survive being looked at for an hour) — sidebar.py:14-22, 100-106.
- **`Retry insert`'s visibility is coupled to which flash text is showing**, not to
  "was there ever a failure" — it is shown only when the current banner is the
  Ctrl+V variant (nothing landed); an Enter-only or streaming flash never offers it,
  because there is nothing to retry (sidebar.py "Under it sits the one button...").

---

## 7. Textual-specific details NOT to carry over

- **`priority=True` bindings** (`F3`, `F7`, `F8`, `F6`, `shift+tab`, `ctrl+x`,
  `ctrl+o`) exist purely to jump ahead of a focused `TextArea` (the composer) that
  would otherwise consume the key, and — for `shift+tab` specifically — to override
  Textual's own `Screen.focus_previous` binding. A web front end has no equivalent
  "widget eats my global hotkey" problem in the same shape; it needs its own
  focus/keyboard-capture strategy, not a port of `priority`.
- **`show=False` bindings** are a Textual footer-widget concern (keeping the footer
  from overflowing); they say nothing about whether the *feature* should be
  discoverable in a different UI's keyboard-shortcut surface.
- **Reactive attributes with `bindings=True`** (`pending_approval`, `busy`, etc. on
  `MainScreen`) drive Textual's `check_action`/footer-dimming machinery
  automatically on every mutation. A second front end has no equivalent implicit
  re-evaluation and must explicitly recompute enabled/disabled state after each
  relevant change.
- **`RichLog` widget internals** — `max_lines`, `wrap=False`, `auto_scroll=False`
  with manual `scroll_end=` per write, `is_vertical_scroll_end` as the "am I at the
  bottom" oracle — are Textual's scrollback primitive. The *behavior* to preserve
  (freeze-on-scroll-up, one-entry-one-line, sideways-scroll for long lines) is
  covered in §6; the RichLog API surface used to get there is not portable.
  `markup=False, highlight=False` specifically suppress Rich's markup/syntax
  interpretation of log text — a web front end just needs to not interpret the log
  text as markup either, by whatever means.
- **`Static`/`Select`/`Button` Textual widget classes**, their `query_one(...,
  Static)` repaint pattern, and CSS classes (`side-status`, `side-probe`,
  `side-state-row`/`side-state-legal`/`side-state-active`, `flash-alt` blink toggle
  class, `.seg`, `st-dim`/`st-plan`/`st-unattended`/`st-busy`/`st-attn`/`st-done`/
  `st-armed`/`st-err`/`st-yolo`) are Textual/CSS-specific styling hooks. The
  semantic states they encode (which row is dim/legal/active; which segment variant
  is showing) are in §3; the class names themselves are not meaningful outside
  Textual.
- **The blink timer** (`set_interval(_FLASH_BLINK_S, ...)`, `_FLASH_BLINK_S = 0.4`)
  is explicitly called out in the source as "pure presentation, so the dumb widget
  may own it" — i.e. even within the TUI this was deliberately kept out of
  `MainScreen`. A web front end can implement the blink however CSS/JS does
  animation; the 0.4s cadence itself is not load-bearing, only "obnoxious enough to
  notice."
- **`post_message` / `call_from_thread` thread-bridging** (`_mcp_status_hook`,
  the detector poller's worker thread posting `BusyProbed`/`IdleProbed`/
  `StaleProbed`/`SendReadyProbed`/`ElementsMatched`) is Textual's message-queue
  mechanism for crossing from a worker thread to the UI event loop. Any second
  front end needs *some* thread-safe hop from the automation poller (which will
  keep running on a background thread regardless of UI framework) to its own
  render loop, but the specific `Message` subclasses and `on_*` handler dispatch
  are Textual idioms.
- **`display = True/False` as the show/hide primitive** and the associated "hidden
  widgets still exist and can be queried" model (e.g. the harness log pane staying
  mounted forever, just toggling `display`) is a Textual layout convenience. A DOM
  equivalent (`hidden` attribute, CSS `display:none`) achieves the same effect but
  is not literally this API.
- **`NoMatches` exception suppression** (`with suppress(NoMatches): ...`) throughout
  `MainScreen` guards against querying a widget before the screen is mounted or
  after teardown — a Textual composition-timing artifact, not a behavior to
  reproduce.

---

## Ambiguities found while writing this brief

1. **Sub-agent rebadging on the status bar itself** — tui.md and the code confirm
   that every `StatusSnapshot`-derived field (mode/service/out/turn/edits/instr)
   silently switches to describe the sub-agent's engine during a delegation, but I
   found no dedicated status-bar segment or visual marker *on the bar* that says
   "you are looking at the sub-agent's numbers" (the cue lives in the gate-title
   prefix and panel focus instead). Worth confirming with the TUI maintainer whether
   this is intentional (bar is per-conversation, not per-window) or a gap a second
   front end should not reproduce.
2. **Full enumeration of `#seg-watch` renderings** — `_base_watch_segment` in
   main.py gives 8 states explicitly in code; tui.md:535 additionally lists
   `◍ EXECUTING`, `◍ APPROVAL?` (blinking), `◍ PART 2/3` (chunk mode), and
   `✗ CLIP ERR` (provider fault) as bar states. Some of these did not resolve to an
   obvious single branch in the code I read (they may be produced by watcher-state
   plumbing elsewhere, e.g. `WatcherStateChanged` messages, that I did not trace to
   its status-bar repaint site). Treat §3.2's list as the states confirmed from
   `_base_watch_segment` plus the doc's superset; a full reconciliation would need
   to trace the clipboard-watcher message handlers directly.
3. **Where exactly the `AutomationView` port boundary will fall** — this brief
   labels DETECTION/STATE(non-idle)/paste-flash as "TUI-internal, not yet behind a
   port" based on the current `ChatView` protocol not mentioning them, but since the
   port is described as newly being extracted, the actual method-level shape of
   `AutomationView` was not available to consult — section 4's split is a snapshot
   of current coupling, not a preview of the new port's API.
