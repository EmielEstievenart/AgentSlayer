"""The GUI shell's slice 1: a window over packaged assets, and nothing else.

Nothing here opens a window. What these tests pin is everything about the shell
that can be true before the loop starts - that the page is really shipped, that
it reaches nothing off this machine, and that a launch reaches it with the
resolved Launch and the ingredients built above it.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from agentclip import __version__, cli
from agentclip.config import MonitorTarget, load_config
from agentclip.driver.clip.base import select_provider
from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.hosts.local import LocalHost
from agentclip.shell.app.link import Link
from agentclip.shell.app.monitor_launch import LaunchLocal, SubprocessLauncher
from agentclip.shell.chat.shell import (
    ASSET_PACKAGE,
    MIN_WINDOW_SIZE,
    SCREEN_MARGIN,
    WINDOW_SIZE,
    WINDOW_TEXT_SELECT,
    WINDOW_TITLE,
    asset_dir,
    entry_url,
    initial_window_size,
    primary_screen_size,
    run_gui,
)
from agentclip.shell.webview.assets import ASSET_DIR, ASSET_NAMES, ENTRY_PAGE


def asset_text(name: str) -> str:
    return files(ASSET_PACKAGE).joinpath(ASSET_DIR, name).read_text(encoding="utf-8")


def _no_engine(request: EngineRequest) -> Link:
    """An engine factory for the paths that never reach a session start."""
    raise AssertionError("no engine should be built on this path")


# == the packaged page ========================================================


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_every_asset_ships_and_says_something(name: str) -> None:
    """Read through importlib.resources, which is how the shell finds them:
    a file that only exists next to __file__ is a file a wheel can lose."""
    assert asset_text(name).strip()


def test_the_page_pulls_in_its_stylesheet_and_script() -> None:
    html = asset_text(ENTRY_PAGE)
    assert 'href="app.css"' in html
    assert 'src="app.js"' in html


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_asset_reaches_the_network(name: str) -> None:
    """No CDN, no web font, no analytics - not even in a comment.

    The window is a local app looking at the user's screen and clipboard; the
    moment the page can fetch something, "local" stops being checkable by
    reading it (docs/design/gui.md section 2: no build step, no network).
    """
    text = asset_text(name)
    assert "http://" not in text
    assert "https://" not in text


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_asset_fetches_anything_at_runtime_either(name: str) -> None:
    """The URL check above catches a CDN written into the source; this catches
    the same thing arriving at runtime.

    No build step and no npm means every renderer here is hand-written - the
    markdown, the diff colouring, the spinner - and the moment one of them is
    "just one library away" the page stops being a local file that can be read
    end to end (docs/design/gui.md section 2).
    """
    text = asset_text(name)
    for reach in ("fetch(", "XMLHttpRequest", "import(", "importScripts", "WebSocket", "//cdn"):
        assert reach not in text, f"{name} reaches outside itself: {reach}"


def test_the_entry_url_is_a_local_file_carrying_the_version() -> None:
    with asset_dir() as assets:
        url = entry_url(assets)
    assert url.startswith("file://")
    assert url.endswith(f"#v={__version__}")
    # A file:// URL rather than a bare path on purpose: a path makes pywebview
    # start a local HTTP server, and this page has nothing to serve.
    assert "://127.0.0.1" not in url and "localhost" not in url


def test_the_asset_directory_is_a_real_directory_while_it_is_open() -> None:
    with asset_dir() as assets:
        assert (assets / ENTRY_PAGE).is_file()
        assert {p.name for p in assets.iterdir()} >= set(ASSET_NAMES)


# == import safety ============================================================


def test_importing_the_gui_does_not_import_pywebview() -> None:
    """The `gui` extra stays unpaid-for until a window is actually asked for.

    A subprocess rather than a sys.modules check in-process: this suite has
    pywebview installed and imported by other tests here, so the only honest
    question is what a fresh interpreter loads.
    """
    probe = (
        "import sys, agentclip.shell.chat.shell;"
        " sys.exit(1 if 'webview' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0


def test_missing_pywebview_names_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """None in sys.modules is exactly what a missing package feels like to
    `import webview` - and the answer must be a sentence, not a traceback."""
    monkeypatch.setitem(sys.modules, "webview", None)
    assert (
        run_gui(
            _launch(Path.cwd()),
            provider=select_provider("manual"),
            engine_factory=_no_engine,
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "gui extra" in err
    assert "uv sync --extra gui" in err


# == the CLI branch ===========================================================


def _launch(root: Path) -> cli.Launch:
    return cli.Launch(
        project_root=root,
        config=load_config(root, global_config_path=root / "no-such-global.toml"),
        host=LocalHost(),
        os_name="TestOS",
        data_root=root,
        home=root,
    )


def test_a_bare_launch_opens_the_one_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no fork any more (docs/design/ui-monitor.md 6.6 deleted the
    Textual shell): a bare ``agentclip`` opens the window, and nothing else
    can be asked for."""
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    opened: list[str] = []
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda given, **rest: opened.append("gui") or 0
    )

    assert cli.main(["--project", str(tmp_path)]) == 0
    assert opened == ["gui"]


