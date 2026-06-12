# Chat service paste limits (researched 2026-06-12)

## General notes

CROSS-CUTTING FINDINGS (researched 2026-06-12; limits drift quickly, treat all numbers as user-editable presets, not constants):

1) CONSERVATIVE / UNKNOWN-SERVICE PRESET: 6,000 chars per paste. Rationale: the lowest commonly-hit hard limits in the wild are ~8,000 chars (unlicensed Microsoft Copilot Chat, older consumer Copilot modes 4k–8k); 6,000 keeps ~20-25% headroom under the 8k floor while staying under ChatGPT's ~5k paste-to-attachment threshold is impossible at 6k, so for truly unknown services that may silently convert pastes to files, a stricter 4,000-char "paranoid" preset is also worth shipping.

2) TOKENS VS CHARS: Almost every backend cap is token-based. AgentClip payloads are code-like (sentinels, paths, diffs), which tokenize at ~3 chars/token, not the ~4 chars/token of English prose. Budget math inside AgentClip should assume 3 chars/token.

3) PASTE-TO-ATTACHMENT IS THE #1 HAZARD, not hard truncation. ChatGPT (Plus/Pro/Business, >~5,000 chars), Claude.ai (small threshold, multi-line pastes), Perplexity (~8k tokens -> paste.txt), and Open WebUI (optional setting) all silently convert large pastes into file attachments. Consequences differ: Claude keeps attached pasted-text fully in model context (safe); ChatGPT and Perplexity route attachments through a file-reading/retrieval pipeline that is usually-but-not-guaranteed lossless for small text. MITIGATION: the bootstrap protocol spec should explicitly tell the model "the user's message may arrive as an attached text file named pasted-text/paste.txt; read the ENTIRE file before acting and treat its contents as the message body." That makes attachment conversion survivable on every service.

4) HARD-LIMIT FAILURE MODES seen: visible character counter that blocks further input (Microsoft Copilot surfaces), "message too long" error on send (ChatGPT free, Gemini, Claude when context exceeded, DeepSeek "Length Limit Reached"), and historic SILENT truncation (older ChatGPT web, some Copilot/Bing builds truncated the paste at the counter limit). Because silent truncation still exists in the wild, the chunking protocol should embed a char-count/checksum in each chunk header (e.g. "part 2/5 len=11990") and instruct the model to verify and reply NACK on mismatch. Also ship a one-shot "calibrate" command: paste a known-length numbered test payload and ask the model to report the last marker it can see.

5) REPLY "COPY" BUTTON: ChatGPT, Claude.ai, DeepSeek, Open WebUI, Perplexity put raw markdown source in the text/plain clipboard flavor (parseable). Microsoft Copilot and Gemini are the problem children: their reply-copy is optimized for rich text and the plain flavor strips markdown markers. HOWEVER, AgentClip's ===CLIP:...=== sentinels are plain text lines containing no markdown syntax, so they survive markdown-stripping copy on every service tested in community reports. Design rules: (a) the wire grammar must never depend on backticks, asterisks, or heading markers; (b) parser must tolerate the blocks arriving wrapped in ``` fences (models love fencing them) AND arriving with fences stripped; (c) parser must tolerate leading/trailing junk (Perplexity appends a citation/source list after the answer; some UIs prepend "Copilot said:"). (d) Fallback worth documenting for users: every major chat UI gives each rendered CODE BLOCK its own copy button that yields verbatim text - instructing the LLM to emit all CLIP blocks inside ONE fenced code block makes the per-block copy button a 100%-reliable extraction path on Copilot/Gemini where the reply-copy is lossy. Consider making "wrap protocol blocks in a single code fence" part of the bootstrap prompt for the Copilot and Gemini presets specifically.

6) PRESET TABLE SHOULD CARRY TWO NUMBERS per service: "inline-safe" (stays below attachment-conversion threshold) and "max" (hard limit with 20% headroom); the suggested_preset_chars below picks the value optimizing reliability for an agent loop.

