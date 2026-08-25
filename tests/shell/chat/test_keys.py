"""Parity increment 6: help, settings, the slash popup, the key chain, the quit gate.

The parity contract is ``docs/design/ui-briefs/modals-keys-esc.md`` - §3.3 and
§6.2 for the Esc stage machine, §5.1 for the key table, §5.2 for the slash
commands, §6.4 for the quit-mid-turn rule - and §2.2 for what F4 is (a theme
picker, and nothing else).

Two kinds of assertion live here and the split is deliberate. What a PRESS does
is Python's and is exercised through the real view (``js_api`` -> ``GuiView`` ->
controller), like every other key in ``tests/shell/chat/test_chrome.py``. What the PAGE
decides - which stage of Esc wins, when the popup opens, that the help sheet and
the key handler read one array - is asserted against ``app.js``'s source, which
is this suite's standing convention for page logic (there is no DOM here and no
window: ``tests/shell/chat/conftest.py``). The source assertions are written to bite:
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
    VALID_THEMES,
    load_config,
    save_gui_theme,
)
from agentclip.engine.engine import Phase
from agentclip.shell.app.commands import COMMANDS
from agentclip.shell.chat.runner import GuiRunner
from agentclip.shell.chat.view import QUIT_BODY, QUIT_TITLE, THEME_CHOICES, GuiView
from agentclip.shell.webview.bridge import JsApi, JsCalls
from tests.shell.chat.conftest import Harness, settle
from tests.shell.chat.test_view import ControllerSpy, session_view, snapshot

ASSETS = Path(__file__).resolve().parents[3] / "src" / "agentclip" / "shell" / "chat" / "assets"
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
    Shift+Enter, Ctrl+J, the arrows, Esc) belong to the textarea, are a
    different dispatcher, and the help sheet describes them as prose.
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
    ["F1", "F2", "F3", "F4", "F5", "F6", "F8", "shift+tab", "ctrl+x", "ctrl+o",
     "ctrl+q", "y", "n", "a", "u", "c", "i", "r", "w", "t", "e", "l", "x"],
)
def test_the_table_carries_every_binding_the_brief_lists(key: str) -> None:
    """§5.1's table, minus what a page has no equivalent for.

    Absent on purpose: ``ctrl+p`` (there is no palette - the composer's slash
    commands are the one command surface), ``ctrl+s`` (``ctrl+enter`` is this shell's
    one send chord, and it IS in the table), and the composer-local and
    modal-local rows, which belong to their own handlers.
    """
    assert any(f'"{key}"' in entry["keys"] for entry in key_entries())


def test_every_row_is_described_and_filed_under_a_section() -> None:
    sections = {"App", "Approval", "Session"}
    rows = key_entries()
    assert len(rows) >= 23
    for row in rows:
        assert row["what"].strip(), row
        assert row["section"] in sections, row
    assert 'var KEY_SECTIONS = ["App", "Approval", "Session"];' in APP_JS


def test_the_newline_chord_is_both_shift_enter_and_ctrl_j() -> None:
    """Shift+Enter is the web-native newline; Ctrl+J is the TUI's, honored here
    too so the muscle memory transfers between shells. Both live in the
    composer's OWN handler - no row of the key table binds Ctrl+J, because a
    newline outside the composer means nothing."""
    assert "Shift+Enter inserts a newline" in APP_JS
    assert "so does Ctrl+J" in APP_JS
    handler = APP_JS[APP_JS.index("function onComposerKey(ev)") :]
    handler = handler[: handler.index("\n  }\n")]
    assert '(ev.key === "j" || ev.key === "J") && ev.ctrlKey' in handler
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


def test_the_question_gets_a_panel_of_its_own_above_the_composer() -> None:
    """A question is a STOP, and the transcript note the TUI leans on scrolls
    away. The banner is the gate's structural twin - pinned above the box,
    styled by its own rules rather than the gate's id-scoped ones."""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert '<section class="ask" id="ask-banner" hidden>' in html
    assert 'id="ask-question"' in html
    assert html.index('id="ask-banner"') > html.index('id="gate"')
    assert html.index('id="ask-banner"') < html.index('<footer class="composer">')
    assert "ANSWER NEEDED" in html and "Esc again cancels the question" in html
    for rule in (".ask {", ".ask-title {", ".ask-question {", ".ask-hint {"):
        assert rule in css, rule


