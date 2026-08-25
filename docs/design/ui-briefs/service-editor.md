# UI brief: Service editor (F2)

> **Status: BUILT, in the MONITOR UI.** The editor described here is
> drawn by `src/agentclip/shell/monitor_ui/` (its model is
> `src/agentclip/shell/webview/service_editor.py`, shared and unchanged) — a
> pywebview window of its own, beside the ELEMENTS column and the chat-region
> picker, over a `LocalUIMonitor` of its own (`docs/design/ui-monitor.md` §2.6,
> §6.4, phase 4). `F2` and the sidebar's **Edit services...** now open that
> WINDOW rather than a modal inside the Chat UI, and `agentclip --calibrate`
> opens it standalone with no session and no engine behind it. §5.10's "who
> applies it" is now split across the two: the editor's saved presets reach the
> Chat UI through the window's `on_config_change`, and the Chat UI suspends
> its own detectors for the whole visit rather than per capture. Everything
> else below is unchanged and still binding. ("GUI" further down is the older
> name for the Chat UI; the prose below is not rewritten.)


Audience: engineers building a second frontend (pywebview/HTML/JS) to reach
feature parity with the Textual TUI, and maintainers keeping both UIs in
sync. This describes BEHAVIOR, framework-neutrally. The Textual
implementation is `src/agentclip/shell/tui/screens/service_editor.py` (1302
lines); the binding design doc is `docs/design/tui.md` §1.4 (lines
192-296). Cross-references below are `file:line`.

---

## 1. Purpose

The service editor is the whole per-service **profile editor**: every
setting that is a property of *one chat service* (ChatGPT, Claude.ai,
Copilot, etc.) rather than of a window or a session. That covers:

- the service's identity (key, display name) and two size budgets
  (`max_paste_chars`, `total_context_chars`);
- the stale-detector's stillness window (`stable_seconds`);
- what the service **looks like** on screen — seven captured appearance
  templates (`TemplateKind`), captured once per service and shared by every
  window/tab pointed at that service;
- which **finish signals** the detector poller may run for this service
  (`finish_signals` + `hover_scan`);
- how an outbound payload is **delivered** into the chat box (`delivery` +
  `auto_submit`);
- how the auto-copy flow **scrolls** to the newest reply (`scroll_action`);
- how a captured appearance is **found** on screen (`matcher` + `tolerance`);
- whether an unfenced reply is refused outright (`require_fenced_reply`);
- one short freeform note to the model about this host's quirks
  (`extra_instructions`).

It does **not** cover the rest of AgentClip's config surface (allowlist,
poll interval, bell/toast switches, backup retention, themes) — those stay
hand-edited TOML / live in the separate Settings screen (F4). Design note
(`service_editor.py:1-11`): this is a narrowed-scope successor to an
originally broader "ConfigScreen" sketch.

---

## 2. Anatomy

Modal, two-panel-plus-list layout (three columns) with a footer. Grouping
below matches the editor's own grouping (`service_editor.py:510-648`,
mirrored in `tui.md:200-259`).

### 2.1 Services column (left)

| Control | id | Notes |
|---|---|---|
| Service picker | `svc-select` | Single-select dropdown. Options: every service key sorted alphabetically, each labelled `"<key> (builtin|custom)"`, plus a trailing `"+ Add new service..."` sentinel row. `service_editor.py:448-454` |

### 2.2 DETECTION block (left column, under the picker)

Header: "DETECTION · finished when"

| Control | id | Label (user-facing) | Maps to |
|---|---|---|---|
| Checkbox | `svc-signal-busy` | "reasoning icon disappears" | `finish_signals` contains `"busy"` |
| Checkbox | `svc-signal-idle` | "send icon appears" | `finish_signals` contains `"idle"` |
| Checkbox | `svc-signal-stale` | "screen stops changing" | `finish_signals` contains `"stale"` |
| Checkbox | `svc-hover-scan` | "hover-scan for copy icon" | `ServicePreset.hover_scan` |
| Checkbox | `svc-require-fenced` | "require fenced replies" | `ServicePreset.require_fenced_reply` |
| Static (readout) | `svc-signal-warning` | e.g. "busy indicator: ticked but not captured — it will be skipped" | derived, not a control |

Labels are deliberately worded as what the user will **see** happen in
their own chat window, not as the underlying TOML/detector names
(`service_editor.py:165-206`).

### 2.3 DELIVERY block

Header: "DELIVERY · how it goes in"

| Control | id | Label | Maps to |
|---|---|---|---|
| Checkbox | `svc-stream-delivery` | "paste the payload in chunks" | `delivery`: unticked = `"paste"`, ticked = `"stream"` |
| Checkbox | `svc-auto-submit` | "press Enter after auto-paste" | `ServicePreset.auto_submit` |

### 2.4 SCROLL block

Header: "SCROLL · reaching the reply". A `RadioSet` (`svc-scroll-set`) of
exactly one selection, three options → `ServicePreset.scroll_action`:

| id | Label | Value |
|---|---|---|
| `svc-scroll-scroll` | "mouse wheel flick" | `"scroll"` |
| `svc-scroll-page_down` | "Page Down taps" | `"page_down"` |
| `svc-scroll-end` | "End key" | `"end"` |

### 2.5 MATCHING block

Header: "MATCHING · how it is found".

- `RadioSet` `svc-matcher-set`, two options → `ServicePreset.matcher`:
  - `svc-matcher-anchors` — "Anchors (built-in)" → `"anchors"`
  - `svc-matcher-opencv` — "OpenCV (exhaustive)" → `"opencv"`
