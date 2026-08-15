"""SshHost: the Host seam over a remote machine, via Paramiko.

One persistent authenticated connection per session (docs/design/remote-ssh.md
decisions 2, 5, 7, 8). Every command gets a fresh exec channel on it -
``bash -lc`` from the workspace root, so profile/rc files are sourced exactly as
a native CLI agent has them and nothing carries between commands. Files go over
one SFTP session on the same connection.

**Paths.** The Host protocol speaks :class:`pathlib.Path`, and on Windows that
is a ``WindowsPath`` whose ``str()`` would hand the server backslashes. So every
path arriving here is normalized to a POSIX string (:func:`_posix`) before it
reaches the wire, and every path handed back is built from one. Nothing above
the seam has to know: ``joinpath``/``relative_to``/``parts``/``as_posix`` are
lexical operations a WindowsPath performs correctly on a POSIX-shaped path. Two
consequences worth naming: a remote file name containing a literal backslash
(legal on Linux) would be split into components, and Windows path comparison is
case-insensitive, so two remote paths differing only in case compare equal above
the seam. Both are accepted - the alternative is a parallel path type through
every tool - and neither can widen the sandbox jail, whose containment check
only ever gets *more* eager.

**Connection loss** (design 5) is a two-state machine::

    LIVE --(transport error)--> DEAD --(next operation calls _ensure)--> LIVE

A command in flight when the link dies is never retried and never reported as
failed: it comes back as an :class:`ExecResult` carrying
:data:`CONNECTION_LOST_EXIT` and a body saying the outcome is unknown, because
it may have completed, half-completed, or still be running. The host marks
itself dead; the next operation transparently re-dials and re-authenticates
(with the credentials that already worked, so a reconnect under a live TUI needs
no prompt it could not show).

**Kill-tree** (design 8): closing a channel kills nothing remote. The wrapper
records its own PID and runs under ``setsid`` where that exists, so it is a
session leader and its PID is the process group of everything the command
spawns; :meth:`SshExec.kill` reads that PID over SFTP and sends
``kill -9 -- -<pgid>`` down a separate channel - the remote twin of LocalExec's
killpg.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import shlex
import stat as stat_module
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from types import TracebackType

import paramiko

from agentclip.executor.hosts.base import DirEntry, ExecResult, FileStat

# The exit code an unfinished command gets when the link died under it: what
# ssh(1) itself uses for "the transport failed", and no shell produces it for
# anything else in practice.
CONNECTION_LOST_EXIT = 255

_KEEPALIVE_S = 30
_CONNECT_TIMEOUT_S = 20
_PASSWORD_ATTEMPTS = 3
_POLL_S = 0.02
_RECV_CHUNK = 65536

# Prompt callbacks the caller supplies. Both are answered BEFORE the TUI starts
# on the first connect (cli.py wires them to the terminal); a re-dial calls them
# again only if the credentials that worked no longer do.
PasswordPrompt = Callable[[str], str | None]  # prompt -> secret; None/"" = give up
HostKeyPrompt = Callable[[str, str, str], bool]  # host, key type, fingerprint -> trust?
# title, instructions, ((prompt, echo), ...) -> one answer per prompt; None = give up.
# Paramiko's own ``auth_interactive`` handler contract, which is the shape a TOTP
# challenge arrives in. Held, not yet called - see _authenticate.
KeyboardPrompt = Callable[[str, str, Sequence[tuple[str, bool]]], list[str] | None]


class SshError(Exception):
    """A connection, authentication or host-key failure. Fatal at launch."""


def _posix(path: Path | str) -> str:
    """The remote spelling of a path: forward slashes, whatever built it."""
    text = str(path).replace("\\", "/")
    return text or "."


def _parent(remote: str) -> str:
    return str(PurePosixPath(remote).parent)


def _as_oserror(exc: OSError, remote: str) -> OSError:
    """Re-raise an SFTP failure as the OSError subclass callers already catch.

    Paramiko answers a missing file with a bare ``IOError(ENOENT, ...)``, which
    is an OSError but NOT a FileNotFoundError - and code above the seam catches
    the subclasses. Rebuilding it through the three-argument constructor gets
    the right class back, filename included.
    """
    if exc.errno:
        return OSError(exc.errno, exc.strerror or str(exc), remote)
    return OSError(errno.EIO, str(exc) or exc.__class__.__name__, remote)


def _fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH's SHA256 fingerprint - the string a user can compare by eye."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def wrap_command(command: str, cwd: str, pidfile: str) -> str:
    """The line actually sent: record the process group, cd, run.

    ``setsid`` puts the wrapper in a session of its own, so its PID *is* the
    process group id of every descendant, and ``--wait`` keeps the command's
    exit status as the channel's. Neither is assumed (busybox has one and not
    the other): both are probed by running them, and the fallback still records
    a PID, so kill() degrades to killing what it can reach rather than not
    existing. The trap clears the pidfile without disturbing the exit status.

    The command is NOT ``exec``'d: a compound command (``a && b``) is not a
    simple command, and exec would run only its head.
    """
    inner = (
        f"trap 'rm -f {pidfile}' EXIT; "
        f"echo $$ >{pidfile} 2>/dev/null; "
        f"cd {shlex.quote(cwd)} || exit 1; "
        f"{command}"
    )
    quoted = shlex.quote(inner)
    return (
        f"setsid --wait true >/dev/null 2>&1 && exec setsid --wait bash -lc {quoted}; "
        f"exec bash -lc {quoted}"
    )


class _AskPolicy(paramiko.MissingHostKeyPolicy):
    """known_hosts, with a question where AutoAddPolicy has a shrug (design 7)."""

    def __init__(self, confirm: HostKeyPrompt) -> None:
        self._confirm = confirm

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        if not self._confirm(hostname, key.get_name(), _fingerprint(key)):
            raise SshError(f"the host key for {hostname} was not accepted")
        client.get_host_keys().add(hostname, key.get_name(), key)


class SshExec:
    """One remote command on its own channel, behind the ExecHandle contract."""

    __slots__ = ("_host", "_chan", "_pidfile", "_buffer", "_result")

    def __init__(self, host: SshHost, chan: paramiko.Channel, pidfile: str) -> None:
        self._host = host
        self._chan = chan
        self._pidfile = pidfile
        self._buffer = ""
        self._result: ExecResult | None = None

    def wait(self, timeout: float) -> ExecResult | None:
        if self._result is not None:
            return self._result  # finished (or lost) earlier: the answer stands
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._pump()
                if self._chan.exit_status_ready() and not self._readable():
                    self._pump()  # anything that arrived behind the status
                    code = self._chan.recv_exit_status()
                    self._result = ExecResult(exit_code=code, output=self._buffer)
                    self._close()
                    return self._result
            except (OSError, EOFError, paramiko.SSHException) as exc:
                return self._connection_lost(exc)
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))

    def peek(self) -> str:
        """The merged output so far, as of the last :meth:`wait`/:meth:`drain`.

        No transport call of its own: the channel is pumped by the polling loop
        that already calls ``wait()`` every slice, and touching it from here
        would mean a second place that can discover a dead link. So a remote
        command streams at exactly the polling loop's resolution, which is the
        same resolution the UI redraws at.
        """
        return self._buffer

    def kill(self) -> None:
        """Kill the remote process group, best effort, never raising."""
        pgid = ""
        # Gone already, or the link died: then the channel close below is all
        # there is, and kill() still may not raise.
        with suppress(OSError):
            pgid = self._host.read_bytes(Path(self._pidfile)).decode("ascii", "replace").strip()
        if pgid.isdigit():
            self._host.run_detached(f"kill -9 -- -{pgid} 2>/dev/null; kill -9 {pgid} 2>/dev/null")
        self._close()

    def drain(self, timeout: float) -> str:
        """Whatever the command managed to emit before it died, best effort."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._pump()
                if not self._readable():
                    break
            except (OSError, EOFError, paramiko.SSHException):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S)
        return self._buffer

    # -- internals ----------------------------------------------------------

    def _readable(self) -> bool:
        return bool(self._chan.recv_ready() or self._chan.recv_stderr_ready())

    def _pump(self) -> None:
        """Move every byte currently available into the buffer; never blocks."""
        while self._chan.recv_ready():
            data = self._chan.recv(_RECV_CHUNK)
            if not data:
                break
            self._buffer += data.decode("utf-8", errors="replace")
        # set_combine_stderr should make the second loop dead code; a server that
        # ignores it must not be able to lose the error output.
        while self._chan.recv_stderr_ready():
            data = self._chan.recv_stderr(_RECV_CHUNK)
            if not data:
                break
            self._buffer += data.decode("utf-8", errors="replace")

    def _connection_lost(self, exc: Exception) -> ExecResult:
        """The honest answer to "did it run?" when the link died: nobody knows."""
        self._host.mark_dead(exc)
        note = (
            f"connection lost to {self._host.target}; command outcome unknown"
            " (it may have completed or still be running)"
        )
        body = f"{self._buffer}\n{note}" if self._buffer else note
        self._result = ExecResult(exit_code=CONNECTION_LOST_EXIT, output=body)
        return self._result

    def _close(self) -> None:
        with suppress(Exception):  # closing a dead channel must never raise
            self._chan.close()