KEY SOURCES: ChatGPT 5k attachment rule: https://www.mindstudio.ai/blog/chatgpt-5k-character-attachment-rule-context-window , https://www.notebookcheck.net/ChatGPT-now-turns-long-pastes-into-attachments-for-Plus-Pro-and-Business-users.1259699.0.html , https://community.openai.com/t/chatgpt-converts-pasted-text-to-file-attachment/1369430 ; Copilot limits: https://techcommunity.microsoft.com/discussions/microsoft365copilot/co-pilot-character-limit/4389676 (work tab 128k / web tab 16k / unlicensed ~8k), https://learn.microsoft.com/en-us/answers/questions/2182542/character-limit-problem-on-copilot-chat-for-web (128k->8k regression, unresolved), https://learn.microsoft.com/en-us/answers/questions/5321232/copilot-pro-prompt-character-limit (consumer 2k/4k/8k/16k churn); Claude attachment behavior: https://github.com/unclecode/claude-paster , https://greasyfork.org/en/scripts/567635-claude-chunked-paste-bypass-attachment-detection ; Gemini ~30k chars: https://discover.oreateai.com/discover/how-the-gemini-character-limit-actually-works-across-different-models , https://support.google.com/gemini/thread/312836444 ; Grok 390k chars: https://x.com/techdevnotes/status/1920966515446141374 ; Perplexity 8k-token paste: https://www.datastudios.org/post/perplexity-ai-context-window-token-limits-and-memory-behavior , https://midjourneyv6.org/stop-perplexity-creating-paste-txt-file/ ; Open WebUI paste-as-file: https://github.com/open-webui/open-webui/discussions/6099 , https://github.com/open-webui/open-webui/issues/13577 ; DeepSeek: https://deepseekai.guide/guides/deepseek-character-limits/ ; ChatGPT copy-as-markdown: https://gist.github.com/tqwewe/06f9dc895dc03fdc2692b04d34c969c4 , https://sheremetyeva.medium.com/how-to-fix-copy-paste-markdown-from-chatgpt-1c571afed3ed .

## Services

### ChatGPT web (Free and Plus, GPT-5.x-era default models)

