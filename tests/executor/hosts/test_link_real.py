"""The link channel against a REAL machine, with a REAL engine on it. Opt in.

Same gate as tests/executor/hosts/test_ssh_real.py, and the same rule behind it:
this touches somebody's actual server, so it never runs by accident. Opt in
with::

    $env:AGENTCLIP_SSH_TESTS = '1'
    $env:AGENTCLIP_SSH_TARGET = 'user@host'          # or an ~/.ssh/config alias
    $env:AGENTCLIP_SSH_ROOT = '/tmp/agentclip-it'    # optional; default below

**This file needs more of the target than test_ssh_real.py does**: ``agentclip``
must be INSTALLED over there (``uv tool install agentclip``, pipx, or pip into an
environment on PATH), so that ``agentclip-engine`` is on the login shell's PATH -
that is the deployment model itself (docs/design/remote-executor.md §2.6), and
this is the only test that can prove it works end to end. The target must also
authenticate without a prompt and already be in known_hosts, since an unattended
run has nobody to ask.

There is deliberately no "not installed" variant here. Making a target that HAS
the engine temporarily not have it means editing somebody's PATH, and the failure
it would prove is already pinned without a network in
tests/executor/hosts/test_link_channel.py and tests/shell/app/test_engine_launch.py.

**Since increment 4's flip this is the path a real ``--ssh`` takes**, not an
opt-in one beside it (docs/design/remote-executor.md §2.12). Nothing here had to
change for that - what these two prove is exactly what the flip made the default:
one exec channel, a handshake, a session and a turn that all happen on the target,
and a process that dies with the channel. The assembly ABOVE them - which shell
calls the factory, and what it closes on the way out - is pinned without a network
in tests/test_launch_remote.py.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator

import pytest

from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.hosts.ssh import LinkChannel, SshHost
from agentclip.shell.app.engine_launch import engine_command
from agentclip.shell.app.remote_link import RemoteLinkClient

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
def link(host: SshHost) -> Iterator[LinkChannel]:
    """One ``agentclip-engine`` on the far end, for the length of one test."""
    root = f"{SSH_ROOT}/{uuid.uuid4().hex[:8]}"
    host.run_blocking(f"mkdir -p {root}", timeout=30)
    channel = host.open_link_channel(engine_command(root))
    yield channel
    channel.close()
    host.run_blocking(f"rm -rf {root}", timeout=30)


def test_a_session_runs_on_the_target_over_one_exec_channel(link: LinkChannel) -> None:
    """The whole transport: launch, handshake, a session, a first turn."""
    client = RemoteLinkClient(link.reader, link.writer)
    server_id = client.hello()
    assert server_id, f"no handshake; the target said: {link.stderr_tail()}"
    assert client.server_package

    remote = client.build_session(EngineRequest(service="claude", chat_name="amber-falcon"))
    assert remote.chat_name == "amber-falcon"
    assert remote.role == "master"

    outbound = asyncio.run(remote.start_task("say hello"))
    assert outbound.text, f"empty bootstrap; the target said: {link.stderr_tail()}"


def test_closing_the_channel_stops_the_engine(host: SshHost) -> None:
    """§2.3: the remote process dies with the channel - no detached daemon."""
    root = f"{SSH_ROOT}/{uuid.uuid4().hex[:8]}"
    host.run_blocking(f"mkdir -p {root}", timeout=30)
    channel = host.open_link_channel(engine_command(root))
    RemoteLinkClient(channel.reader, channel.writer).hello()
    channel.close()

    code, out = host.run_blocking(
        f"pgrep -f 'agentclip-engine --project {root}' | wc -l", timeout=30
    )
    assert code == 0
    assert out.strip().splitlines()[-1].strip() == "0"
    host.run_blocking(f"rm -rf {root}", timeout=30)
