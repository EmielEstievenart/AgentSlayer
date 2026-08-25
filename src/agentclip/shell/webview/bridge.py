"""The GUI's two-way bridge: one FIFO into the page, one ``js_api`` out of it.

The GUI's answer to the thread contracts both view ports carry
(``ChatView.call_started`` and friends, every method of ``AutomationView``):
those calls arrive from the engine's worker thread, from the clipboard watcher
and from the detector poller, and none of them may block or touch the page
directly. So every one of them ends here, in :meth:`Bridge.send` - a JSON dict
with a ``type`` tag appended to a queue - and exactly one thread ever calls
``evaluate_js``.

**Why a drainer thread rather than calling ``evaluate_js`` where the event was
raised.** pywebview 5's ``Window.evaluate_js`` *is* safe to call from any
thread: the WebView2 backend marshals the script onto the WinForms UI thread
with ``Control.Invoke`` and then blocks the caller on a semaphore until the
script has run (``webview/platforms/edgechromium.py``). Both halves of that
matter. Safe-to-call means the queue is not needed for safety; *blocking* means
it is needed for everything else - a paint raised on the detector poller would
stall that tick behind a UI round trip, and the asyncio loop would stall behind
one mid-turn. And two threads calling it concurrently interleave in whatever
order the OS scheduler picks, which is precisely the ordering hazard phase 0
paid for once already (the paint-epoch filter in ``docs/design/gui.md`` §1,
slice 5b): a paint the outgoing run asked for landing after the rebuild that
replaced it. **One FIFO, one drainer, never interleaved** is the whole design,
and it makes the ordering property structural instead of something the shell
has to re-prove per event family.

The drainer also gives the page's own readiness for free: ``evaluate_js`` waits
on pywebview's ``_pywebviewready`` event, so events queued before the window has
finished loading are simply delivered late, in order, rather than lost.

**The other direction** is pywebview's ``js_api`` object. Its methods are called
by pywebview on a FRESH THREAD per call (``webview/util.py:js_bridge_call``
starts a ``Thread`` for every invocation), so nothing here may touch controller
state directly either: :class:`JsApi` is a marshalling shim onto whatever the
runner hands it, and the runner puts every one of them on the GUI's asyncio
loop - the same loop the session controller lives on.

Nothing in this module imports pywebview, or knows a window exists. The sink is
a plain ``Callable[[str], None]``, which is what lets the whole bridge be
exercised - ordering included - by a test with a list in it.

**The event vocabulary.** Every event is a JSON object with a ``type`` and
whatever that type carries; ``json.dumps`` runs with ``ensure_ascii``, so every
payload is ASCII on the wire no matter what the model wrote. The producer is
always :class:`~agentclip.shell.chat.view.GuiView` and the consumer is always
``app.js``'s ``dispatch``. Six families are pinned here because they are the
ones a renderer has to get exactly right; the rest (``state``, ``toast``,
``modal``, ``flash``, ``payload``, ``armed``, ``composer_reset``,
``transcript_clear``, ``modal_close``, ``toggle``) are each raised at one place
in ``chat/view.py``, next to the port method they answer.

The approval gate - ``show_gate`` / ``hide_gate``::

    {type: "gate", open: false}
    {type: "gate", open: true,
     title: str,           # "APPROVE · call 2/5 · run_command npm test"
     position: str,        # "2/5"
     queue: str,           # "✓1 read_file  ▶2 run_command", two spaces between
     tool: str, target: str, kind: "edit"|"command"|"auto",
     preview: str,         # the engine's preview, verbatim
     preview_kind: "diff"|"new_file"|"command"|"mcp"|"text",
     preview_head: str,    # "$ npm test" / the NEW FILE banner / "" for a diff
     preview_body: str,    # the diff, the new file, the mcp args, ""
     reason: str,          # the model's own justification, or ""
     note: str,            # why this is being asked at all, or ""
     timeout: str,         # run_command's, when it set one
     auto_reason: str, always_pattern: str|None,
     always_label: str,    # "" when there is no third answer to offer
     hint: str}            # the keys line, in the ActionPanel's words

The run panel - ``start_working`` / ``stop_working`` and the three
WORKER-THREAD methods (``docs/design/ui-briefs/main-chat.md`` §4)::

    {type: "run", running: false}
    {type: "run", running: true, label: str,
     calls: [{call_id: int, tool: str, detail: str,
              streams: bool, glyph: str}, ...]}   # the whole plan, up front
    {type: "run_call", phase: "started", call_id: int, tool: str,
     detail: str, streams: bool}
    {type: "run_call", phase: "finished", call_id: int, glyph: str}
    {type: "run_output", call_id: int, chunk: str}   # the DELTA, never the buffer

``run_call``/``run_output`` may name a ``call_id`` the plan never mentioned and
may re-resolve one that is already finished, so the page treats both as upserts;
``run_output`` chunks are deltas and the page owns the accumulation, bounded per
call, exactly as ``tui/widgets/run_panel.py`` says.

The status bar and the STATE rail - both composed on the Python side, for the
gate's reason: what is in them is a rule, not a style
(``docs/design/ui-briefs/sidebar-status-log.md`` §3)::

    {type: "status",
     segments: [{id: str, text: str, cls: str}, ...],  # IN ORDER, ten at most
     armed: bool, watching: bool, provider: str, project: str}
    {type: "rail", loop: str,                          # the active LoopState
     rows: [{state: str, label: str, mark: "active"|"legal"|"dim"}, ...]}

A segment that must hide is ABSENT from ``segments`` rather than present and
empty - that is how the TUI hides ``armed``/``instr``/``mcp`` too, by not being
drawn, leaving no padding behind. ``mark`` is ``LOOP_TRANSITIONS`` already
applied: display only, and the reason it is applied here is that the table is
the automation's vocabulary and the page has no business holding a copy.

The sidebar's remaining blocks and the log::

    {type: "sidebar", project: str, services: [[key, label], ...], service: str,
     service_label: str, profile_note: str, locked: bool, region: str,
     slot_note: str, detection_title: str,
     remote: str,             # the target, "" on a local session
     remote_lines: [str],     # target, link state, reconnect count, ruleset
     can_connect: bool}       # is there a way to go remote in this build?
    {type: "detection", kind: str, label: str, text: str}   # kind "STALE" too
    {type: "mcp", rows: [{name: str, state: str, line: str}, ...]}
    {type: "harness", kind: str, time: str, text: str, line: str}
    {type: "toggle", what: "log"}                           # /log, from Python

The ELEMENTS column and the SERVICE EDITOR are NOT in this vocabulary any more.
Both are the calibration window's, and that window has a bridge of its own with
its own event names (``monitor_ui/window.py``, ui-monitor.md 6.4) - two
windows on one bridge would put every calibration paint behind whatever this
window's WebView2 was doing. What crosses HERE about them is one page-to-Python
call (``calibrate``) and nothing coming back: the answers arrive as an ordinary
``sidebar`` repaint, because a chat region drawn over there is a fact about this
window's DETECTION block like any other.

The window tabs and the per-window transcripts - one tab per browser WINDOW,
one persistent transcript per tab (``ui-briefs/tabs-delegation-summary.md``)::

    {type: "tabs", selected: str, focused: str,
     masters: [{window: str, name: str, service: str,
                state: "none"|"running"|"ok"|"failed", label: str}, ...],
     subs: [...]}                       # the SELECTED master's sub-agents
    {type: "transcript", window: str, kind: ..., ...}   # every add_* carries it
    {type: "focus_session", session_id: str, window: str, role: str}

``selected`` is what the user is looking at and what the sidebar configures;
``focused`` is which transcript new output is written into. They are the same
window except for the duration of a delegation, and neither is the automation's
LIVE target - that one never moves for a tab click, which is this surface's
load-bearing invariant. ``state`` is derived from the window's run history
rather than stored, and only the LAST run's outcome is reported: the tab is a
status light, not a log.


The SSH CONNECT DIALOG - the one surface with no TUI equivalent, and the only
one whose event is a picture of a SEQUENCE running somewhere else
(``ui-briefs/ssh-connect.md``)::

    {type: "connect", open: false}
    {type: "connect", open: true,
     phase: "form"|"running"|"failed"|"done",
     target: str, root: str, preview: str,   # "connecting as dev@box:2222"
     saved: [{key, name, detail, root}, ...],     # [remote.<name>] tables
     aliases: [{key, name, detail, root}, ...],   # ~/.ssh/config Host entries
     error: str,                             # why Connect cannot be pressed
     steps: [{step, label, state, note}, ...],    # ALL SIX, always, in order
     failed_step: str, failure: str,
     policy: [str], connected: str,          # the target-owns-policy banner
     can_save: bool, save_name: str, saved_note: str,
     busy: bool}

``steps`` carries every one of the six whatever has happened, because the
checklist's contract is that a stage after a failure stays **pending** rather
than being skipped-with-a-checkmark (brief §3.4) - a row that vanished would
say the opposite. The four states are ``pending``/``running``/``ok``/``failed``
and ``note`` is the sentence that beat produced, which is the SAME string the
terminal path prints (``hosts/connect.py``, ``StepEvent``). ``policy`` is the
one thing this dialog says that no other surface does: which machine's ruleset
is about to govern the session, stated because the alternative is a user whose
host-PC ``permissions.json`` silently stopped applying.

The three questions a dial can ask ride the ORDINARY ``modal`` family, with
their own ``modal`` values (``connect_password`` / ``connect_hostkey`` /
``connect_keyboard``): a password prompt is a flow parked on an answer exactly
as ``confirm`` is, and one modal implementation is the rule (gui.md §3).

The slash-command registry and the appearance, both pushed once per page load
(``GuiView.start`` and ``page_ready``) because both are things the page needs to
HAVE rather than to be told about::

    {type: "commands",
     rows: [{name: str, label: str, summary: str}, ...]}   # COMMANDS, in order
    {type: "settings", theme: "dark"|"light",
     themes: [{value: str, label: str}, ...]}
    {type: "docs",                                         # the titlebar's button
     pages: [{name: str, title: str, text: str, found: bool}, ...]}

``commands`` is ``agentclip.shell.app.commands.COMMANDS`` - the one table the
controller's dispatch, `/help`, the unknown-command hint and the TUI's own popup
and help screen all read - and it feeds BOTH page consumers: the composer's
autocomplete popup and the help modal's command section, so neither can document
a command that no longer exists. The filtering rules stay on the page (a round
trip per keystroke would be latency for string work); the data never does.

``docs`` is the USER GUIDE - ``docs/commands.md`` and ``docs/configuration.md``
read off disk verbatim (``chat/docs.py``) and rendered page-side by the same
``renderMarkdown`` the transcripts use. It rides this one-way channel rather
than being a ``js_api`` method the button awaits, because the files are static
text: the page needs to HAVE them, exactly as it needs the command registry, and
a request/response for a fixed string would be the only value-returning call in
a bridge whose whole design is one FIFO. ``found`` is false when this build
carries no guide at all; ``text`` is then the short markdown note that says so,
so the viewer always has a page to draw.

``settings`` is the F4 surface in full, which is one setting: the TUI's
SettingsScreen is a theme picker and nothing else, so this one is too. The value
lives in the shell's own config table (``[gui] theme``) because the GUI's
palettes are CSS and ``[general] theme`` names a Textual theme - see
``config.VALID_GUI_THEMES``.

Help, settings, the user guide, the payload block and the quit-mid-turn confirm
all reuse the ONE modal element the ``modal`` family above drives. Only the last
of them is a ``modal`` event: the first three are page chrome (like F3's sidebar) and the
quit confirm is an ordinary ``ChatView.confirm``, raised from
``GuiView.confirm_quit`` after the window's ``closing`` handler refused the
close (``chat/runner.py:window_closing``).

``harness`` carries the rendered ``line`` as well as its parts because the
fixed-width kind column is ``HarnessEntry.line``'s decision, taken once below
both shells. The page keeps its own tail of these, bounded at the deque's own
``HARNESS_LOG_MAX``, so revealing the pane costs no replay across the bridge.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

# The one function the page exposes to Python. Guarded rather than called bare
# so an event that arrives before app.js has run is dropped instead of raising
# a ReferenceError inside the page's console (app.js installs the receiver at
# parse time and buffers, so this guard is the belt to that braces).
_RECEIVER = "window.agentclip && window.agentclip.receive"

EmitFn = Callable[[str], None]


def render_call(event: Mapping[str, Any]) -> str:
    """One event as the line of JavaScript that delivers it.

    ``json.dumps`` with the default ``ensure_ascii`` is what makes this safe to
    paste into a script: the result is ASCII, contains no raw newline and no
    unescaped quote, and JSON is a subset of JavaScript's object literal syntax,
    so the page receives a real object rather than a string it has to parse.
    """
    return f"{_RECEIVER}({json.dumps(event)});"


def payload_of(script: str) -> dict[str, Any]:
    """The inverse of :func:`render_call` - what the page would have received.

    Exists for the tests: the bridge's sink is a string sink (it is
    ``evaluate_js``), and a test that asserted on JavaScript source rather than
    on the event inside it would be pinning the wrapper instead of the message.
    """
    start = script.index("(") + 1
    end = script.rindex(")")
    parsed = json.loads(script[start:end])
    if not isinstance(parsed, dict):  # pragma: no cover - render_call only makes dicts
        raise ValueError(f"not an event payload: {script!r}")
    return parsed


class Bridge:
    """Python -> JS: a thread-safe queue, drained by one thread, in order.

    Constructible without a window on purpose (``emit`` is injected), which is
    what lets ``tests/shell/chat`` drive the whole event vocabulary against a recorder.
    """

    def __init__(self, emit: EmitFn | None = None) -> None:
        # SimpleQueue rather than Queue: unbounded, lock-free on the put side,
        # and this queue must never make a poller thread wait.
        self._events: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._emit: EmitFn | None = emit
        self._thread: threading.Thread | None = None
        # Set by ``stop`` before the sentinel goes in, so a late ``send`` from a
        # poller thread that has not noticed the teardown yet is dropped rather
        # than queued behind a drainer that is already leaving.
        self._closed = False

    # -- the sink -------------------------------------------------------------

    def attach(self, emit: EmitFn) -> None:
        """Point the bridge at the real ``evaluate_js``.

        Separate from construction because the window does not exist yet when
        the runner builds its object graph - the ``js_api`` the window needs is
        built from the same graph, so one of the two has to come second.
        """
        self._emit = emit

    def start(self, emit: EmitFn | None = None) -> None:
        """Begin draining. Idempotent; a second call is a no-op."""
        if emit is not None:
            self._emit = emit
        if self._thread is not None:
            return
        self._closed = False
        self._thread = threading.Thread(target=self._drain, name="agentclip-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Flush what is queued, end the drainer, and wait for it (bounded).

        The sentinel goes in BEHIND everything already queued, so a clean close
        still delivers the last toast and the last state push - which is the
        difference between a window that closes and a window that closes having
        said why.
        """
        self._closed = True
        self._events.put(None)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    # -- sending --------------------------------------------------------------

    def send(self, event_type: str, **fields: Any) -> None:
        """Queue one event for the page. Never blocks, never raises.

        The one method every worker thread in the app reaches, which is why it
        does nothing but serialise and append: whatever is wrong with the window
        is the drainer's problem, not the detector poller's.
        """
        if self._closed:
            return
        self._events.put(render_call({"type": event_type, **fields}))

    def drain_pending(self) -> int:
        """Emit everything queued right now, on the CALLING thread, and stop.

        Not used by the app - the drainer thread is - but it is how a test reads
        the bridge without one, and how ``stop`` behaves if a drainer was never
        started. Returns how many events went out.
        """
        sent = 0
        while True:
            try:
                script = self._events.get_nowait()
            except queue.Empty:
                return sent
            if script is None:
                return sent
            self._deliver(script)
            sent += 1

    # -- the drainer ----------------------------------------------------------

    def _drain(self) -> None:
        while True:
            script = self._events.get()
            if script is None:
                return
            self._deliver(script)

    def _deliver(self, script: str) -> None:
        emit = self._emit
        if emit is None:
            return
        try:
            emit(script)
        except Exception:  # noqa: BLE001 - a closing window is not an app error
            # ``evaluate_js`` raises whenever the window is on its way out (and
            # ``JavascriptException`` for a page that broke). Neither is worth
            # taking the app down for, and neither is worth a toast: the toast
            # would go through this same channel.
            return


