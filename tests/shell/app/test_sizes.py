"""How a payload's size is spoken to the user (``shell/app/sizes.py``).

The engine counts characters; the UI states tokens. These are the rules of that
one translation, pinned here rather than through a shell, because both shells
and the CLI share it and a rounding change would otherwise only surface as a
puzzling status-bar number.

The last test is the one that keeps the helper honest: the controller's own
notices have to speak the new unit, or the translation is a function nobody
calls.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentclip.config import Config
from agentclip.shell.app.controller import SessionController
from agentclip.shell.app.sizes import (
    fmt_budget,
    fmt_budget_compact,
    fmt_tokens,
    fmt_tokens_compact,
    token_estimate,
)

from .conftest import FakeChatView, make_factory, start_session


@pytest.mark.parametrize(
    ("chars", "divisor", "expected"),
    [
        (0, 3, 0),
        (1, 3, 0),  # half-up: 0.33 rounds down
        (2, 3, 1),  # ...0.67 rounds up
        (3, 3, 1),
        (36_000, 3, 12_000),
        (12_000, 4, 3_000),
        (10, 0, 10),  # a divisor config could never produce, guarded anyway
    ],
)
def test_the_estimate_divides_and_rounds_half_up(chars: int, divisor: int, expected: int) -> None:
    assert token_estimate(chars, divisor) == expected


@pytest.mark.parametrize(
    ("chars", "expected"),
    [
        (0, "~0 tokens"),
        (2, "~1 token"),  # singular, because "~1 tokens" is a typo the eye catches
        (2_850, "~950 tokens"),
        (12_600, "~4.2k tokens"),
        (36_000, "~12k tokens"),  # no trailing ".0": the ~ already denies precision
        (30_000, "~10k tokens"),  # and the strip must not eat a real zero digit
        (2_997, "~999 tokens"),  # just under the k threshold
        (3_000, "~1k tokens"),
    ],
)
def test_a_size_is_always_an_estimate_and_says_so(chars: int, expected: str) -> None:
    """Every rendering carries the ``~``. AgentClip cannot know the host model's
    tokenizer - the model is on the far side of a browser window - so a number
    printed without the tilde would be claiming something it cannot know."""
    assert fmt_tokens(chars, 3) == expected


def test_the_compact_form_drops_only_the_unit() -> None:
    """The status bar names ``tok`` once for a pair of numbers, so its two halves
    print bare - but they are the same estimate, tilde included."""
    assert fmt_tokens_compact(6_300, 3) == "~2.1k"
    assert fmt_tokens_compact(12_000, 3) == "~4k"
    assert fmt_tokens_compact(150, 3) == "~50"


def test_a_pair_of_status_numbers_stays_a_true_fraction() -> None:
    """Both halves of ``out X/Y`` go through the same divisor, so the ratio the
    bar draws is the ratio the characters had."""
    outbound, budget = 6_000, 12_000
    assert token_estimate(outbound, 3) * 2 == token_estimate(budget, 3)


def test_a_configured_budget_is_shown_in_both_units() -> None:
    """Characters first: that is the unit ``max_paste_chars`` is written in, and
    a caption that hid it would leave the user guessing what to type."""
    assert fmt_budget(12_000, 3) == "12,000 chars ≈ ~4k tokens"
    assert fmt_budget_compact(12_000, 3) == "12k chars ≈ ~4k tokens"


def test_the_divisor_is_the_configured_one() -> None:
    """``[general] chars_per_token`` is not decoration: change it and every size
    in the UI moves."""
    assert fmt_tokens(12_000, 3) == "~4k tokens"
    assert fmt_tokens(12_000, 6) == "~2k tokens"


_TOKENS = re.compile(r"~[\d.]+k? tokens")


async def test_the_controller_sizes_its_notices_in_tokens(
    project: Path, app_config: Config, view: FakeChatView
) -> None:
    """The bootstrap notice - the first size a user ever reads - is an estimate
    in tokens, in the transcript and in the toast alike, and the character count
    it used to print is gone rather than doubled up: a transient notice has room
    for one number, and the useful one is the one the model measures in."""
    controller = SessionController(app_config, make_factory(project), project, view=view)
    view.controller = controller

    await start_session(controller, view)

    note = next(text for text in view.notes() if text.startswith("→ bootstrap copied"))
    assert _TOKENS.search(note)
    assert "chars" not in note
    toast = next(text for text in view.toasts() if text.startswith("bootstrap copied"))
    assert _TOKENS.search(toast)
    assert "chars" not in toast
