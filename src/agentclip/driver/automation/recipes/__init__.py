"""The loop's two halves, split: what each state DOES, and where each outcome GOES.

docs/design/ui-monitor.md §2.4 and §6.2. One file per
:class:`~agentclip.driver.automation.loop_state.LoopState` - each an
``async def run(ctx) -> Outcome`` written in plain focus/wait/click/observe over
the :class:`~agentclip.driver.monitor.protocol.UIMonitor` - plus one pure table
(:mod:`.transitions`) and one loop (:mod:`.loop`). Nothing here captures a frame,
compares a template or names a tolerance: every one of those is a monitor verb,
and what is left in this package is the MEANING of the answers.

The rules the package is written to, all of them §4's:

* **The fire is one-shot** (§4.1). The loop is a single asyncio task, so a
  second harvest cannot start until the first recipe returns.
* **Ghost ticks never arrive** (§4.2). ``observe()`` refuses to hand out a tick
  captured before the last ``configure``, so no recipe checks a stamp.
* **No lock, anywhere** (§4.4). Every writer below is on the event-loop thread.
* **``os_armed`` gates every action** (§4.5) - checked HERE, before the verb, so
  a disarmed brain never sends one.
* **No blind paste** (§4.6): the delivery clicks a chat box a capture verified,
  or it clicks nothing at all.
"""