def test_the_popup_cannot_take_focus() -> None:
    """The TUI's CommandPopup is one non-focusable Static and the caret never
    leaves the composer. Same here: no button, no tabindex, no listener on it."""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert '<div class="cmd-popup" id="cmd-popup" aria-hidden="true" hidden></div>' in html
    assert "el.popup.addEventListener" not in APP_JS
    paint = APP_JS[APP_JS.index("function popupPaint()") :]
    paint = paint[: paint.index("\n  }\n")]
    assert "<button" not in paint and "tabindex" not in paint


# == the send history the arrows walk ==========================================
# tui.md §3.3d, and the same rules on this side. Structure, not formatting: what
# these pin is the ORDER the three claimants on up/down are tried in, and the
# guards that keep the middle one from ever being skipped.


def composer_key_body() -> str:
    fn = APP_JS[APP_JS.index("function onComposerKey(ev)") :]
    return fn[: fn.index("\n  }\n")]


def test_the_arrows_reach_the_history_only_after_the_popup_has_refused() -> None:
    """Three claimants on one pair of keys, and the order is the whole design:
    the open popup, then the textarea's caret, then the history. A history
    branch above the popup's would make the arrows unable to pick a command."""
    fn = composer_key_body()
    assert fn.index("popupOpen()") < fn.index("sentOlder(value)")
    # ...and still ahead of Enter, so the send branch is untouched by any of it.
    assert fn.index("sentOlder(value)") < fn.index('ev.key === "Enter" && !ev.shiftKey')


def test_the_arrows_recall_only_from_the_first_and_last_line() -> None:
    """Otherwise a pasted traceback stops being navigable: one press in the
    middle of it would replace the whole box. `↑` needs no newline before the
    caret, `↓` none after it - which on a one-line box is always true, so it
    behaves like every other chat client with no rule to learn."""
    fn = composer_key_body()
    assert 'value.slice(0, from).indexOf("\\n") === -1' in fn
    assert 'value.slice(to).indexOf("\\n") === -1' in fn
    # A live selection is excluded outright: there the arrows are how a
    # selection is grown, and there is no caret to be "on the first line".
    assert "var collapsed = from === to;" in fn
    assert fn.count("collapsed &&") == 2
    # Modifier-free only, so ctrl/alt/shift+arrow keep whatever they meant.
    assert "!ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey" in fn


def test_declining_the_key_leaves_it_to_the_caret() -> None:
    """The ends of the walk are not dead keys. `sentOlder`/`sentNewer` answer
    null at the oldest entry and outside browse mode, and only a non-null answer
    is preventDefault()ed - so `↓` in a box nobody walked up from is an
    ordinary caret key, exactly as it is in the TUI."""
    fn = composer_key_body()
    assert "if (recalled !== null) {" in fn
    assert fn.index("if (recalled !== null) {") < fn.index("ev.preventDefault();\n        recallSent")


