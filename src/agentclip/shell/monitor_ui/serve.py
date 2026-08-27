"""The Serve panel: who may drive this machine, decided out loud in a window.

``docs/design/ui-monitor.md`` §9.1. The Monitor is a standing process on the
machine with the pixels, and until this panel existed the only way to say "a
Chat UI over there may have my mouse" was to sit at the VM and type a command
line with a port on it. This is that decision as a surface: an address, a port,
a Start button, one status sentence, and the token the far side has to carry.

**The panel owns the server, and the server lives on the window's loop.**
:class:`~agentclip.driver.monitor.server.MonitorServer` is asyncio all the way
down - its listener, its one session and its pusher task are all bound to the
loop they were created on - so every verb here is a *schedule*, never a call:
the page's click arrives on a pywebview thread, drops a coroutine onto the
runner's loop and returns. Nothing on this object is awaited from the page.

**The status line is polled, not pushed.** ``MonitorServer`` has no "a brain
attached" hook and deliberately does not grow one for a label: whether somebody
is on the line is a property (:attr:`~MonitorServer.attached`,
:attr:`~MonitorServer.peer`) and one read a second is both cheap and enough for
a sentence a human is looking at. So while the server is up, one task re-pushes
the panel's state on :data:`STATUS_TICK_S`, and it ends by noticing the server
is gone rather than by being cancelled - there is no task handle to lose.

**Regenerating does not kick anybody.** The token gates ``hello``; a connection
that already shook hands was already authorised, and the session holds its own
copy of the secret it was admitted under. So :meth:`regenerate` writes a new
token, assigns it to the live server, and changes only what the NEXT hello must
carry. The panel says so rather than leaving the operator to find out.

**The no-token escape hatch is loopback-only**, which is the server's rule
(``BindRefused``) said twice: once in the checkbox, which the page disables the
moment a non-loopback address is picked, and once here, because a command line
can still arrive carrying both.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from agentclip.driver.monitor.auth import load_or_create_token, regenerate_token, token_path
from agentclip.driver.monitor.interfaces import Interface, list_interfaces
from agentclip.driver.monitor.protocol import UIMonitor
from agentclip.driver.monitor.server import LOOPBACK, BindRefused, MonitorServer, serve

#: The port the field comes up holding. Not a registered number and not derived
#: from anything - it is four digits an operator can read off one screen and type
#: into another, and it is the number every doc in this wave writes.
DEFAULT_PORT = 7777

#: How often the status line is re-read while serving. A human reading a
#: sentence; anything faster would be a paint nobody sees.
STATUS_TICK_S = 1.0

#: The three states of that sentence, spelled once.
NOT_SERVING = "not serving"
LISTENING_ALONE = "listening on {address} — no Chat UI attached"
LISTENING_ATTACHED = "listening on {address} — attached: {peer}"

#: What choosing a non-loopback address means, said before Start is pressed.
#: The same sentence ``--bind``'s help and ``BindRefused`` carry (§5): picking
#: one of these IS the opt-in, spelled as a click instead of as a flag.
REMOTE_WARNING = (
    "off loopback this port is reachable by anything on that network, and it is"
    " a channel to this machine's mouse, keyboard and clipboard - use a"
    " host-only network or an SSH -L forward, and leave the token on."
)

#: The one combination the panel refuses outright, and the command line refuses
#: with it (``--no-token`` plus a non-loopback ``--bind``): the two opt-ins
#: compose to "anyone on this network may drive this desktop", which is not a
#: thing a checkbox gets to say by accident. The page greys the box off loopback;
#: this is the same rule where a command line can still arrive carrying both.
NO_TOKEN_OFF_LOOPBACK = (
    "refusing to serve {address} without a token: off loopback that is a port"
    " onto this machine's mouse, keyboard and clipboard for anything on the"
    " network - the no-token box is loopback only."
)

#: Regenerating, said out loud. The operator's next question is always "did I
#: just drop the session I am watching", and the answer is no.
REGENERATED = (
    "new token - the attached Chat UI keeps its connection; the next one has to"
    " carry this."
)

#: What the panel falls back to when ``psutil`` is not in the build. Not an
#: empty dropdown: a monitor that cannot enumerate its NICs can still serve
#: loopback, and can still be told to serve everything.
FALLBACK_INTERFACES: tuple[Interface, ...] = (
    Interface(name="loopback", address=LOOPBACK[0], family="ipv4", loopback=True),
    Interface(name="all interfaces", address="0.0.0.0", family="ipv4", loopback=False),
)

PushFn = Callable[[dict[str, Any]], None]
ScheduleFn = Callable[[Coroutine[Any, Any, Any]], None]
NotifyFn = Callable[[str], None]


def is_loopback(address: str) -> bool:
    """Is ``address`` one the server needs no opt-in to bind?"""
    return address in LOOPBACK


class ServePanel:
    """The Serve panel's whole state, and the server it starts.

    Built by whoever built the monitor (``window.run_monitor_ui``, or a test),
    because the thing being served has to be the *same* monitor the window is
    calibrating - a second one would be a second poller over the same screen.
    :meth:`bind` comes later: the loop and the page both exist after this does.
    """

    def __init__(
        self,
        monitor: UIMonitor,
        *,
        config_dir: Path,
        serve_at: tuple[str, int] | None = None,
        no_token: bool = False,
        interfaces: Callable[[], list[Interface]] = list_interfaces,
        tick_seconds: float = STATUS_TICK_S,
    ) -> None:
        self._monitor = monitor
        self._config_dir = Path(config_dir)
        self._interfaces = interfaces
        self._tick_seconds = tick_seconds
        self._listed: tuple[Interface, ...] | None = None
        # The command line's answer, when it gave one: ``--port`` (and
        # ``--bind``) pre-fill the fields AND arm the auto-start, so a launcher
        # that used to type the whole thing still comes up listening.
        self._address = serve_at[0] if serve_at is not None else LOOPBACK[0]
        self._port = serve_at[1] if serve_at is not None else DEFAULT_PORT
        self._no_token = no_token
        self._autostart = serve_at is not None
        # The secret, read (or minted) once. Held in memory as well as on disk
        # because the page SHOWS it: a panel that re-read the file per paint
        # would be a file read a second, and the file is the only writer.
        self._token = load_or_create_token(self._config_dir)
        self._server: MonitorServer | None = None
        # §11.7: the palette the attached Chat UI asked for, and the reason it
        # is HERE rather than read off the server per paint - a monitor keeps
        # the last theme a brain named after that brain has gone, so a detach
        # must not flash the window back to dark.
        self._theme: str | None = None
        self._error = ""
        self._ticking = False
        self._schedule: ScheduleFn = _no_schedule
        self._push: PushFn = _no_push
        self._notify: NotifyFn = _no_notify

    # == wiring ================================================================

    def bind(self, *, schedule: ScheduleFn, push: PushFn, notify: NotifyFn) -> None:
        """Point the panel at the loop its server will live on and at the page.

        Separate from construction for the runner's ordering: the monitor exists
        before the loop does, and the bridge exists before the view that owns
        the event vocabulary.
        """
        self._schedule = schedule
        self._push = push
        self._notify = notify

    # == what a caller can ask =================================================

    @property
    def server(self) -> MonitorServer | None:
        """The running server, or None. What a suite reaches for the port."""
        return self._server

    @property
    def token(self) -> str:
        """The secret the next ``hello`` must carry.

        A plain attribute read, which is what lets ``token_copy`` answer the
        page from pywebview's own thread without touching the loop.
        """
        return self._token

    @property
    def theme(self) -> str | None:
        """The palette the page is wearing, or None for the window's own default."""
        return self._theme

    def set_theme(self, theme: str) -> None:
        """The attached Chat UI picked a palette (§11.7): wear it and repaint.

        Called from the view's ``on_theme`` subscription, which is fed by the
        monitor - the handshake's ``hello`` and the ``set_theme`` verb both land
        there, so this method has no idea which of the two happened and does not
        need one.
        """
        self._theme = theme
        self.push()

    @property
    def status(self) -> str:
        """The one sentence: not serving, listening alone, or attached."""
        server = self._server
        if server is None:
            return NOT_SERVING
        peer = server.peer
        if peer is None:
            return LISTENING_ALONE.format(address=server.address)
        return LISTENING_ATTACHED.format(address=server.address, peer=peer)

    def state(self) -> dict[str, Any]:
        """The whole panel, as the page's ``serve`` event carries it."""
        loopback = is_loopback(self._address)
        server = self._server
        peer = server.peer if server is not None else None
        return {
            "serving": server is not None,
            "status": self.status,
            # The same fact as ``status``, as an enum the page can COLOUR by:
            # off (nothing listening), waiting (listening, nobody on the line),
            # attached (a Chat UI is driving this screen). The header badge is
            # painted from this; the sentence is for reading, this is for
            # seeing from across the room.
            "link": "off" if server is None else ("attached" if peer else "waiting"),
            "peer": peer or "",
            # What the WHOLE PAGE wears, riding on the panel's own event
            # because the panel is the surface that knows a brain is on the
            # line at all (§11.7). "" is "this window's own default" - which is
            # what a monitor nobody has ever attached to paints.
            "theme": self._theme or "",
            # Which SERVICE this monitor is watching - the key its last
            # ``watch``/``configure`` named, resolved against THIS machine's
            # profiles and region store. Shown on the badge next to the peer, so
            # the operator can read what the attached Chat UI is driving without
            # going to the other machine. Since §11.6 it cannot DISAGREE with
            # the Services dropdown: that dropdown is the selection, and picking
            # in it retargets the monitor - so there is no mismatch to report.
            "driving": self._driving(),
            "address": self._address,
            "port": self._port,
            "interfaces": [
                {
                    "value": row.address,
                    "label": f"{row.name} — {row.address}",
                    "loopback": row.loopback,
                }
                for row in self._rows()
            ],
            # Whether the address the panel COMMITTED to is one the server needs
            # no opt-in for. The page's dropdown can be moved without pressing
            # Start, and what the page does with a pending selection - show the
            # warning below, grey the no-token box - it decides from the row's
            # own ``loopback`` flag above. This is the panel's own answer, and it
            # is what a reader of ``state()`` is asking about.
            "loopback": loopback,
            # The words, once, unconditionally: whether they are ON SCREEN is a
            # question about the selection the user is looking at right now, and
            # only the page knows that. Sent rather than spelled in the page for
            # the reason every worded control here is - the sentence is §5's and
            # a second copy of it would be a second thing to keep true.
            "warning": REMOTE_WARNING,
            "error": self._error,
            "no_token": self._no_token,
            "token": self._token,
            "token_path": str(token_path(self._config_dir)),
        }

    def _driving(self) -> str | None:
        """The service key the last ``configure``/``watch`` named, if any."""
        spec = getattr(self._monitor, "spec", None)
        return None if spec is None else str(spec.service)

    def push(self) -> None:
        """Send the panel's state to the page. Safe from any thread."""
        self._push(self.state())

    # == the page's verbs ======================================================

    def start_if_requested(self) -> None:
        """Auto-start, once, when the command line named a port.

        Called from the view's ``start`` rather than from ``__init__`` so the
        first thing the page paints is a panel that is already listening, rather
        than one that starts a second later under the reader's eyes.
        """
        if not self._autostart:
            return
        self._autostart = False
        self.start(self._address, self._port, self._no_token)

    def start(self, address: str, port: int, no_token: bool) -> None:
        """"Start": bind and begin accepting. Returns immediately."""
        self._schedule(self._start(address, port, no_token))

    def stop(self) -> None:
        """"Stop": drop the listener and any attached Chat UI, keep polling."""
        self._schedule(self._stop())

    def regenerate(self) -> None:
        """"Regenerate": a new secret for the next hello, nobody kicked."""
        self._schedule(self._regenerate())

    async def close(self) -> None:
        """The window is going away. Stop listening; do NOT close the monitor.

        Hooked into the runner's stop sequence through the view's ``close``,
        which is the last moment there is a loop to await a server's teardown
        on - and a listener left bound would hold the port against the next
        launch of this very window.
        """
        await self._stop()

    # == the loop's half =======================================================

    async def _start(self, address: str, port: int, no_token: bool) -> None:
        if self._server is not None:
            return
        self._address = address
        self._port = port
        self._no_token = no_token
        loopback = is_loopback(address)
        if no_token and not loopback:
            # REFUSED, not quietly upgraded to a token: the operator asked for
            # something and the panel has to say no rather than serve something
            # else and let them believe the box did nothing.
            self._error = NO_TOKEN_OFF_LOOPBACK.format(address=address)
            self.push()
            return
        token = None if no_token else self._token
        try:
            server = await serve(
                self._monitor,
                host=address,
                port=port,
                # Picking a non-loopback row IS §5's opt-in, spelled as a click.
                allow_remote=not loopback,
                token=token,
            )
        except (BindRefused, OSError) as exc:
            # Into the panel, not into a toast: the reason a port did not open
            # belongs beside the port field that names it, and a toast for
            # "address already in use" would be gone before the operator has
            # finished reading which address.
            self._error = str(exc)
            self.push()
            return
        self._server = server
        self._error = ""
        self.push()
        self._start_ticking()

    async def _stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.close()
        self._error = ""
        self.push()

    async def _regenerate(self) -> None:
        self._token = regenerate_token(self._config_dir)
        server = self._server
        if server is not None:
            # The live server, retold. Its attached session holds the secret it
            # was admitted under, so nobody is dropped (server.py's setter).
            server.token = self._token
        self.push()
        self._notify(REGENERATED)

    def _start_ticking(self) -> None:
        if self._ticking:
            return
        self._ticking = True
        self._schedule(self._tick())

    async def _tick(self) -> None:
        """Re-push the status line while there is a server to read it off.

        Ends by NOTICING rather than by being cancelled: ``stop`` clears the
        server and this sees it within a beat, so there is no task handle for a
        teardown to lose track of and no second loop to start by mistake.
        """
        try:
            while self._server is not None:
                await asyncio.sleep(self._tick_seconds)
                if self._server is None:
                    return
                self.push()
        finally:
            self._ticking = False

    # == small helpers =========================================================

    def _rows(self) -> tuple[Interface, ...]:
        """The dropdown's contents, read once and kept.

        ``psutil`` is imported inside ``list_interfaces`` and the addresses on a
        machine do not move while somebody is looking at a panel, so once is the
        right number of times to ask. An empty answer is a build that lost the
        dependency, and it degrades to "loopback, or everything" rather than to
        a dropdown with nothing in it.
        """
        if self._listed is None:
            found = tuple(self._interfaces())
            self._listed = found if found else FALLBACK_INTERFACES
        return self._listed


def _no_schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """The scheduler an unbound panel gets: close the coroutine, do nothing."""
    coro.close()


def _no_push(state: dict[str, Any]) -> None:
    """The page an unbound panel paints: none."""


def _no_notify(message: str) -> None:
    """The toast an unbound panel raises: none."""


__all__: Sequence[str] = [
    "DEFAULT_PORT",
    "FALLBACK_INTERFACES",
    "LISTENING_ALONE",
    "LISTENING_ATTACHED",
    "NOT_SERVING",
    "NO_TOKEN_OFF_LOOPBACK",
    "REGENERATED",
    "REMOTE_WARNING",
    "STATUS_TICK_S",
    "ServePanel",
    "is_loopback",
]
