"""The GUI shell's slice 1: a window over packaged assets, and nothing else.

Nothing here opens a window. What these tests pin is everything about the shell
that can be true before the loop starts - that the page is really shipped, that
it reaches nothing off this machine, that importing the shell costs a TUI
launch nothing, and that --gui takes the branch that skips the terminal probe.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from agentclip import __version__, cli
from agentclip.config import load_config
from agentclip.driver.clip.base import select_provider
from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.hosts.local import LocalHost
from agentclip.shell.app.link import Link
from agentclip.shell.gui.shell import (
    ASSET_DIR,
    ASSET_NAMES,
    ASSET_PACKAGE,
    ENTRY_PAGE,
    MIN_WINDOW_SIZE,
    WINDOW_SIZE,
    WINDOW_TITLE,
    asset_dir,
    entry_url,
    run_gui,
)


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
        "import sys, agentclip.shell.gui.shell;"
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


def test_the_gui_flag_exists_and_defaults_off() -> None:
    args = cli.build_arg_parser().parse_args([])
    assert args.gui is False
    assert cli.build_arg_parser().parse_args(["--gui"]).gui is True


def test_gui_launch_runs_the_shell_and_never_probes_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sixel probe is a question for a terminal, asked over stdin/stdout.

    On the GUI path there is no terminal waiting to answer it, so the branch
    must sit above it (docs/design/gui.md section 2) - and the shell must be
    reached with the resolved Launch, not with raw argv.
    """
    launch = _launch(tmp_path)
    seen: list[object] = []
    kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli, "local_launch", lambda args: launch)
    monkeypatch.setattr(cli, "probe_terminal", lambda: seen.append("probe"))
    monkeypatch.setattr(
        "agentclip.shell.gui.shell.run_gui",
        lambda given, **rest: (seen.append(given), kwargs.update(rest))[0] or 0,
    )

    assert cli.main(["--gui", "--project", str(tmp_path)]) == 0
    assert seen == [launch]
    # ...and it is handed the shell-agnostic pieces the TUI path builds too, so
    # the two frontends cannot end up on two clipboard backends or two engine
    # factories (docs/design/gui.md section 0).
    assert kwargs["provider"] is not None
    assert callable(kwargs["engine_factory"])
    assert kwargs["mcp_manager"] is None  # no servers configured in a tmp project


def test_without_the_flag_the_tui_path_still_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twin of the test above: the probe is skipped BY the branch, not
    accidentally dropped for everyone."""
    monkeypatch.setattr(cli, "local_launch", lambda args: _launch(tmp_path))
    probed: list[str] = []
    monkeypatch.setattr(cli, "probe_terminal", lambda: probed.append("probe"))
    # The provider and the MCP runtime are built ABOVE the fork now (both shells
    # need them), so the TUI path is cut off at the first thing that is really
    # Textual's.
    monkeypatch.setattr(cli, "AgentClipApp", lambda **kwargs: _stop_here())

    with pytest.raises(_Stop):
        cli.main(["--project", str(tmp_path)])
    assert probed == ["probe"]


class _Stop(Exception):
    """Cut main() off right after the probe, before anything opens a TUI."""


def _stop_here() -> None:
    raise _Stop


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
            hidden=True,
        )
    try:
        assert window.title == WINDOW_TITLE
        assert (window.initial_width, window.initial_height) == WINDOW_SIZE
        assert window.min_size == MIN_WINDOW_SIZE
        assert str(window.original_url).startswith("file://")
    finally:
        del webview.windows[before:]


def test_the_webview2_check_answers_without_a_window() -> None:
    """It reads a verdict pywebview reaches at import time; it must never raise,
    whatever this machine has installed."""
    from agentclip.shell.gui.shell import webview2_missing

    assert webview2_missing() in (True, False)


def test_argparse_help_mentions_the_gui_shell() -> None:
    help_text = cli.build_arg_parser().format_help()
    assert "--gui" in help_text
    assert "GUI shell" in help_text


# == the packaging smoke ======================================================


def test_the_gui_smoke_flag_is_hidden() -> None:
    """It is a build-script probe, not a user-facing feature - like
    --pick-region and --show-identify beside it."""
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
    non-zero at BUILD time rather than at the user's first --gui."""
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
    assert '"agentclip/shell/gui/assets"' in spec, "the assets' destination is not the package path"
    assert "gui_datas" in spec
    assert "textual_datas + gui_datas" in spec, "the assets are collected but never handed to Analysis"
    # The backend gui/shell.py's webview2_missing() reads, which nothing static
    # reaches (webview/guilib.py picks it per platform at import time).
    assert "webview.platforms.winforms" in spec


def test_the_launch_protocol_matches_the_real_launch(tmp_path: Path) -> None:
    """LaunchLike is structural on purpose - cli sits ABOVE both shells, so the
    shell may not import the class it is handed. This is the cheap runtime half
    of that promise (mypy checks the rest at the cli.main call site)."""
    launch = _launch(tmp_path)
    for name in ("project_root", "config"):
        assert hasattr(launch, name), f"cli.Launch has no {name}"
