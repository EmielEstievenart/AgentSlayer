"""The [remote] table, the --ssh/--remote-root flags, and which machine each
piece of a remote session's config comes from (docs/design/remote-ssh.md 4 and
its revision "the target owns its policy", as amended by
docs/design/remote-executor.md section 2.5: the engine owns policy wholesale,
so the target's config answers for [approval] too - only the global config.toml
is read from this PC)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import RemoteTarget, load_config
from agentclip.executor.hosts import FakeHost
from agentclip.executor.permissions import PermissionRule, evaluate

# The remote user's home, and the ruleset AgentClip keeps under it - which in a
# remote session is composed from `home`, not asked of platformdirs (which
# answers for THIS machine only).
REMOTE_HOME = Path("/home/dev")
REMOTE_RULES = "/home/dev/.config/agentclip/permissions.json"
REMOTE_PROJECT_RULES = "/srv/app/.agentclip/permissions.json"

SAVED = """\
[remote.pi]
host = "raspberrypi.local"
user = "emiel"
port = 2222
root = "/home/emiel/code/thing"

[remote.build]
host = "10.0.0.9"
root = "/srv/build"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def global_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _load(project: Path, global_path: Path, **kwargs: object):
    return load_config(project, global_config_path=global_path, **kwargs)  # type: ignore[arg-type]


def _remote(global_path: Path, host: FakeHost):
    """A session on ``host``, loaded the way cli.remote_launch loads one: the
    project root and the home directory both belong to the target."""
    return load_config(
        Path("/srv/app"), global_config_path=global_path, host=host, home=REMOTE_HOME
    )


# -- the table -----------------------------------------------------------------


def test_no_remote_table_is_a_local_session(project: Path, global_path: Path) -> None:
    cfg = _load(project, global_path)
    assert cfg.remote.is_remote() is False
    assert cfg.remote.selected() is None
    assert cfg.remote.targets == {}


def test_saved_targets_are_read_from_the_table(project: Path, global_path: Path) -> None:
    global_path.write_text(SAVED, encoding="utf-8")
    cfg = _load(project, global_path)
    assert cfg.remote.targets["pi"] == RemoteTarget(
        name="pi",
        host="raspberrypi.local",
        user="emiel",
        port=2222,
        root="/home/emiel/code/thing",
    )
    assert cfg.remote.targets["build"].user == ""  # absent keys stay blank
    assert cfg.remote.targets["build"].port == 0  # 0 = ask ~/.ssh/config


def test_a_target_named_for_its_host_need_not_repeat_it(
    project: Path, global_path: Path
) -> None:
    global_path.write_text('[remote."box.local"]\nroot = "/srv/x"\n', encoding="utf-8")
    cfg = _load(project, global_path)
    assert cfg.remote.targets["box.local"].host == "box.local"


def test_a_scalar_where_a_target_should_be_is_warned_about(
    project: Path, global_path: Path
) -> None:
    global_path.write_text('[remote]\npi = "raspberrypi.local"\n', encoding="utf-8")
    cfg = _load(project, global_path)
    assert cfg.remote.targets == {}
    assert any("[remote.pi] must be a table" in w for w in cfg.warnings)


# -- selecting one -------------------------------------------------------------


def test_ssh_flag_selects_a_saved_target(project: Path, global_path: Path) -> None:
    global_path.write_text(SAVED, encoding="utf-8")
    cfg = _load(project, global_path, remote_target="pi")
    selected = cfg.remote.selected()
    assert cfg.remote.is_remote()
    assert selected is not None
    assert (selected.host, selected.user, selected.port) == ("raspberrypi.local", "emiel", 2222)
    assert selected.root == "/home/emiel/code/thing"
    assert not cfg.warnings


def test_remote_root_flag_overrides_the_saved_root(project: Path, global_path: Path) -> None:
    global_path.write_text(SAVED, encoding="utf-8")
    cfg = _load(project, global_path, remote_target="pi", remote_root="/tmp/other")
    selected = cfg.remote.selected()
    assert selected is not None and selected.root == "/tmp/other"


def test_a_spelled_out_destination_needs_no_saved_target(
    project: Path, global_path: Path
) -> None:
    cfg = _load(project, global_path, remote_target="dev@10.0.0.5:2200", remote_root="/srv/app")
    selected = cfg.remote.selected()
    assert selected is not None
    assert (selected.host, selected.user, selected.port) == ("10.0.0.5", "dev", 2200)
    assert selected.root == "/srv/app"
    assert not cfg.warnings  # a destination is not a missing target


