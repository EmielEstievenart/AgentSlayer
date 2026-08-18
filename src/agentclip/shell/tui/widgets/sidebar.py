"""Sidebar: the right-hand settings column on MainScreen (tui.md section 1.3).

Replaces the old new-session modal. The service ("profile") picker lives here
permanently instead of behind a launch dialog: at launch the user sees an empty
chat with the composer focused, and the sidebar tells them *which* service the
first message will start a session against. The Select is locked while a session
runs (a session's preset is fixed - its budget is baked into the engine) and
unlocks again whenever the app is waiting for a new session's first message.

The widget is dumb on purpose: it holds no session state, exposes ``service``
(the chosen preset key), ``set_locked``, ``refresh_services``, ``show_slot``,
``show_profile``, ``show_mcp``, ``update_region``, ``update_template``,
``update_stale``, ``show_loop``, ``show_armed_state`` and the ``show_paste_flash``/
``hide_paste_flash`` pair; MainScreen owns every bit of routing. The paste
flash is the one animated thing here - a deliberately obnoxious blinking banner
that nags the user to Ctrl+V the outbound payload into the chat; the blink timer
is pure presentation, so the dumb widget may own it. Under it sits the one
button that is not configuration, "Retry insert" (``#retry-insert-btn``, shown
by ``show_paste_flash(retry=True)``): the answer to that nag, which re-runs the
click-and-paste MainScreen could not land. Its structural sibling the
DISARMED banner (``show_armed_state``) deliberately does NOT blink: see
``DISARMED_BANNER_TEXT``.

The column is **live status plus the two things you steer with**, and nothing
that has to be configured. Capturing what a service looks like, and ticking
which finish signals it may run, both moved into the service editor (F2): they
are per-service settings that belong next to the service's other settings, and
six capture buttons with six status lines had grown into two thirds of a 32-cell
column. What is left is:

* **STATE** - an eight-line rail at the very top of the column, one row per
  ``driver.automation.loop_state.LoopState``: the browser-automation loop (idle, auto/manual
  insert, wait send, wait generate, auto/manual copy, interpreting), NOT the
  engine's task phase. The active state gets a ``▶`` marker and bold/reverse
  styling, ``LOOP_TRANSITIONS[active]``'s legal next moves read at normal
  brightness, and everything else is dim. ``show_loop(state)`` repaints it, and
  MainScreen drives it straight from the automation's own events (the paste
  attempt, the send gate, the finish detectors, the auto-copy flow, the
  clipboard capture) - so like DETECTION below it, this block describes the
  LIVE window's loop, whichever tab the user happens to be looking at.
* **SERVICE** - the picker, its caption and the read-only appearance summary.
  All three describe the **selected window tab's** service (tui.md 1.6): the
  two browser windows are pointed at a service each, and the tab bar is what
  chooses between them, so there is no AGENT SLOT picker here any more.
  ``show_service`` writes the selected tab's key in without announcing a switch.
* **CHAT WINDOW** - what the selected tab's window is, and the readiness line
  under it. ``show_slot`` repaints the block from one slot in one go. Unlike the
  service picker it is never locked: drawing the sub-agent window mid-session is
  the normal way to reach delegation.
* **DETECTION** - five read-only lines the running automation writes into: the
  send gate holding finish detection back until the user presses Enter, the
  busy and idle probes (``update_template``), the staleness verdict
  (``update_stale``), and what the auto-copy flow's last click attempt did.
  Only these four ``TemplateKind`` values are ever DECIDED from; the two chat
  boxes and the new-chat button are searched on every tick like everything else
  (screen/detector.py) but no verdict here is drawn from them, and the clicks
  that use them report through toasts - so they have no line here. These are the
  WORDS; the pictures behind them - the actual matched pixels, one crop for
  every kind, including the three with no line here - are the ``ElementsPanel``
  column next door (F7, tui.md 1.7), which is written by the same machinery
  under the same rule.
  Unlike everything above it this block
  describes the **live** window rather than the selected tab - it is what the
  detectors are doing right now, and mid-delegation that is the sub-agent's
  window while the user reads the master's - so its heading names that window
  (``show_detection_window``) and *only* the detector machinery writes its
  lines. Nothing driven by a tab click may touch them.
* One read-only **appearance summary** under the service picker
  (``show_profile``) - "appearance: 4/7 captured" - so "is this service usable
  at all?" is answerable at a glance without opening the editor. It follows the
  picker, so switching tabs can change what it says: two windows on two
  services have two sets of captures.

Every status label carries the ``side-status`` class so the column reads as one
list rather than a pile of one-off ids.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Button, Select, Static

from agentclip.config import Config
from agentclip.driver.automation import delivery as _delivery
from agentclip.driver.automation.finish import SEND_READY_ARMED as SEND_READY_ARMED
from agentclip.driver.automation.finish import SEND_READY_HOLDING as SEND_READY_HOLDING
from agentclip.driver.automation.finish import SEND_READY_OVERRIDDEN as SEND_READY_OVERRIDDEN
from agentclip.driver.automation.finish import SEND_READY_RELEASED as SEND_READY_RELEASED
from agentclip.driver.automation.finish import SEND_READY_RESTING as SEND_READY_RESTING
from agentclip.driver.automation.finish import SEND_READY_SEEN as SEND_READY_SEEN
from agentclip.driver.automation.finish import SEND_READY_STUCK as SEND_READY_STUCK
from agentclip.driver.automation.finish import SEND_READY_TIMEOUT as SEND_READY_TIMEOUT
from agentclip.driver.automation.loop_state import LOOP_TRANSITIONS, LoopState
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot, SlotCalibration, can_delegate, missing
from agentclip.executor.mcp.types import McpServerStatus

_HINT = "F3 hides this column · F7 elements · F5 armed · F2 settings · F1 help"

# The DISARMED banner, the paste flash's quiet sibling: same shape, same place
# in the tree, opposite temperament. The flash blinks because it is asking for a
# keystroke *now* and the user is looking at their browser; this one is a
# standing fact about the whole app and has to survive being looked at for an
# hour, so it never animates. It says what stopped, because "disarmed" alone
# would leave the user wondering whether detection died too (it did not).
DISARMED_BANNER_TEXT = "⛔ DISARMED\nwatching only - F5 arms"

# The STATE rail: one row per LoopState, painted at the very top of the column
# so "where in the paste-send-generate-copy loop are we" needs no click
# anywhere. In loop order, which is why it is a tuple here rather than
# ``list(LoopState)``: the rail is a picture of the round trip, not of the
# enum's declaration.
STATE_TITLE = "STATE"
_LOOP_ORDER: tuple[LoopState, ...] = (
    LoopState.IDLE,
    LoopState.AUTO_INSERT,
    LoopState.MANUAL_INSERT,
    LoopState.WAIT_SEND,
    LoopState.WAIT_GENERATE,
    LoopState.AUTO_COPY,
    LoopState.MANUAL_COPY,
    LoopState.INTERPRETING,
)
_LOOP_LABEL: dict[LoopState, str] = {
    LoopState.IDLE: "idle",
    LoopState.AUTO_INSERT: "auto insert",
    LoopState.MANUAL_INSERT: "manual insert",
    LoopState.WAIT_SEND: "wait send",
    LoopState.WAIT_GENERATE: "wait generate",
    LoopState.AUTO_COPY: "auto copy",
    LoopState.MANUAL_COPY: "manual copy",
    LoopState.INTERPRETING: "interpreting",
}


def state_row_id(state: LoopState) -> str:
    return f"side-state-{state.name.lower()}"


# The four things this banner can say moved down with the delivery that CHOOSES
# between them (agentclip.driver.automation.delivery): the automation port takes finished
# words and is told where to put them, so the wording is decided once below both
# shells rather than twice above them. They stay reachable under this widget's
# names, which is where it and the suites already reach for them.
PASTE_FLASH_TEXT = _delivery.PASTE_FLASH_TEXT
ENTER_FLASH_TEXT = _delivery.ENTER_FLASH_TEXT
AUTO_SEND_FLASH_TEXT = _delivery.AUTO_SEND_FLASH_TEXT
STREAM_FLASH_TEXT = _delivery.STREAM_FLASH_TEXT
stream_flash_text = _delivery.stream_flash_text


# The flash's companion button (MainScreen.retry_insert): "the click and the
# Ctrl+V did not land - do them again". It rides with the banner rather than
# living down in the CHAT WINDOW block because it is an answer to what the
# banner is asking for, and a control the user only ever wants in the seconds
# they are reading that banner.
RETRY_INSERT_LABEL = "Retry insert"


_FLASH_BLINK_S = 0.4
_REGION_UNSET = "not set - alt-tab to the chat yourself"
# The one read-only line about what the selected service LOOKS like. The
# captures themselves live in the service editor now, and so does the checklist
# deciding which of them the poller may use - so this says only whether there
# are enough of them to be useful, and names the door to BOTH halves.
PROFILE_HINT = " · F2 for captures + detection"


def profile_summary(profile: ServiceProfile) -> str:
    return f"appearance: {profile.describe()}{PROFILE_HINT}"


# The MCP block: one side-status line per configured server, painted from
# McpManager.statuses() (docs/design/mcp.md section 6). The block is composed
# ONLY when the app was built with an MCP manager - most installs configure no
# servers, and an empty "MCP" heading would be a standing question with no
# answer - so the number of lines is fixed for the process (permissions.json is
# read once, the manager's record list never grows) and the lines are
# addressed by config-order INDEX rather than by server name: names are the
# user's and can collide once sanitized, ids must not.
MCP_TITLE = "MCP"

# The state literal -> the words the column shows. Human wording, one cell of
# intent each; the full detail line rides `failed`/`needs_auth` because those
# are the two states whose whole value is their explanation.
_MCP_STATE_LABEL: dict[str, str] = {
    "pending": "pending",
    "connecting": "connecting",
    "connected": "connected",
    "disabled": "disabled",
    "failed": "failed",
    "needs_auth": "needs auth",
    "missing_sdk": "no mcp sdk",
}


def mcp_row_id(index: int) -> str:
    return f"side-mcp-{index}"


def mcp_line(status: McpServerStatus) -> str:
    """One server's line: name + human state (+ tools when connected, + the
    detail on the two states that are questions until it is read). A 30-cell
    column cuts long details mid-sentence (CSS ellipsis); the full text is
    `/mcp`'s job, and this line is what tells the user to go ask."""
    label = _MCP_STATE_LABEL.get(status.state, status.state)
    parts = [status.name, label]
    if status.state == "connected":
        parts.append(f"{status.tool_count} tool{'' if status.tool_count == 1 else 's'}")
    if status.state in ("failed", "needs_auth") and status.detail:
        parts.append(status.detail)
    return " · ".join(parts)


