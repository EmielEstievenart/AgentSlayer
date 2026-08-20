"""Outbound payload composer: bootstrap / task / results / note rendering.

Stdlib-only protocol leaf (imports config + protocol.types + protocol.spec).
Every outbound payload ends with ===CLIP:EOM turn=N chat=NAME===: turn=N is
AgentClip's internal ordering stamp, chat=NAME is the session's agreed chat
name, which the model echoes back on every reply (the engine's ingest gate
drops replies that do not carry it).

M1 chunking policy: single chunk only. A RESULTS payload that exceeds the
paste budget is fitted by middle-truncating the largest result bodies; a
bootstrap/task/note that cannot fit raises BudgetExceeded.
# M3: replace with PART/ACK chunked send

Outbound payloads ride inside a ~~~~ fence
------------------------------------------
`results`, `task` and `note` payloads are rendered already wrapped in a tilde
fence (`wrap_in_fence`); `bootstrap` deliberately is not (protocol.md section 4,
"outbound payloads are fenced too"). The reason is symmetric with tolerance
#14/#15 on the way in: some hosts treat the INPUT box as rich text as well, and
a payload pasted in as plain prose is rewritten before the model ever reads it -
blank lines came back as literal `<br>` on the host that prompted this. A fence
tells the box "this is code", and the model reads the raw text either way.

The fence lives in the payload STRING, at render time, which is what makes the
rest of the system agree with itself for free:

- `outbound/turn-NNNN.txt` on disk shows exactly what was delivered, not a
  pre-fence draft that no chat ever saw;
- re-copy (the double-tap-c path) re-sends the same string, so a redelivery is
  fenced by construction rather than by remembering to wrap it again;
- streamed delivery (`driver.clip.chunking.split_for_stream`) splits the ALREADY
  fenced string, so the fence wraps the one chat message rather than each
  burst inside it;
- self-write suppression is unaffected: `parser.normalized_hash` strips fence
  lines before hashing, so a fenced payload and its unfenced body hash the same
  and re-ingesting our own text is still `Noise("own-outbound")`;
- the fence characters count against `max_paste_chars`, because they are part
  of the message the host has to accept - hence the wrapping happens INSIDE
  `_render_results` (so the fit loop measures the real thing) and before
  `_single`'s budget check.

There is no knob. On a host that does not process its input box the fence is
two inert lines, and per section 0.6 every fenced payload re-teaches the
fence-your-reply rule by example; a setting would be a config surface with no
failure mode behind it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from agentclip.config import BudgetCaps, ServicePreset
from agentclip.protocol.spec import SECTION_TASK_HEADER, render_spec
from agentclip.protocol.types import Outbound, ToolResult

# In-band marker substituted for the cut middle of an over-budget result body.
TRUNCATION_MARKER = "[truncated by AgentClip to fit the paste budget - request specific ranges]"

# Bounded refinement passes when fitting RESULTS payloads to the budget.
_FIT_ATTEMPTS = 10

# How far over ``max_paste_chars`` the BOOTSTRAP alone may go, as a fraction.
#
# `max_paste_chars` is not one number. It is a per-turn budget on the paste the
# USER has to make repeatedly, chosen low enough that a long session stays
# comfortable, and it is also the hard ceiling of what the host's message box
# will swallow - and those two are not the same figure. The 12,000 on the
# smallest presets is the first kind: a comfort setting. The bootstrap is the
# ONE payload sent exactly once per session, and it is the one with no chunked
# fallback, so applying a comfort budget to it means an oversized brief becomes
# "the session never arms" rather than "the first paste is long". This slack
# buys the brief room for optional sections (a service with edit_by_lines on
# carries roughly 900 characters of ranged-edit doc) without moving the budget
# that governs every OTHER payload - results are still fitted to
# `max_paste_chars` exactly, because those are the pastes that repeat.
#
# Small on purpose: 10% of a comfort setting is still well inside any real
# message box, and it is not a licence to grow section 5 (protocol.md section
# 2's headroom discipline still stands - measure before and after).
BOOTSTRAP_BUDGET_SLACK = 0.10

# The shortest fence we ever emit. Three tildes would be legal (`parser._FENCE_RE`
# accepts `~{3,}`), but four is what the bootstrap teaches the model to use, and
# outbound is the example the model imitates - section 0.6's symmetry is a
# feature, not a coincidence.
_MIN_FENCE_TILDES = 4

# A line that could close our fence from inside: a run of 3+ tildes at the start
# of a line, which is exactly what the fence recogniser matches. Only tildes are
# checked because we only ever fence with tildes - a body line of backticks is
# inert inside a tilde fence, which is why the tilde fence was chosen (section
# 3 of the transport research: file content is full of backticks).
_LEADING_TILDES_RE = re.compile(r"^(~{3,})")


def wrap_in_fence(payload: str) -> str:
    """``payload`` inside a tilde fence long enough that nothing in it can close.

    The collision rule of section 2, now applied to our OWN payloads rather than
    only taught: the outer delimiter must not be reachable from inside, so the
    fence is one tilde longer than the longest leading tilde run in the content
    (minimum :data:`_MIN_FENCE_TILDES`). A payload containing a `~~~~~` line -
    a result body quoting this very file, say - gets a six-tilde fence.

    Keeps the payloads-end-with-a-newline convention: the closing fence is the
    last line and it is newline-terminated.
    """
    longest = max(
        (len(m.group(1)) for m in map(_LEADING_TILDES_RE.match, payload.split("\n")) if m),
        default=0,
    )
    fence = "~" * max(_MIN_FENCE_TILDES, longest + 1)
    body = payload.rstrip("\n")
    return f"{fence}\n{body}\n{fence}\n"


class BudgetExceeded(Exception):
    """A payload cannot fit the preset's max_paste_chars (M1: no chunking)."""

    def __init__(self, needed_chars: int, budget_chars: int) -> None:
        super().__init__(
            f"payload needs {needed_chars} chars but the paste budget is {budget_chars}"
        )
        self.needed_chars = needed_chars
        self.budget_chars = budget_chars


