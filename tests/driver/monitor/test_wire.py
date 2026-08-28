"""The monitor wire's codecs, one round trip each.

docs/design/ui-monitor.md §2.7 and §6.5. Everything here is the same assertion
said about a different value: *encode then decode is identity*. That is the only
property a codec on a two-machine seam owes, and it is the one a hand-written
``decode`` silently loses the day somebody adds a field to the dataclass and not
to the reader.

Two things are pinned beyond the round trips, because both are places a wire
goes wrong quietly rather than loudly:

* **The table covers the Protocol.** ``test_every_verb_round_trips`` iterates
  :data:`~agentclip.driver.monitor.wire.VERBS` rather than a list written out
  here, so a verb added to ``UIMonitor`` and to the table is exercised the same
  day, and a verb added to one of them and not the other fails.
* **Three-state sightings survive.** ``absent`` (never searched), ``None``
  (searched, not on screen) and a rectangle are three different facts, and the
  first is the one a naive mapping loses.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentclip.driver.monitor.protocol import (
    EMPTY_WATCHED,
    ElementClick,
    Located,
    Tick,
    Watched,
)
from agentclip.driver.monitor.wire import (
    ERROR_KINDS,
    MONITOR_WIRE_VERSION,
    OURS,
    VERBS,
    Versions,
    WireError,
    WireVersionError,
    call_frame,
    clip_frame,
    decode_busy_probe,
    decode_kind,
    decode_line,
    decode_located,
    decode_params,
    decode_region,
    decode_result,
    decode_slot,
    decode_stale_probe,
    decode_tick,
    decode_watched,
    encode_busy_probe,
    encode_kind,
    encode_line,
    encode_located,
    encode_params,
    encode_region,
    encode_result,
    encode_slot,
    encode_stale_probe,
    encode_tick,
    encode_watched,
    error_frame,
    hello_ack_frame,
    hello_frame,
    read_call,
    read_clip,
    read_error,
    read_hello,
    read_hello_ack,
    read_result,
    read_tick,
    result_frame,
    tick_frame,
)
from agentclip.driver.screen.busy import BusyProbe, BusyState
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.region import ScreenRegion
from agentclip.driver.screen.slot import AgentSlot
from agentclip.driver.screen.stale import StaleProbe, StaleState

COPY = TemplateKind.COPY
CHATBOX = TemplateKind.CHATBOX_ONGOING

# Deliberately not at the origin, and deliberately negative in one axis: a
# monitor to the LEFT of the primary one has negative origins, and this wire
# carries virtual-screen coordinates.
REGION = ScreenRegion(-1200, 40, 640, 480)

# What ``watch`` answers with: the monitor's whole effective service (§10.5).
# Every field set to something OTHER than its default, so a codec that dropped
# one fails rather than accidentally agreeing with the constructor.
WATCHED = Watched(
    service="claude",
    region=REGION,
    profiled=True,
    label="Claude web",
    generation=9,
    delivery="stream",
    auto_submit=True,
    submit_delay_s=3.5,
    scroll_action="end",
    snap_back=False,
    hover_scan=True,
    max_paste_chars=12_000,
    total_context_chars=400_000,
    wrap_blocks_in_fence=False,
    attachment_note=False,
    require_fenced_reply=True,
    extra_instructions="mind the ]( sequences",
    captured=(COPY, CHATBOX),
)

# The one pixel a click on ``REGION`` would land on, as ``Located.target``
# carries it: a 1x1 rectangle, because that is what every click here takes.
TARGET = ScreenRegion(-1000, 300, 1, 1)

TICK = Tick(
    seq=7,
    generation=3,
    at=1234.5,
    captured=True,
    busy=BusyProbe(BusyState.CHANGED, 0.4, generating_now=True),
    idle=BusyProbe(BusyState.MATCH, None),
    stale=StaleProbe(StaleState.STALE, 0.0, 4),
    # All three states of the map at once: found, searched-and-absent, and (by
    # omission) never searched at all.
    sightings={COPY: ScreenRegion(10, 20, 30, 40), CHATBOX: None},
    active_detectors=("busy", "stale"),
    stale_arm_streak=2,
    changed_streak=1,
)


# == the value codecs ==========================================================


def test_a_region_round_trips_with_signed_origins() -> None:
    assert decode_region(encode_region(REGION)) == REGION


@pytest.mark.parametrize(
    "probe",
    [
        BusyProbe(BusyState.MATCH, 0.125, generating_now=True),
        BusyProbe(BusyState.ERROR, None),
        BusyProbe(BusyState.CHANGED, 1.0),
    ],
)
def test_a_busy_probe_round_trips(probe: BusyProbe) -> None:
    """Including the ERROR case, whose ``diff`` is None - the reading with no
    frame behind it, which is exactly the one a lazy codec turns into 0.0."""
    assert decode_busy_probe(encode_busy_probe(probe)) == probe


@pytest.mark.parametrize(
    "probe",
    [
        StaleProbe(StaleState.STALE, 0.0, 5),
        StaleProbe(StaleState.CHANGING, 0.3, 0),
        StaleProbe(StaleState.ERROR, None, 0),
    ],
)
def test_a_stale_probe_round_trips(probe: StaleProbe) -> None:
    assert decode_stale_probe(encode_stale_probe(probe)) == probe


def test_every_template_kind_round_trips() -> None:
    """By VALUE, because the value is the identity ``profile_store`` already
    writes to disk - the wire and the store say the same word."""
    for kind in TemplateKind:
        assert decode_kind(encode_kind(kind)) is kind


def test_every_slot_round_trips() -> None:
    """By VALUE, like ``TemplateKind`` and unlike the state enums: ``master`` /
    ``subagent`` are what a slot IS everywhere else in the program."""
    for slot in AgentSlot:
        assert decode_slot(encode_slot(slot)) is slot


def test_a_slot_that_is_not_one_is_refused() -> None:
    with pytest.raises(WireError):
        decode_slot("supervisor")


def test_the_whole_effective_service_round_trips() -> None:
    """Every field, including the eleven preset ones: this value is the ONLY
    way a brain learns what service it is driving (§10.5), so a field lost in
    the codec is a turn composed against the wrong budget."""
    assert decode_watched(encode_watched(WATCHED)) == WATCHED


def test_a_watched_with_nothing_watched_round_trips() -> None:
    """What a monitor answers before it has been pointed at anything, and what
    an idle handle answers for ever."""
    assert decode_watched(encode_watched(EMPTY_WATCHED)) == EMPTY_WATCHED


def test_the_spec_is_not_on_this_wire_at_all() -> None:
    """§10.5's rule, as a table assertion: a brain may not name a service, a
    rectangle or a search tolerance on a desktop it cannot see."""
    assert "configure" not in VERBS
    assert "watch" in VERBS


@pytest.mark.parametrize(
    "located",
    [
        Located(region=REGION, ambiguous=False, best_miss=None, target=TARGET),
        Located(region=REGION, ambiguous=True, best_miss=None, target=TARGET),
        Located(region=None, ambiguous=False, best_miss=0.42),
        Located(region=None, ambiguous=False, best_miss=None),
    ],
)
def test_a_located_round_trips(located: Located) -> None:
    """All four shapes, because the whole point of the four fields is that a
    caller with only ``region`` cannot tell the failures apart - and a caller
    with only ``region`` does not know which pixel to press."""
    assert decode_located(encode_located(located)) == located


def test_the_click_target_is_on_the_wire_rather_than_recomputed() -> None:
    """§11.3: the click point is applied on the machine that holds the pictures.

    Asserted as a field on the frame, because the failure this replaces was a
    brain doing the arithmetic against its own empty profile store - which is
    silent, and lands the press in the middle of a control the service labelled
    there.
    """
    frame = encode_located(Located(REGION, False, None, TARGET))
    assert frame["target"] == {"left": -1000, "top": 300, "width": 1, "height": 1}


def test_a_watched_carries_which_appearances_the_monitor_has() -> None:
    """The kinds ride as their own values, and an empty tuple is a value:
    "profiled, nothing captured" is the answer that refuses every click."""
    frame = encode_watched(WATCHED)
    assert frame["captured"] == ["copy", "chatbox-ongoing"]
    assert decode_watched(encode_watched(EMPTY_WATCHED)).captured == ()


def test_a_captured_kind_this_build_has_never_heard_of_is_dropped() -> None:
    """The one tolerant decode on this wire (see ``_captured_at``): the version
    gate pins the SHAPE, not the enum inside it, so a monitor a release ahead
    may hold an appearance this brain cannot name - and "no, I have not got one
    of those" is the honest answer to a question about a kind that does not
    exist here, not a dead connection."""
    frame = {**encode_watched(WATCHED), "captured": ["copy", "hologram", 7]}
    assert decode_watched(frame).captured == (COPY,)


def test_a_watched_with_no_captured_list_at_all_reads_as_nothing_captured() -> None:
    frame = {key: value for key, value in encode_watched(WATCHED).items() if key != "captured"}
    assert decode_watched(frame).captured == ()


def test_a_tick_round_trips_whole() -> None:
    assert decode_tick(encode_tick(TICK)) == TICK


def test_a_tick_keeps_the_three_states_of_a_sighting() -> None:
    """Found, searched-and-absent, never-searched. The third is the one a
    mapping that only carried "where things are" would lose."""
    back = decode_tick(encode_tick(TICK))
    assert back.locate(COPY) == ScreenRegion(10, 20, 30, 40)
    assert back.searched(CHATBOX) and back.present(CHATBOX) is False
    assert not back.searched(TemplateKind.NEW_CHAT)
    assert back.present(TemplateKind.NEW_CHAT) is None


def test_a_failed_capture_tick_round_trips() -> None:
    """A frame that failed searched nothing, so the map is empty - and empty is
    not the same as absent-field."""
    blind = Tick(
        seq=1,
        generation=1,
        at=0.0,
        captured=False,
        busy=None,
        idle=None,
        stale=None,
        sightings={},
        active_detectors=(),
    )
    assert decode_tick(encode_tick(blind)) == blind


# == the per-verb table ========================================================

# One sample call and one sample return per verb. Written out rather than
# generated, because the point is to state what each verb's shapes ARE - and the
# test below fails if a verb appears in the table with no row here, which is how
# a new verb gets noticed.
CALLS: dict[str, dict[str, Any]] = {
    "watch": {"slot": AgentSlot.SUBAGENT},
    "suspend": {},
    "resume": {},
    "close": {},
    "set_theme": {"theme": "claude-dark"},
    "focus_window": {"handle": 12345},
    "foreground_window": {},
    "click": {"region": REGION, "settle_s": 0.25},
    "move_cursor": {"x": -40, "y": 900},
    "scroll": {"region": REGION, "detents": -3},
    "scroll_key": {"key": "page_down", "taps": 4},
    "send_paste": {},
    "send_enter": {},
    "read_clipboard": {},
    "write_clipboard": {"text": "a reply\nwith a newline"},
    "watch_clipboard": {"on": True},
    "find_all": {"kind": COPY},
    "locate": {"kind": COPY, "exclude_kinds": (CHATBOX, TemplateKind.SEND_READY)},
    "click_element": {"kind": COPY, "settle_s": None},
    "hover_scan": {"kind": COPY},
    "watched": {},
    "snap_to_bottom": {"action": "wheel"},
}

RETURNS: dict[str, Any] = {
    "watch": WATCHED,
    "suspend": None,
    "resume": None,
    "close": None,
    "set_theme": None,
    "focus_window": True,
    "foreground_window": 99887,
    "click": False,
    "move_cursor": True,
    "scroll": True,
    "scroll_key": False,
    "send_paste": True,
    "send_enter": False,
    "read_clipboard": "what was on the clipboard",
    "write_clipboard": None,
    "watch_clipboard": True,
    "find_all": (REGION, ScreenRegion(0, 0, 4, 4)),
    "locate": Located(region=REGION, ambiguous=True, best_miss=None, target=TARGET),
    "click_element": ElementClick.AMBIGUOUS,
    "hover_scan": Located(region=REGION, ambiguous=False, best_miss=None, target=TARGET),
    "snap_to_bottom": None,
    "watched": WATCHED,
}


@pytest.mark.parametrize("verb", VERBS)
def test_every_verb_round_trips(verb: str) -> None:
    """Params and result, both directions, for every verb the table carries.

    Parametrised over ``VERBS`` itself: a verb added to ``UIMonitor`` and to the
    table is covered the same day it lands, and one added to the table with no
    sample here fails on the KeyError rather than passing vacuously.
    """
    kwargs = CALLS[verb]
    assert decode_params(verb, encode_params(verb, **kwargs)) == kwargs
    value = RETURNS[verb]
    assert decode_result(verb, encode_result(verb, value)) == value


def test_an_omitted_default_is_still_written_in_full() -> None:
    """The far side never has to know what the near side's default was."""
    params = encode_params("scroll_key", key="end")
    assert params == {"key": "end", "taps": 1}
    assert decode_params("scroll_key", params) == {"key": "end", "taps": 1}