# The stale detector has nothing to capture - it watches the drawn window stop
# changing - so its readout has only these resting states, plus whatever live
# verdict the poller paints over them.
STALE_UNSET = "no chat region - staleness check disabled"
STALE_CALIBRATED = "watching the chat region"
# The service's checklist does not tick "screen stops changing", but something
# else does run - so this line has no verdict to show and would otherwise sit
# blank while the icon detectors do the work.
STALE_UNTICKED = "stillness not watched for this service - F2"
# ...plus one more, which is not about the stale detector at all: the service's
# finish-signal checklist leaves NOTHING running (empty, or asking only for
# appearances it has none of). It goes here because this is the line the finish
# verdict is read off, and "auto-copy will never fire" has to be visible
# somewhere other than in a copy that never arrives - with the door to the
# checklist named, since that is the only place it can be turned back on.
STALE_OFF = "finish detection off - F2 to configure"

# The DETECTION block: four of the seven appearances have something to say while
# the automation runs, and each gets one line. The lines are otherwise
# indistinguishable verdicts stacked on top of each other, so the widget names
# each one as it paints it.
DETECTOR_LABEL = {
    TemplateKind.SEND_READY: "send",
    TemplateKind.BUSY: "busy",
    TemplateKind.IDLE: "idle",
    TemplateKind.COPY: "copy",
}
# What those lines say before anything has run. Nothing about capture state:
# that is the summary line's job, and the editor's.
PROBE_RESTING = "no verdict yet"
COPY_RESTING = "no click yet"
# ...except for the one combination that will never produce a verdict at all:
# the checklist ticks this signal and the service has no appearance to match it
# against, so the detector is silently skipped. "no verdict yet" for the rest of
# the run is indistinguishable from a detector that simply never finds anything.
PROBE_UNCAPTURED = "ticked but not captured - F2"

