"""The wire vocabulary of the brain<->monitor link - monitor protocol version 4.

The one place the two halves of docs/design/ui-monitor.md §6.5 agree on what a
message looks like. Both ends import it: :mod:`agentclip.driver.monitor.server`,
which runs beside the ``LocalUIMonitor`` on the machine whose screen is being
watched, and :mod:`agentclip.driver.monitor.remote`, which stands in for that
monitor inside the brain. Neither owns a schema of its own, so neither can drift
from the other.

Framing
-------
JSON Lines, exactly as ``engine/link/wire.py`` frames the engine link (§2.7):
one JSON object per line, UTF-8, ``"\\n"``-terminated, compact separators, no
raw newline anywhere inside a line. Copied rather than imported, because
``driver/monitor`` may not reach into ``engine`` (tests/test_layering.py) - and
because the two protocols must be free to version apart, which is the next
paragraph.

Its own version, deliberately
-----------------------------
:data:`MONITOR_WIRE_VERSION` is **not** the engine's ``WIRE_VERSION`` (§2.7:
"the monitor protocol gets its own wire version constant - do not couple it to
the engine's"). The two links join different pairs of processes on different
schedules: a monitor VM is redeployed when the calibration surface changes, an
SSH target when the engine does, and a shared integer would make each of those
a forced upgrade of the other.

Frame vocabulary (v4)
---------------------
``{"type":"hello","version":4,"package":"0.1.0","token":"<32 hex>"|null}``
    The client's first line. Nothing else may precede it. ``token`` is §5's
    shared secret (:mod:`agentclip.driver.monitor.auth`) and **null is a real
    value**: a monitor started with ``--no-token`` accepts it, and one started
    with a token refuses it exactly as it refuses a wrong one. Adding the field
    is what took this wire from 1 to 2 - the handshake had "room for a secret"
    from the start, and filling that room changes the shape of the first line,
    which is precisely what a version gate is for. **3 is §10.5**: ``configure``
    left this table (a brain may not name a service on somebody else's desktop)
    and ``watch`` took its place. A verb removed is a breaking change by
    definition - a v2 brain's first act after the handshake would be a call the
    v3 server has never heard of - so the two halves have to be upgraded
    together, and the version gate is what says so in one sentence instead of
    with a ``bad_request`` on the first retarget. **4 is §11.3**: the brain
    stopped holding templates at all, so ``Watched`` grew ``captured`` (which
    appearances the monitor has, kind by kind) and ``Located`` grew ``target``
    (the one pixel to click, the service's click point already applied). Both
    are answers only the machine with the pictures can give, and a v3 monitor
    gives neither - a brain that took its silence for "nothing is calibrated"
    would refuse every click on a perfectly calibrated desktop, which is
    exactly the failure this wave was opened by.
``{"type":"hello_ack","version":4,"package":"0.1.0","server_id":"<uuid4>",
"clipboard_kind":"copykitten"|null}``
    The server's reply. ``server_id`` identifies the PROCESS (a monitor is
    long-lived and survives every brain that dials it, §2.8, so a redial wants
    to be able to notice it reached a *different* monitor). ``clipboard_kind``
    is the one piece of monitor state that has to be known before any call can
    be made: :attr:`~agentclip.driver.monitor.protocol.UIMonitor.clipboard_kind`
    and ``watch_clipboard`` are the Protocol's two SYNCHRONOUS members, and a
    property cannot await a round trip. It is fixed for the monitor's lifetime -
    the backend is chosen once, when the process starts - so stating it in the
    handshake is not a cache that can go stale.
``{"type":"call","id":<int>,"verb":"<str>","params":{...}}``
    A request. ``id`` is client-chosen, strictly increasing, and echoed by
    exactly one ``result`` or ``error``.
``{"type":"result","id":<int>,"value":<encoded value>}``
    The answer. ``value`` is whatever :func:`encode_result` makes of the verb's
    return.
``{"type":"error","id":<int>|null,"kind":"<kind>","message":"<str>"}``
    A failure. ``id`` is the call it answers, or **null** for a failure that
    belongs to the CONNECTION rather than to any one call - the second brain's
    refusal (``kind="busy"``) and the unauthenticated one's
    (``kind="unauthorized"``), which are the only frames a server ever sends
    before a call has been made.
``{"type":"tick","tick":{...}}``
    One :class:`~agentclip.driver.monitor.protocol.Tick`, pushed. Unsolicited:
    the monitor polls on its own (§2.1) and the client's reader task is where
    they land.
``{"type":"clip","text":"..."}``
    One clipboard capture the monitor's watcher accepted (§2.11).

Why a table
-----------
:data:`VERBS`, ``_PARAMS`` and ``_RESULTS`` state every verb's parameters and
return shape ONCE. The server holds one line of dispatch and the client one line
of await, so a parameter renamed on one side is a decode error on the same day
rather than a silently-dropped keyword six months later.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agentclip import __version__
from agentclip.driver.monitor.protocol import ElementClick, Located, Tick, Watched
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.stale import StaleProbe, StaleState

MONITOR_WIRE_VERSION = 4

#: Per-line ceiling for both ends' stream readers. asyncio's default is 64 KiB,
#: and a ``write_clipboard`` carrying a long reply is bigger than that on a
#: normal day: a limit that small would not truncate the frame, it would kill
#: the connection with a ``LimitOverrunError`` the moment somebody pasted a file.
LINE_LIMIT = 16 * 1024 * 1024


class WireError(Exception):
    """A line, frame or value that is not valid monitor protocol v4.

    Raised by every decoder here and by nothing else. Both ends treat it as
    fatal for the frame it was raised on: the server answers the offending call
    with ``kind="bad_request"``, the client fails that one call, and neither
    ever tries to salvage a partially-understood message.
    """


@dataclass(frozen=True, slots=True)
class Versions:
    """One end's two numbers, as the handshake states them.

    ``wire`` is the protocol this end speaks (the gate); ``package`` is the
    ``agentclip`` distribution it was installed from (the diagnostic, never
    branched on). The monitor half is a SEPARATE install on a separate machine -
    an ``agentclip-monitor`` binary or console script on the VM - so the two
    package versions differing is normal and only a wire mismatch is fatal.
    """

    wire: int
    package: str


#: This process's own pair; the "ours" half of every :class:`WireVersionError`.
OURS = Versions(wire=MONITOR_WIRE_VERSION, package=__version__)


class WireVersionError(WireError):
    """A peer that does not speak our wire version - with both installs named.

    Both numbers, because the one a HUMAN can act on is the package: the user
    installed a release on the VM and another on this PC, and "monitor wire
    version 3 is not 2" tells them nothing about which of the two to upgrade.
    """

    def __init__(self, what: str, peer: Versions, ours: Versions = OURS) -> None:
        super().__init__(
            f"{what}: monitor wire version {peer.wire} is not {ours.wire}"
            f" - the far side is agentclip {peer.package},"
            f" this side is agentclip {ours.package}"
        )
        self.peer = peer
        self.ours = ours


# The frame types of v4, and the error kinds an ``error`` frame may carry.
FRAME_TYPES: tuple[str, ...] = (
    "hello",
    "hello_ack",
    "call",
    "result",
    "error",
    "tick",
    "clip",
)

#: ``busy``   - a second brain dialled a monitor that already has one (§2.8).
#: ``unauthorized`` - the hello carried no token, or the wrong one (§5). Sent
#:   BEFORE the ``hello_ack``, so an unauthorised peer never learns the
#:   monitor's ``server_id`` or which clipboard backend the machine has.
#: ``bad_request`` - the frame or its params did not decode.
#: ``clipboard_unavailable`` - the one monitor-side exception with a type of its
#:   own on this seam, because a delivery path catches it BY TYPE and a wire that
#:   flattened it to "internal" would turn a manual-paste fallback into a crash.
#: ``internal`` - anything else the verb raised.
ERROR_KINDS: tuple[str, ...] = (
    "busy",
    "unauthorized",
    "bad_request",
    "clipboard_unavailable",
    "internal",
)


# -- lines ---------------------------------------------------------------------


def encode_line(frame: dict[str, Any]) -> str:
    """One frame as the exact text that goes on the wire.

    Compact separators and ``ensure_ascii=False``: the stream is UTF-8, so a
    clipboard capture full of em-dashes rides as itself. The only raw newline in
    the result is the terminator - JSON escapes the ones inside strings, which
    is what keeps "one frame per line" true of a 200k-char paste.
    """
    try:
        line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:  # a value no codec here produced
        raise WireError(f"frame is not JSON-encodable: {exc}") from exc
    return line + "\n"


def decode_line(line: str) -> dict[str, Any]:
    """One line back into a frame, or :class:`WireError`.

    Only the envelope is checked - a JSON object with a string ``type`` - because
    that is all a reader needs to route it. The per-type readers do the rest.
    """
    try:
        value = json.loads(line)
    except ValueError as exc:
        raise WireError(f"not a JSON line: {exc}") from exc
    if not isinstance(value, dict):
        raise WireError(f"frame must be a JSON object, got {type(value).__name__}")
    kind = value.get("type")
    if not isinstance(kind, str):
        raise WireError("frame has no string 'type'")
    return value


# -- strict readers ------------------------------------------------------------


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WireError(f"{what}: expected an object, got {type(value).__name__}")
    return value


def _field(data: dict[str, Any], key: str, what: str) -> Any:
    if key not in data:
        raise WireError(f"{what}: missing {key!r}")
    return data[key]


def _as_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise WireError(f"{what}: expected a string, got {type(value).__name__}")
    return value


def _as_opt_str(value: Any, what: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise WireError(f"{what}: expected a string or null, got {type(value).__name__}")


def _as_int(value: Any, what: str) -> int:
    # bool is an int in Python and never one on this wire: a detent count that
    # decoded ``true`` into 1 would scroll a window nobody asked to scroll.
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireError(f"{what}: expected an integer, got {type(value).__name__}")
    return value


def _as_opt_int(value: Any, what: str) -> int | None:
    if value is None:
        return None
    return _as_int(value, what)


def _as_float(value: Any, what: str) -> float:
    # An int is a fine float on the wire (JSON writes 0.0 as 0 in some encoders,
    # and a hand-written frame says ``1``), a bool never is.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WireError(f"{what}: expected a number, got {type(value).__name__}")
    return float(value)


def _as_opt_float(value: Any, what: str) -> float | None:
    if value is None:
        return None
    return _as_float(value, what)


def _as_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise WireError(f"{what}: expected a boolean, got {type(value).__name__}")
    return value


def _as_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise WireError(f"{what}: expected a list, got {type(value).__name__}")
    return value


def _as_strs(value: Any, what: str) -> tuple[str, ...]:
    return tuple(_as_str(item, f"{what}[{i}]") for i, item in enumerate(_as_list(value, what)))


def _as_literal(value: Any, allowed: Sequence[str], what: str) -> str:
    text = _as_str(value, what)
    if text not in allowed:
        raise WireError(f"{what}: {text!r} is not one of {tuple(allowed)!r}")
    return text


def _str_at(data: dict[str, Any], key: str, what: str) -> str:
    return _as_str(_field(data, key, what), f"{what}.{key}")


def _int_at(data: dict[str, Any], key: str, what: str) -> int:
    return _as_int(_field(data, key, what), f"{what}.{key}")


def _opt_int_at(data: dict[str, Any], key: str, what: str) -> int | None:
    return _as_opt_int(_field(data, key, what), f"{what}.{key}")


def _float_at(data: dict[str, Any], key: str, what: str) -> float:
    return _as_float(_field(data, key, what), f"{what}.{key}")


def _opt_float_at(data: dict[str, Any], key: str, what: str) -> float | None:
    return _as_opt_float(_field(data, key, what), f"{what}.{key}")


def _opt_str_at(data: dict[str, Any], key: str, what: str) -> str | None:
    value = _field(data, key, what)
    return None if value is None else _as_str(value, f"{what}.{key}")


def _bool_at(data: dict[str, Any], key: str, what: str) -> bool:
    return _as_bool(_field(data, key, what), f"{what}.{key}")


def _strs_at(data: dict[str, Any], key: str, what: str) -> tuple[str, ...]:
    return _as_strs(_field(data, key, what), f"{what}.{key}")


def _captured_at(data: dict[str, Any], what: str) -> tuple[TemplateKind, ...]:
    """``Watched.captured``, decoded as tolerantly as this wire ever decodes.

    The one field here that is read rather than refused, both ways: an absent
    list reads as "nothing captured", and a name this build has no
    :class:`TemplateKind` for is dropped instead of failing the frame. The
    version handshake pins the SHAPE of the wire, not the enum inside it - a
    monitor a release ahead can hold an appearance kind this brain has never
    heard of, and the honest answer to "have you got one of those?" from a
    brain that cannot even name it is no, not a dead connection. Every other
    field is strict on purpose: losing one silently would compose a turn
    against the wrong service.
    """
    value = data.get("captured")
    if value is None:
        return ()
    kinds: list[TemplateKind] = []
    for item in _as_list(value, f"{what}.captured"):
        if not isinstance(item, str):
            continue
        try:
            kinds.append(TemplateKind(item))
        except ValueError:
            continue
    return tuple(kinds)


# -- enums ---------------------------------------------------------------------
#
# Two spellings, on purpose, and the difference is which half of the enum is the
# contract. ``BusyState`` / ``StaleState`` / ``ElementClick`` travel by NAME:
# their values are lowercase words that read like the names and a wire that used
# them would be one rename away from silently meaning something else.
# ``TemplateKind`` travels by VALUE because its value already IS the persisted
# identity - ``profile_store`` writes ``"chatbox-initial"`` into every profile on
# disk, so the wire and the store say the same word.


def encode_busy_state(value: BusyState) -> str:
    return value.name


def decode_busy_state(value: Any, what: str = "busy_state") -> BusyState:
    name = _as_str(value, what)
    try:
        return BusyState[name]
    except KeyError:
        raise WireError(f"{what}: {name!r} is not a BusyState") from None


def encode_stale_state(value: StaleState) -> str:
    return value.name


def decode_stale_state(value: Any, what: str = "stale_state") -> StaleState:
    name = _as_str(value, what)
    try:
        return StaleState[name]
    except KeyError:
        raise WireError(f"{what}: {name!r} is not a StaleState") from None


def encode_element_click(value: ElementClick) -> str:
    return value.name


def decode_element_click(value: Any, what: str = "element_click") -> ElementClick:
    name = _as_str(value, what)
    try:
        return ElementClick[name]
    except KeyError:
        raise WireError(f"{what}: {name!r} is not an ElementClick") from None


def encode_slot(value: AgentSlot) -> str:
    """Which of the monitor's two windows, as its own persisted word.

    By VALUE like ``TemplateKind`` and unlike the three state enums: ``master``
    / ``subagent`` are what a slot IS everywhere else in the codebase (a
    ``StrEnum``, written into prompts and read out of them), so the wire and the
    rest of the program say the same word.
    """
    return value.value


def decode_slot(value: Any, what: str = "slot") -> AgentSlot:
    text = _as_str(value, what)
    try:
        return AgentSlot(text)
    except ValueError:
        raise WireError(f"{what}: {text!r} is not an AgentSlot") from None


def encode_kind(value: TemplateKind) -> str:
    return value.value


def decode_kind(value: Any, what: str = "kind") -> TemplateKind:
    text = _as_str(value, what)
    try:
        return TemplateKind(text)
    except ValueError:
        raise WireError(f"{what}: {text!r} is not a TemplateKind") from None


def encode_kinds(value: Sequence[TemplateKind]) -> list[str]:
    return [encode_kind(kind) for kind in value]


def decode_kinds(value: Any, what: str = "kinds") -> tuple[TemplateKind, ...]:
    return tuple(decode_kind(item, f"{what}[{i}]") for i, item in enumerate(_as_list(value, what)))


# -- dataclasses ---------------------------------------------------------------


def encode_region(value: ScreenRegion) -> dict[str, Any]:
    return {
        "left": value.left,
        "top": value.top,
        "width": value.width,
        "height": value.height,
    }


def decode_region(value: Any, what: str = "region") -> ScreenRegion:
    data = _mapping(value, what)
    return ScreenRegion(
        # Signed on purpose: a monitor left of or above the primary one has
        # negative origins, and this wire carries VIRTUAL-screen coordinates.
        left=_int_at(data, "left", what),
        top=_int_at(data, "top", what),
        width=_int_at(data, "width", what),
        height=_int_at(data, "height", what),
    )


def encode_opt_region(value: ScreenRegion | None) -> dict[str, Any] | None:
    return None if value is None else encode_region(value)


def decode_opt_region(value: Any, what: str = "region") -> ScreenRegion | None:
    return None if value is None else decode_region(value, what)


def encode_regions(value: Sequence[ScreenRegion]) -> list[dict[str, Any]]:
    return [encode_region(region) for region in value]


def decode_regions(value: Any, what: str = "regions") -> tuple[ScreenRegion, ...]:
    return tuple(
        decode_region(item, f"{what}[{i}]") for i, item in enumerate(_as_list(value, what))
    )


def encode_busy_probe(value: BusyProbe) -> dict[str, Any]:
    return {
        "state": encode_busy_state(value.state),
        "diff": value.diff,
        "generating_now": value.generating_now,
    }


def decode_busy_probe(value: Any, what: str = "busy") -> BusyProbe:
    data = _mapping(value, what)
    return BusyProbe(
        state=decode_busy_state(_field(data, "state", what), f"{what}.state"),
        diff=_opt_float_at(data, "diff", what),
        generating_now=_bool_at(data, "generating_now", what),
    )


def encode_opt_busy_probe(value: BusyProbe | None) -> dict[str, Any] | None:
    return None if value is None else encode_busy_probe(value)


def decode_opt_busy_probe(value: Any, what: str = "busy") -> BusyProbe | None:
    return None if value is None else decode_busy_probe(value, what)


def encode_stale_probe(value: StaleProbe) -> dict[str, Any]:
    return {
        "state": encode_stale_state(value.state),
        "diff": value.diff,
        "stable_ticks": value.stable_ticks,
    }


def decode_stale_probe(value: Any, what: str = "stale") -> StaleProbe:
    data = _mapping(value, what)
    return StaleProbe(
        state=decode_stale_state(_field(data, "state", what), f"{what}.state"),
        diff=_opt_float_at(data, "diff", what),
        stable_ticks=_int_at(data, "stable_ticks", what),
    )


def encode_opt_stale_probe(value: StaleProbe | None) -> dict[str, Any] | None:
    return None if value is None else encode_stale_probe(value)


def decode_opt_stale_probe(value: Any, what: str = "stale") -> StaleProbe | None:
    return None if value is None else decode_stale_probe(value, what)


def encode_watched(value: Watched) -> dict[str, Any]:
    """The monitor's whole effective service, field for field (§10.5).

    The biggest value on this wire and deliberately so: it replaced
    ``MonitorSpec`` in the opposite direction. What used to go OUT as "watch
    this" comes back as "this is what I watch", minus everything about how
    pixels are searched - a tolerance and a matcher are the monitor's own
    business and a brain has nothing to do with them.
    """
    return {
        "service": value.service,
        "label": value.label,
        "region": encode_opt_region(value.region),
        "profiled": value.profiled,
        "generation": value.generation,
        "captured": encode_kinds(value.captured),
        "delivery": value.delivery,
        "auto_submit": value.auto_submit,
        "scroll_action": value.scroll_action,
        "snap_back": value.snap_back,
        "hover_scan": value.hover_scan,
        "max_paste_chars": value.max_paste_chars,
        "total_context_chars": value.total_context_chars,
        "wrap_blocks_in_fence": value.wrap_blocks_in_fence,
        "attachment_note": value.attachment_note,
        "require_fenced_reply": value.require_fenced_reply,
        "extra_instructions": value.extra_instructions,
        "edit_by_lines": value.edit_by_lines,
    }


def decode_watched(value: Any, what: str = "watched") -> Watched:
    data = _mapping(value, what)
    return Watched(
        service=_opt_str_at(data, "service", what),
        region=decode_opt_region(_field(data, "region", what), f"{what}.region"),
        profiled=_bool_at(data, "profiled", what),
        label=_str_at(data, "label", what),
        generation=_int_at(data, "generation", what),
        captured=_captured_at(data, what),
        delivery=_str_at(data, "delivery", what),
        auto_submit=_bool_at(data, "auto_submit", what),
        scroll_action=_str_at(data, "scroll_action", what),
        snap_back=_bool_at(data, "snap_back", what),
        hover_scan=_bool_at(data, "hover_scan", what),
        max_paste_chars=_int_at(data, "max_paste_chars", what),
        total_context_chars=_int_at(data, "total_context_chars", what),
        wrap_blocks_in_fence=_bool_at(data, "wrap_blocks_in_fence", what),
        attachment_note=_bool_at(data, "attachment_note", what),
        require_fenced_reply=_bool_at(data, "require_fenced_reply", what),
        extra_instructions=_str_at(data, "extra_instructions", what),
        edit_by_lines=_bool_at(data, "edit_by_lines", what),
    )


def encode_located(value: Located) -> dict[str, Any]:
    return {
        "region": encode_opt_region(value.region),
        "ambiguous": value.ambiguous,
        "best_miss": value.best_miss,
        "target": encode_opt_region(value.target),
    }


def decode_located(value: Any, what: str = "located") -> Located:
    data = _mapping(value, what)
    return Located(
        region=decode_opt_region(_field(data, "region", what), f"{what}.region"),
        ambiguous=_bool_at(data, "ambiguous", what),
        best_miss=_opt_float_at(data, "best_miss", what),
        target=decode_opt_region(_field(data, "target", what), f"{what}.target"),
    )


def encode_tick(value: Tick) -> dict[str, Any]:
    """One tick, whole.

    ``sightings`` is a LIST OF PAIRS rather than a JSON object, and that is the
    one shape decision worth stating: the map is three-state (a region = found
    there, ``null`` = searched and not on screen, ABSENT = not searched at all),
    so "absent" has to survive the crossing. A JSON object would too - but a
    list of pairs also keeps the map's ORDER, which is the order the ELEMENTS
    panel lists kinds in, and it costs nothing.
    """
    return {
        "seq": value.seq,
        "generation": value.generation,
        "at": value.at,
        "captured": value.captured,
        "busy": encode_opt_busy_probe(value.busy),
        "idle": encode_opt_busy_probe(value.idle),
        "stale": encode_opt_stale_probe(value.stale),
        "sightings": [
            [encode_kind(kind), encode_opt_region(region)]
            for kind, region in value.sightings.items()
        ],
        "active_detectors": list(value.active_detectors),
        "stale_arm_streak": value.stale_arm_streak,
        "changed_streak": value.changed_streak,
    }


def _decode_sightings(value: Any, what: str) -> dict[TemplateKind, ScreenRegion | None]:
    sightings: dict[TemplateKind, ScreenRegion | None] = {}
    for index, entry in enumerate(_as_list(value, what)):
        where = f"{what}[{index}]"
        pair = _as_list(entry, where)
        if len(pair) != 2:
            raise WireError(f"{where}: expected a [kind, region] pair of 2, got {len(pair)}")
        sightings[decode_kind(pair[0], f"{where}.kind")] = decode_opt_region(
            pair[1], f"{where}.region"
        )
    return sightings


def decode_tick(value: Any, what: str = "tick") -> Tick:
    data = _mapping(value, what)
    return Tick(
        seq=_int_at(data, "seq", what),
        generation=_int_at(data, "generation", what),
        at=_float_at(data, "at", what),
        captured=_bool_at(data, "captured", what),
        busy=decode_opt_busy_probe(_field(data, "busy", what), f"{what}.busy"),
        idle=decode_opt_busy_probe(_field(data, "idle", what), f"{what}.idle"),
        stale=decode_opt_stale_probe(_field(data, "stale", what), f"{what}.stale"),
        sightings=_decode_sightings(_field(data, "sightings", what), f"{what}.sightings"),
        active_detectors=_strs_at(data, "active_detectors", what),
        stale_arm_streak=_int_at(data, "stale_arm_streak", what),
        changed_streak=_int_at(data, "changed_streak", what),
    )


# -- per-verb plumbing ---------------------------------------------------------
#
# One table, both directions, for every verb of the ``UIMonitor`` Protocol.
# ``latest``, ``generation``, ``subscribe``, ``on_clip`` and ``observe`` are
# absent because none of them is a round trip: the first two are local reads by
# contract (§2.1), the hooks are client-side registrations, and ``observe`` is
# answered off the pushed tick stream rather than by asking (§8: the backlog
# policy is drop-to-latest, and observe only ever wants the newest).

_NO_DEFAULT = object()


@dataclass(frozen=True, slots=True)
class _Param:
    name: str
    encode: Callable[[Any], Any]
    decode: Callable[[Any, str], Any]
    # Present only for parameters the Python signature gives a default: a caller
    # may omit them, and they are still written to the wire in full, so the far
    # side never has to know what the near side's default was.
    default: Any = _NO_DEFAULT


@dataclass(frozen=True, slots=True)
class _Value:
    encode: Callable[[Any], Any]
    decode: Callable[[Any, str], Any]


def _identity(value: Any) -> Any:
    return value


def _encode_none(value: Any) -> Any:
    if value is not None:
        raise WireError(f"expected no value, got {type(value).__name__}")
    return None


def _decode_none(value: Any, what: str = "value") -> None:
    if value is not None:
        raise WireError(f"{what}: expected null, got {type(value).__name__}")
    return None


_STR = (_identity, _as_str)
_INT = (_identity, _as_int)
_BOOL = (_identity, _as_bool)
_OPT_FLOAT = (_identity, _as_opt_float)

#: The retarget, and the only one on this wire. ``configure`` is deliberately
#: NOT here (§10.5): a brain that could send a spec would be naming a service, a
#: rectangle and a search tolerance on a desktop it cannot see, which is the
#: disagreement wave 3 exists to end. The monitor runs its own configuration for
#: the window it is asked about and answers with the whole of it.
WATCH = "watch"
SUSPEND = "suspend"
RESUME = "resume"
CLOSE = "close"

_PARAMS: dict[str, tuple[_Param, ...]] = {
    WATCH: (_Param("slot", encode_slot, decode_slot),),
    "watched": (),
    SUSPEND: (),
    RESUME: (),
    # In the table because the Protocol has it, and answered by the server as an
    # orderly goodbye - see server.py. It is NOT "shut the monitor down": a
    # monitor outlives every brain that dials it (§2.8), so the client's own
    # ``close()`` tears down the LINK and sends nothing.
    CLOSE: (),
    "focus_window": (_Param("handle", *_INT),),
    "foreground_window": (),
    "click": (
        _Param("region", encode_region, decode_region),
        _Param("settle_s", *_OPT_FLOAT, None),
    ),
    "move_cursor": (_Param("x", *_INT), _Param("y", *_INT)),
    "scroll": (
        _Param("region", encode_region, decode_region),
        _Param("detents", *_INT),
    ),
    "scroll_key": (_Param("key", *_STR), _Param("taps", *_INT, 1)),
    "send_paste": (),
    "send_enter": (),
    "read_clipboard": (),
    "write_clipboard": (_Param("text", *_STR),),
    "watch_clipboard": (_Param("on", *_BOOL),),
    "find_all": (_Param("kind", encode_kind, decode_kind),),
    "locate": (
        _Param("kind", encode_kind, decode_kind),
        _Param("exclude_kinds", encode_kinds, decode_kinds, ()),
    ),
    "click_element": (
        _Param("kind", encode_kind, decode_kind),
        _Param("settle_s", *_OPT_FLOAT, None),
    ),
    "hover_scan": (_Param("kind", encode_kind, decode_kind),),
    "snap_to_bottom": (_Param("action", *_STR),),
}

_RESULTS: dict[str, _Value] = {
    WATCH: _Value(encode_watched, decode_watched),
    "watched": _Value(encode_watched, decode_watched),
    SUSPEND: _Value(_encode_none, _decode_none),
    RESUME: _Value(_encode_none, _decode_none),
    CLOSE: _Value(_encode_none, _decode_none),
    "focus_window": _Value(_identity, _as_bool),
    "foreground_window": _Value(_identity, _as_opt_int),
    "click": _Value(_identity, _as_bool),
    "move_cursor": _Value(_identity, _as_bool),
    "scroll": _Value(_identity, _as_bool),
    "scroll_key": _Value(_identity, _as_bool),
    "send_paste": _Value(_identity, _as_bool),
    "send_enter": _Value(_identity, _as_bool),
    "read_clipboard": _Value(_identity, _as_opt_str),
    "write_clipboard": _Value(_encode_none, _decode_none),
    "watch_clipboard": _Value(_identity, _as_bool),
    "find_all": _Value(encode_regions, decode_regions),
    "locate": _Value(encode_located, decode_located),
    "click_element": _Value(encode_element_click, decode_element_click),
    "hover_scan": _Value(encode_located, decode_located),
    "snap_to_bottom": _Value(_encode_none, _decode_none),
}

#: Every verb this wire carries, in table order. The server dispatches on
#: membership of this set and nothing else.
VERBS: tuple[str, ...] = tuple(_PARAMS)

assert set(_PARAMS) == set(_RESULTS), "every verb needs both a params row and a result row"


def _params_for(verb: str) -> tuple[_Param, ...]:
    try:
        return _PARAMS[verb]
    except KeyError:
        raise WireError(f"unknown verb {verb!r}") from None


def encode_params(verb: str, **kwargs: Any) -> dict[str, Any]:
    """The ``params`` object for one call, by keyword, exactly as the Python
    method takes them. Every parameter is written, defaults included."""
    fields = _params_for(verb)
    unknown = sorted(set(kwargs) - {field.name for field in fields})
    if unknown:
        raise WireError(f"{verb}: unknown parameter(s) {unknown}")
    params: dict[str, Any] = {}
    for field in fields:
        if field.name in kwargs:
            value = kwargs[field.name]
        elif field.default is not _NO_DEFAULT:
            value = field.default
        else:
            raise WireError(f"{verb}: missing parameter {field.name!r}")
        params[field.name] = field.encode(value)
    return params


def decode_params(verb: str, params: Any) -> dict[str, Any]:
    """One call's ``params`` back into the keyword arguments the verb takes."""
    fields = _params_for(verb)
    what = f"{verb}.params"
    data = _mapping(params, what)
    unknown = sorted(set(data) - {field.name for field in fields})
    if unknown:
        raise WireError(f"{what}: unknown parameter(s) {unknown}")
    kwargs: dict[str, Any] = {}
    for field in fields:
        if field.name in data:
            kwargs[field.name] = field.decode(data[field.name], f"{what}.{field.name}")
        elif field.default is not _NO_DEFAULT:
            kwargs[field.name] = field.default
        else:
            raise WireError(f"{what}: missing parameter {field.name!r}")
    return kwargs


