# UI Brief: Main Chat Surface

Audience: engineers building a second frontend (pywebview/HTML/JS) that must
reach behavioral parity with the Textual TUI, and maintainers keeping both UIs
in sync. This brief describes BEHAVIOR only, framework-neutrally. The shared
core is `SessionController` (`src/agentclip/app/controller.py`) driving the
`ChatView` protocol (`src/agentclip/app/view.py`); a new frontend implements
`ChatView` and nothing else needs to change.

Surface covered: the transcript panel, the chat composer, the slash-command
popup, the action panel (approval gate, chunked-send wizard, reject-reason
input), and the run panel (per-call rows, live output tail, spinner/running
bar). Window tabs, the sidebar, the ELEMENTS panel and modals (service editor,
settings, summary, confirm, help) are separate surfaces and are out of scope
here except where they gate this one's state.

**Source status note:** `docs/design/tui.md` is this brief's primary citation
source and is largely current (it documents the 2026-08-09 window-tabs/F2/run-
panel rework), but two parts of it are stale against the code as read on
2026-08-14 and are called out explicitly below (§3 States, item on `ask_user`,
and §3 States, item on chunk-walk).

---

## 1. Purpose

The main chat surface is where the user reads the model's turns and steers the
one loop AgentClip automates: describe a task, watch tool calls execute,
approve or reject the ones that need a human, and read the reply. Everything
on it either narrates what the engine is doing to the user's files and shell
(transcript, run panel) or collects the one input the engine is blocked on
(composer, action panel). Its design goal, stated in `docs/design/tui.md`
line 3, is that the steady-state loop costs the user one keypress (`y`) — the
transcript, run panel and status chrome exist so that keypress is an informed
one, not a leap of faith.

## 2. Anatomy

- **TranscriptPanel** (`src/agentclip/tui/widgets/transcript.py`) — a
  vertically-scrolling list, one mounted element per session event: user
  messages, assistant prose, tool calls (with a collapsible raw-block body),
  notes, errors, and outbound-copy notices. One exists per window/session tab;
  this brief describes one panel's behavior, not the tab bar that selects
  between them.
- **ChatComposer** (`src/agentclip/tui/widgets/composer.py`) — the single
  persistent text box docked below the transcript. Serves four different jobs
  depending on session phase (task entry, ask_user answer, follow-up, slash
  command) without ever changing widget — see §3.
- **CommandPopup** (`src/agentclip/tui/widgets/command_popup.py`) — a small
  filtered list of slash commands that appears directly above the composer
  while a `/command` is mid-type, and narrows per keystroke.
- **ActionPanel** (`src/agentclip/tui/widgets/action_panel.py`) — a bottom
  drawer, hidden when idle, that opens when a tool call needs human approval.
  Holds a title line, a queue strip, a scrollable diff/command preview body,
  Approve/Approve+auto-edits/Reject buttons, and a hidden reject-reason
  `Input`. Also the planned home of the chunked-send wizard (§3, not yet
  shipped in code).
- **RunPanel** (`src/agentclip/tui/widgets/run_panel.py`) — hidden when idle,
  shown for the duration of a turn's execution. Contains the `RunningBar`
  header (spinner + label + cancel hint), a rows view (one line per planned
  call, glyph-coded), and a collapsible output tail for the currently running
  `run_command` call.
- **RunningBar** (`src/agentclip/tui/widgets/running_bar.py`) — the animated
  one-line "working" indicator that is the run panel's header; owns only the
  spinner animation, the label and the `(ctrl+x to cancel)` hint.

## 3. States

### Composer modes

Exactly one text widget serves four modes, switched by
`MainScreen._update_composer()` (`src/agentclip/tui/screens/main.py:4862`),
which is driven by the `SessionView` the controller pushes after every state
change. Precedence order (first match wins), each with its border-title copy
and whether Enter's text is parsed for slash commands or delivered verbatim:

