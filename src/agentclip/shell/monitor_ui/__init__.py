"""The MONITOR UI - the window that runs where the pixels are.

``docs/design/ui-monitor.md`` §2.6, §6.4 and §9.1. One window holding the
service editor, the ELEMENTS column, the chat-region picker, ``/identify`` and -
standalone - the **Serve panel**, built over a **local**
:class:`~agentclip.driver.monitor.local.LocalUIMonitor`: never over a remote one
and never over the ``AutomationController``.

Two entry points, one implementation:

* ``agentclip-monitor`` (``__main__.py`` -> :func:`~agentclip.shell.monitor_ui.window.run_monitor_ui`)
  is the standalone one, on the machine with the pixels. It owns the clipboard
  and it has the Serve panel: an address, a port, a token, and the decision
  about who may drive this desktop.
* The Chat UI opens the same window beside itself in local mode, through
  :func:`~agentclip.shell.monitor_ui.window.open_calibration_window`, over the
  monitor it is already driving and with no Serve panel - a second listener onto
  the same mouse is not a feature.

Nothing in this package imports ``agentclip.shell.app`` or ``agentclip.shell.chat``:
a Monitor UI process has no session, no engine and no transcript, and the import
graph is where that stays true (tests/test_layering.py:
test_monitor_ui_never_imports_chat_or_app). What it shares with the Chat UI lives
one package over, in ``agentclip.shell.webview``.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentclip.shell.monitor_ui.serve import ServePanel
from agentclip.shell.monitor_ui.view import CalibrationMonitor, CalibrationView
from agentclip.shell.monitor_ui.window import (
    CalibrationBridge,
    CalibrationJsApi,
    CalibrationRunner,
    open_calibration_window,
    run_monitor_ui,
)

__all__: Sequence[str] = [
    "CalibrationBridge",
    "CalibrationJsApi",
    "CalibrationMonitor",
    "CalibrationRunner",
    "CalibrationView",
    "ServePanel",
    "open_calibration_window",
    "run_monitor_ui",
]
