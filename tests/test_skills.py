"""Agent Skills: discovery from the Claude/OpenCode folders, frontmatter
parsing, the `skill` tool, registry wiring, and an engine round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import Engine, NewTurn, Send
from agentclip.engine.states import Phase
from agentclip.protocol.composer import Composer
from agentclip.protocol.types import ToolCall
from agentclip.store.backups import BackupStore
from agentclip.store.session import SessionStore
from agentclip.tools.registry import ToolContext, default_registry
from agentclip.tools.sandbox import Workspace
from agentclip.tools.skills import (
    Skill,
    discover_skills,
    make_skill_spec,
    skill_search_roots,
)

GREET_SKILL = """\
---
name: greet
description: Say hello to a person in a friendly, formal tone.
---

# Greeting skill

When greeting someone:
1. Use their name.
2. Keep it to one sentence.
"""


def _write_skill(root: Path, name: str, text: str) -> Path:
    """Write `<root>/<name>/SKILL.md` and return its path."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


# -- search roots -------------------------------------------------------------


def test_search_roots_cover_claude_and_opencode(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    roots = skill_search_roots(project, home)
    # Exactly the folders both ecosystems scan, project-local first.
    assert roots == (
        project / ".claude" / "skills",
        project / ".opencode" / "skills",
        project / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".config" / "opencode" / "skills",
        home / ".agents" / "skills",
    )


# -- frontmatter parsing ------------------------------------------------------


def test_parse_name_description_and_body(tmp_path: Path) -> None:
    _write_skill(tmp_path / ".claude" / "skills", "greet", GREET_SKILL)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.name == "greet"
    assert skill.description == "Say hello to a person in a friendly, formal tone."
    assert skill.body.startswith("# Greeting skill")
    assert "Use their name." in skill.body
    assert skill.model_invocable is True


def test_frontmatter_name_overrides_directory(tmp_path: Path) -> None:
    text = "---\nname: pretty-name\ndescription: x described here.\n---\nbody\n"
    _write_skill(tmp_path / ".claude" / "skills", "ugly-dir", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.name == "pretty-name"


def test_missing_name_falls_back_to_directory(tmp_path: Path) -> None:
    text = "---\ndescription: no name field here.\n---\nbody\n"
    _write_skill(tmp_path / ".claude" / "skills", "dir-name-wins", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.name == "dir-name-wins"


def test_missing_description_falls_back_to_first_paragraph(tmp_path: Path) -> None:
    text = "---\nname: doc\n---\n\n# Heading\n\nThe first real paragraph.\nStill it.\n\nLater.\n"
    _write_skill(tmp_path / ".claude" / "skills", "doc", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.description == "The first real paragraph. Still it."


def test_disable_model_invocation_flag(tmp_path: Path) -> None:
    text = "---\nname: manual\ndescription: only manual.\ndisable-model-invocation: true\n---\nx\n"
    _write_skill(tmp_path / ".claude" / "skills", "manual", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.model_invocable is False


def test_quoted_frontmatter_values(tmp_path: Path) -> None:
    text = '---\nname: "q"\ndescription: "a quoted one."\n---\nbody\n'
    _write_skill(tmp_path / ".claude" / "skills", "q", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.name == "q"
    assert skill.description == "a quoted one."


def test_crlf_frontmatter(tmp_path: Path) -> None:
    text = "---\r\nname: crlf\r\ndescription: windows line endings.\r\n---\r\nbody line\r\n"
    _write_skill(tmp_path / ".claude" / "skills", "crlf", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.name == "crlf"
    assert skill.description == "windows line endings."
    assert skill.body == "body line"


def test_no_frontmatter_uses_body(tmp_path: Path) -> None:
    text = "Just instructions, no frontmatter.\nSecond line.\n"
    _write_skill(tmp_path / ".claude" / "skills", "bare", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.name == "bare"  # from directory
    assert skill.description == "Just instructions, no frontmatter. Second line."


def test_long_description_is_clipped(tmp_path: Path) -> None:
    long = "x" * 1000
    text = f"---\nname: big\ndescription: {long}\n---\nbody\n"
    _write_skill(tmp_path / ".claude" / "skills", "big", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert len(skill.description) <= 200
    assert skill.description.endswith("…")


def test_unterminated_frontmatter_honors_disable_flag_without_leaking(tmp_path: Path) -> None:
    # A forgotten closing '---': the blank line still ends the block, so the
    # disable flag is honored and the raw frontmatter never leaks into the listing.
    text = (
        "---\n"
        "name: secret\n"
        "description: internal only.\n"
        "disable-model-invocation: true\n"
        "\n"
        "# Body\n"
        "hidden instructions\n"
    )
    _write_skill(tmp_path / ".claude" / "skills", "secret", text)
    (skill,) = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert skill.model_invocable is False  # not failed open
    assert "---" not in skill.description
    assert "disable-model-invocation" not in skill.description
    assert "hidden instructions" in skill.body


# -- discovery: roots, precedence, robustness ---------------------------------


def test_discovers_from_all_ecosystem_folders(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    _write_skill(project / ".claude" / "skills", "a", "---\ndescription: A.\n---\nbody\n")
    _write_skill(project / ".opencode" / "skills", "b", "---\ndescription: B.\n---\nbody\n")
    _write_skill(project / ".agents" / "skills", "c", "---\ndescription: C.\n---\nbody\n")
    _write_skill(home / ".claude" / "skills", "d", "---\ndescription: D.\n---\nbody\n")
    _write_skill(home / ".config" / "opencode" / "skills", "e", "---\ndescription: E.\n---\nbody\n")
    _write_skill(home / ".agents" / "skills", "f", "---\ndescription: F.\n---\nbody\n")
    names = [s.name for s in discover_skills(project, home=home)]
    assert names == ["a", "b", "c", "d", "e", "f"]  # sorted by name


def test_project_skill_shadows_global_same_name(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    _write_skill(project / ".claude" / "skills", "dup", "---\ndescription: project wins.\n---\nP\n")
    _write_skill(home / ".claude" / "skills", "dup", "---\ndescription: global loses.\n---\nG\n")
    (skill,) = discover_skills(project, home=home)
    assert skill.description == "project wins."
    assert skill.body == "P"


def test_name_clash_is_case_insensitive(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    _write_skill(project / ".claude" / "skills", "Dup", "---\ndescription: first.\n---\nP\n")
    _write_skill(home / ".claude" / "skills", "dup", "---\ndescription: second.\n---\nG\n")
    skills = discover_skills(project, home=home)
    assert len(skills) == 1
    assert skills[0].description == "first."


def test_missing_roots_and_folders_without_skill_md_are_skipped(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".claude" / "skills" / "empty").mkdir(parents=True)  # no SKILL.md
    (project / ".claude" / "skills" / "loose.txt").parent.mkdir(exist_ok=True)
    (project / ".claude" / "skills" / "loose.txt").write_text("not a folder", encoding="utf-8")
    assert discover_skills(project, home=tmp_path / "nohome") == []


def test_discovery_never_raises_on_missing_everything(tmp_path: Path) -> None:
    assert discover_skills(tmp_path / "does-not-exist", home=tmp_path / "nope") == []


# -- the skill tool -----------------------------------------------------------


def _ctx(tmp_path: Path) -> ToolContext:
    from agentclip.config import caps_for_budget

    return ToolContext(
        workspace=Workspace(tmp_path, Config().excluded_names()),
        limits=Config().limits,
        caps=caps_for_budget(12_000),
    )


def _skill(name: str, body: str, *, desc: str = "d", invocable: bool = True) -> Skill:
    src = Path("x") / ".claude" / "skills" / name / "SKILL.md"
    return Skill(name=name, description=desc, body=body, source=src, model_invocable=invocable)


def test_skill_tool_returns_body(tmp_path: Path) -> None:
    spec = make_skill_spec([_skill("greet", "Greet warmly.")])
    call = ToolCall(id=1, tool="skill", params={"name": "greet"}, raw="")
    result = spec.handler(_ctx(tmp_path), call)
    assert result.status == "ok"
    assert result.body == "Greet warmly."


def test_skill_tool_is_auto_with_no_preview() -> None:
    spec = make_skill_spec([_skill("greet", "x")])
    assert spec.name == "skill"
    assert spec.approval_kind == "auto"
    assert spec.preview is None


def test_skill_tool_catalog_lists_skills() -> None:
    spec = make_skill_spec([_skill("greet", "x", desc="say hi"), _skill("lint", "y", desc="lint")])
    assert "- greet: say hi" in spec.catalog_doc
    assert "- lint: lint" in spec.catalog_doc
    assert "tool=skill" in spec.catalog_doc
    assert "===CLIP:END===" in spec.catalog_doc


def test_skill_tool_unknown_name_errors_with_available(tmp_path: Path) -> None:
    spec = make_skill_spec([_skill("greet", "x")])
    call = ToolCall(id=1, tool="skill", params={"name": "nope"}, raw="")
    result = spec.handler(_ctx(tmp_path), call)
    assert result.status == "error"
    assert result.code == "unknown_skill"
    assert "greet" in result.body  # lists what's available


def test_skill_tool_resolves_dir_name_alias_and_casefold(tmp_path: Path) -> None:
    spec = make_skill_spec([_skill("Pretty-Name", "BODY")])  # dir is "Pretty-Name"
    for name in ("Pretty-Name", "pretty-name", "PRETTY-NAME"):
        call = ToolCall(id=1, tool="skill", params={"name": name}, raw="")
        assert spec.handler(_ctx(tmp_path), call).body == "BODY"


def test_skill_tool_missing_name_param(tmp_path: Path) -> None:
    spec = make_skill_spec([_skill("greet", "x")])
    call = ToolCall(id=1, tool="skill", params={}, raw="")
    result = spec.handler(_ctx(tmp_path), call)
    assert result.status == "error"
    assert result.code == "missing_param"


def _named(name: str, dir_name: str, body: str) -> Skill:
    src = Path("x") / ".claude" / "skills" / dir_name / "SKILL.md"
    return Skill(name=name, description="d", body=body, source=src, model_invocable=True)


def _load(spec, ctx: ToolContext, name: str) -> str:
    return spec.handler(ctx, ToolCall(id=1, tool="skill", params={"name": name}, raw="")).body


def test_skill_tool_resolves_directory_name_alias(tmp_path: Path) -> None:
    # When frontmatter name differs from the folder, BOTH resolve.
    spec = make_skill_spec([_named("pretty-name", "ugly-dir", "BODY")])
    ctx = _ctx(tmp_path)
    for name in ("pretty-name", "PRETTY-NAME", "ugly-dir", "Ugly-Dir"):
        assert _load(spec, ctx, name) == "BODY"


def test_canonical_name_wins_over_another_skills_dir_alias(tmp_path: Path) -> None:
    # X: name 'deploy' in dir 'release'; Y: name 'release' in dir 'helper'.
    # skill(name='release') must return Y's body, not X's (whose dir is 'release').
    x = _named("deploy", "release", "BODY_X")
    y = _named("release", "helper", "BODY_Y")
    spec = make_skill_spec([x, y])
    ctx = _ctx(tmp_path)
    assert _load(spec, ctx, "release") == "BODY_Y"  # canonical name wins
    assert _load(spec, ctx, "deploy") == "BODY_X"
    assert _load(spec, ctx, "helper") == "BODY_Y"  # dir alias still works where free


def test_skill_listing_is_bounded_by_budget() -> None:
    many = [_skill(f"skill{i:02d}", f"b{i}", desc="d" * 60) for i in range(40)]
    spec = make_skill_spec(many, max_listing_chars=300)
    doc = spec.catalog_doc
    assert "- skill00:" in doc  # at least the first is always listed
    assert "- skill39:" not in doc  # the tail is dropped
    assert "more skill(s) not listed" in doc  # ...and the drop is disclosed


def test_unbounded_listing_shows_all() -> None:
    many = [_skill(f"skill{i:02d}", f"b{i}") for i in range(40)]
    doc = make_skill_spec(many).catalog_doc  # no budget
    assert "- skill39:" in doc
    assert "more skill(s) not listed" not in doc


def test_disabled_skill_unreachable_through_registry_handler(tmp_path: Path) -> None:
    reg = default_registry([_skill("on", "ONBODY"), _skill("off", "OFFBODY", invocable=False)])
    spec = reg.get("skill")
    assert spec is not None
    result = spec.handler(_ctx(tmp_path), ToolCall(id=1, tool="skill", params={"name": "off"}, raw=""))
    assert result.status == "error"
    assert result.code == "unknown_skill"  # the body marked disabled is never loadable
    assert spec.handler(_ctx(tmp_path), ToolCall(id=1, tool="skill", params={"name": "on"}, raw="")).body == "ONBODY"


def test_unknown_skill_code_is_in_closed_error_set(tmp_path: Path) -> None:
    from agentclip.protocol.types import ERROR_CODES

    spec = make_skill_spec([_skill("greet", "x")])
    result = spec.handler(_ctx(tmp_path), ToolCall(id=1, tool="skill", params={"name": "no"}, raw=""))
    assert result.code in ERROR_CODES  # the emitted code honors the documented closed set


# -- registry wiring ----------------------------------------------------------


def test_registry_without_skills_has_no_skill_tool() -> None:
    assert "skill" not in default_registry().names()
    assert "skill" not in default_registry(()).names()


def test_registry_inserts_skill_tool_after_run_command() -> None:
    names = default_registry([_skill("greet", "x")]).names()
    assert "skill" in names
    assert names.index("run_command") < names.index("skill") < names.index("ask_user")


def test_registry_skips_skill_tool_when_all_disabled() -> None:
    names = default_registry([_skill("manual", "x", invocable=False)]).names()
    assert "skill" not in names


def test_registry_lists_only_invocable_skills() -> None:
    reg = default_registry([_skill("on", "x"), _skill("off", "y", invocable=False)])
    catalog = reg.render_catalog()
    assert "- on:" in catalog
    assert "- off:" not in catalog


# -- engine round-trip --------------------------------------------------------


def _engine_with_skills(project: Path, skills: list[Skill]) -> Engine:
    cfg = load_config(project, global_config_path=project / "no-such-global.toml")
    registry = default_registry(skills)
    workspace = Workspace(project, cfg.excluded_names())
    session = SessionStore(project, service=cfg.general.service)
    backups = BackupStore(session.session_dir)
    composer = Composer(cfg.preset(), cfg.caps(), registry.render_catalog(), project.name, "TestOS")
    return Engine(cfg, registry, workspace, session, backups, composer)


def test_engine_runs_skill_call_and_returns_body(project: Path) -> None:
    engine = _engine_with_skills(project, [_skill("greet", "Greet the user warmly and briefly.")])
    bootstrap = engine.start_task("greet someone")
    assert "skill(name" in bootstrap.chunks[0]  # the catalog advertises the skill tool
    assert "- greet:" in bootstrap.chunks[0]

    reply = (
        "===CLIP:CALL id=1 tool=skill===\n"
        "name: greet\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 turn=1===\n"
    )
    assert isinstance(engine.ingest(reply), NewTurn)
    assert engine.pending() == ()  # auto tool: no approval gate
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "status=ok" in payload
    assert "Greet the user warmly and briefly." in payload
    assert engine.status().phase is Phase.AWAITING_REPLY


def test_engine_unknown_skill_returns_error(project: Path) -> None:
    engine = _engine_with_skills(project, [_skill("greet", "body")])
    engine.start_task("t")
    reply = (
        "===CLIP:CALL id=1 tool=skill===\n"
        "name: missing\n"
        "===CLIP:END===\n"
        "===CLIP:EOM calls=1 turn=1===\n"
    )
    assert isinstance(engine.ingest(reply), NewTurn)
    step = engine.execute()
    assert isinstance(step, Send)
    payload = step.outbound.chunks[0]
    assert "code=unknown_skill" in payload


@pytest.mark.parametrize(
    ("text", "expected_name"),
    [
        ("", "edge"),
        ("---\n", "edge"),
        ("---\nname: x\n", "x"),  # unterminated, but name recovered best-effort
        ("---\n---\n", "edge"),
    ],
)
def test_parser_tolerates_degenerate_files(
    tmp_path: Path, text: str, expected_name: str
) -> None:
    _write_skill(tmp_path / ".claude" / "skills", "edge", text)
    skills = discover_skills(tmp_path, home=tmp_path / "nohome")
    assert len(skills) == 1  # never raises
    assert skills[0].name == expected_name
