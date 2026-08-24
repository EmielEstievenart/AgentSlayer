"""The Shell: the user-facing surfaces AgentClip presents, and what backs them.

``app`` is the UI-agnostic session controller; ``gui`` is the shell that drives
it - a webview desktop app. It had a sibling, a Textual terminal app, until
phase 6 of docs/design/ui-monitor.md deleted it; ``app`` stays UI-agnostic
anyway, because that is what let one of the two go without the other noticing. A
shell may reach down into driver/executor/engine, never the other way round
(enforced by tests/test_layering.py).
"""
