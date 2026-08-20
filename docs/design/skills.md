# Agent Skills

AgentClip discovers **Agent Skills** — reusable `SKILL.md` instruction files —
from the same folders Claude Code and OpenCode use, and exposes them to the web
LLM through a `skill` tool. A developer's existing skills work here with no
copying or conversion.

A skill is a folder containing a `SKILL.md` with YAML frontmatter (`name`,
`description`) and a markdown body of instructions:

```
greet/
└── SKILL.md
```

```yaml
---
name: greet
description: Say hello to a person in a friendly, formal tone.
---

# Greeting skill
When greeting someone, use their name and keep it to one sentence.
```

## Where skills are found

On session start AgentClip scans these roots, **highest precedence first** —
on a name clash the first hit wins, so a project-local skill beats a same-named
global one (matching OpenCode; note Claude Code resolves clashes the other way,
personal/enterprise over project, so a skill present in both a project and your
home dir may resolve differently here than in Claude Code). The folder *set* is
the union of what both ecosystems scan:

| Scope | Path |
|---|---|
| Project — Claude | `<project>/.claude/skills/<name>/SKILL.md` |
| Project — OpenCode | `<project>/.opencode/skills/<name>/SKILL.md` |
| Project — Agent Skills standard | `<project>/.agents/skills/<name>/SKILL.md` |
| Global — Claude | `~/.claude/skills/<name>/SKILL.md` |
| Global — OpenCode | `~/.config/opencode/skills/<name>/SKILL.md` |
| Global — Agent Skills standard | `~/.agents/skills/<name>/SKILL.md` |

Discovery never raises: a missing root, a folder without a `SKILL.md`, or an
unreadable file is skipped. The roots are listed in
`executor/tools/skills.py:skill_search_roots`.

## Progressive disclosure

Mirroring both ecosystems, skills are surfaced cheaply and loaded on demand:

1. **Bootstrap (cheap):** the `skill` tool's catalog entry lists each
   model-invocable skill as `- <name>: <description>`. Each description is
   clipped to one line (≤200 chars), and the listing as a whole is bounded to a
   budget derived from the active preset (¼ of `max_paste_chars`) — overflow
   skills are dropped with a `(+N more …)` footer — because the bootstrap has no
   truncation fallback and must not be overflowable by a large skills library.
   The `skill` tool is omitted entirely when no skills are discovered.
2. **On demand (full):** the model calls `skill(name=...)` and the result opens
   with a short header — the skill's absolute folder, and what is beside
   `SKILL.md` in it — followed by the full body, which it then follows.

The `skill` tool is read-only and auto-approved (no gate) — loading instructions
is harmless; any actions the skill prescribes still flow through AgentClip's
normal gated tools (`edit_file`, `run_command`, …). Name lookup is
case-insensitive and accepts either the frontmatter `name` or the directory
name. An unknown name returns `error code=unknown_skill` listing what is
available.

## Bundled side files

A skill that ships a `scripts/` or a `references/` beside its `SKILL.md` works.
The tool result's header is what makes it work:

```
folder: /home/u/.claude/skills/deploy
files: reference.md, scripts/check.py
(read a side file with read_file on its full path, /home/u/.claude/skills/deploy/<file>;
 run its scripts through run_command as usual)

<the SKILL.md body, verbatim>
```

Two halves, deliberately:

- **The listing IS the discovery surface.** `glob`/`grep`/`list_dir` do not
  reach into skill folders (below), so the result names what is there instead.
  It is bounded — 3 levels deep, ≤50 entries, ≤400 chars, with an honest
  `and N more` tail — because it rides in every result the tool returns.
- **The session's `Workspace` carries each discovered skill folder as an
  `extra_read_roots` entry** (`executor/tools/sandbox.py`): an **absolute** path
  resolving inside one is readable. Only absolute — a relative path still means
  "under the project root", so a skill folder can never shadow a project file by
  name. Containment is checked on the *resolved* path, so a symlink or a `..`
  inside a skill folder is not a tunnel; `HARD_EXCLUDED_NAMES` still seals
  `.agentclip` there; and writes, edits and deletes are refused with *"skill
  folders are read-only"*. Traversal does not extend into them
  (`Workspace.resolve_scan`, which `list_dir`/`glob`/`grep` use instead of
  `resolve_read`).

The header rides in the **result**, never in the catalog: the bootstrap has
~150 chars of slack and no truncation fallback (`protocol.md` §2, "Budget
headroom"), and a folder path is of no use until a skill is actually loaded.

For permissions, an absolute skill-folder path reaches the matcher **as
written** — it has no workspace-relative form, and an invented one would be a
second spelling for a rule to miss. Nothing is bypassed by that: `*` crosses
slashes in the wildcard matcher, so the shipped `read: {"*.env": "ask"}` still
asks about `<skill folder>/.env`.

Wiring: `engine/link/factory.py` passes `tuple(skill.source.parent for skill in
discovered_skills)` when it builds the `Workspace`. Discovery and the workspace
run on the same machine through the same `Host`, so in a remote session both
halves are the target's — the paths agree by construction, not by agreement.

## Frontmatter fields read

| Field | Effect |
|---|---|
| `name` | The skill's listed/callable name. Defaults to the directory name. |
| `description` | Shown in the listing so the model knows when to use the skill. Falls back to the body's first paragraph when absent. |
| `disable-model-invocation` | When `true`, the skill is hidden from the listing and not loadable by the model (AgentClip has no separate user-invocation surface, so such skills are effectively dormant here). |

Other frontmatter fields (`allowed-tools`, `context`, `model`, …) are ignored:
they are agent-runtime concerns that do not map onto AgentClip's clipboard relay.

Parsing is stdlib-only (no YAML dependency, PyInstaller-friendly): the simple
top-level `key: value` scalars skills use in practice are read; block scalars
and nested maps are not interpreted.

## Deliberate limitations

- **No dynamic context injection.** Claude Code's `` !`command` `` preprocessing
  is not run; the body is returned verbatim. The model can run any needed
  command through `run_command`.
- **No `$ARGUMENTS` substitution.** Skills are loaded as standing instructions,
  not parameterized commands.
- **Discovered once per process.** There is no live re-scan; add a skill and
  restart to pick it up.

## Wiring

`engine/link/factory.py` calls `discover_skills(project_root)` once per builder
and passes the result to `default_registry(skills)`, which inserts the `skill`
tool (built by `executor/tools/skills.py:make_skill_spec`) after `run_command`
and before the meta tools — and passes the same skills' folders to the
`Workspace` as read-only carve-outs (above). Everything the tools need lives in
the `agentclip.executor.tools` layer so the import direction in
`architecture.md` is preserved; only the wiring is engine-side.
