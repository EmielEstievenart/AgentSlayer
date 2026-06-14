"""Agent Skills: discover reusable SKILL.md instructions and expose them as a
`skill` tool, the same way Claude Code and OpenCode do.

A skill is a folder `<name>/SKILL.md` holding YAML frontmatter (`name`,
`description`) and a markdown body of instructions. AgentClip lists each
discovered skill's name + description in the bootstrap (cheap, progressive
disclosure) and lets the model pull the full body on demand by calling
`skill(name=...)` - mirroring the native `skill` tool in both ecosystems.

Search roots, highest precedence first (project beats global; on a name clash
the first hit wins). These are exactly the folders Claude Code and OpenCode
scan, so a developer's existing skills work here with no copying:

    <project>/.claude/skills/    <project>/.opencode/skills/   <project>/.agents/skills/
    ~/.claude/skills/            ~/.config/opencode/skills/     ~/.agents/skills/

Stdlib-only (no YAML dependency, PyInstaller-friendly): the frontmatter parser
reads the simple top-level `key: value` scalars skills use in practice; block
scalars and nested maps are not interpreted. A missing/blank description falls
back to the body's first paragraph, as Claude Code does.

Not (yet) supported, by design: dynamic context injection (`` !`cmd` ``),
`$ARGUMENTS` substitution, and reading a skill's bundled side files (those live
outside the sandboxed project root). The model gets the SKILL.md body verbatim
and drives any commands through the normal gated tools.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agentclip.protocol.types import ToolCall
from agentclip.tools.registry import ToolContext, ToolError, ToolSpec, require, tool_handler

# A listed description is one line, clipped so the bootstrap stays inside the
# paste budget; the full body still loads on demand via the skill tool. The
# whole listing is additionally bounded by a total budget (see skill_listing).
_MAX_DESCRIPTION_CHARS = 200


@dataclass(frozen=True, slots=True)
class Skill:
    """One discovered skill. `body` is the SKILL.md content after frontmatter."""

    name: str
    description: str
    body: str
    source: Path  # the SKILL.md file
    model_invocable: bool = True  # False when frontmatter sets disable-model-invocation


def skill_search_roots(project_root: Path, home: Path | None = None) -> tuple[Path, ...]:
    """The skill folders to scan, highest precedence first. Project-local roots
    beat the user's global roots, matching OpenCode's precedence."""
    base = home if home is not None else Path.home()
    return (
        project_root / ".claude" / "skills",
        project_root / ".opencode" / "skills",
        project_root / ".agents" / "skills",
        base / ".claude" / "skills",
        base / ".config" / "opencode" / "skills",
        base / ".agents" / "skills",
    )


def discover_skills(project_root: Path, *, home: Path | None = None) -> list[Skill]:
    """Load every `<root>/<name>/SKILL.md` across the search roots.

    De-duplicated by name (case-insensitive); the first occurrence wins, so a
    project skill shadows a same-named global one. Never raises - unreadable
    roots/files and skills without a SKILL.md are simply skipped. Returned
    sorted by name for a stable bootstrap listing.
    """
    seen: dict[str, Skill] = {}
    for root in skill_search_roots(project_root, home):
        for skill in _load_root(root):
            seen.setdefault(skill.name.casefold(), skill)
    return sorted(seen.values(), key=lambda s: s.name.casefold())


def _load_root(root: Path) -> list[Skill]:
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []  # root absent or unreadable: no skills here
    skills: list[Skill] = []
    for folder in folders:
        path = folder / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # no SKILL.md (or unreadable): not a skill folder
        skills.append(_parse_skill(folder.name, text, path))
    return skills


def _parse_skill(dir_name: str, text: str, source: Path) -> Skill:
    front, body = _split_frontmatter(text)
    name = (front.get("name") or "").strip() or dir_name
    description = (front.get("description") or "").strip() or _first_paragraph(body)
    return Skill(
        name=name,
        description=_clip(description, _MAX_DESCRIPTION_CHARS),
        body=body.strip(),
        source=source,
        model_invocable=not _truthy(front.get("disable-model-invocation")),
    )


# -- frontmatter parsing (minimal, dependency-free) ---------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading `---`-delimited YAML frontmatter block from the body.

    Returns (frontmatter dict of top-level scalars, body). With no opening or
    closing `---` fence the whole text is the body and the dict is empty.
    """
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, norm
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return _parse_front_lines(lines[1:i]), "\n".join(lines[i + 1 :])
    # Unterminated frontmatter (a forgotten closing `---`): fall back to the
    # blank line that conventionally ends the block. This still honors
    # disable-model-invocation and keeps the raw `---`/`key: value` lines out of
    # the body (and out of the listed description), rather than failing open.
    end = next((i for i in range(1, len(lines)) if not lines[i].strip()), len(lines))
    return _parse_front_lines(lines[1:end]), "\n".join(lines[end:])


