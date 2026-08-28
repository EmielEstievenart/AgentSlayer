"""§11.9: the engine composes against the MONITOR's service, and re-asks per turn.

The bug these pin is the one a user reported in one sentence: "I changed paste
size in the monitor and it has no effect, not even when connecting a new GUI."
Everything the automation ACTS on already crossed on ``Watched``; everything the
ENGINE acts on - the paste budget, the per-result caps that hang off it, the
fence rule, the extra instructions - was still being read out of the Chat UI's
own ``[services.*]`` table, once, at construction.

So the engine holds no ``ServicePreset`` any more. It holds a
:class:`~agentclip.protocol.preset.LivePreset`, asks it every turn, and falls
back to the local config when nobody is answering - which is the whole of the
seam, tested here from both sides:

* what the live service says governs the payload, not what this host's config
  says;
* a service that CHANGES mid-session governs the next turn, tool caps and all;
* with no source behind it, nothing changes at all and a headless engine
  behaves exactly as it did before the seam existed.

The Chat UI half - where the answer comes off ``Watched`` - is
tests/shell/chat/test_monitor_preset.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import Config, ServicePreset, load_config
from agentclip.engine.engine import AutoReply, Engine, NewTurn, Send
from agentclip.engine.link.factory import EngineRequest, make_engine_builder
from agentclip.protocol.preset import LivePreset

EngineFactory = Callable[..., Engine]

CHAT_NAME = "amber-falcon"

BIG_FILE = "big.txt"
# 800 lines of 40 chars: far more than any preset's per-result cap, so how much
# of it comes back is decided entirely by the budget in force at the time.
BIG_LINES = 800

READ_BIG = f"""===CLIP:CALL id=1 tool=read_file===
path: {BIG_FILE}
===CLIP:END===
===CLIP:EOM calls=1 chat={CHAT_NAME}===
"""

# The same 800 lines, but SHORT ones: nothing about this file is near a
# character cap, so how much of it comes back is decided by the span cap alone -
# which is the half of the budget that reaches the tools through ToolContext.
TALL_FILE = "tall.txt"

READ_TALL = f"""===CLIP:CALL id=1 tool=read_file===
path: {TALL_FILE}
===CLIP:END===
===CLIP:EOM calls=1 chat={CHAT_NAME}===
"""


def _fenced(reply: str) -> str:
    return f"~~~~\n{reply}~~~~\n"


@pytest.fixture
def big_project(project: Path) -> Path:
    body = "".join(f"line {i:04d} " + "x" * 26 + "\n" for i in range(BIG_LINES))
    (project / BIG_FILE).write_text(body, encoding="utf-8")
    tall = "".join(f"{i:04d}\n" for i in range(BIG_LINES))
    (project / TALL_FILE).write_text(tall, encoding="utf-8")
    return project


@pytest.fixture
def config(big_project: Path) -> Config:
    return load_config(big_project, global_config_path=big_project / "no-such-global.toml")


def watched_preset(**fields: object) -> ServicePreset:
    """A preset shaped like what ``preset_from_watched`` builds on the Chat UI
    side: a service key this host has never heard of, with the monitor's own
    numbers on it."""
    base = ServicePreset("monitored", "Whatever the monitor is driving", 16_000, 400_000)
    return replace(base, **fields)  # type: ignore[arg-type]


def moving_preset(
    fallback: ServicePreset, first: ServicePreset
) -> tuple[LivePreset, list[ServicePreset]]:
    """A live preset whose answer a test can move, the way a Monitor UI save does."""
    cell = [first]
    return LivePreset(fallback, lambda: cell[0]), cell


# -- the live service is the one that governs ---------------------------------


def test_the_live_budget_is_the_budget_the_payload_is_fitted_to(
    config: Config, make_engine: EngineFactory
) -> None:
    """The report, at its sharpest: this host's table says 12,000 and the
    monitor says 16,000, and the payload that goes out is a 16,000-char one -
    for a service key this machine's config has never heard of."""
    presets = LivePreset(config.preset(), lambda: watched_preset(max_paste_chars=16_000))
    engine = make_engine(config=config, presets=presets)

    assert config.preset().max_paste_chars != 16_000
    assert engine.status().budget_chars == 16_000
    assert engine.status().service_key == "monitored"

    engine.start_task("t")
    assert isinstance(engine.ingest(READ_BIG), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    # The file alone is 32,000 chars, so this payload is one the fitting really
    # had to cut - to the monitor's number, not to this host's.
    assert len(step.outbound.chunks[0]) <= 16_000


def test_a_budget_the_monitor_raises_mid_session_reaches_the_next_turn(
    config: Config, make_engine: EngineFactory
) -> None:
    """The half that "not even when connecting a new GUI" was about: no restart,
    no new session - the very next turn is composed against the new number.

    And the caps travel with it, which is the part that could have been missed:
    the tool context is built once per session and handed to every handler, so
    its budget-shaped fields - the resolved ``[limits]`` and the ``BudgetCaps``
    - are re-derived at the top of every plan. Here that shows up as
    ``read_file`` being allowed further down the same file than it was a turn
    ago.
    """
    presets, cell = moving_preset(config.preset(), watched_preset(max_paste_chars=16_000))
    engine = make_engine(config=config, presets=presets)
    engine.start_task("t")

    assert isinstance(engine.ingest(READ_TALL), NewTurn)
    first = engine.execute()
    assert isinstance(first, Send)
    small = first.outbound.chunks[0]
    assert f"lines 1-600 of {BIG_LINES}" in small  # a 16k budget spans 600 lines

    cell[0] = watched_preset(max_paste_chars=40_000)
    assert engine.status().budget_chars == 40_000

    assert isinstance(engine.ingest(READ_TALL), NewTurn)
    second = engine.execute()
    assert isinstance(second, Send)
    big = second.outbound.chunks[0]
    # The same read, further down the file: a 40k budget spans 1,500 lines, so
    # the whole of it comes back. Nothing was rebuilt - same engine, same
    # session, same context object, next turn.
    assert f"lines 1-{BIG_LINES} of {BIG_LINES}" in big
    assert len(big) > len(small)


def test_the_fence_rule_is_the_live_service_s(
    config: Config, make_engine: EngineFactory
) -> None:
    """``require_fenced_reply`` turned on in the Monitor UI mid-session refuses
    the very next unfenced reply - it does not wait for a new session."""
    presets, cell = moving_preset(config.preset(), watched_preset())
    engine = make_engine(config=config, presets=presets)
    engine.start_task("t")

    assert isinstance(engine.ingest(READ_BIG), NewTurn)
    engine.execute()

    cell[0] = watched_preset(require_fenced_reply=True)
    bounced = engine.ingest(READ_BIG)
    assert isinstance(bounced, AutoReply)
    assert "fence" in bounced.outbound.chunks[0].lower()
    # ...and a fenced arrival still gets through on the same, unchanged service.
    assert isinstance(engine.ingest(_fenced(READ_BIG)), NewTurn)


def test_extra_instructions_come_from_the_live_service(
    config: Config, make_engine: EngineFactory
) -> None:
    """Both halves of the feature read the same live answer: whether there is
    anything to re-inject, and what the reminder then says."""
    presets, cell = moving_preset(config.preset(), watched_preset())
    engine = make_engine(config=config, presets=presets)
    engine.start_task("t")

    assert engine.status().has_extra_instructions is False
    assert engine.arm_extra_instructions() == "no-instructions"

    text = "always put a space between ] and ( in code you send"
    cell[0] = watched_preset(extra_instructions=text)
    assert engine.status().has_extra_instructions is True
    assert engine.arm_extra_instructions() == "armed"
    assert text in engine.follow_up("carry on").chunks[0]


# -- and with nobody answering, nothing changed -------------------------------


def test_with_no_source_the_local_preset_governs(
    config: Config, make_engine: EngineFactory
) -> None:
    """The monitor-less fallback: a CLI or headless engine, a remote engine on a
    target, a window that never attached a monitor. Every one of them builds a
    ``LivePreset`` over this machine's config and sees exactly what it saw
    before the seam existed."""
    engine = make_engine(config=config)
    assert engine.status().budget_chars == config.preset().max_paste_chars
    assert engine.status().service_key == config.general.service

    engine.start_task("t")
    assert isinstance(engine.ingest(READ_BIG), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    assert len(step.outbound.chunks[0]) <= config.preset().max_paste_chars


def test_a_source_that_answers_nothing_falls_back_too(
    config: Config, make_engine: EngineFactory
) -> None:
    """The source exists but the monitor has not been pointed at anything yet
    (``EMPTY_WATCHED``, an idle start). None means "ask the config", never a
    zero-char budget nothing could be composed against."""
    engine = make_engine(config=config, presets=LivePreset(config.preset(), lambda: None))
    assert engine.status().budget_chars == config.preset().max_paste_chars


# -- the factory's read, which is a session-start one -------------------------


def _built(project: Path, source: object) -> Engine:
    """One engine, built the way ``cli.make_engine_factory`` builds them - with
    the window's "what is the monitor driving?" wired into the builder."""
    builder = make_engine_builder(
        lambda: load_config(project, global_config_path=project / "no-such-global.toml"),
        project,
        CHAT_NAME,
        get_preset=source,  # type: ignore[arg-type]
        home=project,
    )
    return builder(EngineRequest(service="claude"))


def test_the_catalog_a_session_is_built_with_is_the_monitor_s(big_project: Path) -> None:
    """``edit_by_lines`` is the one preset field that decides a CATALOG rather
    than a payload, so it is read where the catalog is built - at session start,
    from the same live answer. A flip reaches the NEXT session (the bootstrap is
    what taught the model this catalog), and that is the whole of its liveness.
    """
    engine = _built(big_project, lambda: watched_preset(edit_by_lines=True))
    payload = engine.start_task("t").chunks[0]
    assert "replace_lines(" in payload
    assert engine.status().budget_chars == 16_000


def test_without_a_monitor_the_factory_builds_the_local_service(big_project: Path) -> None:
    cfg = load_config(big_project, global_config_path=big_project / "no-such-global.toml")
    engine = _built(big_project, None)
    payload = engine.start_task("t").chunks[0]
    assert "replace_lines(" not in payload
    assert engine.status().budget_chars == cfg.services["claude"].max_paste_chars
