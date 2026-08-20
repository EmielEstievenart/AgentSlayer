"""How a payload's size is SPOKEN to the user: tokens, not characters.

Everything below the Shell counts characters and always will. The paste budget
in a service preset is a character count, the engine enforces a character
budget, the composer slices on characters and the wire carries character
totals - because a character is the only unit both halves of a remote session
can agree on without knowing which model is on the far end of the clipboard.

But nobody thinks in characters. Context windows, pricing and "will this fit"
are all quoted in tokens, so a size the UI states in characters is a number the
reader has to divide in their head before it means anything. These helpers do
that division once, in one place, with the divisor the user configured
(``[general] chars_per_token``, default 3 - docs/design/research-paste-limits.md
finding 2 measured AgentClip's code-like payloads at roughly that, against the
~4 chars/token of English prose).

Three deliberate properties:

* It is an ESTIMATE and says so. Every rendering carries a ``~``. AgentClip
  cannot know the host model's tokenizer (that is the whole point of a
  clipboard harness - the model is on the other side of a browser window), and
  half the numbers reaching these functions are character counts that crossed a
  wire with no text attached. A consistent estimate everywhere beats a display
  that is exact in the two places we happen to hold the text and estimated in
  the rest, because a reader cannot tell which kind of number they are looking
  at.
* One divisor, so ratios stay honest. The status bar's ``out ~2.1k/~4k`` is
  still a true fraction: both halves went through the same arithmetic.
* Where the number is a CONFIGURED value the user may have to edit - a
  preset's budget - both units are shown (:func:`fmt_budget`). The key they
  would change is in characters, so hiding characters there would leave them
  guessing what to type.
"""

from __future__ import annotations


def token_estimate(chars: int, chars_per_token: int) -> int:
    """Characters to an estimated token count, rounded half-up.

    Half-up rather than Python's bankers' rounding so the number moves the way
    a reader expects when they compare two sizes; ``chars_per_token`` is
    clamped to at least 1 because a zero divisor is not worth a crash in a
    status bar (config already validates the range 1-10, so this only guards
    a caller that built a Config by hand).
    """
    divisor = max(1, chars_per_token)
    return (chars + divisor // 2) // divisor


def _compact(value: int) -> str:
    """``950`` / ``4.2k`` / ``12k``: thousands collapsed, a trailing ``.0`` dropped.

    The ``.0`` matters. "12k tokens" is a size; "12.0k tokens" reads like a
    measurement precise to a hundred tokens, which is exactly the false
    precision the ``~`` is there to deny.
    """
    if value < 1000:
        return str(value)
    text = f"{value / 1000:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}k"


def fmt_tokens(chars: int, chars_per_token: int) -> str:
    """``~4.2k tokens`` - the full form, for prose (notices, table cells, rows)."""
    tokens = token_estimate(chars, chars_per_token)
    return f"~{_compact(tokens)} token{'' if tokens == 1 else 's'}"


def fmt_tokens_compact(chars: int, chars_per_token: int) -> str:
    """``~4.2k`` - the same number without its unit, for a status-bar segment
    that names the unit once for a pair of numbers (``out ~2.1k/~4k tok``)."""
    return f"~{_compact(token_estimate(chars, chars_per_token))}"


def fmt_budget(chars: int, chars_per_token: int) -> str:
    """``12,000 chars ≈ ~4k tokens`` - a configured budget in both units.

    Characters first and in full, because that is the number the user would
    type into ``config.toml``; the token estimate second, because that is the
    number that tells them whether the budget is big enough.
    """
    return f"{chars:,} chars ≈ {fmt_tokens(chars, chars_per_token)}"


def fmt_budget_compact(chars: int, chars_per_token: int) -> str:
    """``12k chars ≈ ~4k tokens`` - :func:`fmt_budget` for a line that is already
    long (the exported log's header), where the exact character count is one
    more digit group than the line can carry."""
    return f"{_compact(chars)} chars ≈ {fmt_tokens(chars, chars_per_token)}"
