"""``agentclip-monitor``: one parser, two doors.

``docs/design/ui-monitor.md`` §9.1. The console script points at
``agentclip.shell.monitor_ui.__main__`` now, and everything that module does is
choose: ``--headless`` goes to the Driver's ``main`` verbatim (the windowless
server for a VM with no desktop), anything else opens the Monitor UI. So what is
checked here is the DISPATCH and the assembly it hands over - never a window,
never a socket, never a real clipboard.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentclip import __version__
from agentclip.driver.monitor import __main__ as driver_main
from agentclip.driver.monitor.local import LocalUIMonitor
from agentclip.shell.monitor_ui import __main__ as entry
from agentclip.shell.monitor_ui.serve import DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def argv(project: Path, tmp_path: Path, *rest: str) -> list[str]:
    """A command line that reads and writes nothing outside ``tmp_path``."""
    return [
        "--project",
        str(project),
        "--global-config",
        str(tmp_path / "none.toml"),
        "--profile-root",
        str(tmp_path / "profiles"),
        "--config-dir",
        str(tmp_path / "monitor"),
        *rest,
    ]


# == the console script ======================================================


def test_the_console_script_points_at_this_module() -> None:
    """The rename is the phase: ``[project.scripts]`` is what makes the binary
    open a window rather than only serve a port."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        data["project"]["scripts"]["agentclip-monitor"]
        == "agentclip.shell.monitor_ui.__main__:main"
    )


def test_the_frozen_spec_runs_this_module_as_its_entry_script() -> None:
    """PyInstaller runs an entry script as ``__main__``, so the frozen binary
    and ``python -m agentclip.shell.monitor_ui`` have to be the same door."""
    spec = (ROOT / "packaging" / "agentclip-monitor.spec").read_text(encoding="utf-8")
    assert '"agentclip", "shell", "monitor_ui", "__main__.py"' in spec
    # And the window it now opens has to find its page under _MEIPASS.
    assert '"agentclip/shell/monitor_ui/assets"' in spec
    assert '"webview",' in spec


# == --headless ==============================================================


def test_headless_delegates_to_the_driver_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ORIGINAL argv, not a namespace re-spelled as a command line: the
    windowless door owns its own validation and its own sentences, and
    re-deriving one from the other is how two doors start to disagree."""
    seen: list[list[str] | None] = []
    monkeypatch.setattr(entry, "headless_main", lambda argv: (seen.append(argv), 0)[1])
    monkeypatch.setattr(
        entry, "run_monitor_ui", lambda *a, **k: pytest.fail("--headless opened a window")
    )
    line = ["--headless", "--port", "7777", "--bind", "127.0.0.1"]
    assert entry.main(line) == 0
    assert seen == [line]


def test_headless_still_needs_a_port() -> None:
    """The Serve panel is where a port is asked for now, and ``--headless`` has
    no panel: a windowless monitor with no port is a usage error, said with the
    usage line beside it."""
    with pytest.raises(SystemExit) as exit_code:
        driver_main.main(["--headless"])
    assert exit_code.value.code == 2


def test_a_window_run_needs_no_port(project: Path, tmp_path: Path) -> None:
    """The change ``--port`` had to make for any of this to work."""
    args = driver_main.build_arg_parser().parse_args(argv(project, tmp_path))
    assert args.port is None
    assert args.headless is False


# == the version and the matcher list ========================================


@pytest.mark.parametrize("flag", ["--version", "--list-matchers"])
def test_the_build_smoke_tests_answer_from_inside_parsing(flag: str) -> None:
    """Both build scripts run these against the frozen binary. They have to exit
    before either door is chosen, with no port, no config read and no socket."""
    with pytest.raises(SystemExit) as exit_code:
        entry.main([flag])
    assert exit_code.value.code == 0


def test_the_version_names_the_monitor(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        entry.main(["--version"])
    assert capsys.readouterr().out.strip() == f"agentclip-monitor {__version__}"


# == the window door =========================================================


def test_a_plain_run_opens_the_monitor_ui_over_this_machine(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config, config dir, profile root and a REAL clipboard provider - the one
    thing this window has that the Chat UI's calibration visit does not, because
    the Monitor owns the clipboard (§2.11)."""
    seen: dict[str, Any] = {}

    def opened(config: Any, **rest: Any) -> int:
        seen["config"] = config
        seen.update(rest)
        return 0

    monkeypatch.setattr(entry, "run_monitor_ui", opened)
    monkeypatch.setattr(
        entry, "headless_main", lambda argv: pytest.fail("a windowed run went headless")
    )
    assert entry.main(argv(project, tmp_path)) == 0
    assert seen["config"].services
    assert seen["config_dir"] == tmp_path / "monitor"
    assert seen["profile_root"] == tmp_path / "profiles"
    assert seen["provider"] is not None
    # Nothing on the command line asked for a port, so the panel comes up idle.
    assert seen["serve_at"] is None
    assert seen["no_token"] is False