1. **`awaiting_new_session`** (task entry / "describe the task") — title
   `"Describe the task · Enter starts the session · Ctrl+J newline"`. Enter
   dispatches slash lines as commands (`MainScreen._submit_text`,
   `main.py:2699`) and only a non-empty, non-slash line resolves the pending
   `prompt_new_session()` future into a `SessionSpec`. A literal task starting
   with `/` is escaped as `//…` (one slash stripped).
2. **`awaiting_answer`** (ask_user) — title `"Answer the model · Enter sends ·
   Ctrl+J newline"`. `composer.verbatim = True`: the popup is suppressed and
   the entire typed text becomes the answer with no command parsing, even a
   leading `/no` (`SessionController.submit_message`, `controller.py:536-551`
   — the ask_user gate wins over slash-command parsing unconditionally).
   **Note on tui.md staleness:** `docs/design/tui.md` §9 ("Edge cases")
   describes ask_user as an `ActionPanel` mode with a `TextArea#answer` and
   `ctrl+enter` to submit — that is superseded. Current code
   (`action_panel.py`'s own docstring, `composer.py`'s docstring, and
   `_update_composer`) puts ask_user answering entirely on the persistent
   composer; the ActionPanel is approval-only.
3. **Sub-agent running** (`sub_running and not pending_approval`) — title
   `"Sub-agent running · /abort ends it and tells the model"`. Enabled (not
   disabled like the general busy case) specifically so `/abort` is reachable.
4. **Armed and idle / DONE** (follow-up) — title is
   `"Task done · type a follow-up to continue · Esc clears / shortcuts"` when
   `phase_name == "DONE"`, else `"Message the model · Enter sends · Ctrl+J
   newline · Esc clears / shortcuts"`. Normal slash-command parsing applies.
5. **Disabled** (no session, busy/executing, or a gate is pending) — the box
   is disabled outright; title reads `"no session"` / `"working - the chat box
   is paused"` / `"approve or reject the action above first"`
   (`_composer_idle_title`, `main.py:4921`).

Transitions are driven entirely by the `SessionView` snapshot pushed via
`render_state`; the composer itself holds no session-phase state.

### Slash-command popup

- **Closed** — no leading `/`, or the box is disabled, or `verbatim` is true.
- **Open, no highlight** — a bare `/` was typed: every command listed, none
  pre-armed (`preselect=len(text) > 1` in `composer.py:100`).
- **Open, highlighted** — one or more further characters typed, or an arrow
  key pressed; a row is selected and Enter/Tab complete it to `/name ` (the
  trailing space closes the popup, so the *next* Enter sends normally).
- Closes on: `Escape` (text/focus untouched), a completing Enter/Tab, the box
  becoming empty of a leading slash, or the line committing to an argument
  (`/yolo `) or matching nothing (`/xyz`).

### Action panel (approval gate)

- **Closed** — `display: none`, the default. `ActionPanel.hide_panel()`.
- **Open, approval mode** — one call needs a decision. Shows title
  (`"{prefix}APPROVE · call {position} · {tool} {target}"`), queue strip,
  rendered preview (diff / command line / mcp args), Approve/Reject buttons,
  and a third button that is either **"Approve + auto-edits (a)"** (legacy
  auto-accept-edits, edit-kind gates only) or **"Always: {pattern} (a)"**
  (ruleset mode, any gate carrying `always_pattern`) — `action_panel.py:150-169`.
  Focus lands on the Approve button on show so bare `y`/`n`/`a` bubble to the
  screen (`focus_default`, `action_panel.py:173`).
- **Open, reject-reason mode** — a sub-state entered by pressing `n`/Reject:
  the hidden `#reject-reason` `Input` becomes visible and focused
  (`open_reject_input`); `Enter` (even empty) confirms rejection, `Escape`
  cancels back to approval mode (`close_reject_input`). Tracked by
  `MainScreen.reject_open: reactive[bool]`.
