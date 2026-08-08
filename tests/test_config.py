"""Headless tests for the per-service ``total_context_chars`` and
``stable_seconds`` fields and the service-editor persistence path
(``save_services``): TOML merge/validation, round-tripping through load_config,
minimal-diff writes, and the atomic-write mechanics. No Textual here - the pilot
tests for the editor UI itself live in tests/tui/test_service_editor_ui.py."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    DEFAULT_STABLE_SECONDS,
    DEFAULT_THEME,
    ServicePreset,
    default_global_config_path,
    default_profile_dir,
    default_services,
    load_config,
    save_services,
    save_theme,
)


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


def test_default_profile_dir_sits_beside_the_global_config() -> None:
    """Appearance profiles are app state, so they live in the same config home
    the screen layer is handed as a plain path (it may not import platformdirs)."""
    profiles = default_profile_dir()
    assert profiles.name == "profiles"
    assert profiles.parent == default_global_config_path().parent