- Static readout `svc-matcher-warning` — shown only when OpenCV is selected
  and unavailable on this machine/build (see §6).
- Label "Pixel tolerance" (static), then a **slider** `svc-tolerance`,
  range 0-64, default 24 → `ServicePreset.tolerance`.

### 2.6 Form column (middle, the flexible/resizing column while there are three)

| Control | id | Field | Notes |
|---|---|---|---|
| Text input | `svc-key` | `ServicePreset.key` | Placeholder "lowercase-with-hyphens". Disabled/read-only except when creating a new service; immutable once created. |
| Text input | `svc-label` | `ServicePreset.label` | Placeholder "display name". |
| Text input | `svc-max` | `ServicePreset.max_paste_chars` | Placeholder "e.g. 12000". Integer. |
| Text input | `svc-total` | `ServicePreset.total_context_chars` | Placeholder "e.g. 500000". Integer. |
| Text input | `svc-stable` | `ServicePreset.stable_seconds` | Placeholder "e.g. 2.0". Float, 0.5-60. |
| Multiline text area | `svc-extra-instructions` | `ServicePreset.extra_instructions` | Placeholder "e.g. put a space between ] and ( in code". Small/short box by design — see §6 budget note. |
| Static (error readout) | `svc-error` | — | Inline validation message; empty when valid. |

### 2.7 Appearance column (right)

Header: "APPEARANCE · what it looks like". One **card** per `TemplateKind`
(7 cards, declaration order — see §6 for the full list). A
card is a strict vertical stack: side by side, a picture, the arrows that
walk its stack, a coordinate pair and two buttons need more unshrinkable
width than this lane has at any window size, and it is the buttons that
fall off the end. Stacked, a card is about half a lane wide, so the cards
flow into as many columns as the lane can hold — two in the three-lane
layout, more once the lanes stack (§7) and this one has the modal's full
width. Top to bottom, a card holds:

1. **Name + status**, stacked, as the card's heading:
   - kind label (e.g. "Busy indicator")
   - status line `svc-tpl-<kind>`: `"not captured"` / `"<w>×<h> · captured"`
     / `"<w>×<h> · N/M"` — the position of the shown variant in the stack,
     and its dimensions, since variants of one control are routinely
     different sizes. (TUI: `"<w>×<h> · N images"`, the first image's size,
     `service_editor.py:363-376`.)
2. **Thumbnail** (`svc-tpl-preview-<kind>`) — a picture of *one* captured
   image for that kind — the one the card is currently **showing** — or blank
   if none. Rendered as a sixel bitmap when the terminal supports it, else as
   a 12×2 half-block-cell approximation (TUI-only distinction — see §7). In a
   GUI frontend this is simply an `<img>`, as wide as the card has room for
   and of a fixed height, so a row of cards stays level.
3. **Two arrows**, one either side of the thumbnail — on the same line as
   it, the one thing on the card that is not a row of its own — that step the
   shown variant back and forward through that kind's stack, **wrapping** at
   both ends. Disabled (not hidden — the column must not reflow when a second
   capture lands) while the kind holds fewer than two images. Which variant
   is shown is state of the editor, not of the card's widget: it is
   reconciled — clamped — against the folder every time the profile is
   re-read. *TUI: not implemented; the Textual editor shows the first
   variant only (§7).*
4. **Click point** — one labelled line under the picture it aims into:
   "click %" and two number boxes with a `×` between them (x then y, 0-100,
   default 50/50 — behaviour in §5.5). Three characters wide each, so what
   the number means is on the boxes' tooltips rather than beside them.
5. **Actions**, side by side along the bottom of the card:
   - Button `svc-capture-<kind>-btn` — "Capture"
   - Button `svc-clear-<kind>-btn` — "Clear" (disabled while that kind has
     nothing captured)

Below the seven rows:

- Static `svc-templates` — read-only summary count, e.g. `"appearance:
  3/7 captured"`, or `"appearance: nothing captured yet"` when nothing at
  all is captured.
- Button `svc-forget-templates-btn` — "Forget appearance" — visible only
  when the service has at least one captured kind. Deletes the **whole**
  profile (all 7 kinds), behind a confirmation dialog.

### 2.8 Footer

One row shared by a hint and exactly one mode-dependent action button
(only one of the three buttons below is visible/displayed at a time,
`_update_buttons`, `service_editor.py:833-866`):

- Hint text: "escape closes (applies valid edits) · built-ins: edit or
  reset, never delete"
- Button `svc-add-btn` — "Add service" (primary) — visible only in
  "+ Add new" mode; enabled only once the new-service candidate validates.
- Button `svc-reset-btn` — "Reset to default" — visible only when the
  selected service is one of the 12 built-ins.
- Button `svc-delete-btn` — "Delete" (destructive) — visible only when the
  selected service is a non-built-in (custom) key.

---

## 3. States

### 3.1 Per-field validity (form column)

Validation runs on every keystroke against the **whole candidate** at once
(`_revalidate`, `service_editor.py:887-978`) — it is not per-field, it is
one function that walks the fields in order and stops at the first
problem:

1. If in "+ Add new" mode: `key` required, non-empty, must match
   `^[a-z0-9]+(-[a-z0-9]+)*$`, and must not already be in use.
