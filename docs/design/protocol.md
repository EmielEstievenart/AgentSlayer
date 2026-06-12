# AgentClip Wire Protocol v1 — "CLIP/1"

Protocol designer deliverable. All numbers below are defaults wired to config, not constants.

---

## 0. Design invariants

These drive every choice below; they come straight from the research digest:

1. **No markdown dependence.** No sentinel, key, or terminator uses backticks, `#`, `*`, or `_`. Sentinels are plain `=`/`:`/alpha lines that survive Copilot/Gemini markdown-stripping copy.
2. **Fence-agnostic parsing.** The LLM is told to fence its blocks (per-code-block copy button = 100%-reliable extraction on Copilot/Gemini); the parser works identically with or without fences.
3. **One reserved line inside raw content.** All multi-line content is heredoc-framed; the *only* line that can collide with content is the heredoc tag itself, and the tag is chooseable. This fully solves "file contains `>>>` or `===CLIP:END===`".
4. **Every failure routes back through the protocol.** Parse errors, truncation, denials, hallucinated tools — each produces a structured error result the LLM can act on. There is no failure mode whose only fix is "human edits text by hand".
5. **Round trips are the scarce resource** (each costs two human pastes). The protocol favors batching (multiple calls per reply) and self-describing payloads over chattiness.
6. **The format is symmetric.** Tool→LLM payloads use the same grammar as LLM→tool, so every turn re-teaches the grammar by example — important for long sessions where the bootstrap scrolls out of effective attention.

---

## 1. Sentinel grammar (normative)

### 1.1 Reserved line forms

A *sentinel line* is a line that, after normalizing NBSP→space and trimming surrounding whitespace, matches:

```
^={3,}\s*CLIP:(CALL|END|EOM|RESULTS|RESULT|PART|PART-END|ACK|NACK)\b(.*?)={0,}$
```

Keyword matching is case-insensitive. Trailing `===` is decorative and optional. Attributes in the middle section are space-separated `key=value` pairs, order-free.

### 1.2 CALL block

```
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
occurrence: 1
find <<EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace <<EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
```

**Header.** `id` = positive integer, unique within the reply, starting at 1. `tool` = tool name from the catalog. If `id` is missing or duplicated, the parser assigns/renumbers sequentially and reports the mapping back (§6.3) — ids exist only to correlate results.

**Inline params.** `key: value` — one line, value trimmed. Separator tolerance: `key:` or `key=` both accepted (LLMs drift between them).

**Heredoc params** (any param may be heredoc form; required for multi-line values):

```
key <<TAG
...verbatim lines, completely uninterpreted...
TAG
```

- Opener: `key <<TAG` — 2 or more `<` accepted (`<<` canonical, `<<<` tolerated since the legacy example used it).
- `TAG`: 1–32 chars of `[A-Za-z0-9_-]`. Canonical default is `EOT`.
- Terminator: a line equal to `TAG` after whitespace trim. Nothing else terminates a heredoc — not `===CLIP:END===`, not a fence, nothing. **All collision risk therefore reduces to the tag**, and the tag is free to vary.
- **Collision rule (taught verbatim in bootstrap):** *"If any line of your content is exactly the tag, use a different tag — e.g. EOT2, RAW_A. Check before you write."* The tool's own outbound payloads pick tags programmatically (`R1`, `R1x`, … — guaranteed collision-free by scanning content, which the tool can do perfectly).
- Content lines are preserved byte-for-byte (leading whitespace included). CRLF normalized to LF at ingestion.

Rejected alternative: bare `find <<< ... >>>` markers (the original sketch) — unrecoverable when content contains `>>>` (every Python repl transcript, every merge-conflict file). Tagged heredocs are a pattern LLMs already know cold from shell.

### 1.3 End-of-message marker

Every LLM reply containing calls MUST end with:

```
===CLIP:EOM calls=2===
```

