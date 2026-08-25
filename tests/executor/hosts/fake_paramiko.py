"""A paramiko stand-in: an SSH server that is a dict, and channels that are lists.

Only the surface :mod:`agentclip.executor.hosts.ssh` actually touches is modelled -
SSHClient/Transport/Channel/SFTPClient - and the exception types stay the REAL
paramiko ones, because that is what the code catches. Nothing here opens a
socket, so the unit tests cost nothing and can run anywhere.
"""

from __future__ import annotations

import errno
import queue
import stat as stat_module
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import paramiko

BROKEN = "the connection is broken"


class FakeAttr:
    """paramiko.SFTPAttributes: what listdir_attr/stat/lstat answer with."""

    def __init__(self, filename: str, mode: int, size: int = 0) -> None:
        self.filename = filename
        self.st_mode = mode
        self.st_size = size


class FakeFile:
    """The handle sftp.open() returns; reads honour the size argument."""

    def __init__(self, fs: FakeFs, remote: str, mode: str) -> None:
        self._fs = fs
        self._remote = remote
        self._mode = mode
        self._pos = 0
        if "w" in mode:
            fs.files[remote] = b""

    def read(self, size: int | None = None) -> bytes:
        data = self._fs.files.get(self._remote, b"")[self._pos :]
        taken = data if size is None else data[:size]
        self._pos += len(taken)
        self._fs.reads.append((self._remote, size))
        return taken

    def write(self, data: bytes) -> None:
        self._fs.files[self._remote] = self._fs.files.get(self._remote, b"") + data

    def prefetch(self, file_size: int | None = None) -> None:
        self._fs.prefetched.append(self._remote)

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class FakeFs:
    """The remote filesystem: two dicts and a symlink table."""

    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/"})
    links: dict[str, str] = field(default_factory=dict)  # link -> absolute target
    reads: list[tuple[str, int | None]] = field(default_factory=list)
    prefetched: list[str] = field(default_factory=list)
    made: list[str] = field(default_factory=list)

    def add_file(self, remote: str, content: bytes | str = b"") -> None:
        self.files[remote] = content.encode() if isinstance(content, str) else content
        self.add_dir(remote.rsplit("/", 1)[0])

    def add_dir(self, remote: str) -> None:
        while remote:
            self.dirs.add(remote)
            remote = remote.rsplit("/", 1)[0]

    def exists(self, remote: str) -> bool:
        return remote in self.files or remote in self.dirs or remote in self.links

    def resolve(self, remote: str) -> str:
        seen = remote
        for _ in range(10):
            if seen in self.links:
                seen = self.links[seen]
            else:
                break
        return seen


def _enoent(remote: str) -> OSError:
    return OSError(errno.ENOENT, "No such file", remote)


class FakeSftp:
    def __init__(self, fs: FakeFs, client: FakeSSHClient) -> None:
        self.fs = fs
        self._client = client
        self.closed = False

    def _guard(self) -> None:
        if self._client.broken:
            raise paramiko.SSHException(BROKEN)

    def open(self, remote: str, mode: str = "r") -> FakeFile:
        self._guard()
        if "r" in mode and remote not in self.fs.files:
            raise _enoent(remote)
        return FakeFile(self.fs, remote, mode)

    def stat(self, remote: str) -> FakeAttr:
        self._guard()
        target = self.fs.resolve(remote)
        if target in self.fs.dirs:
            return FakeAttr(target.rsplit("/", 1)[-1], stat_module.S_IFDIR | 0o755)
        if target in self.fs.files:
            return FakeAttr(
                target.rsplit("/", 1)[-1], stat_module.S_IFREG | 0o644, len(self.fs.files[target])
            )
        raise _enoent(remote)

    def lstat(self, remote: str) -> FakeAttr:
        self._guard()
        if remote in self.fs.links:
            return FakeAttr(remote.rsplit("/", 1)[-1], stat_module.S_IFLNK | 0o777)
        return self.stat(remote)

    def listdir_attr(self, remote: str) -> list[FakeAttr]:
        self._guard()
        if remote not in self.fs.dirs:
            raise _enoent(remote)
        prefix = remote.rstrip("/") + "/"
        out: list[FakeAttr] = []
        for name in sorted(
            {
                p[len(prefix) :].split("/")[0]
                for p in (*self.fs.files, *self.fs.dirs, *self.fs.links)
                if p.startswith(prefix) and p != remote
            }
        ):
            out.append(self.lstat(prefix + name))
            out[-1].filename = name
        return out

    def remove(self, remote: str) -> None:
        self._guard()
        if remote not in self.fs.files:
            raise _enoent(remote)
        del self.fs.files[remote]

    def mkdir(self, remote: str) -> None:
        self._guard()
        self.fs.made.append(remote)
        self.fs.dirs.add(remote)

    def rmdir(self, remote: str) -> None:
        self._guard()
        if remote not in self.fs.dirs:
            raise _enoent(remote)
        prefix = remote.rstrip("/") + "/"
        if any(p.startswith(prefix) for p in (*self.fs.files, *self.fs.dirs, *self.fs.links)):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", remote)
        self.fs.dirs.discard(remote)

    def normalize(self, remote: str) -> str:
        self._guard()
        if not self.fs.exists(remote):
            raise _enoent(remote)  # sftp-server's realpath(3), which needs the path
        return self.fs.resolve(remote)

    def close(self) -> None:
        self.closed = True


