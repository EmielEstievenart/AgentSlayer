"""The browser-automation loop: what AgentClip is doing to the chat window.

Deliberately NOT ``engine.states.Phase``. The engine's machine says where the
TASK is (awaiting reply, review, done); this one says where the round trip
through the browser is - the paste, the send, the generation, the copy - which
is the loop the user actually watches the tool run and the one the sidebar's
STATE rail draws. The evidence behind it is scattered by design (the send
gate, the finish detectors' verdicts, the auto-copy flow's outcome); this enum
is the one story they add up to, and MainScreen is its only writer.

``LOOP_TRANSITIONS`` is the legal-next table the rail's styling reads, in the
same shape as ``engine.states.TRANSITIONS``. It describes the loop's forward
motion only: session teardown (``/new``) sends every state home to IDLE, and
that is a reset rather than a transition - drawing "idle" as reachable from
everywhere would make the rail's legal-next brightness meaningless.
"""

from __future__ import annotations

from enum import Enum, auto


class LoopState(Enum):
    """One full turn of the copy-paste loop, in the order it runs."""

    IDLE = auto()  # nothing outstanding; the user's next chat message starts the loop
    AUTO_INSERT = auto()  # outbound copied; clicking the chat box and pasting it in
    MANUAL_INSERT = auto()  # the click/paste did not land; the user Ctrl+Vs it themselves
    WAIT_SEND = auto()  # the payload is in the chat box; waiting for the user's Enter
    WAIT_GENERATE = auto()  # sent, and the model is visibly generating
    AUTO_COPY = auto()  # generation stopped; hunting for the reply's copy button
    MANUAL_COPY = auto()  # no copy button to click; the user copies the reply themselves
    INTERPRETING = auto()  # the reply is on the clipboard; parsing and acting on it


LOOP_TRANSITIONS: dict[LoopState, frozenset[LoopState]] = {
    # A chat message became an outbound payload (``copy_outbound``).
    LoopState.IDLE: frozenset({LoopState.AUTO_INSERT}),
    # The focus click + synthetic Ctrl+V either landed (wait for the send) or
    # did not (the user is asked to paste).
    LoopState.AUTO_INSERT: frozenset({LoopState.WAIT_SEND, LoopState.MANUAL_INSERT}),
    # A manual paste is proven by the ready-to-send button appearing - or, for
    # a service without that capture, only by the generation it leads to.
    LoopState.MANUAL_INSERT: frozenset({LoopState.WAIT_SEND, LoopState.WAIT_GENERATE}),
    # The send button going away, or a busy icon / sustained streaming delta,
    # is the user's Enter.
    LoopState.WAIT_SEND: frozenset({LoopState.WAIT_GENERATE}),
    # The finish detectors agreeing "stopped" fires the auto-copy flow - unless
    # there is no captured copy button, in which case the harvest is the user's.
    LoopState.WAIT_GENERATE: frozenset({LoopState.AUTO_COPY, LoopState.MANUAL_COPY}),
    # The flow's click puts the reply on the clipboard; any failure (capture,
    # search, a click that did not take) hands the copy to the user.
    LoopState.AUTO_COPY: frozenset({LoopState.INTERPRETING, LoopState.MANUAL_COPY}),
    LoopState.MANUAL_COPY: frozenset({LoopState.INTERPRETING}),
    # The turn runs (parse, gate, execute); its next outbound restarts the loop,
    # and a turn that ends waiting on the user settles back to idle.
    LoopState.INTERPRETING: frozenset({LoopState.AUTO_INSERT, LoopState.IDLE}),
}
