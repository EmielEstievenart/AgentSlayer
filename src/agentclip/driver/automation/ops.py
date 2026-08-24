"""What one click can come to - re-exported.

:class:`ElementClick` is the verdict of the find-then-click primitive, and it
moved to :mod:`agentclip.driver.monitor.protocol` in phase 2 with the primitive
itself (``UIMonitor.click_element``, docs/design/ui-monitor.md §2.3): four of its
six values are pixel verdicts the monitor answers, and an enum has to live where
the thing that produces it does. It stays reachable here because the brain and
its suites name it at this address.

The OS adapter that used to live beside it (``ScreenOps``) went the same way one
phase earlier - :mod:`agentclip.driver.monitor.ops`.
"""

from __future__ import annotations

from agentclip.driver.monitor.beats import NEW_CHAT_SETTLE_S  # noqa: F401
from agentclip.driver.monitor.protocol import ElementClick  # noqa: F401
