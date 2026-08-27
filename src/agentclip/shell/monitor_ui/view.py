"""The calibration window's view: the service editor, the ELEMENTS column, the
chat-region picker and ``/identify``, over one local monitor.

``docs/design/ui-monitor.md`` §2.6 and §6.4. Everything here is a **calibration
surface** in that document's sense: it deals in PIXELS - crops, captured
appearances, a fullscreen overlay drawn on top of the real browser - and pixels
never ride the monitor wire (§2.2). So this object is constructed over a
:class:`~agentclip.driver.monitor.local.LocalUIMonitor` and nothing else: never
over a ``RemoteUIMonitor``, and never over the
:class:`~agentclip.driver.automation.controller.AutomationController`, which is
the brain's and knows what a state means. What the monitor is asked for here is
only what the machine can answer: capture this rectangle, hand me every frame
you recognise something in, stop polling while an overlay owns the screen.

**Why it is a second window rather than a panel.** In split mode the pixels are
on the VM and the chat GUI is on the operator's desk, so the two surfaces cannot
share a window even in principle. Making them two windows in local mode too is
what makes "same code, two entry points" true (§2.6).

**What is deliberately NOT here.** No session, no engine, no ``ChatView``, no
transcript, no clipboard watcher, no loop state. ``agentclip --calibrate``
builds a config, a clipboard provider and a monitor, and that is the whole
dependency list - which is also why this module imports neither
``agentclip.shell.app`` nor ``agentclip.shell.chat.view``.

The thread contract is the chat GUI's, for the same reason: :meth:`paint_elements`
and :meth:`_on_frame` are called on the monitor's POLL thread, and everything
they do is build a JSON event and append it to
:class:`~agentclip.shell.webview.bridge.Bridge`'s queue.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from agentclip.config import (
    Config,
    ServicePreset,
    default_global_config_path,
    default_profile_dir,
    save_services,
)
from agentclip.driver.monitor.protocol import MonitorSpec, SpecFor, spec_from_preset
from agentclip.driver.screen.capture import CaptureError, RegionImage, crop
from agentclip.driver.screen.detector import RUNTIME_KINDS, Sighting
from agentclip.driver.screen.identify import IdentifiedElement, identify_elements, summarise
from agentclip.driver.screen.matchers import select_matcher
from agentclip.driver.screen.picker import ScreenPickError, draw_identify_overlay, pick_region
from agentclip.driver.screen.profile import ServiceProfile, TemplateKind
from agentclip.driver.screen.profile_store import load_profile
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.monitor_ui.serve import ServePanel
from agentclip.shell.webview.bridge import Bridge
from agentclip.shell.webview.service_editor import ServiceEditor, kind_of, png_data_uri

Severity = Literal["information", "warning", "error"]

# The two windows a calibration can be about, and the words the page shows for
# them. Said here rather than imported from the chat GUI's view for the reason
# every display string in this package is said again: this window has to be
# runnable with no chat shell in the process at all (``agentclip --calibrate``).
MASTER = "MASTER"
SUBAGENT = "SUB-AGENT"
SLOT_LABELS: dict[AgentSlot, str] = {AgentSlot.MASTER: MASTER, AgentSlot.SUBAGENT: SUBAGENT}
SLOT_BY_NAME: dict[str, AgentSlot] = {label: slot for slot, label in SLOT_LABELS.items()}

# == the ELEMENTS column's words ==============================================
# ``ui-briefs/elements-panel.md``. Every kind, in the DETECTOR's own report
# order, so a row can never be mistaken for a picture of another row's search.

ELEMENT_ORDER: tuple[TemplateKind, ...] = RUNTIME_KINDS

ELEMENT_LABEL: dict[TemplateKind, str] = {
    TemplateKind.SEND_READY: "send button",
    TemplateKind.BUSY: "busy icon",
    TemplateKind.IDLE: "idle icon",
    TemplateKind.COPY: "copy button",
    TemplateKind.CHATBOX_INITIAL: "start chat box",
    TemplateKind.CHATBOX_ONGOING: "ongoing chat box",
    TemplateKind.NEW_CHAT: "new-chat button",
}

# Three row states, and the distinction is the panel's whole point: "nothing has
# been looked for" and "we looked and it is not there" are opposite readings of
# the same blank space (§3.1).
ELEMENT_RESTING = "no match yet"
ELEMENT_MISSING = "not on screen"

STATE_RESTING = "resting"
STATE_MISSING = "missing"
STATE_FOUND = "found"

# What the user is asked to draw for a window. Generous rather than tight -
# everything else is recognised inside it, including the new-chat button, which
# most chat sites park in a sidebar.
CHAT_REGION_PROMPT = (
    "Drag a box around the WHOLE browser window hosting the chat - including its "
    "sidebar, so the New Chat button is inside it · Esc cancels"
)

REGION_UNSET = "not set"
PICKER_BUSY = "a region picker is already open - finish it or press Esc first"
NO_REGION_TO_IDENTIFY = (
    'no chat window drawn for this window - use "Set chat region..." first; '
    "there is nothing to identify inside yet"
)


@dataclass(frozen=True, slots=True)
class ElementCrop:
    """One matched appearance: the pixels that matched, and how well.

    The image is the crop UNTOUCHED, at the size the screenshot has it - a page
    has one rendering path and CSS to fit with, so there is nothing to decide on
    this side of the bridge (elements-panel.md §4.4, §7).
    """

    image: RegionImage
    diff: float


def element_crop(scene: RegionImage, sighting: Sighting | None) -> ElementCrop | None:
    """Cut a verified match out of the frame it was found in. POLL-THREAD side.

    ``None`` in, ``None`` out - "nothing matched" and "the match is too
    degenerate to draw" are the same row, and there is nothing useful to tell
    apart.
    """
    if sighting is None:
        return None
    template, match = sighting.template, sighting.match
    cut = crop(scene, match.x, match.y, template.width, template.height)
    if cut.width <= 0 or cut.height <= 0:
        return None
    return ElementCrop(cut, match.diff)


def element_png(image: RegionImage) -> str:
    """One crop as a ``data:`` URI an ``<img>`` can be pointed straight at.

    ``""`` for anything that cannot be encoded (a truncated buffer, a zero-area
    cut). The row still says ``found`` with its diff: the search DID match, and
    blanking the verdict because the picture failed would report the opposite
    (elements-panel.md §6.3).
    """
    return png_data_uri(image)


def found_line(diff: float) -> str:
    """What a matched row says under its name."""
    return f"found · {diff:.1%}"


class CalibrationMonitor(Protocol):
    """The subset of ``LocalUIMonitor`` this window drives.

    Named at the call site rather than taken as ``LocalUIMonitor`` for the
    reason ``AutomationController.MonitorLike`` is: half of what is here is the
    **local-only tier** (``capture``, ``on_frame``, ``saved_region``) that a
    ``RemoteUIMonitor`` will never answer, and stating the subset is what makes
    "this window is always local" a type error rather than a comment.

    ``saved_region`` is the region store's READ (``driver/monitor/regions.py``):
    where this machine last saw a service's chat window. It answers ``None`` for
    a monitor that remembers nothing.

    ``set_spec_for`` is §10.5's seam and the reason this window is not merely a
    reader: what a brain gets back from ``watch(slot)`` is composed by the
    callable installed here, so the service picked and the box drawn in THIS
    window are what the far Chat UI is told it is driving. One state, two
    surfaces - which is the disagreement wave 3 exists to end.
    """

    async def configure(self, spec: MonitorSpec) -> int: ...
    def set_spec_for(self, spec_for: SpecFor | None) -> None: ...
    async def suspend(self) -> None: ...
    async def resume(self) -> None: ...
    async def close(self) -> None: ...
    def capture(self, region: ScreenRegion) -> RegionImage: ...
    def saved_region(self, service: str) -> ScreenRegion | None: ...
    def on_frame(
        self,
        hook: Callable[[RegionImage, Mapping[TemplateKind, Sighting | None]], None],
    ) -> Callable[[], None]: ...


class CalibrationView:
    """Everything the calibration window decides, with no window behind it."""

    def __init__(
        self,
        bridge: Bridge,
        *,
        config: Config,
        monitor: CalibrationMonitor,
        profile_root: Path | None = None,
        global_config_path: Path | None = None,
        schedule: Callable[[Coroutine[Any, Any, Any]], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        on_config_change: Callable[[Config], None] | None = None,
        on_calibration: Callable[[AgentSlot, ScreenRegion | None], None] | None = None,
        serve: ServePanel | None = None,
    ) -> None:
        self._bridge = bridge
        self._config = config
        self._monitor = monitor
        # §10.5: the window IS the monitor's configuration. Installed here rather
        # than at :meth:`start`, so a ``watch`` that arrives over the wire before
        # the page has finished loading is still answered with this window's
        # selection instead of with the config file's.
        monitor.set_spec_for(self.spec_for)
        self._profile_root = profile_root if profile_root is not None else default_profile_dir()
        self._global_config_path = (
            global_config_path if global_config_path is not None else default_global_config_path()
        )
        self._schedule = schedule if schedule is not None else _no_schedule
        self._on_exit = on_exit if on_exit is not None else _no_exit
        # How a saved preset table reaches whoever built the engine factory. In
        # standalone mode nobody is listening and the write to config.toml IS
        # the whole propagation; in local mode (phase 4B) the chat GUI passes
        # its own ``_adopt_config`` here.
        self._on_config_change = on_config_change if on_config_change is not None else _no_config
        # How a drawn rectangle reaches the brain. The chat regions are the one
        # piece of calibration with NO persistent home today - they live in
        # ``AutomationController._slots`` for the life of a process and are
        # written nowhere (``driver/screen/slot.py:SlotCalibration``) - so this
        # callback is not a convenience: standalone, it is the only way out of
        # this window at all, and phase 5 is where the monitor grows a home for
        # it on its own machine.
        self._on_calibration = on_calibration if on_calibration is not None else _no_calibration

        # -- who may drive this machine ----------------------------------------
        # The Serve panel (ui-monitor.md 9.1), or None where there is nothing to
        # serve: the Chat UI opens this window over the monitor it is ALREADY
        # driving in local mode, and a second listener onto the same mouse is
        # not a feature. Standalone it is always there.
        #
        # Constructed by whoever built the monitor and bound HERE, because the
        # two things it needs are this view's: the loop its server has to live
        # on, and the page's event vocabulary.
        self._serve = serve
        if serve is not None:
            serve.bind(
                schedule=self._schedule,
                push=lambda state: self._bridge.send("serve", **state),
                notify=lambda message: self.notify(message),
            )

        # -- what is being calibrated ------------------------------------------
        self._slot = AgentSlot.MASTER
        self._regions: dict[AgentSlot, ScreenRegion | None] = {
            AgentSlot.MASTER: None,
            AgentSlot.SUBAGENT: None,
        }
        self._profiles: dict[str, ServiceProfile] = {}

        # -- the editor --------------------------------------------------------
        # Not a modal here, the way it is in the chat GUI: this window IS the
        # editor, so one is built at :meth:`start` and lives for the window's
        # life. Closing it closes the window (see :meth:`svc_close`).
        self._editor: ServiceEditor | None = None

        # -- the ELEMENTS column ------------------------------------------------
        # A kind ABSENT has never been searched (this service has no capture of
        # it), present-and-None was searched and not found, present-and-crop was
        # found. Written by the poll thread, read by it and by the loop thread -
        # one whole-dict rebind per frame rather than an in-place edit, so a
        # reader sees either the old frame or the new one.
        self._elements: dict[TemplateKind, ElementCrop | None] = {}
        self._elements_open = True
        self._element_pngs: dict[TemplateKind, tuple[RegionImage, str]] = {}
        self._drop_frames: Callable[[], None] | None = None

        # -- the one fullscreen child process at a time -------------------------
        # The region picker, ``/identify`` and the editor's capture buttons all
        # spawn the same blocking tkinter child (``driver/screen/picker.py``),
        # and cancelling a task cannot kill one - so the only safe guard against
        # two stacked overlays is refusing the second ask.
        self._picker_open = False

        # -- blocking prompts (the confirm the editor asks for) ------------------
        self._prompts: dict[str, asyncio.Future[Any]] = {}
        self._prompt_ids = itertools.count(1)

    # == reading (for tests and for the hosting shell) =========================

    @property
    def config(self) -> Config:
        return self._config

    @property
    def slot(self) -> AgentSlot:
        """Which window this visit is calibrating."""
        return self._slot

    @property
    def region(self) -> ScreenRegion | None:
        """The rectangle drawn for the selected window, or None."""
        return self._regions[self._slot]

    @property
    def editor(self) -> ServiceEditor | None:
        return self._editor

    @property
    def serve(self) -> ServePanel | None:
        """The Serve panel, or None for a window with nothing to serve."""
        return self._serve

    # == lifecycle =============================================================

    def start(self) -> None:
        """The page finished loading: wire the frame hook and paint everything.

        The frame hook FIRST, so no tick of the poller can land in the gap
        between "the window exists" and "the column is listening".
        """
        if self._drop_frames is None:
            self._drop_frames = self._monitor.on_frame(self._on_frame)
        self._seed_region()
        self.open_service_editor()
        self._push_all()
        self._retarget()
        # Last, and only when a command line asked for it: the first thing the
        # page paints is then a panel that is already listening rather than one
        # that starts a beat later under the reader's eyes.
        if self._serve is not None:
            self._serve.start_if_requested()

    def page_ready(self) -> None:
        """A reload: repaint every surface from the state we already hold."""
        self._push_all()

    def set_region(self, slot: AgentSlot, region: ScreenRegion | None) -> None:
        """Adopt a rectangle somebody else drew (the chat GUI, at hand-off).

        Deliberately does NOT call back out through ``on_calibration``: this is
        the way IN, and echoing it would send the brain its own answer.
        """
        self._regions[slot] = region
        if slot is self._slot:
            self._retarget()
        self._push_calibration()

    async def close(self) -> None:
        """Stop listening and end the monitor's threads for good. Idempotent.

        The Serve panel's listener goes FIRST and on this loop: it is bound to
        the loop that is about to stop, and a socket left listening would hold
        the port against the next launch of this very window.
        """
        if self._serve is not None:
            await self._serve.close()
        drop, self._drop_frames = self._drop_frames, None
        if drop is not None:
            drop()
        await self._monitor.close()

    # == the Serve panel =======================================================
    # Four verbs, each one a forward: the panel owns the server, the token and
    # every sentence about them (serve.py), and the view owns only the fact that
    # a page can ask.

    def serve_start(self, address: str, port: int, no_token: bool) -> None:
        if self._serve is not None:
            self._serve.start(address, port, no_token)

    def serve_stop(self) -> None:
        if self._serve is not None:
            self._serve.stop()

    def token_copy(self) -> str:
        """The token, ANSWERED rather than pushed: the page writes it to the
        clipboard, which is a thing only a real user gesture may do."""
        return "" if self._serve is None else self._serve.token

    def token_regenerate(self) -> None:
        if self._serve is not None:
            self._serve.regenerate()

    # == the window's chrome ===================================================

    def select_slot(self, name: str) -> None:
        """The MASTER / SUB-AGENT picker: which window is being calibrated."""
        slot = SLOT_BY_NAME.get(name)
        if slot is None or slot is self._slot:
            return
        self._adopt_slot(slot)
        self._retarget()

    def _adopt_slot(self, slot: AgentSlot) -> None:
        """Show ``slot`` and repaint every surface that is about it.

        Everything :meth:`select_slot` does EXCEPT the retarget, because the
        other caller is :meth:`spec_for` - where the retarget is the very call
        being answered, and asking for a second one would configure the monitor
        twice for one ``watch``.
        """
        self._slot = slot
        self._seed_region()
        self._clear_elements()
        self._push_calibration()

    def set_elements_visible(self, visible: bool) -> None:
        """The column opened or closed.

        The crops keep being cut while it is hidden, so opening it shows the
        CURRENT frame rather than the next one; what hiding stops is the one
        part that is not free - a PNG per matched appearance, twice a second
        (elements-panel.md §3.1).
        """
        was_open = self._elements_open
        self._elements_open = visible
        if visible and not was_open:
            self._push_elements()

    def request_close(self) -> None:
        """The window's own close button: the same door ``svc_close`` is, so a
        close can never skip the editor's apply/confirm path."""
        self.svc_close()

    # == the two fullscreen child processes ====================================
    # Same shape, one mutual-exclusion flag: a translucent, always-on-top tkinter
    # window in a CHILD PROCESS, because no shell of ours can host tkinter. That
    # mechanism is shell-agnostic and carries over verbatim.

    def _refuse_second_picker(self) -> bool:
        """True (and toast) when an overlay is already up."""
        if self._picker_open:
            self.notify(PICKER_BUSY, severity="warning")
            return True
        self._picker_open = True
        return False

    def set_chat_region(self) -> None:
        """"Set chat region...": draw the box the chatbot window lives in.

        The target slot is decided HERE, when the overlay opens, and travels
        with the flow: the overlay blocks for as long as the user takes to drag
        a box, and the picker on the page can move meanwhile.
        """
        if self._refuse_second_picker():
            return
        self._schedule(self._pick_chat_region(self._slot))

    async def _pick_chat_region(self, slot: AgentSlot) -> None:
        """Run the draw-a-box overlay and adopt what was drawn as ``slot``'s window.

        The detectors are suspended for the whole visit: this overlay is a
        fullscreen window thrown over the very browser they watch, and an
        overlay appearing and vanishing is precisely the sustained large delta
        that arms a finish trigger on staleness alone. ``await
        monitor.suspend()`` rather than a fire-and-forget schedule, because the
        very next line captures the screen and the suspension has to have
        happened by then (ui-monitor.md §6.4).
        """
        await self._monitor.suspend()
        try:
            region = await asyncio.to_thread(
                pick_region, prompt=self._slot_prompt(CHAT_REGION_PROMPT, slot)
            )
        except ScreenPickError as exc:
            self.notify(str(exc), severity="error")
            return
        else:
            if region is None:
                self.notify("chat region unchanged (selection cancelled)")
                return
            self._regions[slot] = region
            # The way out of this window: the brain is TOLD, because nothing
            # persists a chat region today (see ``_on_calibration``).
            self._on_calibration(slot, region)
            self._push_calibration()
            if slot is self._slot:
                self._retarget()
            self.notify(
                f"chat region set ({region.describe()}) - the chatbot window; "
                "everything is recognised inside it"
            )
        finally:
            self._picker_open = False
            # After the adoption above, so the common case has already
            # retargeted the monitor and this only puts the polling back.
            await self._monitor.resume()

    def show_identify_overlay(self) -> None:
        """Box every part of the selected chat window we can recognise.

        The debug view of the whole recognition model - everything the
        automation does is "find this captured appearance inside that drawn
        rectangle", and this draws the search's actual answer on the actual
        screen, next to the actual buttons.
        """
        if self._refuse_second_picker():
            return
        self._schedule(self._identify_window())

    async def _identify_window(self) -> None:
        """Capture the chat region, work out what is in it, draw the answer.

        The capture happens FIRST and exactly once, before any overlay exists:
        the overlay covers the browser, so a frame taken with it up would be
        identified as part of the chat window. The search runs with the same
        tolerance and matcher the poller uses - an overlay that searched with
        different settings would answer a question nobody asked
        (elements-panel.md §4.5).
        """
        try:
            region = self._regions[self._slot]
            if region is None:
                self.notify(NO_REGION_TO_IDENTIFY, severity="warning")
                return
            try:
                scene = await asyncio.to_thread(self._monitor.capture, region)
            except CaptureError as exc:
                self.notify(f"could not capture the chat window: {exc}", severity="error")
                return
            preset = self._preset()
            elements: list[IdentifiedElement] = await asyncio.to_thread(
                identify_elements,
                region,
                self._profile(self._service_key()),
                scene,
                tolerance=preset.tolerance,
                matcher=select_matcher(preset.matcher).origins,
            )
            await self._monitor.suspend()
            try:
                await asyncio.to_thread(draw_identify_overlay, elements)
            except ScreenPickError as exc:
                self.notify(str(exc), severity="error")
                return
            finally:
                await self._monitor.resume()
            # After the overlay is down, so the summary is readable rather than
            # painted behind it.
            self.notify(summarise(elements))
        finally:
            self._picker_open = False

    def _slot_prompt(self, prompt: str, slot: AgentSlot) -> str:
        """Both windows share the picker, so the sub-agent's prompts have to say
        out loud which window the user is being asked to draw on."""
        if slot is AgentSlot.SUBAGENT:
            return f"SUB-AGENT window · {prompt}"
        return prompt

    # == the service editor ====================================================
    # Every ``svc_*`` intent, verbatim from the chat GUI's view: the model owns
    # every refusal (it toasts them itself), so each of these is one call on it
    # followed by one ``editor`` event back.

    def open_service_editor(self) -> None:
        """Build the editor, once. Idempotent - this window is never without one."""
        if self._editor is not None:
            return
        self._editor = ServiceEditor(
            self._config,
            self._profile_root,
            self._service_key(),
            notify=self._editor_notify,
            confirm=self.confirm,
        )
        self._push_editor()

    def _editor_notify(self, message: str, severity: str) -> None:
        """The model's toast sink, widened to this view's ``notify`` shape."""
        self.notify(message, severity=cast(Severity, severity))

    def _push_editor(self) -> None:
        """The whole editor as one event. Closed is one field, not a second type."""
        editor = self._editor
        if editor is None:
            self._bridge.send("editor", open=False)
            return
        self._bridge.send("editor", **editor.state())

    def svc_select(self, key: str) -> None:
        if self._editor is None:
            return
        self._editor.select(key)
        self._push_editor()

    def svc_form(self, fields: dict[str, Any]) -> None:
        """A keystroke in the form column: the WHOLE candidate, revalidated.

        The page sends every field on any change because ``max <= total`` is a
        cross-field rule - there is no per-field validity to send.
        """
        if self._editor is None:
            return
        self._editor.set_form({k: str(v) for k, v in dict(fields).items()})
        self._push_editor()

    def svc_detection(self, state: dict[str, Any]) -> None:
        """Any toggle on the left column: all of them, folded in at once."""
        if self._editor is None:
            return
        self._editor.set_detection(
            signals=[str(name) for name in state.get("signals") or ()],
            hover_scan=bool(state.get("hover_scan")),
            require_fenced=bool(state.get("require_fenced")),
            stream=bool(state.get("stream")),
            auto_submit=bool(state.get("auto_submit")),
        )
        self._push_editor()

    def svc_edit_by_lines(self, on: bool) -> None:
        if self._editor is None:
            return
        self._editor.set_edit_by_lines(on)
        self._push_editor()

    def svc_after_delivery(self, state: dict[str, Any]) -> None:
        """Either tick in the AFTER DELIVERY block: both of them, at once."""
        if self._editor is None:
            return
        self._editor.set_after_delivery(
            snap_back=bool(state.get("snap_back")),
            alert_sound=bool(state.get("alert_sound")),
        )
        self._push_editor()

    def svc_scroll(self, action: str) -> None:
        if self._editor is None:
            return
        self._editor.set_scroll(action)
        self._push_editor()

    def svc_matcher(self, matcher: str) -> None:
        if self._editor is None:
            return
        self._editor.set_matcher(matcher)
        self._push_editor()

    def svc_tolerance(self, value: int) -> None:
        if self._editor is None:
            return
        self._editor.set_tolerance(value)
        self._push_editor()

    def svc_add(self) -> None:
        if self._editor is None:
            return
        self._editor.add()
        self._push_editor()

    def svc_reset(self) -> None:
        if self._editor is None:
            return
        self._editor.reset()
        self._push_editor()

    def svc_delete(self) -> None:
        if self._editor is None:
            return
        self._editor.delete()
        self._push_editor()

    def svc_prev(self, kind_name: str) -> None:
        """The arrow left of a thumbnail: show that kind's previous variant."""
        editor, kind = self._editor, kind_of(kind_name)
        if editor is None or kind is None:
            return
        editor.show_previous(kind)
        self._push_editor()

    def svc_next(self, kind_name: str) -> None:
        """The arrow right of a thumbnail: show that kind's next variant."""
        editor, kind = self._editor, kind_of(kind_name)
        if editor is None or kind is None:
            return
        editor.show_next(kind)
        self._push_editor()

    def svc_click_point(self, kind_name: str, x: int, y: int) -> None:
        """Where inside that appearance its click lands, written immediately."""
        editor, kind = self._editor, kind_of(kind_name)
        if editor is None or kind is None:
            return
        editor.set_click_point(kind, x, y)
        self._push_editor()

    def svc_clear(self, kind_name: str) -> None:
        """The variant on show, gone from disk. No confirm, by design."""
        editor, kind = self._editor, kind_of(kind_name)
        if editor is None or kind is None:
            return
        editor.clear(kind)
        self._profiles.clear()
        self._push_editor()

    def svc_forget(self) -> None:
        if self._editor is None:
            return
        self._schedule(self._svc_forget())

    async def _svc_forget(self) -> None:
        editor = self._editor
        if editor is None:
            return
        await editor.forget()
        self._profiles.clear()
        self._push_editor()

    def svc_capture(self, kind_name: str) -> None:
        """Draw a box around one appearance and file the pixels under this service.

        The claim is synchronous and the work is scheduled: two presses both
        marshal onto this loop as two callbacks, and if the flag were taken
        inside the coroutine neither would have seen the other's.
        """
        editor, kind = self._editor, kind_of(kind_name)
        if editor is None or kind is None:
            return
        if not editor.start_capture(kind):
            self._push_editor()
            return
        # The window-wide overlay flag too, so nothing else here can put a
        # second child process up while this one is drawing.
        self._picker_open = True
        self._push_editor()
        self._schedule(self._svc_capture(kind))

    async def _svc_capture(self, kind: TemplateKind) -> None:
        """The capture, bracketed by a real suspend.

        The bracket is per CAPTURE here, not per visit as it is in the chat
        GUI's modal: this window keeps the editor open for its whole life, and
        suspending for that long would leave the ELEMENTS column frozen at
        whatever the first frame said - which is the surface the user is
        calibrating AGAINST (ui-monitor.md §6.4).
        """
        editor = self._editor
        if editor is None:
            self._picker_open = False
            return
        await self._monitor.suspend()
        try:
            await editor.run_capture(kind)
        finally:
            self._picker_open = False
            self._profiles.clear()
            self._push_editor()
            await self._monitor.resume()
            # A new appearance changes what the poller can see, so the run it is
            # in the middle of is watching an out-of-date profile.
            self._retarget()

    def svc_close(self) -> None:
        """Esc, or the window's close button. May be refused - see the model."""
        if self._editor is None:
            self._on_exit()
            return
        self._schedule(self._svc_close())

    async def _svc_close(self) -> None:
        """Apply what validated, then leave.

        Two independent kinds of change come back and the propagation runs for
        either: the presets table, which is ours to write to config.toml, and
        captured appearances the editor already wrote or deleted on disk.

        Then the WINDOW closes, which is where this parts company with the chat
        GUI's modal: a calibration window with its editor closed would be an
        empty frame, and "Close" on a window means the window.
        """
        editor = self._editor
        if editor is None:
            return
        result = await editor.close()
        if not result.closed:
            self._push_editor()  # a capture is up, or the discard was declined
            return
        self._editor = None
        self._bridge.send("editor", open=False)
        if result.edits is not None:
            self._save(result.edits.services)
        self._on_exit()

    def _save(self, services: dict[str, ServicePreset] | None) -> None:
        """Persist an edited preset table and adopt it here.

        Every way the write can fail degrades to a toast and an adoption that
        still happened: the user's edit is real for this process even when the
        file it should outlive it in could not be written.
        """
        if services is None:
            self._adopt_config(self._config)
            self.notify("appearance updated", timeout=4)
            return
        saved = True
        try:
            save_services(services, self._global_config_path)
        except OSError as exc:
            saved = False
            self.notify(f"could not save the service presets: {exc}", severity="error", timeout=8)
        self._adopt_config(replace(self._config, services=services))
        if saved:
            self.notify("service presets saved", timeout=4)

    def _adopt_config(self, config: Config) -> None:
        """Take an edited config as this window's own, and hand it on.

        The hand-back matters even standalone: in local mode (phase 4B) it is
        how the chat GUI's engine factory learns about the edit, and the
        callback is the same shape ``run_gui`` already takes.
        """
        self._config = config
        self._on_config_change(config)
        # The editor can delete a service's captured appearances (and a service
        # itself), so the per-run cache is no longer trustworthy.
        self._profiles.clear()
        # An edit can move a window onto another service, and that service has a
        # remembered rectangle of its own.
        self._seed_region()
        self._push_calibration()
        self._retarget()

    # == the ELEMENTS column ===================================================

    def _on_frame(
        self, scene: RegionImage, sightings: Mapping[TemplateKind, Sighting | None]
    ) -> None:
        """One poll frame and what was recognised in it. POLL-THREAD.

        The cut runs HERE, on the thread that captured the frame, because what
        should reach a UI is an icon per appearance and not a whole chat window.
        """
        self.paint_elements(
            {kind: element_crop(scene, sighting) for kind, sighting in sightings.items()}
        )

    def paint_elements(self, crops: Mapping[TemplateKind, ElementCrop | None]) -> None:
        """One frame's recognitions into the column.

        A kind ABSENT from ``crops`` keeps whatever its row last said: the
        detector searches every calibrated kind on every frame, so the only
        reason a frame says nothing about one is that this service has no
        capture of it, and a frame that never looked must not blank a row
        (elements-panel.md §4.2). Present-and-``None`` is the opposite claim -
        the search ran and found nothing - and it clears the picture.
        """
        merged = dict(self._elements)
        for kind, found in crops.items():
            if kind not in ELEMENT_LABEL:
                # The floor under a TemplateKind added to the enum and not to
                # the label table: a lost row rather than a crashed poll tick.
                continue
            merged[kind] = found if isinstance(found, ElementCrop) else None
        self._elements = merged
        self._push_elements()

    def _push_elements(self) -> None:
        """The column, whole: one row per kind, in the detector's report order.

        Whole rather than per-kind because a row's state is only readable
        against the others - the two chat-box rows are EXPECTED to disagree.
        Raised from the poll thread, so it does what every paint here does:
        build and queue.
        """
        crops = self._elements
        rows: list[dict[str, Any]] = []
        for kind in ELEMENT_ORDER:
            row: dict[str, Any] = {"kind": kind.name, "label": ELEMENT_LABEL[kind]}
            if kind not in crops:
                row["state"] = STATE_RESTING
                row["text"] = ELEMENT_RESTING
            elif crops[kind] is None:
                row["state"] = STATE_MISSING
                row["text"] = ELEMENT_MISSING
            else:
                found = crops[kind]
                assert found is not None  # narrowed by the branch above
                row["state"] = STATE_FOUND
                row["text"] = found_line(found.diff)
                if self._elements_open:
                    png = self._element_png(kind, found.image)
                    if png:
                        row["png"] = png
            rows.append(row)
        self._bridge.send("elements", window=SLOT_LABELS[self._slot], rows=rows)

    def _element_png(self, kind: TemplateKind, image: RegionImage) -> str:
        """This row's crop as a data URI, re-encoded only when the pixels moved.

        The encode is what costs here, not the paint, and a still icon re-cut
        twice a second is the common case (elements-panel.md §6.8).
        """
        cached = self._element_pngs.get(kind)
        if cached is not None and cached[0] == image:
            return cached[1]
        png = element_png(image)
        self._element_pngs[kind] = (image, png)
        return png

    def _clear_elements(self) -> None:
        """Back to "no match yet", every row - the monitor was repointed.

        A crop cut from the old window under the new one's name is a
        straightforward lie. The rows refill on the new run's first frame.
        """
        self._elements = {}
        self._element_pngs.clear()
        self._push_elements()

    # == toasts and the one modal ==============================================

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
    ) -> None:
        """A toast, from whichever thread asked for it."""
        self._bridge.send(
            "toast", message=message, title=title, severity=severity, timeout=timeout
        )

    async def confirm(self, title: str, body: str = "") -> bool:
        """The editor's discard / forget question, as a page modal."""
        return bool(await self._modal("confirm", title=title, body=body))

    async def _modal(self, modal: str, **fields: Any) -> Any:
        """Open one modal and park on the answer the page sends back.

        Keyed by id rather than by "the modal that is up": a stale answer must
        resolve nothing rather than resolve the next question.
        """
        prompt_id = f"c{next(self._prompt_ids)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._prompts[prompt_id] = future
        self._bridge.send("modal", modal=modal, prompt_id=prompt_id, **fields)
        try:
            return await future
        finally:
            self._prompts.pop(prompt_id, None)
            self._bridge.send("modal_close", prompt_id=prompt_id)

    def answer_prompt(self, prompt_id: str, value: Any) -> None:
        future = self._prompts.get(prompt_id)
        if future is not None and not future.done():
            future.set_result(value)

    # == the monitor's target ==================================================

    def spec_for(self, slot: AgentSlot) -> MonitorSpec:
        """What this window watches for ``slot`` - the monitor's own ``spec_for``.

        Wave 3 §10.5: a brain names a window and the MONITOR answers with the
        service. On a machine with a Monitor UI the answer has to be this
        window's, not the config file's, because the config file does not know
        which service the operator just picked or which box they just drew.

        Which is why a ``watch`` for a slot this window is not showing SWITCHES
        it. The alternative is worse in both directions: answering for the other
        slot silently would leave the header, the ELEMENTS column and the
        service line describing a window nobody is watching, and refusing would
        make the brain's slot switch impossible to honour at all. Switching is
        also what the user wants to see - a delegation starting is exactly when
        the sub-agent's window is worth looking at.

        Called on the loop (the Serve panel's server runs on this view's own
        loop), so painting from here is the same thread every other paint uses.
        """
        if slot is not self._slot:
            self._adopt_slot(slot)
        return self._spec()

    def _spec(self) -> MonitorSpec:
        """What the monitor has to know to watch the SELECTED window.

        Scalars only, and every one of them read fresh: the service KEY (never
        the profile - the template PNGs are this machine's own business), the
        drawn rectangle, and the preset - the searching half for the poller, the
        acting half for whatever brain reads it back off ``Watched`` (§10.5).
        ``stable_seconds`` goes over RAW; converting it into a tick count
        belongs to whatever is doing the ticking (§2.10).
        """
        return spec_from_preset(
            self._preset(), self._regions[self._slot], service=self._service_key()
        )

    def _retarget(self) -> None:
        """Point the monitor at the selected window - the synchronous door.

        Every caller is chrome (a slot picked, a region drawn, a capture saved,
        a config adopted) and none of them can await, while ``configure`` is a
        coroutine by contract. So the door stays synchronous and the retarget
        goes on the loop, where it runs in the order it was asked for.
        """
        self._schedule(self._configure())

    async def _configure(self) -> None:
        await self._monitor.configure(self._spec())
        self._clear_elements()

    # == the page's other blocks ===============================================

    def _push_all(self) -> None:
        self._push_calibration()
        self._push_editor()
        self._push_elements()
        if self._serve is not None:
            self._serve.push()

    def _push_calibration(self) -> None:
        """The header block: which window, what is drawn for it, which service."""
        key = self._service_key()
        preset = self._config.services.get(key)
        region = self._regions[self._slot]
        self._bridge.send(
            "calib",
            slot=SLOT_LABELS[self._slot],
            slots=[SLOT_LABELS[AgentSlot.MASTER], SLOT_LABELS[AgentSlot.SUBAGENT]],
            region=region.describe() if region is not None else REGION_UNSET,
            service=key,
            service_label=preset.label if preset is not None else key,
        )

    # == small helpers =========================================================

    def _seed_region(self) -> bool:
        """Adopt what this machine remembers about the selected window, if the
        user has drawn nothing for it yet. True when a rectangle was taken up.

        Without this the window opens saying "not set" over a monitor that is
        about to poll the very rectangle it claims not to have - ``configure``
        fills a region-less spec from the store on its own
        (``local.py:_remember_region``) - and ``Identify`` refuses a window it
        can see perfectly well. The header and the store have to agree.

        A DRAWN rectangle always wins: this only ever fills a hole, so re-reading
        it on a slot switch or a config edit can never undo a drag.

        Deliberately silent towards ``on_calibration``: this came OUT of the
        monitor's own store, so telling the monitor about it would be echoing it
        its own answer - the same reason :meth:`set_region` does not echo.
        """
        if self._regions[self._slot] is not None:
            return False
        remembered = self._monitor.saved_region(self._service_key())
        if remembered is None:
            return False
        self._regions[self._slot] = remembered
        return True

    def _service_key(self) -> str:
        """Which service the SELECTED window is pointed at.

        Read off the global config rather than off a controller: this window has
        no session and therefore no per-window service map of its own.
        """
        general = self._config.general
        key = general.service if self._slot is AgentSlot.MASTER else general.subagent_service
        return key if key in self._config.services else general.service

    def _preset(self) -> ServicePreset:
        return self._config.services.get(self._service_key()) or self._config.preset()

    def _profile(self, key: str) -> ServiceProfile:
        """A service's captured appearances, read off disk once per window run."""
        profile = self._profiles.get(key)
        if profile is None:
            profile = load_profile(self._profile_root, key)
            self._profiles[key] = profile
        return profile


def _no_schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """The scheduler a view nobody wired a loop into gets: close the coroutine
    rather than leak it, and do nothing."""
    coro.close()


def _no_exit() -> None:
    """The exit a view with no window behind it gets."""


def _no_config(config: Config) -> None:
    """The config hand-back a standalone window gets: config.toml is the whole
    propagation, and the next launch reads it."""


def _no_calibration(slot: AgentSlot, region: ScreenRegion | None) -> None:
    """The calibration hand-back a standalone window gets - see the note in
    ``__init__``: nothing persists a chat region today, so this is a drop."""


__all__: Sequence[str] = [
    "CHAT_REGION_PROMPT",
    "ELEMENT_LABEL",
    "ELEMENT_MISSING",
    "ELEMENT_ORDER",
    "ELEMENT_RESTING",
    "MASTER",
    "NO_REGION_TO_IDENTIFY",
    "PICKER_BUSY",
    "REGION_UNSET",
    "SLOT_LABELS",
    "STATE_FOUND",
    "STATE_MISSING",
    "STATE_RESTING",
    "SUBAGENT",
    "CalibrationMonitor",
    "CalibrationView",
    "ElementCrop",
    "Severity",
    "element_crop",
    "element_png",
    "found_line",
]
