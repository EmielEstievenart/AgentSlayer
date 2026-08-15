"""The pywebview desktop shell: AgentClip's second UI, beside ``agentclip.shell.tui``.

A native window (WebView2 on Windows) rendering the hand-written HTML/CSS/JS in
``assets/``, with Python in the same process - so this shell drives exactly the
same :class:`~agentclip.shell.app.SessionController` and
:class:`~agentclip.driver.automation.AutomationController` the Textual TUI drives, and
neither shell owns any behavior the other cannot have
(docs/design/gui.md sections 0 and 2).

May import ``pywebview`` (and it is the ONLY package that may); must never
import ``textual`` - the two shells are siblings, not layers, and a Textual
import here would mean the GUI is somehow built on the TUI. Enforced by
tests/test_layering.py, twice.

Nothing here is loaded by a TUI launch: ``cli.main`` imports this package only
on ``--gui``, and ``shell.py`` imports pywebview itself lazily inside its
functions, so an install without the ``gui`` extra is unaffected.
"""

from __future__ import annotations
