/* The GUI shell's frontend: one receiver, one renderer keyed on event type.

   The Python side is agentclip/gui/bridge.py, which calls exactly one function
   here - window.agentclip.receive(event) - from a single drainer thread, so
   events arrive in the order they were raised and this file never has to think
   about ordering. Going the other way, everything the user does ends in a
   window.pywebview.api call, and every one of those lands on the same
   controller method the TUI's key binding does.

   Two toggles never leave this file: F3 (the sidebar) and F8 (the log pane) are
   pure show/hide of a page element. /log comes back the other way as a `toggle`
   event, so the command and the key stay one implementation.

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
  var stick = true;
  var parked = false;
  var runRows = {};
  var runOutput = {};
  var streamingCall = null;
  var tailOpen = false;
  var gateOpen = false;
  var gateAlwaysOffered = false;
  var rejectOpen = false;
  var modalId = null;
  // The harness decision log, page-side. The deque below is the source of
  // truth; this is a view of it that happens to keep its own copy, because the
  // bridge is one-way and a pane revealed after an hour must show the whole
  // tail rather than whatever arrived since. Bounded at the deque's own number
  // (agentclip/automation/harness_log.py, HARNESS_LOG_MAX): a debugging tail,
  // not an archive.
  var LOG_MAX = 500;
  var logEntries = [];
  var logOpen = false;
  // agentclip.automation.harness_log.EMPTY_LOG_LINE - a log that explains its
  // own silence, so "nothing has happened" is never read as "nothing works".
  var EMPTY_LOG_LINE =
    "nothing logged yet - the harness writes here as it moves through the loop " +
    "(paste, send, generate, copy).";
  // What the picker's <option> list currently says, so a repaint that changes
  // nothing does not rebuild a <select> the user may have open.
  var serviceOptions = "";

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
     Hand-written, and only what a transcript actually contains: fenced code,
     headings, bullet/ordered lists, paragraphs, and the four inline forms.
     HTML is escaped FIRST and never unescaped, so no model output can inject
     markup - correctness over features, per docs/design/gui.md 2. */

  function inlineMarkdown(text) {
    // Code spans are lifted out FIRST, behind a marker the text cannot contain
    // (escapeHtml has already run, and a NUL survives no escaping), so
    // `*not italic*` inside backticks stays exactly what the model wrote.
    var codes = [];
    var out = escapeHtml(text).replace(/`([^`]+)`/g, function (_, code) {
      codes.push(code);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*\w])\*([^*]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_\w])_([^_]+)_/g, "$1<em>$2</em>");
    return out.replace(/\u0000(\d+)\u0000/g, function (_, n) {
      return "<code>" + codes[Number(n)] + "</code>";
    });
  }

  var BULLET = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
  var HEADING = /^(#{1,6})\s+(.*)$/;
  var FENCE = /^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$/;

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
          BULLET.test(lines[i])
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

  /* == transcript ========================================================== */

  function append(node, beat) {
    var empty = el.transcript.querySelector(".empty");
    if (empty) empty.parentNode.removeChild(empty);
    el.transcript.appendChild(node);
    // Fit-or-park (main-chat.md 6): an event taller than the viewport parks
    // with its top at the top so the user reads from the first line; anything
    // that fits pins the panel to the bottom unless they scrolled away.
    var viewport = el.transcript.clientHeight;
    if (viewport > 0 && node.offsetHeight > viewport) {
      el.transcript.scrollTop = node.offsetTop;
      parked = true;
      stick = false;
      return;
    }
    if (parked && !beat) return;
    parked = false;
    if (stick) el.transcript.scrollTop = el.transcript.scrollHeight;
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
    var blocks = el.transcript.querySelectorAll("details");
    if (!blocks.length) return;
    var last = blocks[blocks.length - 1];
    last.open = !last.open;
    if (last.open) last.scrollIntoView({ block: "nearest" });
  }

  function addTranscript(event) {
    if (event.kind === "user" || event.kind === "prose") {
      var label = event.label || (event.kind === "user" ? "you" : "assistant");
      append(
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
      append(block("ev ev-call", html), false);
      return;
    }
    if (event.kind === "outbound") {
      var note = escapeHtml(event.note);
      if (event.parts > 1) note += " · part 1 of " + event.parts;
      append(
        block(
          "ev ev-call",
          '<div class="ev-summary">' +
            note +
            "</div>" +
            details("outbound turn " + event.turn + " (" + event.chars + " chars)", event.payload)
        ),
        false
      );
      return;
    }
    if (event.kind === "error") {
      append(block("ev ev-error", escapeHtml(event.text)), false);
      return;
    }
    append(block("ev " + noteClass(event.text), escapeHtml(event.text)), false);
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
  }

  function hideGate() {
    gateOpen = false;
    gateAlwaysOffered = false;
    closeReject();
    el.gate.hidden = true;
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
    el.scrim.hidden = true;
    if (target) api("prompt", target, value);
  }

  function showModal(event) {
    modalId = event.prompt_id;
    el.modalTitle.textContent = event.title || "";
    el.modalBody.innerHTML = "";
    el.modalActions.innerHTML = "";
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
    } else if (event.modal === "summary") {
      var rows = (event.rows || [])
        .map(function (pair) {
          return "<tr><th>" + escapeHtml(pair[0]) + "</th><td>" + escapeHtml(pair[1]) + "</td></tr>";
        })
        .join("");
      el.modalBody.innerHTML =
        "<table class='stats'>" + rows + "</table>" + renderMarkdown(event.summary || "");
      el.modalActions.appendChild(button("New session", "new"));
      el.modalActions.appendChild(button("Undo last turn", "undo"));
      el.modalActions.appendChild(button("Export log", "export"));
      el.modalActions.appendChild(button("Close", "close"));
    }
    el.scrim.hidden = false;
  }

  function showPayload(event) {
    // park_off_clipboard's GUI equivalent: the clipboard provider refused the
    // payload and this shell has no OSC-52, so the text is put somewhere the
    // user can select it (docs/design/gui.md 2).
    modalId = null;
    el.modalTitle.textContent = "Copy this payload by hand";
    el.modalBody.innerHTML = "<pre class='payload'>" + escapeHtml(event.text) + "</pre>";
    el.modalActions.innerHTML = "";
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.addEventListener("click", function () {
      el.scrim.hidden = true;
    });
    el.modalActions.appendChild(close);
    el.scrim.hidden = false;
  }

  /* == sidebar ==============================================================
     Everything below RENDERS; nothing decides. The STATE rail's brightness came
     out of LOOP_TRANSITIONS on the Python side, the status bar's segments came
     composed and in order, and the detection lines came worded - which is what
     keeps the two shells from growing two ideas of what any of them mean
     (docs/design/ui-briefs/sidebar-status-log.md section 3). */

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
    el.sideServiceLabel.textContent = event.service_label || "";
    el.sideProfileNote.textContent = event.profile_note || "";
    el.sideRegion.textContent = event.region || "";
    el.sideSlotNote.textContent = event.slot_note || "";
    el.sideDetectionTitle.textContent = event.detection_title || "DETECTION";
    var options = JSON.stringify(event.services || []);
    if (options !== serviceOptions) {
      serviceOptions = options;
      el.serviceSelect.innerHTML = "";
      (event.services || []).forEach(function (pair) {
        var option = document.createElement("option");
        option.value = pair[0];
        option.textContent = pair[1];
        el.serviceSelect.appendChild(option);
      });
    }
    if (event.service) el.serviceSelect.value = event.service;
    // Locked while a session owns the services: the master's budget is baked
    // into its Engine at bootstrap, so the preset may not move mid-session.
    el.serviceSelect.disabled = Boolean(event.locked);
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

  function paintStatus(event) {
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

  /* == chrome ============================================================== */

  function paintState(event) {
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
    if (event.composer_enabled && !rejectOpen) el.composer.focus();
  }

  /* == dispatch ============================================================ */

  function dispatch(event) {
    switch (event.type) {
      case "transcript":
        addTranscript(event);
        return;
      case "transcript_clear":
        el.transcript.innerHTML = '<p class="empty">Describe a task below to start a session.</p>';
        parked = false;
        stick = true;
        return;
      case "focus_session":
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
          el.scrim.hidden = true;
        }
        return;
      case "payload":
        showPayload(event);
        return;
      case "detection":
        paintDetection(event);
        return;
      case "harness":
        appendLog(event);
        return;
      case "toggle":
        // /log's way in. The same call F8 makes, deliberately: two ways to ask
        // for one thing, one implementation of it.
        if (event.what === "log") toggleLog();
        return;
      case "elements":
        // Nothing draws these yet - the ELEMENTS column is increment 6. It is
        // dispatched (rather than dropped upstream) so the renderer is the only
        // thing that increment has to grow.
        return;
      default:
        return;
    }
  }

  /* == input =============================================================== */

  function send() {
    if (el.composer.disabled) return;
    var text = el.composer.value;
    if (!text.trim()) return;
    el.composer.value = "";
    api("submit", text);
  }

  function boot() {
    el = {
      transcript: id("transcript"),
      composer: id("composer"),
      send: id("send"),
      phase: id("phase"),
      loop: id("loop"),
      service: id("service"),
      flash: id("flash"),
      flashText: id("flash-text"),
      retryInsert: id("retry-insert"),
      sidebar: id("sidebar"),
      rail: id("rail"),
      sideArmed: id("side-armed"),
      sideRoot: id("side-root"),
      mcpBlock: id("mcp-block"),
      mcpRows: id("mcp-rows"),
      serviceSelect: id("service-select"),
      sideServiceLabel: id("side-service-label"),
      sideProfileNote: id("side-profile-note"),
      setRegion: id("set-region"),
      sideRegion: id("side-region"),
      sideSlotNote: id("side-slot-note"),
      sideDetectionTitle: id("side-detection-title"),
      logpane: id("logpane"),
      logLines: id("log-lines"),
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
      toasts: id("toasts"),
      scrim: id("scrim"),
      modalTitle: id("modal-title"),
      modalBody: id("modal-body"),
      modalActions: id("modal-actions")
    };

    // The app version still rides in the URL fragment, as it has since slice 1:
    // a hard-coded string here would drift the first time __version__ moved.
    var version = /(?:^|[#&])v=([^&]+)/.exec(window.location.hash || "");
    var slot = id("version");
    if (version && slot) slot.textContent = "v" + decodeURIComponent(version[1]);

    el.transcript.addEventListener("scroll", function () {
      var bottom =
        el.transcript.scrollHeight - el.transcript.scrollTop - el.transcript.clientHeight;
      stick = bottom < 24;
      if (stick) parked = false;
    });

    // Enter sends, Shift+Enter is a newline. The TUI uses ctrl+j for the
    // newline because Enter is its send key inside a TextArea; this is the
    // web-native convention and a deliberate shell-idiom difference
    // (docs/design/gui.md 2), not drift.
    el.composer.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        send();
        return;
      }
      // Esc's two composer stages (modals-keys-esc.md section 3.3): text
      // present clears the box and KEEPS focus so the rewrite can start
      // immediately; an empty box blurs, which is what makes the single-key
      // shortcuts (w/c/i/r/x, y/n/a) reachable at all - the inert-while-typing
      // rule is only safe because there is a way to stop typing. The clear goes
      // through execCommand so the browser's own undo stack survives it;
      // assigning .value would throw the paragraph away for good.
      if (ev.key === "Escape") {
        ev.preventDefault();
        if (!el.composer.value) {
          el.composer.blur();
          return;
        }
        el.composer.select();
        if (!document.execCommand || !document.execCommand("delete")) {
          el.composer.value = "";
        }
      }
    });
    el.send.addEventListener("click", send);

    // A click anywhere on the run panel is the same request as ctrl+o: the
    // panel is a few rows tall and its only interactive state is that one
    // toggle, so hunting for a disclosure triangle would be ceremony.
    el.run.addEventListener("click", toggleRunOutput);

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
    // A genuine user pick only: nothing here writes the value back into the
    // picker except paintSidebar, and setting .value programmatically fires no
    // change event - which is the whole of the TUI's _reported_service dance,
    // for free (Sidebar.show_service).
    el.serviceSelect.addEventListener("change", function () {
      api("service", el.serviceSelect.value);
    });
    el.setRegion.addEventListener("click", function () {
      toast({
        message:
          "drawing the chat region lands with the elements panel increment - " +
          "until then the payload goes to the clipboard and the paste is yours",
        severity: "warning"
      });
    });
    // The log pane never scrolls its own way: reading the scroll position is
    // how "following" is decided, so the listener exists only to make the pane
    // focusable-by-wheel behave like any other scroll box.
    el.logLines.addEventListener("wheel", function () {}, { passive: true });

    document.addEventListener("keydown", function (ev) {
      // The inert-letters rule (main-chat.md section 6): a focused text box
      // swallows every bare letter, and it is the ONLY thing keeping y/n/a/x
      // and the five session keys from firing into a sentence someone is
      // typing. The function keys, shift+tab and the ctrl-chords are the TUI's
      // priority bindings and fire regardless of focus.
      // SELECT counts as typing: a focused <select> uses bare letters to jump
      // to an option, and the service picker is one keystroke away from every
      // session key on this list.
      var tag = ev.target && ev.target.tagName;
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (!ev.ctrlKey && !ev.altKey && !ev.metaKey) {
        if (ev.key === "F3") {
          ev.preventDefault();
          toggleSidebar();
          return;
        }
        if (ev.key === "F5") {
          // preventDefault first: F5 is the browser's reload, and a WebView2
          // window that reloaded here would drop the whole session's chrome.
          ev.preventDefault();
          api("armed", null); // null = toggle, the bare /armed and F5
          return;
        }
        if (ev.key === "F8") {
          ev.preventDefault();
          toggleLog();
          return;
        }
      }
      if (ev.key === "Tab" && ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
        // Never gated - it must work pre-session and mid-turn, which are the
        // two moments the feature exists for (modals-keys-esc.md section 6.3).
        ev.preventDefault();
        api("mode");
        return;
      }
      if (ev.ctrlKey && !ev.altKey && !ev.metaKey) {
        if (ev.key === "x" || ev.key === "X") {
          ev.preventDefault();
          api("cancel");
        } else if (ev.key === "o" || ev.key === "O") {
          ev.preventDefault();
          toggleRunOutput();
        }
        return;
      }
      if (typing || ev.ctrlKey || ev.altKey || ev.metaKey) return;
      if (ev.key === "x") {
        toggleLastBlock();
        return;
      }
      // The session keys. Each one is a single controller call on the far side
      // and every refusal lives there, so a press that cannot do anything comes
      // back as a toast rather than being swallowed here.
      if (ev.key === "w") {
        api("watch");
        return;
      }
      if (ev.key === "c") {
        api("recopy");
        return;
      }
      if (ev.key === "i") {
        api("ingest");
        return;
      }
      if (ev.key === "r") {
        api("reinstruct");
        return;
      }
      if (!gateOpen) return;
      if (ev.key === "y") api("decide", "approve", "");
      else if (ev.key === "n") openReject();
      else if (ev.key === "a" && gateAlwaysOffered) api("decide", "approve_always", "");
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
