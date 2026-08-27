"""The calibration WINDOW: its assets, its js_api, and the two entry points.

The half of phase 4A that is about pywebview rather than about pixels
(docs/design/ui-monitor.md §6.4). Nothing here opens a window: what is checked
is the page this window would load and the marshalling shim the page reaches -
two of the three things ``tests/shell/chat/test_shell.py`` checks for the chat
window, because this is the repo's SECOND ``create_window`` and none of it had a
precedent. The third, the standalone dispatch, moved to ``test_main.py`` when
``agentclip-monitor`` became this window's entry point (9.1).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from agentclip import __version__
from agentclip.config import load_config
from agentclip.shell.monitor_ui.view import CalibrationView
from agentclip.shell.monitor_ui.window import (
    ASSET_PACKAGE,
    WINDOW_TEXT_SELECT,
    CalibrationBridge,
    CalibrationCalls,
    CalibrationJsApi,
    CalibrationRunner,
    asset_dir,
    entry_url,
)
from agentclip.shell.webview.assets import ASSET_DIR, ASSET_NAMES, ENTRY_PAGE

# The MONITOR's spec, not the app's: ui-monitor.md §10.3 took the whole screen
# half out of ``agentclip.exe``, this page with it, and the only binary that
# ships the Monitor UI is ``agentclip-monitor``.
SPEC = Path(__file__).resolve().parents[3] / "packaging" / "agentclip-monitor.spec"


def asset(name: str) -> str:
    return files(ASSET_PACKAGE).joinpath(ASSET_DIR, name).read_text(encoding="utf-8")


# == the page ================================================================


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_every_asset_ships_and_says_something(name: str) -> None:
    """Package data, reached the way the window reaches it. An asset that is in
    the repo but not resolvable through ``importlib.resources`` is an asset a
    frozen build will not find either."""
    assert asset(name).strip()


def test_the_page_pulls_in_its_own_bundle_and_not_the_chat_windows() -> None:
    """Two windows, two bundles: a relative href, so the calibration page can
    never end up rendering the chat shell's stylesheet from a file:// origin
    that has no idea the other directory exists."""
    html = asset(ENTRY_PAGE)
    assert '<link rel="stylesheet" href="app.css" />' in html
    assert '<script src="app.js"></script>' in html
    assert "../assets/" not in html


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_asset_reaches_the_network(name: str) -> None:
    """A file:// origin can reach nothing, and there is nothing here that wants
    to: the thumbnails and the crops arrive as ``data:`` URIs built in Python."""
    text = asset(name)
    assert "http://" not in text and "https://" not in text
    for reach in ("fetch(", "XMLHttpRequest", "WebSocket", "import("):
        assert reach not in text


def test_the_page_draws_the_editor_the_column_and_the_two_overlays() -> None:
    """Every id the renderers write into has to exist in the markup, and the two
    fullscreen child processes need a door apiece."""
    html = asset(ENTRY_PAGE)
    js = asset("app.js")
    for element in ("svc-select", "svc-kinds", "svc-close", "el-rows", "elements-title",
                    "calib-tabs", "calib-region", "scrim", "toasts"):
        assert f'id="{element}"' in html, element
    assert 'api("set_region")' in js
    assert 'api("identify")' in js
    assert 'api("elements", elementsOpen)' in js
    assert 'api("svc_close")' in js


def test_the_page_draws_the_serve_panel_and_can_reach_all_four_of_its_verbs() -> None:
    """The Serve panel's markup and its four doors (ui-monitor.md 9.1). Every id
    the renderer writes into has to exist, and every verb the panel needs has to
    be raised from somewhere - a button with no ``api(...)`` behind it is a click
    that silently does nothing."""
    html = asset(ENTRY_PAGE)
    js = asset("app.js")
    for element in (
        "serve",
        "serve-address",
        "serve-port",
        "serve-toggle",
        "serve-status",
        "link-badge",
        "serve-warning",
        "serve-error",
        "serve-token",
        "serve-copy",
        "serve-regenerate",
        "serve-no-token",
    ):
        assert f'id="{element}"' in html, element
    assert 'api("serve_start"' in js
    assert 'api("serve_stop")' in js
    assert 'api("token_regenerate")' in js
    # The one verb with an ANSWER: the token comes back through pywebview's
    # return path so the page can write it to the clipboard inside the click
    # that asked for it.
    assert 'apiAsk("token_copy"' in js
    assert 'case "serve":' in js


def test_the_serve_panel_is_hidden_until_a_panel_says_otherwise() -> None:
    """The window the Chat UI opens beside itself in local mode has no Serve
    panel at all - there is nothing to serve when the brain is in the same
    process - and the page learns that by never being sent a ``serve``
    event."""
    assert '<section class="serve" id="serve" hidden>' in asset(ENTRY_PAGE)
    assert "el.serve.hidden = false;" in asset("app.js")


def test_the_stylesheet_names_no_colour_outside_its_palette() -> None:
    """Every colour is a variable, so the day this window grows a theme picker
    is a day of adding blocks rather than of hunting hexes - the chat bundle's
    own rule, kept."""
    css = asset("app.css")
    head, _, rest = css.partition("/* -- the frame ---")
    assert "#" in head  # the palette is where the hexes live...
    body = rest.split("\n")
    offenders = [line for line in body if "#" in line and "--" not in line and "/*" not in line]
    assert offenders == [], offenders


def test_the_palettes_are_the_chat_windows_own_copied_verbatim() -> None:
    """§11.7: this window wears the palette the attached Chat UI picked, and the
    four palettes are one set of colours in two files.

    A COPY with a named source of truth rather than a shared stylesheet: each
    page is loaded from its own package's ``assets/`` over a file:// URL and
    each spec collects exactly that directory. So the guard is this test - the
    three blocks have to be there character for character, and the comment has
    to say where they came from.
    """
    monitor_css = asset("app.css")
    chat_css = files("agentclip.shell.chat").joinpath(ASSET_DIR, "app.css").read_text("utf-8")
    for name in ("light", "claude-warm", "claude-dark"):
        block = _palette(chat_css, name)
        assert block in monitor_css, name
    assert "shell/chat/assets/app.css" in monitor_css, "the source of truth is not named"


def _palette(css: str, name: str) -> str:
    """One ``body.theme-<name>`` block, opening line to closing brace."""
    start = css.index(f"body.theme-{name} {{")
    end = css.index(chr(10) + "}" + chr(10), start)
    return css[start : end + 3]


def test_the_page_wears_the_theme_the_serve_event_carries() -> None:
    """One class on <body> per palette, and the default wears none at all -
    ``:root`` is already that palette. Painted from the ``serve`` event, because
    that is the event that knows a Chat UI is on the line."""
    js = asset("app.js")
    assert "applyTheme(event.theme);" in js
    assert 'document.body.classList.add("theme-" + theme)' in js
    assert 'var THEME_DEFAULT = "dark";' in js


def test_the_entry_url_is_a_local_file_carrying_the_version(tmp_path: Path) -> None:
    """A ``file://`` URL, not a bare path: pywebview spins up a local Bottle HTTP
    server for a plain local path, and this window has nothing to serve."""
    url = entry_url(tmp_path)
    assert url.startswith("file:///")
    assert ENTRY_PAGE in url
    assert url.endswith(f"#v={__version__}")


def test_the_asset_directory_is_a_real_directory_while_it_is_open() -> None:
    with asset_dir() as assets:
        assert assets.is_dir()
        assert {p.name for p in assets.iterdir()} >= set(ASSET_NAMES)


def test_the_window_lets_the_user_select_text() -> None:
    """pywebview's default injects ``body {user-select: none}`` AFTER the
    stylesheet loads, so no rule in app.css can win it back - and a service key
    nobody can copy out of the form is a form that fights its user."""
    assert WINDOW_TEXT_SELECT is True


def test_the_spec_ships_the_page_where_importlib_resources_will_look() -> None:
    """The frozen build's half of ``asset_dir``. PyInstaller collects only what
    the spec names, and the destination has to be this PACKAGE's relative path
    or ``files("agentclip.shell.monitor_ui")`` finds nothing under
    ``_MEIPASS``.

    Asserted against ``agentclip-monitor.spec`` because that is now the only
    binary this window is in (§10.3): the app's spec dropped the block, and a
    test still pointed at it would be pinning the page into an exe that never
    opens it."""
    text = SPEC.read_text(encoding="utf-8")
    assert '"agentclip/shell/monitor_ui/assets"' in text
    assert "MONITOR_UI_ASSETS" in text


# == the js_api ==============================================================


class Calls:
    """Every intent, recorded. Structurally a :class:`CalibrationCalls`."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any) -> None:
            self.seen.append((name, args))

        return record


