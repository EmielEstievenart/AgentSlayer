"""What the model was actually SHOWN of a numbered read.

The ranged-edit mode (``ServicePreset.edit_by_lines``) lets a model overwrite
lines 88-90 of a file without ever sending the old text back. That is the whole
point on a host that cannot echo code faithfully, and it is also the whole risk:
`edit_file` is self-verifying - a find-block that does not match refuses - while
`replace_lines` will happily write anything anywhere, because "lines 88-90" is
true of every file that has ninety lines.

So the verification moves up a level, to the only place that knows what the
model can possibly have read: the engine, holding the payload it just sent. A
ranged edit is legal only when the range was inside a `numbered` read in the
IMMEDIATELY PRECEDING results payload, and this module is how that is known -
not guessed.

Derived from the delivered text, not from the intent
----------------------------------------------------
A `read_file` result writes its own header ("lines 40-140 of 900") and is then
handed to two independent truncation passes - `engine.results.fit_results` for
the per-result cap, `composer._fit_bodies` for the paste budget - either of
which can cut the MIDDLE out of that body afterwards. The header survives the
cut and keeps claiming lines the model never saw. Trusting it would leave a hole
exactly the shape of this feature's failure mode.

:func:`surviving_numbered_lines` therefore reads the FINAL payload string - the
literal characters the user copied - and takes only the gutter-prefixed lines
still standing in it. Truncation is not something it needs to know about, model
or otherwise: whatever survived, survived.

The block walk uses the composer's own heredoc framing (``body << TAG`` ... a
line equal to ``TAG``) rather than hunting sentinels, because a result body may
QUOTE a sentinel - reading this repo's protocol docs does exactly that - and the
tag is chosen not to collide with the body it wraps.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from agentclip.executor.tools.fs_tools import GUTTER_SEP

# One result envelope, as protocol/composer.py renders it.
_RESULT_HEADER_RE = re.compile(r"^===CLIP:RESULT id=(\d+) status=")
_BODY_OPENER_RE = re.compile(r"^body << (\S+)$")

# A surviving gutter line. Anchored and derived from GUTTER_SEP, which is what
# makes it unambiguous against a file whose own content looks like a gutter:
# a source line "50| x" served as line 12 arrives as "12| 50| x", and an
# anchored match reads the 12 and hands back "50| x" as the text. The optional
# group is for a served EMPTY line, whose trailing space a copy may drop.
_GUTTER_RE = re.compile(r"^ *(\d+)" + re.escape(GUTTER_SEP.rstrip()) + r"(?: (.*))?$")


@dataclass(slots=True)
class ServedRead:
    """The numbered lines of one file that reached the model last turn.

    ``lines`` is line number -> the exact text served, sparse on purpose: two
    reads of one file, or one read middle-truncated into a head and a tail, are
    the same thing here - a set of lines the model has seen - and a membership
    test over it answers "was the whole requested range shown?" without any
    range arithmetic.

    ``content_hash`` is of the whole file as it stood when the payload went out.
    It is what turns "these line numbers were right" into "these line numbers
    are still right": any edit anywhere in the file can renumber the lines
    below it, so a changed file invalidates every range, not just overlapping
    ones.
    """

    content_hash: str
    lines: dict[int, str] = field(default_factory=dict)


def content_hash(text: str) -> str:
    """Stable identity of a file's LF-normalised text."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def surviving_numbered_lines(payload: str, call_ids: Iterable[int]) -> dict[int, dict[int, str]]:
    """The gutter lines still present in ``payload``, per result id.

    ``call_ids`` names the reads that ASKED for a gutter; every other block is
    walked past. Ids with nothing left after truncation are simply absent.
    """
    wanted = set(call_ids)
    if not wanted:
        return {}
    found: dict[int, dict[int, str]] = {}
    lines = payload.split("\n")
    i = 0
    while i < len(lines):
        header = _RESULT_HEADER_RE.match(lines[i])
        opener = _BODY_OPENER_RE.match(lines[i + 1]) if header and i + 1 < len(lines) else None
        if header is None or opener is None:
            i += 1
            continue
        call_id, tag = int(header.group(1)), opener.group(1)
        body: dict[int, str] = {}
        j = i + 2
        while j < len(lines) and lines[j].strip() != tag:
            if call_id in wanted and (m := _GUTTER_RE.match(lines[j])) is not None:
                body[int(m.group(1))] = m.group(2) or ""
            j += 1
        if body:
            found.setdefault(call_id, {}).update(body)
        i = j + 1
    return found


def describe_ranges(numbers: Iterable[int]) -> str:
    """Sorted line numbers condensed to "12-80, 120-200" - the refusal's evidence.

    A refusal that only says "not in what you read" invites a blind retry; the
    ranges say which read to widen, or that there was none worth widening.
    """
    runs: list[list[int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in runs) or "none"