def test_a_bare_name_is_taken_as_a_host_or_ssh_config_alias(
    project: Path, global_path: Path
) -> None:
    cfg = _load(project, global_path, remote_target="workbox", remote_root="/srv/app")
    selected = cfg.remote.selected()
    assert selected is not None and selected.host == "workbox" and selected.user == ""
    assert any("names no [remote.workbox] target" in w for w in cfg.warnings)


# -- where the project's own config comes from ---------------------------------


def test_the_project_config_is_read_through_the_host(
    project: Path, global_path: Path
) -> None:
    """In a remote session the project file is the REMOTE one (design 6)."""
    host = FakeHost("/srv/app")
    host.add_file("/srv/app/.agentclip.toml", '[general]\nservice = "claude"\n')
    # ...and the LOCAL file of the same name must not be what wins.
    (project / ".agentclip.toml").write_text('[general]\nservice = "gemini"\n', encoding="utf-8")

    cfg = load_config(Path("/srv/app"), global_config_path=global_path, host=host)
    assert cfg.general.service == "claude"


def test_a_project_without_a_config_on_the_host_is_not_an_error(global_path: Path) -> None:
    host = FakeHost("/srv/app")
    cfg = load_config(Path("/srv/app"), global_config_path=global_path, host=host)
    assert cfg.warnings == ()
    assert cfg.general.service == "chatgpt-attach"


def test_the_global_file_stays_local_in_a_remote_session(global_path: Path) -> None:
    """The user's own settings are the operator's, not the remote machine's."""
    global_path.write_text('[general]\nchars_per_token = 4\n', encoding="utf-8")
    host = FakeHost("/srv/app")
    cfg = load_config(Path("/srv/app"), global_config_path=global_path, host=host)
    assert cfg.general.chars_per_token == 4


def test_permissions_are_read_through_the_host(
    global_path: Path, tmp_path: Path
) -> None:
    """The tripwire, inverted: the ruleset now comes off the TARGET.

    Every file a rule can save is over there, so the machine whose files are at
    risk is the one that says what may happen to them ("the target owns its
    policy"). A local file at the very same path must not be what answers.
    """
    rules = tmp_path / "permissions.json"
    rules.write_text('{"permission": {"bash": "deny"}}', encoding="utf-8")
    global_path.write_text(
        f'[permission]\npermissions_config = "{rules.as_posix()}"\n', encoding="utf-8"
    )
    host = FakeHost("/srv/app")
    host.add_file(rules.as_posix(), '{"permission": {"bash": "allow"}}')

    cfg = load_config(Path("/srv/app"), global_config_path=global_path, host=host)
    assert cfg.permission_source == f"fake:{rules.as_posix()}"
    assert cfg.permission_rules[-1].action == "allow"  # the REMOTE file's answer


def test_the_remote_global_ruleset_comes_from_the_remote_home(global_path: Path) -> None:
    """With no [permission] permissions_config, the path is composed from the
    TARGET user's home: ~/.config/agentclip/permissions.json over there."""
    host = FakeHost("/srv/app")
    host.add_file(REMOTE_RULES, '{"permission": {"bash": "deny"}}')

    cfg = _remote(global_path, host)
    assert cfg.permission_rules[-1] == PermissionRule("bash", "*", "deny")
    assert cfg.permission_source == f"fake:{REMOTE_RULES}"


def test_a_tilde_in_permissions_config_expands_on_the_target(global_path: Path) -> None:
    """The setting names which file holds the ruleset, and the ruleset is over
    there - so its ~ is the remote home, not the operator's."""
    global_path.write_text(
        '[permission]\npermissions_config = "~/rules.json"\n', encoding="utf-8"
    )
    host = FakeHost("/srv/app")
    host.add_file("/home/dev/rules.json", '{"permission": {"bash": "deny"}}')

    cfg = _remote(global_path, host)
    assert cfg.permission_source == "fake:/home/dev/rules.json"


def test_the_projects_ruleset_outranks_the_remote_global_one(
    global_path: Path,
) -> None:
    host = FakeHost("/srv/app")
    host.add_file(REMOTE_RULES, '{"permission": {"bash": "deny"}}')
    host.add_file(REMOTE_PROJECT_RULES, '{"permission": {"bash": "allow"}}')

    cfg = _remote(global_path, host)
    # Ordered rules, last match wins: both layers are present, the project's
    # answers.
    assert evaluate("bash", "ls", cfg.permission_rules).action == "allow"
    assert cfg.permission_source == f"fake:{REMOTE_RULES}, fake:{REMOTE_PROJECT_RULES}"


