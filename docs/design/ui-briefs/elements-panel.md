# UI Brief: Elements Panel + `/identify` Overlay

> **Status: BUILT, in the MONITOR UI.** Both surfaces described here
> live in `src/agentclip/shell/monitor_ui/` — a pywebview window of its
> own, over a `LocalUIMonitor` of its own, never over the Chat UI's controller
> (`docs/design/ui-monitor.md` §2.6, §6.4, §9.1). The window that hosts them is
> specified by `monitor-ui.md`, beside this file. Two entry points, one
> implementation: **`agentclip-monitor`** runs it standalone on the machine the
> Browser is on (and `agentclip --calibrate`, which used to, is now a stub that
> names that binary), and the Chat UI opens it beside itself with `F2`, the
> titlebar's **calibrate** button or either sidebar door. It is **not** in the
> Chat UI's own window any more — there is no F7 column and no `elements`
> event on that window's bridge, and `/identify` opens the Monitor UI
> rather than drawing from the chat one. One thing the older text below cannot
> know: the crops keep updating while you are in that window, because the
> suspend bracket there is per capture, not per visit (`monitor-ui.md` §6.2) —
> this column is the surface you calibrate against. The behaviour below is
> otherwise unchanged and still binding; §7's "do not carry over" list applies
> to the Monitor UI's page exactly as written. ("GUI" further down is the older
> name for the Chat UI; the prose below is not rewritten.)
>
> **2026-08-27, ui-monitor.md §10:** the second entry point above is gone.
> The Chat UI no longer opens this beside itself — `F2`, the titlebar button,
> the sidebar doors and `/identify` all just say where the Monitor UI is, and in
> local mode that is an `agentclip-monitor` process the Chat UI launched. One
> window, one `LocalUIMonitor`, one entry point.


Audience: engineers building a second frontend (pywebview/HTML/JS) that must reach
feature parity with the Textual TUI, and maintainers keeping both UIs in sync.
This brief describes **behavior**, framework-neutrally. Textual/sixel-specific
mechanics are called out separately in §7 so they are not mistaken for product
requirements.

Primary sources: `docs/design/tui.md` §1.2, §1.7, §3.3a ("`/identify`"), §3.4e,
§3.4f; `src/agentclip/shell/tui/widgets/elements.py`; `src/agentclip/shell/tui/graphics.py`;
`src/agentclip/shell/tui/pixels.py`; `src/agentclip/shell/tui/messages.py`;
`src/agentclip/shell/tui/screens/main.py`; `src/agentclip/driver/screen/identify.py`;
`src/agentclip/driver/screen/overlay.py`; `src/agentclip/driver/screen/picker.py`;
`src/agentclip/shell/app/view.py`.

---

## 1. Purpose

AgentClip drives a browser by template-matching captured appearances (icons,
buttons, chat-box layouts) inside a user-drawn screen rectangle. Every decision
the automation makes — "is the model still generating", "is there a send
button to click", "is the copy button on screen" — is invisible by default: a
calibration status reads `"24×24 · captured"` whether or not it is actually
finding the right pixels on the live browser (`tui.md` §1.7, elements.py
docstring lines 1–23).

Two features exist to make that invisible recognition **visible**, and they
are siblings answering the same underlying question at two different scales:

- **The Elements Panel** (F7) is the *live, ambient* answer: a standing column
  showing, for every appearance the detector can currently recognise, the
  actual pixels it most recently matched, refreshed roughly twice a second.
- **`/identify`** is the *on-demand, high-fidelity* answer: a single frame
  captured right now, searched exhaustively, and shown as labelled boxes drawn
  directly over the real desktop, at 1:1 scale, next to the real buttons —
  because a 16-column crop column cannot show "is this box aimed at the right
  spot on my actual screen" the way a full-desktop overlay can (`tui.md` §1.7,
  "Deliberately not built: a full-size viewer modal").

Both exist because a calibration that silently stops matching is otherwise
undiscoverable until an automation fails in some other, unrelated way (a paste
lands in the wrong box, a copy never fires, a delegation refuses to arm).

