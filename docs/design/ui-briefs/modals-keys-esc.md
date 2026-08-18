# UI Brief: Modals, Global Keybindings, Esc Behavior

Audience: engineers building a second frontend (pywebview/HTML/JS) that must
reach feature parity with the existing Textual TUI, and maintainers keeping
both UIs in sync. This brief describes BEHAVIOR framework-neutrally — the
Textual mechanics that implement it (priority bindings, focus routing,
`check_action`, CSS) are called out separately in section 7 so they are not
mistaken for contract.

Primary sources: `docs/design/tui.md` (§§1.1, 1.3, 1.5, 2.4, 2.6a, 3.3a,
3.3c, 3.4a, 3.5, 8, 9, 10); `src/agentclip/shell/tui/screens/settings.py`,
`help.py`, `confirm.py`, `text_entry.py`, `summary.py`, `main.py`;
`src/agentclip/shell/tui/app.py`; `src/agentclip/shell/tui/widgets/composer.py`,
`action_panel.py`; `src/agentclip/shell/app/commands.py`, `controller.py`,
`view.py`.

---

## 1. Purpose

This surface covers everything that is **not** the transcript, the
approval-gate diff, or the sidebar/ELEMENTS columns: the six modal
screens, the app-wide and screen-wide keybinding table, the staged `Esc`
behavior, permission-mode cycling, the ARMED/DISARMED switch, and the
bell/toast notification policy. It is the parity contract for a second
frontend because almost none of it is optional chrome — `shift+tab`,
`F5`, the `Esc` stages and the ask-user-wins-over-slash-parsing rule are
load-bearing invariants the core (`shell/app/controller.py`, `shell/app/commands.py`)
was written to, not presentation choices a GUI is free to reinterpret.

The one hard rule that must survive porting word-for-word: **while the
composer is parked on an `ask_user` answer, whatever the user typed is
delivered to the model verbatim — never parsed as a slash command.**
(`shell/app/controller.py:541-554`, `submit_message`). A GUI text box that ran
its own command-parsing before checking "are we in answer mode?" would
silently eat a legitimate answer like `/etc/hosts` or `/no`.

---

## 2. Anatomy

### 2.1 Screen inventory

```
AgentClipApp
├── MainScreen                  # default; not itself a modal, but hosts the docked composer
├── ServiceEditorScreen         # F2 — out of scope for this brief (separate UI surface)
├── SettingsScreen[str|None]    # F4 — theme picker
├── SummaryScreen[str]          # e (end session)
├── ConfirmScreen[bool]         # generic y/n confirm
├── HelpScreen[None]            # F1 / ?
└── TextEntryScreen[str|None]   # manual-paste fallback for force-ingest
```
(`docs/design/tui.md:14-19`; `src/agentclip/shell/tui/app.py:29-33`)

### 2.2 SettingsScreen (F4) — `src/agentclip/shell/tui/screens/settings.py`

A `ModalScreen[str | None]`. One `TabbedContent` with a single
"Appearance" tab (deliberately over-structured for tabs that don't exist
yet — settings.py:1-14), holding a `RadioSet` of four themes:

| radio id | theme name | label |
|---|---|---|
| `theme-textual-light` | `textual-light` | Light |
| `theme-textual-dark` | `textual-dark` | Dark |
| `theme-claude-warm` | `claude-warm` | Claude Warm |
| `theme-claude-dark` | `claude-dark` | Claude Dark |

(`settings.py:27-32`; the two Claude themes are registered by
`AgentClipApp.on_mount` from full palettes defined in `app.py:38-72`.)

Below the radio set: `Save` (primary) and `Cancel` buttons, and a hint
line: *"selecting a theme previews it live · escape cancels (reverts
preview)"*.

**Model**: selecting a radio button applies the theme **immediately**
(`self.app.theme = ...`, `settings.py:71-77`) — live preview, not a
staged edit. `Save` just hands back whatever theme is currently applied
(`settings.py:81-84`). `Cancel`/`Escape` restore the theme that was
active when the screen opened (`_initial_theme`) before dismissing with
`None` (`settings.py:91-93`).

### 2.3 HelpScreen (F1 / `?`) — `src/agentclip/shell/tui/screens/help.py`

A `ModalScreen[None]`, one scrolling block of prose plus one **generated**
section. The chat-commands section (`commands_block()`, `help.py:25-29`)
is rendered from `agentclip.shell.app.commands.COMMANDS` — the exact same tuple
that drives the slash-command popup, the `/help` note, and the
"unknown command" hint — so a second frontend that renders its own help
screen must pull from that same registry rather than hand-writing prose,
or it will drift the moment a command is added (a test in the Python
codebase pins the two together).

Sections, in order: chat box basics, chat commands (generated), window
tabs, sub-agents, approval keys, permission mode, session keys, App
(function-key) section, then a one-paragraph loop summary. Bindings:
`escape`, `f1`, `q` all close it (`help.py:122`).

### 2.4 ConfirmScreen — `src/agentclip/shell/tui/screens/confirm.py`

A `ModalScreen[bool]`, the generic y/n dialog. Constructor takes
`(title, body="")`. Renders a title `Static`, an optional body `Static`,
and a fixed hint line `"y yes · n / escape no"`. **No buttons — keyboard
only.** Bindings: `y`/`enter` → `dismiss(True)`; `n`/`escape` →
`dismiss(False)` (`confirm.py:14-17, 31-35`). A GUI port that wants mouse
parity must add Yes/No buttons itself; nothing in the Python widget
offers one.