def pick_heredoc_tag(content: str, base: str = "R") -> str:
    """Return a heredoc tag guaranteed not to collide with any line of content.

    A heredoc is terminated by a line equal to the tag after whitespace trim,
    so collision is checked against the stripped lines. Returns ``base``, or
    ``base + "x"``, ``base + "xx"``, ... until non-colliding.
    """
    lines = {line.strip() for line in content.split("\n")}
    tag = base
    while tag in lines:
        tag += "x"
    return tag


def _truncate_middle(body: str, target: int) -> str:
    """Middle-truncate body to roughly ``target`` chars on line boundaries.

    The first and last lines are always kept; the cut middle is replaced with
    TRUNCATION_MARKER. May still exceed ``target`` when even the minimal form
    (first line + marker + last line) is longer - the caller re-checks.
    """
    if len(body) <= target:
        return body
    lines = body.split("\n")
    if len(lines) <= 2:
        return body  # nothing cuttable without touching the first/last line
    head: list[str] = [lines[0]]
    tail: list[str] = [lines[-1]]
    middle = lines[1:-1]
    size = len(lines[0]) + len(TRUNCATION_MARKER) + len(lines[-1]) + 2  # + joins
    front, back = 0, len(middle)
    take_front = True
    while front < back:
        candidate = middle[front] if take_front else middle[back - 1]
        if size + len(candidate) + 1 > target:
            break
        size += len(candidate) + 1
        if take_front:
            head.append(middle[front])
            front += 1
        else:
            tail.insert(0, middle[back - 1])
            back -= 1
        take_front = not take_front
    if front >= back:  # defensive: everything fit after all
        return body
    return "\n".join([*head, TRUNCATION_MARKER, *tail])


def _fit_bodies(bodies: Sequence[str], available: int) -> list[str]:
    """Shrink the largest bodies so their total is <= available (approx).

    Finds the largest per-body cap T with sum(min(len(b), T)) <= available and
    middle-truncates every body longer than T. Only the largest bodies shrink;
    bodies already under the cap are untouched.
    """
    if not bodies:
        return []
    sizes = [len(b) for b in bodies]
    if sum(sizes) <= available:
        return list(bodies)
    lo, hi = 0, max(sizes)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(s, mid) for s in sizes) <= available:
            lo = mid
        else:
            hi = mid - 1
    cap = lo
    return [_truncate_middle(b, cap) if len(b) > cap else b for b in bodies]