2. `label` required, non-empty.
3. `max_paste_chars`: must parse as an integer, must be `> 0`.
4. `total_context_chars`: must parse as an integer, must be `> 0`.
5. Cross-field: `max_paste_chars` must be `<= total_context_chars`.
6. `stable_seconds`: must parse as a float, must be in `[0.5, 60.0]`
   inclusive (the exact bounds `config.py`'s loader enforces, so a value
   the editor accepts is never silently rewritten on next start).

`extra_instructions` has **no validation at all** — any text, including
empty, is legal.

The error message (first one hit, if any) is shown in `svc-error`; empty
string when the candidate is fully valid.

### 3.2 Dirty / apply state

Two different commit models depending on whether an existing service or a
new one is being edited:

- **Editing an existing preset** (key already exists): every field that
  currently validates is written **live**, straight into the in-memory
  working copy, on every change — there is no separate "apply"/"save"
  step per field. An **invalid** candidate is never committed; the working
  copy simply keeps its last-valid values while `svc-error` shows why.
- **Creating a new preset** (`+ Add new service...` selected): nothing is
  written to the working copy until the discrete "Add service" button is
  pressed. Until then a validated candidate is only held in a pending-new
  slot; "Add service" is disabled unless the whole candidate currently
  validates.

The checkboxes, radios and slider in the left column follow the same
live-apply rule as the form fields, independent of the form's validity —
they always write straight into the working copy on change, **except**
while in "+ Add new" mode, where they are disabled (see §3.4).

### 3.3 Capture-in-progress

Only one appearance capture (or the "Forget appearance" confirm flow) may
be in flight at a time, tracked by a single `capturing` flag
(`service_editor.py:1127-1186`). While a capture is in progress:

- Pressing any other "Capture" button is refused with a toast: "a region
  picker is already open - finish it or press Esc first."
- Escape / closing the editor is refused with a toast: "a region picker is
  open - finish it or cancel it first."

Rationale (framework-neutral, keep for the GUI): the capture flow spawns
an OS-level screen-region-picking overlay as a **separate process**;
cancelling the in-app request cannot kill that external process, so a
second concurrent request is refused outright rather than raced.

### 3.4 Discard-invalid-edit / close confirm

