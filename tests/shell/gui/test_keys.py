"""Parity increment 6: help, settings, the slash popup, the key chain, the quit gate.

The parity contract is ``docs/design/ui-briefs/modals-keys-esc.md`` - §3.3 and
§6.2 for the Esc stage machine, §5.1 for the key table, §5.2 for the slash
commands, §6.4 for the quit-mid-turn rule - and §2.2 for what F4 is (a theme
picker, and nothing else).

Two kinds of assertion live here and the split is deliberate. What a PRESS does
is Python's and is exercised through the real view (``js_api`` -> ``GuiView`` ->
controller), like every other key in ``tests/shell/gui/test_chrome.py``. What the PAGE
decides - which stage of Esc wins, when the popup opens, that the help sheet and
the key handler read one array - is asserted against ``app.js``'s source, which
is this suite's standing convention for page logic (there is no DOM here and no
window: ``tests/shell/gui/conftest.py``). The source assertions are written to bite:
they pin the ONE table and the absence of any second dispatch path, not the
presence of a string.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from agentclip.config import (
    DEFAULT_GUI_THEME,
    VALID_GUI_THEMES,
    load_config,
    save_gui_theme,
)
from agentclip.engine.engine import Phase
from agentclip.shell.app.commands import COMMANDS
from agentclip.shell.gui.bridge import JsApi, JsCalls
from agentclip.shell.gui.runner import GuiRunner
from agentclip.shell.gui.view import QUIT_BODY, QUIT_TITLE, THEME_CHOICES, GuiView
from tests.shell.gui.conftest import Harness, settle
from tests.shell.gui.test_view import ControllerSpy, session_view, snapshot

ASSETS = Path(__file__).resolve().parents[3] / "src" / "agentclip" / "shell" / "gui" / "assets"
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")


def api_of(harness: Harness) -> JsApi:
    return JsApi(harness.view)  # type: ignore[arg-type]


class KeySpy(ControllerSpy):
    """The three controller calls the new keys make."""

    def __init__(self) -> None:
        super().__init__()
        self.undos = 0
        self.exports = 0
        self.summaries = 0
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def undo(self) -> None:
        self.undos += 1

    def export_log(self) -> None:
        self.exports += 1

    def end_session(self) -> None:
        self.summaries += 1


def spy_on(harness: Harness) -> KeySpy:
    spy = KeySpy()
    harness.view._controller = spy  # type: ignore[assignment]
    return spy


def settled(harness: Harness, phase: Phase = Phase.AWAITING_REPLY, **kwargs: Any) -> None:
    """Put the view in the one state `u`/`e` are allowed in."""
    harness.view.render_state(session_view(snapshot=snapshot(phase), **kwargs))


# == the key table is one table ===============================================


def key_entries() -> list[dict[str, str]]:
    """Every KEYS row, as the source declares it.

    Split first, then read fields: one regex over the whole table would have to
    know the order the fields happen to be written in, and this test exists to
    guard the table's CONTENT, not its formatting.
    """
    table = APP_JS[APP_JS.index("var KEYS = [") :]
    table = table[: table.index("\n  ];")]
    entries = []
    for chunk in table.split("{ keys: [")[1:]:
        keys = chunk[: chunk.index("]")]
        on = re.search(r"on: \[([^\]]*)\]", chunk)
        section = re.search(r'section: "([^"]+)"', chunk)
        what = re.search(r'what: "([^"]*)"', chunk)
        assert on and section and what, chunk[:120]
        entries.append(
            {"keys": keys, "on": on.group(1), "section": section.group(1), "what": what.group(1)}
        )
    return entries


def test_every_key_the_page_binds_is_a_row_of_the_one_table() -> None:
    """The single-source property, asserted as the ABSENCE of a second door.

    The help sheet renders ``KEYS``; the dispatcher iterates ``KEYS``. The only
    way the two can drift is a binding that bypasses the table - a stray
    ``ev.key === "..."`` in the app-wide handler - so that is what this looks
    for. The composer's own handler is exempt and named: its keys (Enter,
    Shift+Enter, the arrows, Esc) belong to the textarea, are a different
    dispatcher, and the help sheet describes them as prose.
    """
    handler = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    handler = handler[: handler.index("\n  }\n")]
    compared = set(re.findall(r'ev\.key === "([^"]+)"', handler))
    # Escape is the stage machine's own (stages 4-6) and F1 closes the help
    # sheet it opened - both are about a modal being up, not about a binding.
    assert compared <= {"Escape", "F1"}, f"a key is dispatched outside KEYS: {compared}"
    assert "dispatchKey(ev, typing)" in handler


def test_the_help_sheet_is_rendered_from_the_key_table() -> None:
    help_fn = APP_JS[APP_JS.index("function openHelp()") :]
    help_fn = help_fn[: help_fn.index("\n  }\n")]
    # Not "contains the word KEYS": it iterates the sections and filters the
    # table, which is the only way a new row shows up without being typed twice.
    assert "KEY_SECTIONS.forEach" in help_fn
    assert "KEYS.filter" in help_fn
    assert "entry.keys.join" in help_fn and "entry.what" in help_fn


@pytest.mark.parametrize(
    "key",
    ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "shift+tab", "ctrl+x", "ctrl+o",
     "ctrl+q", "y", "n", "a", "u", "c", "i", "r", "w", "t", "e", "l", "x"],
)
def test_the_table_carries_every_binding_the_brief_lists(key: str) -> None:
    """§5.1's table, minus what a page has no equivalent for.

    Absent on purpose: ``ctrl+p`` (Textual's command palette - there is no
    palette here), ``ctrl+s`` (``ctrl+enter`` is this shell's one send chord,
    and it IS in the table), and the composer-local and modal-local rows, which
    belong to their own handlers.
    """
    assert any(f'"{key}"' in entry["keys"] for entry in key_entries())


def test_every_row_is_described_and_filed_under_a_section() -> None:
    sections = {"App", "Approval", "Session"}
    rows = key_entries()
    assert len(rows) >= 24
    for row in rows:
        assert row["what"].strip(), row
        assert row["section"] in sections, row
    assert 'var KEY_SECTIONS = ["App", "Approval", "Session"];' in APP_JS


def test_the_gui_documents_its_own_divergences_not_the_tuis() -> None:
    """The sheet shown must be THIS shell's bindings, recorded divergences and
    all - not a copy of the TUI's table. The newline key is the tell: the help
    says Shift+Enter and names Ctrl+J only as the difference, and no row of the
    key table binds Ctrl+J at all."""
    assert "Shift+Enter inserts a newline" in APP_JS
    assert "the TUI uses Ctrl+J" in APP_JS
    assert not any("ctrl+j" in entry["keys"].lower() for entry in key_entries())


# == the slash popup ===========================================================


def test_the_registry_crosses_whole_and_in_order(harness: Harness) -> None:
    spy_on(harness)
    harness.view.start()
    rows = harness.flush().last("commands")["rows"]
    assert [row["name"] for row in rows] == [command.name for command in COMMANDS]
    assert [row["label"] for row in rows] == [command.label for command in COMMANDS]
    assert [row["summary"] for row in rows] == [command.summary for command in COMMANDS]


def test_a_reload_gets_the_registry_back(harness: Harness) -> None:
    harness.view.page_ready()
    assert harness.flush().last("commands")["rows"]


def test_the_pages_filter_is_match_prefixs_three_closers() -> None:
    """``app/commands.py:match_prefix``, rule for rule.

    The DATA never leaves Python; the filtering does, because a round trip per
    keystroke would be latency for string work. So the three ways a line stops
    being a command in progress are pinned here against the source: the ``//``
    escape hatch, whitespace anywhere in the token, and no match at all.
    """
    fn = APP_JS[APP_JS.index("function matchPrefix(text)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert 'line.charAt(0) !== "/"' in fn
    assert 'line.slice(0, 2) === "//"' in fn
    assert "/\\s/.test(token)" in fn
    assert "command.name.indexOf(prefix) === 0" in fn


def test_nothing_is_highlighted_until_a_letter_is_typed() -> None:
    """A bare ``/`` offers the whole registry with no row lit - the second lock
    on the door ``/yolo`` sits behind (commands.py's ordering note). One letter
    arms row 0, and a redundant sync must not throw an arrow press away."""
    fn = APP_JS[APP_JS.index("function syncPopup()") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "el.composer.value.length > 1 ? 0 : null" in fn
    assert "if (!sameMatches(matches, popupMatches))" in fn


def test_an_arrow_arms_the_list_from_either_end_and_wraps() -> None:
    fn = APP_JS[APP_JS.index("function popupMove(delta)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "popupIndex = delta > 0 ? 0 : popupMatches.length - 1;" in fn
    assert "% popupMatches.length" in fn


def test_completing_writes_the_name_and_one_trailing_space() -> None:
    """Never the argument hint. The space is what makes the popup close itself
    (match_prefix rejects a token with whitespace), which is what makes the NEXT
    Enter a plain send."""
    fn = APP_JS[APP_JS.index("function popupComplete()") :]
    fn = fn[: fn.index("\n  }\n")]
    assert 'el.composer.value = "/" + popupMatches[popupIndex].name + " ";' in fn
    assert "if (popupIndex === null) return;" in fn


def test_enter_and_tab_are_swallowed_even_with_nothing_highlighted() -> None:
    """`/` then Enter must not send a lone slash to the model - the popup keeps
    the key and waits to be narrowed."""
    fn = APP_JS[APP_JS.index("function onComposerKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    popup_branch = fn[: fn.index('if (ev.key === "Enter" && !ev.shiftKey')]
    assert 'ev.key === "Enter" || ev.key === "Tab"' in popup_branch
    assert "popupComplete();" in popup_branch


def test_the_popup_is_suppressed_while_an_ask_user_answer_is_open() -> None:
    """The parity contract's sharpest edge (§6.1): in answer mode a leading
    slash is TEXT, so offering to complete one would be a lie about what Enter
    is going to do. The controller enforces the rule; this is the view's half."""
    assert 'if (composerMode === "answer" || el.composer.disabled) {' in APP_JS


def test_the_popup_cannot_take_focus() -> None:
    """The TUI's CommandPopup is one non-focusable Static and the caret never
    leaves the composer. Same here: no button, no tabindex, no listener on it."""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert '<div class="cmd-popup" id="cmd-popup" aria-hidden="true" hidden></div>' in html
    assert "el.popup.addEventListener" not in APP_JS
    paint = APP_JS[APP_JS.index("function popupPaint()") :]
    paint = paint[: paint.index("\n  }\n")]
    assert "<button" not in paint and "tabindex" not in paint


# == the Esc chain =============================================================


def esc_stage_order() -> list[int]:
    """The stage numbers in the order the source's comments claim them."""
    return [int(n) for n in re.findall(r"ESC STAGE (\d)", APP_JS)]


def test_the_composer_owns_stages_one_to_three_in_that_order() -> None:
    fn = APP_JS[APP_JS.index("function onComposerKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert [int(n) for n in re.findall(r"ESC STAGE (\d)", fn)] == [1, 3, 2]
    # Stage 1 returns rather than falling through, which is the whole of the
    # brief's warning: a frontend that skipped it would make "/" Esc Enter send
    # a bare slash to the model.
    stage_one = fn[fn.index("ESC STAGE 1") :]
    assert stage_one[: stage_one.index("}")].count("popupHide();") == 1
    # Stage 2 clears UNDOABLY - execCommand keeps the browser's own undo stack,
    # which is what history.checkpoint() buys on the other side.
    assert 'document.execCommand("delete")' in fn


def test_the_document_handler_owns_stages_four_five_and_six() -> None:
    fn = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert [int(n) for n in re.findall(r"ESC STAGE (\d)", fn)] == [5, 5, 4, 6]
    # Stage 5 first, and it returns: a modal owns the keyboard while it is up.
    assert fn.index("ESC STAGE 5") < fn.index("ESC STAGE 4")


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("pageModal", "closePageModal();"),
        ("summary", 'answer("close");'),
        ("text", "answer(null);"),
        ("confirm", "answer(false);"),
    ],
)
def test_each_modal_escapes_the_way_that_modal_says_no(surface: str, expected: str) -> None:
    """Stage 5 is not one behaviour - each screen owns its own meaning of Esc
    (§3.3 stage 5). A page screen closes; the summary dismisses "close"; a text
    prompt cancels with None; a confirm denies, exactly as pressing n does."""
    fn = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    branch = fn[fn.index("ESC STAGE 5, part one") :]
    assert expected in branch, surface


def test_the_service_editor_is_the_layer_below_the_scrim() -> None:
    fn = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert fn.index("if (modalUp())") < fn.index("if (editorOpen)")
    assert fn.index("if (editorOpen)") < fn.index("if (rejectOpen) closeReject();")


def test_a_nearer_handler_stops_the_chain() -> None:
    """Stages 1-3 fire on the composer and the event then bubbles to the
    document. Without this guard, Esc-in-the-composer would ALSO close a
    reject-reason box that happened to be open - two stages for one press."""
    fn = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    body = [line.strip() for line in fn.split("\n") if line.strip() and not line.strip().startswith("//")]
    assert body[1] == "if (ev.defaultPrevented) return;"


# == the modal element is one element =========================================


def test_help_settings_payload_and_the_prompts_share_one_modal() -> None:
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert html.count('class="scrim" id="scrim"') == 1
    for opener in ("openHelp", "openSettings", "showPayload"):
        fn = APP_JS[APP_JS.index("function " + opener + "(") :]
        assert "openPageModal(" in fn[: fn.index("\n  }\n")], opener
    # ...and a parked flow always wins the element back.
    assert APP_JS.index("if (modalId) return false;") > 0


# == settings (F4) =============================================================


def test_f4_offers_what_the_tuis_settings_screen_offers_and_no_more() -> None:
    """§2.2: the TUI's SettingsScreen is a theme picker with a single
    "Appearance" tab. It does not touch [notify] bell/toast - those are
    file-only in both shells - so neither does this one."""
    assert [value for value, _ in THEME_CHOICES] == ["dark", "light"]
    assert set(value for value, _ in THEME_CHOICES) == set(VALID_GUI_THEMES)
    fn = APP_JS[APP_JS.index("function openSettings()") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "bell" not in fn and "toast" not in fn
    assert 'input.type = "radio"' in fn


def test_the_theme_crosses_at_start_and_after_a_reload(harness: Harness) -> None:
    spy_on(harness)
    harness.view.start()
    event = harness.flush().last("settings")
    assert event["theme"] == DEFAULT_GUI_THEME
    assert [row["value"] for row in event["themes"]] == ["dark", "light"]
    harness.recorder.clear()
    harness.view.page_ready()
    assert harness.flush().last("settings")["theme"] == DEFAULT_GUI_THEME


def test_picking_a_theme_saves_it_and_it_survives_a_reload(
    harness: Harness, tmp_path: Path, project: Path
) -> None:
    """The round trip that matters: what F4 wrote is what the next launch reads."""
    config_path = tmp_path / "global.toml"
    harness.view._global_config_path = config_path
    api_of(harness).theme("light")
    assert harness.view._config.gui.theme == "light"
    assert harness.flush().last("settings")["theme"] == "light"
    reloaded = load_config(project, global_config_path=config_path)
    assert reloaded.gui.theme == "light"
    assert not reloaded.warnings


def test_the_gui_theme_has_its_own_table_and_leaves_the_tuis_alone(
    tmp_path: Path, project: Path
) -> None:
    """The reason for a ``[gui]`` table rather than sharing ``[general] theme``:
    the GUI's palettes are CSS names and would be rejected there, warning on
    every TUI launch. Written minimally, and the other table is untouched."""
    config_path = tmp_path / "global.toml"
    config_path.write_text('[general]\ntheme = "claude-dark"\nservice = "claude"\n', encoding="utf-8")
    save_gui_theme("light", config_path)
    text = config_path.read_text(encoding="utf-8")
    assert '[gui]' in text and 'theme = "light"' in text
    loaded = load_config(project, global_config_path=config_path)
    assert loaded.general.theme == "claude-dark"
    assert loaded.general.service == "claude"
    assert loaded.gui.theme == "light"


def test_an_unknown_gui_theme_falls_back_and_says_so(tmp_path: Path, project: Path) -> None:
    config_path = tmp_path / "global.toml"
    config_path.write_text('[gui]\ntheme = "solarized"\n', encoding="utf-8")
    loaded = load_config(project, global_config_path=config_path)
    assert loaded.gui.theme == DEFAULT_GUI_THEME
    assert any("gui theme" in warning for warning in loaded.warnings)


def test_an_unsavable_theme_still_applies(harness: Harness, tmp_path: Path) -> None:
    """The same trade ``_persist_services`` makes: remembering a preference is a
    convenience, never the point of the press."""
    harness.view._global_config_path = tmp_path / "nope" / "deep" / "x" / "global.toml"

    def boom(theme: str, path: Path | None = None) -> None:
        raise OSError("read-only")

    import agentclip.shell.gui.view as view_module

    original = view_module.save_gui_theme
    view_module.save_gui_theme = boom  # type: ignore[assignment]
    try:
        harness.view.set_theme("light")
    finally:
        view_module.save_gui_theme = original  # type: ignore[assignment]
    assert harness.view._config.gui.theme == "light"
    assert "could not save the theme" in harness.flush().last("toast")["message"]


def test_an_unknown_theme_name_from_the_page_is_ignored(harness: Harness) -> None:
    api_of(harness).theme("neon")
    assert harness.view._config.gui.theme == DEFAULT_GUI_THEME


def test_the_page_paints_a_real_light_palette() -> None:
    """"Ship dark-only and label it" was the fallback; this is the other branch,
    so the variables have to actually exist rather than the class being inert."""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    light = css[css.index("body.theme-light {") :]
    light = light[: light.index("\n}")]
    for token in ("--bg", "--bg-raised", "--line", "--text", "--text-dim",
                  "--accent", "--ok", "--warn", "--err", "--on-solid"):
        assert token + ":" in light, token
    # Nothing below the palette may hard-code a colour, or the theme is a lie.
    body = css[css.index("\n* {") :]
    assert not re.search(r":\s*#[0-9a-fA-F]{3,8}\b", body), "a colour escaped the palette"
    assert 'classList.toggle("theme-light"' in APP_JS


# == the remaining keys ========================================================


def test_u_reaches_the_controllers_undo_when_the_floor_is_back(harness: Harness) -> None:
    spy = spy_on(harness)
    settled(harness)
    api_of(harness).undo()
    assert spy.undos == 1


@pytest.mark.parametrize(
    ("kwargs", "phase"),
    [({"busy": True}, Phase.AWAITING_REPLY), ({}, Phase.REVIEW)],
)
def test_u_refuses_out_loud_mid_turn(harness: Harness, kwargs: dict, phase: Phase) -> None:
    """The TUI dims the key; this shell has no footer to dim it in, so it says
    why (increment 2's recorded divergence)."""
    spy = spy_on(harness)
    settled(harness, phase, **kwargs)
    api_of(harness).undo()
    assert spy.undos == 0
    assert "a turn is running" in harness.flush().last("toast")["message"]


def test_u_with_no_session_says_there_is_nothing_to_undo(harness: Harness) -> None:
    spy = spy_on(harness)
    api_of(harness).undo()
    assert spy.undos == 0
    assert "no session" in harness.flush().last("toast")["message"]


def test_l_exports_and_is_allowed_mid_turn(harness: Harness) -> None:
    """`l` is a read-only snapshot that never touches the engine, so the TUI
    gates it on the session alone - not on the phase."""
    spy = spy_on(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.REVIEW)))
    api_of(harness).export_log()
    assert spy.exports == 1