def test_an_unknown_verb_is_refused_in_both_directions() -> None:
    with pytest.raises(WireError):
        encode_params("teleport", where="mars")
    with pytest.raises(WireError):
        decode_result("teleport", None)


def test_an_unknown_parameter_is_refused() -> None:
    with pytest.raises(WireError):
        encode_params("scroll_key", key="end", speed=3)
    with pytest.raises(WireError):
        decode_params("scroll_key", {"key": "end", "taps": 1, "speed": 3})


def test_a_missing_parameter_is_refused() -> None:
    with pytest.raises(WireError):
        decode_params("scroll_key", {"taps": 1})


def test_a_boolean_is_never_an_integer_on_this_wire() -> None:
    """``True`` is an int in Python and never one here: a detent count that
    decoded ``true`` into 1 would scroll a window nobody asked to scroll."""
    with pytest.raises(WireError):
        decode_params("scroll", {"region": encode_region(REGION), "detents": True})


# == lines and frames ==========================================================


def test_a_line_is_one_line_even_with_newlines_in_it() -> None:
    line = encode_line(clip_frame("two\nlines\r\nand a tab\t"))
    assert line.endswith("\n") and line.count("\n") == 1
    assert read_clip(decode_line(line)) == "two\nlines\r\nand a tab\t"