def test_the_tui_flag_is_a_stub_that_says_what_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kept for one release, and kept as a REFUSAL (6.6; 8's "Textual removal
    timing"). A script that still carries the flag must not be quietly handed a
    different shell than it asked for, and argparse's "unrecognized arguments"
    would tell it nothing about where the old one went.
    """
    assert cli.build_arg_parser().parse_args(["--tui"]).tui is True
    monkeypatch.setattr(cli, "local_launch", lambda args: pytest.fail("resolved a launch"))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda *a, **k: pytest.fail("opened the window")
    )

    assert cli.main(["--tui", "--project", str(tmp_path)]) == 2
    assert cli.TUI_REMOVED in capsys.readouterr().err


def test_the_gui_flag_is_an_accepted_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept for one release so a muscle-memory ``--gui`` (and every script and
    shortcut still carrying it) lands on what it always asked for - the GUI,
    which is now simply what a launch opens."""
    assert cli.build_arg_parser().parse_args(["--gui"]).gui is True

    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    opened: list[str] = []
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda given, **rest: opened.append("gui") or 0
    )

    assert cli.main(["--gui", "--project", str(tmp_path)]) == 0
    assert opened == ["gui"]


def test_the_shell_is_handed_the_resolved_launch_and_its_ingredients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_gui`` is reached with the resolved Launch, not with raw argv - and
    with the pieces ``main`` builds above it, so nothing below that line has to
    re-decide which clipboard backend or which engine factory a session uses
    (docs/design/gui.md section 0).
    """
    launch = _launch(tmp_path)
    seen: list[object] = []
    kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli, "local_launch", lambda args: launch)
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui",
        lambda given, **rest: (seen.append(given), kwargs.update(rest))[0] or 0,
    )

    assert cli.main(["--project", str(tmp_path)]) == 0
    assert seen == [launch]
    assert kwargs["provider"] is not None
    assert callable(kwargs["engine_factory"])
    assert kwargs["mcp_manager"] is None  # no servers configured in a tmp project


# == the real toolkit, without a window =======================================


def test_a_window_can_be_described_without_starting_a_loop() -> None:
    """A smoke test for the arguments themselves, where pywebview is installed.

    create_window() on the main thread before start() only records the window,
    so this exercises the real signature (a renamed keyword in a later
    pywebview would fail here) without anything appearing on the user's screen.
    """
    webview = pytest.importorskip("webview", reason="the gui extra is not installed")
    before = len(webview.windows)
    with asset_dir() as assets:
        window = webview.create_window(
            WINDOW_TITLE,
            url=entry_url(assets),
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=MIN_WINDOW_SIZE,
            text_select=WINDOW_TEXT_SELECT,
            hidden=True,
        )
    try:
        assert window.title == WINDOW_TITLE
        assert (window.initial_width, window.initial_height) == WINDOW_SIZE
        assert window.min_size == MIN_WINDOW_SIZE
        assert str(window.original_url).startswith("file://")
        # The one kwarg with no visible geometry to give it away, and the one
        # that would silently take copy-out-of-the-transcript away again if it
        # went missing: pywebview defaults it to False and enforces THAT with
        # injected CSS the stylesheet cannot outrank (shell.WINDOW_TEXT_SELECT).
        assert window.text_select is True
    finally:
        del webview.windows[before:]


# == how big the window may be ================================================
# The monitor this policy was written for is 900x1400, and the ask was that the
# window take half of it - either half. So the numbers below are not taste:
# 450x1400 and 900x700 have to be sizes the window can actually be.

HALF_A_SMALL_MONITOR = (900 // 2, 1400 // 2)


def test_the_minimum_lets_the_window_take_half_a_small_portrait_monitor() -> None:
    assert MIN_WINDOW_SIZE[0] <= HALF_A_SMALL_MONITOR[0]
    assert MIN_WINDOW_SIZE[1] <= HALF_A_SMALL_MONITOR[1]


def test_the_default_size_is_clamped_into_a_screen_it_would_not_fit() -> None:
    """A 1200-wide default on a 900-wide panel opens a third of itself off the
    screen; the height already fits and must be left alone."""
    assert initial_window_size(900, 1400) == (900 - SCREEN_MARGIN, WINDOW_SIZE[1])


def test_a_roomy_screen_gets_the_plain_default() -> None:
    assert initial_window_size(2560, 1440) == WINDOW_SIZE


def test_a_screen_too_small_for_the_minimum_still_gets_the_minimum() -> None:
    """Clamping down is a courtesy; clamping below what the window may be
    would hand pywebview a size it has to refuse anyway."""
    assert initial_window_size(320, 240) == MIN_WINDOW_SIZE


@pytest.mark.parametrize("screen", [(None, None), (0, 0), (None, 1400)])
def test_an_unanswered_screen_leaves_that_axis_at_the_default(
    screen: tuple[int | None, int | None],
) -> None:
    width, height = initial_window_size(*screen)
    assert width == WINDOW_SIZE[0]
    assert height == WINDOW_SIZE[1]


def test_the_screen_reader_shrugs_at_a_toolkit_that_cannot_answer() -> None:
    """webview.screens asks the native toolkit, which has no answer on a
    headless box. Every way that can go wrong is the same (None, None)."""

    class _Raises:
        @property
        def screens(self) -> list[object]:
            raise RuntimeError("no display")

    class _Empty:
        screens: list[object] = []

    class _Nameless:
        screens = [object()]

    assert primary_screen_size(_Raises()) == (None, None)
    assert primary_screen_size(_Empty()) == (None, None)
    assert primary_screen_size(_Nameless()) == (None, None)


def test_the_screen_reader_takes_the_first_screen() -> None:
    class _Screen:
        def __init__(self, width: int, height: int) -> None:
            self.width = width
            self.height = height

    class _Toolkit:
        screens = [_Screen(900, 1400), _Screen(3840, 2160)]

    assert primary_screen_size(_Toolkit()) == (900, 1400)


def test_the_stylesheet_reshapes_itself_for_a_narrow_window() -> None:
    """The minimum above is only honest if the page can be drawn at it: below
    the breakpoint the sidebar must FLOAT over the chat rather than go on
    squeezing it, and the connect dialog must stop being multi-column."""
    css = asset_text("app.css")
    for breakpoint in ("640px", "560px"):
        assert f"@media (max-width: {breakpoint})" in css
    narrow = css.split("@media (max-width: 640px)", 1)[1].split("@media", 1)[0]
    assert ".sidebar {" in narrow
    assert "position: absolute;" in narrow
    # And above the breakpoint they give ground instead of holding a number.
    assert "width: clamp(220px, 28vw, 300px);" in css


def test_the_webview2_check_answers_without_a_window() -> None:
    """It reads a verdict pywebview reaches at import time; it must never raise,
    whatever this machine has installed."""
    from agentclip.shell.chat.shell import webview2_missing

    assert webview2_missing() in (True, False)


def test_argparse_help_names_both_retired_flags_for_what_they_now_are() -> None:
    help_text = cli.build_arg_parser().format_help()
    assert "Chat UI" in help_text
    # Both stay visible for a release, each marked for what it now is: one is a
    # refusal, the other does nothing.
    assert "--tui" in help_text
    assert "removed" in help_text
    assert "--gui" in help_text
    assert "deprecated no-op" in help_text


# == the packaging smoke ======================================================


def test_the_gui_smoke_flag_is_hidden() -> None:
    """It is a build-script probe, not a user-facing feature. The last one this
    parser has: --pick-region, --show-identify and --list-matchers left it in
    ui-monitor.md §10.1 for the binary that actually draws and matches."""
    args = cli.build_arg_parser().parse_args([])
    assert args.gui_smoke is False
    assert cli.build_arg_parser().parse_args(["--gui-smoke"]).gui_smoke is True
    assert "--gui-smoke" not in cli.build_arg_parser().format_help()


def test_the_gui_smoke_reports_ok_and_names_the_renderer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """From source it proves only what it proves frozen: pywebview imports, all
    three assets resolve through importlib.resources and are non-empty.

    The renderer word is REPORTED, never asserted - `missing` means this
    machine has no WebView2 runtime, which is a fact about the box and not
    about the build, and the exit code stays 0 either way (scripts/build-exe.ps1
    is what runs this against the frozen exe)."""
    pytest.importorskip("webview", reason="the gui extra is not installed")

    assert cli.main(["--gui-smoke"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("gui-smoke: ok renderer=")
    assert out.split("renderer=")[1].strip() in {"edgechromium", "missing", "n/a"}


def test_the_gui_smoke_fails_when_pywebview_is_gone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the check: a build that lost the extra must exit
    non-zero at BUILD time rather than at the user's first launch."""
    monkeypatch.setitem(sys.modules, "webview", None)
    assert cli.main(["--gui-smoke"]) == 2
    assert "pywebview is not in this build" in capsys.readouterr().err