class SshHost:
    """The Host implementation for a machine reached over SSH."""

    case_sensitive = True  # every OS worth reaching this way

    def __init__(
        self,
        hostname: str,
        *,
        user: str = "",
        port: int = 0,
        password_prompt: PasswordPrompt | None = None,
        host_key_prompt: HostKeyPrompt | None = None,
        keyboard_prompt: KeyboardPrompt | None = None,
        ssh_config_path: Path | None = None,
        known_hosts_path: Path | None = None,
    ) -> None:
        self._alias = hostname
        self._user = user
        self._port = port
        self._password_prompt = password_prompt
        self._host_key_prompt = host_key_prompt
        self._keyboard_prompt = keyboard_prompt
        self._ssh_config_path = (
            ssh_config_path if ssh_config_path is not None else Path.home() / ".ssh" / "config"
        )
        self._known_hosts_path = (
            known_hosts_path
            if known_hosts_path is not None
            else Path.home() / ".ssh" / "known_hosts"
        )
        # Filled in from ~/.ssh/config when we dial; the alias until then.
        self._resolved_host = hostname
        self._resolved_user = user
        self._resolved_port = port or 22
        self._identity_files: list[str] = []
        self._password: str | None = None  # what worked, so a re-dial can be silent
        self._client: paramiko.SSHClient | None = None
        self._sftp_client: paramiko.SFTPClient | None = None
        # Re-entrant: an operation takes the lock and may reach _ensure under it.
        self._lock = threading.RLock()
        self.name = f"ssh:{hostname}"  # diagnostics
        self.os_name = "remote"  # replaced by probe(): the bootstrap's "on {os}" slot
        self.reconnects = 0  # how often the link had to be re-dialed
        self.last_error: BaseException | None = None
        self._dialed = False  # has a connection ever been established?

    # -- identity ------------------------------------------------------------

    @property
    def target(self) -> str:
        """How this machine is named in what the user (and the model) reads."""
        user = self._resolved_user or self._user
        base = f"{user}@{self._resolved_host}" if user else self._resolved_host
        return base if self._resolved_port == 22 else f"{base}:{self._resolved_port}"

    @property
    def connected(self) -> bool:
        return self._client is not None

    # -- the connection state machine ---------------------------------------

    def connect(self) -> None:
        """Dial and authenticate. Raises SshError - call it before the TUI starts."""
        with self._lock:
            self._connect_locked()

    def close(self) -> None:
        with self._lock:
            self._drop_locked()

    def reconnect(self) -> bool:
        """Re-dial NOW rather than on the next operation. Never raises.

        The reconnect model is lazy and stays lazy (design 5): nothing here
        polls, and a dead link is discovered by the operation that needed it.
        This is the same ``_ensure`` that operation would have called, exposed
        so a standing UI can offer "reconnect now" without reaching into a
        private method or opening a second dial path with its own bugs
        (docs/design/gui.md §4 ruling 5). On a live link it is a no-op; on a
        dead one it costs exactly what the next command would have cost.
        """
        try:
            self._ensure()
        except (SshError, OSError, paramiko.SSHException) as exc:
            self.last_error = exc
            return False
        return True

    def mark_dead(self, exc: BaseException | None = None) -> None:
        """The link is gone: drop it, so the next operation re-dials (design 5)."""
        with self._lock:
            self.last_error = exc if exc is not None else self.last_error
            self._drop_locked()

    def _drop_locked(self) -> None:
        for closeable in (self._sftp_client, self._client):
            if closeable is not None:
                with suppress(Exception):  # tearing down something already broken
                    closeable.close()
        self._sftp_client = None
        self._client = None

    def _ensure(self) -> paramiko.SSHClient:
        """The live client, re-dialing and re-authenticating if it went away."""
        with self._lock:
            if self._client is None:
                if self._dialed:  # the first dial of all is not a re-dial
                    self.reconnects += 1
                self._connect_locked()
            client = self._client
            assert client is not None
            return client

    def _check_alive(self) -> None:
        """Mark the host dead when the transport underneath has stopped working.

        Asked after any failure rather than guessed from the exception type:
        paramiko reports a dead link and a missing file through the same
        IOError, and the transport itself is the one thing that knows.
        """
        with self._lock:
            client = self._client
            if client is None:
                return
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                self._drop_locked()

    def _connect_locked(self) -> None:
        self._read_ssh_config()
        client = paramiko.SSHClient()
        with suppress(OSError):
            client.load_system_host_keys()
        # No known_hosts file yet is fine: the policy below still asks before
        # trusting anything, and accepting writes the key into this object.
        with suppress(OSError):
            client.load_host_keys(str(self._known_hosts_path))
        client.set_missing_host_key_policy(_AskPolicy(self._confirm_host_key))
        self._authenticate(client)
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(_KEEPALIVE_S)
        self._client = client
        self._sftp_client = None
        self._dialed = True

    def _read_ssh_config(self) -> None:
        """Resolve the target through ~/.ssh/config, so aliases work (design 7)."""
        config = paramiko.SSHConfig()
        try:
            with open(self._ssh_config_path, encoding="utf-8", errors="replace") as f:
                config.parse(f)
        except OSError:
            pass
        entry = config.lookup(self._alias)
        self._resolved_host = str(entry.get("hostname") or self._alias)
        self._resolved_user = self._user or str(entry.get("user") or "")
        port = entry.get("port")
        self._resolved_port = self._port or (int(port) if port else 22)
        self._identity_files = [str(Path(p).expanduser()) for p in entry.get("identityfile") or []]

    def _authenticate(self, client: paramiko.SSHClient) -> None:
        """Agent and keys first; an interactive secret only once they are refused.

        **Keyboard-interactive/2FA is still paramiko's own fallback, and still
        untested** (docs/design/remote-ssh.md, "Auth, in practice"). When a
        server refuses the password and offers ``keyboard-interactive``,
        ``SSHClient._auth`` drops to ``Transport.auth_interactive_dumb``
        (paramiko ``client.py:811``), whose default handler prints the prompts
        and reads ``input()`` from stdin - which works in a bare shell and
        nowhere else. ``self._keyboard_prompt`` is the seam that fixes it
        (``ssh-connect.md`` §3.7 designs the dialog against paramiko's handler
        contract, and ``gui.md`` §4 ruling 4 caps it at three attempts), and it
        is deliberately NOT wired here: routing this path means bypassing
        ``client.connect`` for ``transport.auth_interactive``, i.e. rebuilding
        the auth flow, and there is no target in this suite that can prove the
        result. TODO, needing a real 2FA box: call the prompt from here instead
        of letting paramiko reach stdin.
        """
        common = {
            "hostname": self._resolved_host,
            "port": self._resolved_port,
            "username": self._resolved_user or None,
            "timeout": _CONNECT_TIMEOUT_S,
            "auth_timeout": _CONNECT_TIMEOUT_S,
        }
        try:
            client.connect(
                allow_agent=True,
                look_for_keys=True,
                key_filename=self._identity_files or None,
                **common,
            )
            return
        except paramiko.AuthenticationException as exc:
            failure: Exception = exc
        except (paramiko.SSHException, OSError) as exc:
            raise SshError(f"cannot reach {self.target}: {exc}") from exc

        # The password that already worked is tried first and never re-asked:
        # by the time a reconnect happens the terminal that could prompt is
        # underneath the TUI.
        secrets: list[str] = [self._password] if self._password else []
        for attempt in range(_PASSWORD_ATTEMPTS):
            if attempt >= len(secrets):
                if self._password_prompt is None:
                    break
                answer = self._password_prompt(f"password for {self.target}: ")
                if not answer:
                    break
                secrets.append(answer)
            try:
                client.connect(
                    allow_agent=False, look_for_keys=False, password=secrets[attempt], **common
                )
            except paramiko.AuthenticationException as exc:
                failure = exc
                continue
            except (paramiko.SSHException, OSError) as exc:
                raise SshError(f"cannot reach {self.target}: {exc}") from exc
            self._password = secrets[attempt]
            return
        raise SshError(f"authentication failed for {self.target}: {failure}")

    def _confirm_host_key(self, hostname: str, keytype: str, fingerprint: str) -> bool:
        if self._host_key_prompt is None:
            return False  # nobody to ask means no trust: never auto-add (design 7)
        return self._host_key_prompt(hostname, keytype, fingerprint)

    # -- launch-time probes --------------------------------------------------

    def probe_os(self) -> str:
        """``uname -s`` on the far end, as the bootstrap's OS name. Fails loudly."""
        code, out = self.run_blocking("uname -s", timeout=_CONNECT_TIMEOUT_S)
        kernel = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if code != 0 or not kernel:
            raise SshError(
                f"{self.target} did not answer 'uname -s' (exit {code}): {out.strip() or '(no output)'}"
            )
        self.os_name = f"{kernel} (ssh)"
        return self.os_name

    def home_dir(self) -> Path:
        """The remote user's home directory - where their skill folders live.

        SFTP starts in it, so normalizing "." is the question. Falls back to the
        POSIX convention rather than raising: a home that cannot be resolved
        means no global skills, not a failed launch.
        """
        try:
            with self._sftp(".") as sftp:
                return Path(sftp.normalize("."))
        except OSError:
            return Path(f"/home/{self._resolved_user}") if self._resolved_user else Path("/root")

    def run_blocking(self, command: str, *, timeout: float = 60.0) -> tuple[int, str]:
        """Run one command to completion. For launch-time probes, not for tools."""
        handle = self.spawn(command, Path("."))
        deadline = time.monotonic() + timeout
        while True:
            result = handle.wait(min(0.5, max(0.0, deadline - time.monotonic())))
            if result is not None:
                return result.exit_code, result.output
            if time.monotonic() >= deadline:
                handle.kill()
                return CONNECTION_LOST_EXIT, f"{command!r} timed out after {timeout:.0f}s"

    def run_detached(self, command: str) -> None:
        """Fire a command and forget it (the kill path). Never raises."""
        try:
            transport = self._ensure().get_transport()
            if transport is None:
                return
            chan = transport.open_session(timeout=_CONNECT_TIMEOUT_S)
            chan.exec_command(command)
            chan.close()
        except Exception:  # noqa: BLE001 - killing is best effort by contract
            pass

    # -- Host: process execution --------------------------------------------

    def spawn(self, command: str, cwd: Path) -> SshExec:
        client = self._ensure()
        pidfile = f"/tmp/.agentclip-{uuid.uuid4().hex}.pid"
        try:
            transport = client.get_transport()
            if transport is None:
                raise paramiko.SSHException("the transport is gone")
            chan = transport.open_session(timeout=_CONNECT_TIMEOUT_S)
            chan.set_combine_stderr(True)  # one merged stream, as LocalHost has it
            chan.exec_command(wrap_command(command, _posix(cwd), pidfile))
        except (paramiko.SSHException, OSError, EOFError) as exc:
            self.mark_dead(exc)
            raise OSError(errno.EIO, f"connection lost to {self.target}: {exc}") from exc
        return SshExec(self, chan, pidfile)

    # -- Host: files ---------------------------------------------------------

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        remote = _posix(path)
        with self._sftp(remote) as sftp, sftp.open(remote, "rb") as f:
            if max_bytes is not None:
                return bytes(f.read(max_bytes))  # a prefix, not the whole file
            f.prefetch()
            return bytes(f.read())

    def write_bytes(self, path: Path, data: bytes, *, append: bool = False) -> None:
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            _mkdirs(sftp, _parent(remote))
            with sftp.open(remote, "ab" if append else "wb") as f:
                f.write(data)

    def delete(self, path: Path) -> None:
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            sftp.remove(remote)

    # -- Host: directories ---------------------------------------------------

    def mkdir(self, path: Path) -> None:
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            _mkdirs(sftp, remote)

    def rmdir(self, path: Path) -> None:
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            sftp.rmdir(remote)

    # -- Host: metadata ------------------------------------------------------

    def stat(self, path: Path) -> FileStat | None:
        return self._describe(path, follow=True)

    def lstat(self, path: Path) -> FileStat | None:
        return self._describe(path, follow=False)

    def _describe(self, path: Path, *, follow: bool) -> FileStat | None:
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            try:
                attr = sftp.lstat(remote)
            except OSError:
                return None
            mode = attr.st_mode or 0
            is_symlink = stat_module.S_ISLNK(mode)
            if follow and is_symlink:
                try:
                    attr = sftp.stat(remote)
                except OSError:
                    return None  # broken link: nothing at the end of it
                mode = attr.st_mode or 0
            return FileStat(
                is_dir=stat_module.S_ISDIR(mode),
                is_file=stat_module.S_ISREG(mode),
                is_symlink=is_symlink,
                size=attr.st_size or 0,
            )

    def listdir(self, path: Path) -> list[DirEntry]:
        remote = _posix(path)
        entries: list[DirEntry] = []
        with self._sftp(remote) as sftp:
            for attr in sftp.listdir_attr(remote):
                mode = attr.st_mode or 0
                is_symlink = stat_module.S_ISLNK(mode)
                if is_symlink:
                    # listdir_attr answers lstat, but DirEntry's flags follow
                    # links as os.scandir's do - so a link costs one round trip.
                    mode = _stat_mode(sftp, f"{remote.rstrip('/')}/{attr.filename}")
                is_dir = stat_module.S_ISDIR(mode)
                entries.append(
                    DirEntry(
                        name=attr.filename,
                        is_dir=is_dir,
                        is_file=stat_module.S_ISREG(mode),
                        is_symlink=is_symlink,
                        size=0 if is_dir else (attr.st_size or 0),
                    )
                )
        return entries

    def realpath(self, path: Path, *, strict: bool = False) -> Path:
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            if strict:
                sftp.stat(remote)  # must exist, as Path.resolve(strict=True) has it
                return Path(sftp.normalize(remote))
            # normalize() is the server's realpath(3), and servers differ on
            # what they do with a path that is not there yet. Resolve the
            # deepest existing ancestor and re-join the missing tail, which is
            # what Path.resolve(strict=False) answers.
            tail: list[str] = []
            cur = remote
            while True:
                try:
                    resolved = sftp.normalize(cur)
                    break
                except OSError:
                    parent = _parent(cur)
                    if parent == cur:  # walked up to the root and it still failed
                        return Path(remote)
                    tail.append(PurePosixPath(cur).name)
                    cur = parent
            return Path(str(PurePosixPath(resolved).joinpath(*reversed(tail))))

    # -- SFTP plumbing -------------------------------------------------------

    def _sftp(self, remote: str) -> _SftpCall:
        """The SFTP session as a context manager that translates its failures.

        A dead transport marks the host dead (so the NEXT call re-dials) and
        surfaces as an OSError naming the connection; an ordinary filesystem
        failure surfaces as the OSError subclass callers already catch.
        """
        return _SftpCall(self, remote)

    def _sftp_session(self) -> paramiko.SFTPClient:
        with self._lock:
            client = self._ensure()
            if self._sftp_client is None:
                self._sftp_client = client.open_sftp()
            return self._sftp_client