def test_l_with_no_session_refuses(harness: Harness) -> None:
    spy = spy_on(harness)
    api_of(harness).export_log()
    assert spy.exports == 0
    assert "nothing to export" in harness.flush().last("toast")["message"]


def test_t_never_leaves_the_page() -> None:
    """Putting the caret back in the chat box is a fact about this window, like
    F3's sidebar - and its gate is the box's own disabled flag, which Python
    already composed from the brief's precedence table."""
    fn = APP_JS[APP_JS.index("function focusComposer()") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "if (el.composer.disabled) return;" in fn
    assert "el.composer.focus();" in fn
    assert "api(" not in fn


def test_e_and_u_share_one_gate(harness: Harness) -> None:
    """``check_action`` has one clause for both; so does this."""
    spy = spy_on(harness)
    settled(harness, Phase.REVIEW)
    api_of(harness).undo()
    api_of(harness).end_session()
    assert (spy.undos, spy.summaries) == (0, 0)
    settled(harness, Phase.DONE)
    api_of(harness).undo()
    api_of(harness).end_session()
    assert (spy.undos, spy.summaries) == (1, 1)


# == the transcript prune ======================================================


def test_the_page_prunes_each_transcript_at_the_tuis_number() -> None:
    """``TranscriptPanel.MAX_EVENTS``, per WINDOW: the cap is about how much DOM
    one scroll carries, and each panel is its own scroll. The exported log is
    the archive and is built Python-side, which the prune never touches."""
    assert "var MAX_EVENTS = 500;" in APP_JS
    fn = APP_JS[APP_JS.index("function append(win, node, beat)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "while (box.childElementCount > MAX_EVENTS) box.removeChild(box.firstElementChild);" in fn
    # ...and it happens BEFORE the fit-or-park arithmetic, which measures
    # offsetTop and would read a stale one.
    assert fn.index("MAX_EVENTS") < fn.index("node.offsetHeight > viewport")


def test_pruning_never_touches_the_python_side_event_log(harness: Harness) -> None:
    """The page's cap is a rendering budget; ``render_log`` must still export
    everything that ever happened."""
    for index in range(600):
        harness.view._record(f"note {index}")
    assert len(harness.view._events["m1"]) == 600
    assert "note 0" in harness.view.render_log([])


# == quitting mid-turn =========================================================


class FakeWindow:
    """The window layer, minus the window. ``destroy`` is what pywebview's own
    ``destroy_window`` reaches, and it is recorded rather than performed."""

    def __init__(self) -> None:
        self.destroyed = 0

    def destroy(self) -> None:
        self.destroyed += 1


def runner_for(harness: Harness) -> tuple[GuiRunner, FakeWindow]:
    """A runner wrapped around the harness's view, with no window and no thread.

    ``schedule_call`` becomes a straight call and the view's ``spawn`` becomes a
    task on the test's own loop: the real ones hop onto the GUI's loop from
    another thread, and what these tests are about is the DECISION the closing
    handler makes, not the hop. The confirm flow itself is real - it opens the
    same modal every other blocking prompt does and is answered the same way.
    """
    window = FakeWindow()
    runner = object.__new__(GuiRunner)
    runner._quit_ok = threading.Event()  # type: ignore[attr-defined]
    runner._on_close = window.destroy  # type: ignore[attr-defined]
    runner.view = harness.view  # type: ignore[attr-defined]
    runner.schedule_call = lambda fn, *args: fn(*args)  # type: ignore[assignment]
    harness.view._schedule = asyncio.ensure_future  # type: ignore[assignment]
    # ...and the view's way out is the runner's, exactly as GuiRunner wires it
    # (``on_exit=self.request_close``) - which is where the approval flag is set.
    harness.view._on_exit = runner.request_close  # type: ignore[assignment]
    return runner, window


async def test_closing_is_cancelled_when_a_turn_would_be_lost(harness: Harness) -> None:
    """§6.4. ``False`` is what pywebview turns into ``args.Cancel = True``
    (``webview/event.py:Event.set`` -> ``winforms.py:on_closing``); it is the
    ONLY thing the window's own thread is allowed to do here."""
    runner, window = runner_for(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.REVIEW)))
    assert runner.window_closing() is False
    await settle()
    assert window.destroyed == 0
    # ...and the confirm went out, through the ordinary modal path.
    modal = harness.flush().last("modal")
    assert modal["modal"] == "confirm"
    assert modal["title"] == QUIT_TITLE
    assert modal["body"] == QUIT_BODY


@pytest.mark.parametrize(
    "state",
    [
        {"session_active": False},
        {"busy": False, "pending_approval": False, "awaiting_answer": False},
    ],
)
def test_closing_proceeds_when_there_is_no_turn_to_lose(harness: Harness, state: dict) -> None:
    runner, _window = runner_for(harness)
    harness.view.render_state(session_view(snapshot=snapshot(), **state))
    assert runner.window_closing() is None
    assert not harness.flush().of_type("modal")


def test_the_start_prompt_is_not_a_turn(harness: Harness) -> None:
    """The carve-out that matters: the inline "describe the task" prompt leaves
    the session worker parked on a future, which reads as busy - but there is
    nothing to lose, so quitting from the empty start screen must not warn."""
    runner, _window = runner_for(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.IDLE)))
    harness.view._awaiting_new_session = True
    assert runner.window_closing() is None