---

## 2. Anatomy

### 2.1 Elements Panel

A column, sibling to the main chat area and the settings sidebar, **not**
nested inside the sidebar (`tui.md` §1.2, §1.7 "A third column, not more
sidebar"). Top to bottom:

1. **Title row** — `ELEMENTS · MASTER` or `ELEMENTS · SUB-AGENT`, naming which
   browser window (not which UI tab is currently selected) the rows below
   describe. `elements.elements_title()` / `ElementsPanel.show_window()`.
2. **One row per `TemplateKind`**, all seven, in a fixed order
   (`elements.ELEMENT_ORDER == screen.detector.RUNTIME_KINDS`,
   `src/agentclip/driver/screen/detector.py:113-121`):
   1. send button (`SEND_READY`)
   2. busy icon (`BUSY`)
   3. idle icon (`IDLE`)
   4. copy button (`COPY`)
   5. start chat box (`CHATBOX_INITIAL`)
   6. ongoing chat box (`CHATBOX_ONGOING`)
   7. new-chat button (`NEW_CHAT`)

   Each row has two parts:
   - a **label** — two lines of text, "kind name" over "verdict text"
     (`elements.element_line`, `elements.py:181-190`), e.g. `copy button` /
     `found · 1.2%`;
   - a **crop** — a picture of the actual matched pixels, or blank.

   Row order is deliberately identical to the detector's own report order, so
   a row can never be mistaken for a picture of a different row's search
   (`elements.py:96-103`).
3. **Hint line** — "F7 hides this column".
4. **Mode line** — `crops · sixel` or `crops · half-block` (`elements-mode`),
   naming which renderer is drawing the crops (see §7). Framework-neutral
   restatement: **the panel should always say, in words, whether it is
   currently able to show real pixels or is degraded to a fallback**, because
   a picture nobody can see and a detector that never matches are otherwise
   indistinguishable (`elements.py:140-144`; `tui.md` §1.7 "The column says
   which renderer it is using").

The column scrolls vertically when its content overtakes the viewport
(`ElementsPanel` is `overflow-y: auto`, same as the sidebar).

### 2.2 `/identify` overlay

A separate, full-desktop surface, not part of the main window at all:

1. **Prompt line**, centred near the top: *"What AgentClip sees in the chat
   window · click or press any key to dismiss"* (`IDENTIFY_PROMPT`,
   `driver/screen/overlay.py:37`).
2. **One outlined rectangle per identified element**, each in a colour shared
   by every element of the same kind, assigned in first-appearance order
   (`_identify_colours`, `overlay.py:156-166`). The drawn chat region itself
   is always included as the first, differently-coloured box (`#ffd166`,
   `IDENTIFY_REGION_COLOUR`) — the container the user drew, not a template
   match.
3. **A short badge on each box** — `#1`, `#2`, … in the order elements were
   found, `#R` for the chat-region box — rather than the full description, to
   stay readable when several matches of one appearance land a few pixels
   apart (`overlay.py:169-208`). A badge that would land on top of one already
   placed steps sideways by one badge-width.
4. **A legend block, top-right corner**, one row per element in that
   element's colour: badge, kind label, and the match diff (e.g. `#3  copy
   0.013`), with a solid backdrop so it stays legible over the translucent
   view of the browser underneath (`_draw_legend`, `overlay.py:211-253`).
5. Whole surface is **translucent** (alpha 0.45) over the real desktop, so the
   boxes are read directly against the real browser buttons they claim to
   match.

Dismissal (click anywhere, or any keypress) produces a **toast/summary
message** back in the main UI once the overlay closes: either a count broken
down by kind (*"identified 4 elements: copy×3, new-chat×1"*) or an explanation
when nothing but the chat region matched (`driver/screen/identify.py:141-157`,
`summarise()`).

---

## 3. States

### 3.1 Elements Panel

- **Hidden / visible.** A whole-column show/hide toggle (not per-row
  collapse) — F7. Hiding does **not** stop the underlying polling: crops keep
  being computed and posted into the (unrendered) panel so that un-hiding
  shows the *current* state immediately rather than a stale one from before
  the toggle (`tui.md` §3.4f; `main.py` `action_toggle_elements`,
  `main.py:2864-2876`).
- **Per-row: three distinct states**, and the distinction matters (`tui.md`
  §1.7, "Three row states"):
  1. **`no match yet`** — nothing has been searched for this kind at all,
     because the live window's service has **no capture** of that appearance
     (`ELEMENT_RESTING`). This is the row's state at mount and stays that way
     for a kind the user never calibrated.
  2. **`not on screen`** — the kind *was* searched this tick and is not
     currently visible (`ELEMENT_MISSING`).
  3. **found** — crop shown, with `found · N.N%` (the match's diff,
     `found_line()`, `elements.py:193-196`) underneath.

  A row's state comes from whether its `TemplateKind` key is present/absent
  in the tick's payload map, not from any flag — see §4.
- **Update cadence.** Roughly one search pass every 0.5 s
  (`_BUSY_POLL_S = 0.5`, `main.py:287`), one capture feeding all seven
  searches at once so every row's verdict describes the *same instant*.
  Rows that are unchanged from the previous tick are left alone (no
  redraw) — a still icon compared frame-to-frame by byte equality
  (`elements.py:267-288`, "A row showing the same pixels it was already
  showing is LEFT ALONE").
- **All 7 rows are always searched**, regardless of which finish signals are
  ticked in configuration (busy/idle can be captured but not "voted" on if
  their checklist entry is off — see §6). The panel is a picture of *what the
  tool can see*, not of what the automation is currently using to decide
  anything (`tui.md` §3.4f).
- **Whole-panel reset:** cleared back to `no match yet` on every detector
  rebuild (new region drawn, service recaptured, slot retargeted) — a crop
  cut from the old window must never be shown under the new window's title
  (`ElementsPanel.clear()`, `elements.py:329-341`).

### 3.2 `/identify` overlay

- **Closed** (normal state) — nothing on screen.
- **Refused, no overlay shown** — when the live window has no chat region
  drawn at all: a warning notification, no overlay (`main.py:3050-3056`).
  Also refused (silently, as a no-op) if another fullscreen picker/overlay
  child process is already active (`_refuse_second_picker`,
  `main.py:3031-3032`) — only one such child process may run at a time.
- **Open** — stays up until the user dismisses it (click or any key); there
  is **no timeout, no auto-dismiss**. It is "a picture the user asked for" and
  stays until they are done reading it (`overlay.py:269-272`; an earlier
  self-destruct timer was deliberately removed).
- **Detector polling is suspended while the overlay is open** and resumed
  when it closes, because the overlay itself is a large, sustained pixel
  delta over the very region being watched, which would otherwise be
  mistaken by the stillness/staleness detector for the chat's content
  changing (`main.py:3071-3078`; `tui.md` §3.4e last bullet).

---

## 4. Inputs from core

### 4.1 `RegionImage` — the raw pixel type

```
RegionImage(width: int, height: int, pixels: bytes)
```
`src/agentclip/driver/screen/capture.py:27-32`. `pixels` is **BGRX**, top-down rows,
exactly `width * height * 4` bytes — 4 bytes per pixel, blue first, 4th byte
**undefined** (not alpha). This is the on-the-wire crop format used
everywhere in this surface.

### 4.2 `ElementCrop` / `ElementsMatched` — the panel's live feed

```
ElementCrop(image: RegionImage, diff: float)          # shell/tui/messages.py:169-183
ElementsMatched(crops: Mapping[TemplateKind, ElementCrop | None], generation: int)
                                                        # shell/tui/messages.py:186-216
```

- `crops` maps each `TemplateKind` that was searched **this tick** to either
  an `ElementCrop` (found — here are the matched pixels and the diff) or
  `None` (searched, not found). **A kind absent from the map** means "not
  searched this tick" (the live service is not calibrated for that kind at
  all) — the frontend must leave that row exactly as it last was, not blank
  it (`shell/tui/messages.py:186-200`).
- A tick whose capture fails posts **no message at all** — a dropped frame is
  not evidence about anything and must not blank any row
  (`elements.py:267-276`; `tui.md` §1.7 last paragraph).
- `generation` is a monotonically-increasing stamp identifying which detector
  *run* produced the message. Consumers must discard ("ghost-filter") any
  message whose generation does not match the currently active run — a
  cancelled/rebuilt poller's in-flight messages can otherwise arrive after
  the panel has already retitled itself for a different window
  (`main.py:3487-3509`, `_ghost`).

### 4.3 Producer thread and cadence

- One dedicated worker thread per detector "run" (`MainScreen._start_detector_worker`,
  `main.py:3160-3320`), started/restarted whenever the live window, its
  drawn region, or its calibration changes. It is **not** the UI thread.
- Each tick: **one screen capture**, handed to **one** `ScreenDetector.observe()`
  call, which runs all seven searches against that single frame — this is
  deliberate: every row's verdict describes the same instant of a moving
  screen rather than seven separately-timed reads (`main.py:3166-3169`).
- Poll interval: `_BUSY_POLL_S = 0.5` seconds (`main.py:287`).
- Crop extraction (`pixels.crop`, cutting the matched rectangle back out of
  the full frame) happens **in that worker thread**, never on the UI thread —
  only an icon-sized rectangle per matched kind crosses the thread boundary,
  not the full captured region (`tui.md` §1.7 "Which thread").
- The message is posted **after** the tick's finish-detector verdicts
  (busy/idle/stale), as one combined message for that tick's pictures
  (`main.py:3296-3309`).

### 4.4 Crop/scale policy — what's presentation vs. what a GUI must keep

`elements.element_crop_image()` (`elements.py:151-166`) sizes a freshly-cut
match **for whichever renderer is live**, still in the worker thread:

- **Renderer-neutral (keep this in a GUI, it's not terminal policy):**
  - The crop cut from the frame is the **matched rectangle only** (icon-sized,
    not the whole chat window) — `pixels.crop`, `src/agentclip/shell/tui/pixels.py:126-158`.
  - BGRX→RGB conversion must treat the 4th byte as **ignored, not alpha**
    (`shell/tui/graphics.py:272-288`, `region_to_pil`, raw mode `"BGRX"` not
    `"BGRA"`) — reading it as alpha makes every crop render as fully
    transparent/invisible. **This rule must survive into any new renderer.**
  - Same-image-as-last-tick suppression (skip re-render when bytes are
    identical) is good practice for any renderer to avoid redundant paints on
    a 2Hz timer, though how much it matters depends on the GUI's own paint
    cost model.
- **Terminal-specific (do NOT carry over as-is — see §7 for the replacement):**
  - The choice between "exact cell grid, pre-averaged" (half-block fallback)
    vs. "untouched pixels, fit downstream" (sixel) is a consequence of two
    different terminal rendering paths, not a general policy. A GUI has one
    rendering path (a bitmap/image element) and should always receive full,
    untouched matched pixels and let CSS/layout scale them.
  - The specific pixel budgets (16 cols × 2–8 rows, cell-size-derived) are
    terminal cell-grid arithmetic and have no GUI equivalent; a GUI should
    define its own fixed row height in real pixels/CSS units instead.

### 4.5 `identify_elements` — `/identify`'s payload

```python
identify_elements(region: ScreenRegion, profile: ServiceProfile, scene: RegionImage,
                   *, tolerance: int, matcher: CandidateSource | None) -> list[IdentifiedElement]
```
`src/agentclip/driver/screen/identify.py:80-138`. Pure function: one already-captured
frame in, every match out. `IdentifiedElement(label: str, rect: ScreenRegion, diff: float | None)`
— `rect` is in **absolute screen pixels** (not scene-local), `diff` is `None`
only for the synthetic chat-region entry (index 0, always present, labelled
`"chat region"`). Every other kind: every captured variant is searched, up to
`MAX_MATCHES = 8` matches each, near-duplicate matches of the *same kind*
folded together (`same_element`), but matches of *different* kinds are never
folded even if they overlap — an overlap across kinds is itself a
mis-calibration worth surfacing.

`identify_elements` runs with the **same `tolerance`/`matcher` settings the
live poller uses** (`MainScreen._live_search()`, read off the live window's
service preset) — a GUI implementation must do the same, or the overlay
answers a different question than the one the poller is actually asking
(`tui.md` §3.3a, "an overlay that searched with different settings from the
poller would answer a question nobody asked").

---

## 5. User actions out

- **F7** — toggle the Elements Panel's visibility. A `priority` binding (must
  win over any focused text input) — same rationale as F3 for the sidebar
  (`tui.md` §1.2; `main.py:752`). Pure client-side visibility flip; it does
  not touch the detector worker.
- **`/identify`** (chat-command, no keyboard shortcut) — capture the live
  window's chat region once, run every calibrated search against that one
  frame, and show the desktop overlay. Also reachable from the "Describe the
  task" / awaiting-new-session prompt, where slash lines are still dispatched
  as commands rather than becoming the session's opening message
  (`tui.md` §1.3, §3.3a).
  - **No session gate** — deliberately the one command usable with no active
    session, because the moments a user most needs it are the moments
    nothing is armed yet (`tui.md` §3.3a, "Five rules"; §3.3, `/identify`
    entry).
  - **Refuses** only when there is no chat region drawn for the live window
    (toast, no overlay) or when another fullscreen picker/overlay child is
    already up (silent no-op).
  - Blocking from the caller's perspective in implementation (the child
    process's `subprocess.run` call blocks the worker thread), but **must not
    block the UI thread** — in the TUI this runs on a `Worker`; a GUI
    equivalent should run it off the main/render thread and surface the
    summary only after the overlay/dialog closes.
- **Double-tap-c re-delivery** (mentioned in project memory as a
  identify/flatten/Esc-wave feature): investigated and **does not touch this
  surface**. It concerns re-sending a flattened chat reply, unrelated to the
  Elements Panel or `/identify`. No cross-reference needed here.

---

## 6. Invariants & edge cases

1. **All seven kinds are searched every tick, unconditionally of whether
   their finish-signal checklist entry is ticked.** Whether busy/idle
   *may decide* a response finished is a separate, independent question from
   whether they are searched and drawn — a captured stop icon nobody ticked
   is still searched, cut, and drawn every tick (`tui.md` §3.4f, §1.7 "A
   capture is enough. A tick is not required"). A GUI must not gate row
   visibility/searching on any "active detector" flag.
2. **A row that's never had a capture stays at "no match yet" forever** —
   this is the *only* remaining cause of that resting state (`shell/tui/messages.py:194-197`).
   It is a precise, actionable signal ("nobody ever captured this"), not a
   loading state.
3. **BGRX, not BGRA.** The 4th captured byte is undefined; treating it as
   alpha makes every crop transparent/invisible (`shell/tui/graphics.py:272-278`).
   Cite: `region_to_pil`, `src/agentclip/shell/tui/graphics.py:284-288`.
4. **Ghost filtering by `generation`.** Any consumer of `ElementsMatched` (or
   the finish-probe messages) must drop messages whose `generation` doesn't
   match the currently active detector run — cancelling a worker only raises
   a flag, it does not stop an in-flight tick from finishing and posting
   (`main.py:3421-3431`, `_ghost`; `shell/tui/messages.py:206-209`).
5. **Live window vs. selected UI tab.** Both the Elements Panel and
   `/identify` describe the **live** (automation-driven) window, which during
   a delegation can differ from whichever window/tab the user currently has
   selected/focused in the UI. The panel's title names which window it is
   showing for exactly this reason; a GUI must carry an equivalent "which
   window is this a picture of" label and must not silently repaint the panel
   in response to the user merely switching viewed tabs (`tui.md` §3.4e,
   "Who owns the sidebar's DETECTION lines" — the Elements Panel is bound by
   the identical ownership rule).
6. **Detector off / service unconfigured.**
   - No chat region drawn at all → no detector worker runs, all rows stay at
     the empty/initial state, and `/identify` refuses with a toast instead of
     opening an overlay (`main.py:3225-3227`, `3050-3056`).
   - Region drawn but the service has **zero captured appearances**
     (`ScreenDetector.watching` is false) → no worker starts either; nothing
     to search, nothing to show (`main.py:3260-3266`).
   - Region drawn, some appearances captured, but the finish-signal checklist
     is empty → the worker **does** run and rows **do** populate (the panel
     is independent of what "may finish a response"), but no auto-copy will
     ever fire — an orthogonal, sidebar-only concern (`tui.md` §1.3 "An empty
     checklist is legal").
7. **Failed capture ≠ empty result.** A tick whose screen capture itself
   failed (`CaptureError`) posts **no** `ElementsMatched` message at all,
   rather than one with every kind mapped to `None` — a dropped frame must
   never be read as "searched and found nothing" (`elements.py:267-276`, and
   the `ElementsMatched` docstring, `shell/tui/messages.py:198-200`).
8. **Suppressed repaint on unchanged pixels.** A row whose crop bytes are
   byte-identical to what it already displayed is left untouched rather than
   re-rendered — relevant to any GUI implementation choosing its own
   diffing/memoization strategy, not just the terminal one
   (`elements.py:267-279`).
9. **`/identify` overlay suspends the live detector polling for its entire
   open duration** (unbounded, since there's no auto-dismiss), and resumes it
   in a `finally` on close — a GUI implementation opening any full-desktop
   overlay child process must apply the same bracket, or the overlay's own
   appearance/disappearance will be read by the staleness detector as the
   chat content itself changing (`main.py:3071-3078`; `tui.md` §3.4e last
   bullet, listing `/identify`, the chat-region picker, and the service
   editor as the three surfaces that must all bracket this way).
10. **Only one fullscreen picker/overlay child process at a time.** `/identify`,
    the region-drawing picker, and the service editor's own capture overlays
    all share one mutual-exclusion flag; a second request while one is open
    is refused, not queued (`main.py:3031-3032`; `tui.md` §1.4, §3.4e).
11. **Element-fold rule for `/identify`:** duplicate matches fold **only
    within one `TemplateKind`**, never across kinds — an appearance matching
    two different kinds at the same location is a real calibration problem
    that must stay visible as two boxes, not be hidden (`driver/screen/identify.py:96-100`).

---

## 7. Textual-specific details NOT to carry over

The **entire sixel/half-block rendering machinery** is a terminal-only
adaptation layer. None of the following is a product requirement for a
pywebview/HTML/JS frontend — a GUI has a native, universal way to display a
bitmap and should use it directly.

- **The "probe before Textual starts" dance.** `shell/tui/graphics.py`'s whole
  reason for existing is that `textual_image`'s sixel auto-detection performs
  a live terminal round-trip (writes a DA1 escape sequence, reads the reply
  off stdin) that **must** happen before Textual claims the terminal's stdin
  reader, or the reply is silently swallowed and sixel support is
  misdetected as absent (`shell/tui/graphics.py:1-27`, `probe_terminal()`,
  `graphics.py:135-177`). A GUI has no such race — it renders bitmaps
  natively — so there is nothing to probe and nothing to sequence before
  startup.
- **Two parallel renderers chosen once at compose time and never revisited**
  (`elements.py:245-265`, `_crop_widget`) — sixel widget vs. half-block
  `Static` text. A GUI needs exactly **one** rendering path: an `<img>` /
  canvas element (or equivalent) per row, always. **Replace with:** each
  matched crop encoded as a **PNG data-URI (or a `Blob`/object URL) and
  assigned directly to an `<img>` element per row**, sized in real CSS pixels
  the layout defines. No mode selection, no fallback path, no "which
  renderer" readout line — that entire concept (§2.1 point 4, `elements-mode`)
  disappears; there is nothing to disambiguate.
- **Compositor bypass.** Sixel data is not a printable character stream — the
  Textual sixel widget injects raw escape sequences at `render_lines` time and
  moves the cursor itself, bypassing Textual's normal compositing entirely
  (`tui.md` §1.7, "Sixel cannot go through the ordinary renderable path").
  This has no GUI analogue at all; an `<img>`/canvas paints through the
  browser's ordinary compositor like everything else on the page.
- **Height-capping so sixel rows never scroll / cell-grid budget arithmetic**
  (`crop_rows`, `CROP_BOX_HEIGHT_PX`, `ELEMENT_CROP_COLS`/`ROWS`, the
  half-block `fit_cells` 2-pixels-per-cell halving, `shell/tui/graphics.py:206-269`,
  `shell/tui/pixels.py:53-69`). These exist solely to fit a raster image into a
  discrete character-cell grid without breaking terminal scroll semantics. A
  GUI lays out images with ordinary CSS (fixed row height in px/rem,
  `object-fit: contain`, no cell quantization) and this machinery has no
  equivalent to port — just pick a comfortable fixed row height.
- **`half_block_text` / the whole `shell/tui/pixels.py` averaging module.** This is
  purely the "every terminal that cannot do sixel" fallback (U+2580 glyphs,
  box-averaging to a 4×4-sample grid). A GUI can always render real bitmaps,
  so this fallback tier does not exist and should not be ported in any form.
- **The upscale/Lanczos/NEAREST resampling policy in `fit_pixels`/`scale_crop`**
  (`shell/tui/graphics.py:222-308`) is tuned for *terminal cell* granularity (doubling
  tiny icons so they're not sub-pixel in a coarse grid, padding to an exact
  cell box so `textual-image` doesn't re-stretch it). A GUI can pick its own,
  simpler scaling policy (e.g., CSS `image-rendering: pixelated` below some
  size threshold, `object-fit: contain` otherwise) — the *intent* (don't blur
  a small icon, don't distort a crop's own aspect ratio) is worth keeping;
  the specific pixel-budget constants and cell-based padding math are not.

**What legitimately carries over unchanged** (repeated from §4.4 for clarity,
since it's easy to over-correct and discard this too): BGRX→RGB byte-order
handling, the fact that a crop is the matched-rectangle-only (not the whole
frame), the "absent from map = not searched" / "present+None = searched, not
found" / "present+crop = found" three-state contract, generation-based ghost
filtering, and the "identical bytes ⇒ skip repaint" memoization idea (though a
GUI may choose to just always repaint if that's cheap enough for its own
paint pipeline).

---

## Ambiguities / open questions for the GUI implementer

1. **`/identify` as a desktop overlay is inherently OS-native (tkinter,
   process-wide topmost translucent window).** A pywebview-based frontend
   cannot trivially reproduce a *system-wide*, click-through-dismissible,
   translucent overlay above **all** windows including non-AgentClip ones
   using only web technology — pywebview itself is just one more window. The
   brief describes the *behavior* (§2.2, §3.2) precisely so an implementer
   can decide whether to keep shelling out to the existing `driver.screen.overlay`/
   `driver.screen.picker` child-process mechanism (verbatim reuse — the "core" layer
   this brief was told to treat as shared) or build a GUI-native equivalent;
   this was not resolved by reading the code and needs a product decision.
2. **Whether the mode-readout line (`crops · sixel` / `crops · half-block`)
   has any GUI equivalent at all.** §7 argues it should simply disappear
   (a GUI can always show real pixels), but if there's ever a degraded state
   a GUI *can* hit (e.g. capture permission denied, no crop available for
   some platform reason) an analogous "why is this blank" readout might still
   be warranted — left as a judgment call rather than specified, since no
   such failure mode exists in the code today.
3. **Push vs. poll to a GUI process.** The core's `ElementsMatched` is
   delivered via Textual's in-process message queue from a worker thread. A
   pywebview frontend will need some transport (websocket, IPC, polling) to
   get the same ticks across a process boundary if the GUI and core don't
   share a process — this brief does not prescribe one since it's an
   integration detail outside "behavior," but flagging it since every other
   brief in this series will hit the identical question.
