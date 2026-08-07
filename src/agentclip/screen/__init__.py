"""OS-level screen interaction: the draw-a-box region picker and the focus click.

Like ``agentclip.clip`` this is a side-effect layer only ``tui``/``cli`` may
import (enforced by tests/test_layering.py). Everything here is stdlib-only:
tkinter for the overlay (in a child process), ctypes for the Windows click.
"""