def encode_result(verb: str, value: Any) -> Any:
    """One verb's return value as the ``value`` of a ``result`` frame."""
    try:
        codec = _RESULTS[verb]
    except KeyError:
        raise WireError(f"unknown verb {verb!r}") from None
    return codec.encode(value)


def decode_result(verb: str, payload: Any) -> Any:
    """A ``result`` frame's ``value`` back into what the verb returns."""
    try:
        codec = _RESULTS[verb]
    except KeyError:
        raise WireError(f"unknown verb {verb!r}") from None
    return codec.decode(payload, f"{verb}.result")


# -- frames --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallFrame:
    id: int
    verb: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    """``id`` is ``None`` for a failure that belongs to the CONNECTION rather
    than to a call - the refusal a second brain gets (§2.8)."""

    id: int | None
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class Hello:
    """The client's first line, once it has been checked.

    Two fields rather than one, because the two are answered by different parts
    of the server: the versions are a WIRE question the decoder has already
    settled by the time this exists, and the token is a POLICY question only the
    server that owns the secret can answer.
    """

    versions: Versions
    token: str | None


@dataclass(frozen=True, slots=True)
class HelloAck:
    """The server's reply, once it has been checked."""

    server_id: str
    versions: Versions
    clipboard_kind: str | None


def frame_type(frame: dict[str, Any]) -> str:
    """The frame's ``type``, checked against the v4 vocabulary."""
    kind = _str_at(frame, "type", "frame")
    if kind not in FRAME_TYPES:
        raise WireError(f"unknown frame type {kind!r}")
    return kind