# The send gate's line (tui.md 3.4b) is re-exported from the imports above rather
# than spelled here: the gate itself is the AutomationController's, so the words
# it puts on that line live with it (agentclip.driver.automation.finish) and both shells
# say the same thing. Eight states, because "nothing is happening" has to be told
# apart from "this service cannot do it at all" - and because the three ways the
# gate can let go WITHOUT seeing the button vanish are three different things to
# tell the user about their capture: the model is visibly generating (so the send
# is proven by better evidence), the button never showed at all, or it showed and
# then never stopped showing.

# The DETECTION block's heading, which names the window every line under it is
# about. That is the LIVE window - the one the automation drives - and not the
# selected tab, and the two are different for the whole of a delegation: without
# the name, a sub-agent's verdicts read as the master tab's.
DETECTION_TITLE = "DETECTION"


def detection_title(window_name: str) -> str:
    return f"{DETECTION_TITLE} · {window_name}" if window_name else DETECTION_TITLE


def template_status_id(kind: TemplateKind) -> str:
    return f"side-tpl-{kind}"


# The CHAT WINDOW note, one line per state. The master window has nothing to be
# "ready" for - it is simply the chat the session runs in - so only the
# sub-agent window reports readiness, and it reports the gaps by name.
SLOT_NOTE_MASTER = "the main agent's chat window"
SLOT_NOTE_READY = "delegation ON"
SLOT_NOTE_MISSING = "delegation off · need: "


