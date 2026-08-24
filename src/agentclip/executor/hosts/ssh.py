"""SshHost: one authenticated SSH connection to a target, via Paramiko.

**No longer a Host.** This module used to implement the whole
:class:`~agentclip.executor.hosts.base.Host` seam over SSH - a fresh exec
channel per tool command, an SFTP round trip per file read - and that path was
deleted with remote-executor.md §2.8 (increment 5). A remote session runs its
Executor on the target now: ``agentclip-engine`` is launched over one exec
channel and every tool it runs uses the target's own
:class:`~agentclip.executor.hosts.local.LocalHost`, so no primitive crosses the
link. What is left here is the connection itself - dial, authenticate, notice a
dead link, re-dial - plus the two things the Shell still asks of it directly:
:meth:`SshHost.open_link_channel`, and the handful of probes and reads
:func:`agentclip.executor.hosts.connect.connect_remote` needs BEFORE a session
exists (the remote OS name, the login environment, the remote home, and the
target's own config files).

**Paths.** The connect sequence speaks :class:`pathlib.Path`, and on Windows
that is a ``WindowsPath`` whose ``str()`` would hand the server backslashes. So
every path arriving here is normalized to a POSIX string (:func:`_posix`) before
it reaches the wire, and every path handed back is built from one:
``joinpath``/``relative_to``/``parts``/``as_posix`` are lexical operations a
WindowsPath performs correctly on a POSIX-shaped path.

**Connection loss** (remote-ssh.md design 5) is a two-state machine::

    LIVE --(transport error)--> DEAD --(next operation calls _ensure)--> LIVE

The host marks itself dead and the next operation transparently re-dials and
re-authenticates, with the credentials that already worked - so a reconnect
under a live window needs no prompt it could not show. What a dead link does to
a session in flight is no longer this module's problem: the session lives on the
target, and the link channel dying is one failed call in
:class:`~agentclip.shell.app.remote_link.RemoteLinkClient`, not a verdict about
a tool.

**The link channel** (:meth:`SshHost.open_link_channel`) is the channel that
matters: one long-lived duplex stream carrying the Shell<->Engine wire protocol
(docs/design/remote-executor.md §2.12). It is deliberately NOT wrapped and NOT
``setsid``'d - see the method's docstring for why each of those is a feature.
"""

from __future__ import annotations

import base64
import codecs
import errno
import hashlib
import shlex
import stat as stat_module
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from types import TracebackType

import paramiko

from agentclip.executor.hosts.base import FileStat

# The exit code a connect-time probe reports when it could not complete: what
# ssh(1) itself uses for "the transport failed", and no shell produces it for
# anything else in practice.
CONNECTION_LOST_EXIT = 255

_KEEPALIVE_S = 30
_CONNECT_TIMEOUT_S = 20
_PASSWORD_ATTEMPTS = 3
_POLL_S = 0.02
_RECV_CHUNK = 65536

# How much of the link channel's stderr is kept. The remote engine's stderr is
# its LOG, never protocol data (docs/design/remote-executor.md §2.9), and its
# whole job here is to explain a handshake that never arrived - a job the LAST
# few kilobytes do as well as all of them, and without letting a chatty target
# grow this buffer for the life of a session.
_LINK_STDERR_TAIL = 8192
_LINK_CLOSE_JOIN_S = 2.0

# Prompt callbacks the caller supplies. Both are answered by whoever drives the
# connect - the terminal launch's getpass, or the GUI dialog's modals; a re-dial
# calls them again only if the credentials that worked no longer do.
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