@pytest.mark.parametrize(
    ("busy", "approval", "answer"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_all_three_halves_of_mid_turn_count(
    harness: Harness, busy: bool, approval: bool, answer: bool
) -> None:
    harness.view.render_state(
        session_view(
            busy=busy,
            pending_approval=approval,
            awaiting_answer=answer,
            snapshot=snapshot(),
        )
    )
    assert harness.view.mid_turn is True


async def test_a_second_close_while_the_confirm_is_up_stacks_nothing(harness: Harness) -> None:
    """``action_quit``'s ``isinstance(self.screen, ConfirmScreen)`` guard."""
    runner, _window = runner_for(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.REVIEW)))
    assert runner.window_closing() is False
    await settle()
    assert runner.window_closing() is False
    await settle()
    assert len(harness.flush().of_type("modal")) == 1


async def test_confirming_the_quit_destroys_the_window(harness: Harness) -> None:
    runner, window = runner_for(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.REVIEW)))
    runner.window_closing()
    await settle()
    prompt_id = harness.flush().last("modal")["prompt_id"]
    harness.view.answer_prompt(prompt_id, True)
    await settle()
    assert window.destroyed == 1
    # The flag is down BEFORE destroy, because destroy raises `closing` again on
    # its way out and that one must sail through rather than re-ask.
    assert runner._quit_ok.is_set()
    assert runner.window_closing() is None