def test_text_rides_as_itself_rather_than_as_escapes() -> None:
    assert "em—dash" in encode_line(clip_frame("em—dash"))


def test_a_line_that_is_not_a_json_object_is_refused() -> None:
    for bad in ("not json at all", "[1,2,3]", '{"no":"type"}'):
        with pytest.raises(WireError):
            decode_line(bad)


def test_the_handshake_round_trips() -> None:
    hello = read_hello(decode_line(encode_line(hello_frame())))
    assert (hello.versions, hello.token) == (OURS, None)
    ack = read_hello_ack(decode_line(encode_line(hello_ack_frame("srv-1", "copykitten"))))
    assert (ack.server_id, ack.versions, ack.clipboard_kind) == ("srv-1", OURS, "copykitten")


def test_the_hello_carries_the_token_and_null_is_a_value() -> None:
    """§5's secret rides the first line, and "no token" is stated rather than
    omitted: the server reads one field either way."""
    secret = "a" * 32
    assert read_hello(decode_line(encode_line(hello_frame(secret)))).token == secret
    assert hello_frame()["token"] is None
    assert read_hello(hello_frame()).token is None


def test_the_hello_carries_the_theme_and_leaving_it_out_is_a_value() -> None:
    """§11.7. The one OPTIONAL field on this wire, both ways.

    Written only when there is one (a hello with no palette to name carries no
    ``theme`` key at all) and read with a default (a hello from a brain that
    never heard of themes decodes to ``None``), which together are why adding
    it did not take the version to 5.
    """
    assert read_hello(decode_line(encode_line(hello_frame(None, "claude-warm")))).theme == (
        "claude-warm"
    )
    assert "theme" not in hello_frame()
    assert read_hello(hello_frame()).theme is None
    assert read_hello({**hello_frame(), "theme": None}).theme is None


