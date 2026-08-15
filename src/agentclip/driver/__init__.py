"""The Driver: everything AgentClip does TO the desktop chat app it operates.

``automation`` is the loop that watches, clicks, pastes and harvests; ``screen``
and ``clip`` are the OS seams it is made of. A UI shell may drive the driver,
never the other way round (enforced by tests/test_layering.py).
"""
