"""The loop's one pure table: ``(state, outcome) -> state``.

docs/design/ui-monitor.md §2.4. This is the authority - the picture of the
machine, with no screen, no clock and no I/O in it, so reading it is how anyone
finds out what the automation can do next. :data:`TRANSITIONS` is TOTAL over
what the recipes can actually return, and ``tests/driver/automation`` pins that
in both directions: every pair a recipe can produce has a row, and every row is
reachable.

Two things are deliberately NOT in it, because they are not outcomes of a recipe
at all - they happen TO the loop, from the shell, through
``AutomationController.set_loop_state``:

* **losing the monitor link** (§2.9), which can land in any state and parks the
  loop in ``DISCONNECTED`` until a redial;
* **the user's own copy landing**, which is what takes ``MANUAL_COPY`` to
  ``INTERPRETING``, and **a turn finishing**, which takes ``INTERPRETING`` home
  to ``IDLE``.

:data:`SHELL_EDGES` names those, so that :func:`legal_next` - the display-only
legal-next map the sidebar's STATE rail draws its brightness from, and the thing
``loop_state.LOOP_TRANSITIONS`` now IS - can be derived from one place instead
of maintained twice.
"""

from __future__ import annotations

from agentclip.driver.automation.loop_state import LoopState
from agentclip.driver.automation.recipes.outcomes import Outcome

TRANSITIONS: dict[tuple[LoopState, Outcome], LoopState] = {
    # A chat message became an outbound payload: insert it ourselves.
    (LoopState.IDLE, Outcome.PAYLOAD_READY): LoopState.AUTO_INSERT,
    # The turn's next payload restarts the round trip without settling home.
    (LoopState.INTERPRETING, Outcome.PAYLOAD_READY): LoopState.AUTO_INSERT,
    # The focus click + synthetic Ctrl+V either landed (wait for the send) or
    # did not (the user is asked to paste).
    (LoopState.AUTO_INSERT, Outcome.PASTED): LoopState.WAIT_SEND,
    (LoopState.AUTO_INSERT, Outcome.NOT_PASTED): LoopState.MANUAL_INSERT,
    # A manual paste is proven by the ready-to-send button appearing - or, for a
    # service without that capture, only by the generation it leads to.
    (LoopState.MANUAL_INSERT, Outcome.SEND_PROVEN): LoopState.WAIT_SEND,
    (LoopState.MANUAL_INSERT, Outcome.GENERATING): LoopState.WAIT_GENERATE,
    # The send button going away, or a busy icon / sustained streaming delta, is
    # the user's Enter.
    (LoopState.WAIT_SEND, Outcome.SENT): LoopState.WAIT_GENERATE,
    (LoopState.WAIT_SEND, Outcome.GENERATING): LoopState.WAIT_GENERATE,
    # The finish detectors agreeing "stopped" fires the auto-copy flow - unless
    # there is no captured copy button (or the app is disarmed), in which case
    # the harvest is the user's.
    (LoopState.WAIT_GENERATE, Outcome.FINISHED): LoopState.AUTO_COPY,
    (LoopState.WAIT_GENERATE, Outcome.NO_HARVEST): LoopState.MANUAL_COPY,
    # The flow's click puts the reply on the clipboard; any failure (nothing
    # drawn, nothing found, a click that did not take) hands the copy over.
    (LoopState.AUTO_COPY, Outcome.HARVESTED): LoopState.INTERPRETING,
    (LoopState.AUTO_COPY, Outcome.NOT_HARVESTED): LoopState.MANUAL_COPY,
}

# The moves the SHELL makes, not the loop: they arrive through
# ``set_loop_state`` and pre-empt whatever recipe is running. Named here only so
# the rail's legal-next picture stays complete (see the module docstring).
SHELL_EDGES: dict[LoopState, frozenset[LoopState]] = {
    # The user copied the reply themselves and the watcher caught it.
    LoopState.MANUAL_COPY: frozenset({LoopState.INTERPRETING}),
    # The turn ran out and the floor is back with the user.
    LoopState.INTERPRETING: frozenset({LoopState.IDLE}),
    # The redial landed. IDLE and nothing else: a reconnect RE-DERIVES from the
    # screen rather than resuming (§2.9), and the loop walks back out by
    # observing it.
    LoopState.DISCONNECTED: frozenset({LoopState.IDLE}),
}

# Losing the link happens to every state except the one it lands in.
_LINK_LOST: frozenset[LoopState] = frozenset({LoopState.DISCONNECTED})


def legal_next() -> dict[LoopState, frozenset[LoopState]]:
    """Every state the loop can reach from each state, as one map per state.

    Display only - "nothing reads it back to make a decision" - and derived, so
    a recipe that grows an outcome brightens the rail without anybody editing a
    second table.
    """
    table: dict[LoopState, frozenset[LoopState]] = {}
    for state in LoopState:
        reachable = frozenset(
            after for (before, _outcome), after in TRANSITIONS.items() if before is state
        )
        reachable |= SHELL_EDGES.get(state, frozenset())
        if state is not LoopState.DISCONNECTED:
            reachable |= _LINK_LOST
        table[state] = reachable
    return table
