"""A fake ``Xlib``, injected into ``sys.modules`` for the X11 backend's tests.

``driver/screen/x11.py`` imports python-xlib lazily inside every function, and
python-xlib is a Linux-only dependency (pyproject's marker), so on the machine
this suite usually runs on there is nothing to import at all. That laziness is
what makes the whole backend testable anywhere: swap the modules the functions
reach for, and the real code path runs against a recorded X server.

Nothing here pretends to be X. It records what was asked - the ``get_image``
rectangle, the order of XTest events, the client message sent to the root - so
a test can assert on the request rather than on a screen.

Shared by ``tests/driver/screen/test_x11.py`` and ``tests/test_os_gate.py``:
the gate's Linux branch neuters ``x11._fake_input``, and proving that requires
driving the same real code path down to it.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# The X protocol constants the backend uses, with their real values so a test
# reading these assertions sees the same numbers a packet capture would.
ZPIXMAP = 2
KEY_PRESS = 2
KEY_RELEASE = 3
BUTTON_PRESS = 4
BUTTON_RELEASE = 5
MOTION_NOTIFY = 6
ANY_PROPERTY_TYPE = 0
CURRENT_TIME = 0
ABOVE = 0
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
SUBSTRUCTURE_REDIRECT_MASK = 1 << 20


class FakeWindow:
    """A window resource: the root, or one named by ``create_resource_object``."""

    def __init__(self, display: FakeDisplay, handle: int) -> None:
        self.display = display
        self.handle = handle
        self.alive = True

    # -- capture --
    def get_image(self, x: int, y: int, width: int, height: int, format: int, mask: int) -> Any:
        self.display.image_requests.append((x, y, width, height, format, mask))
        return types.SimpleNamespace(data=self.display.image_data, depth=self.display.image_depth)

    def get_geometry(self) -> Any:
        left, top, width, height = self.display.geometry
        return types.SimpleNamespace(x=left, y=top, width=width, height=height)

    # -- properties --
    def get_full_property(self, atom: int, property_type: int) -> Any:
        self.display.property_reads.append((atom, property_type))
        value = self.display.active_window
        if value is None:
            return None
        return types.SimpleNamespace(value=[value])

    # -- focus --
    def get_attributes(self) -> Any:
        if not self.alive:
            raise RuntimeError("BadWindow")
        return types.SimpleNamespace(map_state=2)

    def send_event(self, event: Any, event_mask: int = 0) -> None:
        self.display.messages.append((event, event_mask))

    def configure(self, **kwargs: Any) -> None:
        self.display.configures.append((self.handle, kwargs))


class FakeDisplay:
    """One recorded X connection."""

    def __init__(
        self,
        *,
        image_data: bytes = b"",
        image_depth: int = 24,
        bits_per_pixel: int = 32,
        scanline_pad: int = 32,
        image_byte_order: int = 0,
        geometry: tuple[int, int, int, int] = (0, 0, 1920, 1080),
        active_window: int | None = 0x2A,
        keymap: dict[int, int] | None = None,
        dead_windows: frozenset[int] = frozenset(),
    ) -> None:
        self.image_data = image_data
        self.image_depth = image_depth
        self.geometry = geometry
        self.active_window = active_window
        # Keysym -> keycode. Missing entries answer 0, which is X's "unmapped".
        self.keymap = keymap if keymap is not None else {}
        self.dead_windows = dead_windows
        self.info = types.SimpleNamespace(
            pixmap_formats=[
                types.SimpleNamespace(
                    depth=image_depth, bits_per_pixel=bits_per_pixel, scanline_pad=scanline_pad
                )
            ],
            image_byte_order=image_byte_order,
        )
        self.root = FakeWindow(self, 0x1)
        self.image_requests: list[tuple[int, int, int, int, int, int]] = []
        self.property_reads: list[tuple[int, int]] = []
        self.messages: list[tuple[Any, int]] = []
        self.configures: list[tuple[int, dict[str, Any]]] = []
        self.events: list[tuple[int, int, int, int]] = []
        self.syncs = 0
        self.atoms: dict[str, int] = {}

    def screen(self) -> Any:
        return types.SimpleNamespace(root=self.root)

    def sync(self) -> None:
        self.syncs += 1

    def intern_atom(self, name: str) -> int:
        return self.atoms.setdefault(name, 100 + len(self.atoms))

    def keysym_to_keycode(self, keysym: int) -> int:
        return self.keymap.get(keysym, 0)

    def create_resource_object(self, kind: str, handle: int) -> FakeWindow:
        window = FakeWindow(self, handle)
        window.alive = handle not in self.dead_windows
        return window


def _keysym(name: str) -> int:
    """A stable pretend keysym per name - the real table is irrelevant here,
    only that name -> keysym -> keycode survives the round trip."""
    return 0x1000 + (sum(name.encode()) % 0x800)


def install_fake_xlib(
    monkeypatch: pytest.MonkeyPatch, display: FakeDisplay | None = None
) -> FakeDisplay:
    """Put a fake ``Xlib`` in ``sys.modules`` and return the display it opens.

    Also sets ``DISPLAY`` and clears the backend's sticky connection state, so
    ``x11._display()`` really runs (and really caches) against this fake instead
    of being patched out - the connect path is part of what these tests cover.
    """
    from agentclip.driver.screen import x11

    display = display if display is not None else FakeDisplay()

    xlib = types.ModuleType("Xlib")
    x_constants = types.ModuleType("Xlib.X")
    for name, value in (
        ("ZPixmap", ZPIXMAP),
        ("KeyPress", KEY_PRESS),
        ("KeyRelease", KEY_RELEASE),
        ("ButtonPress", BUTTON_PRESS),
        ("ButtonRelease", BUTTON_RELEASE),
        ("MotionNotify", MOTION_NOTIFY),
        ("AnyPropertyType", ANY_PROPERTY_TYPE),
        ("CurrentTime", CURRENT_TIME),
        ("Above", ABOVE),
        ("NONE", 0),
        ("SubstructureNotifyMask", SUBSTRUCTURE_NOTIFY_MASK),
        ("SubstructureRedirectMask", SUBSTRUCTURE_REDIRECT_MASK),
    ):
        setattr(x_constants, name, value)

    display_module = types.ModuleType("Xlib.display")
    display_module.Display = lambda *args, **kwargs: display  # type: ignore[attr-defined]

    xk = types.ModuleType("Xlib.XK")
    xk.string_to_keysym = _keysym  # type: ignore[attr-defined]

    ext = types.ModuleType("Xlib.ext")
    xtest = types.ModuleType("Xlib.ext.xtest")

    def fake_input(disp: Any, event_type: int, detail: int = 0, x: int = 0, y: int = 0) -> None:
        disp.events.append((event_type, detail, x, y))

    xtest.fake_input = fake_input  # type: ignore[attr-defined]
    ext.xtest = xtest  # type: ignore[attr-defined]

    protocol = types.ModuleType("Xlib.protocol")
    event_module = types.ModuleType("Xlib.protocol.event")

    class ClientMessage:
        def __init__(self, *, window: Any, client_type: int, data: Any) -> None:
            self.window = window
            self.client_type = client_type
            self.data = data

    event_module.ClientMessage = ClientMessage  # type: ignore[attr-defined]
    protocol.event = event_module  # type: ignore[attr-defined]

    xlib.X = x_constants  # type: ignore[attr-defined]
    xlib.XK = xk  # type: ignore[attr-defined]
    xlib.display = display_module  # type: ignore[attr-defined]
    xlib.ext = ext  # type: ignore[attr-defined]
    xlib.protocol = protocol  # type: ignore[attr-defined]

    for name, module in (
        ("Xlib", xlib),
        ("Xlib.X", x_constants),
        ("Xlib.XK", xk),
        ("Xlib.display", display_module),
        ("Xlib.ext", ext),
        ("Xlib.ext.xtest", xtest),
        ("Xlib.protocol", protocol),
        ("Xlib.protocol.event", event_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(x11, "_display_state", None)
    monkeypatch.setattr(x11, "_display_failure", None)
    return display


def keycodes_for(*names: str) -> dict[int, int]:
    """A keymap answering a distinct keycode for each keysym name, numbered from
    10 the way a real server's keycodes start well above zero (0 means
    unmapped, which the backend treats as "no such key")."""
    return {_keysym(name): 10 + index for index, name in enumerate(names)}


def keycode_of(display: FakeDisplay, name: str) -> int:
    return display.keymap[_keysym(name)]
