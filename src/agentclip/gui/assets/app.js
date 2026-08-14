/* The GUI shell's frontend: one receiver, one renderer keyed on event type.

   The Python side is agentclip/gui/bridge.py, which calls exactly one function
   here - window.agentclip.receive(event) - from a single drainer thread, so
   events arrive in the order they were raised and this file never has to think
   about ordering. Going the other way, everything the user does ends in one of
   the five window.pywebview.api calls at the bottom.

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
  var gateOpen = false;
  var gateAlwaysOffered = false;
  var modalId = null;

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
    append(block("ev ev-note", escapeHtml(event.text)), false);
  }

  /* == run panel =========================================================== */

  function paintRunRows() {
    el.runRows.innerHTML = "";
    Object.keys(runRows)
      .sort(function (a, b) {
        return Number(a) - Number(b);
      })
      .forEach(function (key) {
        var row = runRows[key];
        var li = document.createElement("li");
        li.className = "run-row" + (row.glyph === "▶" ? " running" : "");
        li.textContent = row.glyph + " " + row.tool + " " + (row.detail || "");
        el.runRows.appendChild(li);
      });
  }

  function paintRunTail() {
    var lines = streamingCall !== null ? runOutput[streamingCall] : null;
    if (!lines || !lines.length) {
      el.runTail.hidden = true;
      return;
    }
    el.runTail.hidden = false;
    el.runTail.textContent = lines.slice(-12).join("\n");
    el.runTail.scrollTop = el.runTail.scrollHeight;
  }

  /* == gate ================================================================ */

  function showGate(event) {
    gateOpen = true;
    gateAlwaysOffered = Boolean(event.always_label);
    el.gateTitle.textContent = event.title;
    el.gateQueue.textContent = event.queue || "";
    el.gatePreview.textContent = event.preview || event.auto_reason || "(no preview)";
    el.gateAlways.hidden = !gateAlwaysOffered;
    if (gateAlwaysOffered) el.gateAlways.textContent = event.always_label + " (a)";
    el.gateHint.textContent = "y approve · n reject" + (gateAlwaysOffered ? " · a always" : "");
    el.gateRejectRow.hidden = true;
    el.gateNote.value = "";
    el.gate.hidden = false;
  }

  function hideGate() {
    gateOpen = false;
    gateAlwaysOffered = false;
    el.gate.hidden = true;
    el.gateRejectRow.hidden = true;
  }

  function openReject() {
    if (!gateOpen) return;
    el.gateRejectRow.hidden = false;
    el.gateNote.focus();
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
    if (event.composer_enabled && document.activeElement !== el.gateNote) {
      el.composer.focus();
    }
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
        if (event.loop !== undefined) el.loop.textContent = event.loop;
        if (event.armed !== undefined) el.armed.hidden = Boolean(event.armed);
        return;
      case "armed":
        el.armed.hidden = Boolean(event.armed);
        return;
      case "gate":
        if (event.open) showGate(event);
        else hideGate();
        return;
      case "run":
        if (event.running) {
          runRows = {};
          runOutput = {};
          streamingCall = null;
          (event.calls || []).forEach(function (call) {
            runRows[call.call_id] = call;
          });
          el.runLabel.textContent = event.label + "  (ctrl+x cancels)";
          paintRunRows();
          paintRunTail();
          el.run.hidden = false;
        } else {
          el.run.hidden = true;
          runRows = {};
          runOutput = {};
          streamingCall = null;
        }
        return;
      case "run_call":
        if (event.phase === "started") {
          var row = runRows[event.call_id] || { call_id: event.call_id, detail: event.detail };
          row.tool = event.tool || row.tool;
          row.detail = event.detail || row.detail;
          row.glyph = "▶";
          runRows[event.call_id] = row;
          streamingCall = row.tool === "run_command" ? event.call_id : null;
        } else {
          if (runRows[event.call_id]) runRows[event.call_id].glyph = event.glyph;
          if (streamingCall === event.call_id) streamingCall = null;
        }
        paintRunRows();
        paintRunTail();
        return;
      case "run_output":
        var buffer = runOutput[event.call_id] || (runOutput[event.call_id] = [""]);
        var chunk = String(event.chunk).replace(/\r\n?/g, "\n").split("\n");
        buffer[buffer.length - 1] += chunk.shift();
        Array.prototype.push.apply(buffer, chunk);
        if (buffer.length > 400) runOutput[event.call_id] = buffer.slice(-400);
        paintRunTail();
        return;
      case "composer_reset":
        el.composer.value = "";
        return;
      case "toast":
        toast(event);
        return;
      case "flash":
        el.flash.hidden = !event.show;
        if (event.show) el.flash.textContent = event.text;
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
      case "elements":
      case "harness":
        // Nothing draws these yet - the sidebar, the ELEMENTS column and the
        // log pane are later increments. They are dispatched (rather than
        // dropped upstream) so the renderer is the only thing those increments
        // have to grow.
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
      armed: id("armed"),
      flash: id("flash"),
      run: id("run"),
      runLabel: id("run-label"),
      runRows: id("run-rows"),
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
      }
    });
    el.send.addEventListener("click", send);

    el.gateApprove = id("gate-approve");
    el.gateReject = id("gate-reject");
    el.gateApprove.addEventListener("click", function () {
      api("decide", "approve", "");
    });
    el.gateAlways.addEventListener("click", function () {
      api("decide", "approve_always", "");
    });
    el.gateReject.addEventListener("click", openReject);
    el.gateNote.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        api("decide", "reject", el.gateNote.value);
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        el.gateRejectRow.hidden = true;
      }
    });

    document.addEventListener("keydown", function (ev) {
      var tag = ev.target && ev.target.tagName;
      var typing = tag === "INPUT" || tag === "TEXTAREA";
      if (ev.ctrlKey && ev.key === "x" && !typing) {
        api("cancel");
        return;
      }
      if (!gateOpen || typing || ev.ctrlKey || ev.altKey || ev.metaKey) return;
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
