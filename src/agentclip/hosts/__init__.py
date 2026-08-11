"""Host: the OS seam every tool runs against (local today, SSH tomorrow).

See :mod:`agentclip.hosts.base` for the contract. A stdlib-only leaf: it imports
nothing else from agentclip, so any layer may depend on it.
"""

from agentclip.hosts.base import DirEntry, ExecHandle, ExecResult, FileStat, Host
from agentclip.hosts.fake import FakeCommand, FakeExec, FakeHost
from agentclip.hosts.local import LocalExec, LocalHost

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