def test_a_command_line_port_is_handed_to_the_panel(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--port`` and ``--bind`` pre-fill the Serve panel and arm its
    auto-start; ``--no-token`` rides along as the box's initial state."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        entry, "run_monitor_ui", lambda config, **rest: (seen.update(rest), 0)[1]
    )
    line = argv(project, tmp_path, "--port", "7788", "--bind", "192.168.1.40", "--no-token")
    assert entry.main(line) == 0
    assert seen["serve_at"] == ("192.168.1.40", 7788)
    assert seen["no_token"] is True


def test_a_token_flag_is_said_to_be_the_headless_doors(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not refused - a launcher may hand the same flags to both halves - but not
    silent either: the panel serves with the token in its config dir, and
    somebody whose ``--token`` is not the one on screen deserves to know why."""
    monkeypatch.setattr(entry, "run_monitor_ui", lambda config, **rest: 0)
    assert entry.main(argv(project, tmp_path, "--token", "abc")) == 0
    assert entry.TOKEN_FLAG_IGNORED in capsys.readouterr().err


# == the window itself, with pywebview faked out =============================


class _Events:
    def __init__(self) -> None:
        self.loaded: list[Any] = []

    def __iadd__(self, handler: Any) -> _Events:
        self.loaded.append(handler)
        return self


class _Window:
    def __init__(self) -> None:
        self.events = SimpleNamespace(loaded=_Events())
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)

    def destroy(self) -> None: ...


class _Webview:
    """pywebview, minus the pump. ``start()`` returns instead of blocking, which
    is what a window closing looks like from ``_run_window``'s side."""

    def __init__(self) -> None:
        self.windows: list[_Window] = []
        self.started = 0
        self.kwargs: dict[str, Any] = {}

    def create_window(self, title: str, **kwargs: Any) -> _Window:
        self.kwargs = dict(kwargs, title=title)
        window = _Window()
        self.windows.append(window)
        return window

    def start(self) -> None:
        self.started += 1


def test_run_monitor_ui_builds_the_window_and_the_serve_panel(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window path end to end with no display: one ``create_window``, one
    pump, and a Serve panel that minted a token into the config dir it was
    given. The monitor is injected so nothing here reaches this machine's
    screen or its clipboard."""
    from agentclip.config import load_config
    from agentclip.shell.monitor_ui import window as window_module

    fake = _Webview()
    monkeypatch.setitem(sys.modules, "webview", fake)
    config = load_config(project, global_config_path=tmp_path / "none.toml")
    config_dir = tmp_path / "monitor"

    assert (
        window_module.run_monitor_ui(
            config,
            config_dir=config_dir,
            profile_root=tmp_path / "profiles",
            monitor=LocalUIMonitor(profile_for=lambda key: None, regions_dir=config_dir),
        )
        == 0
    )
    assert fake.started == 1
    assert len(fake.windows) == 1
    assert fake.kwargs["js_api"] is not None
    # The token is on disk now, which is the whole reason a config dir is
    # required rather than defaulted: a secret nobody can find is no secret.
    assert (config_dir / "monitor-token").is_file()


def test_run_monitor_ui_says_so_when_the_toolkit_is_missing(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A frozen build without pywebview answers with a line a user can act on -
    and ``--headless`` is still there for a machine that never wanted one."""
    from agentclip.config import load_config
    from agentclip.shell.monitor_ui import window as window_module

    monkeypatch.setitem(sys.modules, "webview", None)
    config = load_config(project, global_config_path=tmp_path / "none.toml")
    code = window_module.run_monitor_ui(
        config,
        config_dir=tmp_path / "monitor",
        monitor=LocalUIMonitor(profile_for=lambda key: None),
    )
    assert code == 2
    assert "gui extra" in capsys.readouterr().err


# == the import graph ========================================================


def test_the_entry_point_imports_no_toolkit(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What keeps ``--headless`` honest on a server with no desktop: the
    dispatcher reaches ``webview`` only inside the function that creates a
    window, and the delegation happens before that."""
    source = Path(str(entry.__file__)).read_text(encoding="utf-8")
    assert "\nimport webview" not in source
    assert "\nfrom webview" not in source


def test_the_default_port_is_the_one_the_docs_write() -> None:
    """One number, in the panel and in every sentence about it."""
    assert DEFAULT_PORT == 7777


def test_the_picker_child_flags_are_answered_before_any_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Monitor UI's Capture button re-invokes THIS binary with
    --pick-region (screen.picker uses sys.executable). Before this the
    monitor's parser did not know the flag, so every Capture toasted
    "unrecognized arguments: --pick-region --pick-prompt"."""
    seen: list[str | None] = []
    monkeypatch.setattr(
        "agentclip.driver.screen.picker.pick_region_child",
        lambda prompt=None: (seen.append(prompt), 0)[1],
    )
    monkeypatch.setattr(
        entry, "run_monitor_ui", lambda *a, **k: pytest.fail("the child opened a window")
    )
    assert entry.main(["--pick-region", "--pick-prompt", "draw the chat"]) == 0
    assert seen == ["draw the chat"]
    # The headless door answers it too: the same parser, the same flag.
    assert driver_main.main(["--pick-region"]) == 0
    assert seen == ["draw the chat", None]