def test_the_js_api_marshals_every_intent_the_page_can_raise() -> None:
    """One line per verb, and the point of the test is coverage: a name on the
    page with no method here is a click that silently does nothing."""
    calls = Calls()
    api = CalibrationJsApi(calls)  # type: ignore[arg-type]
    api.ready()
    api.slot("SUB-AGENT")
    api.set_region()
    api.identify()
    api.elements(True)
    api.prompt("c1", False)
    api.close_window()
    api.svc_select("chatgpt")
    api.svc_form({"key": "x"})
    api.svc_detection({"signals": ["stale"]})
    api.svc_edit_by_lines(True)
    api.svc_after_delivery({"snap_back": False})
    api.svc_scroll("end")
    api.svc_matcher("anchors")
    api.svc_tolerance(7)
    api.svc_add()
    api.svc_reset()
    api.svc_delete()
    api.svc_capture("copy")
    api.svc_prev("copy")
    api.svc_next("copy")
    api.svc_clear("copy")
    api.svc_click_point("copy", 10, 20)
    api.svc_forget()
    api.svc_close()

    names = [name for name, _ in calls.seen]
    assert names == [
        "page_ready", "select_slot", "set_chat_region", "show_identify",
        "set_elements_visible", "answer_prompt", "close_from_page",
        "svc_select", "svc_form", "svc_detection", "svc_edit_by_lines",
        "svc_after_delivery", "svc_scroll", "svc_matcher", "svc_tolerance",
        "svc_add", "svc_reset", "svc_delete", "svc_capture", "svc_prev",
        "svc_next", "svc_clear", "svc_click_point", "svc_forget", "svc_close",
    ]
    assert ("svc_click_point", ("copy", 10, 20)) in calls.seen
    assert ("set_elements_visible", (True,)) in calls.seen