On close (Escape, or the GUI's equivalent close action):

- If the currently-displayed field values are **invalid**, nothing was
  ever written to the working copy — but the visible (unsaved, un-appliable)
  text would simply vanish, which is surprising. So close instead shows a
  confirmation dialog: *"Discard the pending edit? The current field
  values are invalid (\<reason\>) and were never applied. Close the service
  editor anyway?"* Declining returns focus to the editor with the invalid
  text intact; confirming closes as if the field had never been touched.
- If fields are valid (or there was nothing to invalidate), close proceeds
  immediately with no dialog.

### 3.5 "+ Add new service" enable/disable state

While the `+ Add new service...` sentinel is selected and "Add service"
has not yet been pressed, there is **no key to file anything under**, so
every one of the following is disabled (not hidden — the layout must not
reflow while the user fills the form):

- every per-kind "Capture" button
- every per-kind "Clear" button
- all DETECTION checkboxes
- both DELIVERY checkboxes
- the SCROLL radio set
- the MATCHING radio set
- the tolerance slider

They are **not blank** while disabled: they display the values a press of
"Add service" would actually create (`ServicePreset` dataclass defaults —
"screen stops changing" ticked, everything else off, matcher = anchors,
tolerance = 24) so the form never lies about what pressing "Add service"
will produce (`service_editor.py:135-144`, `766-793`).

Once "Add service" is pressed, the new key is added to the working copy,
the picker's options refresh, the new key becomes selected, and every
control above comes alive.

### 3.6 Button visibility by selection

- "Add service": visible only in "+ Add new" mode.
- "Reset to default": visible only when the selected key is one of the 12
  built-ins.
- "Delete": visible only when the selected key is a non-built-in (custom)
  key. Built-ins can never be deleted.
- "Forget appearance": visible only when the selected service currently
  has at least one captured `TemplateKind`.
- Each kind's "Clear" button: disabled when that kind currently holds no
  captured images (nothing for it to do). It is enabled by *any* number of
  images, because it removes the one on show rather than the stack (§5.5).
- Each kind's two variant arrows: disabled when that kind holds fewer than
  two images (nothing to cycle between). Disabled rather than hidden, for
  the same reason as everything else in §3.5 — the appearance column must
  not reflow while the user is pointing at it.

---

## 4. Inputs from core

### 4.1 `ServicePreset` (the per-service settings row)

Source: `src/agentclip/config.py:207-277`. Frozen dataclass.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `key` | `str` | — | slug id, immutable once created, `^[a-z0-9]+(-[a-z0-9]+)*$` |
| `label` | `str` | — | display name |
| `max_paste_chars` | `int` | — | budget for a single paste (chunking splits above it) |
| `total_context_chars` | `int` | — | whole conversation window, ~chars (tokens × ~4) |
| `wrap_blocks_in_fence` | `bool` | `True` | not exposed in this editor |
| `attachment_note` | `bool` | `True` | not exposed in this editor |
| `stable_seconds` | `float` | `2.0` (`DEFAULT_STABLE_SECONDS`) | stale-detector stillness window, bounds 0.5-60 |
| `finish_signals` | `tuple[str,...]` | `("stale",)` | subset of `("busy","idle","stale")`, canonical order |
| `hover_scan` | `bool` | `False` | may auto-copy glide the real cursor hunting a copy icon |
| `delivery` | `str` | `"paste"` | `"paste"` or `"stream"` |
| `matcher` | `str` | `"anchors"` | `"anchors"` or `"opencv"` |
| `tolerance` | `int` | `24` | per-channel pixel slack, 0-64 |
| `scroll_action` | `str` | `"scroll"` | `"scroll"`, `"page_down"`, or `"end"` |
| `auto_submit` | `bool` | `False` | press Enter automatically after auto-paste |
| `require_fenced_reply` | `bool` | `False` | refuse/bounce an unfenced reply |
| `extra_instructions` | `str` | `""` | free-text note shipped verbatim in the model bootstrap |

Constants (`config.py`): `FINISH_SIGNALS = ("busy","idle","stale")`,
`DELIVERY_PASTE/"paste"`, `DELIVERY_STREAM/"stream"`,
`MATCHER_ANCHORS/"anchors"`, `MATCHER_OPENCV/"opencv"`,
`SCROLL_WHEEL/"scroll"`, `SCROLL_PAGE_DOWN/"page_down"`,
`SCROLL_END/"end"`, `TOLERANCE_MIN=0`, `TOLERANCE_MAX=64`,
`DEFAULT_STABLE_SECONDS=2.0`. `normalize_finish_signals()`
(`config.py:198-204`) dedupes and re-sorts any signal set back into
canonical `FINISH_SIGNALS` order and drops unknown values.

`BUILTIN_SERVICE_KEYS` (`config.py:308`) = the 12 shipped preset keys
(`default_services()`, `config.py:280-303`): `chatgpt`, `chatgpt-attach`,
`copilot-work`, `copilot-web`, `copilot-free`, `claude`, `gemini`,
`perplexity`, `deepseek`, `grok`, `unknown`, `paranoid`. These can be
edited and reset but never deleted.

### 4.2 `ServiceProfile` / `TemplateKind` (the captured-appearance model)

Source: `src/agentclip/driver/screen/profile.py`.

`TemplateKind` is a `StrEnum` of exactly 7 members (`profile.py:99-108`),
in this declaration order (the order the editor lists them in):

| Member | Value | Meaning | `max_diff` |
|---|---|---|---|
| `BUSY` | `"busy"` | something on screen only while the model is generating (e.g. stop button) | 0.08 |
| `IDLE` | `"idle"` | something on screen only while the chat is idle (e.g. send button) | 0.08 |
| `CHATBOX_INITIAL` | `"chatbox-initial"` | the empty chat input as it sits in a fresh chat (centred) | 0.20 |
| `CHATBOX_ONGOING` | `"chatbox-ongoing"` | the empty chat input as it sits in an ongoing chat (docked bottom) | 0.20 |
| `COPY` | `"copy"` | one copy-button icon under the latest reply | 0.08 |
| `NEW_CHAT` | `"new-chat"` | the browser's new-chat button | 0.10 |
| `SEND_READY` | `"send-ready"` | the send button, captured WITH something typed (the control that disappears on send) | 0.10 |

`max_diff` is the fraction of sampled pixels allowed to differ and still
count as a match — a per-kind property, distinct from the per-service
`tolerance` (which is how far one pixel's channel value may drift).

`ServiceProfile` (`profile.py:126-186`): one service's captured
appearances.

- `templates: dict[TemplateKind, list[Template]]` — a **stack** per kind
  (not a single image): a control can be drawn multiple ways (e.g. a send
  button greyed out during a file upload), and all variants in the stack
  are OR'd together at match time.
- `.has(kind) -> bool`
- `.variants(kind) -> tuple[Template, ...]` — every captured image for
  that kind, in capture order.
- `.put(kind, image: RegionImage) -> None` — appends a new variant; raises
  `ValueError` if the capture is unsearchable (empty drag, or narrower
  than one anchor).
- `.drop(kind) -> None` — empties one kind's whole stack.
- `.captured -> tuple[TemplateKind, ...]` — which kinds have at least one
  variant, in declaration order.
- `.describe() -> str` — `"N/7 captured"`.

### 4.3 Where profiles are stored

Source: `src/agentclip/driver/screen/profile_store.py`. One folder per service
under a `profile_root` directory:

```
<root>/<service-key>/profile.json
<root>/<service-key>/busy.png
<root>/<service-key>/copy.png
<root>/<service-key>/send-ready.png
<root>/<service-key>/send-ready-2.png   # a second variant of the same kind
```

- `load_profile(root, key) -> ServiceProfile` — **never raises**; any
  corruption (missing folder, unknown format version, truncated JSON, a
  PNG that fails to decode) degrades to "not captured" for that piece,
  never an error surfaced to the user (`profile_store.py:20-23`,
  `222-247`).
- `save_template(root, key, kind, image: RegionImage) -> None` — writes
  the PNG then rewrites the manifest, atomically, PNG-first
  manifest-last (`profile_store.py:250-282`). Raises `ProfileStoreError`
  on I/O failure or an unrecognized manifest format version.
- `drop_template(root, key, kind) -> None` — removes one kind's whole
  stack from the manifest, then deletes its files
  (`profile_store.py:285-310`).
- `drop_variant(root, key, kind, index) -> None` — the same, over one
  0-based entry of a kind's stack: unlist it, then unlink it. The stack
  closes up behind it, a kind whose last image goes is unlisted entirely,
  and an index naming nothing is a no-op rather than an error. This is what
  the GUI's per-variant "Clear" calls.
- `delete_profile(root, key) -> None` — removes the entire per-service
  folder (`profile_store.py:313-321`).

Format is versioned (`FORMAT_VERSION = 2`); an old format-1 manifest (one
entry per kind, not a list) is read transparently as a one-variant stack
and migrates to format 2 on next write. A manifest written by a *future*
unknown version is refused for writing (to avoid orphaning files it
doesn't understand) but still read as "nothing captured."

### 4.4 What a capture returns

A capture is a two-step flow:

1. `pick_region(prompt: str) -> ScreenRegion | None` (`driver/screen/picker.py`)
   — opens an OS-level, full-screen "drag a box" overlay (a **separate
   process**, not a widget), pre-filled with the per-`TemplateKind`
   instructional prompt text (`TemplateKind.prompt`, `profile.py:37-75`
   — e.g. for `BUSY`: *"Drag a TIGHT box around something that is on
   screen ONLY while the model is generating... avoid animated
   spinners"*). Returns `None` if the user cancels (Esc); raises
   `ScreenPickError` on a picker failure.
2. `capture_region(region) -> RegionImage` (`driver/screen/capture.py:27-31`,
   `137-199`) — grabs the live pixels of that screen region. `RegionImage`
   is `{width: int, height: int, <BGRX pixel bytes, top-down rows,
   width*height*4 long>}`. Raises `CaptureError` on failure.

The captured `RegionImage` is then anchored (via a throwaway
`ServiceProfile.put`, which raises `ValueError` if the box is
unsearchable — e.g. too narrow for one anchor) *before* it's written to
disk, so a doomed-to-never-match capture is refused up front rather than
silently saved.

---

## 5. User actions out

### 5.1 Field edits (form column)

Editing `svc-key`/`svc-label`/`svc-max`/`svc-total`/`svc-stable`/
`svc-extra-instructions` on an **existing** service re-validates the whole
candidate on every change and, only while it is fully valid, writes
`replace(existing, label=..., max_paste_chars=..., total_context_chars=...,
stable_seconds=..., extra_instructions=...)` into the in-memory working
copy (`_revalidate`, `service_editor.py:969-978`). `key` itself is
never editable on an existing service (input disabled).

On the `+ Add new` candidate, the same fields build a pending
`ServicePreset(key=, label=, max_paste_chars=, total_context_chars=,
stable_seconds=, extra_instructions=...)` with every *other* field left at
dataclass default, held until "Add service" is pressed
(`service_editor.py:959-968`).

### 5.2 DETECTION checkboxes

Any of the 5 checkboxes changing folds **all of them** back into the
selected preset at once (not incrementally per-box), live, no validation
gate (`_on_detection_changed`, `service_editor.py:982-1015`):
`finish_signals` rebuilt via `normalize_finish_signals`, plus
`hover_scan`, `delivery`, `require_fenced_reply`,
`auto_submit` all read fresh and written via `replace(...)`. Reading "as a
set" rather than per-toggle is deliberate: it makes the handler immune to
the echo Textual fires when the screen itself sets checkbox values while
loading a different service (a UI-framework-specific note, but the
*shape* — read the whole group on any single change, never trust which
control fired — is worth carrying into any reactive-forms
implementation).

After the write, the "ticked but not captured" warning line
(`svc-signal-warning`) is recomputed.

### 5.3 SCROLL / MATCHING radios

Selecting a radio button writes only that field —
`scroll_action`/`matcher` — via `replace(...)`
(`_on_scroll_changed`/`_on_matcher_changed`, `service_editor.py:1019-1063`).
Selecting MATCHING also repaints the OpenCV-availability warning
immediately, even in "+ Add new" mode where there is no preset to write to
yet (the warning still needs to reflect what's currently *shown*).

### 5.4 Tolerance slider

Any slider move writes `tolerance=event.value` via `replace(...)`
immediately, with **no validation gate** — the control cannot express a
value outside its own configured 0-64 range, which is exactly the range
`config.py` enforces on load, so nothing it can produce is ever invalid
(`_on_tolerance_changed`, `service_editor.py:1065-1078`).

### 5.5 Capture / Clear (per `TemplateKind`)

- **Capture**: runs the pick→capture→anchor→save flow described in §4.4.
  Writes the PNG to the profile store **immediately** on success — not
  deferred to editor close. Sets an internal `profiles_changed` flag.
  Repaints that kind's thumbnail/status and the whole-profile summary line
  on completion, and leaves the row **showing the newly captured variant**
  (the last of the stack) — a row that went on showing the older picture
  would read as a capture that did not land. On any failure at any step
  (cancel, pick error, capture error, unsearchable region, save error)
  nothing is written and a toast explains why; a cancel is reported
  neutrally ("… unchanged (selection cancelled)").
- **Clear**: immediately drops **the variant the row is showing** from disk
  — one image, not the kind — **no confirmation dialog**; rationale: one
  capture press away from being restored, so a confirm would cost more
  attention than the mistake it guards against. Sets `profiles_changed`.
  Repaints the same readouts as Capture. The shown index does **not** follow
  the picture it dropped: it stays where it is, so the variant that slides
  into the slot is the one on show, and clearing the *last* variant clamps
  back to the new last one. Clearing the only variant returns the row to
  "not captured". *TUI: drops that kind's entire stack — the Textual editor
  has no shown-variant notion (§7).*
- **The two variant arrows**: page-side only. They move which variant the
  row shows (wrapping) and touch neither disk nor `profiles_changed`.
- **The two click-point boxes** (x% × y%, 0-100, default 50/50): where
  inside the matched picture that kind's click lands (tui.md §3.4d). Written
  to the profile store **immediately**, like a capture, and clamped rather
  than refused — a number box can hold "999" for as long as it takes to type
  "99". Disabled in "+ add new" mode with the rest of the controls, and
  reset to 50/50 when the kind's last picture is cleared.

### 5.6 "Forget appearance"

Deletes the **entire** service's profile folder (all 7 kinds, all
variants) from disk. Behind a confirmation dialog: *"Forget the \<key\>
appearance? The captured images of this service's buttons and chat box
will be deleted from disk. Its size settings are untouched, but every one
of those images has to be captured again."* Declining leaves everything
untouched. Confirming sets `profiles_changed` and repaints. Does not touch
the `ServicePreset` row at all (size/detection/etc. settings survive).

### 5.7 "Add service"

Only enabled once the pending-new candidate is fully valid. Commits the
pending `ServicePreset` into the working services dict under its new key,
refreshes the service picker's option list, and selects the newly created
key (which now enables every previously-disabled control for it).

### 5.8 "Reset to default"

Only shown for a built-in key. Replaces that key's row in the working copy
with `default_services()[key]` — the shipped defaults — regardless of
whether it currently differs (idempotent no-op if already default).
Reloads the form to reflect it. Does **not** touch that service's captured
appearances at all.

### 5.9 "Delete"

Only shown for a non-built-in (custom) key. Removes the key from the
working services dict **and** deletes its whole profile folder from disk
(best-effort; a delete failure is swallowed, not surfaced — the profile
readout is always re-derived from disk state anyway). Rationale: once the
key is gone from every picker in the app, its profile folder is
unreachable from anywhere, so leaving it on disk would just be orphaned
files. Selects the alphabetically-first remaining key afterward.

### 5.10 Save path — the `ServiceEdits` result object

```python
@dataclass(frozen=True, slots=True)
class ServiceEdits:
    services: dict[str, ServicePreset] | None
    profiles_changed: bool
```

(`service_editor.py:433-445`)

- `services` is the **whole edited presets table**, or `None` when the
  table came out byte-for-byte identical to what the editor opened with
  (nothing to persist).
- `profiles_changed` is a **separate** boolean because captured
  appearances are written/deleted on disk the instant the user acts on
  them (§5.5, §5.6, §5.9's delete side-effect) — never on close — so the
  caller cannot diff for that; it has to be told explicitly to invalidate
  its own profile cache.

On close, if `services` is `None` **and** `profiles_changed` is `False`,
the whole screen result is `None` ("nothing happened here at all",
`service_editor.py:1297-1301`) and the caller does nothing.

**Who applies it** — `AgentClipApp._open_service_editor`
(`src/agentclip/shell/tui/app.py:780-825`), called from the F2 binding /
sidebar "Edit services..." button (`action_settings`,
`shell/tui/app.py:762-778`):

1. Before opening, suspends the finish-signal detector poller for the
   whole visit — capturing an appearance throws a fullscreen overlay over
   the very browser window the detector is watching, and the overlay
   appearing/vanishing is itself a large-enough delta to false-trigger the
   staleness detector.
2. Opens the editor pre-selected on whichever service the currently
   active window tab is pointed at (re-read fresh on every open, not
   cached), falling back to the configured default service, then
   alphabetically-first.
3. On a non-`None` result:
   - if `result.services is not None`: persists to the TOML config file
     (`save_services`, writes only fields that differ from a built-in's
     shipped default — see §6), and folds it into a fresh in-memory
     `Config`.
   - regardless of which half changed: refreshes the main screen's
     profile cache, repaints the sidebar's per-service appearance
     summary, and rebuilds/restarts the detector poller around whatever
     is now on disk — this is what makes a freshly-captured busy/idle
     appearance start being detected without an app restart.
4. In a `finally`: resumes the detector poller (idempotent if already
   restarted by step 3).
5. Toasts either "service presets saved" or "appearance updated"
   depending on which half of the result was non-trivial.

A config edit only affects **future** sessions started in this process —
a session already in flight keeps the `Config` snapshot its engine was
built from (`tui.md:295`).

---

## 6. Invariants & edge cases

- **Cross-field size rule**: `max_paste_chars <= total_context_chars`,
  always. Violating it is a validation error, not a silent clamp.
- **Key immutability**: once a service is created, its `key` can never be
  edited — only deleted (custom) or reset (built-in). The key input is
  disabled for any existing service.
- **Key format**: `^[a-z0-9]+(-[a-z0-9]+)*$` — lowercase letters, digits,
  single hyphens between groups. Enforced both by the editor's own regex
  (`service_editor.py:145`) and by `profile_store`'s directory-name guard
  (`profile_store.py:64`), since the key becomes a filesystem directory
  name.
- **`stable_seconds` bounds**: `0.5` to `60.0` inclusive — chosen to match
  exactly what `config.py`'s loader enforces on startup, so a value the
  editor accepts is never silently rewritten the next time the app loads.
- **Invalid save attempt on close**: never possible to *lose* data (an
  invalid candidate was never committed to the working copy in the first
  place), but the user is still asked to confirm discarding the
  never-applied visible text, via a confirmation dialog, before the editor
  is allowed to close (§3.4).
- **The seven `TemplateKind`s** (full list, with meaning and per-kind
  match looseness `max_diff`) — see §4.2 table. All seven are always shown
  regardless of whether any are captured.
- **Capture requirements / anchoring**: a captured region is validated as
  *searchable* (via `ServiceProfile.put` on a throwaway profile, which
  runs `Template.build`) **before** it ever reaches disk. A box too narrow
  to anchor, or an empty drag/cancelled selection, is refused with an
  error toast and nothing is written.
- **A kind is a stack, not a slot**: every `Capture` press on an
  already-captured kind **adds** a new variant rather than replacing the
  existing one (all variants are OR'd together at match time). Only
  `Clear` (the one variant on show) or `Forget appearance` (the whole
  profile) removes anything. The row is therefore a **window onto a stack**,
  not a picture of a slot: exactly one variant is on show at a time, the
  arrows move which, and the status line names the position so the user can
  say what they are looking at. Which variant is showing is UI state and
  nothing else — it is never persisted, it is clamped rather than trusted
  whenever the folder is re-read (it moves under the editor: a clear, a
  capture, a forget, another service selected), and there is no ordering
  meaning to the position beyond capture order.
- **Detection/appearance are ANDed, not just the checklist alone**: a
  ticked `busy` or `idle` finish signal whose corresponding `TemplateKind`
  (`BUSY`/`IDLE`) has never been captured runs **nothing at all** — the
  poller needs both the checklist entry *and* the appearance. This is
  silent unless surfaced; the editor surfaces it inline via
  `svc-signal-warning`: *"\<kind\>: ticked but not captured — it will be
  skipped."* `stale` needs no appearance capture at all (it works off the
  drawn chat region itself).
- **Tolerance slider semantics**: range **0-64** (`TOLERANCE_MIN`/`MAX`),
  default **24** (`DEFAULT_TOLERANCE`). It is per-channel color drift
  allowed before a pixel counts as "different" in the shared verification
  step — a property of *the browser/theme this service renders in*, not
  of any one control. This is a **different knob** from each
  `TemplateKind`'s `max_diff` (§4.2), which is how many *pixels* may
  differ and stays fixed per kind (a property of what kind of control is
  being matched, e.g. icon-on-background vs. big rectangle). Both
  matcher backends are verified through the same tolerant per-pixel
  comparison, so one `tolerance` value governs both.
- **Matcher backend fallback when OpenCV absent**: choosing "OpenCV
  (exhaustive)" **still saves that choice** even on a machine/build
  without OpenCV available — the user may be configuring a machine they're
  about to install it on. But the actual runtime search silently falls
  back to the anchors backend, and the editor shows an inline warning the
  moment OpenCV is selected and unavailable
  (`svc-matcher-warning`/`opencv_missing_note`,
  `service_editor.py:250-269`, `795-807`). The warning **text differs by
  install kind**, and this distinction must be preserved by a second
  frontend:
  - from a source/pip install: *"OpenCV is not installed — anchors will
    be used. Install it with `pip install agentclip[cv]`"*
  - from a frozen/bundled build: *"This build does not include OpenCV —
    anchors will be used. Nothing to install: it has to be built in."*
    (the frozen build ships OpenCV bundled by default, so landing here at
    all means it was built *without* the `cv` extra — telling the user to
    `pip install` would be actively misleading since there's no
    environment to install into.)
  - The OpenCV-availability probe and the frozen/source verdict are each
    computed **once** per editor session (not re-checked per keystroke —
    the probe is a real import of a ~60MB wheel).
- **`extra_instructions` has no length validation**, but the field is
  deliberately kept small/short in the UI (a compact text area, not a
  full editor) because the text rides the model bootstrap payload, which
  has roughly 67 characters of slack on the smallest presets
  (protocol.md §2 budget headroom) — a paragraph pasted in here can push a
  session over budget and fail to arm. This is a soft UX nudge (box size
  + label wording), not an enforced limit.
- **`edit_by_lines` lives in the FORM column, in both shells** (an
  `EDITING` heading with one tick, below the alert fields). Not with the
  other toggles on the left, for two reasons: it is about how the *model*
  edits rather than how its reply is found or delivered, and the left
  column is at its height ceiling — the narrow-terminal Pilot tests
  (120×45 and 100×35) fail the moment it grows. In the GUI it gets its own
  `svc_edit_by_lines` bridge call rather than joining the `set_detection`
  set, which is the left column's toggles read together as one group.
- **`AFTER DELIVERY` lives in the FORM column, in both shells** — "focus
  back after send" (`snap_back`), "beep when it stalls" (`alert_sound`)
  and the "Alert repeat (seconds, 0 = once)" box (`alert_repeat_seconds`),
  directly above the `EDITING` tick. Same two reasons as the bullet above:
  they are about what happens to the *user's attention* once a delivery or
  a harvest is over rather than about how a reply is found, and the left
  column has no rows to spare. In the GUI the two ticks ride one
  `svc_after_delivery` bridge call (read as a pair, never per-box) and the
  seconds is a validated number that rides the form like the sizes do
  (blank = 0, bounds `[0, 3600]` — the loader's). `snap_back` is the debug
  aid and reads as one, so it gates **every** snap the loop makes by
  itself: the auto-send's and the auto-copy harvest's.
- **Built-ins can be edited and reset, never deleted.** `BUILTIN_SERVICE_KEYS`
  (12 keys) always show "Reset to default" and never show "Delete".
  Non-built-in (custom) keys show the reverse.
- **`save_services` writes minimally**: a preset's `[services.<key>]` TOML
  table is written only if it differs *at all* from that key's built-in
  default (an untouched or reset built-in is omitted from the file
  entirely); within a written table, `stable_seconds`/`matcher`/
  `tolerance` are each included only if *that specific field* still
  differs from the built-in default — so editing only the size fields
  doesn't accidentally pin today's default tolerance/matcher/stable value
  into the file, and a service that never customizes one of those fields
  keeps tracking future changes to the shipped default.

---

## 7. Textual-specific details NOT to carry over

These are implementation artifacts of the terminal UI and should be
replaced by native GUI equivalents, not ported:

- **Hand-built slider widget** (`shell/tui/widgets/slider.py`). Textual ships no
  slider control, so the tolerance control here is a bespoke
  track+handle+numeric-readout widget with arrow-key nudging, shift-arrow
  for ±8 steps, home/end, and click-to-jump on the track. A GUI frontend
  should use its platform/toolkit's native range/slider control — only
  the **semantics** (0-64 range, default 24, live-apply with no
  validation gate, value shown numerically beside the control) need to
  carry over, not the input mechanics.
- **Sixel vs. half-block thumbnail rendering** is a terminal-graphics
  compatibility fallback with no GUI equivalent — a GUI frontend just
  renders the captured image (whichever stack variant the row is showing,
  §2.7) as a normal `<img>`/bitmap at whatever resolution is convenient;
  there is no "coarse fallback" tier to reproduce. Specifically NOT to carry
  over:
  - the sixel/half-block **renderer choice made once at modal-open time
    and never revisited** (a real GUI doesn't need this rigidity — image
    elements can just be images);
  - the **fixed pixel/cell row cap** (`SIXEL_PREVIEW_MAX_ROWS = 3`) and
    the "column must never scroll" rule
    (`service_editor.py:299-326`, `tui.md:273-275`) — this exists *only*
    because Textual's sixel widget bypasses the terminal compositor and
    paints escape sequences directly, so a scrolled-away or clipped sixel
    thumbnail leaves visual smears that nothing can erase except a full
    repaint. A GUI's image widgets clip and scroll correctly by
    construction; this entire constraint (and the "appearance column can
    never scroll" layout rule that follows from it) has no reason to
    exist outside a terminal.
  - the 12×2-cell half-block averaging path (`shell/tui/pixels.py`
    `thumbnail`/`half_block_text`) — purely a "no real graphics
    available" degradation.
- **`push_screen_wait` / worker hand-off requirement**: opening the editor
  (F2), closing it, and the "Forget appearance" confirmation are all
  wrapped in Textual's async modal-await pattern (`push_screen_wait`),
  which specifically requires being invoked from inside a Textual
  *worker* because the key/button bindings that trigger these actions
  dispatch as plain synchronous callbacks outside of one. Practically:
  `action_settings`/`action_close`/the forget-flow's confirm are all
  `def`s that immediately do `self.run_worker(self._async_version(),
  ...)` rather than being `async def` themselves
  (`service_editor.py:1274-1284`, `1209-1213`; `shell/tui/app.py:762-778`).
  This two-step "sync entry point hands off to an async worker" dance is
  purely a Textual event-loop constraint. A GUI/JS frontend built on a
  single async event loop (or on promises/callbacks) can simply call an
  async modal-confirm function directly from the button/key handler with
  no equivalent hand-off machinery — but the **behavioral shape** it
  exists to produce must be kept: F2 (or its GUI equivalent) is refused
  outright, with a toast, while either (a) the editor is already the
  active/open screen, or (b) a chat-region capture overlay is open
  elsewhere in the app (`MainScreen.picker_open`) — two full-screen
  capture flows can never coexist regardless of how the async plumbing is
  built.
- **One-overlay-at-a-time via a manually held boolean flag**
  (`self._capturing`) rather than e.g. disabling the whole modal or using
  a modal-stack primitive — an artifact of the capture overlay being an
  external OS-level process that an in-app cancel can't kill. A GUI
  frontend implementing captures as, say, an in-window crop tool instead
  of a separate process might not need this guard at all (a cancel could
  actually cancel); if it keeps a similar separate-window/native capture
  flow, it should keep the equivalent guard, but the *mechanism* (a
  screen-level exclusive-worker group) is TUI-specific.
- **Column width/layout tuning entirely in character cells**
  (`#svc-appearance-col { width: 44 }`, `TEMPLATE_PREVIEW_COLS = 12`,
  fixed-cell text-overflow ellipsis rules, the 120×45 / 100×35 terminal
  geometries the TUI is pinned to) — none of this maps to a GUI; use
  normal responsive layout instead. The **information hierarchy** it
  encodes (fixed-width label/status lanes sized to render key phrases
  whole, one flexible column reserved for the editable form fields, a
  shared footer strip for the hint + the one visible mode-action button)
  is the part worth preserving, not the cell arithmetic.

  Three lanes is itself a *wide-window* arrangement, not the layout. The GUI
  holds the outer lanes near their intended widths only while the dialog has
  room for them, and **below ~860px stacks all three into one scrolling
  column in document order** — settings, form, appearance. The hierarchy
  survives the stacking: each lane keeps its header and its place in the
  order, so "which column am I in" becomes "how far down am I", and the
  footer stays a strip (wrapping its buttons if it must). Any frontend whose
  window can be half a small monitor owes the same fallback — a three-column
  dialog that cannot become one column is a hidden minimum window size.
