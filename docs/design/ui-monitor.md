# UI Monitor — the VM / brain split (plan)

> **Status: PLAN, not yet binding.** Decisions below were settled in a design
> session on 2026-08-24 and grounded in a code investigation the same day
> (every `file:line` reference in this document was verified against commit
> `e8d3ff5`). Phases graduate into **binding** one at a time, exactly as
> `remote-executor.md` did: when a phase lands, its section gets an "as built"
> note and its status flips. Until then everything here is intent.
>
> **Exception: §2.12, §6.0, §6.1, §6.3 and §6.4 are built, and binding.** Phase 0
> shipped on 2026-08-24: the default shell is the GUI, the TUI is behind
> `--tui` and frozen, and every phase-0 row in §7 is applied. Phase 1 shipped
> the same day, in its **Driver half only** — `driver/monitor/` exists, the
> controller consumes a `UIMonitor` instead of owning the poller, and §6.3's
> `describe()` is written and tested. The SHELL half is not in those commits;
> §6.1's status note is the ledger of what that rewire still owes, and until it
> lands the shell suites do not run. §3's interface listing is the shipped
> `driver/monitor/protocol.py`. §6.2 and everything from §6.4 on is still
> plan, **except §6.4**, which shipped on 2026-08-24 (see its own status note)
> and takes its phase-4 rows in §7 with it.
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
>   TUI is **deprecated** as of phase 0 (§6.0) and deleted in phase 6.
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
`shell/gui/view.py:3189 _element_png`; the capture overlay; `/identify`) is a
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
  run it, look up `(state, outcome)`, set the new state, repeat.

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

The monitor port is an unauthenticated channel to a machine's mouse, keyboard
and clipboard. v1 binds `127.0.0.1` by default and requires an explicit
`--bind` to listen elsewhere; the intended deployment is a VM on a private
host-only network or an SSH `-L` forward. Adding a shared-secret handshake is
**OPEN** (§8) and must be resolved before this document calls phase 5 binding.

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
> `566a364` neither shell had been touched: `shell/gui/view.py` and
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

### 6.2 Split decide from do — recipes, transitions, loop

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
> — `shell/gui/calibration/` (`window.py`, `view.py`, its own `assets/`), the
> `--calibrate` flag, and `tests/shell/gui/calibration/`. Part B emptied the
> chat GUI: the service editor, the `svc_*` bridge/runner/js_api families, the
> ELEMENTS column (`paint_elements` is a no-op that satisfies the port and no
> `crop_elements` is wired any more), the chat-region picker, `_picker_open`,
> `_refuse_second_picker` and `/identify`'s overlay are all gone from
> `view.py`/`bridge.py`/`runner.py`/`assets/`, together with their CSS, their
> markup and `tests/shell/gui/test_elements.py`. `service_editor.py` stays where
> it is: it is a MODEL with no window behind it, and the calibration package
> imports it.
>
> What the plan below did not say, and the two windows made true:
>
> - **One `webview.start()` per process.** The native pump runs on the main
>   thread and returns when the LAST window closes, so the two entry points are
>   not symmetric: `run_calibration` owns the pump (standalone), and
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
today (`shell/gui/shell.py:276–285`), so this is new engineering, not a
refactor. Put it in `shell/gui/calibration/` with its own bridge object and
its own HTML/JS/CSS bundle (embedded in Python source per AGENTS.md).

Move in: `ServiceEditor` (`shell/gui/service_editor.py`, already composed and
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

### 6.5 The RPC

`driver/monitor/wire.py` (frames: `hello`, `hello_ack`, `call`, `result`,
`error`, `tick`, `clip`; own version constant), `driver/monitor/server.py`
(TCP listener, long-lived, one brain, §2.8), `driver/monitor/remote.py`
(`RemoteUIMonitor`, reader task, `observe()` resolves on the next pushed tick
with `seq` greater than the one current at call time), a console script
`agentclip-monitor` (`pyproject.toml [project.scripts]`, plus a PyInstaller
spec alongside `packaging/agentclip-engine.spec`), and a `--monitor
host:port` launch flag / GUI connect field. Add `LoopState.DISCONNECTED` and
its transitions (§2.9).

**Done when:** a localhost e2e test runs the full recipe suite against
`RemoteUIMonitor` → real TCP → server → `LocalUIMonitor` with fake ops, and
the harness log is identical to the local-mode run; a kill-and-redial test
lands in `DISCONNECTED` and recovers; §5's bind default is enforced; suite
green; exe and monitor exe rebuilt.

### 6.6 Delete

- `shell/tui/` in full; `textual` out of `pyproject.toml`; `tui.md` header
  becomes "historical, not binding"; the `--tui` flag prints a one-line
  "removed" message for one release, then goes.
- The `SshHost` per-call path, exactly as `remote-executor.md` §2.8 specifies
  (its increment 5): `SshExec`, `wrap_command`, `spawn`, `run_blocking`,
  `run_detached`, the `Host` filesystem primitives in `ssh.py:757–864`, the
  `host=` parameter of `make_engine_factory` / `make_engine_builder`, and the
  pin test `tests/test_launch_remote.py:593–626`. Keep: connect/auth/reconnect,
  `open_link_channel` / `LinkChannel` / `_ChannelReader` / `_ChannelWriter`.
  Amend `remote-ssh.md` as §2.8 says.

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
| 5 | `docs/configuration.md`, `docs/commands.md` | `--monitor`, `--calibrate`, `agentclip-monitor` |
| 6 | `docs/design/tui.md`, `remote-ssh.md` | historical headers |

All **phase 0**, **phase 1** and **phase 4** rows are applied (§6.0's, §6.1's
and §6.4's as-built notes). Phase 5's row is half applied ahead of its phase:
`--calibrate` is in `docs/commands.md` and `docs/configuration.md` already,
because it shipped with §6.4; `--monitor` and `agentclip-monitor` are still
owed. The rest are still owed.

## 8. Open points (**OPEN**)

- **Auth on the monitor port** (§5). Shared secret in `hello`? Rely on SSH
  forwarding only? Must close before phase 5 is binding.
- **Which clipboard the manual fallback states mean in split mode.**
  `MANUAL_INSERT` / `MANUAL_COPY` assume the operator can reach the browser's
  clipboard. On a VM that is the VM's clipboard — the brain's GUI can show the
  text to paste, but the operator pastes it *there*. Confirm the attention
  alarm and the GUI copy still make sense, or park manual states as
  unsupported in split mode.
- **Tick cadence on the wire.** 0.5 s × a few hundred bytes is nothing on a
  LAN; over a WAN link the reader task's backlog policy (drop-to-latest vs.
  queue) is undecided. Default to drop-to-latest; `observe()` only ever wants
  the newest anyway.
- **Textual removal timing.** Phase 6 deletes it; whether `--tui` survives one
  release as a stub or goes immediately is the user's call at phase 6.
