"""The Shell: the user-facing surfaces AgentClip presents, and what backs them.

``app`` is the UI-agnostic session controller; ``tui`` and ``gui`` are the two
shells that drive it - a Textual terminal app and a webview desktop app. A shell
may reach down into driver/executor/engine, never the other way round (enforced
by tests/test_layering.py).
"""
