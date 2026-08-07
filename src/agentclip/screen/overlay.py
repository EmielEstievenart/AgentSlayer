"""The draw-a-box overlay: a translucent fullscreen window to drag a rectangle on.

Runs in ITS OWN PROCESS, spawned by screen.picker via the hidden
``agentclip --pick-region`` CLI flag. It cannot share a process with the
Textual app: tkinter insists on the main thread and runs its own event loop.
The CLI prints the result in the region wire format; this module only returns
it.

tkinter is imported lazily so a Linux install without the Tk bindings can run
everything else - the ImportError surfaces as a per-use CLI error instead of
killing the app at startup.
"""

from __future__ import annotations

import sys

from agentclip.screen.focus import make_dpi_aware
from agentclip.screen.region import ScreenRegion

_MIN_DRAG_PX = 8  # anything smaller is a stray click, not a drawn box
# Callers that pick a different kind of region (e.g. the busy-detector's
# stop-button square) pass their own instruction instead.
DEFAULT_PROMPT = "Drag a box around the AI chat's input area · Esc cancels"


def _virtual_screen_bounds() -> tuple[int, int, int, int] | None:
    """(left, top, width, height) of the whole multi-monitor desktop (Windows);
    None where tkinter's primary-screen metrics are the best we can do."""
    if sys.platform != "win32":
        return None
    import ctypes

    metrics = ctypes.windll.user32.GetSystemMetrics
    # SM_XVIRTUALSCREEN..SM_CYVIRTUALSCREEN
    return (metrics(76), metrics(77), metrics(78), metrics(79))


def run_overlay(prompt: str | None = None) -> ScreenRegion | None:
    """Show the overlay; block until a box is dragged (region) or Esc (None).

    ``prompt`` is the instruction drawn across the top (default: DEFAULT_PROMPT).
    """
    import tkinter as tk

    make_dpi_aware()  # before Tk() so its geometry is physical pixels
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.3)
    bounds = _virtual_screen_bounds()
    if bounds is not None:
        left, top, width, height = bounds
    else:
        left, top = 0, 0
        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{width}x{height}+{left}+{top}")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        width // 2,
        40,
        fill="white",
        font=("Segoe UI", 14),
        text=prompt or DEFAULT_PROMPT,
    )

    picked: list[ScreenRegion] = []
    start: dict[str, int] = {}
    rect: list[int] = []

    def on_press(event: tk.Event) -> None:
        start["x"], start["y"] = event.x, event.y
        rect[:] = [
            canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00ff88", width=2)
        ]

    def on_motion(event: tk.Event) -> None:
        if rect:
            canvas.coords(rect[0], start["x"], start["y"], event.x, event.y)

    def on_release(event: tk.Event) -> None:
        if not rect:
            return
        box_w, box_h = abs(event.x - start["x"]), abs(event.y - start["y"])
        if box_w < _MIN_DRAG_PX or box_h < _MIN_DRAG_PX:
            canvas.delete(rect[0])  # stray click: stay up for another try
            rect.clear()
            return
        # Window-relative -> virtual-screen coordinates (window sits at left/top).
        picked.append(
            ScreenRegion(
                left + min(start["x"], event.x), top + min(start["y"], event.y), box_w, box_h
            )
        )
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_motion)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", lambda _event: root.destroy())
    root.focus_force()
    root.mainloop()
    return picked[0] if picked else None
