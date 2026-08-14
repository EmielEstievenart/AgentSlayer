# UI Brief: Window Tabs, Delegation & Session Summary

Audience: engineers building a second frontend (pywebview/HTML/JS) that must
reach feature parity with the Textual TUI, and maintainers keeping both UIs in
sync. The shared core is `SessionController` (`src/agentclip/app/controller.py`)
driving a `ChatView` protocol (`src/agentclip/app/view.py`); this brief
describes the **behavior** a second `ChatView` implementation must reproduce,
not the Textual widgets that happen to implement it today.

Primary sources: `docs/design/tui.md` §1.2, §1.5, §1.6, §3.4c, §3.4e;
`src/agentclip/tui/widgets/window_tabs.py`; `src/agentclip/tui/screens/summary.py`;
`src/agentclip/tui/screens/main.py`; `src/agentclip/app/view.py`;
`src/agentclip/app/controller.py`; `src/agentclip/app/types.py`.

---

## 1. Purpose

AgentClip drives up to two browser chat windows per session: the **master**
window, where the user's own conversation runs, and a **sub-agent** window the
model may open via the `delegate` tool to run a bounded sub-task in a second,
independent chat. This surface is the UI that:

- lets the user see and steer which browser window is "on screen" right now
  (the tab bar), without ever redirecting the automation's actual target
  (the "live" window) — those are two different pointers;
- shows each window's whole conversation history as one continuous transcript,
  with delegated sub-tasks appended under dividers rather than minted as new
  panes;
