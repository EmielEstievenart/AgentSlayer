"""Headless tests for the per-service detection fields (``total_context_chars``,
``stable_seconds``, ``finish_signals``, ``hover_scan``, ``delivery``) and the
service-editor persistence path (``save_services``): TOML merge/validation, round-tripping
through load_config, minimal-diff writes, and the atomic-write mechanics. No
Textual here - the pilot tests for the editor UI itself live in
tests/tui/test_service_editor_ui.py."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    DEFAULT_DELIVERY,
    DEFAULT_FINISH_SIGNALS,
    DEFAULT_MATCHER,
    DEFAULT_SCROLL_ACTION,
    DEFAULT_STABLE_SECONDS,
    DEFAULT_THEME,
    DEFAULT_TOLERANCE,
    DELIVERY_MODES,
    FINISH_SIGNALS,
    MATCHERS,
    SCROLL_ACTIONS,
    TOLERANCE_MAX,
    TOLERANCE_MIN,
    ServicePreset,
    default_global_config_path,
    default_opencode_config_path,
    default_profile_dir,
    default_services,
    load_config,
    normalize_finish_signals,
    save_active_services,
    save_services,
    save_theme,
)
from agentclip.permissions import default_rules, evaluate


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def global_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


# -- default values -----------------------------------------------------------


def test_every_builtin_has_a_total_context_size_at_least_the_paste_budget() -> None:
    for key, preset in default_services().items():
        assert preset.total_context_chars > 0, key
        assert preset.max_paste_chars <= preset.total_context_chars, key


def test_builtin_service_keys_matches_default_services() -> None:
    assert frozenset(default_services()) == BUILTIN_SERVICE_KEYS
    assert len(BUILTIN_SERVICE_KEYS) == 12


# -- [general] subagent_service ------------------------------------------------
#
# The sub-agent window tab's service (tui.md 1.6). Blank is the default AND a
# meaningful value - "whatever the master tab is on" - which is what keeps the
# key invisible to everybody running one service in both windows, so the three
# cases below are load, name, and name-that-does-not-exist.


def test_subagent_service_defaults_to_blank(project: Path, global_path: Path) -> None:
    config = load_config(project, global_config_path=global_path)
    assert config.general.subagent_service == ""
    assert not config.warnings


def test_load_config_reads_a_subagent_service(project: Path, global_path: Path) -> None:
    global_path.write_text(
        '[general]\nservice = "claude"\nsubagent_service = "gemini"\n', encoding="utf-8"
    )
    config = load_config(project, global_config_path=global_path)
    assert config.general.service == "claude"
    assert config.general.subagent_service == "gemini"
    assert not config.warnings


def test_load_config_warns_and_blanks_an_unknown_subagent_service(
    project: Path, global_path: Path
) -> None:
    """Blanked rather than kept: a window pointed at a preset that is in no
    picker would silently drive the automation off ``Config.preset()``'s
    fallback, on a paste budget nobody chose."""
    global_path.write_text(
        '[general]\nservice = "claude"\nsubagent_service = "nope"\n', encoding="utf-8"
    )
    config = load_config(project, global_config_path=global_path)
    assert config.general.subagent_service == ""
    assert any(
        "unknown subagent_service preset 'nope'" in warning for warning in config.warnings
    )
    assert config.general.service == "claude"  # ...and the master's is untouched


def test_an_unknown_subagent_service_is_not_confused_with_an_unknown_service(
    project: Path, global_path: Path
) -> None:
    global_path.write_text('[general]\nservice = "nope"\n', encoding="utf-8")
    config = load_config(project, global_config_path=global_path)
    assert config.general.service == "unknown"
    assert config.general.subagent_service == ""
    assert any("unknown service preset 'nope'" in warning for warning in config.warnings)
    assert not any("subagent_service" in warning for warning in config.warnings)


# -- TOML merge / validation ---------------------------------------------------


def test_load_config_reads_total_context_chars_override(project: Path, global_path: Path) -> None:
    global_path.write_text(
        "[services.claude]\ntotal_context_chars = 900000\n", encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].total_context_chars == 900_000
    # untouched fields keep the built-in default
    assert cfg.services["claude"].max_paste_chars == default_services()["claude"].max_paste_chars
    assert not cfg.warnings


def test_load_config_new_service_gets_a_default_total_context_chars(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.mycustom]\nlabel = "My Custom"\nmax_paste_chars = 5000\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["mycustom"].total_context_chars > 0
    assert not cfg.warnings


def test_load_config_warns_when_max_paste_exceeds_total_context(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        "[services.claude]\nmax_paste_chars = 800000\ntotal_context_chars = 10000\n",
        encoding="utf-8",
    )
    cfg = load_config(project, global_config_path=global_path)
    assert any("exceeds" in w for w in cfg.warnings)
    # the bad values still land (load_config never raises / substitutes on cross-field checks)
    assert cfg.services["claude"].max_paste_chars == 800_000


def test_load_config_rejects_out_of_range_total_context_chars(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        "[services.claude]\ntotal_context_chars = 100\n", encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].total_context_chars == default_services()["claude"].total_context_chars
    assert any("total_context_chars" in w for w in cfg.warnings)


# -- stable_seconds (the stale finish detector's stillness window) -------------


def test_every_builtin_ships_the_shared_stable_seconds_default() -> None:
    assert DEFAULT_STABLE_SECONDS == 2.0
    for key, preset in default_services().items():
        assert preset.stable_seconds == DEFAULT_STABLE_SECONDS, key


def test_a_bare_preset_defaults_its_stable_seconds() -> None:
    assert ServicePreset("k", "K", 1_000, 5_000).stable_seconds == DEFAULT_STABLE_SECONDS


def test_load_config_reads_stable_seconds_override(project: Path, global_path: Path) -> None:
    global_path.write_text("[services.claude]\nstable_seconds = 4.5\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].stable_seconds == 4.5
    # untouched fields keep the built-in default
    assert cfg.services["claude"].max_paste_chars == default_services()["claude"].max_paste_chars
    assert not cfg.warnings


def test_load_config_accepts_a_whole_number_stable_seconds(
    project: Path, global_path: Path
) -> None:
    """TOML users write `3` as readily as `3.0`; both are numbers, and the
    preset always ends up holding a float."""
    global_path.write_text("[services.claude]\nstable_seconds = 3\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].stable_seconds == 3.0
    assert isinstance(cfg.services["claude"].stable_seconds, float)
    assert not cfg.warnings


def test_load_config_accepts_both_stable_seconds_bounds(project: Path, global_path: Path) -> None:
    for value in ("0.5", "60.0"):
        global_path.write_text(
            f"[services.claude]\nstable_seconds = {value}\n", encoding="utf-8"
        )
        cfg = load_config(project, global_config_path=global_path)
        assert cfg.services["claude"].stable_seconds == float(value)
        assert not cfg.warnings


@pytest.mark.parametrize("value", ["0.1", "60.5", "0"])
def test_load_config_rejects_out_of_range_stable_seconds(
    project: Path, global_path: Path, value: str
) -> None:
    global_path.write_text(f"[services.claude]\nstable_seconds = {value}\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].stable_seconds == DEFAULT_STABLE_SECONDS
    assert any("stable_seconds" in w and "outside" in w for w in cfg.warnings)


@pytest.mark.parametrize("value", ['"soon"', "true"])
def test_load_config_rejects_a_non_numeric_stable_seconds(
    project: Path, global_path: Path, value: str
) -> None:
    """Booleans are ints in Python and must be refused explicitly, exactly as
    the integer knobs refuse them."""
    global_path.write_text(f"[services.claude]\nstable_seconds = {value}\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].stable_seconds == DEFAULT_STABLE_SECONDS
    assert any("stable_seconds" in w and "must be a number" in w for w in cfg.warnings)


def test_load_config_new_service_gets_the_default_stable_seconds(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.mycustom]\nlabel = "My Custom"\nmax_paste_chars = 5000\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["mycustom"].stable_seconds == DEFAULT_STABLE_SECONDS
    assert not cfg.warnings


def test_a_bad_stable_seconds_does_not_poison_the_rest_of_the_preset(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        "[services.claude]\nmax_paste_chars = 30000\nstable_seconds = 999\n", encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].max_paste_chars == 30_000
    assert cfg.services["claude"].stable_seconds == DEFAULT_STABLE_SECONDS


# -- finish_signals + hover_scan (the per-service detection checklist) ---------


def test_every_builtin_ships_the_stale_only_checklist_and_no_hover_scan() -> None:
    """Staleness is what a freshly drawn window can already do; the hover scan
    takes the user's mouse over, so nobody gets it without asking."""
    assert DEFAULT_FINISH_SIGNALS == ("stale",)
    assert FINISH_SIGNALS == ("busy", "idle", "stale")
    for key, preset in default_services().items():
        assert preset.finish_signals == DEFAULT_FINISH_SIGNALS, key
        assert preset.hover_scan is False, key


