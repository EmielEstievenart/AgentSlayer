"""SshHost against a REAL machine. Skipped unless you opt in.

Same rule as the ``real_os`` gate (tests/conftest.py): these tests touch
something outside this repository - here, somebody's actual server - so they
never run by accident and never run without the user's explicit go-ahead. Opt
in with::

    $env:AGENTCLIP_SSH_TESTS = '1'
    $env:AGENTCLIP_SSH_TARGET = 'user@host'      # or an ~/.ssh/config alias
    $env:AGENTCLIP_SSH_ROOT = '/tmp/agentclip-it'  # optional; default below

The target must authenticate without a prompt (agent or key) and its host key
must already be in known_hosts - an unattended run has nobody to ask. Every file
these tests write lands under AGENTCLIP_SSH_ROOT, which they create and clean up.

Much smaller than it was: remote-executor.md §2.8 deleted the per-call tool
path, so there is no ``spawn``, no kill-tree and no write side to prove against a
real box. What is left is the connection and the connect sequence's own probes
and reads - plus tests/executor/hosts/test_link_real.py, which proves the engine
launch on the same target.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentclip.executor.hosts.ssh import SshHost

SSH_TESTS_ENABLED = os.environ.get("AGENTCLIP_SSH_TESTS") == "1"
SSH_TARGET = os.environ.get("AGENTCLIP_SSH_TARGET", "")
SSH_ROOT = os.environ.get("AGENTCLIP_SSH_ROOT", "/tmp/agentclip-integration")

pytestmark = [
    pytest.mark.real_ssh,
    pytest.mark.skipif(
        not (SSH_TESTS_ENABLED and SSH_TARGET),
        reason="needs AGENTCLIP_SSH_TESTS=1 and AGENTCLIP_SSH_TARGET",
    ),
]


@pytest.fixture(scope="module")
def host() -> Iterator[SshHost]:
    user, _, hostname = SSH_TARGET.rpartition("@")
    remote = SshHost(hostname, user=user)
    remote.connect()
    remote.probe_os()
    yield remote
    remote.close()


@pytest.fixture
def workdir(host: SshHost) -> Iterator[Path]:
    """A throwaway directory on the far end, removed afterwards."""
    path = Path(f"{SSH_ROOT}/{uuid.uuid4().hex[:8]}")
    host.probe_command(f"mkdir -p {path.as_posix()}", timeout=30)
    yield path
    host.probe_command(f"rm -rf {path.as_posix()}", timeout=30)


def test_probe_reports_the_remote_kernel(host: SshHost) -> None:
    assert host.os_name.endswith("(ssh)")


def test_a_probe_runs_through_the_login_shell(host: SshHost) -> None:
    """What makes ``printenv`` mean the login environment (connect step 5)."""
    code, out = host.probe_command("echo $HOME", timeout=30)
    assert code == 0
    assert out.strip().splitlines()[-1].strip().startswith("/")


def test_home_dir_is_the_remote_users(host: SshHost) -> None:
    """Where a remote session looks for the global skill folders."""
    code, out = host.probe_command("echo $HOME", timeout=30)
    assert code == 0
    assert host.home_dir().as_posix() == out.strip().splitlines()[-1].strip()


def test_the_connect_sequences_reads_work_over_sftp(host: SshHost, workdir: Path) -> None:
    """Steps 4 and 6: check the remote root, then read the target's config off it."""
    target = workdir / "config.toml"
    host.probe_command(f"echo hi > {target.as_posix()}", timeout=30)

    assert host.read_bytes(target) == b"hi\n"
    assert host.read_bytes(target, max_bytes=1) == b"h"

    st = host.stat(target)
    assert st is not None and st.is_file and st.size == 3
    assert host.stat(workdir / "not-there") is None
    assert host.realpath(workdir, strict=True).as_posix().endswith(workdir.name)


def test_realpath_resolves_a_symlink_and_tolerates_a_missing_tail(
    host: SshHost, workdir: Path
) -> None:
    host.probe_command(f"mkdir -p {(workdir / 'real').as_posix()}", timeout=30)
    host.probe_command(
        f"ln -s {(workdir / 'real').as_posix()} {(workdir / 'link').as_posix()}", timeout=30
    )
    resolved = host.realpath(workdir / "link" / "not-yet.txt")
    assert resolved.as_posix().endswith("/real/not-yet.txt")


def test_a_dead_link_is_re_dialled_by_the_next_operation(host: SshHost, workdir: Path) -> None:
    """The two-state machine, against a real transport."""
    before = host.reconnects
    host.close()
    assert host.stat(workdir) is not None  # LIVE again, transparently
    assert host.reconnects > before