class JsCalls(Protocol):
    """What :class:`JsApi` forwards to - the runner, structurally.

    A Protocol rather than the runner class so the bridge stays importable (and
    testable) without a loop, a window or a controller behind it. Every method
    here is called on a pywebview-owned thread and must return promptly; the
    implementation marshals onto the GUI's asyncio loop.
    """

    def page_ready(self) -> None: ...
    def submit_text(self, text: str) -> None: ...
    def submit_decision(self, choice: str, note: str) -> None: ...
    def cancel_execution(self) -> None: ...
    def dismiss_pending_question(self) -> None: ...
    def answer_prompt(self, prompt_id: str, value: Any) -> None: ...
    # The keys whose state the sidebar and the status bar show. One method per
    # intent rather than a single ``key(name)`` door, so the marshal is typed
    # and a page that asks for something this shell does not do fails at the
    # bridge instead of inside a controller.
    def set_os_armed(self, target: bool | None) -> None: ...
    def cycle_permission_mode(self) -> None: ...
    def toggle_watch(self) -> None: ...
    def recopy(self) -> None: ...
    def force_ingest(self) -> None: ...
    def reinstruct(self) -> None: ...
    def retry_insert(self) -> None: ...
    def set_service(self, key: str) -> None: ...
    # F2, the titlebar's button and both sidebar doors: open the calibration
    # window (``monitor_ui/``). One method, because everything that surface
    # decides it decides over there - this side only opens it and brackets it.
    def open_calibration(self) -> None: ...
    # The window tabs. Both are pure view-side navigation - no controller call
    # is made for either - but they still cross here, because the page is where
    # the click happens and the SELECTION lives in the view.
    def select_window(self, window: str) -> None: ...
    def next_window(self) -> None: ...
    def end_session(self) -> None: ...
    # The three remaining MainScreen bindings. `t` is absent: focusing the
    # composer is a page-side act with no controller behind it, like F3 and F8.
    def undo(self) -> None: ...
    def export_log(self) -> None: ...
    # F4's one setting. The flip is applied page-side (a CSS class), but WHICH
    # theme is current is config, so the page asks rather than remembers.
    def set_theme(self, theme: str) -> None: ...
    # ctrl+q. The window's own close button reaches the same decision through
    # ``GuiRunner.window_closing`` instead, which cannot come through here.
    def request_quit(self) -> None: ...
    # The SSH connect dialog (``ui-briefs/ssh-connect.md``) - the one surface
    # with no TUI equivalent. One method per intent, as the key family above
    # has: the form, the checklist and the three ways out of a failure are all
    # decisions on a Python-side model (``chat/remote.py``), so a page asking for
    # something this shell does not do fails at the bridge.
    def open_connect(self) -> None: ...
    def connect_select(self, key: str) -> None: ...
    def connect_fields(self, target: str, root: str) -> None: ...
    def connect_start(self) -> None: ...
    def connect_edit(self) -> None: ...
    def connect_cancel(self) -> None: ...
    def connect_save(self, name: str) -> None: ...
    def reconnect_now(self) -> None: ...


