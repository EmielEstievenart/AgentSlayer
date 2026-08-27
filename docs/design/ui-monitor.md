# UI Monitor — the VM / brain split

> **Status: BUILT, and binding — every section, §9 included** (last phase
> landed 2026-08-25). This began as a plan: decisions settled in a design
> session on 2026-08-24 and grounded in a code investigation the same day
> (every `file:line` reference in this document was verified against commit
> `e8d3ff5`), with phases graduating into binding one at a time exactly as
> `remote-executor.md` did. They all have. **Each phase section carries its own
> "as built" note, and where a note and the plan text under it disagree, the
> note is the record.** Where each phase landed:
>
> - **§2.12, §6.0** (2026-08-24) — the default shell is the GUI, the TUI is
>   behind `--tui`, and every phase-0 row in §7 is applied.
> - **§6.1** — the Driver half on 2026-08-24 (`driver/monitor/` exists, the
>   controller consumes a `UIMonitor` instead of owning the poller); the SHELL
>   half followed in the same wave (`1931e05` for the GUI), so both shells build
>   one `LocalUIMonitor` and neither mentions `_BUSY_POLL_S`, `build_detector` or
>   `ScreenOps`. §3's interface listing is the shipped
>   `driver/monitor/protocol.py`.
> - **§6.2** (2026-08-24/25) — the inversion: one recipe per state under
>   `driver/automation/recipes/`, one pure `TRANSITIONS` table, one asyncio task
>   that pulls. `controller.py` went 3121 → 593 lines and every §4 invariant has
>   a test (`tests/driver/automation/test_invariants.py`). See its status note.
> - **§6.3** (2026-08-24) — `describe()`, and both shells read their label from
>   it.
> - **§6.4, §6.5** (2026-08-24) — the calibration window, then the RPC and the
>   split-mode brain: the wire, the standing server, the client,
>   `SwitchableMonitor`, `LoopState.DISCONNECTED` and `--monitor host:port`.
>   §5's auth question stays OPEN and is named in §6.5's note.
> - **§6.6** — its first bullet on 2026-08-24 (the Textual TUI is deleted,
>   `textual` is out of `pyproject.toml`, `--tui` is a stub that says so and
>   exits 2, and §8's "Textual removal timing" is closed) and its second on
>   2026-08-25 (the `SshHost` per-call path is deleted; `SshHost` is a
>   connection, not a `Host`).
> - **§9** (2026-08-25) — wave 2, whole, in four phases the same day it was
>   decided: **§9.0** the nomenclature and the package renames (`shell/chat`,
>   `shell/monitor_ui`, and a third package `shell/webview` the rename forced
>   into existence); **§9.1** the Monitor UI — `agentclip-monitor` opens a
>   window, the Serve band, the token, the region store; **§9.2** the Chat UI's
>   Monitor tab, `SshHost.open_tunnel` and `--monitor @name`; **§9.3** this
>   header, the ledger below, `ui-briefs/monitor-ui.md` and the user guide.
>   Each subsection has its own "as built" note.
>
> Every row in §7 is applied. Of §8's six points, **four are closed** — auth on
> the port and chat-region persistence by §9.1 (built, not merely decided), tick
> cadence at phase 5, Textual removal timing at phase 6. **Two stay open:
> Wayland, and what the manual fallback states mean in split mode.**
>
> Where §9 renames things (§9.0), the new words apply to §9 and to the code it
> produced; **§1–§8 keep the vocabulary they shipped under** on purpose, so
> every anchor and every citation still resolves. The paragraph under §1's
> table is the old-word → new-word key that translates them.
>
> **Where this document overrides others** — say it here so no two docs
> disagree:
>
> - `architecture.md:49` ("three seams … deliberately not one object"): the
>   three seams survive, but the `ScreenOps` seam **deepens** into `UIMonitor`
>   (§3). `AutomationView` and `AutomationHost` are untouched.
> - `remote-executor.md` §2.3 ("the remote process dies with the connection"):
>   true for the engine, **deliberately false for the monitor server** (§2.6).
>   The monitor outlives the brain; the brain redials.
> - `gui.md:39-44` (parity policy, "the TUI keeps full parity for now"): the
>   TUI was **deprecated** at phase 0 (§6.0) and **deleted** at phase 6 (§6.6),
>   so there is no parity contract left — the ui-briefs bind the GUI alone.
>
> **For the implementing agent.** Read this whole document, then
> `architecture.md` §0 and `gui.md` §0–§1, before touching code. Work one
> phase per branch/session, in order (§6 says which phases may run in
> parallel). Each phase ends green (`uv run pytest`, `uv run ruff check .`,
> `uv run mypy src`), with this document's phase section amended "as built",
> and with the exe rebuilt via `.\scripts\build-exe.ps1` where the phase says
> so. AGENTS.md's workflow rules apply throughout: one coherent edit per
> commit, push after committing, Sonnet subagents for exploration, never run
> `real_os` tests without the user's explicit go-ahead.

## 1. Goal

Split the *screen automation* half of AgentClip across a process boundary,
the way `remote-executor.md` split the *engine* half:

| Element | Runs | Owns |
|---|---|---|
| **UI monitor** (VM) | on the machine whose browser shows the chat | pixels, template matching, debounce/streak counters, mouse, keyboard, clipboard, the calibration GUI |
| **Brain** (local GUI) | on the operator's machine | every recipe, every transition, the meaning of everything, the chat/session GUI |
| **Executor** (remote engine) | wherever `agentclip-engine` was launched | unchanged — `remote-executor.md` |

**The words in that table are wave 1's.** §9.0 renames them: **UI monitor** →
**Monitor** (with a **Monitor UI** of its own), **brain / local GUI** → **Chat
UI**, the chat app it drives → the **Browser**, and "GUI" stops being a term at
all. §1–§8 are left in the old vocabulary on purpose — they are the record of
what shipped — so read them through this row.

Drivers, in priority order:

1. **Readability.** `driver/automation/controller.py` is 3247 lines and one
   class (`AutomationController`, controller.py:278–3247) that owns the poller
   thread, the trackers *and* the state machine, with `_tick_lock` as the only
   seam between them. After phase 2 it is a dozen small files: one recipe per
   state, one pure transition table, one loop.
2. **The chat browser can live on a VM** while the operator's GUI stays local.
3. **The TUI stops costing double.** Every feature today lands twice
   (gui.md:39–44). It stops.

Explicitly *not* a driver: latency. Ticks are 0.5 s apart today
(`_BUSY_POLL_S`, tui `screens/main.py:319`, gui `view.py:116`); a LAN hop is
noise against that.

## 2. Decided

### 2.1 Queries are local reads; actions are round-trips

The monitor already polls continuously. It keeps doing that and **pushes a
tick** after every observation. Recipes never ask "is the copy icon visible?"
over the wire — they read the newest tick, or `await ui.observe()` to wait for
the *next* one:

```python
# recipes/auto_copy.py
await ui.focus_window()
await asyncio.sleep(0.5)
await ui.scroll(DOWN, 3)
tick = await ui.observe()          # waits for a tick captured AFTER the scroll
if not tick.visible(COPY):
    return Outcome.NO_COPY_ICON
await ui.click_element(COPY)
```

`observe()` returning a *fresh* tick (captured after the call was made, not
the newest cached one) is the whole point: it is the bug you would otherwise
hit right after a scroll. Three round-trips for the recipe above instead of
six, and the local-mode code is byte-identical.

### 2.2 The tick carries booleans, locations and counts — never pixels

The tick is what today's `DetectionSnapshot` (`driver/screen/detector.py:166`)
plus the controller's streak fields carry, and nothing more: per-kind seen /
not seen with the located region, the stale diff and its streak, the
debounced busy/idle/stale verdict inputs, the copy-changed streak, a
generation stamp, a sequence number, a monotonic timestamp. No crops, no
frames, no PNGs. The one wire-shaped thing `engine/link/wire.py` has no codec
for is binary data (wire.py:92–110 is scalar/dataclass/enum only) — this rule
means the monitor wire never needs one.

Anything pixel-shaped a human wants to *see* (the ELEMENTS column's crops,
`shell/chat/view.py:3189 _element_png`; the capture overlay; `/identify`) is a
**calibration** surface, and calibration runs where the pixels are (§2.5).

### 2.3 Policy stays local

`os_armed` (controller.py:519–575), the send gate, the reply gate, the
copy-arming decision, `LoopState` and every transition stay in the brain. The
monitor does not know what a state is. Concretely, today's
`click_profile_element` (controller.py:2221–2261) splits: `MISMATCH`,
`AMBIGUOUS`, `CLICKED`, `NOT_CLICKED` are pixel verdicts and move to the
monitor; `DISARMED` and `NOT_CALIBRATED` are refusals the recipe layer makes
*before* calling it.

### 2.4 Recipes and transitions

- One file per `LoopState` (`driver/automation/loop_state.py`, 8 states) under
  `driver/automation/recipes/`. A recipe is an `async def run(ctx) -> Outcome`
  written in plain `focus / wait / click / observe` — no network vocabulary.
- One pure table `TRANSITIONS: dict[tuple[LoopState, Outcome], LoopState]`.
  `LOOP_TRANSITIONS` (loop_state.py:45–67) already exists as a display-only
  legal-next table that "nothing reads back to make a decision"
  (controller.py:842–844). Phase 2 promotes it: the new table is authoritative
  and the old one is derived from it (or deleted).
- One loop, roughly eight lines: look up the recipe for the current state,
  run it, look up `(state, outcome)`, set the new state, repeat. It is
  **pre-emptible**: a shell can move the loop itself (`set_loop_state` — a
  session reset, a link dropping, a harvested reply being ingested), which drops
  whatever recipe is running and carries on from the state the shell put it in;
  the two stretches that are *driving the machine* (a paste, a harvest's ingest)
  are exempt and run to the end with their outcome thrown away instead.

### 2.5 Two machines stay two machines

Engine `Phase` (`engine/states.py`, 7 variants: where the *task* is) and
`LoopState` (where the *round trip through the browser* is) are not merged.
One function, `describe(phase, loop_state) -> str`, makes the GUI's label.

### 2.6 Calibration is a GUI that runs where the pixels are

The elements panel, service editor, click-point picker, capture overlay and
`/identify` become **one window** that talks to the **local** monitor object
directly — not to the controller, not over the wire. In local mode the chat
GUI opens it; in split mode it stands alone on the VM (`agentclip
--calibrate`). Same code, two entry points. The tkinter child-process overlay
(`driver/screen/picker.py`, `overlay.py`) is already subprocess-isolated and
shell-agnostic and is reused as-is.

### 2.7 Transport

TCP, JSON Lines, one object per `\n`-terminated UTF-8 line. Reuse from
`engine/link/wire.py`: `encode_line`/`decode_line` (wire.py:296–329), the
`hello`/`hello_ack` handshake shape with its hard `version` gate and
diagnostic `package` (wire.py:29–48, 1279–1332), the table-driven
`_PARAMS`/`_RESULTS` plumbing pattern (wire.py:1105–1230), and the
incremental-UTF-8 line reader from `executor/hosts/ssh.py:277–322`.

**Do not reuse** `shell/app/remote_link.py:RemoteLinkClient` or
`engine/link/server.py`. The engine client has **no background reader** — it
only reads frames while a call is in flight (remote_link.py:22–38;
remote-executor.md §2.9 "nothing is pushed"), so an unsolicited tick has
nowhere to land. The monitor client owns a reader task from day one; that is
exactly what the tick stream is for. The monitor protocol gets its **own**
wire version constant — do not couple it to the engine's `WIRE_VERSION = 1`.

### 2.8 The monitor server outlives the connection

Opposite of the engine (remote-executor.md §2.3, binding for the engine and
unchanged there). The monitor is a standing process on the VM: it hosts the
calibration window, keeps polling whether or not a brain is attached, and
accepts a redial. One brain at a time; a second connection is refused with an
error frame naming the first.

### 2.9 Link loss

The brain parks in a new `LoopState.DISCONNECTED`, the GUI says so, and on
reconnect the brain **re-derives from the screen**: trackers are rebuilt
fresh, streaks restart, the recipe for the state it was in when the link
dropped re-runs from its top. Nothing is buffered, nothing is replayed —
same honesty rule as remote-executor.md §2.3. The existing ghost-generation
discipline (controller.py:1220–1248, `retarget_detectors` 675–700) already
models "ticks from before this instant are dead"; a reconnect is a retarget.

### 2.10 What rides over at connect time

Everything `build_detector` (detector.py:456–543) takes as Python objects today
that is **not** pixels: the `ServicePreset` scalars (`config.py:209–313` —
`finish_signals`, `stable_seconds`, `tolerance`, `matcher`, `hover_scan`,
`scroll_action`, `snap_back`, `delivery`, `auto_submit`), the chat region per
slot, and the send-gate tick constants from `driver/automation/finish.py:66–92`
(`SEND_ARM_MIN_DIFF`, `SEND_ARM_TICKS`, `SEND_GATE_TIMEOUT_TICKS`,
`SEND_GATE_SEEN_TIMEOUT_TICKS`). `ServicePreset` is plain data — trivially
encodable in wire.py's existing dataclass style.

**Cadence moves to the monitor.** Today *both shells* convert
`stable_seconds → required_ticks` themselves (`main.py:3120`, `view.py:3367`)
against their own `_BUSY_POLL_S`. The brain ships raw seconds; the monitor
converts against its own tick rate. The shells lose the constant entirely.

`ServiceProfile` (the PNG template stacks, `driver/screen/profile.py:149–241`,
persisted by `profile_store.py`) **never crosses the wire**. It lives on the
monitor's machine, edited there by the calibration window. The brain refers to
a service by key; the monitor loads the profile for it.

### 2.11 Clipboard

The clipboard is a monitor resource. `read_clipboard` / `write_clipboard` are
monitor verbs (text crosses the wire; that is fine). The clipboard **watcher**
(controller.py:576–659) runs on the monitor and pushes a `clip` event with the
new text; the self-write tagging that stops the watcher echoing the monitor's
own writes (controller.py:2427ff, `self_writes`) stays on the monitor side of
the line. In local mode all of this is in-process and unchanged in behaviour.

### 2.12 The TUI is deprecated, then deleted

Phase 0 flips the default shell (plain `agentclip` launches the GUI; the TUI
sits behind `--tui`), amends gui.md's parity policy, and fixes the stale
claims listed in §7. Phase 6 deletes `shell/tui/` (10,989 lines; `shell/gui`
has zero imports from it — verified) and the Textual dependency.

## 3. The `UIMonitor` interface

Lives in a new package `src/agentclip/driver/monitor/`. Layering
(`tests/test_layering.py`): `driver/automation` → `driver/monitor` →
`driver/screen`, `driver/clip`. Nothing in `driver/monitor` imports
`driver/automation`.

As built in phase 1 (`driver/monitor/protocol.py`, §6.1) — this listing is the
file, not a sketch of it:

```python
class UIMonitor(Protocol):
    # -- lifecycle / configuration ---------------------------------------
    async def configure(self, spec: MonitorSpec) -> int: ...       # §2.10 payload; a retarget. Returns the new generation
    async def watched(self) -> Watched: ...                        # service key, box actually watched (spec's or store's), profiled-here? (§9.1)
    async def suspend(self) -> None: ...                           # stop polling (capture overlay is up)
    async def resume(self) -> None: ...
    async def close(self) -> None: ...                             # end every thread/task for good; idempotent
    # -- observation (local reads; no round trip) ------------------------
    @property
    def generation(self) -> int: ...
    @property
    def latest(self) -> Tick | None: ...                           # the newest non-ghost tick
    async def observe(self) -> Tick: ...                           # next tick captured after this call
    def subscribe(self, hook: TickHook) -> Callable[[], None]: ...   # the GUI's live detection panel; returns the unsubscribe
    def on_clip(self, hook: ClipHook) -> Callable[[], None]: ...     # clipboard watcher events
    # -- actions (round trips) -------------------------------------------
    async def focus_window(self, handle: int) -> bool: ...
    async def foreground_window(self) -> int | None: ...
    async def click(self, region: ScreenRegion, *, settle_s: float | None = None) -> bool: ...
    async def move_cursor(self, x: int, y: int) -> bool: ...
    async def scroll(self, region: ScreenRegion, detents: int) -> bool: ...
    async def scroll_key(self, key: str, taps: int = 1) -> bool: ...
    async def send_paste(self) -> bool: ...
    async def send_enter(self) -> bool: ...
    async def read_clipboard(self) -> str | None: ...
    async def write_clipboard(self, text: str) -> None: ...
    # -- the clipboard watcher: SYNC, alone among the verbs, because every
    #    caller is (the armed switch, a session starting, a session ending are
    #    all UI-thread acts that return a state the shell paints from) -------
    def watch_clipboard(self, on: bool) -> bool: ...                # is one polling now?
    @property
    def clipboard_kind(self) -> str | None: ...                    # provider name | "manual" | None
```

Two verbs §2.3 asks for are **not** here yet; they arrive in phase 2 with the
recipes that need them: `click_element(kind) -> ElementClick` (the
MISMATCH/AMBIGUOUS/CLICKED/NOT_CLICKED half of `click_profile_element`) and
`foreground_is_target()`, which shipped as the rawer `foreground_window() ->
int | None` because the delivery's activation poll is what needed it first.
Until phase 2 the controller does that pixel work itself, through the monitor's
local-only `ops` — see §6.1's note.

Two implementations, mirroring `shell/app/link.py:LocalLink` and
`remote_link.py:RemoteLink` (structural conformance pinned with a
`_conforms()` function the way remote_link.py:634–643 does):

- `LocalUIMonitor` — absorbs `ScreenOps` (`ops.py:90`), `ScreenDetector` +
  `build_detector`, `DetectorPoller` + `detector_loop` (controller.py:252,
  702–817), the trackers and their swap discipline (`reset_trackers`,
  controller.py:968–1011), the generation stamp / ghost filter, and the
  clipboard watcher thread. It also exposes a **local-only tier** the wire
  never carries. As built (§6.1) that tier is `ops`, `detector`,
  `capture(region) -> RegionImage`, `on_frame(hook)` (the ELEMENTS panel's
  crops, delivered beside the tick rather than inside it), the three trackers,
  `reset_trackers()`, `self_writes`, `spec`, `poller`, and
  `stamp(...)`/`feed(tick)` — the `tick_feed` test seam. `pick_region()`,
  `identify_overlay(...)` and `profile_for(key)` are still owed: they arrive
  with the calibration window (§2.6, §6.4). The controller names the subset it
  needs as its own `MonitorLike` Protocol at the call site, because a
  `RemoteUIMonitor` will never answer the local half.
- `RemoteUIMonitor` — a TCP client with a reader task; every action is one
  `call`/`result` round trip; `latest`/`observe`/`subscribe` are fed by pushed
  `tick` frames.

The `Tick` dataclass is frozen and is the **only** thing recipes may reason
about. Its exact field list is derived, not invented: enumerate what the five
`consume_*` methods (controller.py:925–1385) and `evaluate_finish`
(1653–1842) *read* from the detector and the streak fields, and carry those.
Phase 1 carried the detector half of that list (§6.1 names every field); the
streak fields `evaluate_finish` keeps are still the controller's and cross in
phase 2.

## 4. Invariants to preserve

These are load-bearing today and easy to lose in the control-flow inversion
of phase 2. Each gets a test that fails if it regresses.

1. **The fire is one-shot.** `evaluate_finish` sets `_flow_running = True`
   *before* calling `_on_fire()` and both happen inside the tick lock
   (controller.py:1653–1842). In the recipe shape: the `WAIT_GENERATE` recipe
   returns exactly one `Outcome` per fire, and the loop is a single asyncio
   task, so a second fire cannot start until the first recipe returns.
2. **Ghost ticks are dropped.** Every tick carries the generation it was
   captured under; a tick whose generation predates the last `configure()` is
   never delivered by `observe()` (controller.py:1220–1248).
3. **Trackers are swapped, not mutated.** `observe()` in the detector is
   read-streak → search → write-streak, non-atomically; an in-place reset
   racing a poll is undone by the poll's own write (controller.py:976–993).
   Reconfigure by building fresh trackers and swapping the reference.
4. **No lock across an await.** The monitor's tick lock covers bookkeeping,
   never `capture()` or `detector.observe()` (controller.py:97–113). Recipes
   hold no locks at all.
5. **`os_armed` gates every OS action, locally.** A disarmed brain never
   sends an action frame (§2.3).
6. **No blind paste.** Delivery requires a verified chatbox target
   (`verified_chatbox_target`, controller.py:1952–2427 region; ranged-edit
   wave). The `deliver` recipe keeps that check.
7. **Paint contract of `AutomationView`** (`view.py:13–22`: may be called from
   a non-event-loop thread, must not block) stays honoured until phase 6.
   After phase 2 the loop runs on the event-loop thread, so paints become
   same-thread — strictly safer, but the GUI's bridge queue still applies.
8. **Harness log parity.** The `harness_log` narration (controller.py:818–923)
   is the diff-check for phase 2: the same recorded scenario must produce the
   same log lines before and after, modulo lines the phase explicitly renames.

## 5. Security note for the monitor port

**Superseded by §9.1, which is built.** The monitor port *was* an
unauthenticated channel to a machine's mouse, keyboard and clipboard. What
survives from this section: v1 binds `127.0.0.1` by default and requires an
explicit `--bind` to listen elsewhere, and the intended deployment is a VM on a
private host-only network or a forward. What changed on 2026-08-25: `hello`
carries a token, a wrong or missing one is refused with `kind="unauthorized"`
before `hello_ack`, and the default is **token required even on loopback**,
waivable only by `--no-token` and only on loopback. `--bind` and the token are
orthogonal — `--bind` answers who can *reach* the port, the token answers who
may *use* it. §9.2 also turned the `-L` forward into a `direct-tcpip` channel on
the connection the app already has, so the manual `ssh -L` is now the fallback
rather than the recipe.

### 5.1 Which desktops the monitor can serve — **as built**

The monitor's whole job is pixels, mouse, keyboard and clipboard, so "runs on a
VM" is a claim about a *display server*, not about an OS. Two backends exist,
picked by `sys.platform` with nothing to configure:

| Platform | Capture | Input | Focus |
|---|---|---|---|
| Windows | GDI `BitBlt`/`GetDIBits` (`driver/screen/capture.py`) | `SendInput` (`driver/screen/focus.py`) | `SetForegroundWindow` + the ALT-tap loophole |
| Linux / **X11** | `XGetImage` (`driver/screen/x11.py`) | XTest `fake_input` | EWMH `_NET_ACTIVE_WINDOW` + `XRaiseWindow` |

`capture.py` and `focus.py` stay the Windows implementation and dispatch to
`x11.py` when the platform says linux; every other platform keeps the
documented `CaptureError`/`False`, so macOS is still "tell the user once". The
X11 module converts ZPixmap to the same BGRX `RegionImage` byte layout GDI
produces, which is what lets the detector, the matchers and the PNG encoder
stay platform-blind. `python-xlib` is a core dependency under a
`sys_platform == 'linux'` marker: the monitor is expected to run there with no
extras. Operator-facing details are in `docs/configuration.md`, "Running the
monitor on Linux".

**Native Wayland is not supported** and is an open point (§8): a Wayland client
cannot screenshot another client's surface or synthesise input into it, so
there is nothing to build the monitor on without going through portals. A
browser under **XWayland** is a normal X client and works.

## 6. Phases

Order is 0 → 1 → 2 → 3 → 4 → 5 → 6, except: **3 and 4 depend only on 1**, so
either may run in parallel with 2 if 2 stalls. Nothing depends on 3 except the
GUI label.

### 6.0 Deprecate the TUI, flip the default shell — **AS BUILT**

> **Status: built, binding (2026-08-24).** Plain `agentclip` opens the GUI;
> `agentclip --tui` opens the Textual shell; `--gui` is accepted and does
> nothing, kept for one release. Every phase-0 row in §7 is applied — AGENTS.md
> now names two shells and says a doc's status header qualifies the blanket
> "binding" claim, `gui.md`'s parity policy freezes the TUI and binds the
> ui-briefs to the GUI alone, `architecture.md`'s "TUI designer" framing is the
> GUI's, `README.md` cites `remote-executor.md` and lists every design doc,
> `docs/commands.md`'s keyboard reference is the GUI's own key table (it is
> rendered *inside* the GUI by the docs button), and `docs/configuration.md`
> describes the service editor by its real doors (the sidebar's
> **Edit services...** button and `F2`) with a forward pointer to §6.4's
> calibration window. The paragraphs below are the plan this section shipped.

Cheap, and a **prerequisite**, not hygiene: gui.md's parity policy makes a
GUI-only feature "a design smell [that] needs a written exception" — every
later phase violates a binding doc until this one amends it.

Code:
- `cli.py:781` branches on `args.gui`; the `else` at `cli.py:968` builds
  `AgentClipApp`. Invert: no flag → GUI; add `--tui`; keep `--gui` as an
  accepted no-op for one release.
- The exe smoke test in `scripts/build-exe.ps1` must still pass.

Docs (all of §7's phase-0 rows). Rebuild the exe at the end.

**Done when:** `agentclip` opens the GUI, `agentclip --tui` opens the TUI,
suite green, §7 phase-0 rows all amended, this section marked as built.

### 6.1 Extract `LocalUIMonitor` (local only, no RPC) — **AS BUILT (the Driver half)**

> **Status: built and binding for `driver/`; the shell half is NOT done
> (2026-08-24).** Four commits: `9dcfd12` (the package exists — `ScreenOps` and
> the delivery beats move under it, plus the `UIMonitor`/`Tick`/`MonitorSpec`
> contract), `6870f4d` (the §4.8 diff-check, recorded first so the refactor
> could be measured against it), `4a36959` (`LocalUIMonitor`) and `566a364`
> (the controller consumes one). What shipped, deviations included — the
> paragraphs under this note are the plan it shipped from:
>
> - **`Tick`** (`driver/monitor/protocol.py`) carries `seq`, `generation`,
>   `at`, `captured`, `busy`, `idle`, `stale`, `sightings` and
>   `active_detectors`, with `searched`/`present`/`visible`/`locate` over the
>   sightings map and `TICK_KINDS` beside it. `sightings` is the detector's
>   three-state map with the pixels taken out: a kind mapped to a
>   **`ScreenRegion`** (absolute screen coordinates) was found there, mapped to
>   `None` was searched and is not on screen, absent was not searched at all.
>   **Deviation from §2.2:** the streak counters are NOT on the tick. The
>   stale-arm streak and the copy-changed streak are still `AutomationController`
>   fields; they cross in phase 2, when `evaluate_finish` splits into a recipe.
> - **`MonitorSpec`** carries `service` (the profile KEY — the PNGs never
>   cross, §2.10), `region`, `finish_signals`, `stable_seconds` (raw seconds,
>   converted by the monitor), `tolerance`, `matcher`, `hover_scan`,
>   `scroll_action`, `snap_back`, `delivery`, `auto_submit`, and the four
>   send-gate budgets `send_arm_min_diff`, `send_arm_ticks`,
>   `send_gate_timeout_ticks`, `send_gate_seen_timeout_ticks`.
> - **The Protocol as actually shaped** — §3's listing was rewritten to match
>   it. Where it deviates from the sketch, the code is the more honest of the
>   two: `configure(spec)` returns the new `generation` (a caller that just
>   retargeted needs the stamp it retargeted to); `close()` was added, because
>   something has to end the threads for good and `suspend()` deliberately does
>   not; `focus_window(handle)` takes the window handle the shell recorded, the
>   monitor holding no idea of "the target window"; `click(region, *,
>   settle_s=None)` and `scroll(region, detents)` are told where, for the same
>   reason; `foreground_window() -> int | None` shipped in place of
>   `foreground_is_target()`, since the raw handle is what the delivery's
>   activation poll compares; `read_clipboard()` returns `str | None`;
>   `watch_clipboard(on) -> bool` and `clipboard_kind` are **sync**, alone among
>   the verbs, because every caller of them is a UI-thread act that paints from
>   the answer. **Deferred to phase 2:** `click_element(kind) -> ElementClick`
>   and `foreground_is_target()` — §2.3's pixel verdicts, which stay in the
>   controller for now (see below).
> - **The local-only tier** (§3) on `LocalUIMonitor`: `ops`, `detector`,
>   `capture(region)`, `on_frame(hook)`, `busy_tracker`/`idle_tracker`/
>   `stale_tracker`, `reset_trackers()`, `self_writes`, `spec`, `poller`, and
>   `stamp(...)`/`feed(tick)` — which is the `tick_feed` seam this section
>   asked for, in `feed_probe`'s place: there is no probe to inject any more,
>   so a suite stamps a whole tick and feeds it through the same ghost check
>   and the same subscribers. `POLL_SECONDS = 0.5` and `required_ticks()` live
>   here too. The controller declares the subset it needs as its own
>   `MonitorLike` Protocol at the call site (contract + local tier), because a
>   `RemoteUIMonitor` will never answer the second half; it shrinks in phase 2.
> - **`FakeUIMonitor`** (`driver/monitor/fake.py`) is the in-memory double the
>   whole automation suite now drives: `make_tick`/`feed`/`push_clip`/
>   `push_frame`, a `calls` log, an `answers` script, and every action
>   delegating to a real `ScreenOps` unless scripted — so the suites that
>   patched `ops` at their own module scope keep working. It lives in `src/`
>   for `driver/clip/fake.py`'s reason: two test packages drive the same seam.
>   The two suites whose subject moved —
>   `tests/driver/automation/test_detector_poller.py` and `test_tracker_reset.py`
>   — were deleted; what they asserted is `tests/driver/monitor/test_local.py`'s
>   now, where the poll thread is real and only the ops are faked.
> - **The delivery beats** live in `driver/monitor/beats.py` — cadence belongs
>   to the machine being driven (§2.10) — and `driver/automation/delivery.py`
>   re-exports them under the names its suites already reach for.
>   `driver/automation/ops.py` is now `ElementClick` and one re-export.
> - **Still pixel work in the controller, deliberately:** `find_all`,
>   `_chatbox_match`, `hover_scan_for_copy`, `verified_copy_click` and the
>   matching inside `auto_copy_flow` all reach `monitor.ops` directly this
>   phase (capture, `all_matches`, `lowest_match`, `move_cursor`). Those are
>   §2.3's pixel verdicts and they are **phase-2 work** — flagged here so
>   nothing reads "the controller consumes a UIMonitor" as "the controller has
>   stopped touching pixels".
> - **Layering** (`tests/test_layering.py`): `driver/automation` →
>   `driver/monitor` → `driver/screen`, `driver/clip` is pinned, and
>   `driver/monitor` may not import `driver/automation`. One carve-out was
>   needed: `agentclip.driver.automation.describe` — that ONE module, first
>   match wins — may import `agentclip.engine.states`, because §2.5's label is
>   a function of both machines. The Driver stays engine-free otherwise.
> - **The diff-check is a file**:
>   `tests/driver/automation/test_harness_log_scenarios.py` pins **39 literal
>   harness-log lines** over six recorded scenarios, generated at `c341a82`
>   (before any of this landed) and unchanged by the whole phase. §4.8 is
>   enforced, not intended.
> - **Docs**: `architecture.md`'s seams table has its fourth row (`UIMonitor`)
>   and the "deepens" note; its §0 layer text and diagram and its §1 module
>   tree name `driver/monitor`.
>
> **What the four commits above do NOT contain** — the shell half. As of
> `566a364` neither shell had been touched: `shell/chat/view.py` and
> `shell/tui/screens/main.py` still built `AutomationController(...)` with the
> pre-phase-1 keywords (`clipboard=`, `poll_interval_ms=`, and in the TUI also
> `ops=`) and passed no `monitor=`, so constructing either one raised
> `TypeError` and `tests/shell/` was red; both still held their own
> `_BUSY_POLL_S = 0.5`, still called `build_detector`, and still mirrored a
> poller in their own chrome — which is why a vestigial `DetectorPoller`
> survives in `controller.py`, and why `describe()` (§6.3) had no caller. This
> section's own "Done when" is that rewire: both shells construct one
> `LocalUIMonitor` and hand it over, neither mentions `_BUSY_POLL_S`,
> `build_detector` or `ScreenOps`, the vestigial `DetectorPoller` goes, the 39
> pinned harness lines are still identical, and the suite is green again. Until
> it lands, §2.10's "the shells lose the constant entirely" is true of
> `driver/` only.

Create `driver/monitor/` with `protocol.py` (`UIMonitor`, `Tick`,
`MonitorSpec`), `local.py` (`LocalUIMonitor`), and a `tick_feed` test seam
that replaces `feed_probe` (controller.py:1387).

Move into `LocalUIMonitor`: `DetectorPoller`, `detector_loop`,
`start/stop/retarget_detectors`, `is_ghost`, `reset_trackers`, the
`stable_seconds → ticks` conversion (from *both* shells), the send-gate tick
constants as `MonitorSpec` fields, `ScreenOps` (the class survives *inside*
the monitor as its OS adapter; nothing outside `driver/monitor` imports it),
and the clipboard watcher thread.

The controller keeps its shape this phase — the five `consume_*` methods
become a single `subscribe` hook that unpacks a `Tick` and calls the existing
bookkeeping. This is deliberately a *plumbing* phase: no decision moves.

Amend `architecture.md:49–55` (the seams table gets a fourth row and the
"deepens" note from this document's header). Extend `tests/test_layering.py`.

**Done when:** both shells construct one `LocalUIMonitor` and hand it to the
controller; neither shell mentions `_BUSY_POLL_S`, `build_detector` or
`ScreenOps`; the harness log for the existing controller test scenarios is
unchanged; suite green.

### 6.2 Split decide from do — recipes, transitions, loop — **AS BUILT**

> **Status: BUILT and binding (2026-08-24/25).** The inversion happened: the
> poller no longer pushes into a consumer, one asyncio task pulls
> (`tick = await ui.observe()`), and `AutomationController` went **3121 → 593
> lines**. The paragraphs under this note are the plan it shipped from; what
> follows is what shipped, deviations included.
>
> **The recipes** — `driver/automation/recipes/`, one `async def run(ctx) ->
> Outcome` per `LoopState`, and the map in `recipes/loop.py` is TOTAL over the
> enum (a state with no recipe is a state the loop parks in for ever):
>
> | Recipe | Lifted from | Outcomes it can return |
> |---|---|---|
> | `idle.py` | the outbound mailbox | `PAYLOAD_READY` |
> | `auto_insert.py` | `deliver` + the clipboard I/O block | `PASTED`, `NOT_PASTED` |
> | `manual_insert.py` | the banner's watch | `SEND_PROVEN`, `GENERATING` |
> | `wait_send.py` | the send-gate block | `SENT`, `GENERATING` |
> | `wait_generate.py` | `evaluate_finish`'s policy half | `FINISHED`, `NO_HARVEST` |
> | `auto_copy.py` | `auto_copy_flow` / `run_auto_copy_flow` | `HARVESTED`, `NOT_HARVESTED` |
> | `manual_copy.py` | the attention-alarm branch of `set_loop_state` | none — it parks |
> | `interpreting.py` | the turn, waiting on the mailbox | `PAYLOAD_READY` |
> | `disconnected.py` | §2.9, new at phase 5 | none — it parks |
>
> `outcomes.py` is the ten-member `Outcome` enum and the default sentence each
> move is narrated with; a recipe with something more specific to say overrides
> it for one return (`ctx.say`), which is how one `NOT_PASTED` tells four
> stories. Beside the nine recipes sit the pieces they share: `context.py`
> (`RecipeContext` — the window onto the controller plus the RUN's own state:
> the mailbox, the `ReplyWatch`, `flow_running`, the prose window, the pre-empt
> event), `acts.py`, `chatbox.py`, `park.py`, `reply.py`, `windows.py`.
>
> **The transitions** — `recipes/transitions.py` is the authority and it has
> **12 rows**, one per `(state, outcome)` pair any recipe can produce.
> `SHELL_EDGES` beside it names the three moves that are *not* a recipe's
> outcome (the user's own copy landing, a turn ending, a redial), and `_LINK_LOST`
> the one that happens TO every state. `LOOP_TRANSITIONS` is **derived** from
> those by `legal_next()` — imported at the bottom of `loop_state.py`, where
> `LoopState` is already bound — and the derived map is identical, key for key,
> to the hand-written one it replaced. Nothing reads it back to make a decision;
> it is the STATE rail's brightness and nothing else.
>
> **The loop** — `recipes/loop.py`, one task, owned by the controller and
> started and stopped there and nowhere else (`start_loop` / `stop_loop`). It is
> **pre-emptible** (§2.4): `set_loop_state` raises `ctx.preempt`, and the loop
> drops the recipe it is running and carries on from the state the shell put it
> in. The exception is a recipe inside `ctx.acting_on_the_machine()` — the
> delivery's click-settle-paste, the harvest's scroll-click-ingest — which is let
> run to the end and has its outcome thrown away instead: cancelling half a paste
> leaves a caret in somebody's chat box behind half a payload. A recipe that
> raises does not kill the loop and is not re-run either; it is logged and the
> loop waits for the shell.
>
> **What is left in `controller.py`** — the loop task, the slot pointers and
> their calibration, `os_armed`, the view/host/monitor wiring, and the narration.
> Four siblings took the rest: `armed.py` (`ArmedSwitch` — the arm and the
> clipboard watcher it takes away and gives back), `narration.py`
> (`LoopNarration` — the rail, the harness log and the attention alarm moved
> together or not at all, through one door that demands a reason), `readout.py`
> (the two paint functions) and `machine.py` (the `MonitorLike` Protocol the
> automation names at its own call site, plus the answers a controller nobody
> wired anything into gives).
>
> **`copy_armed` moved onto `ReplyWatch`** (`recipes/reply.py`), with the send
> gate and the detector verdicts. One object per outstanding reply, opened by the
> state that has one to wait for and dropped the moment the harvest starts —
> which is exactly the old `awaiting_pasted_reply` flag, spelled as the thing it
> was always guarding. `controller.reply is None` is how "no reply is
> outstanding" is now read, and a trigger armed for one reply cannot survive into
> the next.
>
> **The readout hooks stayed** (`monitor.subscribe` / `on_frame`), and they are
> the one thing still delivered on the monitor's own thread. They PAINT and
> decide nothing — which is precisely what the `AutomationView` contract has
> always allowed (§4.7) — so the loop is not woken to redraw three lines of
> DETECTION.
>
> **`on_fire` is gone.** The finish callback was the old push path's last
> survivor and it shipped for one release as a legacy notification; it is now
> deleted from the constructor, from `RecipeContext`, from the AUTO_COPY recipe
> and from the GUI (`_fire_auto_copy`). Entering `AUTO_COPY` *is* the fire, and
> because a transition cannot reach the rail without reaching the harness log,
> the log is what the suites count fires off (`conftest.fire_count`).
> `run_auto_copy_flow` survives as the bracket door for a caller that is not the
> loop, without the "a loop is alive, do nothing" shim the callback needed.
>
> **The GUI starts and stops the loop explicitly.** `GuiView.start` schedules
> `start_loop()` onto the loop thread (after the first `configure`, so the first
> recipe observes a monitor already pointed at a window) and `GuiView.close`
> cancels it before the monitor's threads go, so no recipe is left awaiting a
> tick nothing will push. It is deliberately not picked up implicitly by
> `forget_verdicts` any more: a shell that owns a loop owns starting it.
>
> **§4 is enforced, not intended** — `tests/driver/automation/test_invariants.py`
> is one test per invariant, three of them by reading the source (a behavioural
> test for "nobody took a lock" passes happily when somebody takes one and gets
> away with it):
>
> | Invariant | Test |
> |---|---|
> | §4.1 the fire is one-shot | `test_the_fire_is_one_shot_however_many_finished_ticks_arrive` |
> | §4.2 ghost ticks are dropped | `test_a_tick_from_a_dead_run_never_reaches_a_recipe` |
> | §4.3 trackers are swapped, not mutated | `test_a_recipe_reaches_the_monitor_only_through_the_wire_able_contract` (AST: every `ctx.monitor.X` a recipe names is on the wire-able contract, never the local-only tier) |
> | §4.4 no lock across an await | `test_the_brain_holds_no_lock_and_owns_no_tick_thread` (source: no `import threading`, no `Lock(`) |
> | §4.5 `os_armed` gates every OS action | `test_a_disarmed_brain_never_asks_the_machine_to_do_anything` |
> | §4.6 no blind paste | `test_nothing_is_pasted_into_a_chat_box_nobody_verified` |
> | §4.7 the paint contract | `test_the_brain_holds_no_lock_and_owns_no_tick_thread` (the structural half) + `test_every_paint_a_tick_causes_comes_from_the_loop_task` (which TASK painted, not merely which thread) |
> | §4.8 harness log parity | `test_the_recorded_scenarios_are_still_pinned_line_by_line` — it guards the guard: six scenarios and 39 literal lines, asserted by AST so nobody can relax a pin into a substring probe |
>
> One more beside them, and everything else stands on it:
> `test_every_outcome_a_recipe_can_return_has_a_row`, checked from both sides —
> every pair a recipe can produce has a row, and every row is reachable.
>
> **The one `threading` carve-out is `alerts.py`.** §4.4's test refuses the
> import everywhere in `driver/automation` except there, because a repeating
> "your move" tone is a sleep loop and the event loop may not sleep. It is a
> carve-out only while the alarm stays a LEAF: the same test asserts `alerts.py`
> imports nothing from `driver.automation`, so nothing on the tick path can reach
> the thread behind it.
>
> **Deviations worth naming:**
>
> - **`feed_probe` is a coroutine now.** The suites' one door onto a reading used
>   to be a push into the controller's consumer; there is no consumer, so it
>   waits until the loop is actually parked in an `observe()`, pushes the tick and
>   then gives the loop the turns it needs to fold it. Every suite that drives
>   readings is `async` for that reason — the reading and its consequence are two
>   different event-loop turns.
> - **`LOOP_TRANSITIONS` was promoted rather than deleted** — §2.4 allowed
>   either. Keeping the name meant the rail's one reader did not change at all.
> - **`disconnected.py` exists**, which the plan's file list does not name: §2.9's
>   state arrived with phase 5, before this phase landed, and it needs a recipe
>   like every other state.
>
> **Done when, checked:** every §4 invariant has a passing test; the 39 pinned
> harness lines are unchanged; `controller.py` is 593 lines; no `threading` import
> in `driver/automation` outside the `alerts.py` carve-out; suite green.

The real work. Today the poller thread *pushes*: `loop()` → `consume_*` →
`evaluate_finish` → `_on_fire()` synchronously in its own call stack
(controller.py:741–767, 1653–1842). After this phase a single asyncio task
*pulls*: `tick = await ui.observe()`.

Create `driver/automation/recipes/` — `idle.py`, `auto_insert.py`,
`manual_insert.py`, `wait_send.py`, `wait_generate.py`, `auto_copy.py`,
`manual_copy.py`, `interpreting.py` — plus `outcomes.py`, `transitions.py`,
`loop.py`. Sources to lift:

| Recipe | From |
|---|---|
| `wait_generate` | `evaluate_finish` (1653–1842) — its policy half; its streak half becomes `Tick` fields the monitor computes |
| `wait_send` | the send-gate block (1416–1652) |
| `auto_copy` | `auto_copy_flow` / `run_auto_copy_flow` (2908–3173) |
| `auto_insert` / `manual_insert` | `deliver` (2606–2810) and the clipboard I/O block (2427–2605) |
| `manual_copy` | the attention-alarm branch of `set_loop_state` (836–871) |

`AutomationController` shrinks to: slot pointers and calibration selection
(1844–1951), `os_armed`, the `AutomationView`/`AutomationHost` wiring, and
ownership of the loop task. Target: under 600 lines. Anything left that reads
a `Tick` to decide something belongs in a recipe.

**Done when:** every invariant in §4 has a passing test; the harness-log
diff-check passes on the recorded scenarios; `controller.py` < 600 lines; no
`threading` import outside `driver/monitor`; suite green.

### 6.3 `describe()` — **AS BUILT**

> **Status: built, binding (2026-08-24, `85904e1`).**
> `driver/automation/describe.py` is one function over two **total** tables:
> `PHASE_LABEL` (a word for every `Phase`) and `LOOP_LABEL` (a word for every
> `LoopState`, where `None` is the explicit precedence marker meaning "the
> phase says this better"). The rule is precedence, not prose: a loop state the
> user has to act on, or that is visibly moving on screen, outranks the phase;
> `IDLE` and `INTERPRETING` defer to it. Wording is inherited verbatim from the
> GUI's watch segment, glyph stripped — the glyph and its colour stay the
> shell's styling decision. `tests/driver/automation/test_describe.py` is the
> table test this section asked for: every `(Phase, LoopState)` pair, plus a
> totality check that fails the moment either enum grows a member with no words
> — which is where phase 5's `LoopState.DISCONNECTED` will first be noticed.
> Layering: this one module may import `agentclip.engine.states` (§6.1's note).
>
> **Deviation:** this commit is the FUNCTION only. At `85904e1` neither shell
> called it — `paint_loop_state` in `tui/screens/main.py` and `gui/view.py`
> still built its own label — so adopting it belongs to the shell rewire §6.1
> owes, and this section is not finished until both shells read their label
> from here.

`driver/automation/describe.py`, one function, `(Phase, LoopState) -> str`,
with a table test covering every pair the GUI can show. Both shells use it
for the state label (`paint_loop_state`, tui `main.py:1726`, gui
`view.py:3072`).

### 6.4 The calibration window — **AS BUILT**

> **Status: built and binding (2026-08-24).** Two parts. Part A built the window
> — `shell/monitor_ui/` (`window.py`, `view.py`, its own `assets/`), the
> `--calibrate` flag, and `tests/shell/monitor_ui/`. Part B emptied the
> chat GUI: the service editor, the `svc_*` bridge/runner/js_api families, the
> ELEMENTS column (`paint_elements` is a no-op that satisfies the port and no
> `crop_elements` is wired any more), the chat-region picker, `_picker_open`,
> `_refuse_second_picker` and `/identify`'s overlay are all gone from
> `view.py`/`bridge.py`/`runner.py`/`assets/`, together with their CSS, their
> markup and `tests/shell/chat/test_elements.py`. `service_editor.py` stays where
> it is: it is a MODEL with no window behind it, and the calibration package
> imports it.
>
> What the plan below did not say, and the two windows made true:
>
> - **One `webview.start()` per process.** The native pump runs on the main
>   thread and returns when the LAST window closes, so the two entry points are
>   not symmetric: `run_monitor_ui` owns the pump (standalone), and
>   `open_calibration_window(webview, runner)` only creates the window and wires
>   it, leaving the pump the chat shell is already running to pick it up.
> - **A borrowed loop.** The chat GUI passes `GuiRunner.schedule` as the
>   calibration runner's `schedule`, so both windows' coroutines run on the one
>   loop that shell already owns. A runner that borrowed a loop does not close
>   its own view on the way out — the chat GUI schedules `runner.view.close()`
>   itself from the window's `closed` event.
> - **A bridge and a `js_api` per window.** A bridge is a FIFO plus the one
>   thread allowed to call a particular window's `evaluate_js`, so sharing one
>   would serialise each window's paints behind the other's; pywebview binds
>   `js_api` per window, so neither page can call the other's methods.
> - **The suspend bracket is per VISIT, not per capture.** The plan said
>   `monitor.suspend()`/`resume()` "around any capture". What shipped brackets
>   the whole window: the chat GUI suspends its own detectors when the window
>   opens and resumes when it closes. Two readers of one screen is fine; a
>   fullscreen overlay over the browser those detectors watch is not, and there
>   is no moment inside that window when one cannot appear.
> - **One window at a time**, enforced in the chat GUI (`GuiView._calibration`):
>   a second `F2` toasts rather than opening a second window.
> - **Chat regions are still not persisted anywhere.** A box drawn in the
>   calibration window reaches the chat GUI's controller through
>   `on_calibration` and dies with the process, exactly as before; standalone
>   (`--calibrate`) there is nobody to hand it to at all. Flagged for phase 5,
>   which is where a monitor that outlives its brain has to answer the question.
>
> Entry points as built: `F2`, the titlebar's **calibrate** button and both
> sidebar doors (**Edit services...**, **Set chat region...**) in the chat GUI;
> `agentclip --calibrate` standalone. `/identify` opens the window rather than
> drawing an overlay from the chat one. F7 and the `elements` event are gone
> from the chat window entirely.


A second pywebview window — the GUI has exactly **one** `create_window`
today (`shell/chat/shell.py:276–285`), so this is new engineering, not a
refactor. Put it in `shell/monitor_ui/` with its own bridge object and
its own HTML/JS/CSS bundle (embedded in Python source per AGENTS.md).

Move in: `ServiceEditor` (`shell/chat/service_editor.py`, already composed and
callback-injected, view.py:2681–2688), the `svc_*` bridge methods
(`bridge.py:502–518, 715–802`), the elements panel (`view.py:3099–3204`), the
click-point picker (`view.py:2811`), `/identify` (`view.py:1755–1803`). The
`_picker_open` mutex and `suspend_detectors()` (view.py:2670–2680) become
`monitor.suspend()`/`resume()` around any capture.

The window is constructed over a **`LocalUIMonitor`**, never a remote one,
and never over the controller. Entry points: a titlebar button in the chat
GUI (local mode) and `agentclip --calibrate` (standalone). Rebuild the exe.

**Done when:** the chat GUI has no service-editor, elements or identify code
left in `view.py`/`bridge.py`; `agentclip --calibrate` runs with no engine
and no session; the ui-briefs `elements-panel.md` and `service-editor.md`
are amended to name the window; suite green.

### 6.5 The RPC — **AS BUILT**

> **Status: BUILT and binding**, 2026-08-24, with the gaps named below. What
> shipped:
>
> - **The wire** (`driver/monitor/wire.py`): JSON-lines frames `hello`,
>   `hello_ack`, `call`, `result`, `error`, `tick`, `clip`, with a version
>   constant of its own and a mismatch that names BOTH installs.
> - **The server** (`driver/monitor/server.py`, `driver/monitor/__main__.py`,
>   the `agentclip-monitor` console script and `packaging/agentclip-monitor.spec`):
>   a standing process that keeps polling across disconnects, accepts a redial,
>   and refuses a second brain by naming the first (§2.8). It starts
>   *unconfigured* — the `MonitorSpec` is the brain's payload and arrives on the
>   first `configure`.
> - **The client** (`driver/monitor/remote.py`): a reader task owned from day
>   one, `latest`/`generation` as local fields, one round trip per action
>   matched back by id alone, and link loss that fails everything in flight,
>   raises out of a parked `observe()` and fires `on_disconnect` once.
> - **The brain side** (`driver/monitor/switchable.py`, `shell/chat/view.py`,
>   `cli.py`): `LoopState.DISCONNECTED` and its transitions — every state may
>   reach it, and it leaves only to `IDLE`, because a reconnect re-derives from
>   the screen rather than resuming. The GUI builds its controller over a
>   `SwitchableMonitor` (inert until the first dial, swapped on every redial, so
>   neither the view nor the automation core is rebuilt for a link event), dials
>   after first paint, and on a drop parks in `DISCONNECTED` and redials on a
>   doubling backoff (1s → 10s cap) until the window closes. A redial that comes
>   back with a different `server_id` is reported as a monitor that restarted.
>   Calibration is refused in split mode and points at the monitor's machine.
>
> **Decided here, and now binding:** §8's *tick cadence* question — the reader
> task's backlog policy is **drop-to-latest**. `latest` is a field the newest
> tick overwrites and `observe()` only ever wants the newest, so nothing is
> queued and nothing is replayed; a WAN link that falls behind loses
> intermediate ticks, which is what a slower poll would have done anyway.
>
> **The entry is the flag, and only the flag.** `--monitor host:port`, GUI-only
> (with `--tui` it exits 2 with a one-line message). §6.5's original sketch also
> named a *GUI connect field*; that is deliberately **not** built — one entry is
> enough to run split mode, and a second one would have to answer what a
> mid-session retarget means to a live loop. If it is ever wanted it is a phase
> of its own.
>
> **Still owed / still open:**
>
> - **Auth on the port is still OPEN** (§5, §8). v1 binds `127.0.0.1` unless
>   `--bind` is typed, which is the consent, and the documented deployment is a
>   host-only network or an SSH `-L` forward. This is the one thing that keeps
>   §5 itself from being closed.
> - **Chat regions are still not persisted on the monitor** (§6.4's own note).
>   The brain sends a region in the spec and the monitor's calibration window
>   draws one locally; nothing over there keeps it across a restart, so a
>   restarted monitor is re-targeted by the brain's next `configure` and
>   re-calibrated by hand if the region itself was lost.
> - **`reset_trackers` is not a wire verb.** In split mode the brain's tracker
>   reset is a no-op (`switchable.py` says so out loud); the debounce keeps a
>   streak the caller's own paste produced for one poll longer. Closing it is a
>   wire change, not a shell one.

`driver/monitor/wire.py` (frames: `hello`, `hello_ack`, `call`, `result`,
`error`, `tick`, `clip`; own version constant), `driver/monitor/server.py`
(TCP listener, long-lived, one brain, §2.8), `driver/monitor/remote.py`
(`RemoteUIMonitor`, reader task, `observe()` resolves on the next pushed tick
with `seq` greater than the one current at call time), a console script
`agentclip-monitor` (`pyproject.toml [project.scripts]`, plus a PyInstaller
spec alongside `packaging/agentclip-engine.spec`), and a `--monitor
host:port` launch flag / GUI connect field. Add `LoopState.DISCONNECTED` and
its transitions (§2.9). *(The connect field was dropped — see the status
note.)*

**Done when:** a localhost e2e test runs the full recipe suite against
`RemoteUIMonitor` → real TCP → server → `LocalUIMonitor` with fake ops, and
the harness log is identical to the local-mode run; a kill-and-redial test
lands in `DISCONNECTED` and recovers; §5's bind default is enforced; suite
green; exe and monitor exe rebuilt.

### 6.6 Delete

> **Status: BUILT and binding — both halves.** The TUI half shipped 2026-08-24,
> the `SshHost` half 2026-08-25. What shipped, part A:
>
> - **`src/agentclip/shell/tui/` and `tests/shell/tui/` are gone** — 26 modules
>   and 44 Pilot suites, 31,064 lines, deleted whole. Nothing under `src/` imports `textual`,
>   and `tests/test_layering.py` has a test that says so about every module at
>   once, in place of the three rules that used to keep one shell's toolkit out
>   of the other's graph.
> - **`textual` and `textual-image` are out of `pyproject.toml`**, with
>   `pytest-textual-snapshot` and `textual-dev` out of the dev group;
>   `uv lock` refreshed. `packaging/agentclip.spec` lost its `collect_all`, its
>   pygments lexer sweep, its `textual_image` submodule walk and the
>   textual-dev/textual-serve excludes; `binaries` is empty and `datas` is
>   `gui_datas + doc_datas`. **`pillow` is deliberately left in** — nothing
>   imports it any more (it came in with `textual-image` for the sixel crops),
>   but dropping a runtime dependency is a separate call with its own build to
>   re-prove.
> - **`--tui` is a STUB, not a removal**, which is §8's open point answered the
>   conservative way: it parses, prints `the Textual TUI was removed in this
>   release; plain agentclip opens the GUI` on stderr and exits 2, above every
>   other branch in `main` so a script that carries it cannot be quietly handed
>   a different shell. `cli.TUI_REMOVED` is the sentence, pinned by a test.
>   `--gui` stays the no-op it already was.
> - **The shell fork in `cli.main` is gone**, and with it the launch-time SSH
>   dial: `--ssh` always defers into the window's connect dialog now, because
>   the TUI was the only caller that could not prompt after start-up.
>   `remote_launch` stays — it is still the single construction point that
>   sequence is built from, and `tests/test_launch_remote.py` still drives it
>   end to end over a fake host, through the `RemoteConnect` the shell is
>   handed rather than through a shell fork. `probe_terminal` and the sixel
>   verdict went with `tui/graphics.py`; nothing else asked them anything.
> - **The OS gate moved off the deleted shell.** `tests/conftest.py` used to
>   re-block `pick_region`/`draw_identify_overlay` at `tui/screens/main.py`'s
>   bound names; it now blocks them at `shell/monitor_ui/view.py` and
>   `shell/chat/service_editor.py` (where those names are from-imported today)
>   and additionally blanks the seven injecting verbs at
>   `driver/monitor/ops.py`, which is the adapter the whole automation suite
>   drives. `tests/test_os_gate.py` covers each new patch point.
> - **Prose that names the Textual shell was left where it explains a
>   decision** (`driver/automation/*.py`'s "lifted out of the Textual
>   MainScreen", `gui.md`'s comparisons): those sentences are the record of why
>   a seam is shaped the way it is, and deleting them would delete the reason.
>   What was removed is every dead *import*, flag path and packaging entry.

- `shell/tui/` in full; `textual` out of `pyproject.toml`; `tui.md` header
  becomes "historical, not binding"; the `--tui` flag prints a one-line
  "removed" message for one release, then goes.
- The `SshHost` per-call path, exactly as `remote-executor.md` §2.8 specifies
  (its increment 5).

**AS BUILT, part B (2026-08-25).** `ssh.py` lost 328 lines and gained 145:

- **Deleted:** `SshExec` (the `ExecHandle` over an exec channel — `wait`/`peek`/
  `kill`/`drain` and the connection-lost verdict), `wrap_command` (the
  `setsid`/pidfile kill-tree wrapper), `spawn`, `run_blocking`, `run_detached`,
  and the `Host` filesystem primitives `write_bytes`, `delete`, `mkdir`,
  `rmdir`, `lstat`, `listdir` (plus the `_mkdirs`/`_stat_mode` helpers). Kept
  exactly as the bullet said: connect/auth/reconnect, `open_link_channel`,
  `LinkChannel`, `_ChannelReader`, `_ChannelWriter`.
- **`SshHost` is not a `Host` any more** — it is the dialled **connection**, and
  `executor/hosts/connect.py` is its only real consumer. That is §2.8's own
  answer ("a connection/dialler object, not a Host"), and it made one type
  honest: `cli.Launch.host` / `GuiRuntime.host` are now `Host | SshHost`, which
  is the union they have always actually held. Both shells already read the
  slot through `getattr` (`target`, `connected`, `reconnects`, `reconnect`,
  `close`), so nothing else moved.
- **The connect sequence keeps working, on a much smaller surface.** §6.6's
  bullet named `ssh.py:757–864` — the whole SFTP block — but three of those
  primitives are what connect steps 4 and 6 ARE (`realpath`/`stat` check the
  remote root; `read_bytes` is how `load_config(host=…)` reads the target's
  `.agentclip.toml` and `permissions.json`, which remote-executor.md §2.12
  keeps as "what the shell still keeps locally"). Those three survive as
  read-only connect plumbing, which is what §2.8's prose asks for — "the SFTP
  side survives only as connect/auth plumbing … no file is pushed to the target
  at connect time". Likewise `run_blocking` was deleted but its two callers
  (`probe_os`, and `printenv` for step 5) are connect steps, so it came back
  half the size and under a name that cannot be mistaken for a tool path:
  **`probe_command`** — one bare `bash -lc` on its own channel, merged stderr,
  read to the exit status, no pidfile, no `setsid`, no handle, never raising.
- **The config layer stopped asking for a `Host`.** `load_config` only ever used
  `name` + `read_bytes`, so `hosts/base.py` carves those two out as
  `FileReader` and `Host` inherits it. That is what lets a connection satisfy
  the config read without pretending to be a machine tools run on.
- **`host=` is gone from `make_engine_factory` / `make_engine_builder` /
  `EngineBuilder`**, and with it `mcp_remote_target` (remote-executor.md §2.7
  said it goes in the same increment): an engine runs on the machine its process
  runs on, and `EngineBuilder` now just builds a `LocalHost`. `McpManager`'s own
  `remote_target` parameter is left in place, defaulted and now unreachable.
- **Tests.** The pin test `test_the_legacy_assembly_still_builds_a_whole_session_over_one_host`
  is deleted with its section header, and `tests/test_launch_remote.py` is
  purely the flip now. `test_ssh_host.py` lost the wrapper, `spawn`, peek/kill,
  the write/traverse primitives and the in-flight-command verdict (506 → 359
  lines) and gained three `probe_command` tests (login shell, dead link answers
  rather than raises, channel closed). `test_ssh_real.py` was rewritten around
  the connection and the connect probes. `test_link_channel.py` and
  `test_connect.py` needed only the `run_blocking` → `probe_command` rename in
  their fakes.

## 7. Document amendments

| Phase | File | Change |
|---|---|---|
| 0 | `AGENTS.md:3` | "a Python Textual TUI app" → two shells, GUI primary, TUI deprecated; add: a status header inside a design doc qualifies the blanket "binding" claim |
| 0 | `docs/design/gui.md:39–44` | Parity policy → TUI frozen; ui-briefs bind the GUI alone; drop the carve-out list's TUI-only rows |
| 0 | `docs/design/architecture.md:3, 978, 1030` | "TUI designer" framing → GUI |
| 0 | `README.md:61` | cite `remote-executor.md`, not the superseded `remote-ssh.md` |
| 0 | `README.md:103–108` | design-docs list is missing `gui.md`, `remote-executor.md`, `mcp.md`, `skills.md`, this file |
| 0 | `docs/commands.md:3, 45–69` | keyboard reference is TUI-labelled and is rendered *inside the GUI* by the docs button — replace with the GUI's keys |
| 0 | `docs/configuration.md:13, 109` | "F2 service editor" → the calibration window (once 4 lands, re-check) |
| 1 | `docs/design/architecture.md:49–55` | seams table + the "deepens" note — **applied**, along with §0's layer text/diagram and §1's module tree |
| 1 | `docs/design/gui.md`, `docs/design/tui.md` | **applied**: pointers where the monitor took `ScreenOps`, the delivery beats and the cadence out from under them |
| 4 | `docs/design/ui-briefs/elements-panel.md`, `service-editor.md` | name the calibration window as their host — **applied**, as a status note at the top of each |
| 5 | `docs/configuration.md`, `docs/commands.md` | `--monitor`, `--calibrate`, `agentclip-monitor` — **applied**, with §5's security note in both |
| 6 | `docs/design/tui.md`, `remote-ssh.md` | historical headers — **both applied** (`tui.md` 2026-08-24, `remote-ssh.md` 2026-08-25) |

Phase 6's `tui.md` row is **applied**: the document opens with a "HISTORICAL,
NOT BINDING — the TUI was deleted 2026-08-24" header that points a reader at the
layer each surviving rule lives in now. Three more went with it, unlisted above
because the row predates them: `research-textual.md` carries the same kind of
header, `gui.md` gained one saying there is one shell and that every "the TUI
does X" below it is history (its parity policy is now closed rather than
amended), and `AGENTS.md`, `README.md`, `docs/commands.md` and
`docs/configuration.md` name one shell and describe `--tui` as the stub it is.
The `remote-ssh.md` row belongs to §6.6's second bullet and is **applied**
(2026-08-25): the document opens with a "HISTORICAL, NOT BINDING" header that
names what survives in the code (the connect/auth/reconnect machinery, the link
channel, the connect sequence, decisions 1/4/6 and "the target owns its policy")
and what was deleted, and the four superseded markers that used to say "the
per-call machinery is still in the code" now say it is gone.

**Every row in this table is applied**, and every phase of §6 is as built.

## 8. Open points — **two left** (Wayland; the manual states in split mode)

Four of the six below are answered and struck through; they stay listed, with
the answer and where it landed, because a reader arriving at this section
deserves to find the resolution rather than a dangling question. **The only
points still open are Wayland and what the manual fallback states mean in split
mode.**

- ~~**Auth on the monitor port**~~ (§5). **CLOSED and BUILT by §9.1**
  (2026-08-25). The answer is the shared secret the
  handshake always had room for: `hello` carries a `token`, a wrong or missing
  one is refused with an `error` frame of `kind="unauthorized"`, the token is
  generated and shown by the Monitor UI, and the default is **token required —
  on loopback too**, waivable only by an explicit `--no-token` / checkbox that
  is itself refused off loopback. §5's bind default keeps its own meaning
  beside it: `--bind` answers who can reach the port, the token answers who may
  use it. §9.2 also turned the forward itself into a `direct-tcpip` channel on
  the connection the app already has, so the manual `ssh -L` phase 5 documented
  is now the fallback rather than the recipe.
- **Which clipboard the manual fallback states mean in split mode.**
  `MANUAL_INSERT` / `MANUAL_COPY` assume the operator can reach the browser's
  clipboard. On a VM that is the VM's clipboard — the brain's GUI can show the
  text to paste, but the operator pastes it *there*. Confirm the attention
  alarm and the GUI copy still make sense, or park manual states as
  unsupported in split mode. **Still open with phases 2 and 5 built**: both
  states have a recipe that parks and an alarm that nags, and neither knows
  which machine the human is sitting at.
- ~~**Chat regions are not persisted on the monitor**~~ (§6.4's note, restated by
  §6.5's). **CLOSED and BUILT by §9.1** (2026-08-25), as `regions.json` in the
  monitor's config dir. It lives in **the monitor's own store** — the monitor's config dir,
  keyed by service key — which is the half of the question that was undecided;
  the brain's config was the other candidate and it loses, because the machine
  that can see the box is the machine that should remember it. `MonitorSpec.region`
  becomes optional and the precedence is one line: a spec that names a region
  wins and is written to the store, a spec that omits one is served from it. So
  a restarted monitor keeps the box drawn on that machine, a standalone Monitor
  UI has somewhere to put one at all, and a Chat UI that knows better still
  overrides.
- ~~**Tick cadence on the wire.**~~ **RESOLVED** at phase 5: drop-to-latest.
  `latest` is a field the newest tick overwrites and `observe()` only ever
  wants the newest, so nothing is queued and nothing is replayed — a link that
  falls behind loses intermediate ticks, exactly as a slower poll would have.
- **Wayland.** The X11 backend (§5.1) covers X sessions and XWayland browsers;
  a native Wayland session has no equivalent, and every route to one
  (xdg-desktop-portal ScreenCast for pixels, libei/RemoteDesktop for input) is
  a per-session, user-approved handshake rather than a library call - which
  fights the monitor's "standing process, no operator on that machine" shape.
  Undecided whether that is worth building, or whether "log into an X11
  session" stays the answer.
- ~~**Textual removal timing.**~~ **RESOLVED** at phase 6 (2026-08-24), the
  conservative way: `--tui` survives one release as a **stub**. It parses,
  prints one line naming what happened and where the shell went, and exits 2 —
  so a script or a shortcut that still carries the flag is told, rather than
  meeting an argparse "unrecognized arguments" or, worse, silently getting a
  different shell. It goes for good in the release after this one.

## 9. Wave 2 — the monitor gets its own UI

> **Status: BUILT, and binding** (2026-08-25). Decided by the user that morning,
> after wave 1 (§6) shipped whole, and built the same day: §9.0 (the
> nomenclature and the package renames), §9.1 (the Monitor UI, the token, the
> region store), §9.2 (the Monitor tab and the SSH tunnel) and §9.3 (this
> document, the briefs and the user guide). Each subsection carries its own "as
> built" note naming where the code and the plan disagree — **read those notes
> before the plan text under them**, because where they disagree the note is the
> record and the plan is the intent.
>
> **What wave 1 left behind, and this wave picks up.** §6.5 shipped split mode
> with one entry (`--monitor host:port`) and no way to configure the far machine
> except by sitting at it and typing `agentclip --calibrate`; §5 and §8 left the
> port unauthenticated; §6.4 left a drawn chat region dying with the process.
> Those are three faces of the same gap: **the monitor is a real, standing,
> user-facing process and it has no user interface.** This wave gives it one,
> and renames everything the old vocabulary made confusing on the way.
>
> **Words used below** are §9.0's, not §1–§8's. The earlier sections are the
> record of a wave that shipped under the old vocabulary and are **not**
> retroactively rewritten — §1's table gains an old-word → new-word row and
> nothing else moves, so every anchor and every citation in the other design
> docs keeps resolving.

### 9.0 Nomenclature — three nouns, and "GUI" is not one of them — **AS BUILT** (2026-08-25)

> **Built, and binding.** The renames landed as written (`shell/gui` →
> `shell/chat`, `shell/gui/calibration` → `shell/monitor_ui`, the test trees
> mirrored). Four deviations, each one honest about what the plan got wrong:
>
> - **There is a THIRD package: `shell/webview/`.** The plan said "package
>   renames, and nothing else". It could not be, because the rename made
>   `monitor_ui` a *sibling* of `chat` and `tests/test_layering.py`'s
>   `test_monitor_ui_never_imports_chat_or_app` then forbids the reach the old
>   parent/child shape allowed. Everything the two windows are made of moved
>   down instead of sideways: `webview/bridge.py` (the FIFO, the drainer, the
>   `js_api` shim), `webview/service_editor.py` (the editor's MODEL — the plan
>   said it "moves with" `monitor_ui`, and it went one package further down
>   because the Chat UI still reads presets) and `webview/assets.py` (asset dir
>   + the `file://` entry URL). `shell/chat` keeps no `bridge.py` of its own.
>   The pywebview rule is therefore pinned over **three** package names, not
>   two (`WEBVIEW_PACKAGES`).
> - **`monitor_ui` may import one module of `driver/automation`.** The plan
>   listed the layering rules as "untouched"; the shipped allowance adds
>   `agentclip.driver.automation.finish` — and only that module — for the finish
>   vocabulary the ELEMENTS column labels its sightings with. Not the loop: this
>   window never drives an `AutomationController`. `shell/webview`'s allowance
>   stays narrower still (`config`, `driver/screen`, itself, `webview`).
> - **Identifiers were NOT chased into the corners.** "No module, test, spec
>   file or docstring says `shell.gui` or `calibration`" is not what shipped and
>   is not the bar. What survives on purpose: the **`gui` extra** in
>   `pyproject.toml` (it is the name of a pywebview dependency group, and both
>   windows need it), the **`[gui]` config table** (`theme` lives there — a
>   user's config file is not renamed by a refactor), the deprecated **`--gui`**
>   no-op flag, `run_gui` / `GuiRunner` / `GuiView` in `shell/chat`, and the
>   whole `Calibration*` family in `shell/monitor_ui` (`CalibrationView`,
>   `CalibrationBridge`, `CalibrationJsApi`, `CalibrationRunner`,
>   `CalibrationMonitor`, `open_calibration_window`). The vocabulary is a rule
>   about **prose and packages**, not a global search-and-replace over every
>   symbol; renaming those would churn call sites, tests and one config file for
>   no reader's benefit.
> - **`agentclip --calibrate` is a stub, not a removal**, and it landed in §9.2
>   rather than here. It prints
>   `agentclip: --calibrate was removed in this release; run agentclip-monitor
>   instead` to stderr and exits 2 — the same one-release courtesy `--tui` got
>   (§6.6), for the same reason: a script that still carries the flag is told,
>   rather than silently getting a different thing. It goes for good in the
>   release after this one.

**Goal.** One word per thing. "GUI" named the pywebview window when there was
exactly one; there are two now, on two machines, doing two unrelated jobs, and
every sentence that says "the GUI" has to be read twice to find out which. The
word is retired.

| Term | What it is | Where it runs | Binary |
|---|---|---|---|
| **Chat UI** | what the user looks at and types into: the transcript, the sidebar, the log pane, the run panel, the connect dialog. Holds the session, the recipes and every decision — what §1 called "the brain" | the operator's machine | `agentclip` |
| **Monitor** | watches the **Browser**, clicks it, types into it, owns the clipboard; polls, matches templates, keeps the streaks. Has a small **Monitor UI** of its own (§9.1) | the machine whose screen shows the browser — a VM, or the same PC in local mode | `agentclip-monitor` |
| **Browser** | the chat web page the Monitor operates — Claude, ChatGPT, whatever the service preset describes. Not ours, never automated through an API, only through pixels | with the Monitor, by definition | — |
| **Executor** | unchanged (`remote-executor.md`): permission-gated tools, files and commands on behalf of the agent | wherever it was launched | `agentclip-engine` |

"Monitor" is the process; "Monitor UI" is its window. Both are correct and they
are not interchangeable — `--headless` (§9.1) is a Monitor with no Monitor UI.
"Brain" survives only as informal prose about the split; it is not one of the
nouns, and where a sentence needs the noun it is **Chat UI**.

**Code:** package renames, and nothing else.

- `src/agentclip/shell/chat/` → `src/agentclip/shell/chat/`. Every module inside
  keeps its name (`view.py`, `bridge.py`, `runner.py`, `shell.py`, `remote.py`,
  `docs.py`, `assets/`).
- `src/agentclip/shell/monitor_ui/` → `src/agentclip/shell/monitor_ui/` — a
  **sibling** of `shell/chat`, not a child of it. It stops being "a second
  window the chat shell can open" and becomes the Monitor's own front end
  (§9.1), which is a different process on a different machine in the deployment
  this whole document exists for. `service_editor.py` moves with it: it is a
  model of a *service preset*, and services are configured where the pixels are.
- Tests mirror exactly: `tests/shell/chat/` → `tests/shell/chat/`,
  `tests/shell/monitor_ui/` → `tests/shell/monitor_ui/`.
- **Untouched, deliberately:** the layering *rules* (the same rules with the new
  names — pywebview may be imported by `shell/chat` and `shell/monitor_ui` and
  nowhere else; `driver/` may not import either); the `UIMonitor` / `Tick` /
  `MonitorSpec` contract; `driver/monitor/wire.py`; every `js_api` method name
  and every event name the pages already speak; the asset filenames; the
  ui-briefs' *content* (§9.3 re-hosts them, it does not rewrite their
  specifications).
- **`agentclip --calibrate` is removed.** It existed because the calibration
  surfaces needed a windowless machine to be opened from; `agentclip-monitor`
  **is** that window now (§9.1), so the standalone door is the monitor binary
  and there is exactly one of it. In local mode the Chat UI keeps its in-app
  doors (`F2`, the titlebar button, the two sidebar entries) opening the Monitor
  UI over its own in-process `LocalUIMonitor` — that is `open_calibration_window`
  under a new name and it does not change. What goes is the *second process*
  spelling, its `cli.py` flag, its `_calibrate` assembly and its help text.

**Docs:** none in this phase beyond docstrings and module headers — §9.3 does the
prose in one pass, once the shapes it describes exist.

**Done when:** no module, test, spec file or docstring under `src/` or `tests/`
says `shell.gui` or `calibration`; `packaging/agentclip.spec`'s asset collection
names the new package paths; `tests/test_layering.py` pins the two pywebview
importers by their new names; `agentclip --calibrate` is gone and no doc offers
it; `uv run pytest`, `ruff`, `mypy` green; all three exes rebuilt via
`.\scripts\build-exe.ps1`.

### 9.1 The Monitor UI — `agentclip-monitor` grows a window — **AS BUILT** (2026-08-25)

> **Built, and binding.** `agentclip-monitor` opens a window, the port is
> authenticated, and the chat region survives a restart. The brief for the
> window itself is `docs/design/ui-briefs/monitor-ui.md`; what follows is only
> where the code and the plan below disagree.
>
> **The store is two files in a folder, not one `monitor.json`.** The monitor's
> own config dir is `<platform config dir>/agentclip/monitor/` (`--config-dir`
> overrides it, for both halves at once), and it holds:
>
> - **`monitor-token`** — one line. `secrets.token_hex(16)` (32 hex characters),
>   not `token_urlsafe`: it is a string a human copies out of one window and
>   into another, and hex has no `-`/`_` to lose to a line break or a shell.
>   Written atomically (`mkstemp` → `fsync` → `chmod 0600` → `os.replace`), read
>   with `.strip()`, and re-minted silently if it is missing, empty or
>   unreadable. `load_or_create_token` never regenerates; `regenerate_token` is
>   the deliberate act.
> - **`regions.json`** — `{"version": 1, "regions": {<service key>: {left, top,
>   width, height}}}`. A second key in one JSON blob would have coupled the
>   token's write path to the region's, and the region is written from a
>   different surface at a different rate. One unreadable entry drops one
>   service, never the file.
>
> The precedence is §8's, unchanged and implemented in `LocalUIMonitor`: a
> `configure` spec that names a region wins and is written through; a spec with
> `region=None` is served from the store. `saved_region()` is a local-only read
> that never crosses the wire.
>
> **Amended 2026-08-27 — the brain has to be TOLD.** Serving the store's box
> to the poller was half the feature: the recipes that say "no chat window is
> drawn" read the brain's calibration, and a split-mode Chat UI has none, so a
> monitor whose ELEMENTS column saw everything sat under a `/new` that said
> nothing was visible. `watched()` is the verb that closes it — `Watched(service,
> region, profiled)`: the key the last `configure` named, the rectangle the
> monitor settled on, and whether the monitor's machine has a profile for that
> key — and the Chat UI's `_retarget_monitor` adopts a differing region into
> the live slot's calibration (`set_calibration`) and repaints the sidebar.
> Over the wire it is one round trip after each `configure`; the store itself
> still never crosses.
>
> `profiled` is the second half of the same trap. The brain sends ITS
> selected service key; the monitor resolves both the profile and the region
> store by that key on ITS disk. A Chat UI driving `claude` against a Monitor
> UI calibrated for `zai` therefore gets `NOT_CALIBRATED` on every click while
> the monitor's own ELEMENTS column sees everything. Now both sides say so:
> the Chat UI's DETECTION line (`MONITOR_UNPROFILED`) plus one toast naming
> the peer and the key; the Monitor UI's badge (`CONNECTED · peer · driving
> claude`) plus a mismatch line under the Serve band whenever the driven key
> differs from the window's own selection (`SERVICE_MISMATCH`).
>
> **The wire went to version 2.** `hello` gained `token` (always present,
> `null` when there is none) and that is a breaking frame change, so
> `MONITOR_WIRE_VERSION = 2` — deliberately its own number, not the engine's
> `WIRE_VERSION`. The refusal is an `error` frame with `kind="unauthorized"`
> (a new member of `ERROR_KINDS`) sent **before `hello_ack`**, so an
> unauthorised peer never learns the monitor's `server_id` or which clipboard
> backend that machine has. One sentence covers both "no token" and "wrong
> token", by design.
>
> **The defaults are as decided: token required, loopback included.** A
> first run mints one; the headless door prints it once on stderr. The
> refusals live in `MonitorServer.__init__` rather than in the CLI, so the
> Serve panel gets them for free: a non-loopback bind without `allow_remote`
> is `BindRefused`, and so is a non-loopback bind with `token=None`. The panel
> refuses the same pairing one layer up (`NO_TOKEN_OFF_LOOPBACK`) and disables
> the no-token checkbox whenever the *pending* dropdown row is not loopback.
> The headless door adds `--token TEXT` / `--token-file PATH` beside
> `--no-token`, mutually exclusive; the **window** door accepts those two and
> ignores them with a one-line notice — the Monitor UI serves with the token in
> its config dir, which the panel shows.
>
> **Serve panel, as built.** One `serve` event carries the whole panel state
> (`serving`, `status`, `address`, `port`, `interfaces`, `loopback`, `warning`,
> `error`, `no_token`, `token`, `token_path`) and four verbs go the other way:
> `serve_start(address, port, no_token)`, `serve_stop()`, `token_regenerate()`
> and `token_copy()`. `token_copy` is the one verb in either window that
> **returns a value** — the page needs the string to hand `navigator.clipboard`
> — so it is answered on pywebview's own thread off a plain attribute, never
> marshalled onto the loop. The status sentences are
> `not serving` (the plan said "stopped"),
> `listening on {address} — no Chat UI attached` and
> `listening on {address} — attached: {peer}`; a 1 s task pushes them and ends
> by *observing* that the server is gone rather than by being cancelled.
> Regenerating keeps the attached brain, exactly as planned — the live server's
> token is swapped in place, each session holds its own copy, and a toast says
> so.
>
> **The suspend bracket did not become "always suspended standalone".** The plan
> said a standalone Monitor UI could simply stay suspended. It cannot: the
> ELEMENTS column is the surface you calibrate *against*, and suspending the
> poller for the whole visit freezes exactly the thing you came to watch. What
> shipped is **per-capture** in this window — `svc_capture`, the region picker
> and `/identify` each bracket their own overlay — while the **Chat UI** keeps
> §6.4's per-visit bracket on its *own* monitor. Two monitors, two brackets,
> and the embedded case has both.
>
> **The header seeds itself from the store.** On start, on a slot switch and on
> a config adoption, the view fills an unset region from `saved_region()` and
> deliberately does **not** echo it back through `on_calibration` — otherwise
> the header would read "not set" over a monitor already polling that exact
> rectangle. Standalone passes `regions_dir`; the Chat UI's embedded window does
> not, so a region drawn from `F2` is the session's, as it always was.
>
> **`run_calibration` is gone**; the standalone entry is `run_monitor_ui`
> (§6.4's note is corrected in place). `open_calibration_window` is unchanged
> and still the embedded door.
>
> **Packaging.** `packaging/agentclip-monitor.spec` collects the Monitor UI's
> assets by package-relative path and names the per-platform pywebview backends
> in `hiddenimports`; `webview`/`pythonnet`/`clr` moved out of `excludes`. Both
> build scripts stopped skipping `gui` for `-MonitorOnly` / `--monitor-only`.
> There is deliberately **no `--gui-smoke` for the monitor exe**: that check is
> `cli.py:_gui_smoke`, and `cli` is off this binary's layering allowance — so
> what proves the frozen Monitor UI's pywebview collection is the app binary's
> own `--gui-smoke`, over the same `webview`, the same backend and the same
> environment. Giving the monitor one of its own means moving that function into
> a package both binaries may import; worth doing, and not in this phase.
>
> **The layering cost, named.** Making `monitor_ui` a sibling of `chat` is what
> forced `shell/webview/` into existence (§9.0's note) and what makes
> `agentclip.driver.automation.finish` an explicit, single-module allowance for
> this window. Neither was in the plan; both are the plan's own rule being
> enforced.

**Goal.** The Monitor becomes a thing you can sit in front of: it shows what it
is watching, lets you configure the service it watches, and lets you decide —
out loud, in a panel — who may drive it. This is where **auth** lands (§5, §8)
and where the **chat region** finally gets somewhere to live (§6.4, §8).

**Code.**

*Where the entry point goes.* `driver/monitor/` may not import pywebview: the
Driver is pure, and `tests/test_layering.py` says so about every module at once.
So the console script `agentclip-monitor` re-points from
`agentclip.driver.monitor.__main__:main` to
`agentclip.shell.monitor_ui.__main__:main`, which is a thin dispatcher:

- it parses with the **same** `build_arg_parser` (imported from
  `driver/monitor/__main__.py` — argument grammar is the Driver's, the window is
  not);
- `--headless` delegates to `driver.monitor.__main__.main(argv)` **verbatim** —
  the windowless server for a VM with no desktop, unchanged, still importing no
  toolkit, still `--port N [--bind A]`, still the thing the frozen-binary smoke
  tests drive;
- anything else builds the `LocalUIMonitor` (the existing `build_monitor`, which
  stays in the Driver) and opens the window over it.

`--port` becomes optional, and required only under `--headless`: with a window,
the port is a field in the Serve panel. `--version` and `--list-matchers` keep
working at the shell layer by delegating, so `scripts/build-exe.ps1`'s smoke
tests do not change shape.

*What the window hosts.* Two things, in one pywebview window built on
`shell/monitor_ui/window.py`'s existing shape (its own bridge, its own `js_api`,
its own assets, one `webview.start()` per process — every note in that module's
header still applies):

1. **The whole service configuration, exactly as the app had it before the
   split**: the service editor, the ELEMENTS column with its live crops, the
   chat-region picker, `/identify`'s overlay. This is today's
   `calibration/view.py` re-hosted with no behaviour change. The suspend bracket
   stays per-visit where the Chat UI embeds it, and is simply *always* the case
   standalone: a Monitor UI on the VM is the only thing on that screen that
   matters.
2. **A Serve panel**, new:
   - an **interface dropdown**: loopback first, then every address on every NIC,
     from `psutil.net_if_addrs()` — a **new core dependency**, imported lazily
     inside the function that fills the dropdown, with the dropdown degrading to
     loopback plus `0.0.0.0` if the import fails, so a freeze that lost it is
     not a broken window;
   - a **port field**, a **Start/Stop** button;
   - a **status line**, in these words: `listening on 192.168.1.40:7777 — no
     chat UI attached`, and once a brain dials, `attached: 192.168.1.7:51422`.
     Not listening reads `stopped`;
   - choosing anything that is not loopback shows the **unauthenticated-port
     warning** inline, in the sentence `--bind`'s help and `server.BindRefused`
     already carry, before Start is armed. Choosing it *is* the `--bind` opt-in
     §5 asks for, spelled as a click instead of a flag.

*Auth, decided.* The handshake has had room for a secret since §6.5 and has not
used it. It uses it now:

- The Serve panel shows a **token** — generated on first run
  (`secrets.token_urlsafe`), persisted in the monitor's own config dir
  (`platformdirs.user_config_dir("agentclip")/monitor.json`, mode `0600` where
  the platform has modes), with a **copy** button and a **Regenerate** button.
  Regenerating does **not** drop an attached brain: the token gates `hello`, and
  a connection that already shook hands was already authorised. It changes what
  the *next* `hello` must carry, and the panel says so.
- `hello` gains a `token` field. The server compares it in constant time and, on
  a wrong or missing one, answers a single `error` frame with a new
  `kind="unauthorized"` (a new member of `ERROR_KINDS`, beside the second-brain
  refusal) and closes. The message never says whether a token was configured,
  which one was expected, or which half was wrong — it says
  `the monitor refused this connection: bad or missing token`. The Chat UI turns
  that into a form error on the token field (§9.2), not a link-drop retry loop:
  redialling a wrong token forever is how you lock yourself out of noticing.
- **The default is "token required", on loopback too**, and that is the change
  from §5. Today's default treats loopback as consent — which is a claim about
  who else is on the machine, and it is false on exactly the machine this design
  targets. A VM whose whole job is to run a browser is a machine with a browser
  on it: any local process, any extension, anything a page can talk to gets the
  mouse, the keyboard and the clipboard of the operator's chat session for the
  cost of one TCP connect. A token costs one copy and one paste, once, and
  deletes that class of mistake entirely.
- The escape hatch is explicit and loopback-only: `--no-token` on the command
  line, or an **allow unauthenticated loopback connections** checkbox in the
  Serve panel that is disabled whenever the selected interface is not loopback.
  `--no-token` together with a non-loopback `--bind` is **refused** (exit 2, one
  sentence): the two opt-ins compose to "anyone on this network may drive this
  desktop", and that is not a thing a command line gets to say by accident.
- This **closes §5 and §8's auth point.** `--bind` keeps its meaning and is
  orthogonal: `--bind` answers *who can reach the port*, the token answers *who
  may use it*.

*The chat region, persisted.* A second key in the same config file
(`monitor.json`'s `regions` table, keyed by **service key**) holds the region the
Monitor UI last drew for that service. `MonitorSpec.region` becomes optional on
the wire, and the rule is one line: **a spec that names a region wins and is
written to the store; a spec that omits one is served from the store.** So a
monitor restarted on the VM keeps the box somebody drew over there, a standalone
Monitor UI has somewhere to put one at all, and a Chat UI that knows better
still overrides. No new frame, no new round trip. This **closes §8's chat-region
point** and §6.4's own note.

*Packaging.* `packaging/agentclip-monitor.spec` gains the Monitor UI's assets by
package-relative path (the Chat UI's spec is the precedent) and the monitor
binary now needs the `gui` extra: `scripts/build-exe.ps1`'s `-MonitorOnly` stops
skipping `gui`, and `build-exe.sh` matches. The headless path still imports no
toolkit, which is what keeps `--headless` honest on a server with no desktop.

**Docs:** deferred to §9.3, except `--headless`, the token and the Serve panel
appearing in `agentclip-monitor --help`.

**Done when:** `agentclip-monitor` with no `--port` opens a window; every service
configuration surface works in it exactly as it did in the calibration window;
the Serve panel lists real NIC addresses, starts and stops the server, and shows
both status sentences; a brain with the right token attaches and one with a
wrong or missing token gets `kind="unauthorized"` and no session; `--no-token`
works on loopback and is refused off it; `agentclip-monitor --headless --port N`
serves with pywebview uninstalled; a region drawn in the window survives a
monitor restart and a `configure` that omits one; suite green; all three exes
rebuilt.

### 9.2 Connect a Monitor from the Chat UI — **AS BUILT** (2026-08-25)

> **Built, and binding.** Four deviations from the plan above, each because the
> code that landed for §9.1 settled the question differently:
>
> - **The tunnel is a `Tunnel`, not a reader/writer pair.** The plan proposed
>   giving `RemoteUIMonitor` a reader/writer seam with `connect_tcp` as the
>   convenience. `SshHost.open_tunnel(dest_host, dest_port)` shipped instead: it
>   opens the `direct-tcpip` channel eagerly and pumps it to a loopback listener
>   this process owns, so the dial is the unchanged
>   `RemoteUIMonitor.connect(tunnel.local_host, tunnel.local_port, token=…)`.
>   The monitor client gained nothing at all, which is better than the seam the
>   plan asked for. Eager matters: "nothing is listening over there" comes back
>   as the tunnel's failure, on the form, rather than as a handshake that hangs
>   up two layers later.
> - **`[monitor.<name>]` has `via`, not `mode` + `ssh`.** One optional key
>   instead of two coupled ones: a target with `via` is a Via-SSH target, and a
>   `mode` that could disagree with the presence of an `ssh` name is a state the
>   file can no longer be in. `host` defaults to `127.0.0.1` when `via` is set.
> - **An unconnected SSH target is refused with a hint, not connected.** If the
>   Via-SSH target is not the machine the Executor is on, the tab says
>   `connect the Executor to <name> first - the Monitor tab rides that same
>   connection`. Running the SSH sequence from here would end the user's session
>   (one session, one host) from behind a button that says "attach a monitor" —
>   which is the hidden multi-step action ssh-connect.md §3.2 refuses to be.
> - **The `SwitchableMonitor` is now the handle in EVERY mode**, local included,
>   with the `LocalUIMonitor` as its first inner. That is what makes a
>   mid-session dial a swap rather than a rebuild — and what makes the way back
>   (`monitor_disconnect`) the same swap in the other direction.
>
> **Also landed here:** `--monitor @name`, `--monitor-token`,
> `AGENTCLIP_MONITOR_TOKEN` (flag, then env, then the saved table), and
> `--calibrate`'s removal — a one-line stub naming `agentclip-monitor`, kept for
> one release exactly as `--tui` is. `open_calibration` is refused whenever a
> remote monitor is attached, from the flag or from the tab, and opens again on
> disconnect.

**Goal.** Stop making split mode a launch-time flag. §6.5 refused an in-app
connect field on the grounds that "a second one would have to answer what a
mid-session retarget means to a live loop"; this phase answers it and builds the
field.

**The answer:** a mid-session dial is a **link event, not a new session.** It
parks the loop in `DISCONNECTED`, swaps the `SwitchableMonitor`'s inner monitor
— which is exactly what a redial already does — and re-derives from the screen
(§2.9). Nothing is buffered, nothing is replayed, and dialling a *different*
monitor is indistinguishable from the old one having been restarted somewhere
else. The session, the transcript and the engine are untouched: they are the
Executor's half and the Monitor knows nothing about them.

**Code.**

*The dialog.* A **Monitor** tab on the existing connect dialog, beside the SSH
one. `shell/chat/remote.py` is the precedent and the pattern: the tab is a
**model** with no window in it — which targets are offered, what the form must
say before Connect is armed, which failure lands where, what the three ways out
of a failure do — and `view.py` keeps only running the coroutine and drawing.
`docs/design/ui-briefs/ssh-connect.md` is the brief its shape follows.

Two modes:

- **Direct** — host, port, token. Dials `RemoteUIMonitor` at that address. The
  failures worth spelling: nothing listening, a wrong token
  (`kind="unauthorized"`, shown on the token field), a second brain already
  attached (the server's existing refusal, which names the peer — show it
  verbatim), and a wire-version mismatch (which already names both installs).
- **Via SSH** — pick a **saved SSH target**, give the remote port and the token.
  The Chat UI dials (or reuses) the paramiko connection the SSH tab already
  builds, opens a **`direct-tcpip`** channel on it to `127.0.0.1:<remote port>`,
  and hands that channel's reader/writer to `RemoteUIMonitor`. **No external
  `ssh -L`, no second login, no second password prompt, no second host-key
  question.** This is the deployment §5 always documented, finally spelled as a
  button.

  Precedent, and why this is small: `SshHost.open_link_channel` already turns a
  paramiko channel into the reader/writer pair a wire client consumes
  (`executor/hosts/ssh.py:LinkChannel`, `_ChannelReader`, `_ChannelWriter`), and
  `RemoteLinkClient` already consumes exactly that. The change is
  `transport.open_channel("direct-tcpip", ...)` in place of `open_session()`, a
  sibling method on `SshHost`, and one seam adjustment: **`RemoteUIMonitor` takes
  a reader/writer pair**, with `connect_tcp(host, port)` as the convenience that
  opens a socket and calls it. That mirrors `RemoteLink`/`LinkChannel` exactly.

  A token is still required over the tunnel. SSH proves who reached the port; it
  does not prove which of the several things on that VM did.

*Saved monitor targets*, like saved SSH targets. `[monitor.<name>]` tables in
the **global** `config.toml` only — never the project's, for `[remote.<name>]`'s
reason and ssh-connect.md §6.1's: a monitor target is a fact about how *this PC*
finds a machine, not a property of the project. Fields: `host`, `port`, `mode`
(`direct` | `ssh`), `ssh` (the saved SSH target's name, for `mode = "ssh"`), and
`token`. The token in a config file is stated plainly rather than hidden, and
`AGENTCLIP_MONITOR_TOKEN` overrides it for anyone who will not keep a secret in
one. The dialog offers "save this monitor as…" on a successful connect, writing
through the same `tomli_w` path the service editor already uses.

*The scriptable path stays.* `--monitor HOST:PORT` is unchanged — its
right-partition parse and its pinned `MONITOR_BAD_TARGET` sentence keep working,
including for `[::1]:7777` — and gains `--monitor @name` for a saved target. The
token does **not** ride in the target string: it comes from the saved target,
from `AGENTCLIP_MONITOR_TOKEN`, or from `--monitor-token TOKEN`, documented last
of the three because `argv` is world-readable on the machines this runs on.

**Docs:** deferred to §9.3.

**Done when:** the Chat UI connects to a monitor from the dialog with no restart,
in both modes; a Via-SSH connect opens no second authentication and no external
process; a wrong token shows on the field rather than starting a retry loop; a
mid-session dial parks in `DISCONNECTED` and comes back re-derived, with the
session intact across it; saved `[monitor.<name>]` targets round-trip; a
localhost e2e test drives a full recipe run over a `direct-tcpip` channel to a
fake SSH transport; suite green; exes rebuilt.

### 9.3 Docs & briefs — **AS BUILT** (2026-08-25)

> **Built, and binding.** Every row of the plan below is applied, plus the
> corrections the pass turned up. What landed:
>
> - **`docs/design/ui-briefs/monitor-ui.md` is written.** It specifies the
>   window: the header and its region line, the SERVE band control by control
>   with the verb each one calls, the three status sentences verbatim, the token
>   row and its regenerate semantics, the two files the Monitor keeps, the
>   off-loopback warning, the two refusals, the command line, and the
>   standalone-vs-embedded table. It carries two things the plan did not ask for
>   and the code made true: **the Serve band's absence in the embedded window is
>   decided by the absence of an event, not by a flag**, and the suspend bracket
>   is **per capture** here rather than per visit (§9.1's note). Two rough edges
>   are recorded as rough edges rather than dressed up: a non-numeric port is a
>   silent no-op, and two js_api verbs are declared and never called.
> - **`service-editor.md` and `elements-panel.md`** were already re-hosted onto
>   `shell/monitor_ui/` (and, for the editor's model,
>   `shell/webview/service_editor.py`) by the rename commit. What this pass
>   fixed is the *door*: both still offered `agentclip --calibrate` as the
>   standalone entry. It is `agentclip-monitor`, and the flag is a stub.
> - **`ssh-connect.md` gained §3a, the Monitor tab** — the two tabs and why the
>   dialog keeps them apart, the two modes, when Attach is armed and the four
>   validation sentences, the eager `direct-tcpip` tunnel, the refuse-with-hint
>   rule, where each failure lands, Disconnect, and `[monitor.<name>]`. Its
>   header gained the two-tab amendment. **§6's question 1 is answered: yes** —
>   `[monitor.<name>]` shipped exactly the proposed default, so it is precedent
>   for `[remote.<name>]` rather than a proposal, with two details the proposal
>   did not have (the offer is suppressed by **address**, not by name; the
>   forget control sits *beside* the row so a mis-aimed click cannot delete it).
> - **`README.md`** gained a three-binaries vocabulary table under the run
>   block, a **"Driving a browser on another machine"** section (the window, the
>   `--headless` door, the SERVE band and its token, the Monitor tab's two
>   modes, `--monitor @name`), and the corrections the monitor binary's new
>   shape forced: it **carries pywebview** now, `-MonitorOnly` skips only `mcp`,
>   and there is no `--gui-smoke` for it.
> - **`docs/commands.md`** gained `agentclip-monitor`'s own flag table
>   (`--headless`, `--port`, `--bind`, `--no-token`, `--token`/`--token-file`,
>   `--config-dir`) and the Monitor UI's one key (`Esc`). Its
>   **"the monitor port is unauthenticated"** paragraph was flatly false against
>   §9.1 and is replaced by the token model and the `--bind`-vs-token split.
> - **`docs/configuration.md`** gained the vocabulary up front, the token and
>   region files as rows in the merge-order table, the Serve panel, the region
>   store's precedence rule, and the same correction to the unauthenticated
>   claim. Its Linux section now says what a machine with no desktop does
>   (`--headless`, and calibrate elsewhere against the same config dir).
> - **`docs/design/architecture.md`**: §1's module tree now breaks out
>   `shell/chat/`, `shell/monitor_ui/` (`__main__.py`, `window.py`, `view.py`,
>   `serve.py`, `assets/`) and `shell/webview/` file by file, §0's vocabulary
>   row stopped offering `--calibrate`, and the seams table's
>   "the TUI posts a message; a GUI enqueues to its JS bridge" is now the Chat
>   UI's line alone, with the note that **only the Chat UI implements
>   `AutomationView`** — the Monitor UI has no loop to paint for.
> - **`AGENTS.md`**: the vocabulary row, and the build facts the monitor's new
>   shape changed (`gui` is synced for the monitor too; `-MonitorOnly` skips
>   `mcp` alone; `--gui-smoke` is the app's only, with the reason).
> - **`docs/design/gui.md`** already carries its "historical, not binding"
>   header and keeps its filename, for the reason the plan gives.
>
> **What was NOT done, deliberately.** The plan's "no doc under `docs/` uses
> 'GUI' as a noun for a surface" is not achievable by this pass and was not
> attempted: `gui.md`, `tui.md`, the four briefs with no status header and §1–§8
> of this document are **records of what shipped under the old vocabulary**, and
> rewriting them would break citations across every design doc for no reader's
> benefit (§9's own status note says so). The rule is enforced where it pays:
> the product surfaces (`commands.md`, `configuration.md`, `README.md`), the
> vocabulary tables, and every doc written from here on. The briefs that do
> carry a status header say in it that "GUI" below means the Chat UI.

**Goal.** One vocabulary everywhere, and a brief for the window §9.1 builds.
This phase writes no product code.

**Docs.**

- **`docs/configuration.md`** — the §9.0 vocabulary up front; `agentclip-monitor`
  as a window rather than a daemon; the Serve panel; the token, where it is
  stored and what "token required by default" means for a loopback split; the
  region store; `[monitor.<name>]`; and the "Running the monitor on Linux"
  section reworded around the new binary shape (X11 backend unchanged, §5.1).
- **`docs/commands.md`** — `--calibrate` deleted; `--monitor @name`,
  `--monitor-token` and `AGENTCLIP_MONITOR_TOKEN` added; `agentclip-monitor`'s
  own flags (`--headless`, `--port`, `--bind`, `--no-token`) documented as a
  table beside `agentclip`'s; the Monitor UI's keys. Note this file is rendered
  *inside* the Chat UI by the docs button, so it is a product surface, not a
  README.
- **`README.md`** — the three binaries by their new names and their one-line
  jobs; the design-doc list gains nothing but the vocabulary line.
- **`docs/design/architecture.md`** — §0's layer paragraph and its dependency
  diagram (`shell/chat`, `shell/monitor_ui`), §1's module tree, and the seams
  table's prose, which still says "a GUI enqueues to its JS bridge". The
  standing claim that `shell/` is "the user-facing surfaces … one behaviour"
  needs the honest amendment: there are two windows now and one of them is not a
  front end for the engine at all.
- **`docs/design/ui-briefs/monitor-ui.md` — NEW.** The window: its two halves
  (service configuration, Serve), the Serve panel's controls and its two status
  sentences, the token row and its regenerate semantics, the region store, the
  unauthenticated-port warning, and the one difference between the standalone
  and embedded lives of the same page (who owns the pump, who owns the loop, and
  the suspend bracket). Written as the other briefs are: it specifies the
  surface, it does not re-decide anything §9.1 settled.
- **`docs/design/ui-briefs/service-editor.md`, `elements-panel.md`** — re-hosted:
  their §6.4 status notes become "this surface lives in the **Monitor UI**", with
  the package path and the new brief cited. Their specifications are unchanged.
- **`docs/design/ui-briefs/ssh-connect.md`** — the Monitor tab (§9.2), and its §6
  open question 1 ("should a successful connection offer to save itself?")
  answered affirmatively for `[monitor.<name>]`, which settles the precedent for
  `[remote.<name>]` too.
- **`AGENTS.md`** — the vocabulary line in paragraph 1 (three binaries, four
  nouns, "GUI" retired), the `shell/chat/assets` path in the Dev-commands bullet,
  and the OS-gate bullet's patch points, which name
  `shell/monitor_ui/view.py` and `shell/chat/service_editor.py` today.
- **`docs/design/gui.md`** keeps its **filename** — it is already headed
  "historical, not binding", and renaming it would break every citation in this
  document and in `architecture.md` for no reader's benefit. Its header gains one
  sentence: the shell it describes is the **Chat UI**, and "GUI" is not a term
  any more.
- **This document** — §1's table gains an old-word → new-word row; nothing else
  above §9 is rewritten (see §9's status note).

**Done when:** no doc under `docs/` or `README.md`/`AGENTS.md` uses "GUI" as a
noun for a surface; `monitor-ui.md` exists and covers every control §9.1 built;
every path and flag named in the docs resolves against the code; the Chat UI's
docs button renders the amended `commands.md` without a broken anchor.

## 10. Wave 3 — the Chat UI never hosts a monitor (2026-08-27)

> **Status:** BUILT (2026-08-27). Every sub-section carries its own "as built"
> note below, exactly as §9 did. This supersedes §9.0's "in local mode the Chat
> UI keeps its in-app doors opening the Monitor UI over its own in-process
> `LocalUIMonitor`" — that embedded case is gone.

### 10.0 Why

Two bugs on 2026-08-27 came from one shape: the Chat UI had **two** ways to
reach a screen — an in-process `LocalUIMonitor` with an embedded calibration
window, and the wire — and the two disagreed about where the chat region and
the service key lived. Every "it works locally but not split" report is that
disagreement. The fix is to have **one** way: the Chat UI is a brain, and a
brain reaches pixels only over the wire (§2.9). "Local" becomes "a monitor
process this Chat UI launched on this machine", reached exactly like a remote
one — same dial, same token, same `watched()`, same Monitor UI window for
calibration.

### 10.1 The local monitor is a child process

* New module `shell/app/monitor_launch.py`:
  * `class LocalMonitorLauncher(Protocol)`: `start(project_root, *, global_config_path) -> LaunchedMonitor`, `stop()`, `alive() -> bool`, `exit_code() -> int | None`.
  * `LaunchedMonitor(target: MonitorTarget, process_id: int)`; the target is
    `MonitorTarget(name="local", host="127.0.0.1", port=<chosen>, token=<from the shared file>)`.
  * `SubprocessLauncher`: picks a free loopback port (bind-then-release), runs
    `agentclip-monitor --port N --bind 127.0.0.1 --project <root> [--global-config <path>]`
    **with its window** (no `--headless`) so calibration has a surface. Frozen:
    the sibling executable next to `sys.executable` (`agentclip-monitor`,
    `.exe` on Windows); checkout: `[sys.executable, "-m", "agentclip.shell.monitor_ui", ...]`.
    The token is `driver/monitor/auth.load_or_create_token(default_monitor_dir())` —
    the same file the child reads, so no token is ever passed on a command line.
    `stop()` terminates the child (and waits briefly); it is called from
    `GuiRunner.stop()` after the wire link is closed and before the loop ends.
    Nothing here polls the child: readiness is the dial itself (`_redial_loop`'s
    backoff already handles "not listening yet"), and a child that dies is a
    link that drops, which the existing DISCONNECTED path reports — with one
    extra sentence when `alive()` is False: `the local monitor exited (code N) - relaunch it from the Monitor tab`.
* `cli.py`: `--monitor` accepts `local` (default when the flag is absent),
  `none`, `HOST:PORT`, `@NAME`. `resolve_monitor_target` returns
  `MonitorTarget | LaunchLocal | None | str`, where `LaunchLocal` is a sentinel
  dataclass. `--pick-region` / `--show-identify` and `_list_matchers` leave
  `cli.py`: the Chat UI draws no overlay and runs no matcher.

**As built (2026-08-27, `2aa840e`).** All of the above, unchanged. One thing the
plan did not say and the code does: `SubprocessLauncher` takes its spawn, its
port picker and its token read as constructor seams, so the suite drives the
whole class — the command line, the port, the stop ladder — without a process, a
socket or a config directory.

### 10.2 The Chat UI, always over the wire

* `GuiView` no longer imports `driver.monitor.local`, `driver.screen`, or
  `shell.monitor_ui`. Its `SwitchableMonitor` starts idle; `start()` either
  launches the local monitor and dials it, dials the given target, or (`none`)
  parks the loop in DISCONNECTED with `no monitor attached - attach or launch one from the Monitor tab`.
* Gone: `_build_local_monitor`, `_local_monitor`, `open_calibration` and its
  runner/closed/`_calibrated` callbacks, `_open_calibration_window`,
  `OpenCalibration`, `ShellMonitor.detector`, `copy_seen_note`'s detector
  read, the `monitor=` constructor seam. Tests inject a `FakeUIMonitor` through
  a fake `dial` plus a fake launcher, never as an inner.
* `ShellMonitor` keeps `close()` and `watched()`; DETECTION's active-detector
  readout comes off `Tick.active_detectors` (it already rides every tick).
* F2, the titlebar **calibrate** button, **Edit services...**, **Set chat
  region...**, `/identify`: one door, one sentence —
  `calibration lives in the Monitor UI: the agentclip-monitor window (local), or that window on the monitor's machine (remote)` —
  and the button is relabelled **monitor UI**. No in-process window ever opens.
* Disconnect (Monitor tab) drops the link and stops a local child; the loop
  parks in DISCONNECTED, the badge goes red `NO MONITOR`. Nothing redials on
  its own after a deliberate disconnect.
* Monitor tab: a third mode **Local** (radio `local`) with one button
  **Launch a local monitor** — no host/port/token fields. Its saved-target
  story is unchanged for Direct / Via SSH.
* `_push_link` states: `none` (red, `NO MONITOR · attach or launch one`),
  `up` (green, `MONITOR CONNECTED · local` or `· <peer>`), `down` (red,
  `MONITOR DOWN · <peer> · <reason>`). `state="local"` is retired.

**As built (2026-08-27, `e10e434`, tests `44c8b83`).** Everything above landed.
The deviations and the things the plan left unsaid:

* **`monitor_target` became two fields**, not one. `_launch_local` (a bool) and
  `_monitor_target` (`MonitorTarget | None`), because they are two facts with
  two lifetimes: a local launch does not know WHERE to dial until the child has
  a port. A third, `_launching`, covers the moment between the two so the badge
  says `MONITOR DOWN · local` rather than `NO MONITOR` while a child comes up.
* **The first dial failures are quiet.** `_launch_local_monitor` parks the loop
  in DISCONNECTED with `starting a monitor on this PC - the link comes up when
  it is listening` BEFORE the spawn, which spends `_park_disconnected`'s
  once-per-outage toast on the sentence that is true. A child needs a moment to
  bind, and "cannot reach the monitor" on every launch would be noise.
* **`LOCAL_MONITOR_EXITED` is appended to the dial's own reason** rather than
  replacing it (`_dial_failure`), and it toasts once per dead child rather than
  once per outage — a link that was already down and whose child has now died is
  a NEW fact, and the one the user has to act on.
* **`_NoLauncher`** is the default for a view nobody wired a launcher into: it
  starts nothing and reports through the ordinary failed-launch path. Neither a
  real `SubprocessLauncher` (which would let a test spawn a monitor onto the
  developer's desktop) nor a raise at construction (which would make the seam
  mandatory for the dozen suites that never go near a monitor).
* **`copy_seen_note` returns `""`** rather than being deleted: `AutomationHost`
  still asks, and the auto-copy recipe's sentence simply ends earlier. §10.4.
* **The busy/idle DETECTION rows lost `PROBE_UNCAPTURED`.** That line took the
  service's finish CHECKLIST and the machine's captures, and both are the
  monitor's since §10.5 with no field on `Watched` between them. The STALE row
  keeps the half that matters: an empty `active_detectors` still says "nothing
  here will ever produce a verdict".

### 10.3 Build, layering, docs

* `packaging/agentclip.spec` drops `cv2`, `numpy`, `tkinter`, `Xlib`, and the
  `MONITOR_UI_ASSETS` block; `build-exe.ps1`/`.sh` stop running
  `--list-matchers` against the app binary (the monitor's own check stays).
* `tests/test_layering.py`: `shell.chat` may no longer import `driver.screen`,
  `driver.monitor.local`, or `shell.monitor_ui`; it leaves
  `CLIP_SCREEN_IMPORTERS`. `shell/monitor_ui/window.py`'s
  `open_calibration_window` / `CalibrationRunner` are deleted if nothing else
  calls them.
* Docs: `commands.md` (F2 row, `--monitor` values, "while a link is up"
  paragraph), `configuration.md` (§ the Monitor UI, the attached-monitor
  paragraph), AGENTS.md vocabulary row, `ui-briefs/monitor-ui.md` §6.1–6.3
  (the embedded case is gone), `elements-panel.md` / `service-editor.md`
  headers, README's local-mode sentence, architecture.md if it names the door.

**As built — packaging (2026-08-27, `446a3a8`) and docs (`9b65647`).** Both as
planned.

**As built — layering (2026-08-27, `6694e05`).** `shell.chat` lost
`agentclip.shell.monitor_ui` outright, and its two blanket allowances became
LISTS, which is a stronger rule than the plan asked for:

* `driver.monitor` → `protocol`, `remote`, `switchable`, `auth`. `local` is off
  it, and that one line is what §10.0 comes down to: there is no second way to
  reach a screen.
* `driver.screen` → `slot`, `region`, `capture`, `profile` (the value types the
  automation ports speak), plus `focus` and `profile_store`.

**The one unpaid bill.** `profile_store` is still reached, and it should not be.
`AutomationHost.profile_for` and the delegation readiness rules
(`can_delegate` / `missing`) want a per-KIND answer — "has this service a
captured copy button?" — and the monitor's only answer is `Watched.profiled`,
one boolean. So the Chat UI still reads THIS machine's profile store, which is
right for a local child and stale for a remote monitor. It is named explicitly
in `tests/test_layering.py` so that the day it moves, that line is what changes.
The obvious shape for the fix is already on the wire: `Tick.sightings` holds an
entry per kind SEARCHED, which is exactly "the monitor has a capture of it".

`shell.chat` stays in `CLIP_SCREEN_IMPORTERS`, because the clipboard PROVIDER is
still handed to it as a launch decision (`[clipboard] provider`) and read for
its name in the status bar and for the manual-mode ingest. `provider` therefore
stayed on `GuiView` / `GuiRunner` / `run_gui`.

`open_calibration_window` / `CalibrationRunner` were NOT deleted: `run_monitor_ui`
— the standalone `agentclip-monitor` door — still uses both. Their prose stopped
claiming the Chat UI is the other caller.

### 10.4 Open after this wave

* Presets edited in a local child's service editor reach the Chat UI only on
  its next config reload — there is no wire event for "config changed".
* `copy_seen_note` (the "poller last saw the copy button" line) is gone with
  the local tier; if it is missed, it comes back as a tick field.
* `AutomationHost.profile_for` still reads the Chat UI's own machine (§10.3's
  "unpaid bill"), so the sidebar's `appearance:` line and the delegation
  readiness rules describe THIS desktop rather than the monitor's.
* The sub-agent window's sidebar reads `no service - the monitor has not
  answered for this window` until a delegation makes that slot live, because
  `watch(slot)` is the retarget and selecting a tab is not one. Asking the
  monitor about a window it is not watching would need a read that does not
  retarget — a `watched(slot)`, which the wire does not have.

### 10.5 The service is the monitor's (added 2026-08-27, after the §9.1 mismatch)

**Rule.** The Chat UI never sends a service key, a preset or a spec. The
monitor process owns its service configuration entirely — which service each
window (MASTER / SUB-AGENT) is, its captures, its chat region, and every
preset field — and the brain reads back what it needs.

* `Watched` grows into the monitor's whole effective service: `service`,
  `label`, `region`, `profiled`, `generation`, and the preset fields the brain
  acts on — `delivery`, `auto_submit`, `scroll_action`, `snap_back`,
  `hover_scan`, `max_paste_chars`, `total_context_chars`,
  `wrap_blocks_in_fence`, `attachment_note`, `require_fenced_reply`,
  `extra_instructions`. `ServicePreset` stays the config-side record; the
  brain's `live_preset()` is built FROM `Watched` (a small adapter), never
  from the host's `[services.*]` tables.
* `configure(spec)` leaves the wire. In its place: `watch(slot: "master" |
  "subagent") -> Watched` — the monitor runs ITS OWN spec for that slot and
  bumps its generation. `LocalUIMonitor` gets a `spec_for: Callable[[AgentSlot],
  MonitorSpec]` from the monitor process (headless: straight from its config +
  region store; with a window: the Monitor UI view's `_spec()`, so a tab
  switch or a redraw there and a `watch()` from the brain drive the same
  state). `configure(spec)` remains the in-process door the Monitor UI and
  the tests use; a remote brain cannot call it.
* The brain calls `watch(live_slot)` where it called `configure(_live_spec())`
  (attach, redial, slot switch) and re-reads `watched()` whenever a tick
  arrives with a generation it has not seen — that is how a service picked or
  a region redrawn in the Monitor UI reaches the Chat UI without a new frame.
* The Chat UI's sidebar service picker becomes read-only: it shows the
  monitor's answer (`label (key) · from the monitor`) and its
  **Edit services...** door says the §10.2 sentence. `general.service` /
  `subagent_service` and `[services.*]` in the HOST config are no longer read
  by the Chat UI (they stay in `config.py` because a locally launched monitor
  reads the same file). The paste/context budgets, fences and instructions the
  session builder needs come from `Watched` too.
* Send-gate tick budgets (`SEND_ARM_*`, `SEND_GATE_*`) become the monitor's
  constants; the spec no longer carries them.

**As built — the MONITOR side (2026-08-27, `d5e6a58`…`d91f151`).** Everything
above that lives under `driver/monitor/` and `shell/monitor_ui/`.

* `Watched` is `service`, `region`, `profiled`, `label`, `generation` and the
  eleven preset fields, all defaulted after the first three so
  `EMPTY_WATCHED` is one object. `watched_from(spec, profiled=, generation=)`
  builds it; `spec_from_preset(preset, region, service=)` builds the spec.
* `MonitorSpec` grew the same preset half rather than the monitor taking a
  second `preset_for` callback: one callable then answers the whole of "what is
  this window", and two seams could have named two different services.
* `UIMonitor.watch(slot: AgentSlot) -> Watched` is on the Protocol;
  `configure(spec)` stays as the in-process door. `AgentSlot` is
  `driver/screen/slot.py`'s — a slot is a drawn box, which is below this layer,
  so no new enum was needed.
* Monitor wire **v3**, breaking: `configure` left `_PARAMS`/`_RESULTS`/`VERBS`
  and `watch` (param `slot`, a string) took its place; `encode_spec` /
  `decode_spec` are gone. `RemoteUIMonitor.configure` raises `MonitorCallError`
  carrying `CONFIGURE_IS_LOCAL`.
* `LocalUIMonitor(spec_for=…)` / `set_spec_for(…)`. Headless it comes from
  `driver/monitor/__main__.spec_for_config`; with a window the Monitor UI's
  view installs its own `spec_for` at construction, and a `watch` for the slot
  the window is not showing switches the window and repaints it.
* `SEND_ARM_*` / `SEND_GATE_*` now live in `driver/monitor/beats.py`;
  `driver/automation/finish.py` re-exports them at the address its suites
  already name. The Monitor UI imports no `driver/automation` module at all any
  more, and `tests/test_layering.py` dropped that allowance.

**As built — the BRAIN side (2026-08-27, `7175b5e` + `e10e434`).**

* `Watched` grew a twelfth preset field the plan's list missed: `edit_by_lines`.
  It decides a CATALOG — whether the bootstrap offers `replace_lines` and a
  numbered `read_file` — so a brain reading its own host's copy would build a
  turn for a service somebody else is running, which is what the rest of
  `Watched` exists to stop. `MonitorSpec`, `spec_from_preset`, `watched_from`
  and wire v3's `encode_watched` / `decode_watched` carry it (`7175b5e`).
* `preset_from_watched(watched, *, alerts)` is the adapter, in `chat/view.py`.
  It builds a real `ServicePreset`, so the recipes and the narration are
  untouched. Two kinds of field are deliberately not taken from `Watched`: the
  pixel knobs (`stable_seconds` / `tolerance` / `matcher` / `finish_signals`),
  which never leave the monitor and are left at their defaults because nothing
  above this line reads them; and `alert_sound` / `alert_repeat_seconds`, which
  come from `alerts` — the HOST's `config.preset()` — because the uh-oh alarm
  plays on the machine the user is sitting at.
* `GuiView._watched` is a `dict[AgentSlot, Watched]`, not one value: the sidebar
  describes the SELECTED window while the recipes drive the LIVE one, and the
  two part company for the whole of a delegation. `_adopt_watched(slot, w)` is
  the single writer; `_service_for`, `_preset_for`, `live_preset`, the tab
  labels, the sidebar line and `SessionSpec.service` all derive from it.
  `_initial_services`, `_service_options`, `set_service`, `_persist_services`
  and `save_active_services`'s call site are gone, along with the
  `AutomationController(services=…)` argument — `service_of` / `set_service`
  stay on the controller and this shell no longer calls them.
* The generation back-channel is `_on_monitor_tick`, a `subscribe` the view
  takes out on its own `SwitchableMonitor` at construction. It sets
  `active_detectors` from every tick (§10.2's replacement for the detector
  object) and, on a stamp it has not seen, schedules `_reread_watched` — which
  is `watched()`, never `watch()`, because a second retarget would bump the
  generation again and the two would chase each other. The stamp is claimed on
  the tick thread, so several ticks do not queue several reads.

## 11. Wave 4 — the brain knows no pixels; the GUI starts idle (2026-08-27)

> **Status:** PLANNED. Each sub-section gets an "as built" note when it lands.

### 11.0 Why

Three reports on the same day, one cause. With a green link and a Monitor UI
whose ELEMENTS column saw every button: `/new` answered "did not land
(not_calibrated)", the reply was never auto-copied, and the pasted prompt was
never submitted. Every one of those decisions was taken by the brain against a
**service profile read from the brain's own disk** (`shell/chat/view.py`'s
`profile_for` → `driver/screen/profile_store.load_profile`, the "unpaid bill" of
§10.3/§10.4). On any machine other than the one the appearances were captured
on that profile is empty, so the brain refused before asking. The rule this
wave installs, in the user's words: *the GUI is the brain and the monitor is
its follower. The GUI does not know what a new-chat button looks like; it can
know whether the monitor sees one, and if so tell the monitor to click it.*
Behavioural settings (auto-submit, delivery, chunk size, …) stay the monitor's
to configure and reach the brain in `Watched` (§10.5); the brain drives the
monitor from them. Corollary: nothing in `shell/chat` or `driver/automation`
may import `driver.screen.profile_store`, hold a `ServiceProfile`, or read a
template.

Second decision: a Chat UI started by double-click **does nothing** — no
monitor is launched or dialled — until the user attaches one from the Monitor
tab. `--monitor local|HOST:PORT|@NAME` remain terminal opt-ins.

### 11.1 Idle start

* `cli.resolve_monitor_target`: flag absent → `None` (idle); `local` →
  `LaunchLocal`. Nothing else changes. The Chat UI starts parked in
  DISCONNECTED with the red `NO MONITOR · attach or launch one` badge and the
  loop never ticks.
* Monitor tab, mode **Local**: the one button reads **Launch & connect a local
  monitor** (loopback TCP, token from the shared file, as §10.1 built). Its help
  line says the monitor window opens beside this one and closes with it.
* Docs (`commands.md` `--monitor` table, `configuration.md`, README quick-start)
  say: double-click = idle; attach from the Monitor tab.

**As built (2026-08-27).** `resolve_monitor_target` now answers `None` for an
absent flag and for `none`, and `LaunchLocal` for `local` alone — three
spellings, three answers, one line each. Nothing downstream changed: `main`
already handed all three values through unflattened (§10.2), so an idle start is
the DISCONNECTED path §10.1 built for `--monitor none`, red badge and parked loop
included.

* `--monitor`'s help gained the sentence "Omitted, the window starts idle and you
  attach a monitor from its Monitor tab"; `commands.md`'s table gained a
  *(flag absent)* row above `local`, and `local`'s row lost the words "the
  default when the flag is absent".
* Monitor tab, Local mode: the button reads **Launch & connect a local monitor**
  (`app.js`, the one place its label is decided) and `#mon-local-note` reads
  "opens the agentclip-monitor window beside this one; it closes with this
  window" — the *closes with* half being the fact a user cannot see before
  pressing it.
* `tests/shell/chat/test_shell.py`: `test_local_means_launch_one_here` and
  `test_an_absent_flag_and_none_both_start_idle` replace the old pair that
  parametrized `[None, "local"]` over `LaunchLocal`, and the end-to-end
  `--monitor` test loops the idle case over `["--monitor", "none"]` and `[]`.

### 11.2 Every calibration door leaves the Chat UI

Deleted, not re-pointed: sidebar **Edit services...** and **Set chat
region...**, the titlebar **monitor UI** button (`#calibrate-open`), `F2`,
`/identify`, `CALIBRATION_ELSEWHERE`, `GuiView.open_calibration`,
`show_identify_overlay`, `bridge.calibrate`, `runner.open_calibration`, the
help/palette entries, and the F-key row's `F2 calibrate`. DETECTION-panel hints
that say "F2" (`STALE_UNTICKED`, `STALE_OFF`, `PROFILE_HINT`) say "in the
Monitor UI" instead. `/identify` stays a Monitor UI feature only
(`docs/commands.md` moves the row to that section).

**As built (2026-08-27).** Deleted, in one pass, with nothing left pointing:

* Chat UI page: `#calibrate-open` (titlebar), `#edit-services` and `#set-region`
  (sidebar), their three `api("calibrate")` listeners, their `el` lookups, the
  `F2` row of `KEYS` and the `F2 calibrate` text in the sidebar's key-hint line.
  Two `KEYS` descriptions that said "set them with F2" / "see the sidebar" now
  say "in the Monitor UI".
* Python: `GuiView.open_calibration`, `GuiView.show_identify_overlay`,
  `CALIBRATION_ELSEWHERE`, `GuiRunner.open_calibration`, `JsApi.calibrate`,
  `JsCalls.open_calibration`, `ChatView.show_identify_overlay`,
  `SessionController._cmd_identify` and the `identify` rows of `COMMANDS` and
  the dispatch table. `/identify` is now an unknown command like any typo, and
  the unknown-command hint no longer lists it.
* DETECTION-panel hints: `STALE_UNTICKED` → "stillness not watched for this
  service - in the Monitor UI", `STALE_OFF` → "finish detection off - configure
  it in the Monitor UI", `PROFILE_HINT` → " · captures + detection in the
  Monitor UI". Two controller toasts that named F2 (no extra instructions; a
  preset deleted mid-session) name the Monitor UI instead.
* `F2` is left BOUND TO NOTHING, on purpose — §11.4 is what takes it — and
  `tests/shell/chat/test_keys.py::test_f2_is_bound_to_nothing` is what keeps it
  free until then.
* Tests: the three "every door gives the same sentence" tests became `hasattr`
  assertions on `GuiView` (a door that came back as a no-op would still be a
  door), plus `test_the_bridge_marshals_no_calibrate_verb` — the bridge is the
  whole of what the page may call, so a missing `calibrate` is the enforceable
  form of "no button, key or palette row can reach a calibration surface".
  `FakeChatView.show_identify_overlay` is gone; its counter stays, asserted at
  zero.
* Docs: `commands.md` lost the `F2` and `/identify` rows and gained an
  **Identify** row in its Monitor UI section; `configuration.md` and `AGENTS.md`
  say the Chat UI has no door to the Monitor UI at all; the headers of
  `ui-briefs/elements-panel.md`, `monitor-ui.md` and `service-editor.md` amend
  their §10 notes to say the doors those notes described are deleted.

### 11.3 The monitor answers every pixel question

Wire **v4** (`MONITOR_WIRE_VERSION = 4`):

* `Watched.captured: tuple[TemplateKind, ...]` — the kinds the monitor's own
  profile holds for the watched service (empty when unprofiled). Encoded as kind
  names. `watched_from(spec, profile=...)` fills it; the fake takes it from its
  `specs_for`/profiles.
* `Located.target: ScreenRegion | None` — the ONE pixel to click, i.e. the
  service's click point (`ServiceProfile.click_point(kind)`, a per-image
  percentage the user set in the Monitor UI) applied to `region`. `None` iff
  `region` is None. Computed in `LocalUIMonitor._locate_now`, so the brain never
  sees a click point. `hover_scan` returns a `Located` too (same field, so the
  copy click after a hover uses the same point).
* Brain-side replacements, one per former `live_profile()` read:
  * `acts.click_profile_element`: `NOT_CALIBRATED` when `cal.chat_region is None`
    **or** `kind not in ctx.captured(slot)` — `RecipeContext.captured(slot)` is
    `host.captured_for(slot)` = `Watched.captured` of that slot. The refusal
    sentence names the monitor: `the monitor has no {kind.label} captured for
    {service} - capture one in the Monitor UI`.
  * `auto_copy.py`: "no copy button is captured" ← `COPY not in captured`;
    the click target is `located.target` (keep the `Located`, not just its
    region; the hover-scan branch likewise).
  * `chatbox.py` `target`/`verified_target`: `found.target`.
  * `context.copy_status`: drop the template-size prefix (the Monitor UI shows
    sizes); the line is the status text alone.
* Gone: `AutomationHost.profile_for`, `NullHost.profile_for`,
  `AutomationController.live_profile`, `RecipeContext.live_profile`,
  `GuiView.profile_for/_profile/_profiles/_profile_root`, the `load_profile`
  import, `click_point_region` imports in the recipes. New:
  `AutomationHost.captured_for(slot) -> tuple[TemplateKind, ...]`.
* `tests/test_layering.py`: `shell.chat` and `driver.automation` may import
  from `driver.screen` only `profile.TemplateKind`, `region`, `slot`, `focus`
  (the explicit allowlist loses `profile_store`).

**AS BUILT — the monitor side** (2026-08-27). Wire **v4** is live;
`MONITOR_WIRE_VERSION = 4` and the handshake gates on it, so a v3 monitor and a
v4 brain refuse each other by name rather than disagreeing about a field.

* `Watched.captured: tuple[TemplateKind, ...] = ()`, filled by
  `watched_from(spec, *, profiled, generation, captured=())`. The kinds are
  passed in **beside** the spec rather than read out of it: which appearances
  exist is a fact about the pictures on this machine, and the spec is a fact
  about the row in the config. `LocalUIMonitor.configure` fills it from the
  profile it already resolves before the region check
  (`() if profile is None else profile.captured`), so `profiled` and `captured`
  can never disagree — an unprofiled service captures nothing.
  `EMPTY_WATCHED.captured` is `()`.
* `Located.target: ScreenRegion | None = None` — the 1x1 rectangle a click
  should land on, `click_point_region(region, *profile.click_point(kind))`
  computed in `LocalUIMonitor._locate_now` (and in the hover walk) through one
  new helper, `LocalUIMonitor._aim`. `None` iff `region` is None.
  `click_element` now *reads* `located.target` instead of recomputing it, so the
  find and the press are aimed by one reading of the profile. Defaulted rather
  than required so that every "nothing there" answer stays
  `Located(None, False, None)`.
* `hover_scan(kind) -> Located` on the Protocol, the local monitor, the fake,
  the remote client and both halves of `SwitchableMonitor`. `ambiguous` is
  always False and `best_miss` always None: a walk stops at the first frame the
  thing is on, so it has neither counted a second one nor judged a candidate it
  could report a diff for. `_RESULTS["hover_scan"]` is `encode_located` /
  `decode_located`.
* Wire: `captured` rides as a list of kind VALUES (`encode_kinds`), and its
  decode (`_captured_at`) is the one tolerant reader on this wire — an absent
  list reads as `()` and a name this build has no `TemplateKind` for is dropped.
  The version gate pins the shape of the wire, not the enum inside it, and "no,
  I have not got one of those" is the honest answer about a kind this side
  cannot name. Every other field stays strict. `Located.target` rides as an
  optional region beside `region`.
* `FakeUIMonitor` stages both: `captured: dict[str, tuple[TemplateKind, ...]]`
  keyed by service (like `saved_regions`), with a key nobody staged following
  `profiled` — every kind when the double is calibrated, none when it is not, so
  `captured["claude"] = ()` still means "profiled, no pictures";
  `click_points: dict[TemplateKind, tuple[int, int]]` (empty = the centre), and
  `locate`/`hover_scan` fill `target` under whatever `answers` scripted unless
  the suite wrote one itself. Helpers: `captured_for(service)`, `aim(kind, rect)`.
* Tests: `tests/driver/monitor/test_wire.py` (round-trip tables, the tolerant
  `captured` decode, the version is four), `test_rpc.py` (`captured` and an
  aimed `target` across a real socket, a hover scan crossing as a `Located`),
  `test_verbs.py` (a profile with a click point → `located.target` is that
  pixel; every hover-scan refusal is an empty `Located`), `test_watch.py`
  (local and fake staging).

Not in this note: the brain-side replacements above (`captured_for`,
`live_profile`'s removal, the recipes' new refusals) and the layering-test
change, which land with the `shell.chat` / `driver.automation` half.
`driver/automation/machine.py`'s `MonitorLike.hover_scan` still declares
`ScreenRegion | None` and is the one seam mypy flags until that half lands.

### 11.4 F2 — what the monitor sees

A sidebar block **MONITOR SEES**, hidden by default, toggled by `F2` (the key
is free after §11.2), remembered like the other sidebar toggles. One row per
kind in `TICK_KINDS ∪ Watched.captured` of the SELECTED slot: `✓ on screen` /
`· captured, not on screen` / `✗ not captured`, from the latest tick's
`sightings` and `Watched.captured`; under it the received settings the brain
drives from: `auto-submit on/off · delivery <mode> · paste ≤ N chars · hover
scan on/off · snap back on/off`. Pushed only when the rendered text changes
(a tick a second must not repaint an unchanged block). Help/palette entry:
`F2 — what the monitor sees (and the settings it sent)`.

### 11.5 Docs, tests, memory

`commands.md`, `configuration.md`, README, AGENTS.md vocabulary row,
`ui-briefs/*` headers that mention the Chat UI doors; unit tests for every
gate above with a `FakeUIMonitor` whose profile lives "over there"; the
shell tests that stage the three reports (a captured new-chat on the monitor,
none on the brain: `/new` clicks; a reply finishes: the copy click uses
`located.target`; a paste lands with `auto_submit` on: `send_enter` is
called). Memory note `wave4-brain-knows-no-pixels`.
