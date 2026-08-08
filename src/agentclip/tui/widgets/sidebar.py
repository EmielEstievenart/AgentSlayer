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

The column has exactly two kinds of row below the pickers, and keeping them
visually apart is the whole design:

* **CHAT WINDOW** - the one per-slot block. The AGENT SLOT picker above it
  chooses which window "Set chat region..." writes into, and ``show_slot``
  repaints the block from that slot in one go. Unlike the service picker it is
  never locked: drawing the sub-agent window mid-session is the normal way to
  reach delegation. The staleness readout lives here too, because the drawn
  window IS that detector.
* **APPEARANCE · <service>** - what the selected service looks like, shared by
  both slots and persisted to disk. One "Capture <thing>..." button per
  ``TemplateKind`` in declaration order, each with a status line, a
  "4/6 captured" summary and a "Forget these templates" escape hatch. Every
  button carries the ``capture-btn`` class and encodes its kind in its id
  (``capture-<kind>-btn``), so MainScreen routes all six through one handler
  instead of six near-identical ones.

Two of the six mirror each other deliberately: the chat input box is captured
TWICE (a fresh chat centres it, an ongoing one docks it at the bottom, and one
appearance would be wrong half the time), and the busy/idle finish detectors
read from either end - something on screen only WHILE generating, and something
on screen only while IDLE - so a service with both gets a reinforced verdict.
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
# What an appearance the service profile does not hold yet reads as. One line
# for all of them: they are captured the same way and lost the same way, and
# the per-kind advice belongs on the picker prompt (TemplateKind.prompt), where
# the user is actually being asked to draw the box.
TEMPLATE_UNSET = "not captured"
# The stale detector has nothing to capture - it watches the drawn window stop
# changing - so its readout has only these two resting states, plus whatever
# live verdict the poller paints over them.
STALE_UNSET = "no chat region - staleness check disabled"
STALE_CALIBRATED = "watching the chat region"

# The APPEARANCE block is generated per TemplateKind, so its widget ids are a
# naming convention rather than a table: MainScreen parses the kind back out of
# a pressed button's id, which is what lets one handler serve all six.
CAPTURE_CLASS = "capture-btn"


def capture_button_id(kind: TemplateKind) -> str:
    return f"capture-{kind}-btn"


def template_status_id(kind: TemplateKind) -> str:
    return f"side-tpl-{kind}"


def kind_from_button_id(button_id: str | None) -> TemplateKind | None:
    """The appearance a ``capture-btn`` press is about, or None if it is not one."""
    if button_id is None or not button_id.startswith("capture-") or not button_id.endswith("-btn"):
        return None
    try:
        return TemplateKind(button_id[len("capture-") : -len("-btn")])
    except ValueError:
        return None


def template_status(profile: ServiceProfile, kind: TemplateKind) -> str:
    """One appearance's resting line: its captured size, or the not-set default."""
    template = profile.get(kind)
    if template is None:
        return TEMPLATE_UNSET
    return f"{template.width}×{template.height} · captured"

# The AGENT SLOT note, one line per state. The master slot has nothing to be
# "ready" for - it is simply the chat the session runs in - so only the
# sub-agent slot reports readiness, and it reports the gaps by name.
SLOT_NOTE_MASTER = "the main agent's chat window"
# The APPEARANCE block's heading names the service it belongs to, because that
# is the one thing about it a user could otherwise get wrong: these captures
# are not the slot's and not the app's.
PROFILE_TITLE = "APPEARANCE · "
SLOT_NOTE_READY = "delegation ON"
SLOT_NOTE_MISSING = "delegation off · need: "


def profile_title(key: str) -> str:
    return PROFILE_TITLE + key


def _slot_options() -> list[tuple[str, str]]:
    return [(slot.label, str(slot)) for slot in AgentSlot]