- reports, at a glance, whether a window's most recent job succeeded, failed,
  or is still running (the tab's state glyph and label);
- makes unmistakable, everywhere state is shown, *whose* conversation is
  currently being narrated — the user's, or a delegated sub-agent's — because
  every other on-screen distinction (transcript content, diffs, questions)
  looks identical either way;
- gives the user a way to close out a session and read what happened
  (turns, tool calls, chars moved, an optional sub-agent count, the model's
  own `task_done` summary text) with three simple next actions: undo the last
  turn, start fresh, or just go back.

The organizing idea, stated in `tui.md` §1.6: **a tab is a browser window,
not a session view.** A window exists before any session does, keeps its own
service/region/appearance calibration, and survives `/new`. A *session* (the
user's own run, or one delegated sub-agent run) is a temporary tenant of a
window's transcript.

## 2. Anatomy

```
WindowTabs                              # two-row strip, one selection total
├── row 1 "master windows"              # today: exactly one, id "m1"
│   └── tab "MASTER · <service>"        # label = name · service, + state glyph
└── row 2 "sub-agent windows of the     # today: exactly one, id "m1-s1"
    │      selected master"
    └── tab "SUB-AGENT · <service>"     # "▶ …" running / "✓ …" / "✗ …" / plain

Per-window transcript panel (one persistent panel per window; exactly one shown)
├── master panel                        # the user's own conversation, unbroken
└── sub-agent panel                     # every delegated run ever opened here,
    ├── "── task: <title> ──"           #   back to back, each under its own
    ├── <run 1's events>                #   divider — never cleared between runs
    ├── "── task: <title> ──"
    └── <run 2's events>

Chrome that re-badges while a sub-agent run is live (main.py's "who is talking"):
├── status bar's watch segment          # "◆ SUB-AGENT · <phase>" in magenta (.st-sub)
├── approval gate title                 # "SUB-AGENT ‹task title› · <call>"
└── every bell/toast                    # prefixed "sub-agent: "

SummaryScreen (modal, on demand via 'e')
├── title "SESSION SUMMARY"
├── stats table (label/value rows, no header, unbordered)
└── task_done summary text (Markdown, or "*(the model sent no summary)*")
└── hint row: "u undo last turn · t new session · l export chat log · escape close"
```

Two independent pointers sit underneath the tab bar (`tui.md` §1.6, main.py
docstring lines 109–132):

- **selected window** (`_selected_window` / `_calibrating`) — what the user is
  looking at and what the settings sidebar configures. Moved by clicking a
  tab, or by `F6`.
- **live window** (`_live`) — which window the automation (paste click, finish
  detector poll, auto-copy click) is actually driving right now. Moved only
  by `start_browser_chat` / `end_browser_chat`, which the controller calls
  around a delegation. **Never** moved by tab selection.
- **focused panel** (`_focused_panel`) — which window's transcript the
  controller's `add_*` calls are currently writing into. Moved only by
  `focus_session_view` (called by the controller), never by a tab click. This
  is what lets the user read the master's transcript while a sub-agent run's
  output keeps landing correctly in the sub-agent's panel.

All three coincide almost always; they visibly diverge for exactly the
duration of a delegation, which is the state a second frontend most needs to
get right.

## 3. States

### 3.1 Tab state (label + glyph)

Each tab's text is `<name> · <service>` (service omitted if unset), prefixed
with a state glyph that is **derived from the window's run history, not
stored**:

| Window | Condition | Prefix |
|---|---|---|
| master | always | none (the master doesn't get a run glyph) |
| sub-agent | no run has ever started in this window | none |
| sub-agent | a run is in flight (its slice has no `end` yet) | `▶ ` |
| sub-agent | the last run finished and handed a result back | `✓ ` |
| sub-agent | the last run ended without a result (refused, over-budget, aborted, crashed) | `✗ ` |

Source: `main.py:_window_label` (lines 1615–1637), `window_tabs.py` line 36
(`"▶ SUB-AGENT · claude"` / `"✓ …"` / `"✗ …"`). Only the **last** run's
outcome is shown — earlier runs in the same window are still readable by
scrolling its transcript; the tab is a status light, not a log.

### 3.2 Which transcript is visible

Exactly one window's transcript panel is displayed at a time, and it is
always the **selected** window's (`_show_panel`, main.py:1610). Clicking a
tab (or `F6`) changes which panel is shown; it does not change which panel
new output is written into — that is `_focused_panel` (§2), moved only by the
controller via `focus_session_view`.

### 3.3 Master vs. sub-agent badge

While a delegated run is in flight, `SessionView.session_role == "subagent"`
(`app/view.py:88`) and every piece of chrome that reports "what is currently
happening" is re-derived from the sub-agent's state, not the master's:

- **Status bar watch segment**: rebadged `◆ SUB-AGENT · <phase text>`, style
  class `.st-sub` (magenta — used nowhere else in the app), computed in
  `main.py:_watch_segment` (4942–4953).
- **Approval gate title**: prefixed `SUB-AGENT ‹<task title>› · ` (or
  `SUB-AGENT · ` if the title is empty), `main.py:_gate_prefix` (1829–1836).
  Without this, an edit diff mid-delegation is visually indistinguishable
  from one in the user's own conversation.
- **Bells/toasts**: every `notify`/`alert` call the controller makes while a
  sub-agent is active is prefixed `"sub-agent: "`. This is done by the
  **controller**, not the view — `SessionController._alert_prefix`
  (`controller.py:1605–1609`), which checks whether the currently-active
  session's role is `"subagent"`. A second frontend gets this for free as
  long as it renders whatever string the controller hands it; it must not
  re-derive the prefix itself.

When the run ends, `session_role` reverts to `"master"` and all three revert
with it (this happens as part of `_run_subagent`'s `finally`, §4).

### 3.4 Summary screen lifecycle

- **Entry**: `e`, only when `session_active and not busy and phase in
  (AWAITING_REPLY, DONE)` (`main.py:check_action`, 1308–1313 — this is what
  disables the footer hint and refuses the keypress otherwise). It is **not**
  auto-shown on `task_done`; the user stays in the chat and may follow up
  (`tui.md` §1.5) and opens the summary manually whenever they want it.
- **While open**: the underlying session is untouched; the summary is a
  read-only report plus three exits.
- **`l` (export)**: does not close the screen. The controller writes the chat
  log, then re-shows the summary (`SessionController._show_summary`'s `while
  True` / `continue` loop, `controller.py:1996–2002`).
- **`u` (undo)**: closes the screen and undoes **the single most recent
  turn** (`SessionController._undo_flow`, `controller.py:2066`) — files that
  turn changed are restored from a per-turn backup; `run_command` side
  effects are **not** undone. This itself is gated behind a yes/no confirm
  (`ChatView.confirm`) with explicit copy about what will and won't be
  reverted. On confirm, a revert notice is composed and copied to the
  clipboard for the user to paste back into the chat. The session stays
  active and the user is returned to the chat view, not back to the summary.
- **`t` (new session)**: closes the screen and runs the same session-reset
  path as `/new` (`SessionController._reset_session`) — see §6 for exactly
  what does and does not get cleared.
- **escape (close)**: closes the screen with no side effect. The transcript
  and session state are exactly as they were; this is "back", not "end".

## 4. Inputs from core

The `ChatView` protocol (`src/agentclip/app/view.py`) is the whole contract.
Methods relevant to this surface:

```python
# -- session views (window transcripts) --
async def open_session_view(self, session: SessionRef) -> None: ...
def focus_session_view(self, session_id: str) -> None: ...
async def finish_session_view(self, session_id: str, note: str, ok: bool) -> None: ...

# -- sub-agent transport --
def delegation_available(self) -> bool: ...
def delegation_missing(self) -> tuple[str, ...]: ...
async def start_chat(self, session: SessionRef) -> bool: ...
async def end_chat(self, session: SessionRef) -> None: ...

# -- summary --
async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str: ...
```

Plus the periodic state push:

```python
@dataclass(frozen=True)
class SessionView:
    session_active: bool
    busy: bool
    pending_approval: bool
    awaiting_answer: bool
    has_outbound: bool
    snapshot: StatusSnapshot | None
    session_id: str = "master"
    session_role: Role = "master"      # "master" | "subagent"
    session_title: str = ""
```
(`app/view.py:66–89`)

### `SessionRef` — the identity carried across the port

```python
@dataclass(frozen=True, slots=True)
class SessionRef:
    id: str          # "master", "sub-1", "sub-2", ...
    role: Role        # "master" | "subagent"
    title: str        # short label for the transcript tab/divider
    chat_name: str    # the generated chat name this session's replies must carry
```
(`app/types.py:66–78`). `title` comes from `_short_title(req.task)` on the
controller side (a squeezed first line of the delegated task).

### Call sequence for one delegated run (controller → view)

From `SessionController._run_subagent` / `_sub_run`
(`controller.py:1613–1812`), in order:

1. `view.delegation_available()` — checked **before** any engine is built.
   `False` ⇒ the model gets an `error` result naming
   `view.delegation_missing()`; no view calls happen at all, no tab opens.
2. (controller snapshots and swaps its in-memory session context; no view
   calls)
3. (controller builds the sub-agent's `Engine` against the sub-agent window's
   own frozen service key; no view calls)
4. `view.open_session_view(ref)` — appends the `── task: <title> ──` divider
   to the sub-agent window's persistent panel, records where this run's
   events begin, badges the tab `▶`, and **focuses** that window (moves both
   `_focused_panel` and the selected/visible tab).
5. `view.add_user(task_text)`, `view.add_note(...)` — the composed sub-task
   bootstrap, written into the now-focused sub-agent panel.
6. `view.start_chat(ref)` — finds and clicks the sub-agent window's new-chat
   control. **Boolean, all-or-nothing.** `False` means nothing was clicked
   and nothing was retargeted; the delegation aborts immediately with zero
   paste calls, because a sub-agent's bootstrap landing in the master's chat
   would corrupt it irrecoverably (`controller.py:1795–1799`,
   `tui.md` §3.4c step 5).
7. Ordinary turn loop runs against the sub-agent's engine, writing into the
   focused (sub-agent) panel via the same `add_*` calls the master uses.
8. On completion (success, refusal, budget overrun, abort, or crash — all
   paths converge here): `view.end_chat(ref)` returns automation focus to the
   master window; `view.finish_session_view(session_id, note, ok)` appends
   the outcome note to the sub-agent panel and re-badges its tab `✓`/`✗`;
   the controller restores its saved master context and calls
   `view.focus_session_view(master_id)` to hand focus back to the master
   panel.

`finish_session_view`'s `ok` parameter is **always supplied by the caller**,
never inferred by the view — every ending (delivered, refused, over budget,
aborted, crashed) funnels through the controller's one `finally`, so the view
never has to guess what happened from the note text alone
(`tui.md` §1.6, "ok is a parameter").

### Summary payload

`show_summary(rows, summary)` — `rows` is a flat list of `(label, value)`
string pairs built by `SessionController._stats_rows`
(`controller.py:2008–2023`):

```
service               <preset key or "-">
turns                 <count>
replies ingested      <count>
tool calls            <tool×N, tool×N, ...> or "none"
sub-agent runs        <count>            # present only if > 0
chars copied out      <formatted count>
chars ingested        <formatted count>
session dir           <path>             # present only if a session exists
```

`summary` is the model's own `task_done` text verbatim, or empty (the view
renders a placeholder). The method returns one of four strings —
`"undo" | "new" | "close" | "export"` — which the controller branches on
(§3.4).

## 5. User actions out

| Action | Fires | Controller entry point | Notes |
|---|---|---|---|
| Click a tab | `WindowTabs.WindowSelected(window)` | *(view-local; no controller call)* | Moves selected window + sidebar target only. Re-fires even on the already-selected tab (see §6) so "click the tab I'm on" still works as "show me this window". |
| `F6` | *(view-local)* | *(none)* | Cycles the same selection `order()` returns: every master, then the selected master's sub-agent windows. Priority binding, `show=False` (not in the footer). |
| `e` | `action_end_session` | `SessionController.end_session()` | Gated — see §3.4. Spawns the summary flow, which calls `view.show_summary(...)`. |
| `u` (in summary) | `SummaryScreen.action_undo` → dismiss `"undo"` | `SessionController._undo_flow()` (via `_show_summary`'s branch) | Confirm dialog first (`view.confirm`); undoes one turn. |
| `t` (in summary) | `SummaryScreen.action_new` → dismiss `"new"` | `SessionController._reset_session()` | Same teardown as `/new`. |
| `l` (in summary) | `SummaryScreen.action_export` → dismiss `"export"` | `SessionController._export_log()`, then re-opens the summary | Loop, not a terminal action. |
| `escape` (in summary) | `SummaryScreen.action_close` → dismiss `"close"` | *(no controller call)* | Pure UI dismissal. |
| `/abort` (typed in composer) | command dispatch | `SessionController` — ends the delegated run in flight | No-op + warning if nothing is delegated. Not tab-specific — it targets whatever sub-run is active regardless of which tab is selected. |

Clicking a tab and `F6` are deliberately **not** routed through the
controller at all — they are pure view-side navigation over state the view
already holds (which window is selected, which panel is visible). The
controller only ever learns about a delegation's progress through the
`open_session_view` / `focus_session_view` / `finish_session_view` calls it
itself makes (§4); it never asks "which tab is the user looking at."

## 6. Invariants & edge cases

- **One persistent transcript per window, appended, never re-created.** A
  delegation does not mint a new pane; it appends a divider
  (`── task: <title> ──`) to the sub-agent window's one panel and keeps
  writing. `open_session_view` docstring, `main.py:1473–1494`; the window's
  panel is mounted once for the life of the app run.
- **Sub-run dividers + slicing.** The panel's underlying event log is never
  pruned per-run; each run's `[start, end)` slice is remembered separately
  (`_SubRun` dataclass, `main.py:439–458`) purely so `render_log`
  / export can print one `## sub-agent: <title> (<chat_name>)` heading **per
  run** instead of one heading over N unrelated sub-tasks
  (`main.py:1441–1466`, `tui.md` §1.6 "render_log is still per run").
- **Why the tab bar is not a generic tab widget** — this is the load-bearing
  design note in `window_tabs.py` (module docstring, lines 1–31):
  - Two rows means two potential selections. A generic widget (Textual's
    `Tabs`) gives each row its own `active` reactive; today each row has
    exactly one tab, so a click on an already-active single tab is a
    **no-op that fires no event at all** in the underlying framework
    (`Tabs._activate_tab` assigns the same value; the reactive never
    changes, so `TabActivated` never posts). A hand-rolled bar owns **one**
    selection across both rows instead, so a click always produces a
    `WindowSelected` message, even on the tab that's already selected — this
    is required, not incidental: it's how "click the tab I'm on" gets to mean
    "show me this window" (re-establish view) after focus was moved
    elsewhere by the controller mid-delegation.
  - Both rows would also auto-activate their first tab on mount
    independently, so whichever row mounts last silently steals the
    selection.
  - A GUI implementation has neither problem — a click handler on a DOM
    element naturally distinguishes "already selected" from "not selected"
    and can always fire its own event — so this whole workaround is
    Textual-specific (see §7).
- **A no-op click still repaints nothing.** `_select_window` early-returns
  when `window == self._selected_window` (`main.py:1598–1599`) — the
  `WindowSelected` re-post exists for callers (the summary/composer path)
  that need "select this window" as an idempotent action, not to force a
  rebuild. A second frontend can implement this either way (early-return or
  idempotent repaint) as long as repainting never *loses* a live readout
  that was written directly into DOM/state outside the render cycle — the
  TUI's reason for early-returning is defensive, not load-bearing.
- **Selecting a tab never touches the live/automation target.** This is the
  single most important invariant for correctness: looking at a window is
  not driving it. A click on the master tab while a sub-agent run is mid-flight
  must not redirect the next paste into the master's chat.
  (`window_tabs.py:311`'s equivalent in tui.md §1.6, and `_select_window`'s
  docstring, `main.py:1578–1596`.)
- **Focus (where output lands) is separate from selection (what's shown) is
  separate from "live" (what's driven).** Three independent pointers, only
  two of which a GUI frontend needs to reproduce faithfully as UI state
  (selection and focus — "live" is a browser-automation concept specific to
  this app's screen-scraping transport and may not exist at all in a
  different transport).
- **What happens when a sub-agent run finishes or fails.** In every case —
  success, refusal (uncalibrated window, deleted service preset), budget
  overrun, `/abort`, or an unhandled exception — the same `finally` in
  `_run_subagent` (`controller.py:1693–1707`) runs: the panel gets an outcome
  note, the tab re-badges `✓`/`✗` via `finish_session_view`, automation
  control returns to the master window (`end_chat`), the master's saved
  context is restored, and `focus_session_view(master_id)` hands transcript
  focus back to the master panel. **The result always reaches the model** as
  an `error` or `ok` `ToolResult` on the `delegate` call — there is no path
  where a sub-run silently vanishes from the model's point of view.
- **`/new` keeps both tabs and both drawn windows; it only forgets runs.**
  `_remove_session_views` (`main.py:1538–1549`) clears `_sessions` and
  `_sub_runs` and re-labels the sub-agent tab back to its bare `SUB-AGENT ·
  <service>` state (dropping any `✓`/`✗`), but the window itself — its
  screen region, its service, its captured button appearances — is
  untouched. `clear_transcript` (`main.py:1393–1419`) is the actual teardown
  hook: it calls `_remove_session_views`, resets the live pointer to MASTER,
  re-selects the master tab, and restarts the finish-detector poller against
  the surviving master window. A second frontend's "new session" action must
  reproduce this split: **session/run bookkeeping resets; window
  calibration does not.**
- **A tab click during a mid-turn delegation is allowed and expected**,
  unlike most other mutating actions in this app, precisely because it does
  not touch `_live` — it is pure observation.
- **The summary's `u` undoes one turn, not "the whole session."** Despite
  older prose in `tui.md` §1.5 saying "undo entire session (turn-by-turn
  restore)", the current implementation (`controller.py:2066`,
  `SummaryScreen`'s own binding label `"undo last turn"`) undoes exactly the
  most recent turn per press and returns the user to the live chat (not back
  to the summary). Repeated undos require reopening the summary and pressing
  `u` again each time. **Flag this discrepancy to the docs maintainer** — see
  §Ambiguities below; a second frontend should follow the code (single-turn
  undo), not the older doc prose.
- **`e` is gated**, not always available: `session_active and not busy and
  phase in (AWAITING_REPLY, DONE)` (`main.py:1308–1313`). A GUI should
  disable/hide its "end session" affordance under the same conditions rather
  than let the summary open mid-turn.
- **Delegation eligibility is decided once, at session bootstrap**, from the
  sub-agent tab's calibration at that moment (`can_delegate` against the
  **sub-agent tab's own service profile**, not the master's —
  `delegation_available`, `main.py:4644–4659`). Calibrating the sub-agent
  window mid-session does not retroactively add `delegate` to a
  conversation the model has already read; the view only surfaces a toast
  telling the user to `/new`.

## 7. Textual-specific details NOT to carry over

- **The whole hand-rolled-tab-bar rationale is a Textual limitation, not a
  UX requirement.** `window_tabs.py`'s reason for not using
  `textual.widgets.Tabs` — two independent `Tabs.active` reactives per row
  silently swallowing a click on an already-active 1×1 row, and both rows
  racing to auto-activate their first tab on mount — has no equivalent in a
  DOM/HTML tab strip. A GUI frontend can use whatever native or
  component-library tab widget it likes; the only *behavioral* requirements
  to preserve are (a) exactly one selection spans both rows/levels, (b) a
  click always fires a "selected" event even when clicking the currently
  active tab, and (c) selecting a tab never implicitly changes the
  automation's live target or the panel that new output is written into.
- **"Deliberately not focusable."** The Textual tab bar refuses keyboard
  focus so a click never steals it from the composer text box
  (`window_tabs.py:28–30`). This was a workaround for Textual's default
  click-to-focus behavior on `Static`-derived widgets; a web tab strip can
  be focusable/accessible normally — the underlying goal (don't let a tab
  click yank keyboard focus away from the message composer) is still worth
  keeping, but the mechanism (`focusable = False` equivalent) is
  framework-specific.
- **`F6` cycling is a Textual keybinding workaround for the tab bar's
  unfocusability.** Since the bar can't take focus, there's no way to
  Tab/arrow-key into it; `F6` is the substitute "next tab" gesture bound at
  the screen level. A GUI implementation with a normally focusable, clickable
  tab strip doesn't need an equivalent global hotkey (though offering one is
  reasonable UX, it isn't load-bearing the way it is in the TUI).
- **Two-row CSS layout (`win-row-master` / `win-row-sub`, indentation via
  `padding-left: 3`)** is presentational, sized for a fixed-width terminal
  grid. Nothing about the two-level master→sub-agent hierarchy requires two
  literal rows in a GUI — a nested/indented list, a flyout, or a
  master-selector-plus-sub-selector pair are all equally valid as long as the
  "N masters, each with its own set of sub-agent windows, only the selected
  master's subs shown" tree shape (`WindowSpec`, `window_tabs.py:56–68`) is
  preserved.
- **`SummaryScreen` as a Textual `ModalScreen[str]` returning one of four
  sentinel strings** (`"undo" | "new" | "close" | "export"`) is an
  implementation detail of how the TUI's modal stack communicates a result
  back to an `await`ing coroutine. A GUI just needs to invoke the equivalent
  controller calls (`SessionController._undo_flow` via confirm,
  `_reset_session`, export, or simply close) from whatever UI affordance
  (buttons, not single-letter keys) it uses; the `u`/`t`/`l`/`escape`
  single-key bindings are a terminal-app convention, not a contract.
- **BMP-only glyphs (`▶`/`✓`/`✗`)** are chosen because the TUI brief requires
  Windows Terminal-safe Basic Multilingual Plane characters (see `tui.md`
  §2.3's queue-strip note for the same constraint). A GUI frontend has no
  such restriction and can use any icon/color/badge treatment it likes for
  "running / succeeded / failed" — the only requirement to preserve is the
  three-state distinction itself (never-run is visually distinct from both).
- **The magenta `.st-sub` CSS class and bell/toast text prefixing** are the
  TUI's specific choice of "how to make sub-agent state visually distinct
  inside a monochrome-ish terminal UI." A GUI has much richer options (a
  persistent banner, a colored panel border, a labeled badge next to the tab)
  — the invariant to keep is *some* unmistakable, always-visible signal of
  "you are currently looking at / approving / being asked about a sub-agent's
  work, not your own conversation," not this specific rendering.

---

## Ambiguities found

1. **`tui.md` §1.5 vs. the actual undo behavior.** The design doc's prose
   says summary's `u` triggers "undo entire session (turn-by-turn restore,
   with ConfirmScreen)"; the code (`SessionController._undo_flow`,
   `controller.py:2066`) and `SummaryScreen`'s own binding label ("undo last
   turn") both undo exactly one turn per invocation and return to the live
   chat rather than looping back to the summary. Recommend updating
   `tui.md` §1.5; this brief follows the code.
2. **`ctrl+q` on the summary screen.** `tui.md` §1.5 lists `ctrl+q` (quit)
   as a summary-screen binding, but `SummaryScreen.BINDINGS`
   (`summary.py:22–27`) does not declare it — it works there only because
   `ctrl+q`/quit-confirmation is a global app-level binding
   (`AgentClipApp.action_quit`, `app.py:845`) that reaches every screen,
   not something specific to the summary modal. Not a functional gap, just
   worth knowing it's not part of `SummaryScreen`'s own contract if a GUI
   models "screens" as owning their own key tables.
3. **Whether "focused panel" needs its own first-class concept in a GUI.**
   In the TUI it's a real distinct pointer because Textual widgets are
   mounted/hidden explicitly. A GUI implementation might collapse "focused
   panel" and "selected tab" into fewer concepts as long as it can still
   answer, unambiguously, "which DOM node do new transcript events append
   to" independent of "which tab is visually highlighted" during a
   delegation — I described it as a separate invariant (§6) because getting
   this wrong is the specific bug class the TUI's docstrings call out
   ("looks exactly like data loss"), not because a GUI must literally
   replicate three separate variables.
4. **No add/remove-window controls exist today.** Everything about window
   ids (`WindowSpec`, `_WINDOW_SLOTS`) is shaped for N masters × N sub-agents
   per `tui.md` §1.6, but only one of each is ever instantiated, and there is
   no UI (TUI or otherwise) for creating additional windows. A second
   frontend building toward parity should treat the one-master/one-sub-agent
   shape as the current real contract, not build out N-window chrome
   speculatively.
