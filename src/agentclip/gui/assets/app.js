/* The GUI shell's script. Slice 1 has no bridge to Python yet, so the only
   thing it does is show the version agentclip.gui.shell put in the URL
   fragment (#v=0.1.0) - a hard-coded string here would drift the first time
   __version__ moved.

   A classic script, not an ES module: the page is loaded from a file:// URL and
   Chromium refuses module scripts from that origin (docs/design/gui.md 2). */

(function () {
  "use strict";

  function appVersion() {
    var match = /(?:^|[#&])v=([^&]+)/.exec(window.location.hash || "");
    return match ? decodeURIComponent(match[1]) : "";
  }

  function ready() {
    var version = appVersion();
    var slot = document.getElementById("version");
    if (slot && version) {
      slot.textContent = "v" + version;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }
})();
