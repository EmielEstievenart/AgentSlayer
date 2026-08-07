"""Headless tests for the new per-service ``total_context_chars`` field and the
service-editor persistence path (``save_services``): TOML merge/validation,
round-tripping through load_config, minimal-diff writes, and the atomic-write
mechanics. No Textual here - the pilot tests for the editor UI itself live in
tests/tui/test_service_editor_ui.py."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from agentclip.config import (
    BUILTIN_SERVICE_KEYS,
    ServicePreset,
    default_services,
    load_config,
    save_services,
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