- **Chunked-send wizard mode — DESIGNED BUT NOT YET IMPLEMENTED.** Per
  `docs/design/tui.md` §6, when an outbound payload exceeds the service's
  paste budget the ActionPanel is meant to show a numbered part-wizard
  (`CHUNKED SEND · part 1/3 on clipboard`, with `[space]` skip-ACK,
  `[c]` re-copy, `[esc]` abort). **Current code does not implement this**:
  `controller.py:2125-2130` explicitly short-circuits multi-chunk `Outbound`
  values with `"multi-part outbound - only part 1 copied (chunk walk lands in
  M3)"`, and `ActionPanel` (`action_panel.py`) has no wizard branch at all —
  only `show_approval`/`open_reject_input`/`close_reject_input`. Treat §6 as
  a target design for parity work, not as current behavior to match; the
  `docs/design/agentclip-project-state` memory confirms M3 (PART/ACK
  chunking) was planned but not built as of the last verified milestone check.
  If/when it ships, the key binding table (tui.md §10) notes the `space`
  binding "lands with the feature, not before it."

### Run panel

- **Idle** — `display: none`; `RunningBar` also hidden. `RunPanel.stop()`
  tears rows and tail back to nothing (`run_panel.py:154-161`).
- **Running, tail collapsed (default)** — shown the instant a turn starts
  executing (`ChatView.start_working(label, calls)`); one row per planned
  call, in call-id order, dim for pending (`•`), bold for the currently
  running row (`▶`), and `✓`/`✗`/`−` for finished/failed/skipped. Rows over
  `_MAX_ROWS = 8` are windowed around the running row
  (`_visible_rows`, `run_panel.py:234-240`) with a `"… +N more"` trailer.
  A `run_command` row shows a `"ctrl+o output"` hint while running.
- **Running, tail expanded** — `ctrl+o` (or a click anywhere on the panel)
  toggles the output pane on for the currently *streaming* call only
  (`streaming_call`: the running row's `tool == "run_command"`; toggling on a
  non-streaming running row is a no-op, `toggle_output`, `run_panel.py:185`).
  Shows the last `RUN_TAIL_LINES = 12` lines of that command's output,
  growing live as `CallOutput` messages arrive. Collapses automatically the
  instant that call finishes (`call_finished`, `run_panel.py:175-182`) — the
  next running command starts from a fresh, collapsed pane.
- Every row is guaranteed to resolve to a terminal glyph, even calls the
  engine never actually ran (denied by policy, skipped behind a rejection or
  a cancel) — "a row left pending forever is worse than no row" (tui.md
  §8a).

### Cross-surface state that gates all four widgets

- `pending_approval` — action panel open; composer disabled; run panel not
  concurrently visible (a gate only opens between calls, never mid-call).
- `awaiting_answer` — composer in verbatim answer mode; action panel closed;
  run panel not visible.
- `executing` (a.k.a. `busy` while inside `engine.execute()`) — run panel
  visible; composer disabled; action panel closed (no call is gated while
  auto-run calls are in flight).
- `awaiting_new_session` — composer in task-entry mode; action/run panels
  closed; transcript may already hold events from a prior session if
  `/new` hasn't cleared it yet (it always has, by the time this state shows).

## 4. Inputs from core

The controller drives the surface exclusively through the `ChatView` Protocol
(`src/agentclip/app/view.py`). Methods relevant to this surface:

**Transcript writes** (`view.py:101-109`) — always target the session view
the controller last `focus_session_view`'d, never "whichever tab is on
screen" (delegation is single-flight):
- `add_user(text)` — user task / follow-up / ask_user answer, rendered as
  Markdown, `.ev-user` styling.
- `add_prose(text)` — LLM prose outside CLIP blocks, rendered as Markdown,
  `.ev-prose`.
- `add_call(call: ToolCall)` — one tool call. `ToolCall` (`protocol/types.py:80`)
  = `{id: int, tool: str, params: dict[str,str], raw: str, original_id:
  str|None, issues: tuple[ParseIssue,...]}`. The transcript widget derives its
  one-line summary from `params.get("path"|"command"|"pattern"|"question")`.
