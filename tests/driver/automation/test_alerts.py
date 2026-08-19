"""The attention alarm, with a fake tone generator and no sound card.

Two things are worth asserting about a beep, and neither is the beep: WHEN it
starts and - much more importantly - when it stops. An alarm that outlives the
state it was raised for is worse than no alarm, because the user learns to
ignore it.

The player is injected everywhere here. Nothing in this file may reach
``winsound`` or write a BEL to a terminal: the suite runs while the user is at
the machine, which is the same rule the OS gate enforces for clicks and
keystrokes (AGENTS.md, "Running OS-touching tests").
"""

from __future__ import annotations

import threading
import time

from agentclip.driver.automation.alerts import AttentionAlarm

# Long enough that a scheduler hiccup does not fail a test, short enough that
# the file stays fast: every wait below is a deadline, never a fixed sleep.
DEADLINE_S = 2.0
# The repeat the tests arm with. Real users type seconds; the alarm takes a
# float precisely so a suite does not have to wait one.
FAST_REPEAT_S = 0.01


class FakePlayer:
    """Every uh-oh that was asked for, counted, with an event per sound so a
    test can wait for one instead of sleeping for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sounds = 0
        self.sounded = threading.Event()

    def __call__(self) -> None:
        with self._lock:
            self.sounds += 1
        self.sounded.set()

    @property
    def count(self) -> int:
        with self._lock:
            return self.sounds

    def wait_for(self, target: int) -> bool:
        """True once at least ``target`` sounds have been made."""
        deadline = time.monotonic() + DEADLINE_S
        while time.monotonic() < deadline:
            if self.count >= target:
                return True
            time.sleep(0.005)
        return False


def test_arming_sounds_once() -> None:
    player = FakePlayer()
    alarm = AttentionAlarm(player)

    alarm.arm()

    assert player.wait_for(1)
    assert alarm.armed is True


def test_a_lone_uh_oh_does_not_repeat() -> None:
    """``alert_repeat_seconds`` at zero is one sound per attention state - the
    default, and the only one that is not an alarm clock."""
    player = FakePlayer()
    alarm = AttentionAlarm(player)

    alarm.arm(repeat_seconds=0)
    assert player.wait_for(1)
    time.sleep(0.05)

    assert player.count == 1


def test_a_repeat_keeps_saying_it() -> None:
    player = FakePlayer()
    alarm = AttentionAlarm(player)

    alarm.arm(repeat_seconds=FAST_REPEAT_S)

    assert player.wait_for(3)
    alarm.disarm()


def test_disarming_stops_the_repeat() -> None:
    """The whole point: the loop left the attention state, so the noise ends -
    promptly, from inside the gap rather than after it."""
    player = FakePlayer()
    alarm = AttentionAlarm(player)
    alarm.arm(repeat_seconds=FAST_REPEAT_S)
    assert player.wait_for(2)

    alarm.disarm()
    settled = player.count
    time.sleep(0.1)  # many repeats' worth, had anything survived

    # One tone may already have been in flight when the disarm landed.
    assert player.count <= settled + 1
    assert alarm.armed is False


def test_arming_twice_is_one_alarm() -> None:
    """Armed is a STATE, not an event: a loop that walks from one attention
    state straight into another (a failed paste that becomes a manual copy) is
    still one uh-oh and one schedule."""
    player = FakePlayer()
    alarm = AttentionAlarm(player)

    alarm.arm()
    assert player.wait_for(1)
    alarm.arm()
    time.sleep(0.05)

    assert player.count == 1


def test_disarming_an_idle_alarm_is_harmless() -> None:
    """Every non-attention state calls it, which is what lets the hook in
    ``set_loop_state`` carry no bookkeeping of its own."""
    alarm = AttentionAlarm(FakePlayer())

    alarm.disarm()
    alarm.disarm()

    assert alarm.armed is False


def test_a_chime_sounds_once_and_arms_nothing() -> None:
    """The protocol-error path: a re-sync with no loop state behind it, so
    there would be nothing to disarm it either."""
    player = FakePlayer()
    alarm = AttentionAlarm(player)

    alarm.chime()

    assert player.wait_for(1)
    time.sleep(0.05)
    assert player.count == 1
    assert alarm.armed is False


def test_a_player_that_throws_never_reaches_the_caller() -> None:
    """A machine with no sound device is still a machine that has to finish the
    turn."""
    sounds = []

    def explode() -> None:
        sounds.append(1)
        raise OSError("no sound device")

    alarm = AttentionAlarm(explode)
    alarm.arm(repeat_seconds=FAST_REPEAT_S)

    deadline = time.monotonic() + DEADLINE_S
    while time.monotonic() < deadline and len(sounds) < 2:
        time.sleep(0.005)
    alarm.disarm()

    assert len(sounds) >= 2  # it kept going, rather than dying on the first
