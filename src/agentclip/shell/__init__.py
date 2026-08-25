"""The Shell: the user-facing surfaces AgentClip presents, and what backs them.

``app`` is the UI-agnostic session controller; ``chat`` is the **Chat UI** that
drives it - the pywebview window the user looks at and types into. ``monitor_ui``
is the **Monitor UI**, the window that runs where the pixels are, and ``webview``
is the pywebview plumbing both of those are made of (see AGENTS.md for the
vocabulary). ``chat`` had a sibling, a Textual terminal app, until phase 6 of
docs/design/ui-monitor.md deleted it; ``app`` stays UI-agnostic anyway, because
that is what let one of the two go without the other noticing. A shell may reach
down into driver/executor/engine, never the other way round (enforced by
tests/test_layering.py).
"""