def _typed(frame: dict[str, Any], expected: str) -> dict[str, Any]:
    kind = frame_type(frame)
    if kind != expected:
        raise WireError(f"expected a {expected!r} frame, got {kind!r}")
    return frame


def _read_versions(frame: dict[str, Any], what: str) -> Versions:
    """Both of the peer's numbers, package FIRST.

    The order matters: the package is parsed before the wire version is
    compared, so a peer speaking another version is refused with its install
    already in hand instead of with an integer nobody can act on.
    """
    package = _str_at(frame, "package", what)
    if not package:
        raise WireError(f"{what}: empty package version")
    peer = Versions(wire=_int_at(frame, "version", what), package=package)
    if peer.wire != MONITOR_WIRE_VERSION:
        raise WireVersionError(what, peer)
    return peer


def hello_frame(token: str | None = None) -> dict[str, Any]:
    """The dial. ``token`` is always written, ``null`` included: the far side
    reads one field either way, so "this build has no auth" cannot be confused
    with "this build sent nothing"."""
    return {
        "type": "hello",
        "version": OURS.wire,
        "package": OURS.package,
        "token": token,
    }


def read_hello(frame: dict[str, Any]) -> Hello:
    """The client's versions and token, or :class:`WireVersionError` naming both
    installs.

    The version gate fires FIRST (inside ``_read_versions``), which is what
    keeps a v1 client's tokenless hello a version error rather than an
    authentication one - the message a human can act on is "upgrade the other
    half", not "your token is wrong".
    """
    data = _typed(frame, "hello")
    versions = _read_versions(data, "hello")
    return Hello(
        versions=versions,
        token=_as_opt_str(_field(data, "token", "hello"), "hello.token"),
    )


