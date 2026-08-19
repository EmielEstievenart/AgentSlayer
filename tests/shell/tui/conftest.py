"""Pilot-suite fixtures and the one shared way to send a composer line.

Three jobs. The first is a safety gate rather than a convenience: no test in
this directory may read or write the user's real service profiles. A profile is
a folder of captured PNGs under the app's config home
(``config.default_profile_dir``), and the service editor writes into it the
moment a capture button is pressed - so a Pilot test that built its
``AgentClipApp`` without an explicit ``profile_root`` would calibrate the
developer's actual ChatGPT profile out from under them.

The autouse fixture below redirects that default to ``tmp_path`` for every test
here, patched at BOTH the definition and the ``tui.app`` use site (``app.py``
from-imports the name, so patching only ``agentclip.config`` would miss it) -
the same "patch at the use site" discipline the OS gate in tests/conftest.py
uses. Tests that want to inspect what landed on disk pass ``profile_root``
explicitly and get the same directory. ``_no_real_global_config`` is the same
gate for the other file the app writes to, config.toml.

The second is ``seed_templates``: almost every suite here needs a service that
already knows what its copy button (or chat box, or new-chat control) looks
like, and almost none of them are about *how* it came to know. Driving the
editor's capture flow to arrange that is slow, brittle and re-tests the same
path a dozen times over, so those suites write straight into the profile store
instead - the same files a real capture leaves behind. The capture UI itself is
covered once, in test_profile_capture_ui.py.

The third is :func:`send_composer`. Half the suites here type ``/new`` at some
point to prove that a session teardown leaves their calibration alone, and none
of them are about the chat box - so "how a line is sent" has to live in one
place. It stopped being one keypress when slash-command autocomplete landed
(§3.3a), and six copies of that helper would have been six chances to get the
Enter count wrong.

Those same suites need ``new_chat_click_lands``: ``/new`` now opens the browser's
fresh chat *itself* and resets only when that click lands, so a teardown typed by
a test with no captured new-chat button would be refused rather than performed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from textual.pilot import Pilot

from agentclip.driver.screen.capture import RegionImage
from agentclip.driver.screen.profile import TemplateKind
from agentclip.driver.screen.profile_store import save_template
from agentclip.driver.screen.region import ScreenRegion, click_point_region
from agentclip.driver.screen.slot import AgentSlot
from agentclip.shell.tui.app import AgentClipApp
from agentclip.shell.tui.screens import main as main_mod
from agentclip.shell.tui.screens.main import MainScreen


async def send_composer(app: AgentClipApp, pilot: Pilot, text: str) -> None:
    """Type ``text`` into the chat box and send it, however many Enters that takes.

    Focus is set explicitly because a suite that just clicked a sidebar button
    has left focus on the button. The conditional second Enter is the
    autocomplete rule (§3.3a): a line that is still a bare ``/command`` has the
    popup up, and the first Enter *completes* the highlighted row rather than
    sending it - so a plain message takes one Enter and ``/new`` takes two,
    which is exactly what a user presses.
    """
    main = app.main_screen
    assert main is not None
    main.composer.load_text(text)
    main.composer.focus()
    await pilot.pause()  # let the popup decide whether this line is a command
    if main.command_popup.is_open:
        await pilot.press("enter")  # completes to "<command> ", closing the popup
    await pilot.press("enter")  # sends


def template_image(width: int = 64, height: int = 64) -> RegionImage:
    """A capture varied enough for ``Template.build`` to anchor on.

    Anchors are picked from the most *varied* windows of the quantised blue
    plane, so a flat block of one colour cannot be searched for and
    ``ServiceProfile.put`` rejects it - a cycling byte pattern is the cheapest
    thing that is always accepted.
    """
    size = width * height * 4
    return RegionImage(width, height, (bytes(range(256)) * (size // 256 + 1))[:size])


def aimed_at(box: ScreenRegion) -> ScreenRegion:
    """Where a click on a MATCHED appearance lands: one pixel of the rectangle.

    The service's own click point (tui.md 3.4d) decides which - the middle of
    the picture until somebody moves it, which is what every suite here is set
    up with. The whole-drawn-window fallback is NOT this: it is the region the
    user drew rather than a picture of a control, so it keeps its plain centre
    click and is still written as the rectangle itself.
    """
    return click_point_region(box, 50, 50)


def focus_clicks(*targets: ScreenRegion) -> list[ScreenRegion]:
    """The click trace ONE delivery per target leaves behind.

    The click that focuses the chat box before a paste is a DOUBLE click
    (``driver.automation.delivery.FOCUS_CLICK_GAP_S``): the first wakes the
    browser window, the second lands in the box that window can now route to.
    So every suite here that asserts *where* a delivery aimed sees each target
    twice, and says so through this helper rather than by doubling every
    literal - the interesting part of those assertions is the target, not the
    arithmetic.
    """
    return [target for target in targets for _ in range(2)]


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    """The tmp directory every app in this suite persists appearances into."""
    return tmp_path / "profiles"


@pytest.fixture
def make_template_image() -> Callable[..., RegionImage]:
    """:func:`template_image`, for suites that need bespoke pixels.

    ``save_template(profile_root, key, kind, image)`` is the escape hatch when a
    seeded appearance has to be findable in a *particular* fake scene rather
    than merely present.
    """
    return template_image


@pytest.fixture
def seed_templates(profile_root: Path) -> Callable[..., None]:
    """Give ``key`` the listed appearances, as if they had been captured.

    ``seed(key, TemplateKind.COPY, TemplateKind.NEW_CHAT, size=(24, 24))``. Call
    it before ``app.run_test`` and the app simply loads them; call it during a
    run and clear ``MainScreen._profiles`` (or go through ``update_config``) to
    make the screen re-read them, exactly as an editor visit does.

    A capture ADDS to its kind (screen.profile), so calling this twice for one
    kind seeds a two-image stack rather than replacing the first - which is
    exactly how a test gives a kind more than one appearance, at a different
    ``size`` so the two are distinguishable.
    """

    def seed(
        key: str, *kinds: TemplateKind, size: Iterable[int] = (64, 64)
    ) -> None:
        width, height = tuple(size)
        for kind in kinds:
            save_template(profile_root, key, kind, template_image(width, height))

    return seed


@pytest.fixture
def new_chat_click_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``/new`` tear a session down without a captured new-chat button.

    Since §3.3a the command drives the browser itself - it clicks the new-chat
    control in the master window and resets the session only if that click
    landed - so a suite that types ``/new`` merely to prove its calibration
    survives a teardown would be refused for want of a button it never captured.
    This stubs the *browser* half of the shared flow and keeps its tail, which is
    the reset those suites are actually asking for. The click itself is covered
    once, in test_newchat_ui.py, and nothing here mocks it away.
    """

    async def _click_landed(self: MainScreen, slot: AgentSlot) -> None:
        self._reset_after_new_browser_chat(slot)

    monkeypatch.setattr(MainScreen, "_new_browser_chat", _click_landed)


