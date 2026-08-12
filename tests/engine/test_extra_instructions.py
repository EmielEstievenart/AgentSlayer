"""The per-service `extra_instructions` and their one-shot re-inject.

The instructions themselves ride the bootstrap (covered in tests/protocol);
what is pinned here is the OTHER delivery moment - `r` in the TUI, which arms a
reminder that the next payload carries and then clears:

* it rides the next payload of ANY kind, results or a typed follow-up, because
  a session steered by typed messages would otherwise never spend it;
* exactly once, like the permission-mode note it is modelled on;
* the two refusals stay distinguishable, so the UI can say which door is shut:
  IDLE (the bootstrap embeds the instructions anyway) and a preset with nothing
  to re-inject;
* a second press disarms - the status bar is the only readout, so the key that
  lit it has to be able to put it out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine, NewTurn, Send

from .test_approval import transcript

# The conftest make_engine fixture (its EngineFactory alias lives there, but
# tests/ is not a package, so the alias is restated - see test_cancel.py).
EngineFactory = Callable[..., Engine]

EXTRA = "always put a space between ] and ( in code you send"
REMINDER = f"note: user instructions reminder: {EXTRA}"

READ_REPLY = """===CLIP:CALL id=1 tool=read_file===
path: README.md
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
"""


def instructed_config(project: Path, text: str = EXTRA) -> Config:
    cfg = load_config(project, global_config_path=project / "no-such-global.toml")
    services = dict(cfg.services)
    key = cfg.general.service
    services[key] = replace(services[key], extra_instructions=text)
    return replace(cfg, services=services)


@pytest.fixture
def instructed(project: Path, make_engine: EngineFactory) -> Engine:
    return make_engine(config=instructed_config(project))


def test_the_reminder_rides_the_next_results_payload_once(instructed: Engine) -> None:
    instructed.start_task("t")
    assert instructed.arm_extra_instructions() == "armed"

    assert isinstance(instructed.ingest(READ_REPLY), NewTurn)
    step = instructed.execute()
    assert isinstance(step, Send)
    assert REMINDER in step.outbound.chunks[0]

    assert isinstance(instructed.ingest(READ_REPLY), NewTurn)
    step = instructed.execute()
    assert isinstance(step, Send)
    assert "user instructions reminder" not in step.outbound.chunks[0]


def test_the_reminder_rides_a_typed_follow_up_too(instructed: Engine) -> None:
    """"The next thing we send" has to mean the next thing. A user who steers by
    typing produces no results payload for many turns, and a reminder waiting for
    one would be armed for the whole of it."""
    instructed.start_task("t")
    assert instructed.arm_extra_instructions() == "armed"

    payload = instructed.follow_up("actually, do the other file first").chunks[0]
    assert REMINDER in payload
    assert "===CLIP:NOTE===" in payload
    assert payload.index("===CLIP:NOTE===") < payload.index("===CLIP:TASK===")

    assert "user instructions reminder" not in instructed.follow_up("and then this").chunks[0]


def test_arming_twice_disarms(instructed: Engine) -> None:
    """The status segment is the only readout, so the key that lit it is the way
    out of a press the user did not mean."""
    instructed.start_task("t")
    assert instructed.arm_extra_instructions() == "armed"
    assert instructed.status().instructions_armed is True
    assert instructed.arm_extra_instructions() == "disarmed"
    assert instructed.status().instructions_armed is False

    assert isinstance(instructed.ingest(READ_REPLY), NewTurn)
    step = instructed.execute()
    assert isinstance(step, Send)
    assert "user instructions reminder" not in step.outbound.chunks[0]


def test_arming_is_refused_while_idle(instructed: Engine) -> None:
    """There is no next payload - and once there is, it is the bootstrap, which
    embeds the instructions anyway."""
    assert instructed.arm_extra_instructions() == "no-session"
    assert instructed.status().instructions_armed is False


def test_arming_is_refused_when_the_service_carries_nothing(engine: Engine) -> None:
    """Distinct from "no session": the UI has to point at the service editor
    rather than at the start prompt."""
    engine.start_task("t")
    assert engine.arm_extra_instructions() == "no-instructions"
    assert engine.status().has_extra_instructions is False


def test_whitespace_only_instructions_are_nothing_to_re_inject(
    project: Path, make_engine: EngineFactory
) -> None:
    engine = make_engine(config=instructed_config(project, "   \n  "))
    engine.start_task("t")
    assert engine.arm_extra_instructions() == "no-instructions"
    assert engine.status().has_extra_instructions is False


def test_the_status_snapshot_carries_both_flags(instructed: Engine) -> None:
    """What the `r` key reads: whether it exists at all, and whether it is lit."""
    assert instructed.status().has_extra_instructions is True
    instructed.start_task("t")
    assert instructed.status().instructions_armed is False
    instructed.arm_extra_instructions()
    assert instructed.status().instructions_armed is True


def test_a_mode_note_and_a_reminder_ride_together_and_are_each_spent_once(
    instructed: Engine,
) -> None:
    """Two independent one-shots on the same notes channel; neither eats the
    other, and neither survives the payload that carried it."""
    instructed.start_task("t")
    instructed.set_permission_mode("plan")
    assert instructed.arm_extra_instructions() == "armed"

    assert isinstance(instructed.ingest(READ_REPLY), NewTurn)
    step = instructed.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "note: permission mode is now plan" in payload
    assert REMINDER in payload

    assert isinstance(instructed.ingest(READ_REPLY), NewTurn)
    step = instructed.execute()
    assert isinstance(step, Send)
    assert "permission mode is now" not in step.outbound.chunks[0]
    assert "user instructions reminder" not in step.outbound.chunks[0]


def test_arming_is_audited(instructed: Engine) -> None:
    instructed.start_task("t")
    instructed.arm_extra_instructions()
    instructed.arm_extra_instructions()
    assert [e["armed"] for e in transcript(instructed) if e["t"] == "extra_instructions"] == [
        True,
        False,
    ]
