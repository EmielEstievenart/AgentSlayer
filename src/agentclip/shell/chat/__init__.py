"""The CHAT UI: what the user looks at and types into.

A native window (WebView2 on Windows) rendering the hand-written HTML/CSS/JS in
``assets/``, with Python in the same process - so this shell drives the
:class:`~agentclip.shell.app.SessionController` and
:class:`~agentclip.driver.automation.AutomationController` through the same
ports any other frontend would (docs/design/gui.md sections 0 and 2). It was the
second of two for a while; phase 6 of docs/design/ui-monitor.md deleted the
first, and the ports stayed exactly where they were.

May import ``pywebview``, and it is the ONLY package that may (enforced by
tests/test_layering.py).

``cli.main`` imports this package inside the function that opens the window, and
``shell.py`` imports pywebview itself lazily inside its own functions, so the
flags that answer a question without a window - ``--version``,
``--list-matchers``, ``--pick-region`` - never pay for the optional ``gui``
extra, and an install without it still reaches them.
"""

from __future__ import annotations