@pytest.fixture(autouse=True)
def _no_real_activation_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the delivery's activation poll to a beatless loop.

    Autouse rather than per suite, because unlike every other beat here this one
    is paid by tests that are not about it. ``deliver`` waits for the foreground
    to stop being OUR window before it pastes (§3.4b), the suite-wide OS gate
    leaves ``GetForegroundWindow`` real on purpose (reads tell a test about the
    desktop without touching it), and the window an app mounted in a pytest run
    records as its own IS the foreground one - so every delivery in this
    directory would sit out the whole 1s budget waiting for a browser that does
    not exist. The poll still runs and still asks its full ``attempts``; only
    the sleep between two asks goes.
    """
    monkeypatch.setattr(main_mod, "_ACTIVATION_POLL_S", 0.0)
    # Same argument for the gap between the two halves of the focus click: it is
    # a real beat every delivery in this directory pays, and no suite here is
    # about how long it is - only that the second click happens (which is what
    # ``focus_clicks`` below counts).
    monkeypatch.setattr(main_mod, "_FOCUS_CLICK_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def _no_real_profiles(monkeypatch: pytest.MonkeyPatch, profile_root: Path) -> None:
    """Point ``default_profile_dir`` at the tmp path, definition and use site."""

    def fake_default_profile_dir() -> Path:
        return profile_root

    monkeypatch.setattr("agentclip.config.default_profile_dir", fake_default_profile_dir)
    monkeypatch.setattr(
        "agentclip.shell.tui.app.default_profile_dir", fake_default_profile_dir, raising=False
    )


@pytest.fixture
def default_global_config(tmp_path: Path) -> Path:
    """The config.toml an app built WITHOUT an explicit ``global_config_path``
    falls back to in this suite (see ``_no_real_global_config``)."""
    return tmp_path / "default-global-config.toml"


@pytest.fixture(autouse=True)
def _no_real_global_config(monkeypatch: pytest.MonkeyPatch, default_global_config: Path) -> None:
    """The same safety gate as ``_no_real_profiles``, for config.toml.

    Preferences are written back to the global config now, and not only from the
    settings screens: pointing a window tab at another service persists that
    pick (config.save_active_services), which every suite that switches service
    does incidentally. Most of them build their app without a
    ``global_config_path``, so without this the picker would rewrite the
    developer's real ``[general] service`` mid-test. Patched at both the
    definition and the ``tui.app`` use site, for the from-import reason above.
    """

    def fake_default_global_config_path() -> Path:
        return default_global_config

    monkeypatch.setattr(
        "agentclip.config.default_global_config_path", fake_default_global_config_path
    )
    monkeypatch.setattr(
        "agentclip.shell.tui.app.default_global_config_path",
        fake_default_global_config_path,
        raising=False,
    )