def test_the_js_api_marshals_the_serve_panel_too() -> None:
    """The four verbs 9.1 added, and the coercions they do at the boundary: a
    page hands back whatever an <input> held, and a port is an int."""
    calls = Calls()
    api = CalibrationJsApi(calls)  # type: ignore[arg-type]
    api.serve_start("192.168.1.40", "7788", 1)  # type: ignore[arg-type]
    api.serve_stop()
    api.token_regenerate()
    assert calls.seen == [
        ("serve_start", ("192.168.1.40", 7788, True)),
        ("serve_stop", ()),
        ("token_regenerate", ()),
    ]


def test_the_token_verb_answers_and_never_raises_at_the_page() -> None:
    """``token_copy`` is the one js_api method with a return value, so it cannot
    use the swallow-and-drop path the others do - and it must still swallow,
    because pywebview would otherwise leave the page holding a rejected promise
    and no explanation."""

    class Panelless:
        def token_copy(self) -> str:
            return "cafebabe"

    class Boom:
        def token_copy(self) -> str:
            raise RuntimeError("nope")

    assert CalibrationJsApi(Panelless()).token_copy() == "cafebabe"  # type: ignore[arg-type]
    assert CalibrationJsApi(Boom()).token_copy() == ""  # type: ignore[arg-type]


def test_a_raising_intent_is_swallowed_rather_than_dropped_by_pywebview() -> None:
    """pywebview logs and drops what a js_api method raises, so a failure here
    would be a click that silently did nothing AND a promise the page is still
    holding."""

    class Boom:
        def page_ready(self) -> None:
            raise RuntimeError("nope")

    CalibrationJsApi(Boom()).ready()  # type: ignore[arg-type]


def test_the_runner_answers_every_call_the_js_api_makes() -> None:
    """The Protocol is the contract between the two halves; a verb declared and
    not implemented would only fail when a user pressed it."""
    verbs = [name for name in dir(CalibrationCalls) if not name.startswith("_")]
    assert "svc_close" in verbs and "close_from_page" in verbs  # the listing is real
    for name in verbs:
        assert callable(getattr(CalibrationRunner, name, None)), name


# == the runner ==============================================================