def test_the_gui_smoke_runs_before_any_launch_is_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It answers a question about the BUILD, so it must not need a project, a
    config or a host - the frozen exe is run from wherever the build left it."""
    pytest.importorskip("webview", reason="the gui extra is not installed")
    monkeypatch.setattr(cli, "local_launch", lambda args: pytest.fail("resolved a launch"))
    monkeypatch.setattr(cli, "remote_launch", lambda args: pytest.fail("resolved a launch"))
    assert cli.main(["--gui-smoke"]) == 0


def test_the_spec_ships_the_page_where_importlib_resources_will_look() -> None:
    """The one packaging fact a from-source suite CAN check: that the spec
    collects every asset shell.py promises, at the package-relative path the
    resource reader resolves under _MEIPASS (docs/design/gui.md 5).

    Read as text rather than executed - a spec is only a valid Python file
    inside PyInstaller, which injects SPECPATH and the Analysis/EXE names.
    """
    spec = (Path(__file__).resolve().parents[3] / "packaging" / "agentclip.spec").read_text(
        encoding="utf-8"
    )
    assert '"agentclip/shell/chat/assets"' in spec, "the assets' destination is not the package path"
    assert "page_datas" in spec
    assert "page_datas + doc_datas" in spec, "the assets are collected but never handed to Analysis"
    # The backend chat/shell.py's webview2_missing() reads, which nothing static
    # reaches (webview/guilib.py picks it per platform at import time).
    assert "webview.platforms.winforms" in spec


def test_the_launch_protocol_matches_the_real_launch(tmp_path: Path) -> None:
    """LaunchLike is structural on purpose - cli sits ABOVE both shells, so the
    shell may not import the class it is handed. This is the cheap runtime half
    of that promise (mypy checks the rest at the cli.main call site)."""
    launch = _launch(tmp_path)
    for name in ("project_root", "config"):
        assert hasattr(launch, name), f"cli.Launch has no {name}"


# == --monitor: split mode's whole entry (ui-monitor.md §6.5) ==================
# The launch flag that moves the SCREEN onto another machine. All of it is
# refused or resolved in ``cli.main``, so nothing below that line can ever be
# handed a target it would have to re-parse or re-validate.


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("box:7777", ("box", 7777)),
        ("127.0.0.1:1", ("127.0.0.1", 1)),
        ("vm.local:65535", ("vm.local", 65535)),
        # An IPv6 literal, written the only way a host:port string can carry
        # one - the port is split off the RIGHT, so the colons inside survive.
        ("[::1]:7777", ("::1", 7777)),
    ],
)
def test_a_monitor_target_parses_into_a_host_and_a_port(
    given: str, expected: tuple[str, int]
) -> None:
    assert cli.parse_monitor_target(given) == expected


@pytest.mark.parametrize(
    "given",
    [
        "box",  # no port at all
        "box:",  # ...nor here
        ":7777",  # no host
        "box:ssh",  # a service name is not a port
        "box:0",  # out of range, both ends
        "box:70000",
    ],
)
def test_a_target_that_is_not_host_port_is_refused_rather_than_guessed_at(given: str) -> None:
    """Guessing would mean dialling a port nobody asked for, on a channel that
    reaches a machine's mouse and keyboard (§5)."""
    assert cli.parse_monitor_target(given) is None