def test_a_bare_preset_defaults_its_detection_fields() -> None:
    preset = ServicePreset("k", "K", 1_000, 5_000)
    assert preset.finish_signals == DEFAULT_FINISH_SIGNALS
    assert preset.hover_scan is False


def test_normalize_finish_signals_drops_dedupes_and_orders() -> None:
    assert normalize_finish_signals(["stale", "busy", "busy", "nope", ""]) == ("busy", "stale")
    assert normalize_finish_signals([]) == ()
    assert normalize_finish_signals(["idle", "stale", "busy"]) == FINISH_SIGNALS


def test_load_config_reads_a_finish_signals_override(project: Path, global_path: Path) -> None:
    global_path.write_text(
        '[services.claude]\nfinish_signals = ["stale", "busy"]\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].finish_signals == ("busy", "stale")  # canonical order
    assert not cfg.warnings


def test_load_config_accepts_an_empty_finish_signals_list(project: Path, global_path: Path) -> None:
    """Legal, and distinct from an absent key: "never detect a finish here"."""
    global_path.write_text("[services.claude]\nfinish_signals = []\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].finish_signals == ()
    assert not cfg.warnings


def test_load_config_drops_unknown_finish_signals_and_warns(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.claude]\nfinish_signals = ["busy", "telepathy", "busy"]\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].finish_signals == ("busy",)
    assert any("telepathy" in w for w in cfg.warnings)


def test_load_config_rejects_a_non_list_finish_signals(project: Path, global_path: Path) -> None:
    global_path.write_text('[services.claude]\nfinish_signals = "stale"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].finish_signals == DEFAULT_FINISH_SIGNALS
    assert any("finish_signals" in w and "list of strings" in w for w in cfg.warnings)


def test_load_config_reads_a_hover_scan_override(project: Path, global_path: Path) -> None:
    global_path.write_text("[services.claude]\nhover_scan = true\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].hover_scan is True
    assert not cfg.warnings


def test_load_config_rejects_a_non_boolean_hover_scan(project: Path, global_path: Path) -> None:
    global_path.write_text('[services.claude]\nhover_scan = "yes"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].hover_scan is False
    assert any("hover_scan" in w and "true/false" in w for w in cfg.warnings)


def test_a_config_written_before_the_detection_fields_still_loads(
    project: Path, global_path: Path
) -> None:
    """The exact five-key table earlier versions wrote: it must come back with
    the new fields at their defaults and nothing to complain about."""
    global_path.write_text(
        '[services.claude]\nlabel = "Claude.ai"\nmax_paste_chars = 30000\n'
        "total_context_chars = 700000\nwrap_blocks_in_fence = true\nattachment_note = true\n",
        encoding="utf-8",
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].max_paste_chars == 30_000
    assert cfg.services["claude"].finish_signals == DEFAULT_FINISH_SIGNALS
    assert cfg.services["claude"].hover_scan is False
    assert cfg.services["claude"].delivery == DEFAULT_DELIVERY
    assert not cfg.warnings


def test_load_config_new_service_gets_the_default_detection_fields(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.mycustom]\nlabel = "My Custom"\nmax_paste_chars = 5000\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["mycustom"].finish_signals == DEFAULT_FINISH_SIGNALS
    assert cfg.services["mycustom"].hover_scan is False
    assert not cfg.warnings


# -- delivery (how an outbound payload goes into the chat box) -----------------


def test_every_builtin_ships_the_single_paste_delivery() -> None:
    """Chunked delivery is slower and can leave half a message in the box, so
    nobody gets it without asking for it."""
    assert DELIVERY_MODES == ("paste", "stream")
    assert DEFAULT_DELIVERY == "paste"
    for key, preset in default_services().items():
        assert preset.delivery == "paste", key
    assert ServicePreset("k", "K", 1_000, 5_000).delivery == "paste"


def test_load_config_reads_a_delivery_override(project: Path, global_path: Path) -> None:
    global_path.write_text('[services.claude]\ndelivery = "stream"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].delivery == "stream"
    assert not cfg.warnings


def test_load_config_rejects_an_unknown_delivery_mode(project: Path, global_path: Path) -> None:
    """Silently reading as "paste" is the one outcome a user who wrote this key
    would not notice - one big paste is exactly what they were escaping."""
    global_path.write_text('[services.claude]\ndelivery = "typing"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].delivery == "paste"
    assert any("delivery" in w and "paste, stream" in w for w in cfg.warnings)


def test_load_config_rejects_a_non_string_delivery(project: Path, global_path: Path) -> None:
    global_path.write_text("[services.claude]\ndelivery = true\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].delivery == "paste"
    assert any("delivery" in w for w in cfg.warnings)


def test_save_then_load_round_trips_delivery(project: Path, global_path: Path) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], delivery="stream")

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"]["delivery"] == "stream"

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


def test_save_services_writes_delivery_only_when_it_differs(
    project: Path, global_path: Path
) -> None:
    """It arrived after the rest: a preset whose user never touched it must
    still be written exactly as earlier versions wrote it."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    assert "delivery" not in tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]

    services["my-llm"] = ServicePreset("my-llm", "My LLM", 8_000, 300_000)
    save_services(services, global_path)
    assert "delivery" not in tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["my-llm"]


def test_save_services_delivery_alone_is_enough_to_write_a_builtin(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["gemini"] = replace(services["gemini"], delivery="stream")

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert set(raw["services"]) == {"gemini"}
    assert raw["services"]["gemini"]["delivery"] == "stream"


# -- scroll_action (how the auto-copy flow reaches the newest reply) ------------


def test_every_builtin_ships_the_wheel_scroll() -> None:
    """The keyboard forms only work on pages that scroll on those keys, so
    nobody gets one without asking for it."""
    assert SCROLL_ACTIONS == ("scroll", "page_down", "end")
    assert DEFAULT_SCROLL_ACTION == "scroll"
    for key, preset in default_services().items():
        assert preset.scroll_action == "scroll", key
    assert ServicePreset("k", "K", 1_000, 5_000).scroll_action == "scroll"


@pytest.mark.parametrize("action", ["page_down", "end"])
def test_load_config_reads_a_scroll_action_override(
    project: Path, global_path: Path, action: str
) -> None:
    global_path.write_text(f'[services.claude]\nscroll_action = "{action}"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].scroll_action == action
    assert not cfg.warnings


def test_load_config_rejects_an_unknown_scroll_action(project: Path, global_path: Path) -> None:
    """Silently flicking the wheel is the one outcome a user who wrote this key
    would not notice - the wheel not reaching their page is what they were
    escaping."""
    global_path.write_text('[services.claude]\nscroll_action = "home"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].scroll_action == "scroll"
    assert any("scroll_action" in w and "scroll, page_down, end" in w for w in cfg.warnings)


def test_load_config_rejects_a_non_string_scroll_action(project: Path, global_path: Path) -> None:
    global_path.write_text("[services.claude]\nscroll_action = 3\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].scroll_action == "scroll"
    assert any("scroll_action" in w for w in cfg.warnings)


def test_save_then_load_round_trips_scroll_action(project: Path, global_path: Path) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], scroll_action="end")

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"]["scroll_action"] == "end"

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


def test_save_services_writes_scroll_action_only_when_it_differs(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    assert "scroll_action" not in tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]

    services["my-llm"] = ServicePreset("my-llm", "My LLM", 8_000, 300_000)
    save_services(services, global_path)
    assert "scroll_action" not in tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["my-llm"]


# -- auto_submit + capture_prose (the two automation opt-ins) --------------------


def test_the_automation_opt_ins_ship_off() -> None:
    """A synthetic Enter and a prose ingest are both acts the loop otherwise
    always leaves to the user, so nobody gets either without asking."""
    for key, preset in default_services().items():
        assert preset.auto_submit is False, key
        assert preset.capture_prose is False, key
    fresh = ServicePreset("k", "K", 1_000, 5_000)
    assert fresh.auto_submit is False
    assert fresh.capture_prose is False


@pytest.mark.parametrize("field", ["auto_submit", "capture_prose"])
def test_load_config_reads_an_automation_opt_in(
    project: Path, global_path: Path, field: str
) -> None:
    global_path.write_text(f"[services.claude]\n{field} = true\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert getattr(cfg.services["claude"], field) is True
    assert not cfg.warnings


@pytest.mark.parametrize("field", ["auto_submit", "capture_prose"])
def test_load_config_rejects_a_non_bool_automation_opt_in(
    project: Path, global_path: Path, field: str
) -> None:
    global_path.write_text(f'[services.claude]\n{field} = "yes"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert getattr(cfg.services["claude"], field) is False
    assert any(field in w for w in cfg.warnings)


@pytest.mark.parametrize("field", ["auto_submit", "capture_prose"])
def test_save_then_load_round_trips_an_automation_opt_in(
    project: Path, global_path: Path, field: str
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], **{field: True})

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"][field] is True

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


def test_save_services_writes_the_opt_ins_only_when_they_differ(
    project: Path, global_path: Path
) -> None:
    """They arrived after the rest: a preset whose user never touched them must
    still be written exactly as earlier versions wrote it."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    written = tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]
    assert "auto_submit" not in written
    assert "capture_prose" not in written
    assert "require_fenced_reply" not in written
    assert "extra_instructions" not in written


# -- require_fenced_reply (the unfenced-reply gate, protocol.md 1.4 #15) --------


def test_require_fenced_reply_ships_off_everywhere() -> None:
    """Golden fixture 002 is the reason. An unfenced arrival is NORMAL on the
    hosts whose per-code-block copy button hands over the block's contents
    WITHOUT its fence lines, so a default-on gate would refuse every good reply
    on exactly the services the fence-agnostic parser was built for."""
    for key, preset in default_services().items():
        assert preset.require_fenced_reply is False, key
    assert ServicePreset("k", "K", 1_000, 5_000).require_fenced_reply is False


def test_load_config_reads_require_fenced_reply(project: Path, global_path: Path) -> None:
    global_path.write_text(
        "[services.claude]\nrequire_fenced_reply = true\n", encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].require_fenced_reply is True
    assert not cfg.warnings


def test_load_config_rejects_a_non_bool_require_fenced_reply(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.claude]\nrequire_fenced_reply = "yes"\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].require_fenced_reply is False
    assert any("require_fenced_reply" in w for w in cfg.warnings)


def test_save_then_load_round_trips_require_fenced_reply(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], require_fenced_reply=True)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"]["require_fenced_reply"] is True

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


# -- save_services: round trip + minimal diff ----------------------------------


def test_save_then_load_round_trips_an_edited_builtin(project: Path, global_path: Path) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000, total_context_chars=999_000)

    save_services(services, global_path)
    cfg2 = load_config(project, global_config_path=global_path)

    assert cfg2.services["claude"].max_paste_chars == 30_000
    assert cfg2.services["claude"].total_context_chars == 999_000
    assert not cfg2.warnings
    # every other builtin came back unchanged
    for key, preset in default_services().items():
        if key == "claude":
            continue
        assert cfg2.services[key] == preset


def test_save_then_load_round_trips_a_new_custom_service(project: Path, global_path: Path) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["my-llm"] = ServicePreset("my-llm", "My LLM", 8_000, 300_000)

    save_services(services, global_path)
    cfg2 = load_config(project, global_config_path=global_path)

    assert cfg2.services["my-llm"] == services["my-llm"]


def test_save_services_omits_untouched_builtins_from_the_file(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert set(raw.get("services", {})) == {"claude"}


def test_save_services_reset_to_default_removes_the_override(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)
    save_services(services, global_path)
    assert "claude" in tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]

    services["claude"] = default_services()["claude"]  # reset
    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "claude" not in raw.get("services", {})


def test_save_then_load_round_trips_an_edited_stable_seconds(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], stable_seconds=7.5)

    save_services(services, global_path)
    cfg2 = load_config(project, global_config_path=global_path)

    assert cfg2.services["claude"].stable_seconds == 7.5
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


def test_save_services_writes_stable_seconds_only_when_it_differs(
    project: Path, global_path: Path
) -> None:
    """The knob arrived after the other five fields: a preset whose user never
    touched the stale window must still be written byte-for-byte as earlier
    versions wrote it."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    claude = tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]
    assert "stable_seconds" not in claude

    services["claude"] = replace(services["claude"], stable_seconds=5.0)
    save_services(services, global_path)
    claude = tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]
    assert claude["stable_seconds"] == 5.0


def test_save_services_omits_a_custom_services_default_stable_seconds(
    project: Path, global_path: Path
) -> None:
    """A brand new key has no built-in to compare against, so the dataclass
    default is what "unchanged" means for it."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["my-llm"] = ServicePreset("my-llm", "My LLM", 8_000, 300_000)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "stable_seconds" not in raw["services"]["my-llm"]

    services["my-llm"] = replace(services["my-llm"], stable_seconds=12.0)
    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["my-llm"]["stable_seconds"] == 12.0
    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["my-llm"] == services["my-llm"]


def test_save_services_stable_seconds_alone_is_enough_to_write_a_builtin(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["gemini"] = replace(services["gemini"], stable_seconds=0.5)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert set(raw["services"]) == {"gemini"}
    assert raw["services"]["gemini"]["stable_seconds"] == 0.5


def test_save_then_load_round_trips_the_detection_fields(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(
        services["claude"], finish_signals=("busy", "idle"), hover_scan=True
    )

    save_services(services, global_path)
    cfg2 = load_config(project, global_config_path=global_path)

    assert cfg2.services["claude"].finish_signals == ("busy", "idle")
    assert cfg2.services["claude"].hover_scan is True
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


def test_save_then_load_round_trips_an_empty_checklist(project: Path, global_path: Path) -> None:
    """"Detect nothing here" is a setting, not an absence - it has to survive
    the write, or the preset would silently go back to detecting staleness."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], finish_signals=())

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"]["finish_signals"] == []

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"].finish_signals == ()
    assert not cfg2.warnings


def test_save_services_writes_the_detection_fields_only_when_they_differ(
    project: Path, global_path: Path
) -> None:
    """They arrived after the other six: a preset whose user never touched them
    must still be written exactly as earlier versions wrote it."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    claude = tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]
    assert "finish_signals" not in claude
    assert "hover_scan" not in claude

    services["claude"] = replace(
        services["claude"], finish_signals=("busy", "stale"), hover_scan=True
    )
    save_services(services, global_path)
    claude = tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]["claude"]
    assert claude["finish_signals"] == ["busy", "stale"]
    assert claude["hover_scan"] is True


def test_save_services_hover_scan_alone_is_enough_to_write_a_builtin(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["gemini"] = replace(services["gemini"], hover_scan=True)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert set(raw["services"]) == {"gemini"}
    assert raw["services"]["gemini"]["hover_scan"] is True


def test_save_services_omits_a_custom_services_default_detection_fields(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["my-llm"] = ServicePreset("my-llm", "My LLM", 8_000, 300_000)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "finish_signals" not in raw["services"]["my-llm"]
    assert "hover_scan" not in raw["services"]["my-llm"]

    services["my-llm"] = replace(services["my-llm"], finish_signals=("idle",), hover_scan=True)
    save_services(services, global_path)
    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["my-llm"] == services["my-llm"]


def test_save_services_deleting_a_custom_key_removes_it_from_the_file(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["my-llm"] = ServicePreset("my-llm", "My LLM", 8_000, 300_000)
    save_services(services, global_path)
    assert "my-llm" in tomllib.loads(global_path.read_text(encoding="utf-8"))["services"]

    del services["my-llm"]
    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "my-llm" not in raw.get("services", {})


def test_save_services_preserves_other_top_level_tables(project: Path, global_path: Path) -> None:
    global_path.write_text(
        '[general]\nservice = "claude"\nchars_per_token = 4\n\n'
        "[clipboard]\npoll_interval_ms = 500\n",
        encoding="utf-8",
    )
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert raw["general"] == {"service": "claude", "chars_per_token": 4}
    assert raw["clipboard"] == {"poll_interval_ms": 500}
    # and load_config still sees them
    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.general.chars_per_token == 4
    assert cfg2.clipboard.poll_interval_ms == 500


def test_save_services_no_changes_writes_no_services_table(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    save_services(dict(cfg.services), global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "services" not in raw


def test_save_services_creates_missing_parent_dirs(tmp_path: Path, project: Path) -> None:
    nested = tmp_path / "nested" / "does" / "not" / "exist" / "config.toml"
    cfg = load_config(project, global_config_path=nested)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)

    save_services(services, nested)

    assert nested.exists()
    cfg2 = load_config(project, global_config_path=nested)
    assert cfg2.services["claude"].max_paste_chars == 30_000


def test_save_services_is_atomic_no_leftover_tmp_files(project: Path, global_path: Path) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)
    save_services(services, global_path)

    leftovers = list(global_path.parent.glob("*.tmp"))
    assert leftovers == []
    assert global_path.exists()


# -- theme: default / load / save --------------------------------------------


def test_default_theme_is_textual_dark(project: Path, global_path: Path) -> None:
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.theme == "textual-dark"
    assert DEFAULT_THEME == "textual-dark"


def test_load_config_reads_theme_from_toml(project: Path, global_path: Path) -> None:
    global_path.write_text('[general]\ntheme = "claude-dark"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.theme == "claude-dark"
    assert not cfg.warnings


def test_load_config_rejects_unknown_theme_and_warns(project: Path, global_path: Path) -> None:
    global_path.write_text('[general]\ntheme = "not-a-real-theme"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.theme == DEFAULT_THEME
    assert any("theme" in w for w in cfg.warnings)


def test_load_config_accepts_all_four_selectable_themes(project: Path, global_path: Path) -> None:
    for theme in ("textual-light", "textual-dark", "claude-warm", "claude-dark"):
        global_path.write_text(f'[general]\ntheme = "{theme}"\n', encoding="utf-8")
        cfg = load_config(project, global_config_path=global_path)
        assert cfg.general.theme == theme
        assert not cfg.warnings


def test_save_theme_then_load_round_trips(project: Path, global_path: Path) -> None:
    save_theme("claude-warm", global_path)
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.theme == "claude-warm"
    assert not cfg.warnings


def test_save_theme_preserves_other_general_keys_and_top_level_tables(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[general]\nservice = "claude"\nchars_per_token = 4\n\n'
        "[clipboard]\npoll_interval_ms = 500\n",
        encoding="utf-8",
    )
    save_theme("claude-dark", global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert raw["general"] == {"service": "claude", "chars_per_token": 4, "theme": "claude-dark"}
    assert raw["clipboard"] == {"poll_interval_ms": 500}

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.theme == "claude-dark"
    assert cfg.general.service == "claude"
    assert cfg.general.chars_per_token == 4
    assert cfg.clipboard.poll_interval_ms == 500


def test_save_theme_overwrites_a_previous_theme_value(project: Path, global_path: Path) -> None:
    save_theme("claude-warm", global_path)
    save_theme("claude-dark", global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["general"]["theme"] == "claude-dark"


def test_save_theme_is_atomic_no_leftover_tmp_files(project: Path, global_path: Path) -> None:
    save_theme("claude-dark", global_path)
    leftovers = list(global_path.parent.glob("*.tmp"))
    assert leftovers == []
    assert global_path.exists()


def test_save_theme_creates_missing_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "does" / "not" / "exist" / "config.toml"
    save_theme("claude-warm", nested)
    assert nested.exists()
    raw = tomllib.loads(nested.read_text(encoding="utf-8"))
    assert raw["general"]["theme"] == "claude-warm"


# -- the active service: remembering what the sidebar's picker was last on -----
#
# The TUI wiring (switching in the sidebar calls the saver) is a pilot test, in
# tests/tui/test_slot_ui.py. Everything below is the file format and its
# round-trip: what lands in [general], and what load_config makes of it next
# launch.


def test_save_active_services_then_load_round_trips(project: Path, global_path: Path) -> None:
    save_active_services("claude", "gemini", global_path)
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.service == "claude"
    assert cfg.general.subagent_service == "gemini"
    assert not cfg.warnings


def test_save_active_services_creates_the_file_when_it_is_missing(
    project: Path, global_path: Path
) -> None:
    assert not global_path.exists()
    save_active_services("claude", "", global_path)
    assert global_path.exists()
    assert load_config(project, global_config_path=global_path).general.service == "claude"


def test_save_active_services_creates_missing_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "does" / "not" / "exist" / "config.toml"
    save_active_services("gemini", "", nested)
    assert nested.exists()
    raw = tomllib.loads(nested.read_text(encoding="utf-8"))
    assert raw["general"]["service"] == "gemini"


def test_save_active_services_omits_a_subagent_that_matches_the_master(
    project: Path, global_path: Path
) -> None:
    """Both windows on one service is the blank case, not a pin: the key stays
    out of the file so the sub-agent window keeps FOLLOWING the master when the
    master later moves, instead of freezing on today's coincidence."""
    save_active_services("claude", "claude", global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["general"] == {"service": "claude"}

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.subagent_service == ""
    assert not cfg.warnings


def test_save_active_services_drops_a_stale_subagent_pin(
    project: Path, global_path: Path
) -> None:
    save_active_services("claude", "gemini", global_path)
    save_active_services("gemini", "gemini", global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "subagent_service" not in raw["general"]
    assert load_config(project, global_config_path=global_path).general.service == "gemini"


def test_save_active_services_overwrites_a_previous_choice(global_path: Path) -> None:
    save_active_services("claude", "", global_path)
    save_active_services("gemini", "", global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["general"]["service"] == "gemini"


def test_save_active_services_preserves_other_general_keys_and_top_level_tables(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[general]\ntheme = "claude-dark"\nchars_per_token = 4\n\n'
        "[clipboard]\npoll_interval_ms = 500\n",
        encoding="utf-8",
    )
    save_active_services("claude", "gemini", global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))

    assert raw["general"] == {
        "theme": "claude-dark",
        "chars_per_token": 4,
        "service": "claude",
        "subagent_service": "gemini",
    }
    assert raw["clipboard"] == {"poll_interval_ms": 500}

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.theme == "claude-dark"
    assert cfg.general.chars_per_token == 4
    assert cfg.clipboard.poll_interval_ms == 500


def test_save_active_services_leaves_a_custom_services_table_alone(
    project: Path, global_path: Path
) -> None:
    """The service editor's writes and the picker's writes share one file and
    must not eat each other."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], max_paste_chars=30_000)
    save_services(services, global_path)

    save_active_services("claude", "", global_path)

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"].max_paste_chars == 30_000
    assert cfg2.general.service == "claude"


def test_save_active_services_is_atomic_no_leftover_tmp_files(global_path: Path) -> None:
    save_active_services("claude", "gemini", global_path)
    assert list(global_path.parent.glob("*.tmp")) == []
    assert global_path.exists()


def test_a_project_file_still_beats_the_remembered_service(
    project: Path, global_path: Path
) -> None:
    """Precedence is unchanged by persistence: the picker writes the GLOBAL
    file, and a project that pins a service still wins on the next launch."""
    save_active_services("claude", "", global_path)
    (project / ".agentclip.toml").write_text('[general]\nservice = "gemini"\n', encoding="utf-8")

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.service == "gemini"


def test_the_service_flag_still_beats_the_remembered_service(
    project: Path, global_path: Path
) -> None:
    save_active_services("claude", "", global_path)
    cfg = load_config(project, global_config_path=global_path, service_override="gemini")
    assert cfg.general.service == "gemini"


def test_a_remembered_service_that_was_since_deleted_warns_and_falls_back(
    project: Path, global_path: Path
) -> None:
    """The service editor can delete the very preset the picker just persisted.
    No machinery guards against it - load_config's existing unknown-preset path
    already degrades gracefully, which is why the saver validates nothing."""
    save_active_services("my-llm", "", global_path)
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.general.service == "unknown"
    assert any("my-llm" in w for w in cfg.warnings)


# -- matching (how a captured appearance is hunted for, and how strictly) ------


def test_every_builtin_ships_the_built_in_matcher_at_the_shipped_tolerance() -> None:
    """Both knobs are opt-in: OpenCV is an optional dependency nobody is made
    to install, and 24 is the tolerance every search used before the slider
    existed - so an untouched install behaves exactly as it always did."""
    assert MATCHERS == ("anchors", "opencv")
    assert DEFAULT_MATCHER == "anchors"
    assert (TOLERANCE_MIN, DEFAULT_TOLERANCE, TOLERANCE_MAX) == (0, 24, 64)
    for key, preset in default_services().items():
        assert preset.matcher == "anchors", key
        assert preset.tolerance == 24, key
    fresh = ServicePreset("k", "K", 1_000, 5_000)
    assert (fresh.matcher, fresh.tolerance) == ("anchors", 24)


def test_load_config_reads_a_matcher_and_tolerance_override(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.gemini]\nmatcher = "opencv"\ntolerance = 40\n', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["gemini"].matcher == "opencv"
    assert cfg.services["gemini"].tolerance == 40
    assert not cfg.warnings


def test_load_config_rejects_an_unknown_matcher(project: Path, global_path: Path) -> None:
    """Falling back silently is the one outcome a user who wrote this key would
    not notice - the search they were trying to move off is the search they get."""
    global_path.write_text('[services.gemini]\nmatcher = "sift"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["gemini"].matcher == "anchors"
    assert any("matcher" in w and "anchors, opencv" in w for w in cfg.warnings)


def test_load_config_rejects_a_non_string_matcher(project: Path, global_path: Path) -> None:
    global_path.write_text("[services.gemini]\nmatcher = 3\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["gemini"].matcher == "anchors"
    assert any("matcher" in w for w in cfg.warnings)


def test_load_config_rejects_a_tolerance_outside_its_bounds(
    project: Path, global_path: Path
) -> None:
    """The bounds the slider can express, enforced on load too - so a value the
    editor accepts is never silently replaced on the next start, and a
    hand-written one that is out of range is complained about rather than used."""
    global_path.write_text("[services.gemini]\ntolerance = 250\n", encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["gemini"].tolerance == DEFAULT_TOLERANCE
    assert any("tolerance" in w and "0..64" in w for w in cfg.warnings)


def test_load_config_rejects_a_non_integer_tolerance(project: Path, global_path: Path) -> None:
    global_path.write_text('[services.gemini]\ntolerance = "loose"\n', encoding="utf-8")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["gemini"].tolerance == DEFAULT_TOLERANCE
    assert any("tolerance" in w for w in cfg.warnings)


def test_save_then_load_round_trips_the_matching_fields(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["gemini"] = replace(services["gemini"], matcher="opencv", tolerance=31)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["gemini"]["matcher"] == "opencv"
    assert raw["services"]["gemini"]["tolerance"] == 31

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["gemini"] == services["gemini"]
    assert not cfg2.warnings


def test_untouched_matching_fields_are_not_written_at_all(
    project: Path, global_path: Path
) -> None:
    """The minimal-diff convention every later field has followed: a user who
    has never opened the MATCHING block gets a file that does not mention it,
    so a future change to the shipped defaults keeps applying to them."""
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["gemini"] = replace(services["gemini"], max_paste_chars=30_000)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert "matcher" not in raw["services"]["gemini"]
    assert "tolerance" not in raw["services"]["gemini"]


def test_a_config_written_before_the_matching_fields_existed_still_loads(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        '[services.gemini]\nlabel = "Gemini"\nmax_paste_chars = 24000\n'
        'total_context_chars = 800000\ndelivery = "stream"\n',
        encoding="utf-8",
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["gemini"].matcher == DEFAULT_MATCHER
    assert cfg.services["gemini"].tolerance == DEFAULT_TOLERANCE
    assert cfg.services["gemini"].delivery == "stream"
    assert not cfg.warnings


def test_default_profile_dir_sits_beside_the_global_config() -> None:
    """Appearance profiles are app state, so they live in the same config home
    the screen layer is handed as a plain path (it may not import platformdirs)."""
    profiles = default_profile_dir()
    assert profiles.name == "profiles"
    assert profiles.parent == default_global_config_path().parent


# -- the OpenCode permission ruleset ------------------------------------------

# The shape of a real opencode.json: a top-level "permission" block with a
# nested bash table, plus the agent/plugin keys AgentClip deliberately ignores.
OPENCODE_JSON = """{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "*": "ask",
    "read": "allow",
    "bash": {
      "*": "ask",
      "git status*": "allow",
      "git -C*": "deny"
    }
  },
  "agent": {"explore": {"permission": {"*": "deny"}}},
  "plugin": []
}
"""


def _point_at(project: Path, path: Path, *, enabled: bool | None = None) -> None:
    """Write the project TOML that aims [permission] at ``path``."""
    lines = ["[permission]", f'opencode_config = "{path.as_posix()}"']
    if enabled is not None:
        lines.append(f"enabled = {str(enabled).lower()}")
    (project / ".agentclip.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_opencode_rules_load_with_the_defaults_prepended(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    oc = tmp_path / "opencode.json"
    oc.write_text(OPENCODE_JSON, encoding="utf-8")
    _point_at(project, oc)

    cfg = load_config(project, global_config_path=global_path)
    assert not cfg.warnings
    assert cfg.permission_source == str(oc)
    # Built-in defaults first, the user's rules after: later wins, so their
    # "*": "ask" catch-all overrides the shipped "*": "allow".
    assert cfg.permission_rules[:2] == default_rules()[:2]
    user = cfg.permission_rules[len(default_rules()) :]
    assert [(r.permission, r.pattern, r.action) for r in user] == [
        ("*", "*", "ask"),
        ("read", "*", "allow"),
        ("bash", "*", "ask"),
        ("bash", "git status*", "allow"),
        ("bash", "git -C*", "deny"),
    ]
    assert evaluate("bash", "git status --short", cfg.permission_rules).action == "allow"
    assert evaluate("bash", "git -C /x status", cfg.permission_rules).action == "deny"
    assert evaluate("list", ".", cfg.permission_rules).action == "ask"


def test_the_projects_own_opencode_json_layers_over_the_global_one(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    """OpenCode's own precedence, which AgentClip did not honour before: the
    project's opencode.json is read after the global one, and rules are ordered
    with the last match winning, so the project tightens or loosens what the
    machine-wide file said."""
    oc = tmp_path / "opencode.json"
    oc.write_text('{"permission": {"bash": "allow", "read": "allow"}}', encoding="utf-8")
    _point_at(project, oc)
    (project / "opencode.json").write_text(
        '{"permission": {"bash": "deny"}}', encoding="utf-8"
    )

    cfg = load_config(project, global_config_path=global_path)
    assert evaluate("bash", "ls", cfg.permission_rules).action == "deny"
    assert evaluate("read", "a.txt", cfg.permission_rules).action == "allow"
    assert cfg.permission_source == f"{oc}, {project / 'opencode.json'}"


def test_a_project_opencode_json_alone_is_enough_to_leave_legacy_mode(
    project: Path, global_path: Path
) -> None:
    """No global file at all (the everyday machine): the project's own ruleset
    still counts, since both layers are the same one user's answer."""
    (project / "opencode.json").write_text(
        '{"permission": {"bash": "deny"}}', encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert evaluate("bash", "ls", cfg.permission_rules).action == "deny"
    assert cfg.permission_source == str(project / "opencode.json")


def test_missing_opencode_file_is_not_a_problem(project: Path, global_path: Path, tmp_path: Path) -> None:
    """Most machines have no opencode.json; that is legacy mode, not an error."""
    _point_at(project, tmp_path / "nope.json")
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.permission_rules == ()
    assert cfg.permission_source == ""
    assert not cfg.warnings


def test_malformed_opencode_json_warns_and_stays_in_legacy_mode(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    oc = tmp_path / "opencode.json"
    oc.write_text('{"permission": {"bash": ', encoding="utf-8")
    _point_at(project, oc)

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.permission_rules == ()
    assert any("not valid JSON" in w for w in cfg.warnings)


def test_unknown_action_warns_but_keeps_the_other_rules(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    oc = tmp_path / "opencode.json"
    oc.write_text('{"permission": {"read": "allow", "bash": "perhaps"}}', encoding="utf-8")
    _point_at(project, oc)

    cfg = load_config(project, global_config_path=global_path)
    assert any("perhaps" in w for w in cfg.warnings)
    assert cfg.permission_rules[-1].permission == "read"


def test_permission_can_be_switched_off(project: Path, global_path: Path, tmp_path: Path) -> None:
    oc = tmp_path / "opencode.json"
    oc.write_text(OPENCODE_JSON, encoding="utf-8")
    _point_at(project, oc, enabled=False)

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.permission.enabled is False
    assert cfg.permission_rules == ()


def test_a_file_without_a_permission_block_yields_no_rules(
    project: Path, global_path: Path, tmp_path: Path
) -> None:
    oc = tmp_path / "opencode.json"
    oc.write_text('{"model": "anthropic/claude", "plugin": []}', encoding="utf-8")
    _point_at(project, oc)

    cfg = load_config(project, global_config_path=global_path)
    assert cfg.permission_rules == ()
    assert not cfg.warnings


def test_default_opencode_path_is_opencodes_own() -> None:
    """The point of the feature: the SAME file OpenCode reads, on every platform
    (OpenCode uses ~/.config/opencode, Windows included)."""
    path = default_opencode_config_path()
    assert path.name == "opencode.json"
    assert path.parent.name == "opencode"
    assert path.parent.parent.name == ".config"


# -- extra_instructions (the user's own host-specific line, protocol.md 2) -----


def test_extra_instructions_ship_empty_everywhere() -> None:
    """No built-in knows anything about how its host mangles text: the line is
    the USER's, written after they have watched one get eaten. Empty means the
    bootstrap section and the `r` reminder both simply do not exist."""
    for key, preset in default_services().items():
        assert preset.extra_instructions == "", key
    assert ServicePreset("k", "K", 1_000, 5_000).extra_instructions == ""


def test_load_config_reads_extra_instructions(project: Path, global_path: Path) -> None:
    global_path.write_text(
        '[services.copilot-work]\n'
        'extra_instructions = "always put a space between ] and ( in code you send"\n',
        encoding="utf-8",
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["copilot-work"].extra_instructions == (
        "always put a space between ] and ( in code you send"
    )
    assert not cfg.warnings


def test_load_config_rejects_non_string_extra_instructions(
    project: Path, global_path: Path
) -> None:
    global_path.write_text(
        "[services.claude]\nextra_instructions = 12\n", encoding="utf-8"
    )
    cfg = load_config(project, global_config_path=global_path)
    assert cfg.services["claude"].extra_instructions == ""
    assert any("extra_instructions" in w for w in cfg.warnings)


def test_save_then_load_round_trips_extra_instructions(
    project: Path, global_path: Path
) -> None:
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], extra_instructions="keep ] ( apart")

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"]["extra_instructions"] == "keep ] ( apart"

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"] == services["claude"]
    assert not cfg2.warnings


def test_save_then_load_round_trips_multi_line_extra_instructions(
    project: Path, global_path: Path
) -> None:
    """The editor's field is a little BOX now, so the value can hold newlines -
    and a newline is the one character that can turn a TOML string into a
    broken file. tomlkit escapes it rather than emitting a raw line break, so
    the file stays parseable and the text arrives back byte-for-byte (which
    matters: it ships verbatim to the model, protocol.md 2)."""
    text = "keep ] ( apart\nand write code in one block"
    cfg = load_config(project, global_config_path=global_path)
    services = dict(cfg.services)
    services["claude"] = replace(services["claude"], extra_instructions=text)

    save_services(services, global_path)
    raw = tomllib.loads(global_path.read_text(encoding="utf-8"))
    assert raw["services"]["claude"]["extra_instructions"] == text

    cfg2 = load_config(project, global_config_path=global_path)
    assert cfg2.services["claude"].extra_instructions == text
    assert not cfg2.warnings
