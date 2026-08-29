/* The GUI shell's frontend: one receiver, one renderer keyed on event type.

   The Python side is agentclip/shell/webview/bridge.py, which calls exactly one function
   here - window.agentclip.receive(event) - from a single drainer thread, so
   events arrive in the order they were raised and this file never has to think
   about ordering. Going the other way, everything the user does ends in a
   window.pywebview.api call, and every one of those lands on the same
   controller method the TUI's key binding does.

   Some keys never leave this file. F3 (the sidebar) and F8 (the log pane) are
   pure show/hide of a page element. /log comes back the other way as a `toggle`
   event, so the command and the key stay one implementation. F1 (help) and `t`
   (put the caret back in the chat box) are wholly local; F4's theme picker
   paints locally and tells Python, which is what persists it. F2 (MONITOR
   SEES) is a third one of those: the show/hide is this file's, and every word
   inside the block is composed on the Python side and pushed as `monitor_sees`
   - which is ui-monitor.md 11.4's whole point, that the answer belongs to the
   machine with the pixels and this window only reads it back.

   Two tables are the spine of the key surface, and both exist so that a thing
   cannot be done without being documented: KEYS below drives BOTH the keydown
   dispatcher and the help sheet's key table, and the `commands` event carries
   agentclip.shell.app.commands.COMMANDS, which drives BOTH the slash popup and the
   help sheet's command table.

   Installed at PARSE time, not on DOMContentLoaded: the bridge can be draining
   before the DOM exists (evaluate_js waits for the page, but the first state
   push and the "describe the task" prompt are queued the moment the loop
   starts), so receive() buffers until boot() has the elements.

   A classic script, not an ES module: the page is loaded from a file:// URL and
   Chromium refuses module scripts from that origin (docs/design/gui.md 2). No
   libraries of any kind - including the markdown renderer below, which is
   hand-written for exactly the subset a transcript needs. */