def test_a_hello_with_a_non_string_theme_is_refused() -> None:
    """A tolerant READ is not a tolerant decode: absent is a value, 7 is not."""
    with pytest.raises(WireError):
        read_hello({**hello_frame(), "theme": 7})


def test_a_hello_with_a_non_string_token_is_refused() -> None:
    with pytest.raises(WireError):
        read_hello({**hello_frame(), "token": 7})
    with pytest.raises(WireError):
        read_hello({key: value for key, value in hello_frame().items() if key != "token"})


def test_the_wire_version_is_the_monitors_own_and_is_four() -> None:
    """1 -> 2 when the hello grew a token (§5); 2 -> 3 when ``configure`` left
    the verb table and ``watch`` took its place (§10.5); 3 -> 4 when the brain
    stopped holding templates and ``captured`` and ``target`` became the
    monitor's answers (§11.3); 4 -> 5 when ``Watched`` grew ``submit_delay_s``,
    the per-service beat before the auto-submit Enter (§11.8) - a field the
    brain READS on every delivery, so an end that does not send it is not a
    peer this one can drive. Asserted as a NUMBER on purpose: the two installs
    gate on it, and a silent bump is a silent refusal of every monitor that was
    not upgraded with the brain.

    §11.7 did NOT make it 5, and that is the exception worth stating: the
    hello's ``theme`` and the ``set_theme`` verb are both additive and both
    tolerated by an end that has neither - an old monitor ignores an unknown
    field and refuses an unknown verb with one ``bad_request`` the Chat UI
    swallows, and no frame about a click, a capture or a clipboard changed
    shape."""
    assert MONITOR_WIRE_VERSION == 5
    assert OURS.wire == 5
    assert hello_frame()["version"] == 5
    assert hello_ack_frame("srv-1", None)["version"] == 5


