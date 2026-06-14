"""Wire-level types for the CLIP/1 protocol.

This module is a stdlib-only leaf: it may not import from any other agentclip
module except nothing at all. Everything that crosses the parser/composer/engine
boundary is defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Cheap watcher pre-filter: clipboard text without this substring is never
# protocol traffic and must not be parsed.
PROTOCOL_MARKER = "===CLIP:"

# Sentinel keywords (the X in ===CLIP:X ...===).
SENTINEL_KEYWORDS = frozenset(
    {
        "CALL",
        "END",
        "EOM",
        "RESULTS",
        "RESULT",
        "PART",
        "PART-END",
        "ACK",
        "NACK",
        "TASK",
        "NOTE",
    }
)

ResultStatus = Literal["ok", "error", "denied", "skipped"]

# Closed set of error codes (protocol design §4). Every error result carries one,
# and its body ends with a "hint:" line telling the LLM the recommended next action.
ERROR_CODES = frozenset(
    {
        "parse_error",
        "unknown_tool",
        "missing_param",
        "bad_param",
        "file_not_found",
        "binary_file",
        "path_outside_workspace",
        "match_not_found",
        "multiple_matches",
        "exec_timeout",
        "too_large",
        "unterminated_heredoc",
        "reply_truncated",
        "unknown_skill",
    }
)


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """A tolerated anomaly found while parsing. kind values include:

    missing_end, bad_header, duplicate_id, renumbered, unterminated_heredoc,
    unknown_param, truncation_suspected, calls_count_mismatch, unknown_keyword
    """

    kind: str
    line: int  # 1-based line number in the normalized input; 0 = whole-reply issue
    detail: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: int  # canonical 1-based sequential id (parser renumbers when needed)
    tool: str
    # Scalar `key: value` params and heredoc params merged into one mapping.
    # Heredoc values are byte-faithful multi-line strings (CRLF normalized to LF).
    params: dict[str, str]
    raw: str  # verbatim block text, for transcript/audit
    original_id: str | None = None  # what the LLM actually wrote, if it differs
    issues: tuple[ParseIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class EomInfo:
    present: bool
    calls: int | None = None  # the LLM's own count of CALL blocks it sent
    turn: int | None = None  # echo of the turn number AgentClip stamped outbound


@dataclass(frozen=True, slots=True)
class ParsedReply:
    """Result of parsing one ingested clipboard text.

    kind:
      "reply" - a normal LLM reply (calls and/or prose)
      "ack"   - a chunk handshake ===CLIP:ACK k/n===
      "nack"  - ===CLIP:NACK ...=== (reason in nack_reason)
      "noise" - no protocol content found
    """

    kind: Literal["reply", "ack", "nack", "noise"]
    calls: tuple[ToolCall, ...] = ()
    prose: tuple[str, ...] = ()
    warnings: tuple[ParseIssue, ...] = ()
    eom: EomInfo = EomInfo(present=False)
    truncated: bool = False  # missing EOM / unterminated structures / count mismatch
    normalized_hash: str = ""  # blake2b hex over the normalized text (dedup key)
    ack_part: int | None = None  # k in ACK/NACK k/n
    ack_total: int | None = None  # n in ACK/NACK k/n
    nack_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: int
    status: ResultStatus
    body: str  # already truncated to per-tool caps; includes in-band truncation notes
    tool: str = ""
    code: str | None = None  # one of ERROR_CODES when status == "error"
    user_note: str | None = None  # the user's rejection reason on status == "denied"


@dataclass(frozen=True, slots=True)
class Outbound:
    """A clipboard-ready payload. len(chunks) > 1 means a PART/ACK chunked send."""

    kind: Literal["bootstrap", "results", "user_answer", "note", "calibration"]
    chunks: tuple[str, ...]
    total_chars: int
    turn: int
