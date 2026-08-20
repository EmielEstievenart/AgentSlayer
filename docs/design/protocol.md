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
find << EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace << EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
```

**Header.** `id` = positive integer, unique within the reply, starting at 1. `tool` = tool name from the catalog. If `id` is missing or duplicated, the parser assigns/renumbers sequentially and reports the mapping back (§6.3) — ids exist only to correlate results.

**Inline params.** `key: value` — one line, value trimmed. Separator tolerance: `key:` or `key=` both accepted (LLMs drift between them).

**Heredoc params** (any param may be heredoc form; required for multi-line values):

```
key << TAG
...verbatim lines, completely uninterpreted...
TAG
```

- Opener: `key << TAG` — 2 or more `<` accepted (`<<` canonical, `<<<` tolerated since the legacy example used it), and the space before the tag is optional to the parser but **mandatory in everything we teach or emit**. HTML only opens an element when a letter follows `<` immediately, so `< T` is inert text while `<TAG` is a start tag — and at least one chat client parses the glued form as one, absorbing the whole rest of the reply into it (tolerance #13).
- `TAG`: 1–32 chars of `[A-Za-z0-9_-]`. Canonical default is `EOT`.
- Terminator: a line equal to `TAG` after whitespace trim. Nothing else terminates a heredoc — not `===CLIP:END===`, not a fence, nothing. **All collision risk therefore reduces to the tag**, and the tag is free to vary.
- **Collision rule (taught verbatim in bootstrap):** *"If any line of your content is exactly the tag, use a different tag — e.g. EOT2, RAW_A. Check before you write."* The tool's own outbound payloads pick tags programmatically (`R1`, `R1x`, … — guaranteed collision-free by scanning content, which the tool can do perfectly).
- Content lines are preserved byte-for-byte (leading whitespace included). CRLF normalized to LF at ingestion.

Rejected alternative: bare `find <<< ... >>>` markers (the original sketch) — unrecoverable when content contains `>>>` (every Python repl transcript, every merge-conflict file). Tagged heredocs are a pattern LLMs already know cold from shell.

### 1.3 End-of-message marker and the chat name

Every session gets a short **chat name** — an `adjective-noun` handle like `amber-falcon`, drawn per session from two ~50-word lists (`protocol/names.py`, ~2,900 combinations). It is generated when the session's Engine is built, taught in the bootstrap, and shown once in the transcript so the user knows what their chat answers to.

Every LLM reply containing calls MUST end with:

```
===CLIP:EOM calls=2 chat=amber-falcon===
```

`calls=` is the number of CALL blocks the LLM believes it sent. Missing EOM ⇒ reply truncated (§5.2). Count mismatch with parsed blocks ⇒ a block was eaten by lossy copy ⇒ also routed to §5.2. (LLMs count blocks reliably; we deliberately do NOT ask them to count chars or lines — they can't.)

`chat=` is the session's chat name, echoed verbatim. **It is the ingest gate** (§6.2): a reply whose EOM carries the wrong name, or no name at all, never becomes a turn. Rationale: the only thing standing between AgentClip and an unrelated clipboard is "does this text look like protocol traffic", and *any* text does once the user has been pasting `===CLIP:` blocks around — a second chat tab, a scrollback copy, a doc quoting the protocol. A per-session token the model must reproduce turns that into a positive check. It is a handshake, not a secret: the threat model is accidents, not adversaries.

Matching is tolerant on the way in — case-insensitive, surrounding whitespace, quotes and backticks stripped — and exact after that.

**Outbound** payloads AgentClip composes end with `===CLIP:EOM turn=N chat=amber-falcon===`. `turn=N` is AgentClip's own ordering stamp and is no longer something the model is asked to echo (it remains parseable on the way in for tolerance); `chat=` is what the model reads and copies back.

The chat name is also the **routing key** while a sub-agent run is in flight (§2.1): two chats then exist, both stamping their own name, and the host picks the destination *before* any engine parses the text (`parser.peek_chat_name` — a cheap reverse scan for the last `chat=`-carrying EOM/ACK/NACK line). A paste that names the other chat is dropped with a warning rather than queued; a paste carrying no name at all falls through to the active session, where the engine's own gate is the backstop.

Rejected alternative: **turn echo** (`===CLIP:EOM calls=N turn=T===`, reply dropped when `T < current turn`). It only orders replies *within* one correctly-paired chat, so it caught stale re-pastes and nothing else: a fresh reply from the wrong chat carries a plausible turn number and sails through. The chat name subsumes it — and the turn number was one more thing for the model to get wrong.

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
| 12 | `chat=` missing / wrong / differently-cased / backticked on EOM or ACK/NACK | Parser records it verbatim-but-normalized and never rejects; the *engine* gates (§6.2) |
| 13 | Reply flattened by the chat client into one HTML element (see below) | Per-call `client_mangled_heredoc` issue, fatal — call never executes, and the id=0 "reply was cut off" result is suppressed because it would send the model in a loop |
| 14 | Sentinel line with another `CLIP:` marker glued on after its `===` (see below) | Attributes before the terminator still parse; reply-level `flattened_reply` warning, and the *engine* refuses the whole reply and asks the model to resend it fenced — twice, then it asks the user instead (§6.2) |
| 15 | Reply carrying CALL blocks but **no structural fence line at all**, on a service preset with `require_fenced_reply` (see below) | Parser records `saw_fence` and changes nothing else — the reply parses normally. The *engine* refuses the whole reply and bounces an `id=0 code=reply_unfenced` result asking for a fenced resend, sharing the two-then-the-user budget with #14 (§6.2) |

**Tolerance #11 has one scoped exception.** The auto-copy flow's **verified copy click** — and only that click — hands its harvest to the session even when it carries no `===CLIP:` at all; the text is shown in the transcript as prose and nothing in it executes (the engine still answers `Noise("not-protocol")` — §6.2's gate order is untouched). Not a setting: the flow arms a one-shot window (`AutomationController.prose_window`) immediately before that click and disarms it the moment the harvest returns, so the loosening lasts exactly one act. The watcher's pre-filter itself never loosens: it sees every copy the user makes and must keep ignoring the non-protocol ones, while the flow just watched the copy button write *this* text, so it alone knows the text is the model's reply. Display-only, per tolerance #2's rule: prose reaches the transcript, never the executor.

**Tolerance #13 — the one corruption that is not the model's fault.** A chat client that renders `key <<TAG` sees `<TAG` and parses a start tag. It then absorbs everything after it — the content lines, `===CLIP:END===`, the EOM line, the closing `~~~~` fence — as *attributes* of that element: collapsed onto one line, sorted alphabetically, quoted (which eats one `=` from each `===` run), and re-emitted with a closing `>`. The tell is the sort: the words come back in ASCII order, so the parameter is a bag of tokens with no recoverable order. This is visible in the chat window before the user copies anything; AgentClip only inherits it.

Detection needs two signals together, which is why false positives are ~nil: a line matching the heredoc-opener shape *with trailing content*, **and** `CLIP:END` or `CLIP:EOM` somewhere in that trailing text (`parser._client_mangled_opener`). A well-formed reply never puts a sentinel behind a heredoc tag on the same line.

Response: fail the call with `code=client_mangled_reply` and a message addressed to the **user**, not the model. "Resend this call" — correct for every other parse error — is an infinite loop here: the model emits valid text and the client mangles it identically. Nothing is recovered on purpose; `task_done`'s `summary` is the only thing handed back to a delegating agent, and silently accepting a sorted word-salad is worse than an error. The structural prevention is the mandatory space in `key << TAG` (§1.2), which makes the opener un-tag-shaped in the first place.

**Tolerance #14 — the other flattening: a reply that was never fenced.** CLIP blocks emitted *outside* a `~~~~` fence render as markdown prose, where single newlines are not line breaks; the copy button then hands over one long line — an EOM with the next message's whole `CALL … END` sequence riding behind it. Two consequences, both silent before this tolerance: the marker's own `===` terminator landed inside the last attribute value (`chat=swift-forge===~~~~`, which then failed the §6.2 chat gate and blamed the *chat name* for a transport fault), and the blocks behind it were never parsed at all — a reply whose one surviving call happened to satisfy `calls=` executed as if nothing were missing.

Detection is two signals together, as in #13: text after the sentinel's `===` terminator, **and** `CLIP:` inside that text. The attributes ahead of the terminator parse as usual, so the chat gate sees the real name; the glued text is *not* re-split into blocks. Reassembling it would mean guessing where the model's line breaks were and then executing the result — `run_command` included — from a line the transport already proved it mangled. Instead the parser records one reply-level `flattened_reply` warning and `Engine.ingest` refuses the whole reply, so nothing executes and nothing is silently dropped.

**Recovery is the model's, then the user's.** Unlike #13, resending is not a loop here: the usual cause is a fence the model left off, and putting one back costs it one reply. So the refusal goes *to the model first*, as an ordinary RESULTS payload carrying one `id=0 status=error code=reply_flattened` result (§4) — the same shape as the `reply_truncated` result of §5.2, copied to the clipboard by the same path, with the phase unmoved (still `AWAITING_REPLY`, now for the corrected reply). The body says two things the model would otherwise get wrong: **nothing ran** (so it resends *everything* — the truncation reflex of resending only the tail is the wrong instinct here), and the `~~~~` fence goes around the **whole** reply *including* the final EOM line (an EOM left outside the fence is flattened onto the fence line and the next paste fails identically). It quotes the offending line back, clipped to ~160 chars, because "your reply was flattened" is abstract until the model sees its own text with the break missing. None of this is taught in the bootstrap: §2's budget has no room, and this text only needs to exist at the moment it happens.

**At most two such bounces in a row** (`Engine._transport_bounces`, reset only by a paste that passes both transport checks — this one and #15's, see §6.2). Two populations of hosts hide behind one symptom: the model forgot the fence — one resend fixes it — or the host's copy path strips newlines from *every* copy, in which case the corrected reply comes back flattened exactly like the last one and the loop never terminates, burning the model's context and the user's turns. The third consecutive flattened paste is therefore refused to the **user** as a `ProtocolError` that quotes the same line and names the likely cause (copy from the raw/code view instead of the rendered message), because the host is what needs changing and the human is the only one who can change it.

**Tolerance #15 — the flattening that leaves no trace.** #14 is the *loud* half of prose rendering: the newlines died, whole blocks rode onto one line, and the wreckage is visible in the text. The quiet half is worse. When a host renders an unfenced reply as markdown, the copy hands back text that has been through the **inline** processor as well, and the transformation that matters is link-stripping: `[label](target)` becomes `target`. That shape is not rare in code — it is a C++ lambda introducer. `set_receive_handler([this](uint8_t const* d) {` comes back as `set_receive_handler(uint8_t const* d) {`, the capture list simply gone. The whole class of damage is reproducible in one line:

```python
re.sub(r"\[[^\]]*\]\(([^)]*)\)", r"\1", text)
```

**The reply parses perfectly.** That is the entire problem. Nothing in the CLIP grammar was touched — the sentinels, the heredoc tags, the EOM and its chat name all survive, because none of them are link-shaped. The corruption is *inside* a heredoc body, i.e. inside the one place the protocol promises to carry verbatim, and there is nothing downstream that can notice: `write_file` writes the mangled line to disk and reports success, or `edit_file` reports `match_not_found` against a find-block the model is certain it copied correctly and rewrites the file to "fix" a mismatch that never existed. AgentClip's own pipeline is byte-clean here and pinned by `tests/protocol/test_payload_fidelity.py`; the loss happens in the chat, before the clipboard.

So the refusal is a **transport-safety gate, not a parse failure**, and the `reply_unfenced` body says exactly that in its second sentence. A model told only "your reply was refused" goes hunting for a grammar mistake that does not exist, rewrites its perfectly good CALL blocks, and sends the rewrite unfenced again; naming the mechanism (and showing the `[this](int a)` example) is what turns the next reply into a *resend* instead of a *rewrite*.

**Why per-service, and off by default.** A missing fence is not evidence of corruption everywhere — on Copilot and Gemini it is evidence of the *recommended* workflow. Their per-code-block copy buttons hand over the block's **contents**, without the fence lines (§0.2), so on those hosts a perfect reply arrives unfenced every single time; golden fixture `002-two-calls-crlf-nofence` is exactly that shape, and it is a fixture rather than a bug report on purpose. A global gate, or one that defaulted on, would refuse every good reply on precisely the hosts the fence-agnostic parser was built to serve. `require_fenced_reply` is therefore a `[services.*]` key (and a tick in the service editor), turned on for the other kind of host: one whose whole-message copy *preserves* fence lines when the model fenced, and markdown-processes the text when it did not. There, and only there, "no fence arrived" is reliable evidence that the text has been through the renderer.

**Why `saw_fence` is structural.** The parser sets it only for a fence line it skipped while reading *grammar* — the top-level scan between blocks, or the debris scan inside a CALL body. Heredoc content is never scanned, so a reply whose only ```` ``` ```` lines sit inside the markdown file it is writing is correctly seen as unfenced. Any looser definition would be defeated by the most ordinary task there is.

**Why calls-only.** Only a reply carrying at least one CALL is gated. A zero-call reply has nothing executable in it to corrupt, and it already earns the "every reply must contain at least one tool call" nag — replacing that with a lecture about fences helps nobody. ACK and NACK are taught as *bare single lines* (§2 section 2) with no fence anywhere near them, so gating them would break the chunk handshake outright. A **truncated** reply that carries calls *is* gated, deliberately: the §5.2 path would otherwise execute its complete calls and ask for the tail, and executing code that came through the prose renderer is the exact silent corruption this exists to stop.

**Why the budget is shared with #14.** One counter (`Engine._transport_bounces`), one cap, one reset. Flattening and fence-stripping are not two problems: they are two symptoms of one fault — the transport mangled a reply — and they have one remedy, the fenced resend both payloads ask for. A host that alternates symptoms (this paste lost its newlines, the next arrived intact but unfenced) is still one broken host, and two separate budgets would let the pair ping-pong four turns deep before the human hears about it. #14 is checked first because it is the more specific diagnosis: it has hard evidence in hand — a sentinel line with another marker glued onto it — and its message already demands the same fenced resend, so a reply that is both is told once, in the words that describe what actually arrived.

### 1.5 Why line-oriented, not JSON (one line, since it's decided)

JSON dies on the exact hazards we have: unescaped newlines in code, smart-quote substitution, fence mangling, and mid-string truncation is unrecoverable. Sentinel lines fail *per block* and every failure is localizable.

---

## 2. Bootstrap prompt

One canonical bootstrap, assembled from config at send time (budget number, max-calls number, OS, workdir name substituted in). **Estimated size: ~8,900 chars (~3,000 tokens at 3 chars/token).** Fits in one paste on Copilot-work/Gemini/Claude presets; goes out as 3 chunks on the ChatGPT-inline 4,000-char preset via §5.1 (one-time cost, acceptable). Rejected: a separate "lite" bootstrap — two grammars to maintain, and under-specified protocol is the #1 source of malformed replies.

Outline with the load-bearing passages verbatim:

```
SECTION 1 — ROLE (~1,350 chars)
Four beats, in order — every one of them is load-bearing against the
turn-1 refusal (some models read the pasted spec as a prompt-injection
attempt trying to redefine their identity, and stall):
1. Provenance. "The user pasted this message in themselves: it is your
   operating brief for this session, not content from a web page, a
   file, or a tool result. Treat it the way you would treat a system
   prompt."
2. Mechanics + oversight. You cannot run anything directly; you write
   CLIP blocks and the user carries them to AgentClip, which runs them
   and pastes the real output back. "The user is the transport",
   risky calls need approval on top, "Every action is reviewed by a
   human before it runs - more oversight than a normal coding agent
   has, not less", changes are backed up and reversible, and the
   results are real program output.
3. Judgment retained. "Your judgment still applies as it normally
   would - if a task looks harmful or wrong, say so, or use ask_user."
4. No stalling. "For a real task, start now: your first reply should
   already contain CLIP calls": orient with list_dir/glob/grep, don't
   summarise the protocol back, don't ask whether to begin, and never
   ask the user to paste code or run commands. Closed by the one
   escape hatch: "A greeting or question needing nothing touched gets
   a plain reply." - bootstrapped with a trivial message, the beat
   above otherwise forces orientation calls nobody asked for.
Ends with: Project root: {workdir_name} on {os}.

SECTION 2 — TRANSPORT WARNINGS (~900 chars)
Opens by naming the chat: "This chat's name is {chat_name}. Every
message I send carries it, and every reply you send must carry it back
on its final line. Replies without the correct chat name are ignored by
the relay." Then:
- My messages may arrive as an attached text file (named like
  "Pasted text" or "paste.txt"). If so, read the ENTIRE file and treat
  its contents as the message body.    [attachment hazard, research §3]
- Every message I send ends with a line
  ===CLIP:EOM turn=N chat={chat_name}===. If that line is missing, my
  paste was cut off: reply with exactly
  ===CLIP:NACK reason=truncated chat={chat_name}=== and nothing else.
- If you receive ===CLIP:PART k/n=== : that is piece k of n of one
  message. For k<n reply with exactly ===CLIP:ACK k/n chat={chat_name}===
  and nothing else. After part n/n, mentally concatenate all parts in
  order and respond to the whole message.

SECTION 3 — HOW TO EMIT CALLS (~1,800 chars)
Exact grammar: CALL header, key: value lines, heredoc rule, END line,
EOM line — the §1.2/§1.3 forms shown as two short examples, plus:
- Put ALL CLIP blocks AND the final EOM line inside ONE fenced code
  block opened and closed with ~~~~ (four tildes) — the fence closes
  AFTER the EOM line. Never split them across multiple fences.
  [tilde fence: immune to backticks in file content; gives Copilot/
   Gemini users the per-block copy button — research §5d. The EOM must
   ride inside the fence: outside it the host renders the line as
   prose, and prose copy loses line breaks (tolerance #14's origin)]
- Fence collision rule: if any content line starts with three or more
  tildes, the outer fence must use MORE tildes than that line. Same
  shape as the heredoc collision rule below, same reason: the outer
  delimiter must not be reachable from inside. The parser already
  accepts `~{3,}`, so nothing downstream changes.
- Heredoc collision rule, verbatim from §1.2, with a 4-line worked
  example writing a file that itself contains a line "EOT".
- The space in `key << TAG` is required, with the reason attached (a
  glued tag reads as HTML to some chat clients and costs the whole
  reply — §1.4 tolerance #13).
- ids: integers from 1, unique per reply.
- End every reply with ===CLIP:EOM calls=N chat={chat_name}===, where
  {chat_name} is written exactly as shown, plus the consequence of
  getting it wrong ("ignored by the relay - the user then has to prompt
  you again, which costs a round trip"). The model is NOT asked to echo
  the turn number; that is AgentClip's bookkeeping.

SECTION 4 — TOOL CATALOG (~4,200 chars)
10 entries; each = signature line, 1-2 semantic notes, one minimal
worked example block (examples average ~6 lines). Full specs in §3
of this document.

SECTION 5 — RULES OF ENGAGEMENT (~1,250 chars)
- At most {max_calls} calls per reply. If your reply would be long,
  send fewer calls — a cut-off reply wastes a round trip.
- Calls in one reply run in order; later calls see earlier effects.
  You will not see any results until your whole reply is processed,
  so only batch calls that don't depend on results you haven't seen.
- NEVER modify files via run_command (no sed/redirects/rm). Use
  write_file/edit_file/delete_file so every change is backed up and
  reversible.                       [undo contract — architecture]
- Read before you edit. Keep edit_file find-blocks small but unique.
- Never ask the user to paste file contents or run commands for you -
  read and run things yourself with the tools above.
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
===CLIP:EOM turn=1 chat={chat_name}===
```

**The user's own extra instructions (unnumbered, conditional).** A preset may
carry `extra_instructions` — a line or two the user wrote about *this host*,
shipped verbatim under a bare `EXTRA INSTRUCTIONS FROM THE USER:` header
appended after section 5 (so it reads as the last word before the task). The
case it exists for is a host that corrupts what the model sends and cannot be
argued out of it: M365 Copilot eats `](` sequences, and "always put a space
between ] and ( in code you send" is a fix no protocol design can supply,
because it is a fact about one chat client. Per-service for that reason, empty
in every built-in, and when empty the section does not exist at all.

Unnumbered deliberately: sections 1–5 are the protocol talking, and this is the
user talking over it. Numbering it would invite the model to weigh it against
the rules above rather than on top of them, and would make the count in §2.1's
"sections 1 and 5 swapped" wrong for the sub-agent variant (which gets this
section too, unchanged — a sub-agent talks to the same host).

**It is not free.** Every character comes out of the headroom below, and it is
the *only* section a user can grow without touching this repo. The editor's
field is a single-line `Input` for exactly that reason: a paragraph pasted in
there is a session that never arms. The re-inject in §4 exists so that a long
session does not need a long instruction — one short line, repeated on demand.

**Budget headroom — read before adding prose here.** The whole bootstrap must fit ONE paste: there is no chunked-bootstrap fallback, so over-budget means `BudgetExceeded`, an error toast, and a session that never arms. Assembled with a real skills library (listing saturated at its budget/6 cap) it measures **11,930 chars** against the smallest presets' 12,000-char `max_paste_chars` — **70 chars of slack**, and the skills listing is only capped at budget/6, not fitted to what's left. (It was 11,885 / 115 before `run_command` gained its required `reason`, which cost +48 after trimming that entry's prose; and 11,933 / 67 before §3.2's "exactly one layer truncates" note, which gave 3 back by rewording `run_command`'s catalog entry — the handlers no longer tail-cap, so the entry no longer says they do.) Every sentence added to sections 1–5 spends that slack. Measure before and after (`Engine.start_task` on a `max_paste_chars=12000` preset), and if a rule can be stated in one line, state it in one line.

**The bootstrap's own budget is 10% wider (`composer.BOOTSTRAP_BUDGET_SLACK`).** `max_paste_chars` is doing two jobs at once. It is the hard ceiling of what the host's message box will swallow, *and* it is a comfort setting — chosen low so the paste the user makes **every turn** stays manageable. The 12,000 on the smallest presets is the second kind. The bootstrap is the one payload sent exactly once per session, so holding it to a per-turn comfort figure converts "the first paste is a bit long" into "the session never arms". `Composer._single` therefore allows the `bootstrap` kind, and only that kind, 10% over; `user_answer` and `note` go through the same method and are held to the budget exactly, because those are the pastes that repeat, and `results` is fitted by truncation as before. 10% of a comfort setting is still well inside any real message box.

This is **not** a licence to grow sections 1–5: the discipline above still stands and the measurement is still hand-made. What the slack buys is room for *optional* sections that only some services carry.

**The measurement, so the next one is comparable.** A 12,000-char preset (`max_paste_chars=12000`, hence `caps_for_budget`'s 8k–32k tier), a skills listing saturated at its `budget // 6` = 2,000-char cap, `workdir_name="project"`, `os_name="Windows 11"`, a one-character task, and `Composer.bootstrap` measured with `edit_by_lines` off and on. Today that reads **12,119 chars** with everything off and **13,043** with `edit_by_lines` on (§3.1 — +924 for the `replace_lines` entry, the `numbered` `read_file` entry and the §5 ordering rule), against a 13,200 ceiling — **157 chars of slack**, which `extra_instructions` also draws on. The previous figures here (11,982 / 12,906, 294 of slack) were taken with slightly different parameters, hence the recipe above; the +99 between then and now is `fetch_chunk`'s catalog entry (§3.2), which is two lines and carries no worked example precisely because this is what the budget looks like.

### 2.1 Sub-agent bootstrap variant

A chat opened to serve a `delegate` call gets the same bootstrap with **sections 1 and 5 swapped for sub-agent variants**. Sections 2–4 are byte-identical: a sub-agent talks to AgentClip over exactly the same wire, with its own chat name, and section 4 is generated from its own registry (which simply has no `delegate` entry — see §3).

Section 1 keeps beats 1–4 **verbatim** — the provenance/oversight/judgment framing is what stops a model reading the pasted spec as a prompt-injection attempt and stalling on turn 1, and a sub-agent's turn 1 is exactly as vulnerable — and appends a fifth beat:

```
You are a sub-agent. Another AgentClip agent delegated one bounded task to
you; you cannot see its conversation and it cannot see yours, and you have no
way to ask it anything - the task below is everything you get, so make
reasonable assumptions and state them. When the task is done, call task_done
with a `result` heredoc containing the complete deliverable: that text is the
only thing handed back to the agent that delegated to you. You cannot hand
work to a further sub-agent of your own; do this task yourself.
```

Section 5's last bullet becomes:

```
- When the task is complete and verified, send task_done with `result`
  carrying the full deliverable. Until then every reply must contain at least
  one tool call.
```

(The "after task_done the session is over" clause is dropped: a sub-agent's `task_done` *is* its hand-off, and the delegating agent's follow-up work happens in the master chat, not here.)

Section 6 carries the delegated task rather than the user's: the `task` param of the `delegate` call, plus its `context` param under a "Context from the delegating agent:" heading. The user is still the transport and still the approval authority for every gated call the sub-agent makes — delegation adds an agent, never an unsupervised one.

---

## 3. Tool catalog

Exactly 11 built-in tools, plus two conditional ones: an **optional `skill` tool** that appears only when Agent Skills are discovered on disk (see `docs/design/skills.md`), and an **optional `delegate` tool** that appears only for a master agent whose sub-agent chat window is calibrated. Both are catalog-gated at bootstrap time, so a model is never offered a tool the host cannot honour. Slot justification for the non-obvious one: **`delete_file` takes the last slot** because deletions routed through `run_command rm` would bypass the per-turn backup system and break "undo turn" — deletion *must* be a first-class, backed-up, approval-gated tool. Rejected for slots: `read_files` (batching already comes free from multi-call replies), `append_file` (folded into `write_file mode: append` — and it's the recovery path for writing files larger than one reply, §5.2), `stat` (`list_dir` shows sizes), `move_file` (write+delete or an approved `run_command`; rare enough).

Common rules: all `path`/`root` params resolve inside the working directory; absolute paths and `..`-escapes ⇒ `error code=path_outside_workspace`. All results are delivered in the §4 envelope; bodies are heredoc-framed with tool-chosen tags.

| tool | params (req\*) | result body (status=ok) |
|---|---|---|
| `read_file` | `path`\*, `start`, `end` (1-based, inclusive), `numbered` | Line 1: `src/utils.py lines 80-140 of 412` then raw content heredoc. Range clamped to EOF with note. **No line-number gutter by default** — gutters contaminate the find-blocks LLMs copy back into `edit_file` (line numbers come from `grep` instead). `numbered: yes` prefixes each line `N| `, and only §3.1's ranged-edit mode advertises it. Binary file ⇒ `error code=binary_file`. |
| `write_file` | `path`\*, `mode: overwrite\|create\|append` (default `overwrite`; `create` errors if file exists), `content`\* (heredoc) | `wrote 54 lines (1842 chars) to src/new.py (created)`. Diff approval gate; parent dirs auto-created. |
| `edit_file` | `path`\*, `find`\* (heredoc), `replace`\* (heredoc), `occurrence: N\|first\|all` (default: must match exactly once) | `replaced 1 occurrence at line 88`. Errors: `match_not_found` (body includes closest near-miss region with line numbers, ≤20 lines — turns the LLM's blind retry into a guided one), `multiple_matches` (body lists line numbers; LLM adds context or sets `occurrence`). Match is exact-verbatim, with one fallback pass ignoring trailing whitespace per line (defeats UI whitespace mangling). |
| `replace_lines` | `path`\*, `start`\*, `end`\* (1-based, inclusive), `replace`\* (heredoc) | **Only present when the service sets `edit_by_lines`** (§3.1). `replaced lines 88-90 of src/utils.py (3 lines -> 2 lines)`. An empty `replace` deletes the range. Diff approval gate like any edit; the same `edit` permission key as `edit_file`. Errors: `unverified_range`, `stale_read`, `bad_edit_order` — all §3.1. |
| `delete_file` | `path`\* | `deleted src/old.py (backed up)`. Approval-gated. |
| `list_dir` | `path` (default `.`), `depth` (default 1, max 3) | Indented tree, dirs as `name/`, files as `name (1.2 KB)`; `.git`, `__pycache__`, `node_modules` etc. skipped with a note. |
| `glob` | `pattern`\*, `root` | One path per line + `42 matches` footer; capped per budget tier. |
| `grep` | `pattern`\* (regex), `path`, `glob` (filename filter), `ignore_case: yes`, `context: N` (default 0), `max` | `path:lineno: text` per hit (context lines `path:lineno- text`); capped + truncation note. This is the line-number oracle for ranged reads. |
| `run_command` | `command`\*, `reason`\* (one line, why this command — display-only: it is never executed, it exists so the approval drawer shows the user the model's own intent next to the command line, flattened and clipped to 200 chars there), `timeout` (secs, default 60), `cwd` | Line 1: `exit 0 (2.1s)` then merged stdout+stderr heredoc, kept WHOLE (§3.2: the budget passes cut it for display, and cache what they cut). Allowlist match ⇒ runs silently; else approval gate; user "no" ⇒ `status=denied`. Timeout ⇒ `error code=exec_timeout` with partial tail; a user cancel ⇒ the process tree is killed the same way and reported as `error code=cancelled` with the partial tail. |
| `fetch_chunk` | `id`\*, `part`\* | One slice of the full output of a result that was truncated, prefixed with a one-line `part 2/4 of call 3 (run_command) from turn 12 (41203 chars total)` header. Read-only, auto (no gate), allow-by-default under its own `fetch_chunk` permission key — it reads a cache the engine filled from output the user already approved. Unknown/expired id ⇒ `error code=unknown_chunk` naming what *is* cached and telling the model to re-run the original tool; a part outside 1..K ⇒ `bad_param` naming the range. Its catalog entry is two lines with **no worked example**: the syntax is taught by the marker in the cut body itself (§3.2). |
| `ask_user` | `question`\* (inline or heredoc) | The user's typed answer, verbatim. The turn payload is not sent until the user answers (TUI contract). A user who does not want to answer presses Esc: nothing is sent, the call stays parked, and whatever they type next arrives as the answer prefixed with `[the user declined to answer the question and continued with a new request instead]`. Still `status=ok` — there is no "denied" for `ask_user`. |
| `task_done` | `summary` (heredoc, optional), `result` (heredoc, optional — **advertised to sub-agents only**) | Tool acknowledges and stops expecting calls; the session is marked complete but the user may continue (§8). The model's summary is shown inline in the transcript; full summary + session stats are available on demand (the `e` / SummaryScreen action), not force-pushed. Bootstrap: "after task_done, the session is over; do not emit further calls." `result` is a sub-agent's deliverable: it becomes the result body of the `delegate` call that spawned it, verbatim, and is the *only* thing the delegating agent sees. The engine reads `result` in both roles (a master that sends one simply has nobody to hand it to); only the sub-agent's catalog doc teaches it. Empty/absent `result` ⇒ the host falls back to `summary`, then to a placeholder — the delegating agent's result body is never empty. |
| `delegate` | `task`\*, `context` | **Master only, and only when the sub-agent chat window is calibrated** (`allow_delegate`); otherwise the tool is absent from the catalog entirely. Hands one self-contained sub-task to a fresh sub-agent in its own chat with its own context window, bootstrapped with the §2.1 variant. Synchronous and single-flight: the delegating turn parks (`Phase.AWAITING_SUBAGENT`) until the sub-agent's `task_done` arrives, so at most one chat is live at any instant. Result body = the sub-agent's `result`, verbatim. Missing `task` ⇒ `error code=missing_param` and the turn continues unparked. Every failure of the run itself (chat not calibrated, new-chat click unverified, user abort) comes back as `status=error` on this call, so the model always learns what happened. Auto-approved: opening a sub-agent chat is loudly user-visible and every call the sub-agent makes is gated normally, so a gate here would add friction, not oversight. **A sub-agent's registry has no `delegate`**, so a nested attempt resolves as the ordinary `unknown_tool` error listing the valid tools — nesting is excluded by construction. |
| `skill` | `name`\* | The full body of the named Agent Skill (a reusable procedure/reference), verbatim. Read-only, auto (no gate). Only present when ≥1 model-invocable skill is discovered; the catalog lists each skill's name + description so the model picks one and loads it on demand (progressive disclosure). Unknown name ⇒ `error code=unknown_skill` listing the available skills. See `docs/design/skills.md`. |


### 3.1 Ranged edits — the `edit_by_lines` mode

**The problem.** `edit_file` needs the model to send back a *verbatim* copy of text that came out of the file. §1.4 #15 is the reason that is not always possible: some hosts rewrite code on the way out of the chat. M365 Copilot cannot reproduce a lambda unchanged and eats `](` sequences, so on that service a find-block **never** matches, and no amount of near-miss guidance helps — the model is copying correctly and the channel is corrupting it. That is a *permanently* broken edit path, not a flaky one.

**The trade.** `replace_lines(path, start, end, replace)` sends line numbers and the *new* code only. Nothing that came out of the file has to survive the trip back, so the lossy leg is bypassed entirely. The price is that the tool loses `edit_file`'s self-verification: a find-block that does not match refuses, whereas "lines 88-90" is true of every file with ninety lines. A wrong number is a silent wrong edit.

**Opt-in, and additive.** `ServicePreset.edit_by_lines` (default `false`) turns it on per service. `edit_file` stays in the catalog — find/replace is still the better edit wherever the host carries code faithfully — and `read_file` swaps its catalog entry for the one that teaches `numbered`. With the toggle off the catalog is byte-identical to a build that never heard of the feature, which matters twice: the gutter contaminates find-blocks, and the entries cost ~900 bootstrap characters (see the headroom note in §2).

**The guarantee.** A `replace_lines` is legal only when the range was inside a `read_file numbered: yes` **in the immediately preceding results payload**, and it is the engine that knows this, not the tool. Three layers, `engine/numbered.py` and `Engine._ranged_edit_guard`:

1. **Record** (compose time). The served-reads record is rebuilt from the **final rendered payload** — the literal characters the user copied — by scanning for surviving gutter lines inside each result's heredoc. It cannot be taken from the read's own `lines A-B of N` header: two truncation passes (`fit_results`, then the composer's `_fit_bodies`) can cut the *middle* out of a body afterwards, leaving that header claiming lines the model never saw. Each surviving line is then compared against the file as it stands at the end of the turn and dropped if it differs, so the record can never promise more than the disk holds. The record is **replaced wholesale** every results payload: "the results you were just given" means literally that, because a file edited two turns ago has had two turns to renumber itself.
2. **Plan** (before the approval gate, so a doomed call never makes the user read a diff). Four refusals, each naming its own recovery: `unverified_range` (nothing of that file was read numbered / this range was not inside what was shown — the body lists the ranges that *were*), `stale_read` (the file's content hash has moved since the read — any edit anywhere renumbers what is below it, so a changed file invalidates every range), and `bad_edit_order` for the two ordering rules below.
3. **Apply** (at the instant of the write). The engine hands the planned call the exact text it served for those lines (`ToolContext.numbered_slices`); `replace_lines` compares it with the file just before writing and raises `stale_read` if it has moved. This is the only layer that catches a `run_command` *earlier in the same reply* rewriting the file, and the preview reads the same field, so the gate's diff and the write can never be of different edits.

**Ordering.** Several ranges in one file in one reply must be sent **bottom to top** — strictly descending, non-overlapping starts. With descending order an applied edit only ever touches *higher* line numbers, so the ranges still waiting are still correct and no renumbering arithmetic exists to get wrong. Ascending or overlapping ⇒ `bad_edit_order` telling the model to reorder. This is stated in §5's rules as well as the tool's own entry, because it is a property of the *reply*, not of any one call in it.

### 3.2 Truncated results — the `fetch_chunk` cache

**What was wrong.** Two independent passes cut over-long result bodies before a payload goes out: `engine/results.fit_results` first, against the per-result cap `limits.max_result_chars`, and the composer's `_fit_bodies`/`_truncate_middle` second, against `preset.max_paste_chars` for the payload as a whole. Whatever they cut used to be **gone**, and the marker's advice — "request specific ranges" — was only ever real advice for `read_file`, where the file is still on disk. For anything whose output cannot be re-derived by range it was no advice at all: a 1,200-line MCP result arrived as ~300 lines and the remaining 900 had ceased to exist.

**The fix.** Before composing, the engine slices the full body of every result big enough to be worth keeping (over 1,000 chars) into contiguous parts and mints a monotonic id for it. The marker stamped into a body that *is* cut then names that id and the range: `[truncated by AgentClip - full output cached: fetch_chunk id=c7 part=1..4, one part per turn]`. Both passes take the marker as a **parameter** (`fit_results(..., markers)`, `composer.results(..., markers)`), because either can be the one that cuts a given body and a hint carried by only one of them is a hint the model gets only sometimes. The composer keeps the plain `TRUNCATION_MARKER` as the fallback for a caller with nothing cached, so `protocol/` learns nothing about tools or caches — it is handed text.

**Slices of the whole body, not "the omitted middle".** Reconstructing exactly what was dropped would mean modelling both passes at once — one of which water-fills a per-body cap by binary search against a payload whose size depends on every *other* body in the turn — and staying correct as either changes. The parts here are computed from the original body alone, so part 3 of `c7` means one thing forever, whoever cut what. The price is that a part may repeat text the model already saw at the head or tail of the truncated body; that is the cheap half of the trade. A part is sized (`chunk_chars_for`) at 60% of `max_paste_chars` **and** under `max_result_chars`, minus room for the header, so one part per turn can never itself be truncated. The cache holds at most 512,000 chars of any one body and the marker says so when that bites — a truncation the model is not told about is the exact failure this removes.

**Ids are minted before composing and kept after.** The marker has to name the part count, and the part count is a fact about the original body — the thing that stops existing the moment either pass runs. Which bodies were *really* cut is knowable only afterwards, so the order is mint, render, then keep only the ids whose marker survived into the rendered payload. Same derived-truth discipline as the served-reads record above, and for the same reason: what the model can act on is what the model was really shown. Unused ids are dropped and never reused, so a session's ids are not consecutive.

**Lifetime** (`Engine._update_chunk_cache`, where the rule is written down). A fetch by definition happens in a *later* turn than the truncation, so the cache must outlive a turn; it must not outlive the model's interest, or an expired id would serve output from three tasks ago as if it were fresh. So: **replaced** when a payload lands carrying new truncations (wholesale, never merged), **survives** a payload answering a turn that called `fetch_chunk` (a fetch must not evict what it is fetching, or a model working through parts 1..K loses the cache to part 1), and **cleared** otherwise. A transport bounce (`reply_flattened`/`reply_unfenced`) is not a turn and does not clear it — nothing ran, and the model is one resend away from using it.

**Engine-side only.** Nothing about this touches `Outbound` or `ToolResult`, so `engine/link/wire.py` is unchanged and a remote session inherits the feature with no protocol change: the engine that composed the payload is the engine that holds the cache, wherever it runs. The handler reaches it through a `ToolContext.chunk_cache` field wired once at `Engine.__init__` and mutated in place thereafter — by identity, because a fetch lands turns after the context was built.

**Exactly one layer truncates for display**, and it is the layer that also remembers. The first field report of the cache working showed it caching a body that had *already* been gutted: `mcp`'s handler applied `run_command`'s tail cap inside itself, so a 1,172-line answer became its last 308 lines before the ToolResult existed, and `fetch_chunk part=1` faithfully served line 865. Handlers therefore no longer cap finished output at all (`executor/tools/shell.retain_output`, shared by both): they return the whole thing and the two budget passes above do the cutting, having first cached what they cut. What is left in the handler is a **memory guard** at `limits.max_command_output_chars` — auto-resolved to the same 512,000 the cache stops holding at, so anything it drops is past what any fetch could have reached — and it is the one place `run_command`/`mcp` still stamp the honest `[truncated: showing last N of M output lines]`. The one other exception is a killed command's drain: that tail is an emergency buffer inside an error message, nothing will ever chunk-fetch it, and it stays capped to the budget's `caps`.

**The caps that bound all this scale with the budget.** `limits.max_result_chars` and `limits.max_command_output_chars` default to `0`, meaning *auto*, and are resolved once in `Engine.__init__` against that session's preset (`config.resolve_limits`; a sub-agent resolves against its own). `max_result_chars` auto is `max_paste_chars // 2` — 6,000 at the 12,000-char presets, which is the fixed number they used to ship, and 48,000 at a 96,000-char one, where the old constant clamped every result to an eighth of the room the user had. `max_command_output_chars` auto is the retention cap above. Every consumer — `fit_results`, `chunk_chars_for`, the handlers through `ToolContext` — is handed the resolved numbers, so the sentinel exists nowhere below that one line.

---

## 4. Turn payload (tool → LLM)

Same grammar, `RESULT` blocks keyed by call id, in execution order, and — like every outbound except the bootstrap — delivered inside a `~~~~` fence (see "Outbound payloads are fenced too", below):

```
~~~~
===CLIP:RESULTS turn=4===
===CLIP:RESULT id=1 status=ok===
body << R1
replaced 1 occurrence at line 88
R1
===CLIP:END===
===CLIP:RESULT id=2 status=error code=match_not_found===
body << R2
find-block not found in src/utils.py.
Closest near-miss at lines 86-89 (differs in indentation):
    def parse_date(s):
        # NOTE: legacy format
        return datetime.strptime(s, "%d/%m/%Y")
hint: re-read lines 80-95 and resend the edit with the exact text.
R2
===CLIP:END===
===CLIP:EOM turn=4 chat=amber-falcon===
~~~~
```

- **status:** `ok` | `error` (with `code=`) | `denied` (user rejected at approval gate, or a permission rule refused the call) | `skipped` (user aborted the rest of the turn; bootstrap: "skipped calls did not run — resend them if still wanted").
- **Error codes (closed set):** `parse_error, unknown_tool, missing_param, bad_param, file_not_found, binary_file, path_outside_workspace, match_not_found, multiple_matches, unverified_range, stale_read, bad_edit_order, exec_timeout, cancelled, too_large, unterminated_heredoc, reply_truncated, reply_flattened, reply_unfenced, unknown_skill, unknown_chunk`. Every error body ends with a `hint:` line containing the recommended next action. `cancelled` is the user pressing stop mid-batch: the running call was killed, every call after it never ran (same code, body says so), and the turn's results are sent as usual.
- **Truncation annotations** are in-band. A body cut by one of the two budget passes carries the §3.2 marker in the middle, naming the `fetch_chunk` id and part range that get the rest back — which is what a big result normally gets. A tool that capped its *own* output says so on the first or last line of the body instead (`[truncated: showing last 120 of 2341 lines - rerun with a filter, or read_file specific ranges]`); that annotation now means something narrower than it used to, namely a cut nothing can recover — a `list_dir`/`grep` cap the model can re-issue with a filter, or the 512k **memory guard** of §3.2. `run_command` and `mcp` no longer cap their own finished output at all.
- **Parse errors** that prevent a call from executing become a RESULT with the id the parser assigned (or `id=0` if no header was recoverable), `code=parse_error`, body quoting ≤10 lines around the offending region plus a one-line grammar reminder. Well-formed sibling calls in the same reply still execute — one bad block never wastes the whole round trip.
- **`id=0` results** are AgentClip talking about the *reply as a whole* rather than about a call: `reply_truncated` (§5.2), `reply_flattened` (§1.4 #14) and `reply_unfenced` (§1.4 #15). All three ride an otherwise ordinary RESULTS payload. `reply_truncated` leads a payload whose other results are the completed calls; the other two ride **alone**, because those replies were refused entirely — there are no sibling results, and the payload exists only to ask for the reply back.
- Hallucinated tool ⇒ `code=unknown_tool`, body lists the valid names.
- **Denied by a permission rule** (architecture §2 — a ruleset is always loaded, the shipped defaults if nothing else): the call never gates and never runs; its result is `status=denied` with the body `The user has specified a rule which prevents you from using this specific tool call. Here are some of the relevant rules <json array of the rules whose permission key matches>` (OpenCode's wording, so a model that has seen it there reads it the same way here). Unlike an interactive rejection it does NOT abort the turn: the later calls still run.
- **Denied by plan mode, or because nobody is there** (architecture §2) — same shape as a rule denial (pre-resolved, `status=denied`, the turn carries on), different body, because the model has to know *which* door was shut to pick another route:
  - `plan` mode, every call its overlay denies (`edit`/`bash`/`mcp`/`task`): body `plan mode is active: the user is only exploring and no changes may be made.` and `hint: explore with read_file/list_dir/glob/grep and present your plan via task_done or ask_user; the user can switch modes to enable execution.` A call `build` mode would have denied too keeps the rule wording instead — the mode is only named when the mode is the reason.
  - `unattended` on, every call that would have opened a gate: body `auto-denied: the user is away (unattended is on) and this call is not covered by an allow rule.`, then the rule-denial's own `Here are some of the relevant rules <json array>` line, then `hint: do not retry unchanged; continue with calls that allow rules cover, or finish with task_done and list what was blocked.`
- **The notes channel** (`===CLIP:RESULTS`'s leading `note:` lines — §6's id-hygiene rule is its other user) carries one more thing besides id hygiene: a **permission change** made mid-session. One of `permission mode is now plan: exploration only; edit/command calls will be denied.` / `permission mode is now build: normal approvals resumed.` / `the user has stepped away (unattended): only calls covered by allow rules will run; everything that would have asked them is auto-denied.` / `the user is back (unattended off): normal approvals resumed.`, on the first results payload after the change and never again (a user who cycles three times meant the state they landed on). The mode and the toggle share the one pending-note slot, so the latest change is the one the model hears about — two notes racing for one payload would only ever read as one instruction contradicting another. It is deliberately NOT in the bootstrap: §2's budget headroom has no room for prose about a state that may never be used, and every such denial explains itself in-band.
- **The notes channel carries one more one-shot**: the user's `extra_instructions` (§2), re-injected on demand. The bootstrap already carried them, and a long session on a host that mangles code drifts back to mangling it — so `r` in the TUI (tui.md §3.4h) arms a one-shot reminder that rides the **next outbound of any kind**, as `note: user instructions reminder: <the user's line>`, and clears. "Any kind" is load-bearing: a session steered by typed follow-ups produces no results payload for many turns, so `Composer.task()` takes the same `notes` sequence `results()` does and renders the same `===CLIP:NOTE===` block — ahead of the `===CLIP:TASK===` block rather than inside it, since `===CLIP:RESULTS turn=N===` is an envelope line notes can sit under and `===CLIP:TASK===` is the block itself. The bootstrap is the one outbound that never spends the flag: it embeds the instructions already, and the flag cannot be armed before a session exists.

- Result bodies are always heredoc-framed with tool-chosen collision-free tags, so a result that *contains* `===CLIP:` lines (grepping AgentClip's own source!) cannot confuse the LLM's reading of the envelope.

### 4.1 Outbound payloads are fenced too

`results`, `task` and `note` payloads are rendered **already wrapped in a `~~~~` fence**. The bootstrap is not — see below.

**Why.** §1.4 #14/#15 are about the chat processing text on the way *out*; the same host processes text on the way *in*. A payload pasted into the message box as plain prose is rewritten before the model ever reads it: the observed case was blank lines coming back as literal `<br>`, and the inline transformations of #15 apply here just as well — a results payload carries the model's own code back to it (a `read_file` body, a near-miss region from `match_not_found`), and code corrupted on the inbound leg is a model editing a file it was shown wrong. A fence tells the box "this is code, do not render it"; a model reads raw text either way, so on a host that does nothing to its input the cost is two inert lines. Per §0.6 there is a second, free benefit: every fenced outbound re-teaches the fence-your-reply rule by example, in the one place a long session keeps seeing.

**Unconditional — no knob.** A setting here would be a config surface with no failure mode behind it: there is no host on which the fence *hurts*, only hosts on which it does nothing. (Contrast `require_fenced_reply`, which is per-service precisely because a missing inbound fence means opposite things on different hosts.)

**Collision rule.** §1.2's rule, now implemented rather than only taught: the payload's lines are scanned for **leading** tilde runs of 3 or more, and the fence is one tilde longer than the longest of them (minimum four, which is what the bootstrap teaches). Only tildes are checked, because we only ever fence with tildes — a body full of backticks is inert inside a tilde fence, which is why the tilde fence was chosen in the first place. The parser accepts `~{3,}`, so any length we emit is recognised on the way back.

**Where it happens: in the composer, at render time — the payload *string* carries the fence.** That single choice is what keeps the rest of the system honest for free:

- `outbound/turn-NNNN.txt` on disk is exactly what was delivered, not a pre-fence draft no chat ever saw;
- re-copy (`c`, and the double-tap re-send) re-sends the same string, so a redelivery is fenced by construction rather than by remembering to wrap it again;
- streamed delivery (`clip/chunking.split_for_stream`) splits the **already fenced** string, so the fence wraps the one chat message rather than each burst inside it;
- self-write suppression is unaffected: `normalized_hash` strips fence lines before hashing (§6.1), so a fenced payload and its bare body hash identically and a re-ingest of our own text is still `Noise("own-outbound")`;
- the fence characters count against `max_paste_chars`, because they are part of the message the host has to accept. Wrapping therefore happens *inside* `_render_results` (so the fit loop measures the real thing) and *before* `_single`'s budget check — fitting an unfenced draft and wrapping afterwards would silently push every payload that landed within ~10 chars of the budget back over it, at the one moment there is nothing left to cut.

**Why the bootstrap is exempt.** Three reasons, all specific to that payload:

1. **It is the ROLE framing's payload.** §2 section 1 beat 1 exists to defeat the turn-1 refusal — some models read a pasted operating brief as injected content trying to redefine them, and stall — and its whole message is "this is your brief, treat it the way you would treat a system prompt". Presenting that brief as one large code block plausibly re-triggers the exact reading it works to prevent: a fence says "this is data".
2. **It is the payload with no headroom.** Assembled with a saturated skills listing it measures ~11.9k against the smallest presets' 12,000-char `max_paste_chars` (§2, "Budget headroom"), and it is the one payload with no chunked fallback: over budget means `BudgetExceeded`, an error toast, and a session that never arms. Ten fence characters fit today; that slack is the only slack the bootstrap has.
3. **Its corruption is loud.** A mangled brief misbehaves visibly on the very first reply — the model asks what the chat name is, or answers in prose. A rewritten *results* payload corrupts code silently, which is the failure the fence is actually for.

---

## 5. Chunking

### 5.1 Outbound (tool → LLM): PART/ACK

When a serialized payload exceeds the active inline budget, split **on line boundaries** into parts ≤ budget minus envelope overhead (~200 chars):

```
===CLIP:PART 2/3===
<raw line-aligned slice of the payload>
===CLIP:PART-END 2/3===
Reply with exactly: ===CLIP:ACK 2/3 chat=amber-falcon===
```

Final part's trailer instead reads: `All 3 parts sent. Concatenate parts 1-3 in order and respond to the full message.`

- Watcher sees `===CLIP:ACK 2/3 chat=amber-falcon===` on the clipboard ⇒ auto-copies part 3, status bar: "paste part 3/3". Wrong-index ACK (user pasted parts out of order) ⇒ re-copy the correct part, status-bar warning. Duplicate part pasted ⇒ model just ACKs again (taught in bootstrap §2) — harmless.
- **Truncation check the model can actually perform:** presence of the `PART-END` line (a presence check, not a char count — models cannot count 6k chars). Missing ⇒ `===CLIP:NACK 2/3 reason=truncated chat=amber-falcon===` ⇒ tool re-copies the same part; after 2 NACKs the TUI suggests lowering the budget preset.
- **Fencing a chunked send** (forward decision — M1 is single-chunk, so this is not code yet): each PART *chat message* rides inside **its own** fence, sized by §4.1's collision rule against that message's own lines. Not one fence spanning the whole send: a fence is a property of a message, and there is no way to leave one open across a message boundary. The reassembled payload is unaffected because the model is told to concatenate only the slices **between** the `PART`/`PART-END` markers, so the fence lines — which sit outside them, around the whole message including the trailer — never enter it. The `ACK`/`NACK` lines the model sends back stay bare single lines, unfenced, exactly as §2 teaches them.
- **Calibration (one-shot command):** tool copies a numbered ruler payload (`MARK 0500`, `MARK 1000`, … every 500 chars) and asks the model to report the last MARK visible; sets the budget. Covers the historic silent-truncation UIs.

### 5.2 Inbound (LLM → tool): truncated-reply detection and resume

Triggers: missing `EOM`; `calls=` ≠ parsed block count; unterminated heredoc/CALL at end of input. Recovery flows through a normal results payload:

```
===CLIP:RESULTS turn=5===
===CLIP:RESULT id=0 status=error code=reply_truncated===
body << R0
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
===CLIP:EOM turn=5 chat=amber-falcon===
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

1. **Eligibility — two preconditions, and no third.** A copy-paste is eligible for interpretation iff (a) **the clipboard changed** and (b) **the chat name matches**. (a) is the watcher's job and is enforced byte-wise on the raw clipboard text (`clip/watcher.py` keeps a `last_seen` (length, hash) pair); AgentClip's own writes advance that same baseline, so an identical model reply arriving *after* an outbound counts as changed and is forwarded. (b) is §6.2's chat gate.

   Consequently there is **no duplicate suppression on replies**. Re-pasting a reply AgentClip already ran **re-runs it, by design**: the user deliberately re-copying an older message is an instruction to re-interpret it, and a model that sends the identical response twice means it twice. What *is* suppressed is AgentClip's **own outbound**: blake2b-128 over the normalized payload (fences stripped, CRLF→LF, trailing-whitespace-stripped lines), last 20 chunks, checked at ingest and registered with the watcher before every clipboard write. That suppression is mandatory — results payloads contain `===CLIP:` and would otherwise read as a reply.
2. **Foreign / out-of-order pastes — the ingest gate.** `Engine.ingest` applies these checks, in this order, and the order is normative:

   | # | check | verdict |
   |---|---|---|
   | 1 | phase is neither AWAITING_REPLY nor DONE | `Noise("wrong-phase")` |
   | 2 | no sentinel lines at all | `Noise("not-protocol")` |
   | 3 | normalized hash among the last 20 outbound chunks | `Noise("own-outbound")` |
   | 4 | chat name (below) | `Noise("missing-chat")` / `Noise("wrong-chat")` |
   | 5 | phase is DONE and the paste is not a whole reply with a present EOM | `Noise("wrong-phase")` — otherwise the completed session **reopens** (DONE → AWAITING_REPLY, audited as a `reopened` event) and ingestion continues |
   | 6 | a sentinel line carries glued-on `CLIP:` text (§1.4 #14) | Nothing executes. First two in a row: `AutoReply` — an `id=0 code=reply_flattened` payload asking the model to resend the whole reply inside one `~~~~` fence, copied out like any results. Third in a row: `ProtocolError` for the user |
   | 7 | the preset sets `require_fenced_reply`, the reply carries ≥1 CALL, and no structural fence line arrived (§1.4 #15) | Same shape as step 6, one code along: `AutoReply` carrying `id=0 code=reply_unfenced`, then `ProtocolError` — **sharing step 6's counter**, so the two together get two bounces, not four |

   Step 3 sits ahead of the chat check because our own outbound now carries the chat name and would otherwise sail through step 4; the verdict means exactly one thing — "you copied AgentClip's own text back". A paste rejected at step 4, 6 or 7 is *not* remembered anywhere: a foreign or mangled paste must report the same reason every time it is pasted. Steps 6 and 7 come last so a *broken* paste from another chat is still reported as the foreign paste it is — and because their verdict is the one that *writes back to the chat*, which may only happen for a chat step 4 established is ours. Such a paste in DONE reopens the session first, which is right: the resend the payload asks for then lands normally.

   Step 6 precedes step 7 because it is the more specific diagnosis (§1.4 #15, "why the budget is shared"): it has evidence in hand and its message already demands the same fenced resend, so a reply that is both is told once. The two-then-stop rule is §1.4 #14's; the counter (`Engine._transport_bounces`) lives on the engine beside the phase, is per-session (a sub-agent gets its own budget), and is **reset only by a paste that passes both checks** — passing one and failing the other is not "arrived whole". `AutoReply` is the only outbound AgentClip composes with no turn behind it, so it is a distinct ingest verdict rather than a `Send`: the host copies it exactly like results, but no calls ran, no results exist, and the phase does not move.

   Step 5 is what makes completion non-final for ingestion. `task_done` ends the session, but the eligibility rule above knows nothing about completion, so a valid reply arriving afterwards must run. Only a *whole* reply may reopen: a present EOM that survived step 4 is proof the paste belongs to this chat, whereas an ACK/NACK has nothing left to acknowledge and a truncated reply carries no chat name at all (it is deliberately un-gated, below) — reopening on either would run text never established as ours, breaching precondition (b).

   The chat check applies exactly where the model was told to write the name:
   - reply with an EOM line ⇒ `eom.chat` must equal the session chat name;
   - `ACK` / `NACK` chunk line ⇒ same requirement on that line;
   - **reply with no EOM at all ⇒ not gated.** A missing EOM is the truncation signature (§5.2): the model may well have written the name on a line that never made it across, so the reply stays on the truncated-reply recovery path and the model is told to resend. Gating here would turn every truncation into a silent drop — the one failure mode §0.4 forbids.

   Everything downstream is unchanged: a *different* protocol-bearing clipboard while a turn is mid-approval or mid-PART-handshake ⇒ TUI modal "Unexpected reply detected — Replace current turn / Ignore" (never auto-execute). Regenerated replies (new hash, same intent) land here too; the per-call approval gates are the final backstop.

3. **Id hygiene:** ids missing/duplicated/non-numeric are renumbered sequentially at ingestion; if anything changed, the results payload leads with an informational note: `note: you sent two calls with id=2; treated as id=2 and id=3 below.` Correlation is thus never ambiguous on either side.
4. **Hallucinated tools/params:** closed-set validation ⇒ `unknown_tool` / `missing_param` / `bad_param` results with the valid alternatives in the hint. Execution of valid siblings proceeds.
5. **Ordering within a reply:** strictly sequential by (renumbered) id; a failed call does not halt later calls *except* later calls naming the same path as a failed `write_file`/`edit_file`, which are auto-`skipped` with `hint: prior edit of this file failed; resend after fixing.` (Prevents compounding a failed edit.)
6. **Mutation funnel:** only `write_file`/`edit_file`/`delete_file` mutate files; all three snapshot to the per-turn backup store before applying (undo contract). `run_command` is warned-against for mutation in the bootstrap but cannot be technically prevented — the `bash` rules + the approval gate are the control.
7. **Path jail:** §3 common rule, enforced tool-side, reported as `path_outside_workspace`.

---

## 7. Worked 3-turn session (exact wire format)

**Paste 1 — user → chat (bootstrap; sections 1–5 as specified in §2, then):** — note that this payload, alone among the outbounds, is deliberately **not** fenced (§4.1).

```
===CLIP:TASK===
tests/test_utils.py fails: parse_date in src/utils.py expects DD/MM/YYYY
but the spec says ISO dates (YYYY-MM-DD). Fix it and verify with pytest.
===CLIP:EOM turn=1 chat=amber-falcon===
```

**LLM reply 1 (user clicks Copy; watcher ingests):**

I'll fix the date format and run the tests to verify.

~~~~
===CLIP:CALL id=1 tool=edit_file===
path: src/utils.py
find << EOT
    return datetime.strptime(s, "%d/%m/%Y")
EOT
replace << EOT
    return datetime.strptime(s, "%Y-%m-%d")
EOT
===CLIP:END===
===CLIP:CALL id=2 tool=run_command===
command: pytest tests/test_utils.py -q
reason: check the date-format fix passes its tests
===CLIP:END===
===CLIP:EOM calls=2 chat=amber-falcon===
~~~~

*(AgentClip: shows diff, user approves id=1; `pytest` is approved at its own gate, runs; tool copies results, user pastes:)*

**Paste 2 — results payload** (fenced, per §4.1 — the fence is part of the string AgentClip copies out):

```
~~~~
===CLIP:RESULTS turn=2===
===CLIP:RESULT id=1 status=ok===
body << R1
replaced 1 occurrence at line 88
R1
===CLIP:END===
===CLIP:RESULT id=2 status=ok===
body << R2
exit 0 (1.4s)
.....                                                            [100%]
5 passed in 0.31s
R2
===CLIP:END===
===CLIP:EOM turn=2 chat=amber-falcon===
~~~~
```

**LLM reply 2:**

All five tests pass; the fix is verified.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary << EOT
Changed parse_date in src/utils.py to ISO format (%Y-%m-%d), line 88.
Verified: pytest tests/test_utils.py - 5 passed.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~

*(AgentClip marks the session complete and renders the model's summary inline in the transcript — full stats one keypress away via `e` — and stops the expectation loop. No further paste required; the user may type a follow-up to continue, or undo turns against the backup store.)*

---

## 8. Contracts for other designers

**Architecture designer must honor:**
- Parser is a pure function, clipboard-agnostic: `parse_reply(text: str) -> ParsedReply` where `ParsedReply(calls: list[ToolCall], prose: list[str], warnings: list[ParseIssue], truncation: TruncationInfo | None, eom: EomInfo | None, normalized_hash: str)` and `ToolCall(id: int, original_id: str | None, tool: str, params: dict[str, str], issues: list[ParseIssue])`. Serializer mirror: `render_results(turn_results, budget) -> list[str]` returning 1..n clipboard-ready chunks (PART-wrapped iff n>1).
- `EomInfo` carries `present`, `calls`, `turn` and `chat`; `ParsedReply` additionally carries `ack_chat` for ACK/NACK lines. The parser only records these — the §6.2 gate is the Engine's.
- The Engine is handed its `chat_name` explicitly at construction and exposes it as a read-only property; the Composer is handed the same name and stamps it on every payload. One generator call per session wires both (`make_engine_factory`), so two concurrent sessions can never accept each other's pastes.
- Executor must implement: sequential execution, per-turn backup snapshot before each mutation, path jail, the permission ruleset's gate, the §5.3 cap table, and the §6.5 same-path skip rule.
- Self-write suppression: every string `render_results` produces gets its normalized hash registered with the watcher *before* the clipboard write.
- Heredoc tag generation for outbound bodies must scan content and guarantee no collision.
- `task_done` flips session state to "complete"; watcher keeps running (user may continue) but the TUI must signal completion.

**TUI designer must honor:**
- Watcher pre-filter is the literal substring `===CLIP:`; all parsing happens off the UI thread; detection arrives as a posted message carrying `ParsedReply`.
- Status bar fields fed by this protocol: watcher state, active preset + budget chars, PART progress ("paste part 2/3"), ignored-paste notices (own-outbound, missing/wrong chat), NACK retry counter.
- The chat name is surfaced once in the transcript at session start ("chat name: amber-falcon — the model echoes chat=amber-falcon on every reply; pastes without it are ignored"), and named again in the `missing-chat` / `wrong-chat` toasts so a rejection is self-explaining.
- Approval flow returns exactly one of approve / deny / abort-rest-of-turn, mapping to `ok` / `denied` / `skipped`; "auto-accept edits this session" only affects `write_file`/`edit_file`/`delete_file`, never `run_command`.
- `ask_user` blocks payload assembly until the user answers. There is no cancel and no `denied`: Esc merely *dismisses* the question locally — the call stays parked and the user's next message answers it with the declined-prefix in front (`ui-briefs/modals-keys-esc.md` §3.3 stage 6). The only thing that ends the park without an answer is `/new`, which tears the whole session down behind it.
- "Unexpected reply" modal (§6.2) and the calibration command (§5.1) need UI affordances.
- Bootstrap composer needs per-preset substitutions: budget, max-calls, and the tilde-fence instruction (kept on for all presets).