def slot_note(cal: SlotCalibration, profile: ServiceProfile) -> str:
    """The one-line readiness readout under the selected tab's window.

    Two inputs because readiness has two halves now: the box this window was
    drawn as, and what the service THAT TAB is pointed at looks like - which is
    the sub-agent tab's own service whenever the sub-agent tab is selected.
    """
    if cal.slot is AgentSlot.MASTER:
        return SLOT_NOTE_MASTER
    if can_delegate(cal, profile):
        return SLOT_NOTE_READY
    return SLOT_NOTE_MISSING + ", ".join(missing(cal, profile))


def _short_root(project_root: Path) -> str:
    try:
        return str(Path("~") / project_root.relative_to(Path.home()))
    except ValueError:
        # A root with no drive letter on Windows is a REMOTE, POSIX one: str()
        # would spell /home/dev/app with backslashes, which is not its name.
        return str(project_root) if project_root.drive else project_root.as_posix()


def _budget(chars: int) -> str:
    return f"{chars // 1000}k" if chars >= 1000 else str(chars)


def _service_options(config: Config) -> list[tuple[str, str]]:
    """``key · 12k`` per row - the column is 30 cells wide, so the preset's human
    label goes on its own line under the Select instead of wrapping inside it."""
    presets = sorted(config.services.values(), key=lambda p: p.key)
    return [(f"{p.key} · {_budget(p.max_paste_chars)}", p.key) for p in presets]