class FakeChannel:
    """One exec channel: scripted output and exit status, or a broken link.

    Two shapes, because ssh.py opens two kinds of channel. A per-call exec
    channel answers its whole output at once (``FakeCommandScript.output``) and
    is done. A LINK channel (``SshHost.open_link_channel``) is long-lived and
    duplex, so it is scripted as ``chunks``/``stderr_chunks``: each ``recv``
    hands back the next staged chunk and ``b""`` once they run out, which is
    exactly the boundary-splitting the reader has to survive. Whatever is
    written to it accumulates in ``sent``, since on a link channel the bytes
    going UP are half of what there is to assert about.
    """

    def __init__(self, script: FakeCommandScript, client: FakeSSHClient) -> None:
        self._script = script
        self._client = client
        self.command = ""
        self.combined = False
        self.closed = False
        self.sent = bytearray()
        self._sent = False
        self._chunk = 0
        self._stderr_chunk = 0

    def set_combine_stderr(self, value: bool) -> None:
        self.combined = value

    def exec_command(self, command: str) -> None:
        self.command = command
        self._client.commands.append(command)

    def recv_ready(self) -> bool:
        self._raise_if_broken()
        return not self._sent and bool(self._script.output)

    def recv_stderr_ready(self) -> bool:
        return False

    def recv(self, n: int) -> bytes:
        self._raise_if_broken()
        if self._script.chunks:  # a staged, long-lived stream
            return self._next(self._script.chunks, "_chunk")
        self._sent = True
        return self._script.output.encode()

    def recv_stderr(self, n: int) -> bytes:
        self._raise_if_broken()
        return self._next(self._script.stderr_chunks, "_stderr_chunk")

    def send(self, data: bytes) -> int:
        self._raise_if_broken()
        self.sent.extend(data)
        return len(data)

    def sendall(self, data: bytes) -> None:
        self.send(data)

    def exit_status_ready(self) -> bool:
        self._raise_if_broken()
        return not self._script.hangs

    def recv_exit_status(self) -> int:
        return self._script.exit_code

    def close(self) -> None:
        self.closed = True

    def _next(self, staged: list[bytes], cursor: str) -> bytes:
        """The next staged chunk, or ``b""`` (EOF) once they are exhausted."""
        index = getattr(self, cursor)
        if index >= len(staged):
            return b""
        setattr(self, cursor, index + 1)
        return staged[index]

    def _raise_if_broken(self) -> None:
        if self._client.broken or self._script.breaks:
            self._client.broken = True
            raise paramiko.SSHException(BROKEN)


@dataclass
class FakeCommandScript:
    exit_code: int = 0
    output: str = ""
    hangs: bool = False
    breaks: bool = False  # the link dies while the command is in flight
    # A link channel's two streams, one recv() per element. Empty means "this is
    # an ordinary exec channel"; see FakeChannel.
    chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)