def test_a_bad_monitor_target_is_fatal_before_anything_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused in ``main``, so nothing below it can be handed a target it would
    have to re-parse. Since §9.2 the refusal happens just AFTER the launch is
    resolved rather than just before it - ``--monitor @name`` reads a saved
    table, and there is no config to read one out of until then - so the thing
    pinned here is that no WINDOW opens, not that no Config was built."""
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda *a, **k: pytest.fail("opened a window")
    )

    assert cli.main(["--monitor", "box:ssh", "--project", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "--monitor wants HOST:PORT" in err
    assert "box:ssh" in err  # ...and it says what it was given


def test_the_monitor_target_reaches_the_gui_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threaded like ``pending_connect``: resolved in ``main`` and handed to
    ``run_gui`` as the value type. A ``MonitorTarget`` rather than the old pair
    since §9.2, because the address is no longer all of it - a token rides
    along, and ``@name`` can put an SSH hop in front of the whole thing.

    All THREE values now reach the shell as they are (§10.2): an address to
    dial, ``LaunchLocal`` for "start one here first", and ``None`` for a window
    with no screen. ``main`` flattens none of them any more."""
    kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda given, **rest: kwargs.update(rest) or 0
    )

    assert cli.main(["--monitor", "box:7777", "--project", str(tmp_path)]) == 0
    assert kwargs["monitor_target"] == MonitorTarget(
        name="box:7777", host="box", port=7777
    )

    # An ABSENT flag resolves to LaunchLocal - there is no in-process monitor
    # left to be the default by omission (§10.1), so "no --monitor" means
    # "start one on this PC and dial it".
    kwargs.clear()
    assert cli.main(["--project", str(tmp_path)]) == 0
    assert isinstance(kwargs["monitor_target"], LaunchLocal)
    # ...and the launcher that carries it out is handed over too: the real one,
    # because deciding HOW a monitor process is spawned is a launch question.
    assert isinstance(kwargs["launcher"], SubprocessLauncher)

    # ``--monitor none`` is the third value, and the only one that reaches the
    # shell as None: a window with no screen until the Monitor tab gives it one.
    kwargs.clear()
    assert cli.main(["--monitor", "none", "--project", str(tmp_path)]) == 0
    assert kwargs["monitor_target"] is None


