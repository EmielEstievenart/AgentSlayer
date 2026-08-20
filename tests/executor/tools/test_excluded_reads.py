"""The exclusion list, seen through the tools: readable, unwritable, unlisted.

`paths.exclude` used to mean three things at once - skip in traversal, refuse
to read, refuse to write - and the middle one was wrong: a user who says "look
at .vscode/settings.json" got a sandbox violation for a file sitting in their
own project. The split these tests pin (architecture.md section 3, step 4):

* an explicitly named file inside an excluded directory READS fine;
* writing anywhere inside one is still refused;
* traversal (list_dir/glob/grep) still never descends into one;
* .agentclip / .agentclip.toml stay sealed in every direction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import Config, LimitsConfig, caps_for_budget
from agentclip.executor.tools import fs_tools
from agentclip.executor.tools.registry import ToolContext
from agentclip.executor.tools.sandbox import Workspace
from agentclip.protocol.types import ToolCall


def make_call(tool: str, **params: str) -> ToolCall:
    return ToolCall(id=1, tool=tool, params=dict(params), raw="")


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=Workspace(tmp_path, Config().excluded_names()),
        limits=LimitsConfig(),
        caps=caps_for_budget(12_000),
        backup_hook=None,
    )


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")
    return p


@pytest.mark.parametrize(
    "rel",
    [".vscode/settings.json", "node_modules/left-pad/index.js", ".git/config"],
)
def test_read_file_inside_an_excluded_directory_succeeds(
    ctx: ToolContext, tmp_path: Path, rel: str
) -> None:
    _write(tmp_path, rel, "needle\n")
    res = fs_tools.read_file(ctx, make_call("read_file", path=rel))
    assert res.status == "ok"
    assert "needle" in res.body


def test_write_inside_an_excluded_directory_is_still_refused(
    ctx: ToolContext, tmp_path: Path
) -> None:
    _write(tmp_path, ".vscode/settings.json", "{}\n")
    res = fs_tools.write_file(
        ctx, make_call("write_file", path=".vscode/settings.json", content="{}\n")
    )
    assert res.status == "error" and res.code == "path_outside_workspace"
    assert (tmp_path / ".vscode" / "settings.json").read_text(encoding="utf-8") == "{}\n"


def test_agentclip_stays_sealed_for_reads(ctx: ToolContext, tmp_path: Path) -> None:
    _write(tmp_path, ".agentclip/sessions/log.jsonl", "transcript\n")
    res = fs_tools.read_file(ctx, make_call("read_file", path=".agentclip/sessions/log.jsonl"))
    assert res.status == "error" and res.code == "path_outside_workspace"
    res = fs_tools.write_file(
        ctx, make_call("write_file", path=".agentclip/sessions/log.jsonl", content="x\n")
    )
    assert res.status == "error" and res.code == "path_outside_workspace"


def test_traversal_still_skips_excluded_directories(ctx: ToolContext, tmp_path: Path) -> None:
    """Readable by name is not the same as visible: sweeps stay clean."""
    _write(tmp_path, "src/app.py", "needle\n")
    _write(tmp_path, ".vscode/settings.json", "needle\n")
    _write(tmp_path, "node_modules/left-pad/index.js", "needle\n")

    listing = fs_tools.list_dir(ctx, make_call("list_dir", depth="3"))
    assert listing.status == "ok"
    assert ".vscode/ (excluded, not listed)" in listing.body
    assert "settings.json" not in listing.body

    hits = fs_tools.glob(ctx, make_call("glob", pattern="**/*.js"))
    assert hits.status == "ok" and hits.body.split("\n")[-1] == "0 matches"

    swept = fs_tools.grep(ctx, make_call("grep", pattern="needle"))
    assert swept.status == "ok"
    assert "src/app.py" in swept.body
    assert ".vscode" not in swept.body and "node_modules" not in swept.body
