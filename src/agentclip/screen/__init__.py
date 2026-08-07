"""OS-level screen interaction: the draw-a-box region picker and the focus click.

Like ``agentclip.clip`` this is a side-effect layer only ``tui``/``cli`` may
import (enforced by tests/test_layering.py). Everything here is stdlib-only:
tkinter for the overlay (in a child process), ctypes for the Windows click.

Detection lives here too, all built on the same GDI capture: ``busy`` (does a
region still look like its calibration baseline?), ``element`` (the same
question packaged as a reusable "this is the thing I was pointed at" type),
``template`` (find an icon inside a tall band) and ``hover`` (where to park the
cursor when the icon only renders under the pointer).
"""