def _parse_front_lines(lines: Sequence[str]) -> dict[str, str]:
    front: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            continue  # indented = nested/continuation: only top-level scalars read
        key, sep, value = line.partition(":")
        if not sep:
            continue
        front[key.strip().lower()] = _unquote(value.strip())
    return front


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in ("true", "yes", "on", "1")


def _first_paragraph(body: str) -> str:
    """The first non-blank paragraph of the body, skipping leading headings."""
    para: list[str] = []
    for line in body.strip().split("\n"):
        if line.strip():
            if not para and line.lstrip().startswith("#"):
                continue  # skip a leading markdown heading
            para.append(line.strip())
        elif para:
            break
    return " ".join(para)


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace to one line and clip to `limit` chars for listing."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# -- the `skill` tool ---------------------------------------------------------


def skill_listing(skills: Sequence[Skill], *, max_chars: int | None = None) -> str:
    """The indented `- name: description` block shown in the bootstrap.

    With `max_chars`, the block is bounded so a large skills library can never
    push the bootstrap past the paste budget: lines are added until the budget
    is reached (always at least one), then a `(+N more ...)` footer is appended.
    """
    lines: list[str] = []
    used = 0
    for i, s in enumerate(skills):
        line = f"  - {s.name}: {s.description}" if s.description else f"  - {s.name}"
        if max_chars is not None and lines and used + len(line) + 1 > max_chars:
            dropped = len(skills) - i
            lines.append(f"  (+{dropped} more skill(s) not listed; ask the user for the name)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


_SKILL_DOC = """\
skill(name*)
  Load a reusable skill and follow it: the result body is the skill's full
  instructions - a pre-written procedure or reference for a specific task.
  When a listed skill fits the task, load it before improvising. Available
  skills (call skill to get the full text):
{listing}
===CLIP:CALL id=1 tool=skill===
name: {example}
===CLIP:END==="""


def make_skill_spec(listable: Sequence[Skill], *, max_listing_chars: int | None = None) -> ToolSpec:
    """Build the `skill` ToolSpec over the model-invocable skills. The handler
    returns a skill's body; the catalog_doc lists what is available."""
    if not listable:
        raise ValueError("make_skill_spec requires at least one skill")
    # Two passes so a canonical name is never shadowed by another skill's
    # directory-name alias: register every canonical name first, then add
    # dir-name aliases only where they do not collide with a real name.
    by_name: dict[str, Skill] = {}
    for skill in listable:
        by_name.setdefault(skill.name.casefold(), skill)
    for skill in listable:
        by_name.setdefault(skill.source.parent.name.casefold(), skill)  # dir-name alias

    doc = _SKILL_DOC.format(
        listing=skill_listing(listable, max_chars=max_listing_chars), example=listable[0].name
    )

    @tool_handler
    def handler(ctx: ToolContext, call: ToolCall) -> str:
        (name,) = require(call, "name")
        skill = by_name.get(name.strip().casefold())
        if skill is None:
            available = ", ".join(s.name for s in listable)
            raise ToolError(
                "unknown_skill",
                f"no skill named {name.strip()!r}",
                f"call skill with one of: {available}.",
            )
        return skill.body or skill.description or f"(skill {skill.name!r} has no instructions)"

    return ToolSpec("skill", "auto", handler, None, doc)
