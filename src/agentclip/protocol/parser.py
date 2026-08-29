"""Tolerant parser for CLIP/1 replies (LLM -> tool direction).

Pure functions over strings: no I/O, no clipboard, stdlib-only besides
`agentclip.protocol.types`. Implements every tolerance in the protocol design
(docs/design/protocol.md section 1.4) and the truncation triggers of section
5.2. The parser never validates tool names or required params -- that is the
engine's job; it only recovers structure and reports anomalies as ParseIssues
(per call when localizable, reply-level otherwise).
"""

from __future__ import annotations

import hashlib
import re

from agentclip.protocol.names import normalize_chat_name
from agentclip.protocol.types import (
    PROTOCOL_MARKER,
    EomInfo,
    ParsedReply,
    ParseIssue,
    SayBlock,
    ToolCall,
)

# A sentinel line, matched against the whitespace-trimmed line. Keyword is
# case-insensitive; trailing `===` is decorative. NOTE: PART-END must precede
# PART in the alternation or `\b` would happily split "PART-END" after "PART".
_SENTINEL_RE = re.compile(
    r"^={3,}\s*CLIP:(CALL|SAY|END|EOM|RESULTS|RESULT|PART-END|PART|ACK|NACK|TASK|NOTE)\b(.*?)=*$",
    re.IGNORECASE,
)

# Code-fence line (tolerance #1): ``` or ~~~, any length >= 3, optional simple
# language tag. Only consulted OUTSIDE heredocs.
_FENCE_RE = re.compile(r"^(?:`{3,}|~{3,})\s*[\w+.\-]*\s*$")

# Heredoc opener: `key << TAG` (2+ '<' accepted, space optional, a stray colon
# after the key tolerated). Tag = 1-32 chars of [A-Za-z0-9_-].
_HEREDOC_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:?\s*<{2,}\s*([A-Za-z0-9_-]{1,32})\s*$")

# Same shape but with trailing content on the opener line: the fingerprint of a
# chat client that read `<TAG` as an HTML start tag. See _client_mangled_opener.
_MANGLED_HEREDOC_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_-]*)\s*:?\s*<{2,}\s*([A-Za-z0-9_-]{1,32})\s+\S"
)
_MANGLE_MARKERS = ("CLIP:END", "CLIP:EOM")

# The half of PROTOCOL_MARKER that survives being glued onto another line (the
# leading `===` may have been eaten by the terminator run in front of it).
_GLUED_MARKER = "CLIP:"

# The closing `===` of a sentinel line, when the line did NOT end there. The
# regex above lets the attribute section run to the end of the line, so on a
# line that had text glued after its terminator (see _split_sentinel_section)
# every one of those words would otherwise be read as an attribute -- and the
# terminator itself would end up inside the last VALUE (`chat=amber-falcon===`).
_ATTR_TERMINATOR_RE = re.compile(r"={3,}")

# Inline param: `key: value` or `key=value` (LLMs drift between separators).
_PARAM_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*[:=]\s*(.*)$")

# Positional `k/n` token on ACK/NACK/PART lines.
_POSITIONAL_KN_RE = re.compile(r"^(\d+)/(\d+)$")

