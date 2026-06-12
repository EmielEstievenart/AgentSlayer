"""Shared fixtures: a tmp project workspace, default config, registry, and an
Engine factory. The engine round-trip tests never touch a real clipboard."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine
from agentclip.protocol.composer import Composer
from agentclip.store.backups import BackupStore
from agentclip.store.session import SessionStore
from agentclip.tools.registry import ToolRegistry, default_registry
from agentclip.tools.sandbox import Workspace

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


@pytest.fixture
def config(project: Path) -> Config:
    """Default config: the global file does not exist; <project>/.agentclip.toml
    is honored when a test writes one BEFORE requesting this fixture."""
    return load_config(project, global_config_path=project / "no-such-global.toml")


@pytest.fixture
def registry() -> ToolRegistry:
    return default_registry()


EngineFactory = Callable[..., Engine]


@pytest.fixture
def make_engine(project: Path, registry: ToolRegistry) -> EngineFactory:
    """Factory building a fully wired, headless Engine over the tmp project.

    Reloads config on each call so tests can drop a .agentclip.toml into the
    project (e.g. to extend the command allowlist) before building.
    """

    def factory(config: Config | None = None) -> Engine:
        cfg = config or load_config(project, global_config_path=project / "no-such-global.toml")
        workspace = Workspace(project, cfg.excluded_names())
        session = SessionStore(project, service=cfg.general.service)
        backups = BackupStore(session.session_dir)
        composer = Composer(
            cfg.preset(), cfg.caps(), registry.render_catalog(), project.name, "TestOS"
        )
        return Engine(cfg, registry, workspace, session, backups, composer)

    return factory


@pytest.fixture
def engine(make_engine: EngineFactory) -> Engine:
    return make_engine()