`calls=` is the number of CALL blocks the LLM believes it sent. Missing EOM ⇒ reply truncated (§5.2). Count mismatch with parsed blocks ⇒ a block was eaten by lossy copy ⇒ also routed to §5.2. (LLMs count blocks reliably; we deliberately do NOT ask them to count chars or lines — they can't.)

### 1.4 Parser tolerances (decided, exhaustive)

| # | Input anomaly | Parser behavior |
|---|---|---|
| 1 | Code fence lines (```` ``` ````/`~~~`, any length, ± language tag) outside heredocs | Ignored silently |
| 2 | Prose outside blocks | Captured for transcript display, ignored by executor |
| 3 | "Copilot said:" prefixes, Perplexity citation tails | Covered by #2 |
| 4 | `=` runs ≥3, missing trailing `===`, keyword case, NBSP, smart-space | Normalized, accepted |
| 5 | `key=value` instead of `key: value` on param lines | Accepted |
| 6 | Unknown param key | Per-call warning, call still runs if required params present |
| 7 | Missing `===CLIP:END===` before next `CLIP:CALL` header | Auto-close previous block + warning in results |
| 8 | Unterminated heredoc at end of input | Reply-truncated path (§5.2) |
| 9 | Unterminated heredoc that swallowed a later `CLIP:CALL` header (LLM forgot terminator mid-reply) | **Recovery scan:** at EOF with heredoc open, scan swallowed text for `CLIP:CALL` headers; if found, fail *this* call with `code=unterminated_heredoc` and re-parse from the swallowed header. Later calls survive. |
| 10 | Duplicate / missing / non-integer `id` | Renumber sequentially, report mapping |
| 11 | Whole reply has no sentinel lines | Not protocol traffic — watcher ignores (pre-filter is the literal substring `===CLIP:` per the clipboard research) |

### 1.5 Why line-oriented, not JSON (one line, since it's decided)

JSON dies on the exact hazards we have: unescaped newlines in code, smart-quote substitution, fence mangling, and mid-string truncation is unrecoverable. Sentinel lines fail *per block* and every failure is localizable.

---

## 2. Bootstrap prompt

One canonical bootstrap, assembled from config at send time (budget number, max-calls number, OS, workdir name substituted in). **Estimated size: ~8,800 chars (~2,900 tokens at 3 chars/token).** Fits in one paste on Copilot-work/Gemini/Claude presets; goes out as 3 chunks on the ChatGPT-inline 4,000-char preset via §5.1 (one-time cost, acceptable). Rejected: a separate "lite" bootstrap — two grammars to maintain, and under-specified protocol is the #1 source of malformed replies.

Outline with the load-bearing passages verbatim:

```
SECTION 1 — ROLE (~500 chars)
You are a coding agent operating on the user's machine through a relay
tool called AgentClip. You cannot run anything yourself. You emit tool
calls as plain-text CLIP blocks (spec below); the user relays them to
AgentClip, which executes them in the project directory and pastes the
results back to you. Work autonomously: prefer issuing tool calls over
asking questions. Project root: {workdir_name} on {os}.

SECTION 2 — TRANSPORT WARNINGS (~700 chars)
- My messages may arrive as an attached text file (named like
  "Pasted text" or "paste.txt"). If so, read the ENTIRE file and treat
  its contents as the message body.    [attachment hazard, research §3]
- Every message I send ends with a line ===CLIP:EOM===. If that line is
  missing, my paste was cut off: reply with exactly
  ===CLIP:NACK reason=truncated=== and nothing else.
- If you receive ===CLIP:PART k/n=== : that is piece k of n of one
  message. For k<n reply with exactly ===CLIP:ACK k/n=== and nothing
  else. After part n/n, mentally concatenate all parts in order and
  respond to the whole message.

SECTION 3 — HOW TO EMIT CALLS (~1,800 chars)
Exact grammar: CALL header, key: value lines, heredoc rule, END line,
EOM line — the §1.2/§1.3 forms shown as two short examples, plus:
- Put ALL CLIP blocks inside ONE fenced code block opened and closed
  with ~~~~ (four tildes). Never split blocks across multiple fences.
  [tilde fence: immune to backticks in file content; gives Copilot/
   Gemini users the per-block copy button — research §5d]
- Heredoc collision rule, verbatim from §1.2, with a 4-line worked
  example writing a file that itself contains a line "EOT".
- ids: integers from 1, unique per reply.
- End every reply with ===CLIP:EOM calls=N===.

SECTION 4 — TOOL CATALOG (~4,200 chars)
10 entries; each = signature line, 1-2 semantic notes, one minimal
worked example block (examples average ~6 lines). Full specs in §3
of this document.

SECTION 5 — RULES OF ENGAGEMENT (~1,100 chars)
- At most {max_calls} calls per reply. If your reply would be long,
  send fewer calls — a cut-off reply wastes a round trip.
- Calls in one reply run in order; later calls see earlier effects.
  You will not see any results until your whole reply is processed,
  so only batch calls that don't depend on results you haven't seen.
- NEVER modify files via run_command (no sed/redirects/rm). Use
  write_file/edit_file/delete_file so every change is backed up and
  reversible.                       [undo contract — architecture]
- Read before you edit. Keep edit_file find-blocks small but unique.
- Some calls need user approval. status=denied means the user said
  no: do not retry unchanged; reconsider or use ask_user.
- Results may be truncated, marked like
  [truncated: showing lines 1-200 of 1843 - request further ranges].
  Re-request narrower slices instead of assuming you saw everything.
- When the task is complete and verified, send task_done. Until then
  every reply must contain at least one tool call.

SECTION 6 — THE TASK (~variable)
===CLIP:TASK===
{user task text}
===CLIP:EOM===
```

---

## 3. Tool catalog

Exactly 10 tools. Slot justification for the non-obvious one: **`delete_file` takes the last slot** because deletions routed through `run_command rm` would bypass the per-turn backup system and break "undo turn" — deletion *must* be a first-class, backed-up, approval-gated tool. Rejected for slots: `read_files` (batching already comes free from multi-call replies), `append_file` (folded into `write_file mode: append` — and it's the recovery path for writing files larger than one reply, §5.2), `stat` (`list_dir` shows sizes), `move_file` (write+delete or an approved `run_command`; rare enough).

Common rules: all `path`/`root` params resolve inside the working directory; absolute paths and `..`-escapes ⇒ `error code=path_outside_workspace`. All results are delivered in the §4 envelope; bodies are heredoc-framed with tool-chosen tags.

| tool | params (req\*) | result body (status=ok) |
|---|---|---|
| `read_file` | `path`\*, `start`, `end` (1-based, inclusive) | Line 1: `src/utils.py lines 80-140 of 412` then raw content heredoc. Range clamped to EOF with note. No line-number gutter — gutters contaminate the find-blocks LLMs copy back into `edit_file` (line numbers come from `grep` instead). Binary file ⇒ `error code=binary_file`. |
| `write_file` | `path`\*, `mode: overwrite\|create\|append` (default `overwrite`; `create` errors if file exists), `content`\* (heredoc) | `wrote 54 lines (1842 chars) to src/new.py (created)`. Diff approval gate; parent dirs auto-created. |
| `edit_file` | `path`\*, `find`\* (heredoc), `replace`\* (heredoc), `occurrence: N\|first\|all` (default: must match exactly once) | `replaced 1 occurrence at line 88`. Errors: `match_not_found` (body includes closest near-miss region with line numbers, ≤20 lines — turns the LLM's blind retry into a guided one), `multiple_matches` (body lists line numbers; LLM adds context or sets `occurrence`). Match is exact-verbatim, with one fallback pass ignoring trailing whitespace per line (defeats UI whitespace mangling). |
| `delete_file` | `path`\* | `deleted src/old.py (backed up)`. Approval-gated. |
| `list_dir` | `path` (default `.`), `depth` (default 1, max 3) | Indented tree, dirs as `name/`, files as `name (1.2 KB)`; `.git`, `__pycache__`, `node_modules` etc. skipped with a note. |
| `glob` | `pattern`\*, `root` | One path per line + `42 matches` footer; capped per budget tier. |
| `grep` | `pattern`\* (regex), `path`, `glob` (filename filter), `ignore_case: yes`, `context: N` (default 0), `max` | `path:lineno: text` per hit (context lines `path:lineno- text`); capped + truncation note. This is the line-number oracle for ranged reads. |
| `run_command` | `command`\*, `timeout` (secs, default 60), `cwd` | Line 1: `exit 0 (2.1s)` then merged stdout+stderr heredoc, tail-capped per budget tier (tail, because test/build verdicts live at the end). Allowlist match ⇒ runs silently; else approval gate; user "no" ⇒ `status=denied`. Timeout ⇒ `error code=exec_timeout` with partial tail. |
| `ask_user` | `question`\* (inline or heredoc) | The user's typed answer, verbatim. The turn payload is not sent until the user answers (TUI contract). |
| `task_done` | `summary` (heredoc, optional) | Tool acknowledges, stops expecting calls, shows summary + session stats to user. Bootstrap: "after task_done, the session is over; do not emit further calls." |

---

## 4. Turn payload (tool → LLM)

Same grammar, `RESULT` blocks keyed by call id, in execution order:

```
===CLIP:RESULTS turn=4===
===CLIP:RESULT id=1 status=ok===
body <<R1
replaced 1 occurrence at line 88
R1
===CLIP:END===
===CLIP:RESULT id=2 status=error code=match_not_found===
body <<R2
find-block not found in src/utils.py.
Closest near-miss at lines 86-89 (differs in indentation):
    def parse_date(s):
        # NOTE: legacy format
        return datetime.strptime(s, "%d/%m/%Y")
hint: re-read lines 80-95 and resend the edit with the exact text.
R2
===CLIP:END===
===CLIP:EOM===
```

- **status:** `ok` | `error` (with `code=`) | `denied` (user rejected at approval gate) | `skipped` (user aborted the rest of the turn; bootstrap: "skipped calls did not run — resend them if still wanted").
- **Error codes (closed set):** `parse_error, unknown_tool, missing_param, bad_param, file_not_found, binary_file, path_outside_workspace, match_not_found, multiple_matches, exec_timeout, too_large, unterminated_heredoc, reply_truncated`. Every error body ends with a `hint:` line containing the recommended next action.
- **Truncation annotations** are in-band, first or last line of the body: `[truncated: showing last 120 of 2341 lines - rerun with a filter, or read_file specific ranges]`.
- **Parse errors** that prevent a call from executing become a RESULT with the id the parser assigned (or `id=0` if no header was recoverable), `code=parse_error`, body quoting ≤10 lines around the offending region plus a one-line grammar reminder. Well-formed sibling calls in the same reply still execute — one bad block never wastes the whole round trip.
- Hallucinated tool ⇒ `code=unknown_tool`, body lists the 10 valid names.
- Result bodies are always heredoc-framed with tool-chosen collision-free tags, so a result that *contains* `===CLIP:` lines (grepping AgentClip's own source!) cannot confuse the LLM's reading of the envelope.

---

## 5. Chunking

### 5.1 Outbound (tool → LLM): PART/ACK

When a serialized payload exceeds the active inline budget, split **on line boundaries** into parts ≤ budget minus envelope overhead (~200 chars):

```
===CLIP:PART 2/3===
<raw line-aligned slice of the payload>
===CLIP:PART-END 2/3===
Reply with exactly: ===CLIP:ACK 2/3===
```

Final part's trailer instead reads: `All 3 parts sent. Concatenate parts 1-3 in order and respond to the full message.`

- Watcher sees `===CLIP:ACK 2/3===` on the clipboard ⇒ auto-copies part 3, status bar: "paste part 3/3". Wrong-index ACK (user pasted parts out of order) ⇒ re-copy the correct part, status-bar warning. Duplicate part pasted ⇒ model just ACKs again (taught in bootstrap §2) — harmless.
- **Truncation check the model can actually perform:** presence of the `PART-END` line (a presence check, not a char count — models cannot count 6k chars). Missing ⇒ `===CLIP:NACK 2/3 reason=truncated===` ⇒ tool re-copies the same part; after 2 NACKs the TUI suggests lowering the budget preset.
- **Calibration (one-shot command):** tool copies a numbered ruler payload (`MARK 0500`, `MARK 1000`, … every 500 chars) and asks the model to report the last MARK visible; sets the budget. Covers the historic silent-truncation UIs.

### 5.2 Inbound (LLM → tool): truncated-reply detection and resume

Triggers: missing `EOM`; `calls=` ≠ parsed block count; unterminated heredoc/CALL at end of input. Recovery flows through a normal results payload:

```
===CLIP:RESULTS turn=5===
===CLIP:RESULT id=0 status=error code=reply_truncated===
body <<R0
Your reply was cut off. Received 1 complete call (id=1, executed; result
below) and a partial call id=2 (tool=write_file, heredoc 'content' not
terminated). Partial call was NOT executed.
hint: resend call id=2 and any later calls. Do not resend id=1. If the
content is large, send the first half with write_file mode: create and
the rest with mode: append across replies.
R0
===CLIP:END===
===CLIP:RESULT id=1 status=ok===
...
===CLIP:END===
===CLIP:EOM===
```

Completed calls are executed and reported; only the cut-off tail is re-requested. `write_file mode: append` is the designated escape hatch for content larger than one reply.

### 5.3 Budget → caps table

Budget is in chars (presets per the limits research; token math assumes 3 chars/token for code-like payloads). Default per-tool caps by tier:

| inline budget | read_file default span | grep max hits | run_command tail | list/glob entries | advised max calls/reply |
|---|---|---|---|---|---|
| ≤ 4,000 (ChatGPT-inline, paranoid) | 120 lines | 25 | 60 lines / 3,000 ch | 100 | 3 |
| 4–8k (unknown, Copilot-unlicensed) | 250 lines | 50 | 120 lines / 6,000 ch | 200 | 5 |
| 8–32k (Gemini, ChatGPT attach-OK) | 600 lines | 100 | 250 lines | 400 | 8 |
| > 32k (Copilot work tab, Grok) | 1,500 lines | 200 | 500 lines | 1,000 | 10 |

Explicit ranged requests (`start`/`end`, `max`) are honored up to 4× budget — delivered via PART chunks; beyond that, truncated with annotation. The advised max-calls number is substituted into bootstrap Section 5.

---

## 6. Idempotency and safety

1. **Duplicate ingestion:** blake2b-128 over the normalized reply (fences stripped, CRLF→LF, trailing-whitespace-stripped lines). Watcher keeps the last 20 hashes; a match ⇒ silently ignored + status-bar notice "duplicate reply ignored". Tool's own outbound payload hashes live in the same suppression set (self-write suppression — mandatory, since results payloads contain `===CLIP:` and would otherwise re-trigger the watcher).
2. **Out-of-order / stale pastes:** exact re-copies are caught by (1). A *different* protocol-bearing clipboard while a turn is mid-approval or mid-PART-handshake ⇒ TUI modal "Unexpected reply detected — Replace current turn / Ignore" (never auto-execute). Regenerated replies (new hash, same intent) land here too; the per-call approval gates are the final backstop.
3. **Id hygiene:** ids missing/duplicated/non-numeric are renumbered sequentially at ingestion; if anything changed, the results payload leads with an informational note: `note: you sent two calls with id=2; treated as id=2 and id=3 below.` Correlation is thus never ambiguous on either side.
4. **Hallucinated tools/params:** closed-set validation ⇒ `unknown_tool` / `missing_param` / `bad_param` results with the valid alternatives in the hint. Execution of valid siblings proceeds.
5. **Ordering within a reply:** strictly sequential by (renumbered) id; a failed call does not halt later calls *except* later calls naming the same path as a failed `write_file`/`edit_file`, which are auto-`skipped` with `hint: prior edit of this file failed; resend after fixing.` (Prevents compounding a failed edit.)
6. **Mutation funnel:** only `write_file`/`edit_file`/`delete_file` mutate files; all three snapshot to the per-turn backup store before applying (undo contract). `run_command` is warned-against for mutation in the bootstrap but cannot be technically prevented — the allowlist + approval gate is the control.
7. **Path jail:** §3 common rule, enforced tool-side, reported as `path_outside_workspace`.

---

## 7. Worked 3-turn session (exact wire format)

**Paste 1 — user → chat (bootstrap; sections 1–5 as specified in §2, then):**

```
===CLIP:TASK===
tests/test_utils.py fails: parse_date in src/utils.py expects DD/MM/YYYY
but the spec says ISO dates (YYYY-MM-DD). Fix it and verify with pytest.
===CLIP:EOM===
```

**LLM reply 1 (user clicks Copy; watcher ingests):**

I'll fix the date format and run the tests to verify.

~~~~
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find <<EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace <<EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=run_command===
command: pytest tests/test_utils.py -q
===CLIP:END===
===CLIP:EOM calls=2===
~~~~

*(AgentClip: shows diff, user approves id=1; `pytest` matches the allowlist, runs; tool copies results, user pastes:)*

**Paste 2 — results payload:**

```
===CLIP:RESULTS turn=2===
===CLIP:RESULT id=1 status=ok===
body <<R1
replaced 1 occurrence at line 88
R1
===CLIP:END===
===CLIP:RESULT id=2 status=ok===
body <<R2
exit 0 (1.4s)
.....                                                            [100%]
5 passed in 0.31s
R2
===CLIP:END===
===CLIP:EOM===
```

**LLM reply 2:**

All five tests pass; the fix is verified.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Changed parse_date in src/utils.py to ISO format (%Y-%m-%d), line 88.
Verified: pytest tests/test_utils.py - 5 passed.
EOT
===CLIP:END===
===CLIP:EOM calls=1===
~~~~

*(AgentClip shows the summary, marks the session complete, stops the expectation loop. No further paste required; the TUI offers "undo turn" against the backup store.)*

---

## 8. Contracts for other designers

**Architecture designer must honor:**
- Parser is a pure function, clipboard-agnostic: `parse_reply(text: str) -> ParsedReply` where `ParsedReply(calls: list[ToolCall], prose: list[str], warnings: list[ParseIssue], truncation: TruncationInfo | None, eom: EomInfo | None, normalized_hash: str)` and `ToolCall(id: int, original_id: str | None, tool: str, params: dict[str, str], issues: list[ParseIssue])`. Serializer mirror: `render_results(turn_results, budget) -> list[str]` returning 1..n clipboard-ready chunks (PART-wrapped iff n>1).
- Executor must implement: sequential execution, per-turn backup snapshot before each mutation, path jail, allowlist gate, the §5.3 cap table, and the §6.5 same-path skip rule.
- Self-write suppression: every string `render_results` produces gets its normalized hash registered with the watcher *before* the clipboard write.
- Heredoc tag generation for outbound bodies must scan content and guarantee no collision.
- `task_done` flips session state to "complete"; watcher keeps running (user may continue) but the TUI must signal completion.

**TUI designer must honor:**
- Watcher pre-filter is the literal substring `===CLIP:`; all parsing happens off the UI thread; detection arrives as a posted message carrying `ParsedReply`.
- Status bar fields fed by this protocol: watcher state, active preset + budget chars, PART progress ("paste part 2/3"), duplicate-ignored notices, NACK retry counter.
- Approval flow returns exactly one of approve / deny / abort-rest-of-turn, mapping to `ok` / `denied` / `skipped`; "auto-accept edits this session" only affects `write_file`/`edit_file`/`delete_file`, never `run_command`.
- `ask_user` blocks payload assembly until the user answers in the TUI (or explicitly cancels ⇒ `denied`).
- "Unexpected reply" modal (§6.2) and the calibration command (§5.1) need UI affordances.
- Bootstrap composer needs per-preset substitutions: budget, max-calls, and the tilde-fence instruction (kept on for all presets).