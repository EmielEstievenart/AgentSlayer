# UI brief: the Monitor UI window

> **Status: BUILT, and binding.** Written after the fact, on 2026-08-25, from
> the window `docs/design/ui-monitor.md` §9.1 built. Unlike the older briefs in
> this folder it never described a Textual surface, so there is no
> "Textual-specific details not to carry over" section and no parity contract:
> this specifies the one implementation there is. It **specifies the surface**
> and re-decides nothing — where a rule here has a reason, the reason is
> ui-monitor.md's and is cited.

Audience: whoever changes this window next, and whoever has to know what the
Monitor UI promises before changing something underneath it.

Where it lives: `src/agentclip/shell/monitor_ui/` — `__main__.py` (the
`agentclip-monitor` dispatcher), `window.py` (the pywebview assembly,
`run_monitor_ui` and `open_calibration_window`), `view.py` (`CalibrationView`,
every decision), `serve.py` (`ServePanel`), `assets/` (the page). It is built
over a **`LocalUIMonitor`** and nothing else: never a `RemoteUIMonitor`, never
an `AutomationController`. What it shares with the Chat UI lives one package
down in `src/agentclip/shell/webview/` (the bridge, the service editor's model,
the asset resolution); it may not import `shell/chat` or `shell/app` at all, and
of the Driver's automation layer it may import `finish.py` alone
(`tests/test_layering.py`).

Sibling briefs, unchanged and still binding for the surfaces they describe:
`service-editor.md` (the editor that fills most of this window) and
`elements-panel.md` (the ELEMENTS column and `/identify`). This brief covers the
**window** — what those two are hosted in, and the Serve band that is only ever
here.

---

## 1. Purpose

