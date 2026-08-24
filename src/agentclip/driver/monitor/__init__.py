"""The UI monitor: pixels, matching, debounce, mouse, keyboard, clipboard.

The VM half of docs/design/ui-monitor.md. Layering: ``driver/automation`` ->
``driver/monitor`` -> ``driver/screen``, ``driver/clip``; nothing here imports
``driver/automation``.
"""