class Composer:
    """Renders clipboard-ready outbound payloads for one session."""

    def __init__(
        self,
        preset: ServicePreset,
        caps: BudgetCaps,
        tool_catalog: str,
        workdir_name: str,
        os_name: str,
        chat_name: str,
        role: Literal["master", "subagent"] = "master",
    ) -> None:
        self._preset = preset
        self._caps = caps
        self._tool_catalog = tool_catalog
        self._workdir_name = workdir_name
        self._os_name = os_name
        self._chat_name = chat_name
        self._role = role

    # -- public API ---------------------------------------------------------

    @property
    def chat_name(self) -> str:
        """The chat name stamped on every payload this composer renders."""
        return self._chat_name

    @property
    def role(self) -> Literal["master", "subagent"]:
        """Which bootstrap brief this composer renders (see spec.render_spec)."""
        return self._role

    def bootstrap(self, task: str) -> Outbound:
        """The full protocol spec + tool catalog + initial task. Always turn 1.

        The one outbound that is NOT fenced (see the module docstring for what
        the fence is for). Three reasons, all specific to this payload:

        1. It is the ROLE framing's payload. Section 2 beat 1 exists to defeat
           the turn-1 refusal - some models read a pasted operating brief as
           injected content trying to redefine them and stall - and handing that
           brief over as one big code block is plausibly the exact reading it
           works to prevent. A fence says "this is data"; the bootstrap's whole
           job is to say "this is your brief".
        2. It is the payload with no headroom. Assembled with a saturated skills
           listing it measures ~11,933 chars against the smallest presets'
           12,000-char budget (protocol.md section 2, "Budget headroom"), and it
           is the one payload with no chunked fallback: over budget means
           BudgetExceeded and a session that never arms. Ten fence characters
           fit today; that slack is the only slack there is.
        3. Its corruption is LOUD. A mangled brief misbehaves visibly on the
           very first reply, whereas a rewritten results payload corrupts code
           silently - which is the failure the fence is actually for.
        """
        spec_text = render_spec(
            self._preset,
            self._caps,
            self._tool_catalog,
            self._workdir_name,
            self._os_name,
            self._chat_name,
            role=self._role,
        )
        body = task.rstrip("\n")
        payload = (
            f"{spec_text}\n"
            f"{SECTION_TASK_HEADER}\n"
            "\n"
            "===CLIP:TASK===\n"
            f"{body}\n"
            f"{self._eom(1)}\n"
        )
        return self._single("bootstrap", payload, turn=1)

    def task(self, turn: int, text: str, notes: Sequence[str] = ()) -> Outbound:
        """A follow-up task/message from the user, mid- or post-session.

        ``notes`` renders the same leading ``===CLIP:NOTE===`` block
        :meth:`results` does, inside the same fence: the notes channel is about
        the payload the model is being handed, and a typed follow-up is as much
        an outbound as a results batch. The engine uses it for anything armed
        and waiting for "the next thing we send", which must not mean "the next
        RESULTS we send" - a session steered by follow-ups would never spend it.
        """
        body = text.rstrip("\n")
        lines: list[str] = []
        if notes:
            # Ahead of the TASK block, not inside it: `===CLIP:RESULTS turn=N===`
            # is an envelope line the notes can sit under, but `===CLIP:TASK===`
            # is the block itself, and a NOTE spliced between that header and
            # the user's words would read as part of the task.
            lines += ["===CLIP:NOTE===", *notes, "===CLIP:END==="]
        lines += ["===CLIP:TASK===", body, self._eom(turn)]
        payload = wrap_in_fence("\n".join(lines) + "\n")
        return self._single("user_answer", payload, turn)

    def note(self, turn: int, text: str) -> Outbound:
        """An informational notice to the LLM (e.g. 'the user reverted turn 5')."""
        body = text.rstrip("\n")
        payload = wrap_in_fence(f"===CLIP:NOTE===\n{body}\n{self._eom(turn)}\n")
        return self._single("note", payload, turn)

    def results(
        self,
        turn: int,
        results: Sequence[ToolResult],
        notes: Sequence[str] = (),
    ) -> Outbound:
        """The combined results payload for one executed turn.

        Over budget => fit by truncation: proportionally shrink the largest
        result bodies (sentinel lines are never touched; the first and last
        line of each body are always kept). Raises BudgetExceeded only when
        even maximal truncation cannot fit.
        """
        budget = self._preset.max_paste_chars
        bodies = [self._result_body(r) for r in results]
        payload = self._render_results(turn, results, bodies, notes)
        if len(payload) <= budget:
            return Outbound("results", (payload,), len(payload), turn)

        # M3: replace with PART/ACK chunked send
        overhead = len(payload) - sum(len(b) for b in bodies)
        available = budget - overhead
        for _ in range(_FIT_ATTEMPTS):
            if available < 0:
                break
            fitted = _fit_bodies(bodies, available)
            payload = self._render_results(turn, results, fitted, notes)
            if len(payload) <= budget:
                return Outbound("results", (payload,), len(payload), turn)
            available -= len(payload) - budget
        raise BudgetExceeded(len(payload), budget)

    # -- helpers ------------------------------------------------------------

    def _eom(self, turn: int) -> str:
        """The terminating sentinel. turn= orders payloads for AgentClip's own
        bookkeeping; chat= is the handshake token the model must echo back."""
        return f"===CLIP:EOM turn={turn} chat={self._chat_name}==="

    def _single(
        self,
        kind: Literal["bootstrap", "user_answer", "note"],
        payload: str,
        turn: int,
    ) -> Outbound:
        # The bootstrap, and only the bootstrap, gets BOOTSTRAP_BUDGET_SLACK on
        # top: it is sent once per session and has nothing to truncate, while a
        # `user_answer` or a `note` is one of the pastes the user makes over and
        # over and is held to the budget exactly. The reported budget in the
        # exception stays the preset's, because that is the number the user set
        # and the one they would go and change.
        budget = self._preset.max_paste_chars
        limit = int(budget * (1 + BOOTSTRAP_BUDGET_SLACK)) if kind == "bootstrap" else budget
        if len(payload) > limit:
            raise BudgetExceeded(len(payload), budget)
        return Outbound(kind, (payload,), len(payload), turn)

    @staticmethod
    def _result_body(result: ToolResult) -> str:
        body = result.body
        if result.user_note:
            user_line = f"user: {result.user_note}"
            body = f"{user_line}\n{body}" if body else user_line
        return body

    @staticmethod
    def _result_header(result: ToolResult) -> str:
        header = f"===CLIP:RESULT id={result.call_id} status={result.status}"
        if result.code is not None:
            header += f" code={result.code}"
        return header + "==="

    def _render_results(
        self,
        turn: int,
        results: Sequence[ToolResult],
        bodies: Sequence[str],
        notes: Sequence[str],
    ) -> str:
        lines: list[str] = [f"===CLIP:RESULTS turn={turn}==="]
        if notes:
            lines.append("===CLIP:NOTE===")
            lines.extend(notes)
            lines.append("===CLIP:END===")
        for result, body in zip(results, bodies, strict=True):
            tag = pick_heredoc_tag(body, base=f"R{result.call_id}")
            lines.append(self._result_header(result))
            # Space before the tag: outbound is the example the model imitates,
            # and a glued `<<TAG` is what chat clients read as an HTML start tag
            # (parser._client_mangled_opener).
            lines.append(f"body << {tag}")
            if body:
                lines.append(body)
            lines.append(tag)
            lines.append("===CLIP:END===")
        lines.append(self._eom(turn))
        # Wrapped HERE, not by the caller: `results()` fits the payload to the
        # budget by measuring what it renders, and the fence is part of what the
        # host has to swallow. Fitting an unfenced draft and wrapping afterwards
        # would put every payload that landed within ~10 chars of the budget
        # back over it, silently, at the one moment nothing is left to cut.
        return wrap_in_fence("\n".join(lines) + "\n")
