"""The MONITOR UI - the window that runs where the pixels are.

``docs/design/ui-monitor.md`` §2.6 and §6.4. One window holding the service
editor, the ELEMENTS column, the chat-region picker and ``/identify``, built
over a **local** :class:`~agentclip.driver.monitor.local.LocalUIMonitor` - never
over a remote one and never over the ``AutomationController``. Two entry points,
one implementation: ``agentclip --calibrate`` runs it standalone (on the VM, in
split mode), and the Chat UI opens it beside itself in local mode.

Nothing in this package imports ``agentclip.shell.app`` or ``agentclip.shell.chat``:
a Monitor UI process has no session, no engine and no transcript, and the import
graph is where that stays true (tests/test_layering.py:
test_monitor_ui_never_imports_chat_or_app). What it shares with the Chat UI lives
one package over, in ``agentclip.shell.webview``.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentclip.shell.monitor_ui.view import CalibrationMonitor, CalibrationView
from agentclip.shell.monitor_ui.window import (
    CalibrationBridge,
    CalibrationJsApi,
    CalibrationRunner,
    open_calibration_window,
    run_calibration,
)

__all__: Sequence[str] = [
    "CalibrationBridge",
    "CalibrationJsApi",
    "CalibrationMonitor",
    "CalibrationRunner",
    "CalibrationView",
    "open_calibration_window",
    "run_calibration",
]
