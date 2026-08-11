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
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentclip.hosts.ssh import CONNECTION_LOST_EXIT, SshHost

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
    host.mkdir(path)
    yield path
    host.run_blocking(f"rm -rf {path.as_posix()}", timeout=30)


def test_probe_reports_the_remote_kernel(host: SshHost) -> None:
    assert host.os_name.endswith("(ssh)")


def test_a_command_runs_in_the_workspace_root(host: SshHost, workdir: Path) -> None:
    result = host.spawn("pwd", workdir).wait(30)
    assert result is not None and result.exit_code == 0
    assert result.output.strip().endswith(workdir.name)


def test_stdout_and_stderr_arrive_interleaved_in_one_stream(
    host: SshHost, workdir: Path
) -> None:
    result = host.spawn("echo out; echo err >&2; exit 7", workdir).wait(30)
    assert result is not None and result.exit_code == 7
    assert "out" in result.output and "err" in result.output


def test_a_login_shell_sources_the_profile(host: SshHost, workdir: Path) -> None:
    result = host.spawn("echo $HOME", workdir).wait(30)
    assert result is not None and result.output.strip().startswith("/")


def test_kill_reaps_the_whole_process_tree(host: SshHost, workdir: Path) -> None:
    """The grandchild is the real work; killing only the wrapper would orphan it."""
    marker = f"agentclip-victim-{uuid.uuid4().hex[:8]}"
    handle = host.spawn(f"bash -c 'sleep 300 # {marker}'", workdir)
    assert handle.wait(2.0) is None  # still running
    handle.kill()
    handle.drain(5.0)

    code, out = host.run_blocking(f"pgrep -f {marker} | wc -l", timeout=30)
    assert code == 0
    assert out.strip().splitlines()[-1].strip() == "0"


def test_files_round_trip_over_sftp(host: SshHost, workdir: Path) -> None:
    target = workdir / "deep" / "nested" / "file.txt"
    host.write_bytes(target, b"hello\n")
    assert host.read_bytes(target) == b"hello\n"
    assert host.read_bytes(target, max_bytes=2) == b"he"

    host.write_bytes(target, b"more\n", append=True)
    assert host.read_bytes(target) == b"hello\nmore\n"

    st = host.stat(target)
    assert st is not None and st.is_file and st.size == 11

    names = {e.name for e in host.listdir(workdir / "deep")}
    assert names == {"nested"}

    host.delete(target)
    assert host.stat(target) is None


def test_realpath_resolves_a_symlink_and_tolerates_a_missing_tail(
    host: SshHost, workdir: Path
) -> None:
    host.mkdir(workdir / "real")
    host.run_blocking(f"ln -s {(workdir / 'real').as_posix()} {(workdir / 'link').as_posix()}")
    resolved = host.realpath(workdir / "link" / "not-yet.txt")
    assert resolved.as_posix().endswith("/real/not-yet.txt")


def test_a_lost_connection_reports_an_unknown_outcome(host: SshHost, workdir: Path) -> None:
    """Pull the link out from under a running command; nobody may claim to know."""
    handle = host.spawn("sleep 30", workdir)
    assert handle.wait(1.0) is None
    host.close()  # the link dies while the command is in flight
    result = handle.wait(5.0)
    assert result is not None
    assert result.exit_code == CONNECTION_LOST_EXIT
    assert "outcome unknown" in result.output

    # ...and the next operation transparently re-dials.
    assert host.stat(workdir) is not None
    assert host.reconnects >= 1