- `add_note(text)` / `add_error(text)` — plain informational / red error line.
- `add_outbound(outbound: Outbound, label: str)` — an outbound copy notice.
  `Outbound` (`protocol/types.py:143`) = `{kind: Literal["bootstrap",
  "results","user_answer","note","calibration"], chunks: tuple[str,...],
  total_chars: int, turn: int}`. `chunks[0]` is what the transcript actually
  shows in its collapsible; `len(chunks) > 1` is the not-yet-wired chunk-walk
  case (§3).
- `clear_transcript()`, `has_transcript_events()`, `render_log(meta_lines)`.

**State/chrome push** (`view.py:111-117`):
- `render_state(view: SessionView)` — the one state snapshot, pushed after
  every controller-side change. `SessionView` (`view.py:65-89`) =
  `{session_active, busy, pending_approval, awaiting_answer, has_outbound,
  snapshot: StatusSnapshot|None, session_id="master", session_role:
  Role="master", session_title=""}`. The view maps this onto its own
  reactives (composer mode, gate visibility, run panel visibility) and
  repaints; it is unidirectional.
- `show_gate(action: PendingAction, position: str, queue: str)` /
  `hide_gate()` — opens/closes the action panel. `PendingAction`
  (`engine/engine.py:132`) = `{call: ToolCall, kind: Literal["edit","command",
  "auto"], preview: str, auto_reason: str|None, always_pattern: str|None}`.
  `position` is `"{done+1}/{done+len(pending)}"` (e.g. `"2/5"`,
  `controller.py:1436`). `queue` is the pre-rendered queue strip string,
  `"  ".join(f"{glyph}{id} {tool}" ...)` (`controller.py:1467-1470`).
- `start_working(label: str, calls: Sequence[RunCall] = ())` /
  `stop_working()` — opens/closes the run panel. `RunCall` (`view.py:44-62`)
  = `{call_id: int, tool: str, detail: str, streams: bool=False, glyph:
  str="•"}`. `detail` is already flattened/clipped by the controller — the
  view renders, it does not decide what matters.
- `reset_composer()` — clears the composer text (non-undoable `load_text`).

**Thread contract — the turn executing, call by call** (`view.py:119-132`,
also documented in `tui/messages.py:1-59`): `call_started`, `call_finished`,
`call_output` are called from the **engine's worker thread**, mid-`execute()`
— the only part of the port with this contract. An implementation must be
non-blocking and thread-safe, and must tolerate a `call_id` it has never
heard of (a call the controller did not plan for) and one already resolved
(a parked turn resuming after an `ask_user` mid-plan). The Textual adapter
satisfies this by `post_message`-ing three Textual messages
(`CallStarted`/`CallFinished`/`CallOutput`, `tui/messages.py:84-115`) from the
port methods and doing all actual widget work in the UI-thread handlers
(`main.py:1873-1905`). A GUI frontend needs the equivalent bridge (e.g. a
thread-safe queue drained on the UI event loop) — nothing may touch UI state
directly from that thread.
- `call_started(call_id, tool, detail)` — mark a row running; if the panel
  never planned this row, add it.
- `call_finished(call_id, glyph)` — `glyph` is `✓`/`✗`/`−`.
- `call_output(call_id, chunk)` — **the delta only, never the whole buffer**;
  a chatty command posts one of these per poll slice (5/s). Everything they
  say is redundant with the final results payload; nothing may depend on
  their arrival.