**Actual call sites in the current codebase** (see §6.1 for the drift
this implies against the doc's own screen-inventory comment):

| Use | Title | Body | Call site |
|---|---|---|---|
| Undo last turn | "Undo the most recent turn?" | explains restore + revert-notice copy | `shell/app/controller.py:2070-2075` (`_undo_flow`) |
| Quit mid-turn | "Quit mid-turn?" | explains the turn is incomplete, backups kept | `shell/tui/app.py:861-867` (`_confirm_quit`) |
| Discard invalid edit on close (Service Editor, F2) | "Discard the pending edit?" | names the validation error | `shell/tui/screens/service_editor.py:1288-1294` |
| Forget captured appearances (Service Editor, F2) | "Forget the `<key>` appearance?" | explains captured images are deleted | `shell/tui/screens/service_editor.py:1225-1232` |

### 2.5 TextEntryScreen — `src/agentclip/shell/tui/screens/text_entry.py`

A `ModalScreen[str | None]`, a multi-line `TextArea` fallback. Per its own
docstring: "Follow-up messages and ask_user answers now go through the
persistent ChatComposer on MainScreen; this modal remains only for the
empty-clipboard manual-paste path" (`text_entry.py:1-5`). Constructor
takes `(title, hint="ctrl+s (or ctrl+enter) submit · escape cancel")`.
Bindings: `ctrl+s` and `ctrl+enter` (both `priority=True` so the TextArea
can't eat them) submit; `escape` cancels (`text_entry.py:20-26`).
`action_submit` dismisses with the typed text if non-empty (stripped),
else `None` (`text_entry.py:44-46`).

**The one live call site**: `shell/app/controller.py:2111-2120`
(`_force_ingest_flow`) — when `i` (force-ingest) finds nothing on the
clipboard (empty, or the clipboard provider is dead), it prompts
`"Paste the model's reply"` / `"the clipboard had no text - paste the
reply here; ctrl+s ingests"`. This is also the covered path for the
provider-death case in §8 of `tui.md` — there is no separate modal for
that; an unreadable clipboard just means `read_clipboard()` returns
`None`, which routes into the same prompt.

### 2.6 SummaryScreen (end session, `e`) — `src/agentclip/shell/tui/screens/summary.py`

A `ModalScreen[str]`, pushed on demand via `e` — **not** auto-pushed on
`task_done` (the user may keep following up; `summary.py:1-5`). Renders a
Rich `Table` of stats (turns, tool-call counts, files touched, chars
copied both ways, sub-agent run count when applicable — built by
`shell/app/controller.py:2008-2023`, `_stats_rows`) plus the model's
`task_done` summary text as `Markdown`. Bindings: `u` (undo last turn) →
`dismiss("undo")`; `t` (new session) → `dismiss("new")`; `l` (export chat
log) → `dismiss("export")`; `escape` → `dismiss("close")`
(`summary.py:22-27, 44-54`). No buttons — keyboard only, same as
ConfirmScreen.

---

## 3. States

### 3.1 Modal open/close/result flows

- **SettingsScreen**: opened by `F4` (`AgentClipApp.action_preferences`,
  `app.py:827-833`), guarded against double-open (`isinstance(self.screen,
  SettingsScreen)` no-ops). Dispatched via a worker
  (`run_worker(self._open_settings(), group="preferences", exclusive=True)`)
  because `F4` is a plain synchronous `Binding` action and
  `push_screen_wait` needs a worker context. Result `str | None`: `None`
  means cancelled (theme already reverted by the screen itself, nothing
  to persist); a theme name means `Save` — the controller persists it
  (`save_theme`) and updates `app_config` (`app.py:835-843`).

- **HelpScreen**: opened by `F1`/`?` (`action_help`, `app.py:748-751`),
  same double-open guard. Always dismisses `None`; nothing to persist.

- **ConfirmScreen**: opened via `await self._view.confirm(title, body)`
  (`ChatView.confirm`, implemented at `main.py:2590-2591` as
  `push_screen_wait(ConfirmScreen(title, body))`) or, for the two
  ServiceEditorScreen-only uses, directly via `push_screen_wait`. Returns
  `bool`; the caller branches on it inline (there is no separate
  "cancelled" state — `False` covers both "n" and "escape").

- **TextEntryScreen**: opened via `await self._view.prompt_text(title,
  hint)` (`main.py:2593-2594`). Returns `str | None`; `None` means
  cancelled or submitted empty, and the caller (`_force_ingest_flow`)
  simply returns without ingesting anything.

- **SummaryScreen**: opened via `await self._view.show_summary(rows,
  summary)` (`main.py:2596-2597`), from `controller._show_summary()`
  (`controller.py:1996-2006`), itself spawned as a flow by `end_session()`
  (`controller.py:1072-1075`, gated: session active, not busy, not
  turn-aborting). The controller loops on the result:
  - `"export"` → writes the chat log, then **re-shows the same
    summary screen** (`continue` in the `while True`).
  - `"undo"` → runs `_undo_flow()` (§3.3) **once**, then returns —
    the summary is **not** automatically reopened. See §6.3 for why
    this matters for a port that wants to offer "undo the whole
    session" as one gesture.
  - `"new"` → runs `_reset_session()` (tears the session down, then
    re-arms `prompt_new_session()`, §3.2).
  - `"close"` → nothing further; the user is back in the main chat
    with the transcript untouched.

### 3.2 `ChatView.prompt_new_session` (not a modal — inline)

Not a screen at all, included here because it is the fourth blocking
prompt on the `ChatView` port and behaves like one from the controller's
side. `MainScreen.prompt_new_session()` (`main.py:2563-2588`) flips
`awaiting_new_session = True`, relabels the composer's border title to
*"Describe the task · Enter starts the session · Ctrl+J newline"*, unlocks
the sidebar's service picker, focuses the composer, and parks on an
`asyncio.Future[SessionSpec | None]`. The first non-empty send resolves
it; an empty send just warns. **Slash lines are dispatched as commands
here too** — see §3.3a of `tui.md` and §5.2 below. A GUI port's "new
session" surface should be this same inline state, not a launch dialog:
the design explicitly rejected a modal for it (`tui.md:190`).

### 3.3 Esc stage machine

`Esc` means something different depending on **where focus is and what
is open**, checked in this precedence order (first match wins — a GUI
must replicate the order, not just the individual behaviors):

1. **Slash-command popup open** (composer focused, popup visible) → Esc
   dismisses **only the popup**; the composer keeps its text and its
   focus (`composer.py:143-147`). This is checked first inside
   `ChatComposer._on_key`, ahead of the composer's own two stages.
2. **Composer focused, text present** → Esc **clears the box** (an
   undoable edit via `history.checkpoint()` + `clear()`, *not*
   `load_text`, so `ctrl+z` gives the exact text back) and **keeps
   focus** so the rewrite can start immediately (`composer.py:161-175`).
3. **Composer focused, box empty** → Esc **blurs** the composer
   (`self.screen.set_focus(None)`), returning focus to the screen so the
   single-key shortcuts (`u`/`c`/`i`/`w`/`e`/`x`/`t`/`l`/…) become
   reachable — "command mode" (`composer.py:176-179`).
4. **Reject-reason input open** (`n` was pressed at a gate) → Esc, now
   reaching `MainScreen` because no widget above it claimed the key,
   fires `action_cancel_entry`, which closes the reason `Input` and
   returns to the pending gate without rejecting
   (`main.py:2672-2675`; gated via `check_action("cancel_entry")` →
   `self.reject_open`, `main.py:1356-1357`).
5. **A modal screen is on top** (Settings/Help/Confirm/TextEntry/Summary/
   ServiceEditor) → each modal's own local `escape` binding governs:
   Settings reverts the live theme preview and dismisses `None`
   (`settings.py:91-93`); Help closes (`help.py:130-131`); Confirm denies
   — same as `n` (`confirm.py:34-35`); TextEntry cancels
   (`text_entry.py:48-49`); Summary dismisses `"close"`
   (`summary.py:53-54`); ServiceEditor closes, first asking a
   discard-edit Confirm if the current field values are invalid
   (`service_editor.py:1274-1301`).
6. **An `ask_user` question is open** (GUI only) → Esc **cancels the
   question**: `SessionController.cancel_pending_question()` resolves the
   answer future with `"[cancelled by user]"`, which the model receives as
   the `ask_user` result. Deliberately last before the no-op, so the empty
   composer spends a press on stage 3 first — the key that cancels is never
   the key that was meant to let go of the box. **The TUI has no such
   stage**: there the only way out of a question is to answer it (see the
   note below).
7. **Nothing focused, no modal, `reject_open` false** → Esc is a no-op
   (`check_action` returns `False` for `cancel_entry` when
   `reject_open` is false, so the key does not even dispatch).

**Why stage 6 resolves rather than aborts**: the engine's only exit from
`Phase.AWAITING_USER` is `Engine.answer_user` (phase-guarded), so raising
into the answer future — what `/new` does — would leave a live engine
parked on a question nobody can answer again. `/new`'s poison is safe
only because a full session reset always follows it. Cancelling has no
reset behind it, so it travels the ordinary answer path instead: the
transcript echoes it like any other answer, the phase advances, and
`_ask`'s `finally` puts the composer back.

**Two-stage composer note for a GUI**: stage 2 (clear-with-undo) vs stage
3 (blur) is decided purely by "is the box non-empty right now" — there is
no separate "are you sure" step and no timer. A GUI text box needs its
own undo buffer for the clear to be safe (the Python side leans on
`TextArea`'s native undo history plus an explicit checkpoint,
`composer.py:169-174`).

### 3.4 Double-tap `c` re-delivery

`c` (recopy) is **not** part of the Esc machine, but it is the other
two-stage keyed gesture and belongs in this section:

1. **First press** (`_recopy_armed_at` unset or expired) — writes the
   last outbound payload back to the clipboard only. No click, no
   paste, no mouse movement. Arms a 1.5 s window
   (`_RECOPY_DOUBLE_TAP_S = 1.5`, `controller.py:87`) and toasts
   *"re-copied the last outbound (N chars) — press c again to deliver
   it"*.
2. **Second press within 1.5 s** — escalates to
   `redeliver_outbound(text)`: the same click-then-paste delivery path
   `copy_outbound` uses for any composed payload (verified focus click,
   burst-or-stream, opt-in Enter tap). The arm is **consumed on every
   press** regardless of outcome, so a `c` after the window expires is
   simply a fresh first press, never a stale delivery
   (`controller.py:1100-1115`).

A **fresh** outbound (any new payload the controller composes) drops the
arm immediately (`_recopy_armed_at = None` on every `_copy_outbound`,
`controller.py:2134-2137`), so the second `c` can never accidentally
redeliver a *different* message than the one the first press was about.

### 3.5 Permission-mode cycle (`ask → plan → unattended → ask`)

Three states, cycled by `shift+tab` (`MainScreen.action_cycle_permission_
mode` → `SessionController.cycle_permission_mode()`,
`controller.py:703-705`) or set directly with `/mode [plan|ask|
unattended]`; bare `/mode` reports rather than cycles
(`controller.py:707-724`). The dial:

- Works with **no session** (arms the *next* session started) and
  **mid-turn**, at a gate, and while DISARMED — it has no
  `check_action` gate at all, unlike every letter binding on the screen
  (`main.py:782`, `check_action` falls through to `True` for this
  action — it is not special-cased in the `if` chain at
  `main.py:1302-1365`).
- **Survives `/new`** — alone among session dials, it is not reset by
  `_reset_session` (`controller.py:2047-2056` explicitly documents that
  YOLO resets but the mode does not).
- **Governs every engine in the app run, including sub-agents.** During a
  delegation, `shift+tab` reaches the sub-agent's engine (the
  conversation actually running); the master engine is re-armed on the
  way back out (`tui.md §2.6a`).
- Each transition adds a transcript note, fires an `alert()` (bell+toast,
  `information` severity for `ask`, `warning` otherwise), and repaints
  the leftmost status segment (`controller.py:726-760`, `_apply_mode`).

### 3.6 ARMED / DISARMED (`F5` / `/armed`)

A binary, view-owned flag (`MainScreen._os_armed`, default `True`) with
no relationship to session state or engine state. Toggled by `F5`
(`AgentClipApp.action_toggle_armed`, app-level, no `check_action` — works
in every state including before any session exists and mid-turn,
`app.py:753-761`) or `/armed [on|off]`. `None` target means toggle.

**What DISARMED stops** (four chokepoints,
`MainScreen.set_os_armed`, `main.py:2437-2524`): the paste path (clicking
+ synthetic Ctrl+V — clipboard **write** still happens, so manual paste
stays possible), the one find-then-click primitive (`_click_profile_
element`, refuses with `ElementClick.DISARMED`), the auto-copy launch
decision (lands on `MANUAL_COPY` instead of firing the click flow), and
the clipboard watcher (stopped, remembered so re-arming restores exactly
what the user had). **What keeps running**: every read-only thing —
screen capture, all three finish detectors, the send gate, `/identify`,
`i` force-ingest. In-flight bookkeeping (`_send_gate`, `_copy_armed`,
streaks) is left untouched by a disarm — it is fed by live detection and
clearing it would only make the sidebar lie. An `_auto_copy_flow` already
running is allowed to finish. Indicators: `⛔ DISARMED` status-bar
segment (own slot, hides while armed) and a standing (non-blinking)
`#side-armed-banner` in the sidebar — both painted synchronously, no
status push required (`main.py:2507-2524`).

---

## 4. Inputs from core

Four blocking calls on the `ChatView` port
(`src/agentclip/shell/app/view.py:253-259`) that the controller `await`s
directly — a GUI implementation must provide async equivalents with
these exact return semantics, since the controller's flow logic branches
on them:

| Method | Signature | Blocks until | Return value means |
|---|---|---|---|
| `prompt_new_session` | `() -> SessionSpec \| None` | user submits a non-empty task (or the view decides to give up) | `SessionSpec(task, service, subagent_service)` starts a session; `None` → `controller._session_flow` calls `exit_app()` (the Python TUI's `MainScreen` implementation never actually resolves `None` — it only ever returns a `SessionSpec` — so the "user wants out" path is currently reachable only via `ctrl+q`, not through this port method) |
| `confirm` | `(title, body="") -> bool` | user answers y/n | `True` = proceed (e.g. undo really happens); `False` = abort, no side effect |
| `prompt_text` | `(title, hint) -> str \| None` | user submits or cancels | non-`None` non-empty string is used as the pasted reply; `None` (cancel, or submit-while-empty) means the caller (`_force_ingest_flow`) does nothing further |
| `show_summary` | `(rows, summary) -> str` | user picks one of the summary screen's actions | one of `"undo" \| "new" \| "close" \| "export"`; **`"export"` is special** — the controller loop treats it as "do the export, then call this method again" rather than as a terminal result (`controller.py:1996-2006`) |

None of the four take a timeout or can be cancelled from outside except
by the flow-abort machinery described in `tui.md §3.3a` (a mid-turn
`/new` poisons whichever future the flow is parked on with
`_TurnAborted`/`_SubagentAborted`) — but note that **none of these four
prompts are reachable mid-turn in the current wiring**: `confirm` is only
awaited from `_undo_flow`, and `undo`/`end_session`/`force_ingest` are
all refused by the controller itself while `_busy` (§5's table). A GUI
port does not need to handle an abort landing inside one of these four
awaits today, but should not assume that can never change.

---

## 5. User actions out

### 5.1 Complete keybinding table

Screen = `MainScreen` unless noted. "Dynamic" means gated through
`check_action` + a `reactive(..., bindings=True)` field; the controller
method is what the action ultimately calls (through
`MainScreen.action_*` → `SessionController.*`, or directly on the
modal). Source: `docs/design/tui.md:994-1038`, cross-checked line by
line against `main.py:726-786` (BINDINGS), `main.py:1302-1365`
(`check_action`), `main.py:2646-4859` (action_ methods), and `app.py:78-91`.

| Key | Context (check_action) | Effect | Controller / screen method |
|---|---|---|---|
| `y` | `pending_approval` | approve current gated call | `action_approve` → `controller.submit_decision(APPROVE, None)` |
| `n` | `pending_approval` | open reject-reason input | `action_reject` → opens `#reject-reason` |
| `enter` (in reject input) | native `Input.Submitted` | confirm rejection (reason optional); remaining calls in turn skipped | `on_input_submitted` → `submit_decision(REJECT, reason)` |
| `escape` (reject input open) | `reject_open` | cancel reject, return to pending gate | `action_cancel_entry` |
| `a` | `pending_approval` AND (gate is `edit` OR gate carries `always_pattern`) | approve + arm "auto-accept edits" (legacy) or "always allow this pattern" (ruleset mode) | `action_auto_edits` → `APPROVE_ALL_EDITS` or `APPROVE_ALWAYS` |
| `u` | session_active, not busy, phase ∈ {AWAITING_REPLY, DONE} | undo last turn (ConfirmScreen, then restore) | `action_undo` → `controller.undo()` → `_undo_flow` |
| `c` | `has_outbound` | re-copy outbound; **2nd press ≤1.5s** re-delivers (click+paste) | `action_recopy` → `controller.recopy()` (§3.4) |
| `i` | session_active, not busy, phase == AWAITING_REPLY | force-ingest clipboard now | `action_force_ingest` → `controller.force_ingest()` |
| `w` | session_active AND provider != "manual" AND armed | pause/resume clipboard watcher | `action_toggle_watch` |
| `r` | hidden (`False`, not dimmed) unless `has_extra_instructions`; else `session_active` | arm/disarm re-sending this service's extra instructions on the next payload | `action_reinstruct` → `controller.reinstruct()` |
| `t` | session_active | focus the (docked) composer | `action_follow_up` |
| `e` | session_active, not busy, phase ∈ {AWAITING_REPLY, DONE} | open SummaryScreen | `action_end_session` → `controller.end_session()` |
| `l` | session_active | export the whole chat log to a file | `action_export_log` → `controller.export_log()` |
| `x` | always shown=False | toggle most-recently-mounted transcript `Collapsible` | `action_toggle_last` |
| `enter` (native, Collapsible focused) | native | toggle that Collapsible | Textual native |
| `pageup`/`pagedown`, arrows/`pgup`/`pgdn` | native | scroll transcript / focused panel | native |
| `escape` | `reject_open` (see §3.3, stage 4) | see Esc stage machine | `action_cancel_entry` |
| `F1` / `?` | global (App) | open HelpScreen | `AgentClipApp.action_help` |
| `F2` | global (App) | open ServiceEditorScreen (refused with a toast if the region-picker overlay is up) | `AgentClipApp.action_settings` |
| `F3` | MainScreen, priority | show/hide sidebar | `action_toggle_sidebar` |
| `F4` | global (App) | open SettingsScreen | `AgentClipApp.action_preferences` |
| `F5` | global (App), **every** state, no `check_action` | ARM/DISARM (§3.6) | `AgentClipApp.action_toggle_armed` → `main.set_os_armed(None)` |
| `F6` | MainScreen, priority, show=False | select next window tab (view only — never moves what the automation drives) | `action_next_chat_tab` |
| `F7` | MainScreen, priority, show=False | show/hide ELEMENTS column | `action_toggle_elements` |
| `F8` | MainScreen, priority, show=False | show/hide harness decision log pane (same call as `/log`) | `action_toggle_harness_log` |
| `ctrl+x` | MainScreen, priority, dynamic (`executing`) | cancel tool calls running now (turn still reports back) | `action_cancel_execution` → `controller.cancel_execution()` |
| `ctrl+o` | MainScreen, priority, show=False, hidden outright unless `executing` | show/hide the running command's live output | `action_toggle_run_output` |
| `ctrl+p` | global (App) | **nothing — the palette is disabled** (`ENABLE_COMMAND_PALETTE = False`); the composer's slash commands are the one command surface, in both shells | Textual native, switched off |
| `ctrl+q` | global, Textual default | quit; ConfirmScreen if mid-turn | `AgentClipApp.action_quit` |
| `shift+tab` | MainScreen, priority, show=False, **no `check_action`**, works pre-session and mid-turn | cycle permission mode `ask → plan → unattended → ask` (overrides Textual's own `Screen` binding for this key) | `action_cycle_permission_mode` → `controller.cycle_permission_mode()` |
| `ctrl+s` / `ctrl+enter` | MainScreen, priority, show=False | send composer without focusing it | `action_submit_composer` |
| `enter` (composer, popup open, row highlighted) | composer-local | complete to `/name ` (trailing space closes popup) | `ChatComposer._on_key` |
| `enter` (composer, popup open, no highlight) | composer-local | swallowed — does nothing | `ChatComposer._on_key` |
| `enter` (composer, popup closed) | composer-local | send composer (task / answer / follow-up, per mode) | `ChatComposer.Submitted` → `_submit_text` |
| `tab` (composer, popup open, highlight) | composer-local | complete, same as Enter; does not move focus | `ChatComposer._on_key` |
| `↑`/`↓` (composer, popup open) | composer-local | move popup highlight, wrapping | `CommandPopup.move` |
| `ctrl+j` (composer) | composer-local | insert literal newline | `ChatComposer.insert("\n")` |
| `escape` (composer, popup open) | composer-local, checked first | dismiss popup only | `ChatComposer._on_key` |
| `escape` (composer, text present) | composer-local | clear box (undoable), keep focus | `ChatComposer._on_key` |
| `escape` (composer, empty) | composer-local | blur to screen ("command mode") | `ChatComposer._on_key` |
| SummaryScreen `u` | modal-local | dismiss `"undo"` (undoes **one** turn; does not reopen the summary) | `summary.action_undo` |
| SummaryScreen `t` | modal-local | dismiss `"new"` | `summary.action_new` |
| SummaryScreen `l` | modal-local | dismiss `"export"` (summary reopens after export) | `summary.action_export` |
| SummaryScreen `escape` | modal-local | dismiss `"close"` | `summary.action_close` |
| ConfirmScreen `y`/`enter` | modal-local | dismiss `True` | `confirm.action_confirm` |
| ConfirmScreen `n`/`escape` | modal-local | dismiss `False` | `confirm.action_deny` |
| HelpScreen `escape`/`f1`/`q` | modal-local | dismiss | `help.action_close` |
| SettingsScreen `escape` | modal-local | revert live preview, dismiss `None` | `settings.action_cancel` |
| TextEntryScreen `ctrl+s`/`ctrl+enter` (priority) | modal-local | submit typed text (or `None` if empty) | `text_entry.action_submit` |
| TextEntryScreen `escape` | modal-local | dismiss `None` | `text_entry.action_cancel` |

No `priority=True` letter bindings anywhere on MainScreen — focus-based
suppression (a focused `TextArea` swallows plain letters) is the whole
safety mechanism for text inputs; only chords and function keys are
`priority=True`, because those already reach the app even with a
`TextArea` focused (`tui.md:1038`).

### 5.2 Slash commands

One registry, `agentclip/shell/app/commands.py:67-108`, in **registry order**
(also popup order — order is a deliberate safety property: harmless/
reversible commands first, `/yolo` last, so no stray keystroke near the
top of the list can disable every approval gate):

| Command | Arg | Summary | Session-gated? |
|---|---|---|---|
| `/help` (aliases `/commands`, `/?`) | — | list the commands | no |
| `/new` | — | fresh browser chat + fresh session; aborts a turn in flight rather than refusing | no (with no session it opens the chat and stops there) |
| `/abort` | — | end the sub-agent run in flight | no-op warning if nothing delegated |
| `/identify` | — | draw labelled boxes on the real screen over what the tool currently sees | no |
| `/log` | — | show the harness decision log | no |
| `/mcp` | — | list configured MCP servers | no |
| `/armed` | `[on\|off]` | ARMED/DISARMED, same as F5 | no |
| `/mode` | `[plan\|ask\|unattended]` | set permission mode; bare `/mode` reports | no |
| `/theme` | `[name]` | set the appearance; bare `/theme` lists the themes and marks the current one. Same setting F4 picks, and the names are the **shell's** (four Textual themes in the TUI, two CSS palettes in the GUI) — the controller reads them back over `ChatView.theme_choices` rather than knowing any | no |
| `/yolo` | `[on\|off]` | toggle auto-approve-everything | no (armed at the start prompt too; the policy itself stays session-scoped) |

Precedence rule (repeated for emphasis — this is the parity contract's
sharpest edge): **an open `ask_user` answer always wins**. Slash parsing
is only reached when `SessionController._awaiting_answer` is false
(`controller.py:536-554`). The one *other* place a leading slash is text
rather than a command is the "describe the task" prompt when it is
started with `//` — one slash stripped, the rest sent as the literal
task (`main.py:2731-2732`).

### 5.3 Mouse equivalents

| Keyboard action | Mouse equivalent | Exists? |
|---|---|---|
| `y` / `n` / `a` at a gate | Approve / Reject / "Approve + auto-edits" (or "Always: `<pattern>`") buttons in the ActionPanel footer | yes (`action_panel.py:110-119`) |
| reject-reason submit/cancel | type in `#reject-reason` Input, click elsewhere is not a cancel (only `escape` is) | partial |
| `F6` (next window tab) | click a `WindowTab` widget | yes (`tui.md §1.6`) |
| `F3` / `F7` / `F8` toggles | none — keyboard-only | no |
| `ctrl+o` (run output) | click the run panel | yes (`tui.md §8a`) |
| `x` (expand last collapsible) | click the specific `Collapsible` header | yes (native Textual, but targets *that* one, not "the last") |
| "New browser chat" (sidebar) | `#newchat-btn` click | yes — see `tui.md §1.3`'s five-case table; **this is not the same as `/new`'s keyboard path** in one respect: the button drives the *selected* tab (`_calibrating`), `/new` always targets the master |
| Settings Save/Cancel, theme pick | buttons / `RadioSet` clicks | yes (only interaction mode for SettingsScreen besides `escape`) |
| ConfirmScreen y/n | **none** | no — keyboard only |
| SummaryScreen u/t/l/escape | **none** | no — keyboard only |
| TextEntryScreen submit/cancel | click into the TextArea to position the cursor only; no submit/cancel buttons | partial |
| `shift+tab` (permission mode) | **none** | no |
| `F5` (ARMED) | **none** | no |

A GUI port that wants full mouse parity therefore has to *add* buttons
to Confirm/Summary/TextEntry and a click target for the permission mode
and ARMED switch — none of that exists as a click target in the TUI
today, only as a key or a status-bar segment that is read-only.

---

## 6. Invariants & edge cases

### 6.1 `ask_user` answer must beat slash parsing (hard invariant)

See §5.2. Enforced in exactly one place —
`SessionController.submit_message` (`controller.py:536-554`) checks
`self._awaiting_answer` **before** checking for a leading `/`. A second
frontend must replicate this check at the same layer (the controller, not
the view) since `submit_message` is the one door every frontend's
composer-send routes through.

Because the box has no room left for a command, the way *out* of a
question is a key, not a line of text: `cancel_pending_question()`, bound
to Esc's last stage in the GUI (§3.3 stage 6). It is a second door on the
controller for the same reason the sidebar's "New browser chat" button is
one — it does not compete with the answer for the same keystrokes.

**How the question is SHOWN is where the two shells diverge** (recorded
in `gui.md`): the TUI leaves it at the `"? …"` transcript note plus the
composer's `■ ANSWER NEEDED` mode; the GUI additionally pins an
`#ask-banner` panel above the composer, carrying the question text, built
from the state push's `question` field (`GuiView` scrapes the `"? "` note
rather than growing a `ChatView` method only one shell would implement).

### 6.2 Esc precedence order (hard invariant, restated as a checklist)

1. Popup dismiss (composer-local, text/focus untouched)
2. Clear composer text (undoable, keeps focus)
3. Blur composer (empty box only)
4. Cancel reject-reason input (screen-level, gated on `reject_open`)
5. Modal-local escape (each modal owns its own meaning)
6. Cancel a pending `ask_user` (**GUI only**, gated on `awaiting_answer`;
   resolves the answer, never poisons it — see §3.3)
7. No-op

A frontend that collapses steps 2 and 3 into one ("Esc always blurs") or
skips step 1 will make `/` `Esc` `Enter` send a bare slash to the model
instead of quietly closing the popup — a real behavioral regression, not
a cosmetic one.

### 6.3 `shift+tab` must work pre-session and mid-turn

Verified at `main.py:782` (no `check_action` entry for
`cycle_permission_mode` in the `if`/`elif` chain at
`main.py:1302-1365`, so it falls through to the default `return True`).
This is the **only** letter-adjacent action on the whole screen with that
property; every other dynamic binding is gated on `session_active`/`busy`/
phase. A GUI port must not disable its permission-mode control while
"nothing is happening" or "a turn is running" — those are precisely the
two moments the feature exists for.

### 6.4 Quit-mid-turn confirmation

`AgentClipApp.action_quit` (`app.py:845-858`) computes `mid_turn` as
`session_active AND NOT awaiting_new_session AND (busy OR pending_
approval OR awaiting_answer)`. The `NOT awaiting_new_session` carve-out
matters: the inline "describe the task" prompt technically leaves the
session worker "busy" (parked on a future), but there is no turn to lose,
so quitting from the empty start screen must not show the mid-turn
warning. `ctrl+q` a second time while the ConfirmScreen is already up is
a no-op (`isinstance(self.screen, ConfirmScreen)` guard).

### 6.5 Bell vs toast independence

Both are individually switchable in config (`NotifyConfig.bell`,
`NotifyConfig.toast`, both default `True`, `config.py:447-449`). The
single fan-out point is `ChatView.alert()`
(`main.py:1947-1952`): `if bell: self.app.bell()`, `if toast: self.notify
(...)` — two independent `if`s, not one combined switch. `notify()` alone
(no bell) is inherited straight from Textual's `Screen` and used for
routine, non-attention-grabbing toasts (e.g. "service presets saved").
`alert()` is reserved for things that should pull the user back from the
browser: approval needed, ask_user question, parse error, chunk ACK/NACK,
`task_done`, clipboard provider fault (`tui.md §8`).

### 6.6 Footer key-hint dimming logic

`check_action(action, params) -> bool | None` returns exactly three
things and a GUI port's equivalent "is this control available" function
should preserve the three-way distinction, not collapse it to a boolean:

- `True` → binding fires, shown normal weight in the footer.
- `False` → binding **does not fire at all**, hidden outright from the
  footer (not merely dimmed) — used when the key can *never* do
  anything in the current mode, e.g. `w` while disarmed or in manual
  clipboard mode (`main.py:1339-1340`), or `r` on a service with no
  extra instructions to resend (`main.py:1321-1322`).
- `None` → binding does not fire, but **is shown dimmed** — used when
  the key *will* become available shortly, e.g. `u`/`e`/`i` mid-turn
  (`main.py:1308-1314, 1324-1326`).

(`docs/design/tui.md:996` states this plainly: "`None` returns show
dimmed keys in `Footer` for discoverability.")

### 6.7 Known doc/code drift (found while cross-checking)

1. **`ConfirmScreen`'s screen-inventory comment overclaims.**
   `docs/design/tui.md:17` lists its uses as *"undo, end-session,
   quit-mid-turn, discard-edit"*. In the current code, ending a session
   (`e`) goes **straight to SummaryScreen** with no confirmation
   (`main.py:4819-4820` → `controller.py:1072-1075` →
   `_show_summary`, no `confirm()` call anywhere on that path) — pressing
   `e` never asks "are you sure". Conversely, the doc's own §3.3a text
   about `/new` mid-turn ("aborted... not a reason to refuse") is
   correct as written but easy to conflate with the inventory line: `/new`
   mid-turn also has **no** confirm dialog — it silently aborts the turn
   and toasts. The actual `ConfirmScreen` call sites are: undo
   (`controller.py:2070`), quit-mid-turn (`app.py:861`), and two
   ServiceEditorScreen-only uses the doc's inventory line doesn't
   mention at all — discard-invalid-edit-on-close
   (`service_editor.py:1288`) and forget-captured-appearances
   (`service_editor.py:1225`).

2. **§9's `ask_user` description is stale.** `docs/design/tui.md:988`
   describes "ActionPanel switches to question mode... `TextArea#answer`
   revealed and focused... `ctrl+enter` submits." That widget no longer
   exists. `ActionPanel`'s own docstring says so directly: *"`ask_user`
   answering lives on the persistent chat composer now, not here — this
   widget is approval-only"* (`action_panel.py:15-16`); confirmed by
   grep — there is no `#answer` id or "question" text anywhere in
   `action_panel.py`. The real flow: the question is posted to the
   transcript as a note (`? {question}`,
   `controller.py:1550-1551`), the **docked `ChatComposer`** switches to
   answer mode (`verbatim=True`, border title *"Answer the model · Enter
   sends · Ctrl+J newline"*, `main.py:4885-4887, 4919`), and plain
   `Enter` submits — not `ctrl+enter`. A GUI port should follow the
   composer-based flow, not the doc's `ActionPanel` description.

3. **SummaryScreen's "u" does not loop.** `docs/design/tui.md:299`
   ("`u` undo entire session (turn-by-turn restore, with ConfirmScreen)")
   and `tui.md §7` ("Whole-session undo lives on SummaryScreen (`u`
   there loops it)") both read as if pressing `u` inside the summary
   repeatedly walks back every turn without leaving the modal. It
   doesn't: `controller._show_summary`'s `while True` loop only re-shows
   the summary after `"export"`; `"undo"` breaks the loop, runs
   `_undo_flow()` **once** (one `ConfirmScreen`, one turn restored), and
   returns to the main chat (`controller.py:1996-2006`). Walking back
   multiple turns means either reopening the summary each time (`e` →
   `u` → `e` → `u` → …) or, more directly, pressing the main screen's own
   `u` binding repeatedly (`main.py:730`, `action_undo` →
   `controller.undo()` → the same `_undo_flow`) without going through
   the summary at all. A GUI port should not build an auto-repeating
   "undo everything" loop inside its summary screen to match the doc's
   wording — it would not match what the Python TUI actually does.

---

## 7. Textual-specific details NOT to carry over

- **`priority=True` bindings.** `F3`, `F6`, `F7`, `F8`, `shift+tab`,
  `ctrl+x`, `ctrl+o`, `ctrl+s`/`ctrl+enter` are all marked `priority=True`
  purely to jump ahead of a focused `TextArea` (the composer), which
  would otherwise consume the keystroke. A GUI text box's event model is
  different (e.g. explicit `keydown` capture vs. Textual's bubble-then-
  priority-intercept order); there is no "priority" concept to port,
  only the *outcome* — these keys must work even while the equivalent
  text input has focus.

- **`shift+tab` overriding `Screen.focus_previous`.** In Textual,
  `Screen` binds `shift+tab` to backwards focus navigation by default;
  `MainScreen` deliberately overrides it (`main.py:774-782`) and accepts
  losing backwards-tab navigation as the price (plain `tab` still cycles
  forward). A GUI has no such built-in binding to fight, so this is pure
  Textual plumbing — but the resulting *rule* ("shift+tab always means
  cycle permission mode, never focus-navigate") is still contract.

- **`check_action` / `reactive(..., bindings=True)`.** The
  enabled/dimmed/hidden three-state footer behavior (§6.6) is a Textual
  `Footer` widget feature keyed off `check_action`'s return value. A GUI
  toolbar/menu needs its own enabled/dimmed/hidden logic, but should
  preserve the **three states**, not collapse dimmed and hidden into one
  "disabled" look — the doc leans on dimmed to mean "coming soon" and
  hidden to mean "never, in this mode." *(Done, in the GUI shell's key
  hint strip above its status bar: `KEYS[].foot` is the `show=` flag and
  `KEYS[].avail` returns `"on"`/`"dim"`/`"off"` — `check_action`'s three
  answers by another name, computed from the `state`/`status`/`run`
  pushes the page already gets. Where the page cannot see a gate at all
  — `r`'s "this service has no extra instructions" — the key is shown
  normal and the refusal stays a toast; `docs/design/gui.md §3` records
  that as the remaining divergence. A GUI has one state a Textual footer
  does not: a focused text box swallows the bare letters, so those rows
  dim while the caret is in a box.)*

- **Theme system.** `SettingsScreen` previews live via
  `self.app.theme = ...`, a Textual `App`-level reactive that
  triggers a full CSS re-render. The two custom themes (`claude-warm`,
  `claude-dark`) are `textual.theme.Theme` objects with named color
  roles (`primary`, `secondary`, `accent`, `warning`, `error`, `success`,
  `foreground`, `background`, `surface`, `panel`, plus a
  `button-color-foreground` variable override — `app.py:38-72`). A GUI
  has its own theming system (CSS variables / a design-token file); only
  the *palette values* and the *four named themes* are worth porting,
  not the Textual `Theme` registration mechanics.

- **`ModalScreen` stacking / focus isolation.** Textual's screen stack
  guarantees a pushed modal owns all key input and that dismissing it
  restores focus to whatever had it before (`main.py:4930-4932`'s
  `_focus_composer` explicitly checks `self.app.screen is not self` to
  avoid stealing focus from an open modal). A GUI needs its own modal/
  focus-trap implementation; the behavior to preserve is "a modal owns
  all keyboard input while open, and closing it returns focus to
  wherever it was."

- **`push_screen_wait` requiring a worker.** Every modal open triggered
  directly by a `Binding` action (F2, F4, `ctrl+q`, ServiceEditor's
  escape-with-discard) is wrapped in `self.run_worker(...)` because
  Textual dispatches plain `def action_*` methods outside of an async
  worker context, and `push_screen_wait` needs one. This is pure Textual
  plumbing with no GUI equivalent — an async/await-native frontend can
  simply `await` the modal's result inline.

- **BMP-only status glyphs.** The queue-strip and run-panel glyphs
  (`✓ ✗ ▶ • −`) are constrained to Unicode BMP per a Windows-terminal
  compatibility brief (`tui.md:436`). A GUI frontend has no such
  constraint and may use any icon set; only the *four states* they encode
  (done / failed-or-rejected / current / queued-or-skipped) are contract.