class Sidebar(Vertical):
    """Project root + service picker + the door to the service editor."""

    # Only what the app-level CSS (AgentClipApp.CSS, which wins on specificity)
    # does not already say. The column is a stack of calibration rows now, so
    # the muted status line is a class instead of an id list that has to grow
    # every time a detector is added.
    DEFAULT_CSS = """
    Sidebar .side-status {
        color: $text-muted;
    }
    Sidebar #side-slot-note {
        /* Fixed height: the note grows and shrinks as pieces are calibrated,
           and every line below it must keep its screen position. */
        height: 3;
    }
    Sidebar #side-profile-note {
        height: 2;
    }
    Sidebar .side-probe {
        /* Same reason as the slot note: a live verdict is longer than its
           resting line, and the "New browser chat" button below must not walk
           up and down the column as the poller talks. */
        height: 2;
    }
    Sidebar .side-state-row {
        color: $text-muted;
    }
    Sidebar .side-state-row.side-state-legal {
        color: $text;
    }
    Sidebar .side-state-row.side-state-active {
        color: $text;
        text-style: bold reverse;
    }
    Sidebar .side-mcp-row {
        /* One server, one row, whatever its detail says: a failed server's
           explanation can run to a sentence, and wrapping it would walk every
           block below up and down the column as servers connect and fail. Cut
           with an ellipsis instead - /mcp prints the whole thing. */
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    class ServiceChanged(Message):
        """The user picked a different service.

        Its appearances are a different set, so this is not a display-only
        change: MainScreen reloads the profile behind it.
        """

        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    def __init__(
        self,
        config: Config,
        project_root: Path,
        *,
        mcp_statuses: Sequence[McpServerStatus] | None = None,
        id: str | None = None,  # noqa: A002 - Textual API
    ) -> None:
        super().__init__(id=id)
        self._config = config
        self._project_root = project_root
        # The MCP block's initial paint, and the decision whether the block
        # exists at all: None (or empty) means the app runs without an MCP
        # manager and the column stays exactly what it always was. The count is
        # fixed for the process - ``show_mcp`` repaints these rows, it never
        # adds one - see MCP_TITLE above.
        self._mcp_statuses: tuple[McpServerStatus, ...] = tuple(mcp_statuses or ())
        self._flash_timer: Timer | None = None
        # The service the ServiceChanged message last reported. Textual fires
        # Select.Changed for the value compose sets as readily as for a user's
        # pick, and MainScreen reloads a profile and restarts its detector
        # worker on every one it hears about - so this is what keeps the
        # widget's own writes from reading as a switch.
        self._reported_service = self._default_service()

    def compose(self) -> ComposeResult:
        yield Static(Text(STATE_TITLE), classes="side-title")
        for state in _LOOP_ORDER:
            yield Static(
                Text(f"  {_LOOP_LABEL[state]}"),
                id=state_row_id(state),
                classes="side-state-row",
            )
        yield Static(Text(PASTE_FLASH_TEXT), id="side-paste-flash")
        # The flash's one action, directly under it: the payload is on the
        # clipboard and the automation could not get it into the chat box, so
        # this re-runs the click-settle-paste(-submit) sequence rather than
        # leaving the user to alt-tab and Ctrl+V. Hidden until there is such a
        # failure to undo - see ``show_paste_flash``.
        yield Button(RETRY_INSERT_LABEL, id="retry-insert-btn", variant="warning")
        # Directly under the STATE rail and above everything else: the rail is
        # the first thing read on this column, and "the loop you are reading is
        # not allowed to move on its own" belongs against it rather than at the
        # bottom of a column that scrolls.
        yield Static(Text(DISARMED_BANNER_TEXT), id="side-armed-banner")
        yield Static(Text("PROJECT"), classes="side-title")
        yield Static(Text(_short_root(self._project_root)), id="side-root")
        # The MCP block sits with PROJECT, above the per-tab blocks: like the
        # root it is a fact about the whole app run (one manager per process,
        # docs/design/mcp.md section 3), not about whichever tab is selected.
        # Absent entirely - heading included - when there is no manager.
        if self._mcp_statuses:
            yield Static(Text(MCP_TITLE), id="side-mcp-title", classes="side-title")
            for index, status in enumerate(self._mcp_statuses):
                yield Static(
                    Text(mcp_line(status)),
                    id=mcp_row_id(index),
                    classes="side-status side-mcp-row",
                )
        yield Static(Text("SERVICE"), classes="side-title")
        yield Select(
            _service_options(self._config),
            value=self._default_service(),
            allow_blank=False,
            id="service-select",
        )
        yield Static(Text(self._preset_caption()), id="side-service-label")
        yield Static(Text(""), id="side-profile-note", classes="side-status")
        yield Button("Edit services...", id="edit-services-btn", variant="primary")
        yield Static(Text("CHAT WINDOW"), classes="side-title")
        yield Button("Set chat region...", id="set-region-btn")
        yield Static(Text(_REGION_UNSET), id="side-region", classes="side-status")
        yield Static(Text(SLOT_NOTE_MASTER), id="side-slot-note", classes="side-status")
        yield Static(
            Text(detection_title("")), id="side-detection-title", classes="side-title"
        )
        yield Static(
            Text(self._probe_line(TemplateKind.SEND_READY, SEND_READY_RESTING)),
            id=template_status_id(TemplateKind.SEND_READY),
            classes="side-status side-probe",
        )
        yield Static(
            Text(self._probe_line(TemplateKind.BUSY, PROBE_RESTING)),
            id=template_status_id(TemplateKind.BUSY),
            classes="side-status side-probe",
        )
        yield Static(
            Text(self._probe_line(TemplateKind.IDLE, PROBE_RESTING)),
            id=template_status_id(TemplateKind.IDLE),
            classes="side-status side-probe",
        )
        yield Static(Text(STALE_UNSET), id="side-stale", classes="side-status side-probe")
        yield Static(
            Text(self._probe_line(TemplateKind.COPY, COPY_RESTING)),
            id=template_status_id(TemplateKind.COPY),
            classes="side-status side-probe",
        )
        yield Button("New browser chat", id="newchat-btn")
        yield Static(Text(_HINT), classes="side-hint")

    @staticmethod
    def _probe_line(kind: TemplateKind, text: str) -> str:
        return f"{DETECTOR_LABEL[kind]} · {text}"

    # -- the STATE rail ---------------------------------------------------------

    def show_loop(self, active: LoopState) -> None:
        """Repaint the STATE rail from the automation loop's current state.

        MainScreen owns the state and calls this as the loop's own events land
        (the paste attempt, the send gate, the detectors, the copy flow, the
        clipboard capture). The legal-next set comes straight out of
        ``LOOP_TRANSITIONS`` - forward motion only; resets to IDLE are not
        drawn as legal moves.
        """
        legal_next = LOOP_TRANSITIONS.get(active, frozenset())
        for state in _LOOP_ORDER:
            row = self.query_one(f"#{state_row_id(state)}", Static)
            marker = "▶ " if state is active else "  "
            row.update(Text(f"{marker}{_LOOP_LABEL[state]}"))
            row.set_class(state is active, "side-state-active")
            row.set_class(state is not active and state in legal_next, "side-state-legal")

    def _preset_caption(self, key: str | None = None) -> str:
        preset = self._config.services.get(key or self._default_service())
        if not preset:
            return ""
        return (
            f"{preset.label} · {preset.max_paste_chars:,} chars per paste "
            f"· {preset.total_context_chars:,} chars context"
        )

    @on(Select.Changed, "#service-select")
    def _on_service_changed(self, event: Select.Changed) -> None:
        # The caption is display only. The domain event is re-posted because a
        # different service means a different set of captured appearances -
        # MainScreen has to reload the profile, repaint the appearance summary and
        # restart the detectors against it.
        event.stop()
        value = event.value
        key = None if value is Select.NULL else str(value)
        # Select.Changed is a *message*, so it arrives a turn or more after the
        # value actually moved - and refresh_services moves it twice in one go
        # (set_options resets the value to the first option before the previous
        # selection can be put back). By the time the first of those is
        # delivered it describes a selection that no longer exists, and acting
        # on it would repaint the appearance summary against the wrong service and
        # point the detectors at it. An event whose value is no longer the
        # picker's value is history, not a choice.
        if key is not None and key != self.service:
            return
        self.query_one("#side-service-label", Static).update(Text(self._preset_caption(key)))
        if key is not None and key != self._reported_service:
            self._reported_service = key
            self.post_message(self.ServiceChanged(key))

    def _default_service(self) -> str:
        """The configured default, or the first preset if the config is odd."""
        configured = self._config.general.service
        if configured in self._config.services:
            return configured
        return next(iter(sorted(self._config.services)))

    # -- the service picker ---------------------------------------------------

    @property
    def service_select(self) -> Select[str]:
        return self.query_one("#service-select", Select)

    @property
    def service(self) -> str:
        """The selected preset key (falls back to the configured default)."""
        value = self.service_select.value
        return self._default_service() if value is Select.NULL else str(value)

    def show_service(self, key: str) -> None:
        """Put ``key`` in the picker WITHOUT announcing a service switch.

        The entry point for a window-tab change: each tab carries its own
        service, so selecting one has to move the picker - but that is the
        picker catching up with a choice the user already made, not a new one.
        Announcing it would send MainScreen round the whole "a different service
        is a different set of appearances" loop (reload the profile, restart the
        detectors) on behalf of a window it may not even be driving. Same
        ``_reported_service`` discipline as ``refresh_services``, and the caller
        repaints the summary and the readiness note itself.
        """
        if key not in self._config.services:
            return
        select = self.service_select
        self._reported_service = key
        if select.value != key:
            select.value = key
        self.query_one("#side-service-label", Static).update(Text(self._preset_caption(key)))

    def set_locked(self, locked: bool) -> None:
        """Lock the picker while a session owns the services; unlock between them.

        Only the *service* picker, and it locks whichever tab is selected: the
        master's budget is baked into its Engine at bootstrap and the sub-agent
        tab's service decides the delegate catalog at that same moment, so
        neither may move mid-session. The chat-region button beside it stays
        live for the whole session, because drawing the sub-agent window
        mid-session is the normal way to reach delegation.
        """
        self.service_select.disabled = locked

    # -- the selected tab's chat window ---------------------------------------

    def show_slot(self, cal: SlotCalibration, note: str) -> None:
        """Repaint the CHAT WINDOW block from one window's stored state.

        Called when the window tab bar moves and on session teardown - every
        readout below is a view of ``cal`` and nothing else, which after the
        slot reduction means the drawn window and the readiness line under it.

        Two neighbouring blocks are deliberately NOT repainted here. The
        appearance summary belongs to the service, and a tab switch may or may
        not be a service switch (``show_profile`` is its only entry point, and
        MainScreen drives the two together when it needs to). The DETECTION
        lines belong to the LIVE window rather than the selected one, and only
        the detector machinery writes them (``update_stale`` /
        ``update_template``): a stored region says a window was drawn, not that
        anything is watching it, and painting "watching the chat region" from a
        tab click is how "finish detection off" used to get overwritten with a
        claim that was false for the whole session.
        """
        self.update_region(cal.chat_region)
        self.update_slot_note(note)

    def update_slot_note(self, note: str) -> None:
        """Repaint just the readiness line (after a single calibration landed)."""
        self.query_one("#side-slot-note", Static).update(Text(note))

    # -- the chat region ------------------------------------------------------

    def update_region(self, region: ScreenRegion | None) -> None:
        """Show the session's drawn chat region - the window that hosts the
        chatbot, and the fallback the post-response click uses when no click
        region is drawn (display only; MainScreen owns it)."""
        text = f"{region.describe()} · chatbot window" if region is not None else _REGION_UNSET
        self.query_one("#side-region", Static).update(Text(text))

    # -- the service's captured appearances -----------------------------------

    def show_profile(self, profile: ServiceProfile) -> None:
        """Repaint the SELECTED tab's appearance summary.

        The counterpart of ``show_slot``, and the reason the two are separate
        methods: they are repainted by different events. A slot switch changes
        the block above and nothing here; a service switch, or an edit that
        captured or forgot appearances, changes this and nothing there.

        The probe lines under it used to be reset here as well, on the grounds
        that a different service's verdicts say nothing about this one. That is
        true of the service the poller is RUNNING, which is the live window's -
        not the selected tab's, and clicking between tabs mid-delegation would
        wipe the sub-agent's live readout. Resetting them belongs to the thing
        that rebuilds the detectors, and it happens there.
        """
        self.query_one("#side-profile-note", Static).update(Text(profile_summary(profile)))

    # -- the MCP servers -------------------------------------------------------

    def show_mcp(self, statuses: Sequence[McpServerStatus]) -> None:
        """Repaint the MCP block from a fresh ``McpManager.statuses()`` tuple.

        Display only, like ``show_profile``: MainScreen owns the manager and
        the hook that hears its transitions; this only words the lines. Rows
        are matched by config-order index - statuses() promises that order, and
        the server set is fixed for the process - and a widget-less status (a
        manager handed in after compose, which production never does) is
        dropped rather than mounted: the block's existence was decided when the
        column was built.
        """
        if not self._mcp_statuses:
            return
        for index, status in enumerate(statuses[: len(self._mcp_statuses)]):
            self.query_one(f"#{mcp_row_id(index)}", Static).update(Text(mcp_line(status)))

    def show_detection_window(self, window_name: str) -> None:
        """Name the window the DETECTION lines below are about.

        The LIVE window, written by the detector machinery whenever it rebuilds.
        Without it the block reads as the selected tab's, which is wrong for
        exactly as long as a delegation lasts - the one time the readout matters
        most.
        """
        self.query_one("#side-detection-title", Static).update(Text(detection_title(window_name)))

    def update_template(self, kind: TemplateKind, text: str) -> None:
        """Repaint one detector's live status line, named as it goes in.

        Display only, and deliberately text rather than data: the busy/idle
        detectors report every poll here, the auto-copy flow reports every
        click attempt and the send gate reports which phase it is in, and only
        MainScreen knows how to word them. Kinds nothing here DECIDES from (the
        two chat boxes, the new-chat button) have no line and are silently
        ignored: the clicks that use them report by toast, and the pictures of
        every kind - those three included - are the ELEMENTS column's job.
        """
        if kind not in DETECTOR_LABEL:
            return
        self.query_one(f"#{template_status_id(kind)}", Static).update(
            Text(self._probe_line(kind, text))
        )

    # -- the staleness detector ---------------------------------------------------

    def update_stale(self, text: str) -> None:
        """Repaint the live stale-detector readout - the chat region's
        frame-to-frame stability, where "unchanged long enough" means the
        response has finished. Readout only: there is no button, because the
        drawn chat region IS this detector's whole calibration (display only;
        MainScreen owns the tracker and formats the text - CHANGING/STALE/
        ERROR, or the no-region default)."""
        self.query_one("#side-stale", Static).update(Text(text))

    # -- the paste flash --------------------------------------------------------

    def show_paste_flash(self, text: str = PASTE_FLASH_TEXT, *, retry: bool = False) -> None:
        """Turn on the blinking banner: either the outbound payload still needs
        a manual Ctrl+V (``PASTE_FLASH_TEXT``), or AgentClip already pasted it
        and only Enter is left (``ENTER_FLASH_TEXT``). Obnoxious by design -
        the user is staring at the browser, not at us.

        ``retry`` offers the "Retry insert" button under the banner: the insert
        did not land, and re-running it is a one-press alternative to the Ctrl+V
        the banner is asking for. It is a parameter rather than something this
        widget infers from ``text`` because the widget is dumb on purpose -
        whether there is an insert worth retrying is MainScreen's fact, not a
        property of the words on the banner.
        """
        flash = self.query_one("#side-paste-flash", Static)
        flash.update(Text(text))
        flash.display = True
        self.query_one("#retry-insert-btn", Button).display = retry
        if self._flash_timer is None:
            self._flash_timer = self.set_interval(_FLASH_BLINK_S, self._blink_paste_flash)
        else:
            self._flash_timer.resume()

    def hide_paste_flash(self) -> None:
        """The paste happened (busy region went MATCH) or the moment passed
        (new capture, session reset) - stop nagging.

        The retry button goes with it, unconditionally: every caller here is
        saying the outbound has moved on, and a button that would click into the
        chat and paste it a second time has become the wrong offer.
        """
        flash = self.query_one("#side-paste-flash", Static)
        flash.display = False
        flash.remove_class("flash-alt")
        self.query_one("#retry-insert-btn", Button).display = False
        if self._flash_timer is not None:
            self._flash_timer.pause()

    def _blink_paste_flash(self) -> None:
        self.query_one("#side-paste-flash", Static).toggle_class("flash-alt")

    # -- the DISARMED banner ----------------------------------------------------

    def show_armed_state(self, armed: bool) -> None:
        """Show or hide the standing DISARMED banner (MainScreen owns the flag).

        One call for both directions rather than a show/hide pair like the
        flash's: this banner mirrors a boolean that is always one thing or the
        other, and the caller repaints it from that boolean on every toggle -
        there is no "the moment passed" half to hide separately.
        """
        self.query_one("#side-armed-banner", Static).display = not armed

    def refresh_services(self, config: Config | None = None) -> None:
        """Rebuild the options after the services table changed (service editor hook).

        Keeps the current selection when that preset survived the edit, and
        reports a switch ONLY when it did not.

        Both halves matter. ``set_options`` resets the value to the first option
        before the line below can put the selection back, so the rebuild is a
        round trip through Select's own value handling that says nothing about
        what the user chose; left to speak for itself it announced two service
        switches for an edit that changed nothing - a repaint against the wrong
        profile, a spurious "sub-agent slot ready" toast, and two detector
        restarts. The raw echoes are dropped by ``_on_service_changed`` (see
        there), ``_reported_service`` is brought in line here so the surviving
        selection cannot re-announce itself later, and the one real question -
        is what is selected now a different service than before? - is answered
        outright.
        """
        if config is not None:
            self._config = config
        select = self.service_select
        before = self.service
        select.set_options(_service_options(self._config))
        select.value = before if before in self._config.services else self._default_service()
        after = self.service
        self._reported_service = after
        self.query_one("#side-service-label", Static).update(Text(self._preset_caption(after)))
        if after != before:
            self.post_message(self.ServiceChanged(after))
