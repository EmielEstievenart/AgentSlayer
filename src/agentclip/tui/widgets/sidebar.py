"""Sidebar: the right-hand settings column on MainScreen (tui.md section 1.3).

Replaces the old new-session modal. The service ("profile") picker lives here
permanently instead of behind a launch dialog: at launch the user sees an empty
chat with the composer focused, and the sidebar tells them *which* service the
first message will start a session against. The Select is locked while a session
runs (a session's preset is fixed - its budget is baked into the engine) and
unlocks again whenever the app is waiting for a new session's first message.

The widget is dumb on purpose: it holds no session state, exposes ``service``
(the chosen preset key), ``set_locked``, ``refresh_services``, ``show_slot``,
``show_profile``, ``update_region``, ``update_template``, ``update_stale`` and
the ``show_paste_flash``/``hide_paste_flash`` pair; MainScreen owns every bit of
routing. The paste flash is the one animated thing here - a deliberately
obnoxious blinking banner that nags the user to Ctrl+V the outbound payload
into the chat; the blink timer is pure presentation, so the dumb widget may own
it.

The column is **live status plus the two things you steer with**, and nothing
that has to be configured. Capturing what a service looks like, and ticking
which finish signals it may run, both moved into the service editor (F2): they
are per-service settings that belong next to the service's other settings, and
six capture buttons with six status lines had grown into two thirds of a 32-cell
column. What is left is:

* **SERVICE** - the picker, its caption and the read-only appearance summary.
  All three describe the **selected window tab's** service (tui.md 1.6): the
  two browser windows are pointed at a service each, and the tab bar is what
  chooses between them, so there is no AGENT SLOT picker here any more.
  ``show_service`` writes the selected tab's key in without announcing a switch.
* **CHAT WINDOW** - what the selected tab's window is, and the readiness line
  under it. ``show_slot`` repaints the block from one slot in one go. Unlike the
  service picker it is never locked: drawing the sub-agent window mid-session is
  the normal way to reach delegation.
* **DETECTION** - four read-only lines the running automation writes into: the
  busy and idle probes (``update_template``), the staleness verdict
  (``update_stale``), and what the auto-copy flow's last click attempt did.
  Only these three ``TemplateKind`` values have anything to say at runtime; the
  two chat boxes and the new-chat button are found on demand and report through
  toasts, so they have no line here.
* One read-only **appearance summary** under the service picker
  (``show_profile``) - "appearance: 4/6 captured" - so "is this service usable
  at all?" is answerable at a glance without opening the editor. It follows the
  picker, so switching tabs can change what it says: two windows on two
  services have two sets of captures.

Every status label carries the ``side-status`` class so the column reads as one
list rather than a pile of one-off ids.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Button, Select, Static

from agentclip.config import Config
from agentclip.screen.profile import ServiceProfile, TemplateKind
from agentclip.screen.region import ScreenRegion
from agentclip.screen.slot import AgentSlot, SlotCalibration, can_delegate, missing

_HINT = "F3 hides this column · F2 settings · F1 help"
PASTE_FLASH_TEXT = ">>> PRESS CTRL+V <<<\nin the chat, then send"
ENTER_FLASH_TEXT = ">>> PRESS ENTER <<<\nreply pasted - just send it"
_FLASH_BLINK_S = 0.4
_REGION_UNSET = "not set - alt-tab to the chat yourself"
# The one read-only line about what the selected service LOOKS like. The
# captures themselves live in the service editor now, so this says only whether
# there are enough of them to be useful, and where to go about it.
PROFILE_HINT = " · F2 to capture"


def profile_summary(profile: ServiceProfile) -> str:
    return f"appearance: {profile.describe()}{PROFILE_HINT}"


# The stale detector has nothing to capture - it watches the drawn window stop
# changing - so its readout has only these two resting states, plus whatever
# live verdict the poller paints over them.
STALE_UNSET = "no chat region - staleness check disabled"
STALE_CALIBRATED = "watching the chat region"
# ...plus one more, which is not about the stale detector at all: the service's
# finish-signal checklist leaves NOTHING running (empty, or asking only for
# appearances it has none of). It goes here because this is the line the finish
# verdict is read off, and "auto-copy will never fire" has to be visible
# somewhere other than in a copy that never arrives.
STALE_OFF = "finish detection off for this service"

# The DETECTION block: three of the six appearances have something to say while
# the automation runs, and each gets one line. The lines are otherwise
# indistinguishable verdicts stacked on top of each other, so the widget names
# each one as it paints it.
DETECTOR_LABEL = {
    TemplateKind.BUSY: "busy",
    TemplateKind.IDLE: "idle",
    TemplateKind.COPY: "copy",
}
# What those lines say before anything has run. Nothing about capture state:
# that is the summary line's job, and the editor's.
PROBE_RESTING = "no verdict yet"
COPY_RESTING = "no click yet"


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
        return str(project_root)


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
    """

    class ServiceChanged(Message):
        """The user picked a different service.

        Its appearances are a different set, so this is not a display-only
        change: MainScreen reloads the profile behind it.
        """

        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    def __init__(self, config: Config, project_root: Path, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._config = config
        self._project_root = project_root
        self._flash_timer: Timer | None = None
        # The service the ServiceChanged message last reported. Textual fires
        # Select.Changed for the value compose sets as readily as for a user's
        # pick, and MainScreen reloads a profile and restarts its detector
        # worker on every one it hears about - so this is what keeps the
        # widget's own writes from reading as a switch.
        self._reported_service = self._default_service()

    def compose(self) -> ComposeResult:
        yield Static(Text(PASTE_FLASH_TEXT), id="side-paste-flash")
        yield Static(Text("PROJECT"), classes="side-title")
        yield Static(Text(_short_root(self._project_root)), id="side-root")
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
        yield Static(Text("DETECTION"), classes="side-title")
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
        slot reduction means the drawn window and the staleness detector that
        rides on it. The appearance summary is deliberately NOT repainted here:
        it belongs to the service, and a tab switch may or may not be a service
        switch (``show_profile`` is its only entry point, and MainScreen drives
        the two together when it needs to). The stale line falls back to a
        static "watching" here rather than a live verdict: a probe belongs to
        whichever window the automation is actually driving, and re-deriving one
        from a stored region would be a lie.
        """
        self.update_region(cal.chat_region)
        self.update_slot_note(note)
        self.update_stale(STALE_CALIBRATED if cal.chat_region is not None else STALE_UNSET)

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
        """Repaint everything that depends on WHICH service is selected.

        The counterpart of ``show_slot``, and the reason the two are separate
        methods: they are repainted by different events. A slot switch changes
        the block above and nothing here; a service switch, or an edit that
        captured or forgot appearances, changes this and nothing there.

        Two things, one call, because both are stale for the same reason: the
        summary of how much of the new service is captured, and the live probe
        lines - whose verdicts belonged to the detectors that were watching for
        the OLD service's appearances and say nothing about these.
        """
        self.query_one("#side-profile-note", Static).update(Text(profile_summary(profile)))
        self.update_template(TemplateKind.BUSY, PROBE_RESTING)
        self.update_template(TemplateKind.IDLE, PROBE_RESTING)
        self.update_template(TemplateKind.COPY, COPY_RESTING)

    def update_template(self, kind: TemplateKind, text: str) -> None:
        """Repaint one detector's live status line, named as it goes in.

        Display only, and deliberately text rather than data: the busy/idle
        detectors report every poll here and the auto-copy flow reports every
        click attempt, and only MainScreen knows how to word them. Kinds with no
        runtime status (the two chat boxes, the new-chat button) have no line
        and are silently ignored - they are found on demand and report by toast.
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

    def show_paste_flash(self, text: str = PASTE_FLASH_TEXT) -> None:
        """Turn on the blinking banner: either the outbound payload still needs
        a manual Ctrl+V (``PASTE_FLASH_TEXT``), or AgentClip already pasted it
        and only Enter is left (``ENTER_FLASH_TEXT``). Obnoxious by design -
        the user is staring at the browser, not at us."""
        flash = self.query_one("#side-paste-flash", Static)
        flash.update(Text(text))
        flash.display = True
        if self._flash_timer is None:
            self._flash_timer = self.set_interval(_FLASH_BLINK_S, self._blink_paste_flash)
        else:
            self._flash_timer.resume()

    def hide_paste_flash(self) -> None:
        """The paste happened (busy region went MATCH) or the moment passed
        (new capture, session reset) - stop nagging."""
        flash = self.query_one("#side-paste-flash", Static)
        flash.display = False
        flash.remove_class("flash-alt")
        if self._flash_timer is not None:
            self._flash_timer.pause()

    def _blink_paste_flash(self) -> None:
        self.query_one("#side-paste-flash", Static).toggle_class("flash-alt")

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
