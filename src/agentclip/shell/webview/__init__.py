"""Shared pywebview plumbing: what BOTH windows are made of, and nothing else.

There are two webview windows in this project - the **Chat UI**
(``shell/chat/``) the user talks to, and the **Monitor UI**
(``shell/monitor_ui/``) that runs where the pixels are - and this package holds
the parts neither of them owns:

* :mod:`~agentclip.shell.webview.bridge` - the one-FIFO-one-drainer event bridge
  into a page and the ``js_api`` marshalling shim out of it;
* :mod:`~agentclip.shell.webview.service_editor` - the service editor's MODEL,
  which both windows show;
* :mod:`~agentclip.shell.webview.assets` - resolving a package's ``assets/``
  directory and its ``file://`` entry URL.

The direction is what matters: ``chat`` and ``monitor_ui`` both import THIS
package, and nothing here imports either of them. That is what lets the Monitor
UI run in a process that has no session, no engine and no transcript
(``tests/test_layering.py``).
"""

from __future__ import annotations