def test_the_one_send_door_is_what_grows_the_history() -> None:
    """Enter and the Send button meet in `send()`, so that is the only place the
    list can grow - and the push has to come BEFORE the clear or there is
    nothing left to remember."""
    fn = APP_JS[APP_JS.index("function send()") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "sentPush(text);" in fn
    assert fn.index("sentPush(text);") < fn.index('el.composer.value = "";')


def test_editing_the_box_ends_the_walk() -> None:
    """The page's half of `ChatComposer._text_changed`: the one `input` listener
    that already re-decides the popup also drops browse mode, so what is in the
    box after a keystroke is the user's draft and the next `↑` starts again from
    the newest. A recall must not trip it, or it would undo its own position."""
    boot = APP_JS[APP_JS.index('el.composer.addEventListener("input"') :]
    boot = boot[: boot.index("});")]
    assert "if (!recalling) sentReset();" in boot
    assert "syncPopup();" in boot
    recall = APP_JS[APP_JS.index("function recallSent(text)") :]
    recall = recall[: recall.index("\n  }\n")]
    assert "recalling = true;" in recall and "recalling = false;" in recall
    assert "el.composer.selectionStart = el.composer.selectionEnd = text.length;" in recall


def test_the_composer_walks_its_history_by_the_documented_rules() -> None:
    """These arrows are muscle memory. The cap was asserted equal to the deleted
    shell's constant until phase 6 (docs/design/ui-monitor.md 6.6); it is a
    literal here now, which is where the last copy of it lives."""
    assert "var SENT_MAX = 50;" in APP_JS
    fn = APP_JS[APP_JS.index("function sentPush(text)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "if (!text.trim()) return;" in fn  # blanks are not sends
    assert "sent[sent.length - 1] === text" in fn  # consecutive duplicates collapse
    assert "sent.slice(-SENT_MAX)" in fn
    assert "sentReset();" in fn  # a send ends the walk
    newer = APP_JS[APP_JS.index("function sentNewer()") :]
    newer = newer[: newer.index("\n  }\n")]
    assert "var draft = sentDraft;" in newer  # past the newest, the draft comes back


def test_the_history_does_not_shadow_the_windows_own() -> None:
    """`window.history` is a real API and `var history = []` inside this closure
    would silently take it away from everything in the file."""
    assert not re.search(r"\bvar history\b", APP_JS)


def test_the_help_sheet_says_what_the_arrows_do() -> None:
    """A binding a user cannot discover is a binding that does not exist, and
    the composer's own keys are exempt from the KEYS table precisely because the
    sheet describes them as prose (`test_every_key_the_page_binds...`) - so this
    is the only thing standing between the feature and being invisible."""
    assert "Up/Down walk back through what you have already sent this " in APP_JS
    assert "FIRST/LAST line of " in APP_JS


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


def test_the_document_handler_owns_stages_four_to_seven() -> None:
    fn = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert [int(n) for n in re.findall(r"ESC STAGE (\d)", fn)] == [5, 4, 6, 7]
    # Stage 5 first, and it returns: a modal owns the keyboard while it is up.
    assert fn.index("ESC STAGE 5") < fn.index("ESC STAGE 4")
    # ...and the question is LAST before the no-op: a pending gate's reject box
    # is nearer, and stages 2/3 have already spent a press each, so dismissing
    # can never be the press that was meant to empty or leave the composer.
    assert fn.index("ESC STAGE 4") < fn.index("ESC STAGE 6")


def test_the_last_stage_dismisses_a_pending_question_and_only_then() -> None:
    """The banner's way out. Guarded on the mirrored flag rather than on the
    banner's `hidden` attribute, and it ASKS PYTHON - the decision (send nothing,
    leave the model parked, let the next message answer) is the controller's.
    The same flag is what makes a SECOND Esc a no-op: the push that follows a
    dismissal clears `awaiting_answer`, so the stage is no longer live."""
    fn = APP_JS[APP_JS.index("function onDocumentKey(ev)") :]
    fn = fn[: fn.index("\n  }\n")]
    stage = fn[fn.index("ESC STAGE 6") :]
    assert "if (awaitingAnswer) {" in stage
    assert 'api("dismiss_question");' in stage
    # The flag is the state push's, so the page never guesses when a question
    # ended - and the banner is painted from the same fact.
    assert "awaitingAnswer = Boolean(event.awaiting_answer);" in APP_JS
    assert "el.askBanner.hidden = !(awaitingAnswer && event.question);" in APP_JS


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
    branch = fn[fn.index("ESC STAGE 5") :]
    assert expected in branch, surface


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


def test_f4_is_an_appearance_picker_and_nothing_more_just_as_the_tuis_is() -> None:
    """§2.2: the TUI's SettingsScreen is a theme picker with a single
    "Appearance" tab. It does not touch [notify] bell/toast - those are
    file-only in both shells - so neither does this one.

    What the two screens do NOT have in common is the list itself; that is the
    test below. All this one pins about the list is that the picker offers every
    palette ``[gui] theme`` will accept and no name it would reject, because a
    radio the config layer refuses is a button that does nothing.
    """
    assert set(value for value, _ in THEME_CHOICES) == set(VALID_GUI_THEMES)
    assert THEME_CHOICES[0][0] == DEFAULT_GUI_THEME  # what an unset config wears, first
    fn = APP_JS[APP_JS.index("function openSettings()") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "bell" not in fn and "toast" not in fn
    assert 'input.type = "radio"' in fn


def test_the_two_theme_vocabularies_overlap_in_exactly_the_claude_pair() -> None:
    """The overlap is a decision, not a leak. `[gui] theme` names a CSS palette
    block and `[general] theme` names a theme by the old shell's spelling; the
    two claude names are deliberately in both, so `/theme claude-dark` means one
    thing, while `dark`/`light` stay this shell's own. That is what keeps the
    two settings from collapsing into one with two spellings."""
    shared = VALID_GUI_THEMES & VALID_THEMES
    assert shared == {"claude-warm", "claude-dark"}
    this_shells_own = VALID_GUI_THEMES - VALID_THEMES
    assert this_shells_own == {"dark", "light"}


def test_the_theme_crosses_at_start_and_after_a_reload(harness: Harness) -> None:
    spy_on(harness)
    harness.view.start()
    event = harness.flush().last("settings")
    assert event["theme"] == DEFAULT_GUI_THEME
    assert [row["value"] for row in event["themes"]] == [value for value, _ in THEME_CHOICES]
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

    import agentclip.shell.chat.view as view_module

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


# -- the other door: /theme ----------------------------------------------------
# F4's picker and the chat command are two ways onto one setting, and they share
# the mechanics rather than the message: the picker says "theme saved" because
# the click itself said nothing, while `/theme`'s toast is the controller's, so
# the seam is silent on success and a command says one thing once.


def test_the_theme_seam_offers_this_shells_own_palettes(harness: Harness) -> None:
    """The reason the choices are a port question at all: the two shells' lists
    are neither the same nor disjoint - they share the Claude pair and nothing
    else - so only the view can say what a name means here."""
    assert harness.view.theme_choices() == tuple(value for value, _ in THEME_CHOICES)
    assert harness.view.current_theme() == DEFAULT_GUI_THEME


def test_a_theme_applied_from_a_chat_command_saves_and_repaints(
    harness: Harness, tmp_path: Path, project: Path
) -> None:
    """The page paints only what the ``settings`` event says, so re-pushing it
    is how a theme changed from Python reaches the body class."""
    config_path = tmp_path / "global.toml"
    harness.view._global_config_path = config_path

    harness.view.apply_theme("light")

    assert harness.view.current_theme() == "light"
    assert harness.flush().last("settings")["theme"] == "light"
    assert load_config(project, global_config_path=config_path).gui.theme == "light"
    assert harness.recorder.of_type("toast") == []  # the controller raises the one toast


def test_a_claude_palette_makes_the_same_round_trip_as_the_originals(
    harness: Harness, tmp_path: Path, project: Path
) -> None:
    """The name the two shells share, through both of this shell's doors. It is
    worth its own test because it is the one that could have been half-added -
    a CSS block with no config name, or a config name with no block - and
    neither half fails loudly on its own."""
    config_path = tmp_path / "global.toml"
    harness.view._global_config_path = config_path

    api_of(harness).theme("claude-dark")  # F4's radio
    assert harness.flush().last("settings")["theme"] == "claude-dark"

    harness.view.apply_theme("claude-warm")  # /theme, the other door
    assert harness.view.current_theme() == "claude-warm"
    assert harness.flush().last("settings")["theme"] == "claude-warm"

    reloaded = load_config(project, global_config_path=config_path)
    assert reloaded.gui.theme == "claude-warm"
    assert not reloaded.warnings  # the TUI's name, accepted by the GUI's table


def test_the_settings_event_is_what_repaints_the_page() -> None:
    """The other half of the above, on the page's side: a Python-initiated
    re-push has to wear the theme, not merely refresh the radio buttons."""
    fn = APP_JS[APP_JS.index('case "settings":') :]
    fn = fn[: fn.index("return;")]
    assert "applyTheme(event.theme)" in fn


def _palette(selector: str) -> str:
    """One palette block's body, from its selector to its closing brace.

    Refusing a second ``{`` is the load-bearing half: a palette that lost its
    closing brace slices clean through into the NEXT block here, and every
    "does it define --x" question then answers yes with the neighbour's
    values - which is exactly how an unclosed ``theme-light`` once sailed past
    this file while the browser dropped half the palettes on the floor.
    """
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    block = css[css.index(selector + " {") :]
    body = block[: block.index("\n}")]
    assert body.count("{") == 1, selector + " is not closed and ran into the next block"
    return body


def test_the_page_paints_a_real_light_palette() -> None:
    """"Ship dark-only and label it" was the fallback; this is the other branch,
    so the variables have to actually exist rather than the class being inert."""
    light = _palette("body.theme-light")
    for token in ("--bg", "--bg-raised", "--line", "--text", "--text-dim",
                  "--accent", "--ok", "--warn", "--err", "--on-solid"):
        assert token + ":" in light, token
    # Nothing below the palette region may hard-code a colour, or the theme is a
    # lie - which is also what makes a new palette one block and no edits.
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    body = css[css.index("\n* {") :]
    assert not re.search(r":\s*#[0-9a-fA-F]{3,8}\b", body), "a colour escaped the palette"


def test_every_palette_answers_the_same_questions_as_the_light_one() -> None:
    """A theme is a full set of values, not an override of the handful somebody
    remembered: a block that skips one name inherits :root's dark value for it,
    which is how a light palette ends up with one black rectangle in it. The
    light block is the yardstick because it was written first, by hand, against
    the whole file."""
    wanted = set(re.findall(r"(--[a-z-]+):", _palette("body.theme-light")))
    assert len(wanted) >= 16  # the yardstick is a whole palette, not a stub
    for value, _label in THEME_CHOICES:
        if value == DEFAULT_GUI_THEME:
            continue  # the default IS :root; it has no block by design
        block = _palette("body.theme-" + value)
        assert set(re.findall(r"(--[a-z-]+):", block)) == wanted, value


def test_the_default_palette_is_root_itself_and_wears_no_class() -> None:
    """Otherwise "no class" and "theme-dark" are two ways to be the same thing,
    and the CSS has to keep both of them right."""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert "body.theme-" + DEFAULT_GUI_THEME + " {" not in css
    fn = APP_JS[APP_JS.index("function applyTheme(name)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "theme !== THEME_DEFAULT" in fn


def test_applying_a_theme_swaps_one_class_and_leaves_the_others_alone() -> None:
    """N palettes, not two: the previous `theme-*` class comes off and the new
    one goes on, validated against the list Python pushed rather than a literal
    in here - so the page needs no edit when a palette is added. `yolo` is a
    body class too, and must survive a retheme."""
    fn = APP_JS[APP_JS.index("function applyTheme(name)") :]
    fn = fn[: fn.index("\n  }\n")]
    assert "themeChoices" in fn  # the known set is Python's, with THEME_NAMES as the floor
    assert "THEME_NAMES" in fn
    assert 'indexOf("theme-") === 0' in fn  # only the theme classes are stripped
    assert "classList.remove" in fn and 'classList.add("theme-" + theme)' in fn
    # ...which is what the prefix test is FOR: the yolo banner is a body class
    # as well, set from somewhere else entirely, and a retheme must not eat it.
    assert 'classList.toggle("yolo"' in APP_JS


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
        / "src" / "agentclip" / "shell" / "chat" / "runner.py"
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
        / "src" / "agentclip" / "shell" / "chat" / "shell.py"
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
    doc = __import__("agentclip.shell.webview.bridge", fromlist=["x"]).__doc__ or ""
    assert '{type: "commands",' in doc
    assert '{type: "settings",' in doc