# == --monitor @name, the token's three sources, and --calibrate's epitaph =====
# ui-monitor.md 9.2. The flag is no longer the whole entry - the Chat UI's
# connect dialog grew a Monitor tab - but it stays the SCRIPTABLE one, and it
# gained a saved-target spelling and a token that never rides in the target
# string.


def _saved_monitor(root: Path, body: str) -> Path:
    """A global config.toml with [monitor.*] tables in it, and the launch wired
    to read it - the fixture two of the tests below share."""
    path = root / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _launch_with(root: Path, global_path: Path) -> cli.Launch:
    return cli.Launch(
        project_root=root,
        config=load_config(root, global_config_path=global_path),
        host=LocalHost(),
        os_name="TestOS",
        data_root=root,
        home=root,
    )


def test_a_saved_monitor_is_dialled_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``@name`` reads a ``[monitor.<name>]`` table - the token with it, so the
    common case puts no secret on a command line at all."""
    global_path = _saved_monitor(
        tmp_path,
        '[monitor.vm]\nhost = "10.0.0.5"\nport = 7777\ntoken = "%s"\n' % ("a" * 32),
    )
    kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch_with(tmp_path, global_path))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda given, **rest: kwargs.update(rest) or 0
    )

    assert cli.main(["--monitor", "@vm", "--project", str(tmp_path)]) == 0
    assert kwargs["monitor_target"] == MonitorTarget(
        name="vm", host="10.0.0.5", port=7777, token="a" * 32
    )


def test_a_saved_monitor_can_ride_a_saved_ssh_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``via`` is what makes a target a Via-SSH one, and its host defaults to the
    far side's loopback - which is where a monitor behind a tunnel is."""
    global_path = _saved_monitor(tmp_path, '[monitor.box]\nvia = "pi"\n')
    kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch_with(tmp_path, global_path))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda given, **rest: kwargs.update(rest) or 0
    )

    assert cli.main(["--monitor", "@box", "--project", str(tmp_path)]) == 0
    target = kwargs["monitor_target"]
    assert isinstance(target, MonitorTarget)
    assert (target.via, target.host, target.dial_port()) == ("pi", "127.0.0.1", 7777)


