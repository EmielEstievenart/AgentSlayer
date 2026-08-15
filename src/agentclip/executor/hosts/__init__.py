"""Host: the OS seam every tool runs against (this PC, or a machine over SSH).

See :mod:`agentclip.executor.hosts.base` for the contract. A leaf: it imports nothing
else from agentclip, so any layer may depend on it.

:mod:`agentclip.executor.hosts.ssh` is deliberately NOT re-exported here - importing it
pulls in paramiko and its crypto stack, which a local session has no use for.
Remote sessions import it by name (cli.py, once, at launch).
"""

from agentclip.executor.hosts.base import DirEntry, ExecHandle, ExecResult, FileStat, Host
from agentclip.executor.hosts.fake import FakeCommand, FakeExec, FakeHost
from agentclip.executor.hosts.local import LocalExec, LocalHost

__all__ = [
    "DirEntry",
    "ExecHandle",
    "ExecResult",
    "FakeCommand",
    "FakeExec",
    "FakeHost",
    "FileStat",
    "Host",
    "LocalExec",
    "LocalHost",
]
