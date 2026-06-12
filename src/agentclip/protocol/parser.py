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

from agentclip.protocol.types import (
    PROTOCOL_MARKER,
    EomInfo,
    ParsedReply,
    ParseIssue,
    ToolCall,
)

# A sentinel line, matched against the whitespace-trimmed line. Keyword is
# case-insensitive; trailing `===` is decorative. NOTE: PART-END must precede
# PART in the alternation or `\b` would happily split "PART-END" after "PART".
_SENTINEL_RE = re.compile(
    r"^={3,}\s*CLIP:(CALL|END|EOM|RESULTS|RESULT|PART-END|PART|ACK|NACK|TASK|NOTE)\b(.*?)=*$",
    re.IGNORECASE,
)

# Code-fence line (tolerance #1): ``` or ~~~, any length >= 3, optional simple
# language tag. Only consulted OUTSIDE heredocs.
_FENCE_RE = re.compile(r"^(?:`{3,}|~{3,})\s*[\w+.\-]*\s*$")

# Heredoc opener: `key <<TAG` (2+ '<' accepted; a stray colon after the key is
# tolerated). Tag = 1-32 chars of [A-Za-z0-9_-].
_HEREDOC_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:?\s*<{2,}\s*([A-Za-z0-9_-]{1,32})\s*$")

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
    """Dedup key (protocol design section 6.1): blake2b-128 hex over the
    normalized text with fence lines stripped and per-line trailing whitespace
    stripped. Stable across fenced/unfenced, CRLF/LF, and BOM variants."""
    lines = [
        line.rstrip()
        for line in normalize(text).split("\n")
        if not _FENCE_RE.match(line.strip())
    ]
    return hashlib.blake2b("\n".join(lines).encode("utf-8"), digest_size=16).hexdigest()


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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
        self.warnings: list[ParseIssue] = []
        self.prose: list[str] = []
        self._cur_prose: list[str] = []
        self.saw_sentinel = False
        self.truncated_eof = False
        self.eom_present = False
        self.eom_calls: int | None = None
        self.eom_turn: int | None = None
        self.eom_line = 0
        self.ack_kind: str | None = None  # "ack" | "nack"
        self.ack_part: int | None = None
        self.ack_total: int | None = None
        self.nack_reason: str | None = None

    # -- top level ---------------------------------------------------------

    def run(self) -> None:
        i = 0
        while i < self.n:
            stripped = self.lines[i].strip()
            match = _SENTINEL_RE.match(stripped)
            if match is None:
                if _FENCE_RE.match(stripped):  # tolerance #1: fences ignored
                    i += 1
                    continue
                self._cur_prose.append(self.lines[i])
                i += 1
                continue
            self.saw_sentinel = True
            keyword = match.group(1).upper()
            attrs, positional = _parse_attrs(match.group(2))
            if keyword == "CALL":
                self._flush_prose()
                i = self._parse_call(i, attrs)
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

    def _handle_eom(self, i: int, attrs: dict[str, str]) -> None:
        if self.eom_present:
            return  # first EOM wins; later ones are echo junk
        self.eom_present = True
        self.eom_line = i + 1
        self.eom_calls = _to_int(attrs.get("calls"))
        self.eom_turn = _to_int(attrs.get("turn"))

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
                if kw in ("CALL", "EOM"):
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
            param = _PARAM_RE.match(stripped)
            if param:
                # Unknown keys are kept verbatim; the engine validates them.
                params[param.group(1)] = param.group(2).strip()
                j += 1
                continue
            # Fences, blanks, and soft-wrap debris inside a block: skipped
            # (still present in `raw` for the transcript).
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
        prose=tuple(p.prose),
        warnings=tuple(warnings),
        eom=EomInfo(present=p.eom_present, calls=p.eom_calls, turn=p.eom_turn),
        truncated=truncated,
        normalized_hash=normalized_hash(text),
        ack_part=p.ack_part,
        ack_total=p.ack_total,
        nack_reason=p.nack_reason,
    )