The Monitor is a standing process on the machine whose screen shows the Browser.
Until §9.1 it had no user interface at all: you configured it by sitting at that
machine and running a *different* binary, the port it served was unauthenticated,
and the chat region somebody drew over there died with the process
(ui-monitor.md §9's opening note — "the monitor is a real, standing, user-facing
process and it has no user interface").

This window is that interface, and it answers three questions in one place:

1. **What is it watching?** — the service preset, the drawn chat region, and the
   live ELEMENTS column showing what the detector is actually matching right now.
2. **What should it watch?** — the whole service editor: sizes, finish signals,
   captured appearances, click points, the region picker, `/identify`.
3. **Who may drive it?** — the **Serve** band: which address, which port, the
   token, and whether anything is attached.

Question 3 is the new one, and it is the reason this is a window rather than a
flag. "Which interface, and is that safe" is a decision with a warning attached;
"here is a secret, copy it into the other machine" is a two-field transaction. A
command line can express both and can teach neither.

---

## 2. Anatomy

One pywebview window: title `AgentClip · calibration`, 1180×820, minimum
420×320, dark ground `#14161a`, `text_select=True`. Top to bottom:

### 2.1 Header

`CALIBRATION` · the two slot tabs · the service label · the region line ·
(spacer) · three buttons.

- **Slot tabs** — exactly two, `MASTER` and `SUB-AGENT`, rendered as buttons
  rather than a generated list because there are exactly two slots and there
  always will be. The current one carries `on`.
- **Service label** — `<display name> (<key>)`.
- **Region line** — `chat region: <left top width height>`, or
  `chat region: not set`.
- **`Set chat region...`**, **`Identify`**, **`Hide elements`** /
  **`Show elements`** (the last toggles its own label).

The whole row is painted from one `calib` event carrying `slot`, `slots`,
`region`, `service`, `service_label`.

**The region line seeds itself from the store.** On start, on a slot switch and
after a config adoption, an unset region is filled from the monitor's remembered
one (§4.2) — and that fill is deliberately **not** echoed back through
`on_calibration`, because the header would otherwise read "not set" over a
monitor already polling that exact rectangle.

### 2.2 The SERVE band

A two-row band under the header, **present only in the standalone window**
(§6.1). Row 1:

| Control | What it is |
|---|---|
| `SERVE` | the band's title |
| address `<select>` | one row per address on this machine, loopback first: `<adapter name> — <address>` |
| port `<input>` | numeric, placeholder `port`, default `7777` |
| **Start** / **Stop** | one button, label toggled |
| status | one sentence (§3.2) |

Then a **warning** line and an **error** line, each hidden when empty.

Row 2 — the token row:

| Control | What it is |
|---|---|
| `TOKEN` | the row's title |
| token | the 32-character token, shown in full |
| **Copy** | puts it on this machine's clipboard |
| **Regenerate** | mints a new one |
| `no token (loopback only)` | a checkbox, disabled off loopback |
| token path | where the token file is, so the user can find it without this window |

### 2.3 The service editor

Three columns and a footer, filling the rest of the window. It is **not a
modal**: the editor *is* this window, and closing it closes the window. Its
specification is `service-editor.md`; what this brief adds is only that it is
hosted here, that its model is `shell/webview/service_editor.py`, and that its
whole state arrives as one `editor` event.

### 2.4 The ELEMENTS column

An aside on the right: a title (`ELEMENTS · MASTER` / `· SUB-AGENT`) over one
row per `TemplateKind` in the detector's own report order, each row a label, a
verdict (`no match yet` / `not on screen` / `found · 1.2%`) and a crop of the
pixels last matched. Specification: `elements-panel.md`. Toggled by the header's
**Hide elements** button; crops are only attached to the event while the column
is open.

### 2.5 The picker and the overlay

Neither is part of the page: both are fullscreen surfaces drawn by a child
process over the real desktop. **Set chat region...** drags a box;
**Identify** labels every element the detector can find inside it. Both are in
`elements-panel.md` §2.2 and §5. One at a time, always (§6.4).

### 2.6 Modals and toasts

One scrim + one Yes/No dialog keyed by a `prompt_id` (a stale answer resolves
nothing), and a toast stack. Toast lifetime is the event's `timeout`, or 10 s
for `error` and 5 s otherwise.

---

## 3. States

### 3.1 The window's own states

There are only three, and none of them is modal: **not serving**, **serving with
nobody attached**, **serving with a brain attached**. Everything else on screen
(editor, ELEMENTS, picker) is orthogonal to them — a monitor serves whether or
not somebody is editing it, which is the entire point of a standing process.

### 3.2 The status sentences

Exactly three, verbatim (`shell/monitor_ui/serve.py`):

- `not serving`
- `listening on {address} — no Chat UI attached`
- `listening on {address} — attached: {peer}`

They are **polled**, not pushed: a 1 s task republishes the panel while a server
is up, and it ends by observing that the server is gone rather than by being
cancelled — so there is no task handle to lose. `{address}` is `host:port`;
`{peer}` is the attached brain's address.

### 3.3 While serving

The address dropdown, the port field and the no-token checkbox are **disabled**.
Changing where a live listener is bound is not an edit, it is a stop and a start,
and the button that does that is right there. The port field is never rewritten
from an event while it has focus.

### 3.4 Off loopback

The moment the *pending* dropdown selection is not a loopback address — before
Start is pressed, before anything is committed — the warning appears:

> off loopback this port is reachable by anything on that network, and it is a
> channel to this machine's mouse, keyboard and clipboard - use a host-only
> network or an SSH -L forward, and leave the token on.

and the no-token checkbox is disabled *and* forced unchecked. Choosing a
non-loopback row **is** the `--bind` opt-in ui-monitor.md §5 asks for, spelled as
a click.

The warning tracks the **pending selection**; the panel's `loopback` field
describes the **committed** address. Those are deliberately two different
answers: the user needs to be told what a choice would mean before making it.

### 3.5 Refusals

Two, and they are refusals rather than silent corrections:

- **No token, off loopback** — the panel does not serve and says so:
  `refusing to serve {address} without a token: off loopback that is a port onto
  this machine's mouse, keyboard and clipboard for anything on the network - the
  no-token box is loopback only.` It is not quietly upgraded to a token; the user
  asked for two things that compose into "anyone on this network may drive this
  desktop", and is told no.
- **`BindRefused` / `OSError` from the server** — the address is in use, or the
  server's own bind rules refuse it. The sentence lands in the panel's **error**
  line, not in a toast: an error about a form belongs on the form.

---

## 4. Inputs from core

### 4.1 The `serve` event

One event carries the whole panel:

| Field | Meaning |
|---|---|
| `serving` | is a server up |
| `status` | the sentence from §3.2 |
| `address`, `port` | the committed pair |
| `interfaces` | `[{value, label, loopback}]`, loopback first |
| `loopback` | is the **committed** address loopback |
| `warning` | the §3.4 sentence, sent unconditionally |
| `error` | the §3.5 sentence, or empty |
| `no_token` | the committed checkbox |
| `token` | the token, in full |
| `token_path` | where the token file is |

The other events in this window's vocabulary are `calib`, `editor`, `elements`,
`toast`, `modal` and `modal_close`.

### 4.2 The two files the Monitor keeps

Both in the monitor's own config dir — `<platform config dir>/agentclip/monitor/`
by default, `--config-dir` for both at once:

- **`monitor-token`** — one line, 32 hex characters (`secrets.token_hex(16)`).
  Minted on first use, written atomically at mode `0600`, re-minted silently if
  it is missing, empty or unreadable. **Merely opening this window creates it**:
  the panel loads-or-creates the token in its constructor, because the page shows
  it whether or not anything is being served.
- **`regions.json`** — `{"version": 1, "regions": {<service key>: {left, top,
  width, height}}}`. The rule is ui-monitor.md §8's, one line: **a `configure`
  spec that names a region wins and is written through; a spec that omits one is
  served from the store.** So a restarted monitor keeps the box somebody drew on
  that machine, a standalone Monitor UI has somewhere to put one at all, and a
  Chat UI that knows better still overrides.

### 4.3 The address list

`psutil.net_if_addrs()`, imported lazily, one row per address (an adapter with
both families is two rows), IPv6 zone indices stripped, link-local IPv6 dropped,
sorted loopback-first then IPv4-first then by name. Listed **once** and cached —
a dropdown that reshuffles under the cursor is worse than one that is a minute
stale.

If psutil is missing or answers nothing, the panel falls back to exactly two
rows, `loopback — 127.0.0.1` and `all interfaces — 0.0.0.0`, so a freeze that
lost the dependency is a reduced dropdown rather than a broken window.

---

## 5. User actions out

Every control on this page is a **schedule, not a call**: the click lands on a
pywebview thread, drops a coroutine on the runner's loop and returns.

### 5.1 The Serve band

| Control | Verb | Effect |
|---|---|---|
| address `<select>` | *(none)* | page-local; only repaints the warning and the checkbox |
| Start / Stop | `serve_start(address, port, no_token)` / `serve_stop()` | commits the three values, then serves or stops |
| Copy | `token_copy()` | **the one verb that returns a value** |
| Regenerate | `token_regenerate()` | mints, persists, swaps it into a live server |
| no-token checkbox | *(none)* | rides as `serve_start`'s third argument |

**`token_copy` is the exception to the schedule rule**, and deliberately: the
page needs the string in hand to give `navigator.clipboard`, so it is answered
synchronously on pywebview's own per-call thread off a plain attribute, never
marshalled onto the loop. The page falls back to a throwaway `<textarea>` plus
`document.execCommand("copy")`, because a `file://` page is an opaque origin and
the modern clipboard API is not always available to it.

**Regenerate does not drop an attached brain.** The token gates `hello`, and a
connection that already shook hands was already authorised; each session holds
its own copy so a mid-connection regenerate cannot retroactively unauthorise it.
What changes is what the *next* `hello` must carry, and the toast says exactly
that: `new token - the attached Chat UI keeps its connection; the next one has to
carry this.`

### 5.2 The header

| Control | Verb |
|---|---|
| slot tab | `slot(name)` |
| `Set chat region...` | `set_region()` |
| `Identify` | `identify()` |
| `Hide elements` | `elements(visible)` |

### 5.3 The editor

The `svc_*` family — `svc_select`, `svc_form`, `svc_detection`,
`svc_edit_by_lines`, `svc_after_delivery`, `svc_scroll`, `svc_matcher`,
`svc_tolerance`, `svc_add`, `svc_reset`, `svc_delete`, `svc_prev`, `svc_next`,
`svc_capture`, `svc_clear`, `svc_click_point`, `svc_forget`, `svc_close` — plus
`prompt(prompt_id, value)` for the modal. Specified in `service-editor.md`.

---

## 6. Invariants & edge cases

### 6.1 The embedded window has no Serve band

The Chat UI opens this same page beside itself (`F2`, the titlebar's
**calibrate** button, either sidebar door) and **never** passes a `ServePanel`.
No `serve` event is ever sent, and the band stays `hidden` — its visibility is
decided by the *absence of an event*, not by a flag, which is why there is no way
for a chat window to accidentally show it.

The reason is not tidiness. A second listener onto the same mouse is not a
feature: the machine already has a Chat UI driving it in-process, and a port that
let a second brain in beside it would be two brains on one desktop with no
arbiter. Serving is what a monitor does when it is *alone* on its machine.

The other differences, standalone vs embedded:

| | Standalone (`agentclip-monitor`) | Embedded (`F2` in the Chat UI) |
|---|---|---|
| the pywebview pump | this process owns it; `webview.start()` returns when the last window closes | the Chat UI already owns it; this window is picked up by the running pump |
| the asyncio loop | the runner owns one, on a daemon thread | borrowed from `GuiRunner`; the runner closes no view it did not open |
| the Serve band | always | never |
| remembered regions | `regions_dir` is passed; regions persist | not passed; the region is the session's, as it always was |
| where a saved preset goes | `config.toml`, and that is the whole propagation | plus `on_config_change` / `on_calibration` back into the live session |
| closing | the pump returns, then the runner stops | pywebview's `closed` event closes the view on the *chat's* loop |

### 6.2 The suspend bracket is per capture here, per visit there

ui-monitor.md §9.1 planned for a standalone Monitor UI to simply stay suspended.
It cannot: the ELEMENTS column is the surface you calibrate *against*, and
suspending the poller for the whole visit freezes exactly the thing you came to
watch. So **in this window the bracket is per operation** — a capture, the region
picker and `/identify` each `suspend()` and `resume()` around their own overlay —
while the **Chat UI** keeps §6.4's per-visit bracket on its *own* monitor. Two
monitors, two brackets, and the embedded case has both.

### 6.3 One window at a time, one picker at a time

A second `F2` in the Chat UI toasts `the calibration window is already open`
rather than opening a second window. A second fullscreen surface while one is up
toasts `a region picker is already open - finish it or press Esc first`, because
the tkinter child process cannot be cancelled once it is drawn.

### 6.4 The picker captures its slot when it opens

The target slot is taken when the overlay appears, not when it closes, so moving
the tab mid-drag cannot misfile the rectangle.

### 6.5 `/identify` captures before it draws

The frame is captured *first*, with the poller's own tolerance and matcher, and
only then is the overlay drawn — otherwise the overlay would be in the picture.
The summary toast comes after the overlay is down. With no region drawn it
refuses: `no chat window drawn for this window - use "Set chat region..." first;
there is nothing to identify inside yet`.

### 6.6 The panel closes before the window

`CalibrationView.close()` stops the Serve panel *first*, so the listener dies
while there is still a loop to close it on. Otherwise the port would be held
against the next launch of the same window.

### 6.7 A non-numeric port does nothing

`Start` with an unparseable port is a silent no-op — no toast, no error, no
disabled button. This is a **known rough edge**, recorded here so the next reader
does not mistake it for a decision: the field should either refuse or be
constrained, and today it does neither.

### 6.8 Two verbs are declared and unused

`start()` and `close_window()` exist on the js_api and the page never calls
them. The page's only close door is the editor's `Close (esc)`; there is no
window-close button in the header at all.

---

## 7. The command line

The window and the windowless server are **one parser** — `build_arg_parser` is
imported unchanged from `driver/monitor/__main__.py`, so `--help` is identical
either way and the two doors can never drift on argument grammar.

| Flag | With a window | With `--headless` |
|---|---|---|
| `--port N` | optional: pre-fills the Serve panel **and arms auto-start** | **required** |
| `--bind ADDRESS` | pre-fills the address dropdown | the bind address |
| `--no-token` | pre-ticks the checkbox | serve unauthenticated (loopback only) |
| `--token` / `--token-file` | **accepted and ignored**, with a notice | the token to require |
| `--config-dir PATH` | where the token and the regions live | same |
| `--project`, `--service`, `--global-config`, `--profile-root` | config layering | same |
| `--version`, `--list-matchers` | answered during parsing, before either door | same |

`--headless` delegates to `driver/monitor/__main__.py`'s `main` with the
**original `argv`**, not the parsed namespace — so the windowless door owns its
own validation and its own error sentences, and the two can never disagree. That
door imports no toolkit at all, which is what keeps `--headless` honest on a
server with no desktop.

`--token` / `--token-file` are ignored rather than rejected in the window so one
launcher script can feed both doors. The notice says why:

> agentclip-monitor: --token/--token-file is a --headless flag; the Monitor UI
> serves with the token in its config dir, which the Serve panel shows

Exit codes from the window door: `0` when the pump returns, `2` when pywebview is
missing (`agentclip: the gui extra is not installed - run: uv sync --extra gui
(or: pip install 'agentclip[gui]')`) and `2` for any other failure to start
(`agentclip: the monitor window could not start: {exc}`).

---

## 8. What this window deliberately does not have

- **No session, no transcript, no engine.** It cannot import `shell/chat` or
  `shell/app`, and the layering test says so by name. A monitor holding a session
  would be a second brain on the machine it is supposed to be serving to one.
- **No automation loop.** It never builds an `AutomationController` and never
  implements `AutomationView`. It watches; it does not decide.
- **No remote monitor.** It is built over a `LocalUIMonitor`, always: this window
  configures *the machine it is running on*. Configuring somebody else's machine
  from here is what the Chat UI's Monitor tab plus a Monitor UI over there
  already is.
- **No "who is attached" beyond one sentence.** The status line names the peer
  and that is all. A monitor serves one brain at a time and the server refuses
  the second by name, so there is nothing to manage.