class JsApi:
    """JS -> Python: the object pywebview exposes as ``window.pywebview.api``.

    Deliberately tiny, and deliberately not a second controller: every method
    forwards to the same call the TUI's key bindings make
    (``SessionController.submit_message`` / ``submit_decision`` /
    ``cancel_execution``), so the two shells cannot grow two different ideas of
    what a send or an approval IS.

    ``submit_clipboard`` is absent on purpose: the clipboard watcher ingests,
    the page never does (``AutomationController`` owns the watcher thread and
    hands captures to the session itself).

    Every method swallows its own exceptions. pywebview logs and drops what a
    js_api method raises, so a failure here would otherwise be a click that
    silently did nothing - and the page is holding a promise that would never
    settle.
    """

    def __init__(self, calls: JsCalls) -> None:
        self._calls = calls

    def ready(self) -> None:
        """The page has installed its receiver and is ready to be painted."""
        self._safely(self._calls.page_ready)

    def submit(self, text: str = "") -> None:
        """The composer's Enter: a task, an ask_user answer, a follow-up or a
        slash command - the same one door ``MainScreen._submit_text`` is."""
        self._safely(lambda: self._calls.submit_text(text))

    def decide(self, choice: str = "", note: str = "") -> None:
        """The approval gate's answer: ``approve`` / ``approve_always`` / ``reject``."""
        self._safely(lambda: self._calls.submit_decision(choice, note))

    def cancel(self) -> None:
        """ctrl+x's equivalent: stop the tool calls in flight (the turn still
        finishes and still reports back - it is not an abort)."""
        self._safely(self._calls.cancel_execution)

    def dismiss_question(self) -> None:
        """Esc at an open ``ask_user``: put it aside without answering. Nothing
        is sent - the model keeps waiting and the next message answers it."""
        self._safely(self._calls.dismiss_pending_question)

    def prompt(self, prompt_id: str = "", value: Any = None) -> None:
        """One blocking prompt's answer, keyed by the id the modal was opened
        with (confirm / prompt_text / show_summary)."""
        self._safely(lambda: self._calls.answer_prompt(prompt_id, value))

    # -- the keys the sidebar and the status bar report on --------------------
    # F3 (sidebar) and F8 (log pane) are absent on purpose: both are pure
    # show/hide of a page element, so they never leave the page. ``/log`` comes
    # back the other way, as a ``toggle`` event, which is what keeps the command
    # and the key one implementation.

    def armed(self, target: bool | None = None) -> None:
        """F5 and ``/armed [on|off]``. ``None`` toggles, which is the bare form
        of both. No session gate, in any state, ever."""
        self._safely(lambda: self._calls.set_os_armed(target))

    def mode(self) -> None:
        """shift+tab: cycle build -> plan -> build. Never gated -
        it must work pre-session and mid-turn, which are the two moments the
        feature exists for."""
        self._safely(self._calls.cycle_permission_mode)

    def watch(self) -> None:
        """`w`: pause or resume the clipboard watcher."""
        self._safely(self._calls.toggle_watch)

    def recopy(self) -> None:
        """`c`: re-copy the last outbound; a second press inside the controller's
        double-tap window re-delivers it."""
        self._safely(self._calls.recopy)

    def ingest(self) -> None:
        """`i`: parse whatever is on the clipboard right now."""
        self._safely(self._calls.force_ingest)

    def reinstruct(self) -> None:
        """`r`: arm the service's extra instructions for the next payload."""
        self._safely(self._calls.reinstruct)

    def retry_insert(self) -> None:
        """The paste flash's button: run the click-and-paste that did not land
        again."""
        self._safely(self._calls.retry_insert)

    def service(self, key: str = "") -> None:
        """The sidebar's service picker - it edits the SELECTED window."""
        self._safely(lambda: self._calls.set_service(key))

    def calibrate(self) -> None:
        """F2, the titlebar's "calibrate" button and both sidebar doors: open
        the calibration window.

        One call for four affordances because they all ask for one thing - the
        window where the pixels are (ui-monitor.md 2.6). Nothing comes back
        through here: a chat region drawn over there arrives as a ``sidebar``
        repaint, and a preset saved over there as the ordinary config adoption.
        """
        self._safely(self._calls.open_calibration)

    def window(self, key: str = "") -> None:
        """A window tab was clicked: show that window and point the sidebar at
        it. Fired even for the tab that is already selected - that is how "click
        the tab I am on" means "show me this window" after a delegation moved
        the view (tabs-delegation-summary.md §6)."""
        self._safely(lambda: self._calls.select_window(key))

    def next_window(self) -> None:
        """F6: the next window tab in the bar's order."""
        self._safely(self._calls.next_window)

    def end_session(self) -> None:
        """`e`: open the session summary. Gated on the view side, where the
        phase is known, and refused with a toast rather than silence."""
        self._safely(self._calls.end_session)

    def undo(self) -> None:
        """`u`: undo the most recent turn. The confirm the controller opens on
        the way is the same one the TUI shows, composed there."""
        self._safely(self._calls.undo)

    def export_log(self) -> None:
        """`l`: write the whole chat log to a file. Safe mid-turn - it is a
        read-only snapshot and never touches the engine."""
        self._safely(self._calls.export_log)

    def theme(self, name: str = "") -> None:
        """F4's appearance picker: persist ``[gui] theme`` and repaint the
        settings modal from what was actually saved."""
        self._safely(lambda: self._calls.set_theme(name))

    def quit(self) -> None:
        """ctrl+q: close the window, asking first when a turn is in flight."""
        self._safely(self._calls.request_quit)

    # -- the SSH connect dialog -----------------------------------------------
    # The GUI-only surface. What crosses is intent, never the sequence: the six
    # steps, their order and every failure mode are ``hosts/connect.py``'s, the
    # same function ``cli.remote_launch`` drives with getpass.

    def connect_open(self) -> None:
        """The sidebar's "Connect to remote..." button. Also how the brief's
        "reconnect to a different target" is offered - there is no separate
        switch action, because connecting IS a new session (decision 4)."""
        self._safely(self._calls.open_connect)

    def connect_select(self, key: str = "") -> None:
        """A picker row: a saved ``[remote.<name>]`` target or a ~/.ssh/config
        alias. Prefills the form and connects nothing."""
        self._safely(lambda: self._calls.connect_select(key))

    def connect_fields(self, target: str = "", root: str = "") -> None:
        """A keystroke in the form, as the WHOLE candidate. No repaint follows -
        the page owns its inputs while they hold the caret."""
        self._safely(lambda: self._calls.connect_fields(target, root))

    def connect_start(self) -> None:
        """"Connect", and "Retry" - one call, because a retry IS a fresh attempt
        at the same values (every failure closes the host it built)."""
        self._safely(self._calls.connect_start)

    def connect_edit(self) -> None:
        """"Edit": back to the form with the attempted values in it."""
        self._safely(self._calls.connect_edit)

    def connect_cancel(self) -> None:
        """"Cancel"/"Close": drop the dialog, keep the window - which is the
        whole point of the surface (ssh-connect.md §1: no relaunch)."""
        self._safely(self._calls.connect_cancel)

    def connect_save(self, name: str = "") -> None:
        """"Save this target": one ``[remote.<name>]`` table in the GLOBAL
        config, and no secret in it (gui.md §4 rulings 1 and 2)."""
        self._safely(lambda: self._calls.connect_save(name))

    def reconnect_now(self) -> None:
        """The link indicator's button: spend the lazy re-dial early. The same
        ``_ensure`` the next operation would have called, never a second path."""
        self._safely(self._calls.reconnect_now)

    @staticmethod
    def _safely(call: Callable[[], None]) -> None:
        try:
            call()
        except Exception:  # noqa: BLE001 - see the class docstring
            return