def _stat_mode(sftp: paramiko.SFTPClient, remote: str) -> int:
    """st_mode through symlinks; 0 (neither dir nor file) when it cannot be had."""
    try:
        return sftp.stat(remote).st_mode or 0
    except OSError:
        return 0


def _mkdirs(sftp: paramiko.SFTPClient, remote: str) -> None:
    """mkdir -p: create the missing ancestors, shallowest first."""
    missing: list[str] = []
    cur = remote
    while cur not in ("/", "", "."):
        try:
            sftp.stat(cur)
            break
        except OSError:
            missing.append(cur)
            cur = _parent(cur)
    for directory in reversed(missing):
        try:
            sftp.mkdir(directory)
        except OSError:
            if not stat_module.S_ISDIR(_stat_mode(sftp, directory)):
                raise  # lost a race is fine; refused is not


class _SftpCall:
    """See :meth:`SshHost._sftp`."""

    __slots__ = ("_host", "_remote")

    def __init__(self, host: SshHost, remote: str) -> None:
        self._host = host
        self._remote = remote

    def __enter__(self) -> paramiko.SFTPClient:
        try:
            return self._host._sftp_session()
        except (SshError, paramiko.SSHException, EOFError) as exc:
            self._host.mark_dead(exc)
            raise OSError(
                errno.EIO, f"connection lost to {self._host.target}: {exc}", self._remote
            ) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is None:
            return
        if isinstance(exc, (OSError, EOFError, paramiko.SSHException)):
            self._host._check_alive()
        if isinstance(exc, (EOFError, paramiko.SSHException)) and not isinstance(exc, OSError):
            raise OSError(
                errno.EIO, f"connection lost to {self._host.target}: {exc}", self._remote
            ) from exc
        if isinstance(exc, OSError):
            raise _as_oserror(exc, self._remote) from exc