def slot_note(cal: SlotCalibration, profile: ServiceProfile) -> str:
    """The one-line readiness readout under the slot picker.

    Two inputs because readiness has two halves now: the box this slot's window
    was drawn as, and what the service it is pointed at looks like.
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
           and every button below it must keep its screen position. */
        height: 3;
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

    class SlotChanged(Message):
        """The user picked a different agent slot for the calibration buttons.

        MainScreen owns the slot pointers; the sidebar only reports the choice.
        """

        def __init__(self, slot: AgentSlot) -> None:
            self.slot = slot
            super().__init__()

    def __init__(self, config: Config, project_root: Path, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._config = config
        self._project_root = project_root
        self._flash_timer: Timer | None = None
        # The service the ServiceChanged message last reported. Textual fires
        # Select.Changed when the initial value is set in compose, and again
        # whenever refresh_services re-selects the same key - neither is a user
        # switching services, and MainScreen restarts its detector worker on
        # every one it hears about.
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
        yield Button("Edit services...", id="edit-services-btn", variant="primary")
        yield Static(Text("AGENT SLOT"), classes="side-title")
        yield Select(
            _slot_options(),
            value=str(AgentSlot.MASTER),
            allow_blank=False,
            id="slot-select",
        )
        yield Static(Text(SLOT_NOTE_MASTER), id="side-slot-note", classes="side-status")
        yield Static(Text("CHAT WINDOW"), classes="side-title")
        yield Button("Set chat region...", id="set-region-btn")
        yield Static(Text(_REGION_UNSET), id="side-region", classes="side-status")
        yield Static(Text(STALE_UNSET), id="side-stale", classes="side-status")
        yield Static(Text(profile_title(self._default_service())), id="side-profile-title",
                     classes="side-title")
        for kind in TemplateKind:
            yield Button(f"Capture {kind.label}...", id=capture_button_id(kind),
                         classes=CAPTURE_CLASS)
            yield Static(
                Text(TEMPLATE_UNSET), id=template_status_id(kind), classes="side-status"
            )
        yield Static(Text(""), id="side-profile-note", classes="side-status")
        yield Button("Forget these templates", id="forget-profile-btn")
        yield Button("New browser chat", id="newchat-btn")
        yield Static(Text(_HINT), classes="side-hint")

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
        # MainScreen has to reload the profile, repaint the APPEARANCE block and
        # restart the detectors against it.
        event.stop()
        value = event.value
        key = None if value is Select.NULL else str(value)
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

    def set_locked(self, locked: bool) -> None:
        """Lock the picker while a session owns the service; unlock between sessions.

        Only the *service* picker: the slot picker stays live for the whole
        session, because calibrating the sub-agent window mid-session is the
        normal way to reach delegation.
        """
        self.service_select.disabled = locked

    # -- the agent slot picker -------------------------------------------------

    @property
    def slot_select(self) -> Select[str]:
        return self.query_one("#slot-select", Select)

    @property
    def slot(self) -> AgentSlot:
        """The slot the calibration buttons currently write into."""
        value = self.slot_select.value
        return AgentSlot.MASTER if value is Select.NULL else AgentSlot(str(value))

    @on(Select.Changed, "#slot-select")
    def _on_slot_changed(self, event: Select.Changed) -> None:
        # Stop the raw Select event and re-post the domain one: MainScreen owns
        # the slot pointers and repaints the column via show_slot().
        event.stop()
        if event.value is Select.NULL:
            return
        self.post_message(self.SlotChanged(AgentSlot(str(event.value))))

    def show_slot(self, cal: SlotCalibration, note: str) -> None:
        """Repaint every calibration readout from one slot's stored state.

        Called when the slot picker moves and on session teardown - every
        readout below is a view of ``cal`` and nothing else, which after the
        slot reduction means the drawn window and the staleness detector that
        rides on it. The captured appearances are deliberately NOT repainted
        here: they belong to the service, not to a window, so switching slots
        must not change what they say (``update_template`` is their only entry
        point). The stale line falls back to a static "watching" here rather
        than a live verdict: a probe belongs to whichever slot the automation
        is actually driving, and re-deriving one from a stored region would be
        a lie.
        """
        select = self.slot_select
        if select.value != str(cal.slot):
            select.value = str(cal.slot)
        self.update_slot_note(note)
        self.update_region(cal.chat_region)
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
        """Repaint the whole APPEARANCE block from one service's profile.

        The counterpart of ``show_slot``, and the reason the two are separate
        methods: they are repainted by different events. A slot switch changes
        the block above and nothing here; a service switch or a fresh capture
        changes this block and nothing there.
        """
        self.query_one("#side-profile-title", Static).update(Text(profile_title(profile.key)))
        for kind in TemplateKind:
            self.update_template(kind, template_status(profile, kind))
        captured = profile.captured
        note = f"{profile.describe()} · saved for {profile.key}"
        self.query_one("#side-profile-note", Static).update(Text(note))
        # Nothing to forget reads as a disabled button rather than a hidden one:
        # the column must not reflow as the user captures things.
        self.query_one("#forget-profile-btn", Button).disabled = not captured

    def update_template(self, kind: TemplateKind, text: str) -> None:
        """Repaint one appearance's status line.

        Display only, and deliberately text rather than data: the same Static
        shows a stored fact most of the time ("captured", with the size of the
        box the user drew) and a live verdict for the rest (the busy/idle
        detectors report every poll here, the copy button reports every click
        attempt), and only MainScreen knows which.
        """
        self.query_one(f"#{template_status_id(kind)}", Static).update(Text(text))

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

        Keeps the current selection when that preset survived the edit.
        """
        if config is not None:
            self._config = config
        current = self.service
        select = self.service_select
        select.set_options(_service_options(self._config))
        select.value = current if current in self._config.services else self._default_service()
        self.query_one("#side-service-label", Static).update(
            Text(self._preset_caption(self.service))
        )