class _ChannelReader:
    """Lines of UTF-8 text off a channel's stdout, blocking until there is one.

    Deliberately hand-rolled rather than ``channel.makefile()``: paramiko's
    ``BufferedFile`` is not a clean text stream (its ``readline`` has its own
    newline rules and its own idea of when a read is short), and the wire's whole
    framing rests on "one ``\\n``-terminated line is one frame". So the same
    ``recv`` loop moves bytes, an INCREMENTAL decoder turns
    them into text - a multibyte character split across two TCP-sized chunks must
    not become two replacement characters - and only ``"\\n"`` ends a line.

    EOF answers ``""``, which is what :class:`RemoteLinkClient` reads as "the
    link closed". A transport that died mid-session raises inside ``recv``; that
    is the same event seen from lower down, so it is reported the same way rather
    than as an exception type the protocol client would have to learn.
    """

    __slots__ = ("_chan", "_decoder", "_buffer", "_eof")

    def __init__(self, chan: paramiko.Channel) -> None:
        self._chan = chan
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._eof = False

    def readline(self, limit: int = -1) -> str:
        """One frame, or ``""`` at EOF. Blocks until one of the two happens."""
        while True:
            newline = self._buffer.find("\n")
            if newline >= 0:
                line = self._buffer[: newline + 1]
                self._buffer = self._buffer[newline + 1 :]
                return line
            if self._eof:
                # A final line with no terminator: hand it over once, then "".
                rest, self._buffer = self._buffer, ""
                return rest
            try:
                data = self._chan.recv(_RECV_CHUNK)
            except (OSError, EOFError, paramiko.SSHException):
                data = b""  # the link died: EOF, from up here
            if data:
                self._buffer += self._decoder.decode(data)
                continue
            self._eof = True
            self._buffer += self._decoder.decode(b"", final=True)


class _ChannelWriter:
    """Text onto a channel's stdin, flushed by the act of writing it.

    ``sendall`` blocks until every byte is on the transport, so ``flush`` has
    nothing left to do - but it exists because the protocol client writes
    ``write`` then ``flush`` and must not have to know which transport it got.
    A dead channel surfaces as :class:`OSError`, which is what that client
    already catches as "the link closed mid-write".
    """

    __slots__ = ("_chan",)

    def __init__(self, chan: paramiko.Channel) -> None:
        self._chan = chan

    def write(self, s: str, /) -> int:
        try:
            self._chan.sendall(s.encode("utf-8"))
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise OSError(errno.EIO, f"the link channel is gone: {exc}") from exc
        return len(s)

    def flush(self) -> None:
        """A no-op: :meth:`write` already blocked until the bytes were sent."""


class LinkChannel:
    """One long-lived duplex channel: the transport a remote engine speaks over.

    What :class:`~agentclip.shell.app.remote_link.RemoteLinkClient` needs is a
    reader and a writer, and this is where an SSH session becomes them
    (docs/design/remote-executor.md §2.12). Nothing above the seam touches a
    paramiko type: :attr:`reader` and :attr:`writer` are the two text-stream
    shapes the client was written against, and the three methods beside them are
    about the CHANNEL rather than the protocol on it.

    **stderr is drained on a thread, always.** Two reasons, and either alone
    would be enough. An unread stderr fills the channel's window and then wedges
    the remote process the moment it writes one more byte - a deadlock whose
    symptom is a link that simply stops answering. And when the handshake never
    arrives, what the launch printed to stderr IS the diagnosis ("command not
    found", a traceback, a permission error), so :meth:`stderr_tail` is what
    turns "the link closed" into a sentence naming what went wrong.
    """

    def __init__(self, chan: paramiko.Channel) -> None:
        self._chan = chan
        self.reader = _ChannelReader(chan)
        self.writer = _ChannelWriter(chan)
        self._tail = bytearray()
        self._tail_lock = threading.Lock()
        self._drain = threading.Thread(
            target=self._drain_stderr, name="agentclip-link-stderr", daemon=True
        )
        self._drain.start()

    def stderr_tail(self) -> str:
        """The last few KB the remote process wrote to stderr, decoded loosely."""
        with self._tail_lock:
            return bytes(self._tail).decode("utf-8", errors="replace")

    def exit_status(self) -> int | None:
        """The channel's exit status, or None while the process is still running."""
        try:
            if not self._chan.exit_status_ready():
                return None
            return self._chan.recv_exit_status()
        except (OSError, EOFError, paramiko.SSHException):
            return None

    def close(self) -> None:
        """Close the channel - which is how the remote engine is stopped."""
        with suppress(Exception):  # closing a dead channel must never raise
            self._chan.close()
        self._drain.join(timeout=_LINK_CLOSE_JOIN_S)

    def _drain_stderr(self) -> None:
        while True:
            try:
                data = self._chan.recv_stderr(_RECV_CHUNK)
            except (OSError, EOFError, paramiko.SSHException):
                return
            if not data:
                return  # EOF on stderr: the process is done writing logs
            with self._tail_lock:
                self._tail.extend(data)
                excess = len(self._tail) - _LINK_STDERR_TAIL
                if excess > 0:
                    del self._tail[:excess]