class FakeTcpChannel:
    """A ``direct-tcpip`` channel: a blocking byte pipe, not a scripted command.

    The other fake channel answers a whole command at once and then EOFs, which
    is exactly wrong for a tunnel - the pump thread would see EOF before a local
    client ever connected. So this one BLOCKS in ``recv`` until a test stages
    bytes with :meth:`feed`, calls :meth:`feed_eof`, or closes it, which is what
    a real forwarded connection does while the far side is quiet.

    Whatever the tunnel writes accumulates in ``sent``; :meth:`wait_for_sent`
    is how a test waits for bytes that another thread is carrying.
    """

    def __init__(self, kind: str, dest: Any, origin: Any) -> None:
        self.kind = kind
        self.dest = dest
        self.origin = origin
        self.sent = bytearray()
        self.closed = False
        self._lock = threading.Lock()
        self._incoming: queue.Queue[bytes | None] = queue.Queue()
        self._left = b""

    # -- what a test drives it with -----------------------------------------

    def feed(self, data: bytes) -> None:
        """Bytes arriving FROM the far side, as if the destination wrote them."""
        self._incoming.put(data)

    def feed_eof(self) -> None:
        """The far side hung up."""
        self._incoming.put(None)

    def wait_for_sent(self, count: int, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.sent) >= count:
                    return bytes(self.sent)
            time.sleep(0.005)
        with self._lock:
            return bytes(self.sent)

    # -- paramiko.Channel surface -------------------------------------------

    def recv(self, n: int) -> bytes:
        if self._left:
            data, self._left = self._left[:n], self._left[n:]
            return data
        if self.closed:
            return b""
        item = self._incoming.get()  # blocks, like the real thing
        if item is None:
            return b""
        data, self._left = item[:n], item[n:]
        return data

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError("socket is closed")
        with self._lock:
            self.sent.extend(data)

    def send(self, data: bytes) -> int:
        self.sendall(data)
        return len(data)

    def settimeout(self, timeout: float | None) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self._incoming.put(None)  # wake whoever is blocked in recv()


class FakeTransport:
    def __init__(self, client: FakeSSHClient) -> None:
        self._client = client
        self.keepalive = 0

    def set_keepalive(self, seconds: int) -> None:
        self.keepalive = seconds

    def is_active(self) -> bool:
        return not self._client.broken

    def open_session(self, timeout: float | None = None) -> FakeChannel:
        if self._client.broken:
            raise paramiko.SSHException(BROKEN)
        command_index = len(self._client.commands)
        script = (
            self._client.scripts[command_index]
            if command_index < len(self._client.scripts)
            else FakeCommandScript()
        )
        chan = FakeChannel(script, self._client)
        self._client.channels.append(chan)
        return chan

    def open_channel(
        self,
        kind: str,
        dest_addr: Any = None,
        src_addr: Any = None,
        timeout: float | None = None,
    ) -> FakeTcpChannel:
        if self._client.broken:
            raise paramiko.SSHException(BROKEN)
        if FakeSSHClient.open_channel_error is not None:
            raise FakeSSHClient.open_channel_error
        chan = FakeTcpChannel(kind, dest_addr, src_addr)
        self._client.tcp_channels.append(chan)
        return chan


class FakeSSHClient:
    """paramiko.SSHClient with the socket removed.

    Class-level ``connects``/``instances`` record every dial across the whole
    test, which is how the reconnect tests see a re-dial happen.
    """

    instances: list[FakeSSHClient] = []
    connects: list[dict[str, Any]] = []
    fs = FakeFs()
    scripts: list[FakeCommandScript] = []
    # Exceptions to raise from connect(), consumed one per call.
    connect_errors: list[BaseException | None] = []
    host_keys_seen: list[str] = []
    # Raised by transport.open_channel() when set: how a tunnel's open failure
    # is injected, either as a refusal or as a dead transport.
    open_channel_error: BaseException | None = None

    def __init__(self) -> None:
        self.broken = False
        self.commands: list[str] = []
        self.channels: list[FakeChannel] = []
        self.tcp_channels: list[FakeTcpChannel] = []
        self.policy: paramiko.MissingHostKeyPolicy | None = None
        self.sftp: FakeSftp | None = None
        self._transport: FakeTransport | None = None
        FakeSSHClient.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.connects = []
        cls.fs = FakeFs()
        cls.scripts = []
        cls.connect_errors = []
        cls.host_keys_seen = []
        cls.open_channel_error = None

    # -- paramiko.SSHClient surface -----------------------------------------

    def load_system_host_keys(self) -> None:
        pass

    def load_host_keys(self, path: str) -> None:
        FakeSSHClient.host_keys_seen.append(path)

    def set_missing_host_key_policy(self, policy: paramiko.MissingHostKeyPolicy) -> None:
        self.policy = policy

    def connect(self, **kwargs: Any) -> None:
        FakeSSHClient.connects.append(kwargs)
        if FakeSSHClient.connect_errors:
            error = FakeSSHClient.connect_errors.pop(0)
            if error is not None:
                raise error
        self._transport = FakeTransport(self)

    def get_transport(self) -> FakeTransport | None:
        return self._transport

    def open_sftp(self) -> FakeSftp:
        if self.broken:
            raise paramiko.SSHException(BROKEN)
        self.sftp = FakeSftp(FakeSSHClient.fs, self)
        return self.sftp

    def close(self) -> None:
        self._transport = None
