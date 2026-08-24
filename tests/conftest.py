"""Shared fixtures: a tmp project workspace, default config, registry, and an
Engine factory. The engine round-trip tests never touch a real clipboard.

Also home to the suite-wide OS-input gate (``_no_real_os_input``) - see its
docstring: nothing here is allowed to move the user's cursor, type into the
window they are looking at, or throw a fullscreen overlay in their face.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine
from agentclip.engine.store.backups import BackupStore
from agentclip.engine.store.session import SessionStore
from agentclip.executor.tools.registry import ToolRegistry, default_registry
from agentclip.executor.tools.sandbox import Workspace
from agentclip.protocol.composer import Composer

UTILS_PY = '''"""Utility helpers."""

from datetime import datetime


def parse_date(s):
    # NOTE: legacy format
    return datetime.strptime(s, "%d/%m/%Y")
'''

TEST_UTILS_PY = """def test_parse_date():
    from src.utils import parse_date
    assert parse_date("2026-06-12")
"""

# Every engine built by these fixtures agrees this chat name with its "model",
# so canned replies can hard-code `chat=amber-falcon` on their EOM line.
CHAT_NAME = "amber-falcon"

# == the OS-input gate ========================================================

# Opt back in with AGENTCLIP_OS_TESTS=1 (read once: nothing may flip the gate
# mid-run), or per test with @pytest.mark.real_os.
OS_TESTS_ENABLED = os.environ.get("AGENTCLIP_OS_TESTS") == "1"

PICK_REGION_BLOCKED = (
    "pick_region reached the real overlay - mock it at the use site (main_mod.pick_region)"
)
IDENTIFY_OVERLAY_BLOCKED = (
    "draw_identify_overlay reached the real overlay - mock it at the use site "
    "(main_mod.draw_identify_overlay)"
)


def _blocked_pick_region(*args: Any, **kwargs: Any) -> None:
    raise AssertionError(PICK_REGION_BLOCKED)


def _blocked_identify_overlay(*args: Any, **kwargs: Any) -> None:
    raise AssertionError(IDENTIFY_OVERLAY_BLOCKED)


@pytest.fixture(autouse=True)
def _no_real_os_input(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed on synthetic input: no test moves the cursor, clicks, scrolls
    or types into whatever window the user is actually looking at.

    This generalizes the per-file ``_no_real_paste`` fixtures - a real Ctrl+V
    escaping into the test runner's window is unforgivable, and so is a real
    click landing wherever the pointer happened to be. Individual tests patch
    ``main_mod.click_region`` / ``main_mod.send_paste`` at the use site, but
    ``main.py`` from-imports those names, so any test that forgets one drives
    the real OS. The gate sits under all of them instead.

    The choke point is ``ctypes.windll.user32``: every injecting call in
    ``screen.focus`` (SendInput for paste/scroll/move, SetCursorPos for the
    aimed click, SetForegroundWindow for the snap-back) resolves off that one
    process-wide WinDLL instance *at call time*, whatever name the caller
    imported. Neutered here, ``click_region``/``scroll_region``/``move_cursor``/
    ``send_paste``/``focus_window`` all report a plain False - the same answer
    they give on an unsupported platform, which every caller already handles.

    On Linux the same functions dispatch to ``screen.x11`` instead, so the gate
    has a second pair of seams there (``_fake_input`` and ``_activate``, see
    below): a Linux ``uv run pytest`` must be no more able to type into the
    developer's browser than a Windows one is.

    Read-only calls stay real (GetForegroundWindow, GetSystemMetrics, and the
    GDI capture in ``screen.capture``, which uses a private WinDLL anyway): they
    tell tests about the desktop without touching it.

    ``pick_region`` gets a loud stub rather than a no-op, because its failure
    mode is a fullscreen tkinter overlay in a child process - better an
    AssertionError naming the mock the test forgot. ``draw_identify_overlay``
    (`/identify`'s read-only twin) is blocked the same way: it too spawns a child
    process that covers the user's whole desktop, and it has no timer of its own,
    so a test that forgets it would hang behind a picture of somebody's browser
    until a human dismissed it.
    """
    if OS_TESTS_ENABLED or request.node.get_closest_marker("real_os"):
        return

    # The overlay guards are platform-independent (the picker shells out).
    monkeypatch.setattr("agentclip.driver.screen.picker.pick_region", _blocked_pick_region)
    monkeypatch.setattr(
        "agentclip.driver.screen.picker.draw_identify_overlay", _blocked_identify_overlay
    )
    # ...and the drawing itself, which the identify CHILD calls in-process: a
    # test that exercises the `--show-identify` entry point (cli.main, or the
    # child function directly) would otherwise open a real Tk window right here,
    # with no child process in between to keep it away from the test runner.
    monkeypatch.setattr(
        "agentclip.driver.screen.overlay.run_identify_overlay", _blocked_identify_overlay
    )
    # ...and again at main.py's bound names, which are the seam every caller uses.
    monkeypatch.setattr(
        "agentclip.shell.tui.screens.main.pick_region", _blocked_pick_region, raising=False
    )
    monkeypatch.setattr(
        "agentclip.shell.tui.screens.main.draw_identify_overlay",
        _blocked_identify_overlay,
        raising=False,
    )

    if sys.platform.startswith("linux"):
        # The same choke point, one platform over. ``screen.x11`` funnels EVERY
        # injecting call through two functions - ``_fake_input`` (the XTest
        # event behind every click, wheel detent, cursor move and keystroke) and
        # ``_activate`` (the EWMH client message that yanks a window forward) -
        # so neutering those two leaves click_region/scroll_region/move_cursor/
        # send_paste/focus_window reporting the same plain False the Windows
        # branch below produces, whatever name a caller imported. Read-only
        # calls stay real here too: XGetImage, the root geometry and the
        # _NET_ACTIVE_WINDOW read tell tests about the desktop without touching
        # it.
        monkeypatch.setattr(
            "agentclip.driver.screen.x11._fake_input", lambda *args, **kwargs: False
        )
        monkeypatch.setattr("agentclip.driver.screen.x11._activate", lambda *args, **kwargs: False)
        return
    if sys.platform != "win32":
        return  # nothing else here can inject; ctypes.windll does not exist
    import ctypes

    user32 = ctypes.windll.user32
    # Signatures are irrelevant: focus.py only reads the return value, and the
    # argtypes/restype it assigns land harmlessly on these function objects.
    monkeypatch.setattr(user32, "SendInput", lambda *args: 0, raising=False)
    monkeypatch.setattr(user32, "SetCursorPos", lambda *args: False, raising=False)
    monkeypatch.setattr(user32, "SetForegroundWindow", lambda *args: False, raising=False)