- **Paste limit:** Two distinct limits. (a) Paste-to-attachment threshold: ~5,000 characters - pastes above this auto-convert to a 'Pasted text' attachment on Plus/Pro/Business (rolled out ~March 2025; multiple sources). (b) Hard send limit: fuzzy, token-based; community reports range ~15,000-32,000 chars for free tier before 'message too long' errors; paid tiers accept far more via the attachment path (model context 128k+ tokens).
- **Suggested preset:** 4000 chars
- **Long-paste behavior:** Plus/Pro/Business: paste >~5k chars silently becomes a file-style 'Pasted text' attachment above the composer; user can click 'Show in text field' to revert it inline. Attached text is processed via the file-reading pipeline - usually read fully for small text files but routed differently than inline prompt and occasionally handled lossily (community complaints). Free tier: paste stays inline; oversized sends produce 'The message you submitted was too long' error; older builds silently truncated (~6k chars in an April 2023 empirical test), so verify-by-checksum is still advisable.
- **Copy button:** Reply copy button writes raw markdown source to the text/plain clipboard flavor and rich text to text/html. Plain flavor preserves code fences and structure (good for AgentClip's parser); known mangling is mostly LaTeX/math, which is irrelevant to the CLIP protocol. Community gripes about 'rich text copy' concern the HTML flavor used by rich editors, not the plain flavor a clipboard watcher reads.
- **Confidence:** medium
- **Notes:** Preset of 4,000 keeps payloads INLINE (below the ~5k attachment threshold) - the most reliable mode for protocol parsing. Offer an alternate 'ChatGPT (attachment OK)' preset of 12,000 chars for users who accept the attachment path; pair it with the bootstrap instruction to fully read attached pasted-text files. The 5k threshold and the hard cap have both changed within the last year - keep editable.

### Microsoft 365 Copilot Chat - work tab (licensed M365 Copilot, enterprise)

- **Paste limit:** ~128,000 characters; the input box shows a live character counter at 128k for users with a paid M365 Copilot license (Tech Community reports, 2025). In-app Copilot panes (Word/Excel/PowerPoint chat) are far smaller: 2,000-8,000 chars reported depending on app and license wave.
- **Suggested preset:** 96000 chars
- **Long-paste behavior:** Input box hard-stops at the counter limit - excess paste is cut off at the boundary (visible, since the counter pegs at max, but effectively truncation if the user does not look). No paste-to-attachment conversion reported. Note: users report grounding/RAG means very long inputs may still be summarized rather than fully attended.
- **Copy button:** Reply copy is optimized for rich text (HTML flavor for pasting into Word/Outlook); the text/plain flavor strips markdown markers (no #, **, and code fences not guaranteed). Plain sentinel lines like ===CLIP:CALL=== survive intact. Recommend the Copilot preset's bootstrap prompt instruct the model to wrap all CLIP blocks in one fenced code block so the user can use the code block's own copy button (verbatim) as fallback.
- **Confidence:** medium
- **Notes:** This is one of the user's two primary services. Do NOT use the in-app (Word/Excel) Copilot panes for AgentClip - their 2k-8k prompt boxes are too small. The 128k counter is widely reported but Microsoft has regressed limits before (128k -> 8k on the web surface, MS Q&A thread escalated March 2025, unresolved as of July 2025), so AgentClip should tell users to check the visible counter and set budget ~20% below it.

### Microsoft 365 Copilot Chat - web tab / unlicensed enterprise (Entra ID, no paid Copilot license)

- **Paste limit:** ~8,000 characters without a paid license; ~16,000 characters reported on the web-grounded tab in some tenants/waves. A documented regression took this surface from 128k down to 8k in early 2025.
- **Suggested preset:** 6000 chars
- **Long-paste behavior:** Input box character counter blocks/cuts input at the limit; users additionally report output truncation and that file uploads do not bypass the cap on this surface. No paste-to-attachment conversion.
- **Copy button:** Same as licensed Copilot: rich-text-oriented copy; plain flavor loses markdown formatting but plain sentinel lines survive. Per-code-block copy button is the reliable path.
- **Confidence:** medium
- **Notes:** 6,000 = 20%+ headroom under the 8k floor. If the tenant shows 16k on the counter, user can raise to 12,800. This surface's limit has churned three times in a year - runtime calibration strongly advised.

### Microsoft Copilot consumer (copilot.microsoft.com / Copilot in Edge / Copilot Pro)

- **Paste limit:** Historically mode-dependent and volatile: 2,000/4,000 chars (2024), raised to 4,000/8,000 (April 2024), with user reports of 10,240 and 16,000-char counters in some builds; one Copilot Pro user reported 16k suddenly reduced to 4k. No single stable number as of mid-2026.
- **Suggested preset:** 6000 chars
- **Long-paste behavior:** The input box has a visible character counter and refuses input past the limit (paste gets clipped at the boundary - effectively silent truncation of the tail if unnoticed). No paste-to-attachment conversion reported on this surface.
- **Copy button:** Same family behavior as M365 Copilot: copy targets rich text; plain flavor strips markdown. Sentinel lines survive; use code-fence wrapping + per-block copy as the robust path. Community extensions exist specifically because native markdown copy is absent.
- **Confidence:** low
- **Notes:** Most volatile service surveyed. Preset 6,000 fits the 8k-class builds; AgentClip's status bar should prompt the user to read the counter shown in their build and adjust. If counter shows 4,000, drop budget to 3,200.

### Claude.ai web

- **Paste limit:** Effectively context-window bound (~200k tokens, roughly 500-700k chars) rather than an input-box limit, because multi-line pastes past a very small threshold (community measurements: a handful of lines / a few thousand chars) auto-convert to an inline 'pasted text' attachment that IS fully included in model context. Immediate paste errors only at extreme (megabyte-scale) sizes.
- **Suggested preset:** 80000 chars
- **Long-paste behavior:** Large pastes silently become a 'PASTED' attachment chip - no option to keep inline natively (community scripts like claude-paster/Chunked Paste exist to bypass). Unlike ChatGPT, the attached pasted text is injected into the prompt verbatim, so nothing is lost to a retrieval pipeline. If the conversation+paste exceeds the context window, Claude shows an explicit 'message would exceed the length limit' error - no silent truncation.
- **Copy button:** Reply copy button yields raw markdown source in text/plain - the best-behaved copy button of all surveyed services for AgentClip parsing. Code fences, lists, and sentinel lines preserved verbatim.
- **Confidence:** medium
- **Notes:** Technically the friendliest service for AgentClip: lossless attachment conversion plus markdown copy. The 80k preset is conservative vs the context window but keeps free-tier usage burn and per-turn latency sane; Pro users could go 200k+. Bootstrap prompt should still mention content may arrive 'as a pasted attachment'.

### Gemini web (gemini.google.com, free and AI Pro)

- **Paste limit:** ~30,000-32,000 characters per message practical limit reported across community threads and 2025-2026 guides; described as a UI-level restriction independent of the model's (1M-token-class) context. Some users report lower limits by account/region.
- **Suggested preset:** 24000 chars
- **Long-paste behavior:** Oversized sends are rejected with a 'message too long'-style error (visible failure, not silent truncation) per community reports. No widely-reported automatic paste-to-attachment conversion in the standard composer as of the research date; file upload is the documented path for bigger inputs.
- **Copy button:** Weakest copy story surveyed: the response copy gives rendered text with markdown markers stripped (open feature requests on the Gemini Apps community asking for markdown copy; third-party 'Gemini to Markdown' extensions exist to fill the gap). Plain sentinel lines survive; bold/heading/fence syntax does not. Use code-fence wrapping + the code block's own copy button for reliability.
- **Confidence:** medium
- **Notes:** Preset 24,000 = 20% under the 30k floor. The 1M-token model context is irrelevant to paste budget - the composer is the bottleneck. Gemini's copy behavior makes the 'wrap CLIP blocks in one code fence' bootstrap instruction near-mandatory for this preset.

### Mistral Le Chat

- **Paste limit:** Unknown / no documented per-message UI limit found; bounded by model context (~128k tokens for Mistral Medium 3 / Large / Pixtral per 2025 docs). No community reports of a composer character cap surfaced.
- **Suggested preset:** 30000 chars
- **Long-paste behavior:** No automatic paste-to-attachment conversion reported (unlike Claude/ChatGPT). Exceeding model context causes prompt rejection or output truncation per Mistral docs. Behavior of the composer itself at extreme paste sizes is unverified - assume possible browser sluggishness and visible rejection.
- **Copy button:** Unverified. Le Chat renders markdown and modern chat UIs of its generation typically copy markdown source, but no authoritative community report found; third-party export tools exist for full conversations. Mark as 'verify at runtime' in the preset.
- **Confidence:** low
- **Notes:** 30,000 is a conservative guess (~10k tokens, well inside 128k context, similar to Gemini-class UI limits). Calibration command recommended on first use.

### DeepSeek web (chat.deepseek.com)

- **Paste limit:** No fixed per-message character cap documented; enforcement is token-based against the conversation context (64k tokens input+output for V3-era chat, 128k for newer versions). The visible failure is the well-known 'Length Limit Reached, Start a New Chat' error once cumulative context fills - a single huge paste can trigger it in one turn.
- **Suggested preset:** 40000 chars
- **Long-paste behavior:** Explicit 'Length Limit Reached' error rather than documented silent truncation; however, because the budget is cumulative across the thread and AgentClip loops are long-lived, the effective per-paste headroom SHRINKS every turn - the chunker should reserve aggressive margin and AgentClip should suggest 'new chat + re-bootstrap' when turns start failing. No paste-to-attachment conversion reported.
- **Copy button:** Community reports indicate the reply copy button yields raw markdown text (DeepSeek's UI is a fairly standard markdown chat). Not strongly verified - treat as likely-good, verify at runtime.
- **Confidence:** low
- **Notes:** 40,000 chars is ~13k tokens of code-like text, leaving most of the 64k window for conversation history. The cumulative-context failure mode makes DeepSeek the strongest case for AgentClip's planned 'context refresh' / re-bootstrap flow.

### Grok (grok.com / X)

- **Paste limit:** ~390,000 characters empirically measured in the grok.com prompt box (Tech Dev Notes on X, May 2025) - single-source. Model context 128k-256k tokens, so the box outruns the model. Free-tier constraints are about query counts (per-2-hour quotas), not paste size.
- **Suggested preset:** 100000 chars
- **Long-paste behavior:** Unverified beyond the 390k figure; no widespread reports of paste-to-attachment conversion or silent truncation found. Expect either input-box refusal or context-overflow errors at the extreme. Cumulative context limits apply across the conversation.
- **Copy button:** Unverified. No significant community complaints about Grok's copy button mangling markdown were found (weak positive signal). Verify at runtime; code-fence wrapping recommended as belt-and-braces.
- **Confidence:** low
- **Notes:** Preset capped at 100k (far below 390k minus headroom) because pastes beyond ~100k risk eating the model's token context in one turn and degrade browser performance; the box accepting text does not mean the model attends to all of it.

### Perplexity web

- **Paste limit:** ~8,000 tokens (~25,000-30,000 chars) of pasted text per query (datastudios 2025 analysis); pastes beyond that are auto-converted to a 'paste.txt' file attachment. Community reports suggest the conversion can trigger at smaller sizes in practice; threshold is unofficial and shifting.
- **Suggested preset:** 20000 chars
- **Long-paste behavior:** Auto-converted to a paste.txt attachment processed through the file-upload pipeline (retrieval-based) - full verbatim reading NOT guaranteed for large content, which is risky for protocol payloads. Community workarounds: edit-existing-prompt instead of pasting into a blank box, or the Complexity browser extension to suppress file creation.
- **Copy button:** Reply copy yields markdown but APPENDS citation markers and a sources list after the answer body - AgentClip's 'ignore prose outside sentinels' rule already handles this. Code blocks have individual copy buttons.
- **Confidence:** low
- **Notes:** Perplexity is search-oriented and a weak fit for an agent loop (it may run web searches on your tool results); preset included for completeness. 20,000 keeps most payloads inline below the reported ~8k-token conversion threshold.

### Open WebUI / self-hosted UIs (general guidance)

- **Paste limit:** No hard UI limit by default - bounded by the backend model's context window and browser performance. Optional 'Paste Large Text as File' toggle (Settings > Interface, shipped v0.4 via PR #7020, off by default) converts big pastes to file attachments; threshold configurable in newer builds (see issues #13577/#10970 for skip-hotkey and inline-toggle requests).
- **Suggested preset:** 100000 chars
- **Long-paste behavior:** With paste-as-file OFF: text stays inline; failure mode is backend context overflow (behavior depends on backend - Ollama and others may silently truncate oldest context, which IS a silent-truncation hazard). With paste-as-file ON: attachment goes through Open WebUI's RAG pipeline by default - lossy for protocol payloads unless the admin sets full-context document mode. AgentClip docs should tell self-hosters: disable paste-as-file, or set its threshold above the AgentClip budget.
- **Copy button:** Reply copy button yields raw markdown source in text/plain (recent Chrome builds also add an HTML flavor - issue #19083 - but the plain flavor remains markdown). Reliable for AgentClip.
- **Confidence:** high
- **Notes:** Since it is self-hosted, the real budget is min(model context, server settings). Suggest AgentClip expose this preset as 'Custom/self-hosted' with the 100k default and a prominent edit field; advise users to budget ~3 chars/token against their model's context minus conversation history.