(function () {
  "use strict";

  var pending = [];
  var booted = false;
  var el = {};
  // One transcript per browser WINDOW, keyed by window id, each with its own
  // scroll state: a tab is a window (tui.md 1.6), its transcript is permanent,
  // and switching tabs must put the user back exactly where they were reading.
  // The keys are the Python side's MASTER_WINDOW / SUBAGENT_WINDOW.
  var panels = {};
  var MASTER_WINDOW = "m1";
  // Which window is SHOWN and which one new output is written into. They are
  // the same except during a delegation, and neither is the automation's live
  // target - selecting a tab never redirects a paste.
  var selectedWindow = MASTER_WINDOW;
  var runRows = {};
  var runOutput = {};
  var streamingCall = null;
  var tailOpen = false;
  var gateOpen = false;
  var gateAlwaysOffered = false;
  var rejectOpen = false;
  // Whether an `ask_user` is on the floor right now, mirrored off the `state`
  // push for the same reason `live` below mirrors the rest of it: the last Esc
  // stage has to answer "is there a question to cancel" BETWEEN events. The
  // banner's visibility is the same fact, but reading it off the DOM would make
  // a hidden attribute load-bearing.
  var awaitingAnswer = false;
  // What the last `state`, `status` and `run` pushes said, remembered because
  // the key hint footer has to answer "would this key fire right now" BETWEEN
  // events - which is the question MainScreen.check_action answers on the other
  // side, out of exactly these facts. Nothing else reads them: the page still
  // paints only what it is told, and this is that telling, kept.
  var live = {
    sessionActive: false,
    busy: false,
    phase: "IDLE",
    hasOutbound: false,
    armed: true,
    provider: "",
    executing: false
  };
  var modalId = null;
  var modalKind = null;
  // The page's OWN modals, which ride the same element the Python prompts do
  // (docs/design/gui.md 3): "help" (F1), "settings" (F4), "docs" (the titlebar's
  // user guide) and "payload" (the clipboard fallback). Never both at once with
  // a prompt - a prompt is a flow parked on an answer and it always wins.
  var pageModal = null;
  // The USER GUIDE, as it arrives from Python (the `docs` event: docs/*.md read
  // off disk verbatim), and which page the viewer is on. The name outlives a
  // close, so re-opening the button lands where the reader left off.
  var docPages = [];
  var docCurrent = "";
  // The slash-command registry, as it arrives from agentclip.shell.app.commands, and
  // the popup's two pieces of state. `popupIndex === null` is the RESTING state
  // of an unnarrowed list, not an error: nothing is highlighted until the user
  // has typed a letter or pressed an arrow, which is why Enter on a bare "/"
  // completes nothing (commands.py's own note on why /yolo is last).
  var commands = [];
  var popupMatches = [];
  var popupIndex = null;
  // Which composer mode is current. The popup is suppressed in exactly one of
  // them - `answer`, the TUI's `verbatim` - because while an ask_user answer is
  // open a leading slash is TEXT, and offering to complete it would be a lie
  // about what Enter is going to do (modals-keys-esc.md 6.1).
  var composerMode = "idle";
  // What has been SENT from the box this run, oldest first, and where the
  // arrows currently stand in it (`sentAt === null` is "not browsing", which is
  // a different state from "standing on the newest": only in the first is
  // ArrowDown the textarea's key again). `sentDraft` is what was half-typed
  // when the walk began, handed back by walking down past the newest - which is
  // what makes an accidental ArrowUp cost nothing.
  //
  // NOT called `history`: that name is `window.history`, and a var shadowing it
  // inside this closure is the sort of thing that works right up until
  // something in here reaches for a real navigation API.
  var SENT_MAX = 50; // the send-history cap: 50 recalls, which is more than anyone walks back
  var sent = [];
  var sentAt = null;
  var sentDraft = "";
  // Set while WE rewrite the box, so the `input` listener does not read a recall
  // as the user editing and end the walk on the spot. Assigning `.value` fires
  // no `input` event in any browser, so today this is belt AND braces - it is
  // here so that stays a fact about the DOM rather than a load-bearing one.
  var recalling = false;
  // The appearance, and the choices the settings modal offers. Both are Python's
  // ([gui] theme); this side only wears what it is told.
  //
  // THEME_NAMES is a FLOOR, not the list: the real one arrives with the first
  // `settings` event (config.VALID_GUI_THEMES, in the order THEME_CHOICES puts
  // them) and replaces it, so adding a palette is one Python edit plus one CSS
  // block and nothing here. It exists for the handful of milliseconds before
  // that event lands, when a `/theme` echo or a saved theme would otherwise be
  // validated against an empty list and fall back to the default.
  var THEME_DEFAULT = "dark";
  var THEME_NAMES = ["dark", "light", "claude-warm", "claude-dark"];
  var theme = THEME_DEFAULT;
  var themeChoices = [];
  // tui/widgets/transcript.py's MAX_EVENTS, per WINDOW: a transcript is a
  // reading surface, not an archive, and the whole log is one `l` away.
  var MAX_EVENTS = 500;
  // The SSH connect dialog. Open/closed is mirrored here because the form's
  // text boxes may only be rewritten on a RELOAD, and "was it already open" is
  // what tells a repaint which one it is.
  var connectOpen = false;
  var CONN_HINTS = {
    form: "the root is checked on the box, after connecting - a bad one is a retry",
    running: "cancel any prompt to give up on this attempt",
    failed: "Retry re-runs the whole sequence; Edit puts the values back in the form",
    done: "this session's tools, files and skills are on the remote machine now"
  };
  // The Monitor tab of the same dialog. Mirrored for the same reason
  // connectOpen is: the form's text boxes may only be rewritten on a RELOAD.
  var monitorOpen = false;
  var MON_HINTS = {
    form: "attaching swaps the screen, not the session - the transcript survives it",
    running: "dialling the monitor...",
    failed: "Retry dials again at these values; Edit puts them back in the form",
    done: "this window is driving that machine's browser now"
  };

  // The harness decision log, page-side. The deque below is the source of
  // truth; this is a view of it that happens to keep its own copy, because the
  // bridge is one-way and a pane revealed after an hour must show the whole
  // tail rather than whatever arrived since. Bounded at the deque's own number
  // (agentclip/driver/automation/harness_log.py, HARNESS_LOG_MAX): a debugging tail,
  // not an archive.
  var LOG_MAX = 500;
  var logEntries = [];
  var logOpen = false;
  // agentclip.driver.automation.harness_log.EMPTY_LOG_LINE - a log that explains its
  // own silence, so "nothing has happened" is never read as "nothing works".
  var EMPTY_LOG_LINE =
    "nothing logged yet - the harness writes here as it moves through the loop " +
    "(paste, send, generate, copy).";
  // What the picker's <option> list currently says, so a repaint that changes
  // nothing does not rebuild a <select> the user may have open.

  // The run panel's two depths, both of them tui/widgets/run_panel.py's:
  // RUN_TAIL_LINES is what the pane SHOWS (a compiler's last error plus its
  // context) and RUN_OUTPUT_LINES is what the page KEEPS per call, so a command
  // that prints a million lines costs the same as one that prints twelve. The
  // buffer is the truth and it fills whether or not the pane is open.
  var TAIL_LINES = 12;
  var OUTPUT_LINES = 400;

  window.agentclip = { receive: receive };

  function receive(event) {
    if (!booted) {
      pending.push(event);
      return;
    }
    try {
      dispatch(event);
    } catch (err) {
      // One malformed event must never close the channel: everything after it
      // is what tells the user what went wrong.
      if (window.console) console.error("agentclip: bad event", event, err);
    }
  }

  /* == helpers ============================================================ */

  function id(name) {
    return document.getElementById(name);
  }

  function escapeHtml(text) {
    return String(text === undefined || text === null ? "" : text).replace(
      /[&<>"']/g,
      function (ch) {
        return {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;"
        }[ch];
      }
    );
  }

  function api(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    var bridge = window.pywebview && window.pywebview.api;
    if (!bridge || typeof bridge[name] !== "function") return;
    try {
      bridge[name].apply(bridge, args);
    } catch (err) {
      if (window.console) console.error("agentclip: api " + name, err);
    }
  }

  /* == markdown =============================================================
     Hand-written, and only what its two consumers actually contain: the
     TRANSCRIPTS (fenced code, headings, bullet/ordered lists, paragraphs and
     the four inline forms) and the USER GUIDE, which is what the four block
     forms below it were grown for - pipe tables, block quotes, links, and the
     backslash escapes GitHub's flavour needs to write a `|` inside a table cell
     or a literal `<name>` in a heading.

     HTML is escaped FIRST and never unescaped, so neither model output nor a
     document full of literal <project> and <key> can inject markup -
     correctness over features, per docs/design/gui.md 2. Everything here is
     PURE - source in, HTML string out, no DOM and no state - which is what lets
     one renderer serve a transcript block and a reference manual. */

  // A code span's content, CommonMark's rule: one leading and one trailing
  // space are stripped when both are there and something else is too, which is
  // how a double-tick span around " ` " writes a literal backtick.
  function codeSpan(code) {
    if (
      code.length > 2 &&
      code.charAt(0) === " " &&
      code.charAt(code.length - 1) === " " &&
      /[^ ]/.test(code)
    ) {
      return code.slice(1, -1);
    }
    return code;
  }

  /* One pass, two jobs, because both are "lift this out before any other rule
     can see it":

       a CODE SPAN, by backtick RUN rather than by single tick - the
       configuration guide spells a literal backtick as a double-tick span
       around one, and a one-tick-only rule reads that line inside out;

       a BACKSLASH ESCAPE - how a heading writes `\<name\>` and a table cell
       writes `\|`. The HTML ENTITIES are in the list because escapeHtml has
       already run by the time this is applied: `\<` is `\&lt;` by then.

     What goes into the stash is finished HTML either way, so the restore at the
     bottom of inlineMarkdown puts back exactly one thing. */
  var SPANS = /(`+)([\s\S]*?)\1(?!`)|\\(&(?:amp|lt|gt|quot|#39);|[\\`*_{}[\]()#+\-.!|>~])/g;
  var LINK = /\[([^\]]*)\]\(([^()\s]*)\)/g;

  function inlineMarkdown(text) {
    // Both are lifted out FIRST, behind a marker the text cannot contain
    // (escapeHtml has already run, and a NUL survives no escaping), so
    // `*not italic*` inside backticks stays exactly what the model wrote.
    var codes = [];
    var out = escapeHtml(text).replace(SPANS, function (whole, ticks, code, escaped) {
      codes.push(escaped === undefined ? "<code>" + codeSpan(code) + "</code>" : escaped);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    out = out.replace(LINK, docLink);
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*\w])\*([^*]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_\w])_([^_]+)_/g, "$1<em>$2</em>");
    return out.replace(/\u0000(\d+)\u0000/g, function (_, n) {
      return codes[Number(n)];
    });
  }

  /* A link, in a window with nowhere to navigate to. Exactly one kind is live:
     a cross-reference to another page of the guide, which SWITCHES THE VIEWER
     (openDocs answers the click, and there is no href, so nothing can leave the
     page). Every other link keeps its words - plus its URL in brackets when the
     URL is one a user could paste into a browser themselves. */
  function docLink(_, label, href) {
    var found = /(?:^|\/)([\w-]+)\.md(?:#[\w-]*)?$/.exec(href);
    var name = found ? found[1].toLowerCase() : "";
    var known = docPages.some(function (page) {
      return page.name === name;
    });
    if (known) return '<a data-doc="' + name + '">' + label + "</a>";
    return /^https?:/i.test(href) ? label + " (" + href + ")" : label;
  }

  var BULLET = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
  var HEADING = /^(#{1,6})\s+(.*)$/;
  var FENCE = /^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$/;
  var QUOTE = /^\s*>\s?(.*)$/;

  // A table is its SECOND line: GFM's delimiter row, which is pipes, dashes,
  // alignment colons and nothing else. So a paragraph that happens to contain a
  // pipe stays a paragraph, and a `---` rule with no pipe is not a table
  // either. (Alignment is READ and ignored: these documents use none.)
  function isTableRule(line) {
    return /\|/.test(line) && /-/.test(line) && /^[\s|:-]+$/.test(line);
  }

  function startsTable(lines, i) {
    return lines[i].indexOf("|") !== -1 && i + 1 < lines.length && isTableRule(lines[i + 1]);
  }

  // One row's cells: split on UNESCAPED pipes, and turn `\|` into a real one
  // here rather than inline. That is GFM's order - the escape belongs to the
  // TABLE and is resolved before any inline rule, so a `\|` inside a code span
  // in a cell is a pipe there too, which is how commands.md writes
  // `/armed [on|off]`.
  function tableCells(line) {
    var cells = [];
    var cell = "";
    var text = line.trim();
    for (var i = text.charAt(0) === "|" ? 1 : 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (ch === "\\" && text.charAt(i + 1) === "|") {
        cell += "|";
        i += 1;
      } else if (ch === "|") {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += ch;
      }
    }
    // The closing pipe leaves an empty tail behind; a row without one does not.
    if (cell.trim() !== "" || cells.length === 0) cells.push(cell.trim());
    return cells;
  }

  // Wrapped in its own scroll box, because a four-column reference table is the
  // one thing in these documents that cannot be squeezed into a 400px window -
  // and a table that widened the modal would take the prose with it.
  function renderTable(head, rows) {
    var html = "<div class='doc-table'><table><thead><tr>";
    head.forEach(function (cell) {
      html += "<th>" + inlineMarkdown(cell) + "</th>";
    });
    html += "</tr></thead><tbody>";
    rows.forEach(function (row) {
      html += "<tr>";
      // Ragged rows are legal markdown; padding to the header is what keeps
      // every cell under the heading it belongs to.
      for (var col = 0; col < head.length; col++) {
        html += "<td>" + inlineMarkdown(col < row.length ? row[col] : "") + "</td>";
      }
      html += "</tr>";
    });
    return html + "</tbody></table></div>";
  }

  function renderMarkdown(source) {
    var lines = String(source === undefined ? "" : source)
      .replace(/\r\n?/g, "\n")
      .split("\n");
    var html = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var fence = FENCE.exec(line);
      if (fence) {
        var marker = fence[1].charAt(0);
        var body = [];
        i += 1;
        while (i < lines.length) {
          var close = FENCE.exec(lines[i]);
          if (close && close[1].charAt(0) === marker) {
            i += 1;
            break;
          }
          body.push(lines[i]);
          i += 1;
        }
        html.push("<pre><code>" + escapeHtml(body.join("\n")) + "</code></pre>");
        continue;
      }
      if (/^\s*$/.test(line)) {
        i += 1;
        continue;
      }
      var heading = HEADING.exec(line);
      if (heading) {
        var level = heading[1].length;
        html.push("<h" + level + ">" + inlineMarkdown(heading[2]) + "</h" + level + ">");
        i += 1;
        continue;
      }
      if (startsTable(lines, i)) {
        var head = tableCells(line);
        i += 2; // the header row and the delimiter row under it
        var rows = [];
        while (i < lines.length && !/^\s*$/.test(lines[i]) && lines[i].indexOf("|") !== -1) {
          rows.push(tableCells(lines[i]));
          i += 1;
        }
        html.push(renderTable(head, rows));
        continue;
      }
      var quote = QUOTE.exec(line);
      if (quote) {
        var quoted = [];
        while (i < lines.length) {
          var deeper = QUOTE.exec(lines[i]);
          if (!deeper) break;
          quoted.push(deeper[1]);
          i += 1;
        }
        // Recursive, so a quoted list or table is one - and the recursion is
        // bounded by the "> " this loop has already stripped.
        html.push("<blockquote>" + renderMarkdown(quoted.join("\n")) + "</blockquote>");
        continue;
      }
      var bullet = BULLET.exec(line);
      if (bullet) {
        var ordered = /^\s*\d/.test(line);
        var items = [];
        while (i < lines.length) {
          var next = BULLET.exec(lines[i]);
          if (!next) break;
          items.push("<li>" + inlineMarkdown(next[2]) + "</li>");
          i += 1;
        }
        var tag = ordered ? "ol" : "ul";
        html.push("<" + tag + ">" + items.join("") + "</" + tag + ">");
        continue;
      }
      var para = [];
      while (i < lines.length) {
        if (
          /^\s*$/.test(lines[i]) ||
          FENCE.test(lines[i]) ||
          HEADING.test(lines[i]) ||
          BULLET.test(lines[i]) ||
          QUOTE.test(lines[i]) ||
          startsTable(lines, i)
        ) {
          break;
        }
        para.push(lines[i]);
        i += 1;
      }
      html.push("<p>" + inlineMarkdown(para.join("\n")) + "</p>");
    }
    return html.join("");
  }

  /* == transcripts (one per window) ========================================= */

  function panel(win) {
    // An event for a window this page does not draw lands in the master's
    // rather than nowhere: a transcript line is never worth losing, and the
    // only way to get here is a Python side that grew a third window first.
    return panels[win] || panels[MASTER_WINDOW];
  }

  function append(win, node, beat) {
    var target = panel(win);
    var box = target.node;
    var empty = box.querySelector(".empty");
    if (empty) empty.parentNode.removeChild(empty);
    box.appendChild(node);
    // The first node of an ingested reply claims the mark `reply_start` armed;
    // `reveal_reply` scrolls back to it once the whole reply has landed.
    if (target.awaitingReplyStart) {
      target.replyStart = node;
      target.awaitingReplyStart = false;
    }
    // Prune from the top at MAX_EVENTS, the TUI's rule and its number. Per
    // window, because the cap is about how much DOM one scroll carries and each
    // panel is its own scroll; the exported log (`l`) is the archive and is
    // built from the Python-side event list, which this never touches.
    while (box.childElementCount > MAX_EVENTS) box.removeChild(box.firstElementChild);
    // A hidden panel has no layout to measure - offsetHeight and clientHeight
    // are both 0 - so the fit-or-park arithmetic below would park every event
    // written into the window the user is not looking at. Pin it to the bottom
    // instead and let the first paint after it is shown do the real thing.
    if (box.hidden) {
      target.stick = true;
      target.parked = false;
      box.scrollTop = box.scrollHeight;
      return;
    }
    // Fit-or-park (main-chat.md 6): an event taller than the viewport parks
    // with its top at the top so the user reads from the first line; anything
    // that fits pins the panel to the bottom unless they scrolled away.
    var viewport = box.clientHeight;
    if (viewport > 0 && node.offsetHeight > viewport) {
      box.scrollTop = node.offsetTop;
      target.parked = true;
      target.stick = false;
      return;
    }
    if (target.parked && !beat) return;
    target.parked = false;
    if (target.stick) box.scrollTop = box.scrollHeight;
  }

  // One ingested reply is prose plus a node per tool call, and the fit-or-park
  // rule above judges each of them alone - so a reply of small nodes ends
  // pinned at its LAST line with its opening scrolled off the top. The Python
  // side brackets the reply (ChatView.begin_reply/reveal_reply) and this is the
  // other half: fit-or-park asked ONCE, about the reply as a whole. A reply is
  // always the transcript's tail, so its height is everything from its first
  // node down. Taller than the viewport, there is more of it than the user can
  // see at once and where they start reading matters: park its first line at
  // the first row, the same park a tall node gets. Shorter, the whole reply is
  // on screen from the bottom anyway - so go to the bottom and resume
  // following, because a park there would buy nothing and cost everything
  // after it: every short reply would leave the panel frozen, with the next
  // turn's output piling up below the fold.
  function revealReply(win) {
    var target = panel(win);
    var node = target.replyStart;
    target.replyStart = null;
    target.awaitingReplyStart = false;
    var box = target.node;
    // A hidden panel has no layout to measure (offsetTop is 0), exactly as in
    // `append`: leave it pinned and let the first paint after it is shown do
    // the honest thing. A pruned node is likewise nothing to scroll to.
    if (!node || box.hidden || node.parentNode !== box) return;
    if (box.scrollHeight - node.offsetTop > box.clientHeight) {
      box.scrollTop = node.offsetTop;
      target.parked = true;
      target.stick = false;
      return;
    }
    box.scrollTop = box.scrollHeight;
    target.stick = true;
    target.parked = false;
  }

  function block(className, html) {
    var node = document.createElement("div");
    node.className = className;
    node.innerHTML = html;
    return node;
  }

  function details(summary, body) {
    return (
      "<details><summary>" +
      escapeHtml(summary) +
      "</summary><pre>" +
      escapeHtml(body) +
      "</pre></details>"
    );
  }

  // A note's leading glyph is the controller's own verdict alphabet (the ✓/✗ a
  // call result carries, the ? an ask_user asks with, the ! of a warning), so
  // the line is coloured from it rather than from a second event field: one
  // vocabulary, decided above both shells.
  function noteClass(text) {
    var head = String(text || "").charAt(0);
    if (head === "✓") return "ev-note ok";
    if (head === "✗") return "ev-note bad";
    if (head === "?") return "ev-note ask";
    if (head === "!") return "ev-note warn";
    return "ev-note";
  }

  // `x` toggles the MOST RECENT collapsible, independent of focus - the TUI's
  // deliberate "what did that command print" shortcut (main-chat.md section 6).
  function toggleLastBlock() {
    // In the transcript the user is LOOKING at: the shortcut means "what did
    // that command print", and that question is always about what is on screen.
    var blocks = panel(selectedWindow).node.querySelectorAll("details");
    if (!blocks.length) return;
    var last = blocks[blocks.length - 1];
    last.open = !last.open;
    if (last.open) last.scrollIntoView({ block: "nearest" });
  }

  function addTranscript(event) {
    // Every transcript event names its window, so output keeps landing in the
    // right panel while the user reads another one - the specific bug class the
    // TUI's docstrings call out ("looks exactly like data loss").
    var win = event.window || MASTER_WINDOW;
    // The reply brackets carry no content: they arm the mark and cash it in.
    if (event.kind === "reply_start") {
      var opening = panel(win);
      opening.replyStart = null;
      opening.awaitingReplyStart = true;
      return;
    }
    if (event.kind === "reply_reveal") {
      revealReply(win);
      return;
    }
    // Three kinds render as markdown: what the user typed, what the model SAID
    // to them (a ===CLIP:SAY block, which is why it is markdown at all), and
    // the loose text a model left outside its blocks. Only the class differs -
    // a SAY is the model addressing the user, prose is an aside.
    if (event.kind === "user" || event.kind === "say" || event.kind === "prose") {
      var label = event.label || (event.kind === "user" ? "you" : "assistant");
      append(
        win,
        block(
          "ev ev-" + event.kind,
          '<div class="ev-head">' + escapeHtml(label) + "</div>" + renderMarkdown(event.text)
        ),
        true
      );
      return;
    }
    if (event.kind === "call") {
      var html = '<div class="ev-summary">' + escapeHtml(event.summary) + "</div>";
      if (event.raw) {
        html += details("raw block (" + event.raw.split("\n").length + " lines)", event.raw);
      }
      append(win, block("ev ev-call", html), false);
      return;
    }
    if (event.kind === "outbound") {
      var note = escapeHtml(event.note);
      if (event.parts > 1) note += " · part 1 of " + event.parts;
      append(
        win,
        block(
          "ev ev-call",
          '<div class="ev-summary">' +
            note +
            "</div>" +
            // ``size`` arrives already rendered ("~4.2k tokens"): the divisor
            // behind it is configuration, and this page never learns it.
            details("outbound turn " + event.turn + " (" + event.size + ")", event.payload)
        ),
        false
      );
      return;
    }
    if (event.kind === "error") {
      append(win, block("ev ev-error", escapeHtml(event.text)), false);
      return;
    }
    // A sub-run divider is a note like any other; it gets its own rule so the
    // eye finds where one delegated task ended and the next began.
    var cls = /^── task: /.test(String(event.text || "")) ? "ev-divider" : noteClass(event.text);
    append(win, block("ev " + cls, escapeHtml(event.text)), false);
  }

  /* == window tabs ==========================================================
     Rendered, never decided: the label (glyph included), the state and which
     window is selected all arrive composed, because "how did the last run in
     this window go" is one rule and there must not be two copies of it. */

  function tabNode(tab) {
    var node = document.createElement("button");
    node.type = "button";
    node.className = "win-tab " + (tab.state || "none");
    node.id = "win-" + tab.window;
    node.textContent = tab.label;
    if (tab.window === selectedWindow) node.classList.add("selected");
    node.addEventListener("click", function () {
      // Fired even for the tab that is already selected: after the controller
      // moved the view mid-delegation, "click the tab I am on" is how the user
      // says "show me this window" (tabs-delegation-summary.md 6).
      api("window", tab.window);
    });
    return node;
  }

  // What each window is CALLED, learned from the tabs event so the sidebar's
  // two per-window headings can name the window they configure without a
  // second copy of the vocabulary.
  var windowNames = {};

  function windowName(win) {
    return windowNames[win] || "";
  }

  function paintTabs(event) {
    selectedWindow = event.selected || MASTER_WINDOW;
    [["masters", el.winRowMaster], ["subs", el.winRowSub]].forEach(function (pair) {
      pair[1].innerHTML = "";
      (event[pair[0]] || []).forEach(function (tab) {
        windowNames[tab.window] = tab.name;
        var node = tabNode(tab);
        // Where new output is going, when that is not where the user is
        // looking - the one moment the two pointers visibly disagree.
        if (tab.window === event.focused && tab.window !== selectedWindow) {
          node.classList.add("writing");
          node.title = "output is landing here";
        }
        pair[1].appendChild(node);
      });
    });
    showPanel(selectedWindow);
  }

  function showPanel(win) {
    Object.keys(panels).forEach(function (key) {
      var target = panels[key];
      target.node.hidden = key !== win;
    });
    var shown = panel(win);
    // A panel that was written into while hidden has no measured scroll, so
    // following it is re-established the moment it is on screen again.
    if (shown.stick) shown.node.scrollTop = shown.node.scrollHeight;
  }

  /* == run panel ===========================================================
     One row per planned call, in call-id order, glyph-coded with the same
     ✓ ✗ ▶ • − alphabet the queue strip uses. No row windowing: the TUI's
     _MAX_ROWS = 8 is terminal real estate (the composer must not be pushed off
     screen), and a scrollable panel has no such problem - main-chat.md
     section 7 says so explicitly. */

  var GLYPH_CLASS = {
    "✓": "ok",
    "✗": "bad",
    "▶": "running",
    "•": "pending",
    "−": "skipped"
  };

  function runOrder() {
    return Object.keys(runRows).sort(function (a, b) {
      return Number(a) - Number(b);
    });
  }

  function paintRunRows() {
    el.runRows.innerHTML = "";
    runOrder().forEach(function (key) {
      var row = runRows[key];
      var li = document.createElement("li");
      li.className = "run-row " + (GLYPH_CLASS[row.glyph] || "pending");
      var html =
        '<span class="rg">' +
        escapeHtml(row.glyph || "•") +
        '</span><span class="rid">' +
        escapeHtml(row.call_id) +
        '</span><span class="rtool">' +
        escapeHtml(row.tool || "") +
        '</span><span class="rdetail">' +
        escapeHtml(row.detail || "") +
        "</span>";
      // The hint rides only the row it applies to: the running call that can
      // actually produce output. Expanding a write_file row would open an empty
      // pane and teach the user the key does nothing.
      if (row.streams && row.call_id === streamingCall && !tailOpen) {
        html += '<span class="rhint">ctrl+o output</span>';
      }
      li.innerHTML = html;
      el.runRows.appendChild(li);
    });
  }

  function paintRunTail() {
    el.runTailWrap.hidden = !tailOpen;
    if (!tailOpen) return;
    var lines = (streamingCall !== null ? runOutput[streamingCall] : null) || [];
    // A trailing partial line is a real line; an empty last element (the chunk
    // ended on a newline) is not. Dropped BEFORE the slice, so the pane shows
    // TAIL_LINES of output rather than eleven and a blank.
    if (lines.length && lines[lines.length - 1] === "") lines = lines.slice(0, -1);
    var shown = lines.slice(-TAIL_LINES);
    el.runTail.textContent = shown.length ? shown.join("\n") : "(no output yet)";
    el.runTail.scrollTop = el.runTail.scrollHeight;
  }

  // Did the click that is being handled right now finish a text selection?
  // A drag ending inside an element still fires `click` on it, so any handler
  // that turns a whole panel of readable text into one big button has to ask
  // this first or copying out of that panel is impossible.
  function draggedOutText() {
    var sel = window.getSelection && window.getSelection();
    return !!(sel && !sel.isCollapsed && String(sel).length);
  }

  function toggleRunOutput() {
    // Opening on a non-streaming row is a silent no-op, exactly as
    // RunPanel.toggle_output is; closing always works.
    if (!tailOpen && streamingCall === null) return;
    tailOpen = !tailOpen;
    paintRunTail();
    paintRunRows();
  }

  function endStreaming(callId) {
    if (streamingCall !== callId) return;
    // The tail belonged to the command that just ended: the next one starts
    // from a fresh, collapsed pane rather than inheriting its predecessor's.
    streamingCall = null;
    tailOpen = false;
  }

  /* == gate ================================================================
     The diff is coloured by hand, line by line, because a highlighter would be
     a library and this page has none (docs/design/gui.md section 2). HTML is
     escaped FIRST and the classes are added around the escaped text, so no
     model-authored line can inject markup by looking like a tag. */

  function diffClass(line) {
    // Order matters: "+++"/"---" are file headers, not an addition and a
    // deletion, and they lose to the single-character test if tested after it.
    if (/^@@/.test(line)) return "d-hunk";
    if (/^(\+\+\+|---|diff |index )/.test(line)) return "d-file";
    if (line.charAt(0) === "+") return "d-add";
    if (line.charAt(0) === "-") return "d-del";
    if (line.charAt(0) === "\\") return "d-meta";
    return "d-ctx";
  }

  function renderDiff(text) {
    return String(text)
      .replace(/\r\n?/g, "\n")
      .split("\n")
      .map(function (line) {
        return (
          '<div class="' + diffClass(line) + '">' + (escapeHtml(line) || "&nbsp;") + "</div>"
        );
      })
      .join("");
  }

  // A brand-new file has no diff to read - it is all addition - so it gets the
  // banner the TUI paints and numbered lines instead of a wall of "+".
  function renderNewFile(head, body) {
    var lines = String(body)
      .replace(/\r\n?/g, "\n")
      .split("\n")
      .map(function (line) {
        return "<li>" + (escapeHtml(line) || "&nbsp;") + "</li>";
      })
      .join("");
    return (
      '<div class="new-file">' +
      escapeHtml(head) +
      '</div><ol class="code">' +
      lines +
      "</ol>"
    );
  }

  function renderPreview(event) {
    var kind = event.preview_kind || "text";
    var html = "";
    if (kind === "diff") return '<div class="diff">' + renderDiff(event.preview_body) + "</div>";
    if (kind === "new_file") return renderNewFile(event.preview_head, event.preview_body);
    if (kind === "command" || kind === "mcp") {
      html += '<div class="cmd">' + escapeHtml(event.preview_head) + "</div>";
      if (event.reason) html += '<div class="reason">' + escapeHtml(event.reason) + "</div>";
      if (kind === "mcp" && event.preview_body) {
        html +=
          '<div class="args-label">args:</div><pre class="args">' +
          escapeHtml(event.preview_body) +
          "</pre>";
      }
      if (event.note) html += '<div class="why">' + escapeHtml(event.note) + "</div>";
      if (event.timeout) {
        html += '<div class="why">timeout: ' + escapeHtml(event.timeout) + "s</div>";
      }
      return html;
    }
    var body = event.preview_body || event.preview || event.auto_reason || "(no preview)";
    return "<pre class='plain'>" + escapeHtml(body) + "</pre>";
  }

  // The queue strip: the controller pre-renders it as "✓1 read_file  ▶2 mcp",
  // joined with exactly two spaces (SessionController._queue_strip), so the
  // split is exact rather than a guess - and each entry becomes a chip coloured
  // by the same glyph alphabet the run rows use.
  function paintQueue(queue) {
    el.gateQueue.innerHTML = "";
    var entries = String(queue || "")
      .split(/ {2,}/)
      .filter(function (entry) {
        return entry.length > 0;
      });
    el.gateQueue.hidden = entries.length === 0;
    entries.forEach(function (entry) {
      var chip = document.createElement("span");
      chip.className = "qchip " + (GLYPH_CLASS[entry.charAt(0)] || "pending");
      chip.textContent = entry;
      el.gateQueue.appendChild(chip);
    });
  }

  function showGate(event) {
    gateOpen = true;
    gateAlwaysOffered = Boolean(event.always_label);
    el.gateTitle.textContent = event.title;
    paintQueue(event.queue);
    el.gatePreview.innerHTML = renderPreview(event);
    el.gatePreview.scrollTop = 0;
    el.gateAlways.hidden = !gateAlwaysOffered;
    if (gateAlwaysOffered) el.gateAlways.textContent = event.always_label + " (a)";
    el.gateHint.textContent = event.hint || "press y to approve · n to reject";
    closeReject();
    el.gate.hidden = false;
    paintKeyHints(); // y/n/a come out of their dim
  }

  function hideGate() {
    gateOpen = false;
    gateAlwaysOffered = false;
    closeReject();
    el.gate.hidden = true;
    paintKeyHints();
  }

  function openReject() {
    if (!gateOpen) return;
    rejectOpen = true;
    el.gateRejectRow.hidden = false;
    el.gateNote.focus();
  }

  function closeReject() {
    rejectOpen = false;
    el.gateRejectRow.hidden = true;
    el.gateNote.value = "";
  }

  function sendReject() {
    // Enter confirms even when the box is empty - a rejection with no note is
    // still a rejection, and making the user type one would be a tax on saying
    // no (main-chat.md section 5).
    var note = el.gateNote.value;
    closeReject();
    api("decide", "reject", note);
  }

  /* == the slash-command popup =============================================
     agentclip/shell/tui/widgets/command_popup.py, in a div. The registry itself
     arrives from Python (the `commands` event) so a command added there cannot
     go missing here; what this file owns is when the list is up, which row is
     lit and what completing one writes - and none of that may take focus away
     from the composer, which is why the popup is a block of divs with nothing
     clickable in it and is driven entirely from the textarea's own events. */

  // app/commands.py:match_prefix, rule for rule. A line qualifies only while it
  // is a single bare token behind ONE slash, and each of the three ways it stops
  // being a command in progress closes the list on its own: "//" is the
  // literal-slash escape hatch, any whitespace means the command is chosen and
  // its argument is being typed, and no match at all is no list.
  function matchPrefix(text) {
    var line = String(text || "");
    if (line.charAt(0) !== "/" || line.slice(0, 2) === "//") return [];
    var token = line.slice(1);
    if (/\s/.test(token)) return [];
    var prefix = token.toLowerCase();
    return commands.filter(function (command) {
      return command.name.indexOf(prefix) === 0;
    });
  }

  function sameMatches(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  // The one place visibility is decided, called from every event that can change
  // the box: typing, pasting, completing, a mode change, a reset.
  function syncPopup() {
    if (composerMode === "answer" || el.composer.disabled) {
      popupHide();
      return;
    }
    var matches = matchPrefix(el.composer.value);
    if (!matches.length) {
      popupHide();
      return;
    }
    // The highlight is re-decided only when the LIST actually changed, so a
    // redundant sync cannot throw away the user's arrow presses. And nothing is
    // preselected until at least one letter has been typed - "/" alone offers
    // the whole registry with no row lit, which is the second lock on the door
    // /yolo sits behind (commands.py's ordering note).
    if (!sameMatches(matches, popupMatches)) {
      popupMatches = matches;
      popupIndex = el.composer.value.length > 1 ? 0 : null;
    }
    popupPaint();
    el.popup.hidden = false;
  }

  function popupPaint() {
    var html = "";
    for (var i = 0; i < popupMatches.length; i += 1) {
      html +=
        '<div class="cmd-row' +
        (i === popupIndex ? " on" : "") +
        '"><span class="cmd-name">' +
        escapeHtml(popupMatches[i].label) +
        '</span><span class="cmd-why">' +
        escapeHtml(popupMatches[i].summary) +
        "</span></div>";
    }
    el.popup.innerHTML = html;
    var lit = el.popup.querySelector(".cmd-row.on");
    if (lit) lit.scrollIntoView({ block: "nearest" });
  }

  function popupOpen() {
    return popupMatches.length > 0 && !el.popup.hidden;
  }

  function popupHide() {
    popupMatches = [];
    popupIndex = null;
    el.popup.hidden = true;
    el.popup.innerHTML = "";
  }

  // From no highlight an arrow ARMS the list: down lands on the first row, up on
  // the last - which is what wrapping would have done from either edge anyway.
  function popupMove(delta) {
    if (!popupOpen()) return;
    if (popupIndex === null) popupIndex = delta > 0 ? 0 : popupMatches.length - 1;
    else popupIndex = (popupIndex + delta + popupMatches.length) % popupMatches.length;
    popupPaint();
  }

  // Enter and Tab do the same thing, and both are swallowed even when there is
  // nothing highlighted: a bare "/" plus Enter must not send a lone slash to the
  // model, it must sit there waiting to be narrowed.
  function popupComplete() {
    if (popupIndex === null) return;
    // The canonical name plus ONE trailing space - never the argument hint. The
    // space is load-bearing twice over: it is where the argument gets typed, and
    // it is what makes match_prefix return nothing, so the list closes itself and
    // the next Enter is a plain send.
    el.composer.value = "/" + popupMatches[popupIndex].name + " ";
    el.composer.selectionStart = el.composer.selectionEnd = el.composer.value.length;
    popupHide();
  }

  /* == toasts ============================================================== */

  function toast(event) {
    var node = document.createElement("div");
    node.className = "toast " + (event.severity || "information");
    node.textContent = (event.title ? event.title + ": " : "") + event.message;
    el.toasts.appendChild(node);
    var life = (event.timeout || (event.severity === "error" ? 10 : 5)) * 1000;
    window.setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, life);
  }

  /* == modals ============================================================== */

  function button(label, value) {
    var node = document.createElement("button");
    node.type = "button";
    node.textContent = label;
    node.addEventListener("click", function () {
      answer(value);
    });
    return node;
  }

  function answer(value) {
    var target = modalId;
    modalId = null;
    modalKind = null;
    el.scrim.hidden = true;
    if (target) api("prompt", target, value);
  }

  function showModal(event) {
    // A controller flow is parked on this answer, so it takes the element off
    // whatever page screen was using it - the help sheet is not worth stranding
    // a turn behind.
    pageModal = null;
    el.modal.classList.remove("wide");
    modalId = event.prompt_id;
    modalKind = event.modal;
    el.modalTitle.textContent = event.title || "";
    el.modalBody.innerHTML = "";
    el.modalActions.innerHTML = "";
    el.modalHint.textContent = event.hint || "";
    el.modalHint.hidden = !event.hint;
    if (event.modal === "confirm") {
      el.modalBody.appendChild(block("modal-text", escapeHtml(event.body || "")));
      el.modalActions.appendChild(button("Yes", true));
      el.modalActions.appendChild(button("No", false));
    } else if (event.modal === "text") {
      el.modalBody.appendChild(block("modal-text", escapeHtml(event.hint || "")));
      var input = document.createElement("input");
      input.type = "text";
      input.id = "modal-input";
      el.modalBody.appendChild(input);
      var ok = document.createElement("button");
      ok.type = "button";
      ok.textContent = "OK";
      ok.addEventListener("click", function () {
        answer(input.value);
      });
      el.modalActions.appendChild(ok);
      el.modalActions.appendChild(button("Cancel", null));
      window.setTimeout(function () {
        input.focus();
      }, 0);
    } else if (event.modal === "connect_password") {
      // Mid-checklist, when agent/key auth was refused. Three of these happen
      // at most and the hint says which one this is, because a wrong password
      // must not feel like an unbounded loop (ssh-connect.md 3.5). Cancel
      // returns null, which is `ask_password`'s own "give up" signal - never an
      // exception, so _authenticate falls through to its ordinary SshError.
      var secret = document.createElement("input");
      secret.type = "password";
      secret.id = "modal-input";
      el.modalBody.appendChild(secret);
      var go = document.createElement("button");
      go.type = "button";
      go.textContent = "Sign in";
      go.addEventListener("click", function () {
        answer(secret.value);
      });
      secret.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          answer(secret.value);
        }
      });
      el.modalActions.appendChild(go);
      el.modalActions.appendChild(button("Cancel", null));
      window.setTimeout(function () {
        secret.focus();
      }, 0);
    } else if (event.modal === "connect_hostkey") {
      // OpenSSH's own question, in OpenSSH's own words and with the SHA256
      // fingerprint in the format `ssh-keygen -lf` prints - so a user checking
      // it against another channel is comparing like with like. There is no
      // "always trust": declining raises out of connect(), and nothing is
      // written to known_hosts unless the answer is yes (3.6).
      el.modalBody.appendChild(block("modal-text", escapeHtml(event.body || "")));
      el.modalActions.appendChild(button("Yes, connect", true));
      el.modalActions.appendChild(button("No", false));
    } else if (event.modal === "connect_keyboard") {
      // Keyboard-interactive/2FA: the server's own instructions verbatim, then
      // one field per prompt tuple, masked where it said echo=false. Submitting
      // sends every answer in order, which is paramiko's handler contract.
      if (event.body) {
        el.modalBody.appendChild(block("modal-text", escapeHtml(event.body)));
      }
      var fields = (event.fields || []).map(function (field) {
        var label = document.createElement("div");
        label.className = "modal-text";
        label.textContent = field.prompt;
        var box = document.createElement("input");
        box.type = field.echo ? "text" : "password";
        el.modalBody.appendChild(label);
        el.modalBody.appendChild(box);
        return box;
      });
      var submit = document.createElement("button");
      submit.type = "button";
      submit.textContent = "Submit";
      submit.addEventListener("click", function () {
        answer(
          fields.map(function (box) {
            return box.value;
          })
        );
      });
      el.modalActions.appendChild(submit);
      el.modalActions.appendChild(button("Cancel", null));
      window.setTimeout(function () {
        if (fields.length) fields[0].focus();
      }, 0);
    } else if (event.modal === "summary") {
      var rows = (event.rows || [])
        .map(function (pair) {
          return "<tr><th>" + escapeHtml(pair[0]) + "</th><td>" + escapeHtml(pair[1]) + "</td></tr>";
        })
        .join("");
      // The model's own task_done text, or the placeholder saying there was
      // none - a blank panel under the table reads as a rendering failure.
      el.modalBody.innerHTML =
        "<table class='stats'>" +
        rows +
        "</table>" +
        renderMarkdown(event.summary || event.placeholder || "");
      el.modalActions.appendChild(button("Undo last turn (u)", "undo"));
      el.modalActions.appendChild(button("New session (t)", "new"));
      // Export is a LOOP, not an exit: the controller writes the log and
      // re-opens this screen, which is why it sits with the other two rather
      // than being the last thing you do.
      el.modalActions.appendChild(button("Export chat log (l)", "export"));
      el.modalActions.appendChild(button("Close (esc)", "close"));
    }
    el.scrim.hidden = false;
  }

  // The summary's single-letter keys. A terminal convention rather than a
  // contract (the buttons are this shell's real affordance), kept because a
  // modal that answered only to a mouse would be the one screen here that did.
  function summaryKey(key) {
    if (key === "u") return "undo";
    if (key === "t") return "new";
    if (key === "l") return "export";
    if (key === "Escape") return "close";
    return null;
  }

  // Is anything at all on the scrim? Both kinds count: a page screen owns the
  // keyboard exactly as a prompt does, which is the "a modal owns all key input
  // while it is open" rule Textual's screen stack gives the TUI for free.
  function modalUp() {
    return modalId !== null || pageModal !== null;
  }

  // The frame every page-owned screen opens with: title, a Close button, and the
  // one element they all share.
  function openPageModal(kind, title, wide) {
    if (modalId) return false; // a parked flow wins; see showModal
    pageModal = kind;
    modalKind = null;
    el.modal.classList.toggle("wide", Boolean(wide));
    // The guide is the one rider that reshapes the modal itself (a column with
    // a switcher that stays put while the page under it scrolls), so the class
    // is set HERE - where every other screen also takes it off again.
    el.modal.classList.toggle("docs", kind === "docs");
    el.modalTitle.textContent = title;
    el.modalBody.innerHTML = "";
    el.modalActions.innerHTML = "";
    el.modalHint.hidden = true;
    el.modalHint.textContent = "";
    el.scrim.hidden = false;
    return true;
  }

  function closePageModal() {
    if (!pageModal) return;
    pageModal = null;
    el.modal.classList.remove("wide");
    el.modal.classList.remove("docs");
    el.scrim.hidden = true;
  }

  function pageModalClose(label) {
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = label;
    close.addEventListener("click", closePageModal);
    el.modalActions.appendChild(close);
  }

  function showPayload(event) {
    // park_off_clipboard's GUI equivalent: the clipboard provider refused the
    // payload and this shell has no OSC-52, so the text is put somewhere the
    // user can select it (docs/design/gui.md 2).
    if (!openPageModal("payload", "Copy this payload by hand", false)) return;
    el.modalBody.innerHTML = "<pre class='payload'>" + escapeHtml(event.text) + "</pre>";
    pageModalClose("Close");
  }

  /* == help (F1) and settings (F4) ==========================================
     Two page-owned screens over the one modal element. What they show comes
     from exactly two places and neither is prose typed twice: the KEY table is
     rendered from KEYS - the same array the keydown handler dispatches from,
     which is what makes "the help documents a key that moved" structurally
     impossible here - and the COMMAND table is rendered from the `commands`
     event, i.e. from agentclip.shell.app.commands.COMMANDS, the registry the
     controller's dispatch and `/help` read. The remaining prose is about the
     chat box and the loop rather than about any binding, and is spelled here
     the way index.html spells the sidebar's headings. */

  // Prose blocks, in the order tui/screens/help.py puts them. Each is a heading
  // and a paragraph; the tables slot in between by section name.
  var HELP_PROSE = [
    [
      "Chat box (bottom of the window)",
      "Type a message and press Enter to send it. Shift+Enter inserts a newline, " +
        "and so does Ctrl+J (the TUI's newline key, honored here too). " +
        "Up/Down walk back through what you have already sent this " +
        "run, newest first, and Down past the newest hands back whatever you " +
        "were half-way through typing - but only from the FIRST/LAST line of " +
        "the box, so in a multi-line message they still move the caret, and the " +
        "command list gets them first while it is up. Esc clears the box and " +
        "keeps the caret there; Esc on an EMPTY box lets go of it, which is " +
        "what frees the single-key shortcuts below - press t to type again."
    ],
    [
      "When the model asks you a question",
      "An ANSWER NEEDED banner sits above the chat box with the question in it, " +
        "and it stays there until you deal with it. Whatever you type is the " +
        "answer VERBATIM - a leading slash is text, not a command. If you do not " +
        "want to answer, Esc cancels the question: once to let go of the box, " +
        "once more to cancel. That is not an abort - the model is told you " +
        "declined and the turn carries on."
    ],
    [
      "Chat commands (type in the chat box, leading slash)",
      'Typing "/" pops the list up above the box and each further character ' +
        "narrows it. Nothing is highlighted until you press a letter or an " +
        "arrow - then Enter (or Tab) COMPLETES the highlighted row and the next " +
        'Enter sends it. Esc closes the list without touching your text. "//text" ' +
        "sends a literal leading slash."
    ],
    [
      "Window tabs (top of the chat column) - a tab is a BROWSER WINDOW",
      "Row 1 is the master window, the chat you steer. Row 2 is the sub-agent " +
        "window a delegated sub-task runs in. Each carries its own service, its " +
        "own drawn rectangle and its own transcript. Selecting a tab only " +
        "changes what YOU see and what the sidebar configures - never where " +
        "output lands, and never which window the automation is driving."
    ],
    [
      "Permission mode (bottom-left of the status bar)",
      "shift+tab cycles it: build -> plan -> build. build is the default " +
        "builder - your permission rules decide, and anything they do not cover " +
        "asks you first; plan REFUSES every edit, command, MCP call and " +
        "delegation, so the model can only read and propose. The key works " +
        "while you are typing and while a turn is running. Which rules decide " +
        "is a file: /config says where it lives, /config global|local reset " +
        "puts the shipped rules back."
    ],
    [
      "Walking away from the PC (/unattended on)",
      "Everything that would have opened a gate is auto-denied instead, so you " +
        "come back to a finished turn rather than a question nobody answered. " +
        "The status bar carries an UNATTENDED badge for as long as it is on. It " +
        "is the opposite of /yolo, and if both are on YOLO wins: it approves " +
        "what this would have refused."
    ],
    [
      "Sub-agents (only once the sub-agent window is calibrated in the Monitor UI)",
      "The model can hand one bounded sub-task to a fresh sub-agent in its own " +
        "chat. That tab shows the run's state and the status bar goes magenta - " +
        "you still approve every edit and command. ctrl+x cancels the calls " +
        "running right now; /abort ends the whole run."
    ]
  ];

  // The last word, after the key tables: what the whole thing is FOR.
  var HELP_CODA = [
    "The loop",
    "AgentClip copies a payload - paste it into your chat and send. Click the " +
      "reply's Copy button; AgentClip detects it, shows what is running, gates " +
      "edits and commands, then copies the combined results - paste them back. " +
      "Repeat until the model sends task_done."
  ];

  function helpTable(rows) {
    var html = "<table class='help-rows'>";
    rows.forEach(function (row) {
      html +=
        "<tr><td class='k" +
        (row.cmd ? " cmd" : "") +
        "'>" +
        escapeHtml(row.keys) +
        "</td><td class='d'>" +
        escapeHtml(row.what) +
        "</td></tr>";
    });
    return html + "</table>";
  }

  function helpSection(head, note, rows) {
    var html = "<div class='help-section'><div class='help-head'>" + escapeHtml(head) + "</div>";
    if (note) html += "<p class='help-note'>" + escapeHtml(note) + "</p>";
    if (rows && rows.length) html += helpTable(rows);
    return html + "</div>";
  }

  function openHelp() {
    if (!openPageModal("help", "AGENTCLIP HELP", true)) return;
    var html = "";
    HELP_PROSE.forEach(function (pair) {
      // The commands section is the one whose body is DATA: the registry rows
      // go under its paragraph rather than being written out here.
      var rows = null;
      if (pair[0].indexOf("Chat commands") === 0) {
        rows = commands.map(function (command) {
          return { keys: command.label, what: command.summary, cmd: true };
        });
      }
      html += helpSection(pair[0], pair[1], rows);
    });
    // ...and every key section, rendered from the very table the handler reads.
    KEY_SECTIONS.forEach(function (section) {
      var rows = KEYS.filter(function (entry) {
        return entry.section === section;
      }).map(function (entry) {
        return { keys: entry.keys.join(" / "), what: entry.what };
      });
      if (rows.length) html += helpSection(section, "", rows);
    });
    html += helpSection(HELP_CODA[0], HELP_CODA[1], null);
    el.modalBody.innerHTML = html;
    el.modalHint.textContent = "escape (or F1) closes";
    el.modalHint.hidden = false;
    // The way ON from the cheatsheet to the manual. This sheet is drawn from
    // what the page HAS (its own key table, the command registry); the guide is
    // the prose about what any of it is for, and the two are one press apart in
    // both directions - the titlebar's button is the other door.
    var guide = document.createElement("button");
    guide.type = "button";
    guide.textContent = "User guide";
    guide.addEventListener("click", function () {
      openDocs("");
    });
    el.modalActions.appendChild(guide);
    pageModalClose("Close (esc)");
  }

  /* == the user guide (the titlebar's "docs" button) ========================
     docs/commands.md and docs/configuration.md, rendered in the page. The FILES
     are the source of truth and nothing here holds a copy of a word of them:
     Python reads them off disk (chat/docs.py) and pushes them whole on the
     `docs` event, exactly as the command registry crosses, and the markdown
     renderer at the top of this file turns them into the modal's body.

     Two pages, one surface: a switcher rather than two buttons in the
     titlebar, because "the manual" is one thing to open and which page you
     wanted is a question you answer after it is up. */

  function docPage(name) {
    var wanted = null;
    docPages.forEach(function (page) {
      if (page.name === name) wanted = page;
    });
    return wanted || docPages[0] || null;
  }

  function openDocs(name) {
    if (!openPageModal("docs", "USER GUIDE", true)) return;
    var tabs = document.createElement("div");
    tabs.className = "doc-tabs";
    docPages.forEach(function (page) {
      var tab = document.createElement("button");
      tab.type = "button";
      tab.className = "doc-tab";
      tab.setAttribute("data-doc", page.name);
      tab.textContent = page.title;
      tab.addEventListener("click", function () {
        showDoc(page.name);
      });
      tabs.appendChild(tab);
    });
    var body = document.createElement("div");
    body.className = "doc-body";
    // A cross-reference between the two pages is answered HERE rather than by
    // an <a href>: this page is a file:// URL with nowhere to navigate to, so
    // the only honest thing a link can do is move the switcher.
    body.addEventListener("click", function (ev) {
      var link = ev.target && ev.target.closest ? ev.target.closest("a[data-doc]") : null;
      if (!link) return;
      ev.preventDefault();
      showDoc(link.getAttribute("data-doc"));
    });
    el.modalBody.appendChild(tabs);
    el.modalBody.appendChild(body);
    el.modalHint.textContent = "escape closes · these pages are docs/*.md in the repository";
    el.modalHint.hidden = false;
    pageModalClose("Close (esc)");
    showDoc(name || docCurrent);
  }

  function showDoc(name) {
    var body = el.modalBody.querySelector(".doc-body");
    if (!body) return;
    var page = docPage(name);
    if (!page) {
      // Only reachable before the first `docs` event, i.e. a window whose page
      // came up before Python's first push. Says so rather than showing an
      // empty box.
      body.innerHTML = "<p>The guide has not arrived from the app yet.</p>";
      return;
    }
    docCurrent = page.name;
    Array.prototype.forEach.call(el.modalBody.querySelectorAll(".doc-tab"), function (tab) {
      tab.classList.toggle("on", tab.getAttribute("data-doc") === page.name);
    });
    body.innerHTML = renderMarkdown(page.text);
    // A switch is a new document, not a scroll position in an old one.
    body.scrollTop = 0;
  }

  function openSettings() {
    if (!openPageModal("settings", "SETTINGS", false)) return;
    var head = document.createElement("div");
    head.className = "help-head";
    head.textContent = "Appearance";
    el.modalBody.appendChild(head);
    themeChoices.forEach(function (choice) {
      var row = document.createElement("label");
      row.className = "set-choice";
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "gui-theme";
      input.value = choice.value;
      input.checked = choice.value === theme;
      // Live: picking one wears it at once and saves it at once. There is no
      // Save button and so nothing to revert on escape - the TUI stages the
      // write behind Save because its preview is an app-wide reactive; a class
      // on <body> costs nothing to try and the user picked what they picked.
      input.addEventListener("change", function () {
        applyTheme(choice.value);
        api("theme", choice.value);
      });
      row.appendChild(input);
      row.appendChild(document.createTextNode(choice.label));
      el.modalBody.appendChild(row);
    });
    el.modalHint.textContent = "picking a theme applies and saves it · escape closes";
    el.modalHint.hidden = false;
    pageModalClose("Close (esc)");
  }

  /* Wear a palette. One class on <body> per theme, and the DEFAULT wears none
     at all - `:root` is already that palette, so "no class" and "theme-dark"
     would be two spellings of one thing and the CSS would have to carry both.

     An unknown name falls back rather than throwing: this runs on an event from
     Python, and a page that refuses to paint because a config file has a typo in
     it is worse than a page in the default palette. Python rejects the same name
     one layer up (`GuiView._persist_theme`), so this is the second net. */
  function applyTheme(name) {
    var known = themeChoices.length
      ? themeChoices.map(function (choice) {
          return choice.value;
        })
      : THEME_NAMES;
    theme = known.indexOf(name) === -1 ? THEME_DEFAULT : name;
    // Only the theme classes go: `yolo` is a body class too and is nobody's
    // business here. Collected before removing, because a live DOMTokenList
    // shifts under an index that is deleting out of it.
    var stale = [];
    for (var i = 0; i < document.body.classList.length; i++) {
      if (document.body.classList[i].indexOf("theme-") === 0) {
        stale.push(document.body.classList[i]);
      }
    }
    stale.forEach(function (cls) {
      document.body.classList.remove(cls);
    });
    if (theme !== THEME_DEFAULT) document.body.classList.add("theme-" + theme);
  }

  /* == sidebar ==============================================================
     Everything below RENDERS; nothing decides. The STATE rail's brightness came
     out of LOOP_TRANSITIONS on the Python side, the status bar's segments came
     composed and in order, and the detection lines came worded - which is what
     keeps the two shells from growing two ideas of what any of them mean
     (docs/design/ui-briefs/sidebar-status-log.md section 3). */

  /* The titlebar's MONITOR badge. Three states from Python (view.py:
     _push_link); the words are the big ones and the fill is the state, so
     "connected or not" is answered before the address is read. Since
     ui-monitor.md 10.2 there is no "this PC" state: watching this machine's
     screen is a link to a monitor PROCESS like any other, whose peer is simply
     called "local". `none` is the new one, and it is RED - a Chat UI with no
     monitor can drive nothing, and the fix is one click away on the Monitor
     tab. */
  function paintMonitorLink(event) {
    var state = event.state || "none";
    el.monitorLink.hidden = false;
    el.monitorLink.className = "pill" + (state === "up" ? " ok" : " warn");
    if (state === "up") {
      el.monitorLink.textContent = "MONITOR CONNECTED · " + event.peer;
    } else if (state === "down") {
      el.monitorLink.textContent =
        "MONITOR DOWN · " + event.peer + " · " + (event.reason || "");
    } else {
      el.monitorLink.textContent = "NO MONITOR · attach or launch one";
    }
    el.monitorLink.title = event.reason || "";
  }

  function paintRail(event) {
    el.loop.textContent = event.loop;
    el.rail.innerHTML = "";
    (event.rows || []).forEach(function (row) {
      var li = document.createElement("li");
      li.className = row.mark === "dim" ? "" : row.mark;
      li.textContent = (row.mark === "active" ? "▶ " : "  ") + row.label;
      el.rail.appendChild(li);
    });
  }

  function paintSidebar(event) {
    el.sideRoot.textContent = event.project || "";
    // The PROJECT block's standing remote marker (gui.md 4, ruling 6) and the
    // two buttons that belong with it. Both are absent - not disabled - when
    // they would mean nothing: no remote session, or a build with no way to go
    // remote at all.
    var lines = event.remote_lines || [];
    el.sideRemote.hidden = lines.length === 0;
    el.sideRemote.classList.toggle("side-remote-lost", (lines[1] || "").indexOf("lost") === 0);
    el.sideRemoteLines.innerHTML = "";
    lines.forEach(function (line) {
      var node = document.createElement("div");
      node.textContent = line;
      el.sideRemoteLines.appendChild(node);
    });
    // "Connect to remote..." doubles as the brief's "reconnect to a different
    // target": there is no separate switch action, because connecting IS a new
    // session (remote-ssh.md decision 4).
    el.connectRemote.hidden = !event.can_connect;
    el.connectRemote.textContent = event.remote
      ? "Connect to another machine..."
      : "Connect to remote...";
    el.sideServiceLabel.textContent = event.service_label || "";
    el.sideProfileNote.textContent = event.profile_note || "";
    el.sideRegion.textContent = event.region || "";
    el.sideSlotNote.textContent = event.slot_note || "";
    el.sideDetectionTitle.textContent = event.detection_title || "DETECTION";
    // Both per-window blocks say which window they are editing: the sidebar
    // sits a long way from the tab bar in a 1200px window, and "which chat is
    // this picker about" must not be a question you answer by looking up.
    var win = windowName(event.window);
    el.sideServiceTitle.textContent = win ? "SERVICE · " + win : "SERVICE";
    el.sideWindowTitle.textContent = win ? "CHAT WINDOW · " + win : "CHAT WINDOW";
    // READ-ONLY since ui-monitor.md 10.5. This was a picker; the service is
    // the MONITOR's now - which one each window drives, and every budget under
    // it, is decided in that process's own window - so all that is left here is
    // the line saying what it settled on, worded on the Python side like every
    // other sentence in this column.
    el.serviceName.textContent = event.service || "";
  }

  function paintMcp(event) {
    var rows = event.rows || [];
    // Absent - heading included - rather than an empty block: a standing
    // question with no answer is worse than no block at all.
    el.mcpBlock.hidden = rows.length === 0;
    el.mcpRows.innerHTML = "";
    rows.forEach(function (row) {
      var node = document.createElement("div");
      node.className = "mcp-row " + (row.state || "");
      node.textContent = row.line;
      node.title = row.line; // the column ellipses; /mcp prints the whole thing
      el.mcpRows.appendChild(node);
    });
  }

  function paintDetection(event) {
    var line = id("det-" + event.kind);
    if (!line) return;
    line.textContent = event.label ? event.label + " · " + event.text : event.text;
  }

  /* == MONITOR SEES (F2) ====================================================
     Python decides every word (chat/view.py: sees_rows, sees_settings) and
     pushes only when they CHANGE - ticks arrive about once a second and this
     block would otherwise repaint for the whole of a session. So there is no
     diffing here: what arrives is different from what is up, always.

     Shown/hidden exactly like F3's sidebar and F8's log pane: a page-local
     flag, no round trip, and the paint runs whether the block is open or not
     so that opening it is a reveal rather than a wait for the next tick. */

  function paintSees(event) {
    var rows = event.rows || [];
    el.seesRows.innerHTML = "";
    rows.forEach(function (row) {
      var node = document.createElement("div");
      node.className = "sees-row " + (row.state || "");
      node.textContent = row.text;
      el.seesRows.appendChild(node);
    });
    el.seesSettings.textContent = event.settings || "";
    el.seesSettings.hidden = !event.settings;
    // The note and the rows are alternatives, never both: it says why there
    // are no rows.
    el.seesNote.textContent = event.note || "";
    el.seesNote.hidden = !event.note;
  }

  function toggleSees() {
    el.sees.hidden = !el.sees.hidden;
  }

  function paintStatus(event) {
    // The provider is here and nowhere else, and it is half of `w`'s "never, in
    // this mode": in manual-clipboard mode nothing polls the clipboard at all.
    live.provider = event.provider || "";
    el.statusbar.innerHTML = "";
    (event.segments || []).forEach(function (segment) {
      var node = document.createElement("span");
      node.className = "seg " + (segment.cls || "");
      node.id = "seg-" + segment.id;
      node.textContent = segment.text;
      el.statusbar.appendChild(node);
    });
  }

  function paintArmed(armed) {
    // Both doors into the banner (`status` and `armed`) come through here, so
    // this is the one place the strip has to be told: while disarmed the
    // watcher key is gone, not faded.
    live.armed = Boolean(armed);
    paintKeyHints();
    el.sideArmed.hidden = Boolean(armed);
    // The wording is the TUI's DISARMED_BANNER_TEXT; it says what stopped,
    // because "disarmed" alone leaves the user wondering whether detection died
    // too (it did not).
    el.sideArmed.textContent = "⛔ DISARMED\nwatching only - F5 arms";
  }

  function toggleSidebar() {
    el.sidebar.hidden = !el.sidebar.hidden;
  }

  /* == harness log ==========================================================
     Follow/freeze is a PROPERTY, not a mode: "am I at the tail" is read fresh
     on every append, so there is no flag that can disagree with where the
     scroll actually is. At the tail a new entry scrolls the view; scrolled up
     it lands below the fold and the view does not move; scrolling back down
     resumes following with nothing having to notice. */

  function following() {
    var box = el.logLines;
    return box.scrollHeight - box.scrollTop - box.clientHeight < 4;
  }

  function paintLog(stick) {
    var box = el.logLines;
    var was = box.scrollTop;
    box.textContent = logEntries.length ? logEntries.join("\n") : EMPTY_LOG_LINE;
    box.scrollTop = stick ? box.scrollHeight : was;
  }

  function appendLog(event) {
    logEntries.push(event.line);
    if (logEntries.length > LOG_MAX) logEntries = logEntries.slice(-LOG_MAX);
    // Hidden, this costs one array push and no render at all - the reveal is
    // one full refill from the buffer anyway.
    if (!logOpen) return;
    paintLog(following());
  }

  function toggleLog() {
    logOpen = !logOpen;
    el.logpane.hidden = !logOpen;
    // A pane that came back showing where it left off would be lying about a
    // log whose whole purpose is to say what just happened.
    if (logOpen) paintLog(true);
  }

  /* == the SSH connect dialog ==============================================
     The one surface with no TUI equivalent. Everything it DECIDES is Python's
     (chat/remote.py:ConnectDialog) - which targets exist, whether Connect may be
     pressed, which row a failure landed on, what the policy banner says. This
     side owns the drawing and one convenience: the "connecting as ..." preview
     is recomputed locally per keystroke so it tracks the caret rather than the
     round trip, using the SAME grammar the backend parses (config.py's
     RemoteConfig.selected). */

  function connParse(spec) {
    var at = spec.lastIndexOf("@");
    var user = at >= 0 ? spec.slice(0, at) : "";
    var rest = at >= 0 ? spec.slice(at + 1) : spec;
    var colon = rest.indexOf(":");
    var host = colon >= 0 ? rest.slice(0, colon) : rest;
    var port = colon >= 0 ? rest.slice(colon + 1) : "";
    var base = user ? user + "@" + host : host;
    if (port && /^[0-9]+$/.test(port) && port !== "22") base += ":" + port;
    return base;
  }

  function connPreviewLocal() {
    var spec = el.connTarget.value.trim();
    el.connPreview.textContent = spec ? "connecting as " + connParse(spec) : "";
  }

  function connList(host, rows) {
    host.innerHTML = "";
    (rows || []).forEach(function (row) {
      var item = document.createElement("li");
      var pick = document.createElement("button");
      pick.type = "button";
      pick.className = "conn-row";
      var name = document.createElement("div");
      name.textContent = row.name;
      var detail = document.createElement("div");
      detail.className = "conn-row-detail";
      detail.textContent = row.root ? row.detail + "  " + row.root : row.detail;
      pick.appendChild(name);
      pick.appendChild(detail);
      pick.addEventListener("click", function () {
        api("connect_select", row.key);
      });
      item.appendChild(pick);
      host.appendChild(item);
    });
  }

  // The four row states, and none of them is interchangeable: a stage AFTER a
  // failure is pending, never skipped-with-a-tick (ssh-connect.md 3.4).
  function connMark(state) {
    if (state === "ok") return "✓";
    if (state === "failed") return "✗";
    if (state === "running") return "▶";
    return "·";
  }

  function paintConnect(event) {
    if (!event.open) {
      connectOpen = false;
      el.connExec.hidden = true;
      // The scrim belongs to the DIALOG, not to either tab: closing one tab
      // while the other is open must leave the frame on screen.
      if (!monitorOpen) el.connScrim.hidden = true;
      return;
    }
    var opening = !connectOpen;
    connectOpen = true;
    el.connScrim.hidden = false;
    el.connExec.hidden = false;
    el.connTabExec.classList.add("on");
    el.connTabMonitor.classList.remove("on");

    var form = event.phase === "form";
    el.connForm.hidden = !form;
    // The checklist appears with the first attempt and STAYS while the user
    // edits: what went wrong is the reason they are back in the form.
    el.connSteps.hidden = form && !event.failed_step;

    // Only on a RELOAD of the form, never per repaint - the same rule the
    // service editor's `reload` flag encodes, and the same reason.
    if (opening || form) {
      if (document.activeElement !== el.connTarget) el.connTarget.value = event.target || "";
      if (document.activeElement !== el.connRoot) el.connRoot.value = event.root || "";
    }
    el.connPreview.textContent = event.preview ? "connecting as " + event.preview : "";
    connList(el.connSaved, event.saved);
    connList(el.connAliases, event.aliases);
    el.connError.textContent = event.error || "";

    el.connSteps.innerHTML = "";
    (event.steps || []).forEach(function (row) {
      var item = document.createElement("li");
      item.className = "conn-step conn-step-" + row.state;
      var mark = document.createElement("span");
      mark.className = "conn-step-mark";
      mark.textContent = connMark(row.state);
      var text = document.createElement("span");
      var label = document.createElement("div");
      label.textContent = row.label;
      text.appendChild(label);
      if (row.note) {
        var note = document.createElement("div");
        note.className = "conn-step-note";
        note.textContent = row.note;
        text.appendChild(note);
      }
      item.appendChild(mark);
      item.appendChild(text);
      el.connSteps.appendChild(item);
    });

    el.connFailure.textContent = event.failure || "";
    el.connFailure.hidden = !event.failure;

    // Shown once, right after the last step and before the dialog closes: a
    // user whose own opencode.json just stopped applying has to be told.
    var policy = event.policy || [];
    el.connPolicy.innerHTML = "";
    el.connPolicy.hidden = policy.length === 0;
    policy.forEach(function (line) {
      var node = document.createElement("div");
      node.textContent = line;
      el.connPolicy.appendChild(node);
    });

    el.connSave.hidden = !event.can_save;
    if (event.can_save && document.activeElement !== el.connSaveName) {
      el.connSaveName.value = event.save_name || "";
    }
    el.connSavedNote.textContent = event.saved_note || "";

    el.connConnect.hidden = event.phase === "done";
    el.connConnect.disabled = Boolean(event.busy);
    el.connConnect.textContent = event.phase === "failed" ? "Retry" : "Connect";
    el.connEdit.hidden = event.phase !== "failed";
    el.connClose.textContent = event.phase === "done" ? "Close" : "Cancel";
    el.connClose.disabled = Boolean(event.busy);
    el.connHint.textContent = CONN_HINTS[event.phase] || "";
    if (opening) {
      window.setTimeout(function () {
        el.connTarget.focus();
      }, 0);
    }
  }

  /* == the Monitor tab (ui-monitor.md 9.2) =================================
     The same arrangement as the dialog beside it and the same division of
     labour: every decision - which targets exist, whether Attach may be
     pressed, what a failure says, whether a save is offered - is Python's
     (chat/remote.py:MonitorDialog). This side draws it and sends whole forms. */

  function monFields() {
    api(
      "monitor_fields",
      el.monModeLocal.checked ? "local" : el.monModeSsh.checked ? "ssh" : "direct",
      el.monHost.value,
      el.monPort.value,
      el.monToken.value,
      el.monVia.value
    );
  }

  function monList(host, rows) {
    host.innerHTML = "";
    (rows || []).forEach(function (row) {
      var item = document.createElement("li");
      var pick = document.createElement("button");
      pick.type = "button";
      pick.className = "conn-row";
      var name = document.createElement("div");
      name.textContent = row.name;
      var detail = document.createElement("div");
      detail.className = "conn-row-detail";
      detail.textContent = row.detail;
      pick.appendChild(name);
      pick.appendChild(detail);
      pick.addEventListener("click", function () {
        api("monitor_select", row.key);
      });
      // Saved from this dialog, so unsavable from it too: the alternative is a
      // TOML file the user has to go and find because a UI wrote it.
      var forget = document.createElement("button");
      forget.type = "button";
      forget.className = "conn-forget";
      forget.title = "forget this monitor";
      forget.textContent = "\u00d7";
      forget.addEventListener("click", function () {
        api("monitor_forget", row.name);
      });
      item.appendChild(pick);
      item.appendChild(forget);
      host.appendChild(item);
    });
  }

  function monVias(rows, selected) {
    el.monVia.innerHTML = "";
    (rows || []).forEach(function (row) {
      var option = document.createElement("option");
      option.value = row.name;
      option.textContent = row.detail ? row.name + "  (" + row.detail + ")" : row.name;
      el.monVia.appendChild(option);
    });
    if (selected) el.monVia.value = selected;
  }

  function paintMonitor(event) {
    if (!event.open) {
      monitorOpen = false;
      el.connMonitor.hidden = true;
      if (!connectOpen) el.connScrim.hidden = true;
      return;
    }
    var opening = !monitorOpen;
    monitorOpen = true;
    el.connScrim.hidden = false;
    el.connMonitor.hidden = false;
    el.connExec.hidden = true;
    el.connTabExec.classList.remove("on");
    el.connTabMonitor.classList.add("on");

    var ssh = event.mode === "ssh";
    var local = event.mode === "local";
    el.monModeSsh.checked = ssh;
    el.monModeLocal.checked = local;
    el.monModeDirect.checked = !ssh && !local;
    el.monViaRow.hidden = !ssh;
    // Local mode has no address at all: the port is picked at spawn and the
    // token is the file both processes read (ui-monitor.md 10.1). So the whole
    // "how to reach it" half is hidden rather than disabled - there is nothing
    // to fill in, and an empty form the user must not touch is worse than none.
    el.monAddress.hidden = local;
    // Via SSH the host box means "as seen from THAT machine", which is a
    // different question with the same answer shape - so the label changes and
    // the placeholder stops suggesting a LAN address.
    el.monHostTitle.textContent = ssh ? "Monitor host, as seen from that machine" : "Monitor host";
    el.monHost.placeholder = ssh ? "127.0.0.1" : "e.g. 192.168.1.40";
    el.monLocalNote.hidden = !local;

    var form = event.phase === "form";
    el.monForm.hidden = !form;
    if (opening || form) {
      if (document.activeElement !== el.monHost) el.monHost.value = event.host || "";
      if (document.activeElement !== el.monPort) el.monPort.value = event.port || "";
      if (document.activeElement !== el.monToken) el.monToken.value = event.token || "";
    }
    monList(el.monSaved, event.saved);
    monVias(event.ssh, event.via);
    el.monError.textContent = event.error || "";
    el.monFailure.textContent = event.failure || "";
    el.monFailure.hidden = !event.failure;
    el.monAttachedLine.textContent = event.attached
      ? "attached: " + event.attached
      : "no monitor attached";

    el.monSave.hidden = !event.can_save;
    if (event.can_save && document.activeElement !== el.monSaveName) {
      el.monSaveName.value = event.save_name || "";
    }
    el.monSavedNote.textContent = event.saved_note || "";

    el.monAttach.hidden = event.phase === "done";
    el.monAttach.disabled = Boolean(event.busy);
    el.monAttach.textContent =
      event.phase === "failed"
        ? "Retry"
        : local
          ? "Launch & connect a local monitor"
          : "Attach";
    el.monEdit.hidden = event.phase !== "failed";
    // Disconnect is about the LINK and Close is about the dialog: a monitor
    // that is attached can be let go of without ending the session, and the two
    // buttons must never be read as the same thing.
    el.monDetach.hidden = !event.attached;
    el.monDetach.disabled = Boolean(event.busy);
    el.monClose.disabled = Boolean(event.busy);
    el.monHint.textContent = MON_HINTS[event.phase] || "";
    if (opening && !local) {
      window.setTimeout(function () {
        el.monHost.focus();
      }, 0);
    }
  }

  /* == chrome ============================================================== */

  function paintState(event) {
    // The composer's mode decides whether a leading slash means anything: in
    // `answer` mode it does not (the TUI's `verbatim`), and the popup is
    // suppressed rather than offering to complete something Enter will send
    // verbatim anyway (modals-keys-esc.md 6.1).
    composerMode = event.composer_mode || "idle";
    // The question, pinned above the composer for as long as it is open. The
    // transcript still gets its "? ..." note - this is the STOP, and a stop that
    // can scroll out of sight is not one. Python decides both the text and when
    // it is over (the `awaiting_answer` flag it rides with), so nothing here has
    // to guess when a question ended.
    awaitingAnswer = Boolean(event.awaiting_answer);
    el.askQuestion.textContent = event.question || "";
    el.askBanner.hidden = !(awaitingAnswer && event.question);
    el.composer.disabled = !event.composer_enabled;
    el.composer.placeholder = event.composer_placeholder || "";
    el.send.disabled = !event.composer_enabled;
    el.phase.textContent = event.session_active ? event.phase : "no session";
    el.service.textContent = event.service
      ? event.service + " · turn " + event.turn + " · " + event.permission_mode
      : "";
    el.service.hidden = !event.service;
    document.body.classList.toggle("yolo", Boolean(event.yolo));
    // Never steal the caret out of a reject note being written: the state push
    // that lands mid-typing is the gate's own, and it would take the box away.
    // Nor out of a selection somebody is making in the transcript - the
    // same rule, one surface wider.
    // rule, one surface wider - out of a selection somebody is making in the
    // transcript: focusing a textarea collapses the document's selection, and a
    // state push landing between the drag and the ctrl+c is not a reason to
    // lose the words. The next click hands the caret back on its own.
    if (
      event.composer_enabled &&
      !rejectOpen &&
      !modalUp() &&
      !draggedOutText()
    ) {
      el.composer.focus();
    }
    syncPopup();
    // ...and the five facts the key hint strip reads. Kept here rather than in
    // the footer's own code because this event IS the state: one snapshot, one
    // place it is unpacked.
    live.sessionActive = Boolean(event.session_active);
    live.busy = Boolean(event.busy);
    live.phase = event.phase || "IDLE";
    live.hasOutbound = Boolean(event.has_outbound);
    paintKeyHints();
  }

  /* == dispatch ============================================================ */

  function dispatch(event) {
    switch (event.type) {
      case "transcript":
        addTranscript(event);
        return;
      case "transcript_clear":
        // Every window's transcript, because /new is a SESSION teardown: the
        // windows themselves - their tabs, their services, their calibrations -
        // are untouched, and only what the sessions wrote goes.
        Object.keys(panels).forEach(function (key) {
          var target = panels[key];
          target.node.innerHTML = '<p class="empty">' + escapeHtml(target.empty) + "</p>";
          target.parked = false;
          target.stick = true;
          target.replyStart = null;
          target.awaitingReplyStart = false;
        });
        return;
      case "focus_session":
        // Where output is going now. The tabs event that follows carries it
        // too; nothing here has to move the view, which is the controller's
        // one reach into the selection and is made on the Python side.
        return;
      case "tabs":
        paintTabs(event);
        return;
      case "state":
        paintState(event);
        return;
      case "status":
        paintStatus(event);
        paintArmed(event.armed);
        return;
      case "rail":
        paintRail(event);
        return;
      case "sidebar":
        paintSidebar(event);
        return;
      case "mcp":
        paintMcp(event);
        return;
      case "armed":
        paintArmed(event.armed);
        return;
      case "gate":
        if (event.open) showGate(event);
        else hideGate();
        return;
      case "run":
        // The panel is torn down whole when the turn ends, buffers included:
        // the model's own copy of the output is already on its way to the
        // transcript, tail-capped, and was never this (run_panel.py).
        runRows = {};
        runOutput = {};
        streamingCall = null;
        tailOpen = false;
        // ctrl+x cancels calls that are running THIS INSTANT and nothing else,
        // so the panel's own lifetime is the hint's.
        live.executing = Boolean(event.running);
        paintKeyHints();
        if (event.running) {
          (event.calls || []).forEach(function (call) {
            runRows[call.call_id] = call;
            if (call.glyph === "▶" && call.streams) streamingCall = call.call_id;
          });
          el.runLabel.textContent = event.label || "";
          paintRunRows();
          paintRunTail();
          el.run.hidden = false;
        } else {
          el.run.hidden = true;
          paintRunTail();
        }
        return;
      case "run_call":
        if (event.phase === "started") {
          // A call the plan never mentioned still gets a row: the port's
          // contract says an id this page has never heard of may arrive.
          var row = runRows[event.call_id] || { call_id: event.call_id };
          row.tool = event.tool || row.tool || "";
          row.detail = event.detail || row.detail || "";
          row.streams = Boolean(event.streams);
          row.glyph = "▶";
          runRows[event.call_id] = row;
          // A new call is a new pane: whatever the last one printed is gone
          // from the view the moment something else is running.
          tailOpen = false;
          streamingCall = row.streams ? event.call_id : null;
        } else {
          if (runRows[event.call_id]) runRows[event.call_id].glyph = event.glyph;
          else runRows[event.call_id] = { call_id: event.call_id, tool: "", detail: "", glyph: event.glyph };
          endStreaming(event.call_id);
        }
        paintRunRows();
        paintRunTail();
        return;
      case "run_output":
        // The chunk is a DELTA, never the whole buffer, so the accumulation is
        // this page's - bounded per call, and painted only while the pane is
        // open (a chatty command costs a couple of array operations per poll
        // slice and no render at all while collapsed).
        var buffer = runOutput[event.call_id] || (runOutput[event.call_id] = [""]);
        var chunk = String(event.chunk).replace(/\r\n?/g, "\n").split("\n");
        buffer[buffer.length - 1] += chunk.shift();
        Array.prototype.push.apply(buffer, chunk);
        if (buffer.length > OUTPUT_LINES) {
          runOutput[event.call_id] = buffer.slice(-OUTPUT_LINES);
        }
        if (tailOpen && streamingCall === event.call_id) paintRunTail();
        return;
      case "composer_reset":
        el.composer.value = "";
        popupHide();
        sentReset(); // the box emptied under the walk; there is no draft left to give back
        return;
      case "commands":
        // The slash registry. One push per load, feeding both the popup and the
        // help sheet's command table (bridge.py's catalogue).
        commands = event.rows || [];
        return;
      case "docs":
        // The user guide, whole, once per load - `commands`' twin (bridge.py's
        // catalogue). A viewer open across a repush is rebuilt on the page it
        // was on rather than left showing a document that has been re-read.
        docPages = event.pages || [];
        if (pageModal === "docs") openDocs(docCurrent);
        return;
      case "settings":
        themeChoices = event.themes || [];
        applyTheme(event.theme);
        // A settings modal open across a repaint (the save's own echo) keeps
        // showing the truth rather than the radio the click left behind.
        if (pageModal === "settings") openSettings();
        return;
      case "toast":
        toast(event);
        return;
      case "flash":
        el.flash.hidden = !event.show;
        if (event.show) el.flashText.textContent = event.text;
        // The button's visibility is coupled to WHICH flash is showing, not to
        // "was there ever a failure": only the Ctrl+V variant has something to
        // retry, and hiding the flash hides it unconditionally.
        el.retryInsert.hidden = !(event.show && event.retry);
        return;
      case "modal":
        showModal(event);
        return;
      case "modal_close":
        if (modalId === event.prompt_id) {
          modalId = null;
          modalKind = null;
          el.scrim.hidden = true;
        }
        return;
      case "payload":
        showPayload(event);
        return;
      case "detection":
        paintDetection(event);
        return;
      case "monitor_sees":
        paintSees(event);
        return;
      case "harness":
        appendLog(event);
        return;
      case "toggle":
        // /log's way in. The same call F8 makes, deliberately: two ways to ask
        // for one thing, one implementation of it.
        if (event.what === "log") toggleLog();
        return;
      case "monitor":
        paintMonitor(event);
        break;
      case "monitor_link":
        paintMonitorLink(event);
        break;
      case "connect":
        paintConnect(event);
        break;
      default:
        return;
    }
  }

  /* == the key table =========================================================
     ONE array, two consumers: the document keydown handler dispatches from it
     and the help sheet renders it. That is the whole point of it being a table
     at all - a key rebound in a switch statement and a help screen written in
     prose drift on the first change, and a cheatsheet nobody trusts is worse
     than none.

     It is the GUI's OWN table, not a copy of the TUI's: Shift+Enter is the
     newline here, F1/F4 open page screens rather than Textual modals, and every
     recorded divergence in docs/design/gui.md §3 shows up as the wording of a
     row. The composer's own keys (Enter, Shift+Enter, the arrows and Esc inside
     the box) are deliberately NOT here - they belong to the textarea's handler,
     which is a different dispatcher, and the help sheet describes them as prose
     in its "Chat box" block.

     Fields:
       keys    what the help sheet SHOWS ("shift+tab", "ctrl+x")
       on      the raw KeyboardEvent.key values that match
       mods    "" | "ctrl" | "shift" - which modifier must be down
       hot     fires even while a text box has focus. The TUI buys this with
               priority=True; here it is this flag, plus one rule the flag
               cannot express: a PRINTABLE key never fires while typing, whatever
               it is bound to, which is why "?" opens help from the transcript
               and types a question mark in the composer.
       when    optional extra condition (the gate keys)
       what    the help sheet's description
       section which help block it appears under
       foot    the KEY HINT FOOTER's short label, and its presence is what puts
               the row in the strip at all - the TUI's `show=` on a Binding,
               with the Binding's own wording. Absent on every key the TUI's
               footer hides for the same reason it does (x, F2, F4, F6-F8,
               ctrl+o, ctrl+q, ctrl+enter, shift+tab): the strip is one row and
               the loop's one-key answers are what belongs on it.
       avail   the footer's three-way state - "on" | "dim" | "off" - which is
               check_action's True / None / False and nothing else. Absent means
               "on", except that a row with a `when` is dimmed while it is
               false, so the gate keys need no second gate written out. */

  var KEY_SECTIONS = ["App", "Approval", "Session"];

  var KEYS = [
    // -- App ---------------------------------------------------------------
    { keys: ["F1", "?"], on: ["F1", "?"], mods: "", hot: true, section: "App",
      foot: "help",
      what: "this help", run: function () { openHelp(); } },
    // F2 used to open the calibration window and was left bound to nothing by
    // ui-monitor.md 11.2. It is a READOUT now (11.4), which is the only thing
    // this window can honestly offer about the machine with the pixels.
    { keys: ["F2"], on: ["F2"], mods: "", hot: true, section: "App",
      what: "what the monitor sees (and the settings it sent): which appearances that machine holds for this service, which of them are on screen right now, and the behaviour it told this window to drive - captures are made in the Monitor UI",
      run: function () { toggleSees(); } },
    { keys: ["F3"], on: ["F3"], mods: "", hot: true, section: "App",
      foot: "sidebar",
      what: "hide/show the sidebar", run: function () { toggleSidebar(); } },
    { keys: ["F4"], on: ["F4"], mods: "", hot: true, section: "App",
      what: "appearance (theme)", run: function () { openSettings(); } },
    { keys: ["F5"], on: ["F5"], mods: "", hot: true, section: "App",
      foot: "armed",
      what: "ARM / DISARM the tool (also /armed). Disarmed it still watches and shows everything, but never clicks, pastes or reads your clipboard",
      run: function () { api("armed", null); } },
    { keys: ["F6"], on: ["F6"], mods: "", hot: true, section: "App",
      what: "select the next window tab (view only - it never moves what the automation drives)",
      run: function () { api("next_window"); } },
    { keys: ["F8"], on: ["F8"], mods: "", hot: true, section: "App",
      what: "hide/show the HARNESS DECISION LOG (also /log)", run: function () { toggleLog(); } },
    { keys: ["shift+tab"], on: ["Tab"], mods: "shift", hot: true, section: "App",
      what: "cycle the permission mode: build -> plan -> build. Works before a session and mid-turn",
      run: function () { api("mode"); } },
    { keys: ["ctrl+enter"], on: ["Enter"], mods: "ctrl", hot: true, section: "App",
      what: "send the chat box without having to be in it", run: function () { send(); } },
    { keys: ["ctrl+q"], on: ["q", "Q"], mods: "ctrl", hot: true, section: "App",
      what: "quit (asks first when a turn is mid-flight, as closing the window does)",
      run: function () { api("quit"); } },

    // -- Approval ----------------------------------------------------------
    // Dimmed rather than dropped with no gate up, which is check_action's
    // `None`: the keys come back the moment a call needs a decision, and a
    // strip that lost three entries and grew them again would move the rest.
    { keys: ["y"], on: ["y"], mods: "", section: "Approval", when: gateIsOpen,
      foot: "approve",
      what: "approve the gated call", run: function () { api("decide", "approve", ""); } },
    { keys: ["n"], on: ["n"], mods: "", section: "Approval", when: gateIsOpen,
      foot: "reject",
      what: "reject it (a reason is optional)", run: function () { openReject(); } },
    { keys: ["a"], on: ["a"], mods: "", section: "Approval", when: alwaysOffered,
      foot: "auto-edits",
      what: "approve and stop asking - auto-accept edits, or always allow this pattern",
      run: function () { api("decide", "approve_always", ""); } },

    // -- Session -----------------------------------------------------------
    { keys: ["u"], on: ["u"], mods: "", section: "Session", avail: availFloor,
      foot: "undo",
      what: "undo the last turn (confirm first; a revert notice is copied for the model)",
      run: function () { api("undo"); } },
    { keys: ["c"], on: ["c"], mods: "", section: "Session", avail: availOutbound,
      // Two words longer than every other label, and they buy the only thing a
      // footer can say about a double tap: that there IS one. The TUI's own
      // wording, for the same reason.
      foot: "re-copy · cc pastes",
      what: "re-copy the last outbound payload; press c TWICE quickly and it is pasted into the chat as well",
      run: function () { api("recopy"); } },
    { keys: ["i"], on: ["i"], mods: "", section: "Session", avail: availIngest,
      foot: "ingest",
      what: "force-ingest the clipboard now", run: function () { api("ingest"); } },
    // The one row whose HIDDEN state this side cannot see: the TUI drops `r`
    // outright on a service with nothing to re-inject, and whether this service
    // carries extra instructions is not on any push the page receives. So it is
    // shown, and the refusal stays a toast from Python - the divergence gui.md
    // §3 records, now down to one key instead of three.
    { keys: ["r"], on: ["r"], mods: "", section: "Session", avail: availSession,
      foot: "re-instruct",
      what: "re-send this service's extra instructions with the next payload (set them in the Monitor UI)",
      run: function () { api("reinstruct"); } },
    { keys: ["w"], on: ["w"], mods: "", section: "Session", avail: availWatch,
      foot: "watcher",
      what: "pause/resume the clipboard watcher", run: function () { api("watch"); } },
    { keys: ["t"], on: ["t"], mods: "", section: "Session", avail: availComposer,
      foot: "type message",
      what: "jump back to the chat box", run: focusComposer },
    { keys: ["e"], on: ["e"], mods: "", section: "Session", avail: availFloor,
      foot: "summary",
      what: "end session / show the summary", run: function () { api("end_session"); } },
    { keys: ["l"], on: ["l"], mods: "", section: "Session", avail: availSession,
      foot: "export log",
      what: "export the whole chat log to a file (raw blocks and payloads, for debugging)",
      run: function () { api("export_log"); } },
    { keys: ["x"], on: ["x"], mods: "", section: "Session",
      what: "expand/collapse the last collapsed output", run: function () { toggleLastBlock(); } },
    { keys: ["ctrl+x"], on: ["x", "X"], mods: "ctrl", hot: true, section: "Session",
      avail: availExecuting, foot: "cancel run",
      what: "cancel the tool calls running now. The turn still ends cleanly and the model is told",
      run: function () { api("cancel"); } },
    { keys: ["ctrl+o"], on: ["o", "O"], mods: "ctrl", hot: true, section: "Session",
      what: "show/hide what the running command is printing (clicking the run panel does the same)",
      run: function () { toggleRunOutput(); } }
  ];

  function gateIsOpen() {
    return gateOpen;
  }

  function alwaysOffered() {
    return gateOpen && gateAlwaysOffered;
  }

  /* == the footer's three states ============================================
     MainScreen.check_action, key for key, out of the pushes this page already
     receives (`state`, `status`, `run`) - which is why none of these reach for
     anything new over the bridge. The three answers are the brief's
     (modals-keys-esc.md §6.6 / §7):

       "on"  - it fires now
       "dim" - it does not, but it WILL: a turn has to finish, a gate has to
               open, a session has to start. Shown, faded.
       "off" - it never can, in this mode. Gone from the strip entirely.

     Nothing here DECIDES anything: every one of these keys is gated again on
     the Python side, where the whole truth is, and refuses out loud when it is
     pressed anyway. This is a cheatsheet that keeps up. */

  // `u` and `e`: a live session, no turn in flight, and the floor back with the
  // user - the summary is a report on a settled session, and undo cannot walk
  // back a turn that is still being written.
  function availFloor() {
    var back =
      live.sessionActive &&
      !live.busy &&
      (live.phase === "AWAITING_REPLY" || live.phase === "DONE");
    return back ? "on" : "dim";
  }

  function availSession() {
    return live.sessionActive ? "on" : "dim";
  }

  function availOutbound() {
    return live.hasOutbound ? "on" : "dim";
  }

  // `i` only parses in AWAITING_REPLY: everywhere else there is no reply for
  // the clipboard to be.
  function availIngest() {
    var ok = live.sessionActive && !live.busy && live.phase === "AWAITING_REPLY";
    return ok ? "on" : "dim";
  }

  // `w` is the one key with a real "never, in this mode": in manual-clipboard
  // mode nothing polls, and while disarmed nothing may - so the key is dropped
  // rather than faded, because it is not coming back until something else
  // changes (F5, or the provider). The DISARMED badge and banner say why.
  function availWatch() {
    if (live.provider === "manual" || !live.armed) return "off";
    return availSession();
  }

  // `t`, against the composer's own disabled flag - which is Python's
  // composed answer to "may the user type right now" (GuiView._composer_mode),
  // and so a better gate than re-deriving one here.
  function availComposer() {
    return el.composer && !el.composer.disabled ? "on" : "dim";
  }

  function availExecuting() {
    return live.executing ? "on" : "dim";
  }

  // `t`. Page-side, with no controller behind it, exactly like F3's sidebar:
  // "put the caret back in the box" is a fact about this window. The gate is the
  // box's own disabled flag, which Python already composed from the brief's
  // precedence table (GuiView._composer_mode) - so there is one answer to "may
  // the user type right now", not two.
  function focusComposer() {
    if (el.composer.disabled) return;
    el.composer.focus();
  }

  function keyMatches(entry, ev) {
    if (ev.altKey || ev.metaKey) return false;
    if (ev.ctrlKey !== (entry.mods === "ctrl")) return false;
    if (entry.mods === "shift" && !ev.shiftKey) return false;
    return entry.on.indexOf(ev.key) !== -1;
  }

  function dispatchKey(ev, typing) {
    var printable =
      typeof ev.key === "string" &&
      ev.key.length === 1 &&
      !ev.ctrlKey &&
      !ev.altKey &&
      !ev.metaKey;
    for (var i = 0; i < KEYS.length; i += 1) {
      var entry = KEYS[i];
      if (!keyMatches(entry, ev)) continue;
      // Inert while a text box has focus - the ONE thing keeping the letters
      // below from firing into a sentence somebody is typing - unless the key is
      // hot AND is not a character the box would have received.
      if (typing && (!entry.hot || printable)) continue;
      if (entry.when && !entry.when()) continue;
      ev.preventDefault();
      entry.run();
      return true;
    }
    return false;
  }

  /* == the key hint footer ===================================================
     The TUI's stock Footer, painted from the SAME table the dispatcher reads
     and the help sheet renders - so this strip cannot advertise a key that does
     not exist, and a key added to the table shows up here by existing. Which
     rows it carries is the table's `foot`, which is the TUI's `show=` on a
     Binding; what state each one is in is `avail`, which is check_action's
     three-way answer (above).

     F1's sheet is still where a key is EXPLAINED - a sentence per row does not
     fit in a strip - and this is where it is remembered. */

  // Built once. The rows never change; only their state does, and rebuilding a
  // row of spans several times a turn would be a repaint for nothing.
  var footRows = [];

  function buildKeyHints() {
    footRows = [];
    el.keyhints.innerHTML = "";
    KEYS.forEach(function (entry) {
      if (!entry.foot) return;
      var node = document.createElement("span");
      node.className = "kh";
      var key = document.createElement("b");
      key.className = "kh-key";
      key.textContent = entry.keys[0];
      node.appendChild(key);
      node.appendChild(document.createTextNode(" " + entry.foot));
      el.keyhints.appendChild(node);
      footRows.push({ entry: entry, node: node });
    });
    paintKeyHints();
  }

  function footState(entry) {
    if (entry.avail) return entry.avail();
    // A row with a gate and no `avail` is the gate's own dimming: the approval
    // keys say "not now" rather than disappearing, which is check_action's
    // `None` for exactly the same three keys.
    if (entry.when) return entry.when() ? "on" : "dim";
    return "on";
  }

  // A focused text box swallows every bare letter - dispatchKey's one rule, and
  // the whole reason the single-letter keys are safe to have at all. While it
  // has the caret those rows are not going to fire whatever their gate says, so
  // the strip must not promise otherwise. Esc gives the box back (stage 3).
  function printableBinding(entry) {
    return (
      entry.mods === "" && !entry.hot && entry.on.length > 0 && entry.on[0].length === 1
    );
  }

  function typingNow() {
    var node = document.activeElement;
    var tag = node && node.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  function paintKeyHints() {
    if (!footRows.length) return;
    var typing = typingNow();
    footRows.forEach(function (row) {
      var state = footState(row.entry);
      if (state === "on" && typing && printableBinding(row.entry)) state = "dim";
      row.node.hidden = state === "off";
      row.node.className = state === "dim" ? "kh dim" : "kh";
    });
  }

  /* == the send history the arrows walk ======================================
     tui/widgets/composer.py's SendHistory, rule for rule: session-local, capped,
     blanks skipped, a repeat of the newest collapsed, and the draft handed back
     past the newest end. Two shells, one behaviour - the arrows are chat-app
     muscle memory and would be worse than useless if they differed. */

  function sentReset() {
    sentAt = null;
    sentDraft = "";
  }

  function sentPush(text) {
    sentReset(); // a send ends the walk
    if (!text.trim()) return;
    // Sending the same thing twice running is common (a retried command, a
    // repeated "continue") and would otherwise cost two ArrowUps to get past.
    if (sent.length && sent[sent.length - 1] === text) return;
    sent.push(text);
    if (sent.length > SENT_MAX) sent = sent.slice(-SENT_MAX);
  }

  // Both of these return null to DECLINE the key, which is how the arrows go
  // back to being ordinary caret keys at the ends of the walk.
  function sentOlder(current) {
    if (sentAt === null) {
      if (!sent.length) return null;
      sentDraft = current; // captured only on the way in
      sentAt = sent.length - 1;
    } else if (sentAt === 0) {
      return null; // already the oldest: let the caret have the key back
    } else {
      sentAt -= 1;
    }
    return sent[sentAt];
  }

  function sentNewer() {
    if (sentAt === null) return null; // nobody walked up; this is a caret key
    if (sentAt === sent.length - 1) {
      var draft = sentDraft; // read before the reset that clears it
      sentReset();
      return draft; // may be "" - an empty box is a perfectly good draft
    }
    sentAt += 1;
    return sent[sentAt];
  }

  // Put a remembered send in the box with the caret at the end. What this
  // overwrites is recoverable by walking back DOWN to the draft, which is why
  // it does not go through execCommand the way Esc's clear does: the key that
  // replaced the text is also the key that gives it back, and that is a better
  // promise than an undo the user has to know about.
  function recallSent(text) {
    recalling = true;
    el.composer.value = text;
    el.composer.selectionStart = el.composer.selectionEnd = text.length;
    recalling = false;
    syncPopup();
  }

  // The composer's own dispatcher, and the first three stages of the Esc
  // chain. Enter sends; Shift+Enter is a newline (the web-native convention)
  // and so is Ctrl+J (the TUI's newline key, honored here too so the muscle
  // memory transfers between shells - docs/design/gui.md 2).
  function onComposerKey(ev) {
    // The popup owns five keys and only while it is open, checked FIRST -
    // ahead of Enter and ahead of Esc's own stages, exactly as
    // ChatComposer._on_key checks it (modals-keys-esc.md 3.3, stage 1).
    if (popupOpen() && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
      if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
        ev.preventDefault();
        popupMove(ev.key === "ArrowUp" ? -1 : 1);
        return;
      }
      if (ev.key === "Enter" || ev.key === "Tab") {
        // Swallowed even with nothing highlighted: a bare "/" plus Enter must
        // not send a lone slash, it must wait to be narrowed. Tab is Enter's
        // twin here and does NOT move focus.
        ev.preventDefault();
        popupComplete();
        return;
      }
      if (ev.key === "Escape") {
        // ESC STAGE 1: the popup only. The text and the caret are untouched,
        // and this does not fall through to the two stages below.
        ev.preventDefault();
        popupHide();
        return;
      }
    }
    // ...and once the popup above has had its refusal, up/down walk the sends -
    // but ONLY from the edge of the text. Anywhere else they are the textarea's
    // own keys, because a pasted traceback has to stay navigable and a key that
    // sometimes moves the caret and sometimes replaces the whole box would be
    // unusable. On a one-line box both edges are the same line, so it behaves
    // like every other chat app with no rule to learn. Same order, same rule and
    // same cap as ChatComposer._on_key.
    if ((ev.key === "ArrowUp" || ev.key === "ArrowDown") && !ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey) {
      var value = el.composer.value;
      var from = el.composer.selectionStart;
      var to = el.composer.selectionEnd;
      // A live selection is excluded outright: shrinking or growing it is what
      // the arrows do there, and there is no caret to be "on the first line".
      var collapsed = from === to;
      var recalled = null;
      if (ev.key === "ArrowUp" && collapsed && value.slice(0, from).indexOf("\n") === -1) {
        recalled = sentOlder(value);
      } else if (ev.key === "ArrowDown" && collapsed && value.slice(to).indexOf("\n") === -1) {
        recalled = sentNewer();
      }
      if (recalled !== null) {
        ev.preventDefault();
        recallSent(recalled);
        return;
      }
    }
    // Ctrl+J = newline, same as Shift+Enter. execCommand keeps the textarea's
    // own undo stack and input events alive; setRangeText is the fallback.
    if ((ev.key === "j" || ev.key === "J") && ev.ctrlKey && !ev.altKey && !ev.metaKey) {
      ev.preventDefault();
      if (!document.execCommand || !document.execCommand("insertText", false, "\n")) {
        el.composer.setRangeText("\n", el.composer.selectionStart, el.composer.selectionEnd, "end");
      }
      return;
    }
    if (ev.key === "Enter" && !ev.shiftKey && !ev.ctrlKey) {
      ev.preventDefault();
      send();
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      // ESC STAGE 3: an empty box lets go, which is what makes the single-key
      // shortcuts (w/c/i/r/x/u/t/l/e, y/n/a) reachable at all - the
      // inert-while-typing rule is only safe because there is a way to stop
      // typing.
      if (!el.composer.value) {
        el.composer.blur();
        return;
      }
      // ESC STAGE 2: text present clears the box and KEEPS focus so the
      // rewrite can start immediately. Through execCommand so the browser's
      // own undo stack survives it - assigning .value would throw the
      // paragraph away for good, and the TUI's equivalent is deliberately an
      // undoable edit (history.checkpoint(); clear()).
      el.composer.select();
      if (!document.execCommand || !document.execCommand("delete")) {
        el.composer.value = "";
      }
      syncPopup();
    }
  
  }

  /* The app-wide dispatcher, and the rest of the Esc chain.

     THE ESC CHAIN, in the brief's precedence order (modals-keys-esc.md 3.3 /
     6.2), across the two handlers that implement it:

       1. slash popup open        -> composer handler above: closes the popup
                                     only, text and caret untouched
       2. composer has text       -> composer handler above: clears it
                                     (undoably) and KEEPS focus
       3. composer is empty       -> composer handler above: blurs, which is
                                     "command mode"
       4. reject-reason box open  -> below, and on the box's own handler when
                                     it has the caret: closes the note and
                                     leaves the gate pending
       5. a modal is on top       -> below, first: each screen's own meaning
                                     (a confirm denies, the summary closes,
                                     a text prompt cancels, help and settings
                                     close, the service editor asks Python)
       6. an ask_user is open     -> below, last: DISMISSES the question.
                                     Nothing is sent and nothing is torn
                                     down - the model stays parked and the
                                     box goes back to normal until the next
                                     message answers it. Deliberately last,
                                     so an empty composer spends one Esc on
                                     stage 3 first and the press that
                                     dismisses is never the press that was
                                     meant to leave the box. The TUI has the
                                     same stage one place earlier in its own
                                     chain (dismiss before blur), because its
                                     composer is not auto-blurred by a modal
       7. nothing of the above    -> no-op

     Stage 5 is CHECKED first here and that is not a reordering: a GUI modal
     traps focus behind its scrim, so stages 1-3 cannot be live at the same
     time as one, and the two orders can only ever agree. Textual gets the
     same guarantee from its screen stack. No stage is skipped - every surface
     the brief names exists in this shell. */
  function onDocumentKey(ev) {
    // Whatever a nearer handler already claimed is not this one's business.
    // The composer's Esc stages and the reject note's own keys all
    // preventDefault, so this is how stages 1-3 stop stage 4 from also
    // firing on the way up.
    if (ev.defaultPrevented) return;
    // The inert-letters rule (main-chat.md section 6): a focused text box
    // swallows every bare letter, and it is the ONLY thing keeping y/n/a/x
    // and the session keys from firing into a sentence someone is typing.
    // SELECT counts as typing: a focused <select> uses bare letters to jump
    // to an option, and the service picker is one keystroke away from every
    // session key on the table.
    var tag = ev.target && ev.target.tagName;
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    var plain = !ev.ctrlKey && !ev.altKey && !ev.metaKey;
    // ESC STAGE 5: a modal owns the keyboard while it is up. The
    // session keys would otherwise fire into a screen that is asking a
    // question, and the summary has four keys of its own.
    if (modalUp()) {
      if (typing || !plain) return;
      if (ev.key === "Escape") {
        ev.preventDefault();
        // Each screen's own escape: a page screen just closes; a prompt
        // answers the way that prompt says no (the summary "closes", a
        // confirm denies, a text prompt cancels).
        if (pageModal) closePageModal();
        else if (modalKind === "summary") answer("close");
        else if (modalKind === "text") answer(null);
        else answer(false);
        return;
      }
      // F1 closes the help sheet it opened, exactly as the TUI's HelpScreen
      // answers to f1 as well as escape.
      if (ev.key === "F1" && pageModal === "help") {
        ev.preventDefault();
        closePageModal();
        return;
      }
      var choice = modalKind === "summary" ? summaryKey(ev.key) : null;
      if (choice) {
        ev.preventDefault();
        answer(choice);
      }
      return;
    }
    // ESC STAGE 4: the reject-reason box is open but does not have the caret
    // (the user clicked away, or never typed). Closing it returns to the
    // pending gate without rejecting anything.
    if (ev.key === "Escape" && plain) {
      ev.preventDefault();
      if (rejectOpen) {
        closeReject();
        return;
      }
      // ESC STAGE 6: an ask_user is on the floor and nothing nearer claimed the
      // key - the composer is empty AND already blurred (stages 2 and 3 spend a
      // press each before this one is reachable, so unsent answer text can never
      // be lost to it). Nothing is SENT: Python leaves the model parked on its
      // question and hands the box back, and the next ordinary message answers
      // it with a note saying the user declined and asked for this instead.
      if (awaitingAnswer) {
        api("dismiss_question");
        return;
      }
      // ESC STAGE 7 otherwise: nothing to cancel, nothing happens.
      return;
    }
    // Everything else is the key table's, which is also what the help sheet
    // is drawn from - so a binding cannot exist without being documented, and
    // cannot be documented without existing.
    dispatchKey(ev, typing);
  
  }

  /* == input =============================================================== */

  function send() {
    if (el.composer.disabled) return;
    var text = el.composer.value;
    if (!text.trim()) return;
    // The one send door, so this is the one place the arrows' memory can grow:
    // follow-ups, slash commands and ask_user answers alike, whether the send
    // came from Enter or from the button. Before the clear, or there would be
    // nothing left to remember.
    sentPush(text);
    el.composer.value = "";
    popupHide();
    api("submit", text);
  }

  function boot() {
    el = {
      transcripts: id("transcripts"),
      wintabs: id("wintabs"),
      winRowMaster: id("win-row-master"),
      winRowSub: id("win-row-sub"),
      composer: id("composer"),
      popup: id("cmd-popup"),
      send: id("send"),
      phase: id("phase"),
      loop: id("loop"),
      service: id("service"),
      monitorLink: id("monitor-link"),
      flash: id("flash"),
      flashText: id("flash-text"),
      retryInsert: id("retry-insert"),
      pressEnter: id("press-enter"),
      copyAgain: id("copy-again"),
      docsOpen: id("docs-open"),
      sidebar: id("sidebar"),
      rail: id("rail"),
      sideArmed: id("side-armed"),
      sideRoot: id("side-root"),
      sideRemote: id("side-remote"),
      sideRemoteLines: id("side-remote-lines"),
      reconnectNow: id("reconnect-now"),
      connectRemote: id("connect-remote"),
      attachMonitor: id("attach-monitor"),
      connScrim: id("conn-scrim"),
      connForm: id("conn-form"),
      connSaved: id("conn-saved"),
      connAliases: id("conn-aliases"),
      connTarget: id("conn-target"),
      connPreview: id("conn-preview"),
      connRoot: id("conn-root"),
      connError: id("conn-error"),
      connSteps: id("conn-steps"),
      connFailure: id("conn-failure"),
      connPolicy: id("conn-policy"),
      connSave: id("conn-save"),
      connSaveName: id("conn-save-name"),
      connSaveBtn: id("conn-save-btn"),
      connTabExec: id("conn-tab-exec"),
      connTabMonitor: id("conn-tab-monitor"),
      connExec: id("conn-exec"),
      connMonitor: id("conn-monitor"),
      monAttachedLine: id("mon-attached"),
      monForm: id("mon-form"),
      monSaved: id("mon-saved"),
      monModeDirect: id("mon-mode-direct"),
      monModeSsh: id("mon-mode-ssh"),
      monModeLocal: id("mon-mode-local"),
      monAddress: id("mon-address"),
      monLocalNote: id("mon-local-note"),
      monViaRow: id("mon-via-row"),
      monVia: id("mon-via"),
      monHostTitle: id("mon-host-title"),
      monHost: id("mon-host"),
      monPort: id("mon-port"),
      monToken: id("mon-token"),
      monError: id("mon-error"),
      monFailure: id("mon-failure"),
      monSave: id("mon-save"),
      monSaveName: id("mon-save-name"),
      monSaveBtn: id("mon-save-btn"),
      monSavedNote: id("mon-saved-note"),
      monHint: id("mon-hint"),
      monAttach: id("mon-attach"),
      monEdit: id("mon-edit"),
      monDetach: id("mon-detach"),
      monClose: id("mon-close"),
      connSavedNote: id("conn-saved-note"),
      connHint: id("conn-hint"),
      connConnect: id("conn-connect"),
      connEdit: id("conn-edit"),
      connClose: id("conn-close"),
      mcpBlock: id("mcp-block"),
      mcpRows: id("mcp-rows"),
      serviceName: id("service-name"),
      sideServiceLabel: id("side-service-label"),
      sideProfileNote: id("side-profile-note"),
      sideServiceTitle: id("side-service-title"),
      sideWindowTitle: id("side-window-title"),
      sideRegion: id("side-region"),
      sideSlotNote: id("side-slot-note"),
      sideDetectionTitle: id("side-detection-title"),
      sees: id("sees"),
      seesRows: id("sees-rows"),
      seesSettings: id("sees-settings"),
      seesNote: id("sees-note"),
      logpane: id("logpane"),
      logLines: id("log-lines"),
      keyhints: id("keyhints"),
      statusbar: id("statusbar"),
      run: id("run"),
      runLabel: id("run-label"),
      runRows: id("run-rows"),
      runTailWrap: id("run-tail-wrap"),
      runTail: id("run-tail"),
      gate: id("gate"),
      gateTitle: id("gate-title"),
      gateQueue: id("gate-queue"),
      gatePreview: id("gate-preview"),
      gateAlways: id("gate-always"),
      gateHint: id("gate-hint"),
      gateRejectRow: id("gate-reject-row"),
      gateNote: id("gate-note"),
      askBanner: id("ask-banner"),
      askQuestion: id("ask-question"),
      toasts: id("toasts"),
      scrim: id("scrim"),
      modal: id("modal"),
      modalTitle: id("modal-title"),
      modalBody: id("modal-body"),
      modalActions: id("modal-actions"),
      modalHint: id("modal-hint")
    };

    // One entry per drawn transcript, each with its own follow/park state. The
    // window ids are the Python side's and are read off the DOM rather than
    // spelled twice, so adding a window is one element and no JS.
    panels = {};
    Array.prototype.forEach.call(
      el.transcripts.querySelectorAll(".transcript"),
      function (node) {
        var empty = node.querySelector(".empty");
        panels[node.getAttribute("data-window")] = {
          node: node,
          stick: true,
          parked: false,
          // Remembered so /new can put each panel's own resting line back.
          empty: empty ? empty.textContent : ""
        };
      }
    );

    // The app version still rides in the URL fragment, as it has since slice 1:
    // a hard-coded string here would drift the first time __version__ moved.
    var version = /(?:^|[#&])v=([^&]+)/.exec(window.location.hash || "");
    var slot = id("version");
    if (version && slot) slot.textContent = "v" + decodeURIComponent(version[1]);

    // Per panel: whether the user has scrolled away is a fact about ONE
    // transcript, and the master's must not be forgotten because a sub-agent's
    // was read to the bottom.
    Object.keys(panels).forEach(function (key) {
      var target = panels[key];
      target.node.addEventListener("scroll", function () {
        var box = target.node;
        var bottom = box.scrollHeight - box.scrollTop - box.clientHeight;
        target.stick = bottom < 24;
        if (target.stick) target.parked = false;
      });
    });

    el.composer.addEventListener("keydown", onComposerKey);
    // Every edit re-decides the list AND ends any walk through the send
    // history: what is in the box after a keystroke is the user's, not the
    // entry they had walked back to, so the next ArrowUp starts again from the
    // newest. One listener rather than one per cause, which is what
    // `_text_changed` on the Changed event is on the other side.
    el.composer.addEventListener("input", function () {
      if (!recalling) sentReset();
      syncPopup();
    });
    el.send.addEventListener("click", send);

    // A click anywhere on the run panel is the same request as ctrl+o: the
    // panel is a few rows tall and its only interactive state is that one
    // toggle, so hunting for a disclosure triangle would be ceremony.
    // The exception is a click that ENDED A DRAG: the open panel's tail is
    // command output, which is exactly the kind of text people copy, and
    // releasing the mouse at the end of selecting some of it must not slam the
    // panel shut on the words just selected.
    el.run.addEventListener("click", function () {
      if (draggedOutText()) return;
      toggleRunOutput();
    });

    el.gateApprove = id("gate-approve");
    el.gateReject = id("gate-reject");
    el.gateNoteSend = id("gate-note-send");
    el.gateApprove.addEventListener("click", function () {
      api("decide", "approve", "");
    });
    el.gateAlways.addEventListener("click", function () {
      api("decide", "approve_always", "");
    });
    // Reject opens the reason box; only SUBMITTING it rejects. Pressing the
    // button again while it is open puts the caret back rather than sending an
    // answer the user has not finished writing.
    el.gateReject.addEventListener("click", openReject);
    el.gateNoteSend.addEventListener("click", sendReject);
    el.gateNote.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        sendReject();
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        closeReject(); // back to approval mode, gate still open
      }
    });

    el.retryInsert.addEventListener("click", function (ev) {
      ev.stopPropagation();
      api("retry_insert");
    });
    // The sidebar's two nudges (ui-monitor.md 11.8). No key goes with either:
    // both are "do the thing the automation should have done", which is a
    // deliberate click after looking at the browser, not a reflex.
    el.pressEnter.addEventListener("click", function () {
      api("press_enter");
    });
    el.copyAgain.addEventListener("click", function () {
      api("copy_again");
    });
    // The titlebar's one button. No key goes with it: every F-key is taken and
    // a bare letter belongs to the main screen's shortcuts, so the button IS
    // the binding (and the help sheet carries the other door to the same
    // screen).
    el.docsOpen.addEventListener("click", function () {
      openDocs("");
    });
    // A genuine user pick only: nothing here writes the value back into the
    // picker except paintSidebar, and setting .value programmatically fires no
    // change event - which is the whole of the TUI's _reported_service dance,
    // for free (Sidebar.show_service).
    // The Monitor UI's doors used to be wired here - F2, the titlebar's
    // button and the sidebar's two buttons, all one `calibrate` call that
    // answered with one toast. ui-monitor.md 11.2 deleted every one of them:
    // the window where the pixels are belongs to the monitor PROCESS, and an
    // affordance that can only name another window is not one.
    // -- the SSH connect dialog's own wiring --------------------------------
    // Both text boxes send the WHOLE form on any input and NOTHING comes back:
    // the model owns the values, but repainting an input the caret is in would
    // fight it - the same contract the calibration window's editor keeps.
    el.connectRemote.addEventListener("click", function () {
      api("connect_open");
    });
    el.reconnectNow.addEventListener("click", function () {
      api("reconnect_now");
    });
    [el.connTarget, el.connRoot].forEach(function (input) {
      input.addEventListener("input", function () {
        connPreviewLocal();
        api("connect_fields", el.connTarget.value, el.connRoot.value);
      });
    });
    el.connConnect.addEventListener("click", function () {
      // The form's values go with the press: an input event and a click can
      // reach Python on two different threads, and the press must not race the
      // keystroke that filled the box it is about.
      api("connect_fields", el.connTarget.value, el.connRoot.value);
      api("connect_start");
    });
    el.connEdit.addEventListener("click", function () {
      api("connect_edit");
    });
    el.connClose.addEventListener("click", function () {
      api("connect_cancel");
    });
    el.connSaveBtn.addEventListener("click", function () {
      api("connect_save", el.connSaveName.value);
    });

    // -- the Monitor tab's own wiring ---------------------------------------
    // Which TAB is showing is a round trip like everything else here: the two
    // models live on the Python side and only one of them may be open, so the
    // page asks rather than deciding.
    el.connTabExec.addEventListener("click", function () {
      api("connect_open");
    });
    el.connTabMonitor.addEventListener("click", function () {
      api("monitor_open");
    });
    el.attachMonitor.addEventListener("click", function () {
      api("monitor_open");
    });
    [el.monHost, el.monPort, el.monToken].forEach(function (input) {
      input.addEventListener("input", monFields);
    });
    [el.monModeDirect, el.monModeSsh, el.monModeLocal].forEach(function (radio) {
      radio.addEventListener("change", function () {
        monFields();
        // The mode decides which fields mean anything, so this ONE control
        // repaints rather than waiting for the next event - the alternative is
        // a Via-SSH form still showing a "Monitor host" label.
        el.monViaRow.hidden = !el.monModeSsh.checked;
        el.monAddress.hidden = el.monModeLocal.checked;
        el.monLocalNote.hidden = !el.monModeLocal.checked;
      });
    });
    el.monVia.addEventListener("change", monFields);
    el.monAttach.addEventListener("click", function () {
      // The values go with the press, for connect_start's reason: an input
      // event and a click reach Python on two different threads.
      monFields();
      api("monitor_start");
    });
    el.monEdit.addEventListener("click", function () {
      api("monitor_edit");
    });
    el.monDetach.addEventListener("click", function () {
      api("monitor_disconnect");
    });
    el.monClose.addEventListener("click", function () {
      api("monitor_cancel");
    });
    el.monSaveBtn.addEventListener("click", function () {
      api("monitor_save", el.monSaveName.value);
    });

    // The log pane never scrolls its own way: reading the scroll position is
    // how "following" is decided, so the listener exists only to make the pane
    // focusable-by-wheel behave like any other scroll box.
    el.logLines.addEventListener("wheel", function () {}, { passive: true });

    document.addEventListener("keydown", onDocumentKey);

    // The key hint strip: drawn once, before the first event, so the row is
    // there (and its height reserved) from the empty start screen onwards.
    buildKeyHints();
    // Its fourth trigger, after `state`, `status`/`armed`, the gate and the run
    // panel: the caret itself. A focused text box swallows the bare letters, and
    // the strip has to say so the moment focus moves either way. On focusout the
    // box still owns document.activeElement, so that answer is one tick away.
    document.addEventListener("focusin", paintKeyHints);
    document.addEventListener("focusout", function () {
      window.setTimeout(paintKeyHints, 0);
    });
    // A cheatsheet may not be a way to LOSE the caret: pressing it does nothing,
    // so it must not blur the chat box either (the CSS makes it unselectable;
    // only this can keep the focus where it was).
    el.keyhints.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
    });

    booted = true;
    while (pending.length) receive(pending.shift());
    api("ready");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