@pytest.fixture(autouse=True)
def _no_real_permissions_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the developer's real ``~/.config/agentclip/permissions.json``.

    The ruleset is the whole permission system (engine/approval.py), so a
    machine whose owner has written one would run this suite under those rules -
    green here, red on the next machine, and the failure would look like an
    approval bug rather than a leak. Sharper than it used to be: ``/config
    global`` CREATES that file, so any developer who has tried the command once
    has one. Tests that want their own rules write them (``write_permissions``,
    or ``[permission] permissions_config`` pointed at a file).
    """
    missing = tmp_path / "no-such-permissions.json"
    monkeypatch.setattr(
        "agentclip.config.default_permissions_config_path", lambda: missing
    )
    # The controller imported the function by name, so it holds a SECOND binding
    # - and its `/config global` would create the real file rather than read it.
    # Patched here, beside the reason, rather than in one command's tests.
    monkeypatch.setattr(
        "agentclip.shell.app.controller.default_permissions_config_path", lambda: missing
    )


@pytest.fixture(autouse=True)
def _half_block_terminal() -> Any:
    """Every test starts on the renderer that needs nothing from a terminal.

    The sixel verdict is a process-global set once at startup (``tui.graphics``),
    so a test that declares one to reach the sixel path would otherwise leave it
    declared for every test after it in the same worker - and a pytest run has
    no terminal to draw sixels on. Reset rather than forbidden: declaring the
    verdict IS the documented way into that path.
    """
    from agentclip.shell.tui.graphics import NO_SIXEL, set_terminal_graphics

    set_terminal_graphics(NO_SIXEL)
    yield
    set_terminal_graphics(NO_SIXEL)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tmp project with a few files for the tools to act on."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "utils.py").write_text(UTILS_PY, encoding="utf-8", newline="")
    (root / "tests" / "test_utils.py").write_text(TEST_UTILS_PY, encoding="utf-8", newline="")
    (root / "README.md").write_text("demo project for engine tests\n", encoding="utf-8")
    return root


def write_permissions(project: Path, document: dict[str, object]) -> Path:
    """Write ``<project>/.agentclip/permissions.json`` and return its path.

    The permission ruleset is the ONLY approval mechanism, so a test whose tool
    calls must run without gating says so here rather than relying on a default.
    Written as the PROJECT layer, which loads last, so it outranks anything else
    a test set up.
    """
    path = project / ".agentclip" / "permissions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def config(project: Path) -> Config:
    """Default config: the global file does not exist; <project>/.agentclip.toml
    is honored when a test writes one BEFORE requesting this fixture."""
    return load_config(project, global_config_path=project / "no-such-global.toml")


@pytest.fixture
def registry() -> ToolRegistry:
    return default_registry()


@pytest.fixture
def chat_name() -> str:
    """The chat name the fixture engines expect every reply to echo."""
    return CHAT_NAME


EngineFactory = Callable[..., Engine]


@pytest.fixture
def make_engine(project: Path, registry: ToolRegistry) -> EngineFactory:
    """Factory building a fully wired, headless Engine over the tmp project.

    Reloads config on each call so tests can drop a .agentclip.toml into the
    project (e.g. to point [permission] elsewhere) before building. Pass
    ``tools=`` to swap the registry (e.g. for a fake slow tool) and ``role=``
    to build a sub-agent engine (pair it with a sub-agent registry).
    """

    def factory(
        config: Config | None = None,
        tools: ToolRegistry | None = None,
        role: Literal["master", "subagent"] = "master",
    ) -> Engine:
        cfg = config or load_config(project, global_config_path=project / "no-such-global.toml")
        reg = tools or registry
        workspace = Workspace(project, cfg.excluded_names())
        session = SessionStore(project, service=cfg.general.service)
        backups = BackupStore(session.session_dir)
        composer = Composer(
            cfg.preset(),
            cfg.caps(),
            reg.render_catalog(),
            project.name,
            "TestOS",
            CHAT_NAME,
            role=role,
        )
        return Engine(cfg, reg, workspace, session, backups, composer, CHAT_NAME, role=role)

    return factory


@pytest.fixture
def engine(make_engine: EngineFactory) -> Engine:
    return make_engine()