class SshHost:
    """One authenticated SSH connection to a machine, and what connect asks it.

    NOT a :class:`~agentclip.executor.hosts.base.Host` any more (see the module
    docstring): it implements none of the tool primitives, and the only file
    reads left on it are the connect sequence's own. What it IS is the dialler
    the Shell holds for the life of a remote session - the thing that
    authenticates, notices a dead link and re-dials it, opens the engine's link
    channel, and answers the six questions
    :func:`agentclip.executor.hosts.connect.connect_remote` asks before a
    session exists.
    """

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
        """Dial and authenticate. Raises SshError - the connect sequence's step 2."""
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
        # a reconnect happens mid-session, where the prompt that could ask has
        # no turn of its own to appear in.
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

    # -- connect-time probes -------------------------------------------------

    def probe_command(self, command: str, *, timeout: float = 60.0) -> tuple[int, str]:
        """Run one short command through a login shell and read all of it.

        The replacement for the deleted ``run_blocking``/``spawn`` pair, and
        deliberately a much smaller thing than either
        (docs/design/remote-executor.md §2.8). There is no
        :class:`~agentclip.executor.hosts.base.ExecHandle` here, no pidfile, no
        ``setsid`` process group and no kill path: those existed so a *tool's*
        command could be streamed and cancelled from above the seam, and no tool
        runs over this connection any more - the engine on the target runs them
        all, in its own process, over its own :class:`LocalHost`.

        What is left is what :func:`connect_remote` needs and only that: two
        one-line questions (``uname -s``, ``printenv``) whose answers cannot
        change under a session, asked once, before one exists. ``bash -lc`` is
        kept because it is load-bearing for the second of them - ``printenv``
        must report the LOGIN shell's environment, which is what an
        ``{env:...}`` in the target's config means.

        Never raises: a failed probe is a (code, text) pair like any other, so a
        non-fatal connect step (ssh-connect.md §2, rows 6-8) can carry on with
        an empty answer. A transport that died on the way marks the host dead,
        so the next operation re-dials.
        """
        client = self._ensure()
        try:
            transport = client.get_transport()
            if transport is None:
                raise paramiko.SSHException("the transport is gone")
            chan = transport.open_session(timeout=_CONNECT_TIMEOUT_S)
            chan.set_combine_stderr(True)  # one merged stream: a probe has no stderr
            chan.exec_command(f"bash -lc {shlex.quote(command)}")
        except (paramiko.SSHException, OSError, EOFError) as exc:
            self.mark_dead(exc)
            return CONNECTION_LOST_EXIT, f"connection lost to {self.target}: {exc}"
        buffer = ""
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while True:
                while chan.recv_ready():
                    data = chan.recv(_RECV_CHUNK)
                    if not data:
                        break
                    buffer += data.decode("utf-8", errors="replace")
                if chan.exit_status_ready() and not chan.recv_ready():
                    return chan.recv_exit_status(), buffer
                if time.monotonic() >= deadline:
                    return CONNECTION_LOST_EXIT, f"{command!r} timed out after {timeout:.0f}s"
                time.sleep(_POLL_S)
        except (OSError, EOFError, paramiko.SSHException) as exc:
            self.mark_dead(exc)
            return CONNECTION_LOST_EXIT, f"connection lost to {self.target}: {exc}"
        finally:
            with suppress(Exception):  # closing a dead channel must never raise
                chan.close()

    def probe_os(self) -> str:
        """``uname -s`` on the far end, as the bootstrap's OS name. Fails loudly."""
        code, out = self.probe_command("uname -s", timeout=_CONNECT_TIMEOUT_S)
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

    # -- the link channel ----------------------------------------------------

    def open_link_channel(self, command: str) -> LinkChannel:
        """Run ``command`` on a channel of its own and hand back its streams.

        The transport a remote engine is spoken to over
        (docs/design/remote-executor.md §2.6, §2.12): ``agentclip-engine`` is
        launched by name on the target and the wire protocol runs over this
        channel's stdio for the life of the session. ``_ensure`` dials or
        re-dials first, so this is also the point a dead link is noticed.

        Three deliberate differences from :meth:`probe_command`, all of them
        semantics rather than tidiness:

        * **No wrapper**, and in particular no ``setsid``. The engine process
          MUST die with this channel and with the connection - that IS §2.3's
          disconnect model ("the remote process dies with the SSH connection; no
          detached daemon in v1"), and a session leader would survive exactly the
          event the design says ends it, leaving an engine behind on the target
          with a session store open under it. (The deleted per-call path DID
          ``setsid`` its commands, so that a tool's whole process tree could be
          killed; nothing here is ever killed by us.)
        * **stderr is kept separate** (``set_combine_stderr(False)``): stdout is
          the protocol and nothing else may appear on it, while stderr is the
          remote log and the only evidence a failed launch leaves.
        * **Not read to completion.** A probe is a question with an answer; this
          is a stream with a lifetime. Nothing here waits for an exit status,
          and a transport that dies surfaces as EOF on the reader, which the
          client turns into one failed call.
        """
        client = self._ensure()
        try:
            transport = client.get_transport()
            if transport is None:
                raise paramiko.SSHException("the transport is gone")
            chan = transport.open_session(timeout=_CONNECT_TIMEOUT_S)
            chan.set_combine_stderr(False)
            chan.exec_command(command)
        except (paramiko.SSHException, OSError, EOFError) as exc:
            self.mark_dead(exc)
            raise OSError(errno.EIO, f"connection lost to {self.target}: {exc}") from exc
        return LinkChannel(chan)

    # -- what the connect sequence reads off the target -----------------------
    #
    # Three SFTP calls, and no more: they are what steps 4 and 6 of
    # :func:`connect_remote` are made of - resolve and check the remote root,
    # then read the target's ``.agentclip.toml`` and ``permissions.json`` into
    # the Config that drives THIS side's knobs (docs/design/remote-executor.md
    # §2.12, "what the shell still keeps locally"). Everything else that used to
    # be here - write_bytes, delete, mkdir, rmdir, lstat, listdir - was the tool
    # path, and went with §2.8. Nothing writes to the target from this side any
    # more, so no file is pushed at connect time.
    #
    # ``read_bytes`` keeps its Host-seam name and signature on purpose:
    # ``config.load_config(host=...)`` calls exactly that, and renaming it here
    # would only move the coupling somewhere less obvious.

    def read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        remote = _posix(path)
        with self._sftp(remote) as sftp, sftp.open(remote, "rb") as f:
            if max_bytes is not None:
                return bytes(f.read(max_bytes))  # a prefix, not the whole file
            f.prefetch()
            return bytes(f.read())

    def stat(self, path: Path) -> FileStat | None:
        """Stat following symlinks; None when the path is not there."""
        remote = _posix(path)
        with self._sftp(remote) as sftp:
            try:
                attr = sftp.lstat(remote)
            except OSError:
                return None
            mode = attr.st_mode or 0
            is_symlink = stat_module.S_ISLNK(mode)
            if is_symlink:
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