def test_the_runner_borrows_a_loop_when_one_is_offered(tmp_path: Path) -> None:
    """Local mode (phase 4B) hands ``GuiRunner.schedule`` in, so both windows'
    coroutines run on the one loop that shell already owns rather than on a
    second one whose teardown would have to be ordered against it."""
    scheduled: list[str] = []
    runner = CalibrationRunner(
        config=load_config(tmp_path, global_config_path=tmp_path / "none.toml"),
        monitor=_NullMonitor(),
        profile_root=tmp_path / "profiles",
        schedule=lambda coro: (scheduled.append(repr(coro)), coro.close())[0],
    )
    assert runner._owns_loop is False
    runner.start()  # a no-op: there is no loop of its own to start
    assert runner._thread is None
    runner.view._retarget()
    assert scheduled  # ...and the work went to the loop that was handed in
    runner.stop()


def test_the_runner_owns_a_loop_when_none_is(tmp_path: Path) -> None:
    runner = CalibrationRunner(
        config=load_config(tmp_path, global_config_path=tmp_path / "none.toml"),
        monitor=_NullMonitor(),
        profile_root=tmp_path / "profiles",
    )
    assert runner._owns_loop is True
    assert isinstance(runner.bridge, CalibrationBridge)
    assert isinstance(runner.view, CalibrationView)
    # Idempotent, and free without a window: both the pump's return and an
    # explicit close reach it.
    runner.stop()
    runner.stop()


class _NullMonitor:
    """The smallest thing that satisfies ``CalibrationMonitor``."""

    async def configure(self, spec: Any) -> int:
        return 1

    def set_spec_for(self, spec_for: Any) -> None:
        """§10.5's seam: the view installs its own ``spec_for`` at construction,
        so the smallest possible monitor has to be able to take one."""
        self.spec_for = spec_for

    async def suspend(self) -> None: ...
    async def resume(self) -> None: ...
    async def close(self) -> None: ...

    def capture(self, region: Any) -> Any:
        raise AssertionError("nothing here captures")

    def on_frame(self, hook: Any) -> Any:
        return lambda: None

    def on_theme(self, hook: Any) -> Any:
        """§11.7's palette hook. Subscribed at construction like ``spec_for``,
        so the smallest possible monitor has to answer it too."""
        return lambda: None


# == the standalone door =====================================================
# ``agentclip --calibrate`` used to live here: a second process spelling of this
# window, opened from the app binary. ui-monitor.md 9.1 removed it - the
# standalone door is ``agentclip-monitor`` and there is exactly one of it - so
# what those tests checked is checked one file over, in ``test_main.py``. What
# stays is ``open_calibration_window``, the window assembly ``run_monitor_ui``
# builds on - the Chat UI's F2 used it until ui-monitor.md 11.2 deleted that
# door, so this binary is now its only caller.


# == the import graph ========================================================


def test_the_calibration_package_drags_in_no_session_machinery() -> None:
    """The design's claim, enforced: ``--calibrate`` needs a config and a
    monitor, so a subprocess that imports only this package must not end up with
    the chat shell, the session controller or an engine in ``sys.modules``.

    A subprocess rather than a ``sys.modules`` check in-process, because the
    rest of this suite has already imported everything.
    """
    code = (
        "import sys;"
        " import agentclip.shell.monitor_ui as c;"
        " bad = [m for m in ('agentclip.shell.app', 'agentclip.shell.chat.view',"
        " 'agentclip.engine.link.factory') if m in sys.modules];"
        " print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout


def test_importing_the_window_does_not_import_pywebview() -> None:
    """The ``gui`` extra is optional, so importing this module must stay free -
    every pywebview name is reached inside a function."""
    module = importlib.import_module("agentclip.shell.monitor_ui.window")
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert "\nimport webview" not in source
    assert "\nfrom webview" not in source


def test_hidden_beats_every_author_display_rule() -> None:
    """The scrim is `display: flex` and starts `hidden`. Without an !important
    [hidden] rule the UA's display:none loses and the empty confirm modal
    covers the page from the first paint - the monitor is unusable."""
    css = asset("app.css")
    assert "[hidden] {" + chr(10) + "  display: none !important;" + chr(10) + "}" in css
    assert css.index("[hidden] {") < css.index(".scrim {")


def test_the_link_badge_is_painted_from_the_serve_event_in_three_colours() -> None:
    """Whether a Chat UI is on the line must be visible from across the room:
    a header badge whose FILL is the state, painted off the event's `link`."""
    js = asset("app.js")
    css = asset("app.css")
    assert 'paintLink(event.link || "off", event.peer || "", event.driving || "")' in js
    for state in ("off", "waiting", "attached"):
        assert f"{state}:" in js, state
    assert ".link-badge.waiting" in css and ".link-badge.attached" in css