**Notifications** (`view.py:134-144`): `notify(message, *, title, severity,
timeout, markup)` (toast) and `alert(message, severity)` (bell + status-bar
flash trigger, per tui.md §8). Fired on: approval needed (`warning`, "the big
one"), ask_user question, parse error/partial copy, task_done
(`information`). Not fired on: routine auto-run completions, outbound copies
the user just triggered.

## 5. User actions out

All key bindings below are `MainScreen` bindings unless noted; "dynamic"
means gated via `check_action` against a `reactive(..., bindings=True)` field
so the footer dims it when inapplicable (tui.md §10).

| Action | Trigger | Controller call |
|---|---|---|
| Send composer (task / answer / follow-up / dispatch a slash command) | `Enter` in composer (when popup is not consuming it), or `ctrl+s`/`ctrl+enter` from anywhere (priority) | `MainScreen._submit_text` → `SessionController.submit_message(text)` (or resolves the `prompt_new_session()` future directly in task-entry mode) |
| Insert literal newline | `ctrl+j` in composer | composer-local, no controller call |
| Clear composer (undoable) / blur to command mode | `Escape` in composer, two-stage (text present → clear+keep focus; empty → blur) | composer-local |
| Complete popup highlight | `Enter`/`Tab` with popup open and a row highlighted | composer-local (`_complete`), no controller call — the completed text is what a later Enter sends |
| Move popup highlight | `↑`/`↓` with popup open | composer-local |
| Dismiss popup | `Escape` with popup open (first refusal, ahead of the composer's own Escape stages) | composer-local |
| Approve current gated call | `y`, or Approve button | `action_approve()` → `SessionController.submit_decision(Decision.APPROVE, None)` |
| Approve + "stop asking" | `a`, or the third button | `action_auto_edits()` → `submit_decision(Decision.APPROVE_ALWAYS, None)` (ruleset mode, gate carries `always_pattern`) or `submit_decision(Decision.APPROVE_ALL_EDITS, None)` (legacy mode, edit-kind gate only) |
| Reject current gated call | `n`, or Reject button — opens the reason input | `action_reject()` sets `reject_open` + opens the `Input`; **submitting** it (`Enter` in `#reject-reason`, even empty) calls `submit_decision(Decision.REJECT, note_or_None)` |
| Cancel the reject-reason input | `Escape` while it is open | `action_cancel_entry()` closes it, no controller call, gate stays open |
| Cancel the turn's tool calls in flight | `ctrl+x` (priority, while `executing`) | `action_cancel_execution()` → `SessionController.cancel_execution()` (thread-safe `Engine.request_cancel()`; the turn still finishes and reports back — it is not aborted) |
| Show/hide the running command's live output | `ctrl+o` (priority, while `executing`), or a click anywhere on the run panel | `action_toggle_run_output()` → `RunPanel.toggle_output(...)`, no controller call (view-local) |
| Toggle most recent transcript collapsible | `x` (always) | view-local, no controller call |
| Toggle a specific collapsible | `Enter` when it has focus (native Textual) | view-local |
| Scroll transcript | `PageUp`/`PageDown` | view-local |
| Scroll focused panel (diff body, autofocused at a gate) | arrows / `PgUp`/`PgDn` | view-local |
| Cycle permission mode `ask → plan → unattended` | `shift+tab` (priority, **works in every state**, no `check_action`) | `action_cycle_permission_mode()` → `SessionController.cycle_permission_mode()` |
| Jump focus to the composer | `t` | view-local focus call |
| Undo last turn | `u` (dynamic: ≥1 undoable turn, state IDLE/ARMED/DONE) | `SessionController.undo()` |
| Force-ingest clipboard now | `i` (dynamic: session active, not busy, AWAITING_REPLY) | `SessionController.force_ingest()` |
| Re-copy / re-deliver outbound | `c` (dynamic: `has_outbound`); a **second** `c` within 1.5s escalates to redelivery | `SessionController.recopy()` (first press: `park_outbound`; double-tap: `redeliver_outbound` via the port, per view.py:154-170) |
| End session | `e` (dynamic) | `SessionController.end_session()` → SummaryScreen |
| Export chat log | `l` | view-local (`render_log`) |

Slash commands (typed in the composer, dispatched by
`SessionController._handle_command`, `controller.py:608-629`, registry in
`src/agentclip/app/commands.py`): `/help` (`/commands`, `/?`), `/new`,
`/abort`, `/identify`, `/log`, `/mcp`, `/armed [on|off]`, `/mode
[plan|ask|unattended]`, `/yolo [on|off]`. Only `/yolo`, `/mode`, `/armed`
directly touch this surface's state (approval policy / permission mode
badge); the rest affect the transcript (a note/listing) or other surfaces.
Precedence: while `awaiting_answer`, **nothing is parsed as a command** — the
typed text is always the literal answer (`controller.py:541-551`).

## 6. Invariants & edge cases

- **Verdict/gate ordering: calls execute strictly in the LLM's given order,
  never reordered or parallelized.** "Calls execute strictly sequentially in
  the LLM's given order (later calls assume earlier effects — edit-then-test)"
  — `docs/design/tui.md` §2.1 (line ~404).
- **A rejection skips every remaining call in that turn.** "On rejection: all
  remaining calls in the turn are skipped (they presumed the rejected
  effect). The queue strip flips them to `−`." — tui.md §2.4 (line 442).
  Confirmed in code: `controller.py:1440-1444` sets every pending/running
  glyph in `_turn_glyphs` to `−` on `Decision.REJECT`.
- **The gate is a panel, not a modal, deliberately.** "The user needs the
  transcript visible behind the diff for context" — tui.md §2.3 (line 434).
  A GUI parity implementation must not hide the transcript behind the
  approval UI.
- **Errors during auto-run calls never gate or abort the turn.** "The result
  entry carries the error/output and execution continues — the LLM is the
  error handler." — tui.md §2.5 (line 453).
- **YOLO mode bypasses the allowlist *and* deny tokens for edit/command
  kinds**, and wins over the approval table but not over permission-mode
  `unattended`/`plan`, which deny regardless (verdict order is
  architecture.md's lane) — tui.md §2.6 (line 461-462) and §2.6a (line 482).
- **Permission mode (`shift+tab`) can only ever refuse more, never allow
  more**, is never gated on session state or `check_action` (works mid-turn,
  at a gate, disarmed), survives `/new` (unlike YOLO, which resets to
  configured default), and governs every engine in the app run including
  sub-agents — tui.md §2.6a (lines 468-478).
- **Transcript pruning: children beyond `MAX_EVENTS = 500` are unmounted**,
  oldest first, to bound layout cost — `transcript.py:14, 62, 85-86, 93-94`.
  The separate `event_log` (used for `/log`'s export) is **not** pruned, so
  export always has the full history even after display pruning.
- **Autoscroll is fit-or-park, decided per event after layout settles.** An
  event that fits the visible viewport pins the panel to the bottom
  (`anchor()`, which releases on manual scroll-up and re-engages on
  scroll-to-bottom); an event taller than the viewport parks with its top at
  the top of the view so the user reads from the first line. While parked,
  follow-up "noise" (tool calls, notes, outbound) mounts below without
  moving the view; a new conversational **beat** (user or assistant message)
  always re-applies the fit rule — tui.md §1.2 (line 88) and
  `transcript.py:97-116` (`_autoscroll`). **Anchoring must be applied to the
  scroll container, not the individual event widgets** — the latter is "a
  silent no-op in Textual 8" (`transcript.py:11-12`); a GUI implementation
  should just use CSS `overflow-anchor`/scroll-to-bottom equivalents and
  replicate the fit-vs-park branch, not this specific Textual pitfall.
- **Collapsible toggling: `x` always affects the *most recent* collapsible**,
  independent of focus — a deliberate shortcut for "what did that command
  print" without focus navigation — tui.md §4 (line 867),
  `main.py:4849-4858`.
- **Bell/toast triggers are explicit and asymmetric.** Fired on: approval
  needed (severity warning — "this is the big one"), ask_user question,
  parse error/partial copy, chunk ACK ok/NACK (once shipped), task_done
  (information), clipboard provider fault (error). Not fired on: routine
  auto-run completions, or outbound copies the user just triggered (they are
  already looking at the terminal) — tui.md §8 (lines 917-921).
- **Run panel: every planned row must resolve to a terminal glyph**, even
  calls that never actually ran (denied by policy/plan/unattended, skipped
  behind a rejection or a cancel) — "a row left pending forever is worse
  than no row" — tui.md §8a (lines 953-956).
- **The run-panel output pane only ever shows the *currently running*
  streaming call's tail**, collapses automatically the instant that call
  finishes, and only `run_command` rows are expandable (`streams=True`) —
  `run_panel.py:132-141, 175-182`; opening it on a non-streaming row is a
  silent no-op (`toggle_output`, `run_panel.py:185-187`).
- **`call_output` chunks are deltas, never full buffers**; the view (not the
  engine) owns the accumulation buffer, bounded per call at
  `RUN_OUTPUT_LINES = 400` even though the visible tail is only
  `RUN_TAIL_LINES = 12` — "the deque is the truth ... a chatty command costs
  a couple of list operations per poll slice and no render at all [while
  collapsed]" — `run_panel.py:55-63`, `tui.md` §8a (lines 963-967).
  Symmetric with the harness-log-pane division of labour cited there.
  **The buffer is torn down whole when the turn ends** — the model's own copy
  of the output (already tail-capped) lives in the results payload / the
  transcript, never in this buffer.
- **The three-way approve/approve-always/reject choice is per-call, not
  per-turn**, and "Approve + auto-edits" only ever appears for `kind ==
  "edit"` gates or gates carrying a ruleset `always_pattern` — never for
  `run_command` gates in legacy mode ("commands stay allowlist-or-prompt" —
  tui.md §2.4, line 441).
- **The gate's title must be built from the same param the verdict was
  computed from, per tool — never "whatever params exist."** An `mcp` call
  carrying a decoy `command:` or `path:` param must not retitle the gate as
  if it were `run_command`/a file op — `action_panel.py:138-146` and its
  docstring (`preview_renderable`, lines 41-58): "a decoy `command: git
  status` riding an mcp call would otherwise repaint the gate as a harmless
  shell line."
- **`ask_user` answering happens on the composer, not a modal or the action
  panel**, is *not* a gate/approval (no `y`/`n`/`a`), and during it every
  bare-letter screen shortcut is naturally inert because a focused `TextArea`
  swallows the keys — `action_panel.py` docstring (lines 15-16), `tui.md`
  line 3 (the "no text widget focused" precondition for bare-letter safety).
- **No `priority=True` letters anywhere** — focus-based suppression (a
  focused `TextArea` intercepting the key) is the sole safety mechanism
  keeping single-letter screen shortcuts (`y`/`n`/`a`/`u`/`c`/`i`/`w`/`e`/
  `x`/`t`/`l`) from firing while the composer or reject-reason input holds
  text focus — tui.md §10 (line 1038).
- **Command-popup pre-selection is a safety rule, not cosmetic.** A bare `/`
  highlights nothing; only a typed letter or an arrow press arms a row —
  otherwise `/` + `Enter` + `Enter` would run `COMMANDS[0]`, and `/yolo` is
  deliberately last in the registry as a second, independent lock on the
  same door — `command_popup.py:26-32`, `commands.py:58-66`,
  tui.md §3.3a "Ordering is a safety property" (line 596).
- **Chunked-send (§6) is a designed, undelivered feature** — see §3 above.
  Do not build GUI parity against it as if it exists in the reference
  implementation today; build against the wizard spec in tui.md §6 as a
  target, and confirm against the TUI codebase at implementation time in
  case it has since landed.

## 7. Textual-specific details NOT to carry over

- **`anchor()`/`release_anchor()` container-level scroll pinning and its
  Textual-8-specific pitfall** (anchoring individual event widgets is a
  silent no-op) — `transcript.py:11-12`. A GUI just needs "pin to bottom
  unless the user scrolled up, unless the new content is taller than the
  viewport, in which case scroll its top into view instead." No equivalent
  gotcha exists in HTML/CSS.
- **`priority=True` Textual bindings to steal keys from a focused
  `TextArea`** (`F3`, `F6`, `F7`, `F8`, `ctrl+x`, `ctrl+o`, `ctrl+s`,
  `ctrl+enter`, `shift+tab`) — this whole mechanism exists because Textual
  routes key events to the focused widget first and only "priority" bindings
  at the `Screen`/`App` level can override that, and `shift+tab` additionally
  had to *override* Textual's own default `focus_previous` binding
  (tui.md §2.6a, lines 474). A GUI's global keyboard shortcuts (e.g. browser
  `keydown` on `document` with `capture: true`, or an Electron/pywebview
  accelerator) don't have a competing default-focus-navigation binding to
  fight, so this whole "which layer intercepts first" negotiation collapses
  to "the shortcut always fires unless a text input is focused and the key
  isn't in that input's own allowed set."
- **The command popup's single-`Static`-painting-Rich-`Text` implementation
  to guarantee it can never steal focus** (`command_popup.py:8-16`) — a
  deliberate workaround for Textual's per-widget focus model (an `OptionList`
  would be focusable). In HTML this is just a `role="listbox"` positioned
  absolutely above the input with `pointer-events` limited to click-to-select;
  no special non-focusability trick is needed, and mouse click-to-select
  should probably be *added* (the TUI drives it by method calls from the
  composer only, since Textual mouse-click handling on a `Static` list would
  have been more code than it was worth).
- **`Collapsible` being a native Textual widget with built-in
  focus/tab/Enter-toggle behavior** (`tui.md` §4, line 867) — a GUI just uses
  a `<details>`/`<summary>` element or an expand/collapse div; there is no
  Textual focus-chain participation to replicate.
- **The reject-reason `Input` being hidden/shown by flipping `display`
  on a pre-mounted widget rather than mounting/unmounting** — a Textual
  performance idiom (avoiding a mount/unmount round trip); irrelevant in a
  DOM where toggling visibility/mounting is cheap either way.
- **BMP-only glyph restriction** (`✓`✗`▶`•`−`, braille spinner frames
  `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) — an artifact of the Windows Terminal / legacy console font
  brief cited throughout tui.md (e.g. §2.3 line 436, `running_bar.py:8`). A
  web frontend has full Unicode/emoji/SVG icon freedom and should not
  inherit this constraint, though reusing the same glyphs is fine for visual
  continuity if desired.
- **`rich.syntax.Syntax`/pygments-based diff and code rendering inside a
  `Static`** (`action_panel.py:22,34-90`, tui.md §5) — a GUI should use a
  proper web diff/syntax-highlighting component (e.g. a diff viewer library,
  or `<pre>` + a JS highlighter); the specific choice of `theme="ansi_dark"`
  and `word_wrap=False` is a terminal-rendering concern, not a UX
  requirement (the UX requirement is "long diff lines are not wrapped,
  because wrapping breaks the hunk's column alignment" — that requirement
  does carry over).
- **The run panel's `_MAX_ROWS = 8` windowing around the running row** is a
  terminal-real-estate compromise ("the composer must not be pushed off
  screen by a model that ignored [an ~8-call turn guideline]",
  `run_panel.py:65-67`); a GUI with a scrollable panel and more vertical
  room may not need to window rows at all, or could window at a much higher
  count.
- **`check_action`/`reactive(..., bindings=True)` dynamic-dimming of Footer
  keys** — Textual's mechanism for showing a key as available-but-dimmed;
  a GUI's equivalent (disabled button state, greyed-out menu item) is
  already how such affordances normally work and needs no special porting
  effort, just parity on *which* conditions gate *which* action (captured
  in §5's table and the `check_action` column of tui.md §10).