def hello_ack_frame(server_id: str, clipboard_kind: str | None) -> dict[str, Any]:
    return {
        "type": "hello_ack",
        "version": OURS.wire,
        "package": OURS.package,
        "server_id": server_id,
        "clipboard_kind": clipboard_kind,
    }


def read_hello_ack(frame: dict[str, Any]) -> HelloAck:
    """The server's identity, versions and clipboard backend."""
    data = _typed(frame, "hello_ack")
    versions = _read_versions(data, "hello_ack")
    server_id = _str_at(data, "server_id", "hello_ack")
    if not server_id:
        raise WireError("hello_ack: empty server_id")
    return HelloAck(
        server_id=server_id,
        versions=versions,
        clipboard_kind=_as_opt_str(
            _field(data, "clipboard_kind", "hello_ack"), "hello_ack.clipboard_kind"
        ),
    )


def call_frame(call_id: int, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    if verb not in _PARAMS:
        raise WireError(f"unknown verb {verb!r}")
    return {"type": "call", "id": call_id, "verb": verb, "params": params}


def read_call(frame: dict[str, Any]) -> CallFrame:
    data = _typed(frame, "call")
    verb = _str_at(data, "verb", "call")
    if verb not in _PARAMS:
        raise WireError(f"unknown verb {verb!r}")
    return CallFrame(
        id=_int_at(data, "id", "call"),
        verb=verb,
        params=_mapping(_field(data, "params", "call"), "call.params"),
    )


def result_frame(call_id: int, value: Any) -> dict[str, Any]:
    return {"type": "result", "id": call_id, "value": value}


def read_result(frame: dict[str, Any]) -> tuple[int, Any]:
    data = _typed(frame, "result")
    return _int_at(data, "id", "result"), _field(data, "value", "result")


def error_frame(call_id: int | None, kind: str, message: str) -> dict[str, Any]:
    if kind not in ERROR_KINDS:
        raise WireError(f"unknown error kind {kind!r}")
    return {"type": "error", "id": call_id, "kind": kind, "message": message}


def read_error(frame: dict[str, Any]) -> ErrorFrame:
    data = _typed(frame, "error")
    return ErrorFrame(
        id=_opt_int_at(data, "id", "error"),
        kind=_as_literal(_field(data, "kind", "error"), ERROR_KINDS, "error.kind"),
        message=_str_at(data, "message", "error"),
    )


def tick_frame(tick: Tick) -> dict[str, Any]:
    return {"type": "tick", "tick": encode_tick(tick)}


def read_tick(frame: dict[str, Any]) -> Tick:
    data = _typed(frame, "tick")
    return decode_tick(_field(data, "tick", "tick"), "tick.tick")


def clip_frame(text: str) -> dict[str, Any]:
    return {"type": "clip", "text": text}


def read_clip(frame: dict[str, Any]) -> str:
    return _str_at(_typed(frame, "clip"), "text", "clip")