async def test_denying_the_quit_leaves_the_window_alone(harness: Harness) -> None:
    runner, window = runner_for(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.REVIEW)))
    runner.window_closing()
    await settle()
    harness.view.answer_prompt(harness.flush().last("modal")["prompt_id"], False)
    await settle()
    assert window.destroyed == 0
    assert not runner._quit_ok.is_set()
    # ...and the guard is released, so the next attempt asks again.
    assert runner.window_closing() is False


async def test_ctrl_q_asks_the_same_question(harness: Harness) -> None:
    runner, window = runner_for(harness)
    harness.view.render_state(session_view(busy=True, snapshot=snapshot(Phase.REVIEW)))
    api_of(harness).quit()
    await settle()
    assert window.destroyed == 0
    assert harness.flush().last("modal")["title"] == QUIT_TITLE


async def test_ctrl_q_with_nothing_in_flight_just_closes(harness: Harness) -> None:
    _runner, window = runner_for(harness)
    harness.view.render_state(session_view(snapshot=snapshot()))
    api_of(harness).quit()
    await settle()
    assert window.destroyed == 1
    assert not harness.flush().of_type("modal")


def test_the_closing_handler_reads_a_flag_and_posts_nothing_else() -> None:
    """The slice-2 hazard, pinned: ``closing`` runs on the window's own thread
    and the bridge drainer parks against that thread inside ``evaluate_js``, so
    this handler may not wait on anything. It reads two flags and calls
    ``schedule_call`` (``call_soon_threadsafe``, which does not block)."""
    source = (
        Path(__file__).resolve().parents[3]
        / "src" / "agentclip" / "shell" / "gui" / "runner.py"
    ).read_text(encoding="utf-8")
    fn = source[source.index("def window_closing(self)") :]
    fn = fn[: fn.index("\n    # -- lifecycle")]
    assert "self.view.shutdown()" not in fn
    assert ".join(" not in fn and ".wait(" not in fn and "stop()" not in fn
    assert "self.schedule_call(self.view.confirm_quit)" in fn
    assert fn.rstrip().endswith("return False")


def test_the_shell_subscribes_the_gate() -> None:
    shell = (
        Path(__file__).resolve().parents[3]
        / "src" / "agentclip" / "shell" / "gui" / "shell.py"
    ).read_text(encoding="utf-8")
    assert "window.events.closing += runner.window_closing" in shell


# == the bridge's catalogue ====================================================


def test_every_new_intent_is_on_the_protocol_the_runner_implements() -> None:
    for name in ("undo", "export_log", "set_theme", "request_quit"):
        assert callable(getattr(JsCalls, name, None)), name
        assert callable(getattr(GuiRunner, name, None)), name


def test_the_new_intents_reach_the_view(harness: Harness) -> None:
    for name in ("undo", "export_log", "set_theme", "request_quit"):
        assert callable(getattr(GuiView, name, None)), name


def test_the_catalogue_names_the_two_new_event_families() -> None:
    doc = __import__("agentclip.shell.gui.bridge", fromlist=["x"]).__doc__ or ""
    assert '{type: "commands",' in doc
    assert '{type: "settings",' in doc
