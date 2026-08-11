"""The [remote] table, the --ssh/--remote-root flags, and where a project's
own config is read from in a remote session (docs/design/remote-ssh.md 4, 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.config import RemoteTarget, load_config
from agentclip.hosts import FakeHost

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


def test_permissions_are_never_read_through_the_host(
    global_path: Path, tmp_path: Path
) -> None:
    """The user's policy must not weaken because of a remote file (design 6)."""
    opencode = tmp_path / "opencode.json"
    opencode.write_text('{"permission": {"bash": "deny"}}', encoding="utf-8")
    global_path.write_text(
        f'[permission]\nopencode_config = "{opencode.as_posix()}"\n', encoding="utf-8"
    )
    host = FakeHost("/srv/app")
    # A remote file at the same path would be a policy the operator never chose.
    host.add_file(opencode.as_posix(), '{"permission": {"bash": "allow"}}')

    cfg = load_config(Path("/srv/app"), global_config_path=global_path, host=host)
    assert cfg.permission_source == str(opencode)
    assert cfg.permission_rules[-1].action == "deny"