# Unicode space-ish characters smart-substituted by chat UIs, normalized to a
# plain space -- but only on lines that thereby become valid sentinel lines.
_SPACE_CODEPOINTS = (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)
_ZERO_WIDTH_CODEPOINTS = (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
_SENTINEL_TRANS: dict[int, str | None] = {cp: " " for cp in _SPACE_CODEPOINTS}
_SENTINEL_TRANS.update(dict.fromkeys(_ZERO_WIDTH_CODEPOINTS))


def looks_like_protocol(text: str) -> bool:
    """Cheap watcher pre-filter: literal substring test, nothing else."""
    return PROTOCOL_MARKER in text


def normalize(text: str) -> str:
    """Strip a leading BOM, normalize CRLF/CR to LF, and normalize NBSP/smart
    spaces to plain spaces on sentinel lines ONLY (heredoc content elsewhere
    stays byte-faithful)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        fixed = line.translate(_SENTINEL_TRANS)
        if fixed != line and _SENTINEL_RE.match(fixed.strip()):
            out.append(fixed)
        else:
            out.append(line)
    return "\n".join(out)


def normalized_hash(text: str) -> str:
    """Self-write suppression key (protocol design section 6.1): blake2b-128
    hex over the normalized text with fence lines stripped and per-line trailing
    whitespace stripped. Stable across fenced/unfenced, CRLF/LF, and BOM
    variants."""
    lines = [
        line.rstrip()
        for line in normalize(text).split("\n")
        if not _FENCE_RE.match(line.strip())
    ]
    return hashlib.blake2b("\n".join(lines).encode("utf-8"), digest_size=16).hexdigest()


# Sentinel keywords the model stamps the chat name on (see spec section 2/3).
_CHAT_STAMPED_KEYWORDS = frozenset({"EOM", "ACK", "NACK"})


def peek_chat_name(text: str) -> str | None:
    """The chat name a pasted reply claims to come from, or None.

    A cheap reverse scan for the LAST chat-stamped sentinel line (EOM, ACK or
    NACK) - the host uses it to route a paste to the right session while a
    sub-agent run is in flight, BEFORE any engine is asked to parse it. Last
    wins because that line is the one the model actually ended its reply with;
    an earlier one is quoted text or an echo of an older message.

    Returns None when no such line carries a `chat=` attribute at all; callers
    treat that as "unknown, route to the active session" and let the engine's
    own chat gate be the backstop. Being a scan rather than a parse, this is
    deliberately approximate: it does not know about heredocs, so a `chat=`
    line quoted inside file content could win. That costs a misroute warning
    at worst, never a wrong execution.
    """
    for line in reversed(normalize(text).split("\n")):
        match = _SENTINEL_RE.match(line.strip())
        if match is None or match.group(1).upper() not in _CHAT_STAMPED_KEYWORDS:
            continue
        attrs, _ = _parse_attrs(_split_sentinel_section(match.group(2))[0])
        return normalize_chat_name(attrs.get("chat"))
    return None


def _client_mangled_opener(line: str) -> tuple[str, str] | None:
    """`(key, tag)` when `line` bears the fingerprint of a chat client that
    mistook a heredoc opener for an HTML start tag, else None.

    Such a client sees `<TAG` inside a glued `key <<TAG` opener, treats the rest
    of the reply as that tag's attributes - collapsing it onto one line, sorting
    it, quoting it - and re-emits the lot as a single element. The wreckage is
    unrecoverable: the words come back in ASCII sort order, so there is nothing
    to salvage and nothing for the model to fix by resending.

    Detection needs both halves, which is why false positives are ~nil: an
    opener with trailing content AND a CLIP sentinel in that trailing text. A
    well-formed reply never puts ===CLIP:END=== or the EOM line behind a heredoc
    tag on the same line, and prose that happens to look like an opener does not
    carry sentinels.
    """
    match = _MANGLED_HEREDOC_RE.match(line)
    if match is None:
        return None
    upper = line.upper()
    if not any(marker in upper for marker in _MANGLE_MARKERS):
        return None
    return match.group(1), match.group(2)


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _split_sentinel_section(section: str) -> tuple[str, str]:
    """Split a sentinel line's tail into `(attributes, text glued on after it)`.

    A sentinel line ends at its `===` terminator, so a run of 3+ `=` INSIDE the
    matched tail means the line kept going after the marker closed: the text
    behind it belongs to a line that no longer exists (tolerance #14). The
    attributes before it are still perfectly good and are parsed as usual - the
    chat name in particular, which the engine gates on, must not inherit the
    terminator.
    """
    match = _ATTR_TERMINATOR_RE.search(section)
    if match is None:
        return section, ""
    return section[: match.start()], section[match.end() :]


def _parse_attrs(section: str) -> tuple[dict[str, str], list[str]]:
    """Space-separated `key=value` attrs (order-free, keys lowercased) plus
    bare positional tokens (the `k/n` of ACK/NACK/PART)."""
    attrs: dict[str, str] = {}
    positional: list[str] = []
    for token in section.split():
        if "=" in token:
            key, _, value = token.partition("=")
            if key:
                attrs[key.lower()] = value
        else:
            positional.append(token)
    return attrs, positional


class _Parser:
    """Single-pass line parser; one instance per parse_reply call."""

    def __init__(self, normalized: str) -> None:
        self.lines = normalized.split("\n")
        self.n = len(self.lines)
        self.calls: list[ToolCall] = []
        self.says: list[SayBlock] = []
        self.warnings: list[ParseIssue] = []
        self.prose: list[str] = []
        self._cur_prose: list[str] = []
        self.saw_sentinel = False
        # A fence line consumed at a STRUCTURAL position (see ParsedReply.
        # saw_fence). Set in exactly the two places the parser skips one: the
        # top-level scan and the CALL-body scan. Heredoc content never passes
        # through either, which is the whole point - a reply writing a markdown
        # file must not look fenced because the file it writes contains ```.
        self.saw_fence = False
        self.truncated_eof = False
        self.eom_present = False
        self.eom_calls: int | None = None
        self.eom_turn: int | None = None
        self.eom_chat: str | None = None
        self.eom_line = 0
        self.ack_kind: str | None = None  # "ack" | "nack"
        self.ack_part: int | None = None
        self.ack_total: int | None = None
        self.ack_chat: str | None = None
        self.nack_reason: str | None = None

    # -- top level ---------------------------------------------------------

    def run(self) -> None:
        i = 0
        while i < self.n:
            stripped = self.lines[i].strip()
            match = _SENTINEL_RE.match(stripped)
            if match is None:
                if _FENCE_RE.match(stripped):  # tolerance #1: fences ignored
                    self.saw_fence = True
                    i += 1
                    continue
                self._cur_prose.append(self.lines[i])
                i += 1
                continue
            self.saw_sentinel = True
            keyword = match.group(1).upper()
            section, tail = _split_sentinel_section(match.group(2))
            attrs, positional = _parse_attrs(section)
            self._check_flattened(i, keyword, tail)
            if keyword == "CALL":
                self._flush_prose()
                i = self._parse_call(i, attrs)
            elif keyword == "SAY":
                self._flush_prose()
                i = self._parse_say(i)
            elif keyword == "EOM":
                self._flush_prose()
                self._handle_eom(i, attrs)
                i += 1
            elif keyword in ("ACK", "NACK"):
                self._flush_prose()
                self._handle_ack_nack(keyword, attrs, positional)
                i += 1
            elif keyword == "END":
                # Stray END outside any block: structural junk, drop it.
                self._flush_prose()
                i += 1
            else:
                # RESULTS / RESULT / PART / PART-END / TASK / NOTE arriving
                # INBOUND means somebody mis-copied one of our own payloads.
                # Keep the line as prose; never derive calls from it.
                self._cur_prose.append(self.lines[i])
                i += 1
        self._flush_prose()

    # -- sentinel handlers ---------------------------------------------------

    def _check_flattened(self, i: int, keyword: str, tail: str) -> None:
        """Flag a sentinel line that another sentinel was glued onto (#14).

        Two signals together, like tolerance #13: text after the marker's `===`
        terminator AND `CLIP:` inside that text. A reply rendered outside a
        ~~~~ fence comes back through the clipboard with its newlines collapsed
        to spaces, so whole CALL blocks end up riding on the previous line and
        are never seen. Nothing here is recovered: the words survive but the
        line structure they were parsed by does not, and guessing where the
        breaks went would mean executing a command AgentClip reassembled.
        """
        if _GLUED_MARKER not in tail.upper():
            return
        self.warnings.append(
            ParseIssue(
                "flattened_reply",
                i + 1,
                f"the ===CLIP:{keyword}=== line has more CLIP text glued onto it - this "
                "reply lost its line breaks in transport, so any block behind that "
                "marker was never parsed",
            )
        )

    def _handle_eom(self, i: int, attrs: dict[str, str]) -> None:
        if self.eom_present:
            return  # first EOM wins; later ones are echo junk
        self.eom_present = True
        self.eom_line = i + 1
        self.eom_calls = _to_int(attrs.get("calls"))
        # turn= is legacy-tolerated (AgentClip still stamps it outbound for
        # ordering); chat= is what the engine actually gates on.
        self.eom_turn = _to_int(attrs.get("turn"))
        self.eom_chat = normalize_chat_name(attrs.get("chat"))

    def _handle_ack_nack(
        self, keyword: str, attrs: dict[str, str], positional: list[str]
    ) -> None:
        if self.ack_kind is not None:
            return
        self.ack_kind = "ack" if keyword == "ACK" else "nack"
        part: int | None = None
        total: int | None = None
        for token in positional:  # canonical `k/n` form
            kn = _POSITIONAL_KN_RE.match(token)
            if kn:
                part, total = int(kn.group(1)), int(kn.group(2))
                break
        if part is None:  # tolerated `part=k total=n` form
            part = _to_int(attrs.get("part"))
            total = _to_int(attrs.get("total"))
        self.ack_part, self.ack_total = part, total
        self.ack_chat = normalize_chat_name(attrs.get("chat"))
        if keyword == "NACK":
            self.nack_reason = attrs.get("reason")

    # -- CALL blocks ---------------------------------------------------------

    def _parse_call(self, start: int, attrs: dict[str, str]) -> int:
        """Parse one CALL block starting at line index `start` (its header).
        Returns the line index parsing should resume from."""
        header_line = start + 1  # 1-based for ParseIssue
        issues: list[ParseIssue] = []
        canonical = len(self.calls) + 1

        original_id: str | None = None
        raw_id = attrs.get("id")
        if raw_id is None:
            self.warnings.append(
                ParseIssue("renumbered", header_line, f"call without id; assigned id={canonical}")
            )
        elif _to_int(raw_id) != canonical:
            original_id = raw_id
            self.warnings.append(
                ParseIssue("renumbered", header_line, f"call id={raw_id} renumbered to id={canonical}")
            )

        tool = attrs.get("tool", "").strip()
        if not tool:
            issues.append(ParseIssue("bad_header", header_line, "CALL header is missing tool="))

        params: dict[str, str] = {}
        j = start + 1
        stop = self.n  # exclusive end of the raw block slice
        closed = False
        while j < self.n:
            stripped = self.lines[j].strip()
            sentinel = _SENTINEL_RE.match(stripped)
            if sentinel:
                kw = sentinel.group(1).upper()
                if kw == "END":
                    stop = j + 1
                    j += 1
                    closed = True
                    break
                if kw in ("CALL", "SAY", "EOM"):
                    # Tolerance #7: missing END auto-closes the block.
                    self.warnings.append(
                        ParseIssue(
                            "missing_end",
                            header_line,
                            f"===CLIP:END=== missing for call id={canonical}; "
                            f"auto-closed at line {j + 1}",
                        )
                    )
                    stop = j
                    closed = True
                    break
                j += 1  # other sentinel inside a call body: ignore as junk
                continue
            heredoc = _HEREDOC_RE.match(stripped)
            if heredoc:
                key, tag = heredoc.group(1), heredoc.group(2)
                term = self._find_heredoc_end(j + 1, tag)
                if term is not None:
                    # Content is byte-faithful: only CRLF->LF was applied.
                    params[key] = "\n".join(self.lines[j + 1 : term])
                    j = term + 1
                    continue
                swallowed = self._find_swallowed_call(j + 1)
                if swallowed is not None:
                    # Tolerance #9: the heredoc swallowed a later CALL header.
                    # Fail THIS call and re-parse from the swallowed header.
                    issue = ParseIssue(
                        "unterminated_heredoc",
                        j + 1,
                        f"heredoc '{key}' (tag {tag}) never terminated; re-parsing "
                        f"from CALL header swallowed at line {swallowed + 1}",
                    )
                    issues.append(issue)
                    self.warnings.append(issue)
                    stop = swallowed
                    j = swallowed
                    closed = True  # resume from the recovered header
                    break
                # Tolerance #8: open heredoc at EOF -> truncated-reply path.
                issue = ParseIssue(
                    "unterminated_heredoc",
                    j + 1,
                    f"heredoc '{key}' (tag {tag}) still open at end of input",
                )
                issues.append(issue)
                self.warnings.append(issue)
                self.truncated_eof = True
                stop = self.n
                j = self.n
                closed = True
                break
            mangled = _client_mangled_opener(stripped)
            if mangled is not None:
                # Transport corruption, not a model mistake: the reply was
                # flattened before it ever reached the clipboard. Fail the call
                # (the engine tells the USER, not the model - resending cannot
                # help) and keep scanning; nothing here is recoverable.
                key, tag = mangled
                issue = ParseIssue(
                    "client_mangled_heredoc",
                    j + 1,
                    f"heredoc opener '{key} << {tag}' was flattened onto one line that "
                    "also swallowed CLIP sentinel text; the chat client mangled this "
                    "reply in transport (it read the tag as an HTML start tag)",
                )
                issues.append(issue)
                self.warnings.append(issue)
                j += 1
                continue
            param = _PARAM_RE.match(stripped)
            if param:
                # Unknown keys are kept verbatim; the engine validates them.
                params[param.group(1)] = param.group(2).strip()
                j += 1
                continue
            # Fences, blanks, and soft-wrap debris inside a block: skipped
            # (still present in `raw` for the transcript). The fence is checked
            # by name rather than left in the debris bucket because a fence HERE
            # is evidence about the whole reply - a model that fences each call
            # separately still proves the transport carried fence lines through
            # - and that evidence is what tolerance #15 turns on.
            if _FENCE_RE.match(stripped):
                self.saw_fence = True
            j += 1
        if not closed:
            # EOF inside the block with no heredoc open: truncated mid-block.
            issues.append(
                ParseIssue(
                    "missing_end",
                    header_line,
                    "input ended inside this CALL block (no ===CLIP:END===)",
                )
            )
            self.truncated_eof = True
            stop = self.n
            j = self.n

        self.calls.append(
            ToolCall(
                id=canonical,
                tool=tool,
                params=params,
                raw="\n".join(self.lines[start:stop]),
                original_id=original_id,
                issues=tuple(issues),
            )
        )
        return j

    # -- SAY blocks ----------------------------------------------------------

    def _parse_say(self, start: int) -> int:
        """Parse one SAY block starting at its header line index `start`.

        The body is markdown addressed to the user and nothing in it is
        grammar, so it is taken verbatim: fences inside it are CONTENT (a SAY
        explaining a fix is full of ``` blocks) and only a sentinel line ends
        it. ===CLIP:END=== is the terminator; any other sentinel auto-closes
        the block where it stands (tolerance #16) and is re-read by the caller,
        so a model that forgot the END still gets its message through and the
        calls behind it still run.
        """
        j = start + 1
        stop = self.n
        closed = False
        while j < self.n:
            match = _SENTINEL_RE.match(self.lines[j].strip())
            if match is None:
                j += 1
                continue
            stop = j
            if match.group(1).upper() == "END":
                j += 1
                closed = True
            else:
                self.warnings.append(
                    ParseIssue(
                        "missing_end",
                        start + 1,
                        f"===CLIP:END=== missing for the SAY block; auto-closed at line {j + 1}",
                    )
                )
            break
        if j >= self.n and not closed:
            # EOF with the block still open. Not a truncation flag of its own:
            # a reply that stopped mid-sentence has no EOM either, and that is
            # what section 5.2 already gates on.
            self.warnings.append(
                ParseIssue(
                    "missing_end",
                    start + 1,
                    "===CLIP:END=== missing for the SAY block; auto-closed at end of input",
                )
            )
        text = "\n".join(self.lines[start + 1 : stop]).strip("\n").rstrip()
        if text.strip():
            self.says.append(SayBlock(text=text, after_calls=len(self.calls)))
        return j

    def _find_heredoc_end(self, start: int, tag: str) -> int | None:
        """Index of the first line equal to `tag` after trim. Nothing else
        terminates a heredoc -- not END, not EOM, not a fence."""
        for k in range(start, self.n):
            if self.lines[k].strip() == tag:
                return k
        return None

    def _find_swallowed_call(self, start: int) -> int | None:
        """Index of the first CLIP:CALL header inside swallowed heredoc text."""
        for k in range(start, self.n):
            match = _SENTINEL_RE.match(self.lines[k].strip())
            if match and match.group(1).upper() == "CALL":
                return k
        return None

    # -- prose ---------------------------------------------------------------

    def _flush_prose(self) -> None:
        chunk = "\n".join(self._cur_prose).strip()
        self._cur_prose = []
        if chunk:
            self.prose.append(chunk)


def parse_reply(text: str) -> ParsedReply:
    """Parse one ingested clipboard text into a ParsedReply (never raises on
    malformed input -- anomalies become ParseIssues / truncation flags)."""
    normalized = normalize(text)
    p = _Parser(normalized)
    p.run()

    if p.calls:
        kind = "reply"
    elif p.ack_kind is not None:
        kind = p.ack_kind
    elif p.saw_sentinel or PROTOCOL_MARKER in normalized:
        kind = "reply"
    else:
        kind = "noise"

    warnings = list(p.warnings)
    truncated = False
    if kind == "reply":
        truncated = p.truncated_eof
        if not p.eom_present:
            warnings.append(ParseIssue("truncation_suspected", 0, "missing ===CLIP:EOM==="))
            truncated = True
        elif p.eom_calls is not None and p.eom_calls != len(p.calls):
            warnings.append(
                ParseIssue(
                    "calls_count_mismatch",
                    p.eom_line,
                    f"EOM declares calls={p.eom_calls} but {len(p.calls)} CALL block(s) parsed",
                )
            )
            truncated = True

    return ParsedReply(
        kind=kind,  # type: ignore[arg-type]
        calls=tuple(p.calls),
        says=tuple(p.says),
        prose=tuple(p.prose),
        warnings=tuple(warnings),
        eom=EomInfo(
            present=p.eom_present, calls=p.eom_calls, turn=p.eom_turn, chat=p.eom_chat
        ),
        truncated=truncated,
        saw_fence=p.saw_fence,
        normalized_hash=normalized_hash(text),
        ack_part=p.ack_part,
        ack_total=p.ack_total,
        ack_chat=p.ack_chat,
        nack_reason=p.nack_reason,
    )