def test_a_name_that_names_nothing_is_fatal_and_says_which_table_it_looked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda *a, **k: pytest.fail("opened a window")
    )

    assert cli.main(["--monitor", "@vm", "--project", str(tmp_path)]) == 2
    assert "[monitor.vm]" in capsys.readouterr().err


def test_the_token_comes_from_the_environment_and_the_flag_beats_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three sources, in this order: the flag, then $AGENTCLIP_MONITOR_TOKEN,
    then the saved table. The flag is documented LAST because argv is
    world-readable on the machines this runs on - which is a reason to prefer
    the other two, not a reason for it to lose when somebody types it."""
    kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    monkeypatch.setattr(
        "agentclip.shell.chat.shell.run_gui", lambda given, **rest: kwargs.update(rest) or 0
    )
    monkeypatch.setenv("AGENTCLIP_MONITOR_TOKEN", "e" * 32)

    assert cli.main(["--monitor", "box:7777", "--project", str(tmp_path)]) == 0
    assert kwargs["monitor_target"].token == "e" * 32  # type: ignore[union-attr]

    kwargs.clear()
    assert (
        cli.main(
            ["--monitor", "box:7777", "--monitor-token", "f" * 32, "--project", str(tmp_path)]
        )
        == 0
    )
    assert kwargs["monitor_target"].token == "f" * 32  # type: ignore[union-attr]


def test_a_saved_targets_token_is_used_when_nothing_overrides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_path = _saved_monitor(
        tmp_path, '[monitor.vm]\nhost = "10.0.0.5"\ntoken = "%s"\n' % ("a" * 32)
    )
    config = load_config(tmp_path, global_config_path=global_path)
    monkeypatch.delenv("AGENTCLIP_MONITOR_TOKEN", raising=False)

    resolved = cli.resolve_monitor_target("@vm", None, config, {})
    assert isinstance(resolved, MonitorTarget)
    assert resolved.token == "a" * 32


# == --monitor local / none: the launcher's two bare words (ui-monitor.md §10.1)


@pytest.mark.parametrize("given", [None, "local"])
def test_no_flag_and_local_both_mean_launch_one_here(
    tmp_path: Path, given: str | None
) -> None:
    """The sentinel, not a target: the port a local monitor listens on is chosen
    when it is launched, so there is nothing to resolve here yet."""
    config = _launch(tmp_path).config
    assert cli.resolve_monitor_target(given, None, config, {}) == LaunchLocal()


def test_none_is_the_window_with_no_screen_attached(tmp_path: Path) -> None:
    """The third answer, and the only way to open the Chat UI without a monitor
    - which is why it is a WORD: every direct target must carry a port, so
    ``none`` could never have been one."""
    config = _launch(tmp_path).config
    assert cli.resolve_monitor_target("none", None, config, {}) is None


def test_the_help_names_all_four_monitor_spellings() -> None:
    help_text = cli.build_arg_parser().format_help()
    assert "local|none|HOST:PORT|@NAME" in help_text
    for word in ("local", "none", "agentclip-monitor"):
        assert word in help_text


def test_calibrate_is_refused_with_the_command_that_replaced_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--tui``'s arrangement, for ``--tui``'s reason (ui-monitor.md 9.0): the
    flag survives one release as a stub that names its replacement, because an
    argparse "unrecognized arguments" tells a script that carried it nothing."""
    monkeypatch.setattr(cli, "local_launch", lambda args: pytest.fail("resolved a launch"))

    assert cli.main(["--calibrate", "--project", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert cli.CALIBRATE_REMOVED in err
    assert "agentclip-monitor" in err