def test_a_target_with_no_ruleset_stays_in_legacy_mode(global_path: Path) -> None:
    """Exactly what a local machine with no ruleset gets: the defaults are NOT
    returned on their own, because empty is the signal for the allowlist gate."""
    cfg = _remote(global_path, FakeHost("/srv/app"))
    assert cfg.permission_rules == ()
    assert cfg.permission_source == ""
    assert cfg.warnings == ()


# -- [approval]: the engine owns policy wholesale ------------------------------
#
# This is the section the remote-executor wave inverted. The /config wave read
# [approval] from THIS PC's config.toml alone whenever a host was given, and
# warned about a remote project that tried to set it - remote-ssh.md's "the
# target owns the rules, the host owns the gate" split. That split is superseded
# by docs/design/remote-executor.md section 2.5: policy is the ENGINE's, and the
# engine's machine is the target, so [approval] merges like every other table
# and the warning is gone with the branch that raised it.


def test_the_remote_projects_approval_table_takes_effect(global_path: Path) -> None:
    """The engine owns policy wholesale (remote-executor.md section 2.5): the
    target's project file sets the mode and yolo for the session it describes,
    and nothing about [approval] is special-cased any more."""
    global_path.write_text('[approval]\nmode = "plan"\n', encoding="utf-8")
    host = FakeHost("/srv/app")
    host.add_file(
        "/srv/app/.agentclip.toml",
        '[approval]\nyolo = true\nmode = "unattended"\n[general]\nservice = "claude"\n',
    )

    cfg = _remote(global_path, host)
    assert cfg.approval.yolo is True
    assert cfg.approval.mode == "unattended"  # the TARGET's project file wins
    assert cfg.general.service == "claude"
    assert cfg.warnings == ()  # no pinning, so nothing to warn about


def test_the_remote_projects_command_rules_take_effect(global_path: Path) -> None:
    """The whole table, not just the two live-state keys: the legacy allowlist
    and the deny-token backstop come off the target too."""
    global_path.write_text(
        '[approval]\ncommand_allowlist = ["ls*"]\ncommand_deny_tokens = [";"]\n',
        encoding="utf-8",
    )
    host = FakeHost("/srv/app")
    host.add_file(
        "/srv/app/.agentclip.toml",
        '[approval]\ncommand_allowlist = ["pytest*"]\n',
    )

    cfg = _remote(global_path, host)
    # Lists REPLACE, never concatenate - the project tightens rather than adds.
    assert cfg.approval.command_allowlist == ("pytest*",)
    # A key the project did not mention still merges down from this PC's file.
    assert cfg.approval.command_deny_tokens == (";",)


def test_the_global_approval_table_still_arms_a_remote_session(global_path: Path) -> None:
    """A target with no [approval] of its own: this PC's config.toml is the only
    layer left, exactly as it is for every other table."""
    global_path.write_text(
        '[approval]\nyolo = true\ncommand_allowlist = ["ls*"]\n', encoding="utf-8"
    )
    cfg = _remote(global_path, FakeHost("/srv/app"))
    assert cfg.approval.yolo is True
    assert cfg.approval.command_allowlist == ("ls*",)


def test_a_remote_project_setting_approval_is_not_warned_about(
    global_path: Path,
) -> None:
    """The pinning warning is gone with the branch: an [approval] table on the
    target is ordinary config now, and a warnings list is for problems."""
    host = FakeHost("/srv/app")
    host.add_file(
        "/srv/app/.agentclip.toml", '[approval]\nmode = "plan"\n[general]\nservice = "claude"\n'
    )
    cfg = _remote(global_path, host)
    assert cfg.warnings == ()
    assert cfg.approval.mode == "plan"


def test_a_local_project_still_supplies_its_own_approval_table(
    project: Path, global_path: Path
) -> None:
    """The local case, unchanged throughout: one machine, one merge."""
    (project / ".agentclip.toml").write_text(
        '[approval]\nyolo = true\nmode = "unattended"\n', encoding="utf-8"
    )
    cfg = _load(project, global_path)
    assert cfg.approval.yolo is True
    assert cfg.approval.mode == "unattended"
    assert cfg.warnings == ()