def test_unauthorized_is_an_error_kind_that_belongs_to_the_connection() -> None:
    """No id, because it answers no call - the same shape as the busy refusal."""
    assert "unauthorized" in ERROR_KINDS
    error = read_error(decode_line(encode_line(error_frame(None, "unauthorized", "nope"))))
    assert (error.id, error.kind, error.message) == (None, "unauthorized", "nope")


def test_a_monitor_with_no_clipboard_says_so_in_the_handshake() -> None:
    """``None`` and ``"manual"`` are different machines and the client answers
    ``watch_clipboard`` off exactly this field."""
    assert read_hello_ack(hello_ack_frame("srv-1", None)).clipboard_kind is None
    assert read_hello_ack(hello_ack_frame("srv-1", "manual")).clipboard_kind == "manual"


def test_a_version_mismatch_names_both_installs() -> None:
    with pytest.raises(WireVersionError) as caught:
        read_hello({"type": "hello", "version": 99, "package": "9.9.9", "token": None})
    error = caught.value
    assert error.peer == Versions(wire=99, package="9.9.9")
    assert error.ours == OURS
    # Both numbers AND both package versions, because the half a human can act
    # on is the install, not the protocol integer.
    assert "99" in str(error) and "9.9.9" in str(error) and OURS.package in str(error)


def test_a_call_frame_round_trips() -> None:
    frame = call_frame(11, "scroll_key", encode_params("scroll_key", key="end"))
    call = read_call(decode_line(encode_line(frame)))
    assert (call.id, call.verb) == (11, "scroll_key")
    assert decode_params(call.verb, call.params) == {"key": "end", "taps": 1}


def test_a_call_frame_refuses_an_unknown_verb_on_both_sides() -> None:
    with pytest.raises(WireError):
        call_frame(1, "teleport", {})
    with pytest.raises(WireError):
        read_call({"type": "call", "id": 1, "verb": "teleport", "params": {}})


def test_a_result_frame_round_trips() -> None:
    frame = result_frame(11, encode_result("locate", RETURNS["locate"]))
    call_id, value = read_result(decode_line(encode_line(frame)))
    assert call_id == 11
    assert decode_result("locate", value) == RETURNS["locate"]


def test_an_error_frame_carries_a_null_id_for_a_connection_level_failure() -> None:
    """Which is what the second brain's refusal is: it answers no call (§2.8)."""
    frame = error_frame(None, "busy", "already attached from 127.0.0.1:5001")
    error = read_error(decode_line(encode_line(frame)))
    assert (error.id, error.kind) == (None, "busy")
    assert "127.0.0.1:5001" in error.message


def test_an_error_frame_carries_the_call_it_answers() -> None:
    error = read_error(decode_line(encode_line(error_frame(3, "internal", "boom"))))
    assert (error.id, error.kind, error.message) == (3, "internal", "boom")


def test_an_unknown_error_kind_is_refused() -> None:
    with pytest.raises(WireError):
        error_frame(1, "weather", "raining")
    with pytest.raises(WireError):
        read_error({"type": "error", "id": 1, "kind": "weather", "message": "raining"})


def test_a_tick_frame_round_trips() -> None:
    assert read_tick(decode_line(encode_line(tick_frame(TICK)))) == TICK


def test_an_unknown_frame_type_is_refused() -> None:
    with pytest.raises(WireError):
        read_tick({"type": "telemetry"})
