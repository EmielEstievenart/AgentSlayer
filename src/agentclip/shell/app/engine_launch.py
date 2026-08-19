"""How the remote engine is asked for, and what to say when it does not come.

Pure functions, and deliberately nothing else. Launching an
``agentclip-engine`` on a target is three separate concerns and only two of them
belong to a transport: *what to run* (this module), *how to run it*
(``executor.hosts.ssh.LinkChannel``), and *what the failure meant* (this module
again). Keeping the first and third here is what lets them be tested without a
network, a paramiko, or a process - and what lets ``shell.app`` hold them at
all, since this layer may not import ``executor.hosts`` and so must never be
handed a channel to interrogate. It is handed the two VALUES a channel can
report instead (docs/design/remote-executor.md §2.12).

The version mismatch is not here: a target running another wire version answers
the handshake, and ``remote_link.hello()`` already turns that into a sentence
naming both installs (§2.9). What is here is the failure with no answer at all.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

#: The name the engine half is installed under on the target. A console script
#: on PATH, launched by name over an exec channel - not a path, not a module,
#: not something AgentClip deploys (docs/design/remote-executor.md §2.6).
ENGINE_EXECUTABLE = "agentclip-engine"

#: Where the documented install actually puts that console script. ``uv tool
#: install`` - and pipx, and ``pip install --user`` - write into ``~/.local/bin``,
#: and sshd's non-interactive exec channel does NOT have it on PATH: that channel
#: gets the stock ``/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin``
#: with nothing of the user's own added, which on Ubuntu is the rule rather than
#: the exception (~/.profile is what usually adds it, and no profile is read
#: here). So our own documented install produces a target where launching by
#: name fails and launching by path works - hence the one fallback below.
USER_BIN_DIR = ".local/bin"

#: How that fallback is SPELLED to a human. ``~`` rather than the resolved home,
#: because the sentence is advice about an install, not a path we resolved.
FALLBACK_DISPLAY = f"~/{USER_BIN_DIR}/{ENGINE_EXECUTABLE}"

#: What a user does about a target that has no engine on it. One spelling, named
#: here, because the message is the whole product of a failed launch. The second
#: half is for the case the first has already been done: an install that landed
#: in a directory no non-interactive shell looks in.
INSTALL_HINT = (
    "install it with e.g. `uv tool install agentclip`, or symlink it into /usr/local/bin"
)

# What a POSIX shell says when the name is not on PATH. Checked alongside exit
# 127 rather than instead of it: 127 is the reliable signal and the text is the
# one that survives a shell which exits some other way.
_NOT_FOUND_MARKERS = ("command not found", "not found")


def engine_command(
    project_root: str, service: str | None = None, *, executable: str = ENGINE_EXECUTABLE
) -> str:
    """The command line that starts the engine half on the target.

    ``--project`` and at most ``--service``, and that is the whole contract.
    Notably absent are ``--global-config``, ``--home`` and ``--data-root``: on a
    target the engine reads the TARGET's own config, permissions and platform
    directories by plain local reads, which is §2.5's "the engine owns policy
    wholesale" seen from the launch side. Passing this machine's paths over
    there would be both meaningless (they name directories on the wrong
    computer) and a policy leak.

    Quoting is POSIX (``shlex.quote``) whatever this machine is, because the
    line is read by the target's shell: a remote root containing a space is
    ordinary, and unquoted it would silently become two arguments. The
    executable is quoted for the same reason - ``executable`` is a path once
    :func:`fallback_engine_command` builds it, and a home directory with a space
    in it is somebody's Monday.

    **The line is run BARE - never under a login shell.** ``bash -lc`` would fix
    the PATH problem :data:`USER_BIN_DIR` describes, and it must not be used
    for it: stdout of this process IS the protocol (§2.9), and a profile that
    prints anything - a banner, an SDK's shell hook, a stray ``echo`` in
    ``.bashrc`` - prepends that text to the first JSON line and corrupts the
    handshake. Hence a second explicit spelling instead of a second shell.
    """
    parts = [shlex.quote(executable), "--project", shlex.quote(project_root)]
    if service:
        parts += ["--service", shlex.quote(service)]
    return " ".join(parts)


def fallback_engine_command(home: str, project_root: str, service: str | None = None) -> str:
    """The same launch, spelled as a path into the target's ``~/.local/bin``.

    The one retry a failed launch gets, and deliberately the only one: this is
    where the install method we DOCUMENT puts the binary, not a guess at a PATH
    we cannot see. ``home`` is the remote home the connect sequence already
    captured (``connect.py`` step 5, "Capture home and environment"), joined
    POSIX-style because it names a directory on the target - and passed as the
    resolved path rather than a ``~`` for the target's shell to expand, since a
    bare exec channel has no shell doing tilde expansion for us.
    """
    executable = str(PurePosixPath(home) / USER_BIN_DIR / ENGINE_EXECUTABLE)
    return engine_command(project_root, service, executable=executable)


def is_missing_engine(exit_status: int | None, stderr_tail: str) -> bool:
    """Did this launch fail because nothing of that name could be run?

    The predicate behind the 127 branch of :func:`classify_launch_failure`,
    named apart because a caller acts on it before there is any message: it is
    the one failure worth retrying at another spelling. A shell that cannot find
    a command exits 127 and says so, and either half is enough - a target whose
    shell fails some other way still leaves the sentence behind.
    """
    lowered = stderr_tail.strip().lower()
    return exit_status == 127 or any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def classify_launch_failure(exit_status: int | None, stderr_tail: str, target: str) -> str:
    """What to tell the user when the handshake never arrived.

    The launch produced no ``hello_ack``, so all the evidence there is is how
    the channel ended and what the target wrote to stderr - which is exactly why
    ``LinkChannel`` drains stderr continuously instead of letting it fill a
    window and disappear.

    One case is worth naming apart, because it is the one every first connect
    hits and the one with an action attached: nothing of that name can be run
    over there (:func:`is_missing_engine`).

    That sentence names BOTH spellings, and it has to. By the time a launch is
    classified the caller has already tried the plain name and then
    :func:`fallback_engine_command`, so "is not installed" would be a guess -
    and usually the wrong one, since the far likelier truth is an install in
    some third directory that the non-interactive PATH does not include
    (:data:`USER_BIN_DIR`). Saying what was tried is what turns "it IS
    installed, your tool is broken" into an actionable line.

    Everything else stays honest rather than guessing: the status the channel
    ended with, and the target's own words.
    """
    tail = stderr_tail.strip()
    if is_missing_engine(exit_status, tail):
        return (
            f"{ENGINE_EXECUTABLE} is not on the non-interactive PATH of {target}"
            f" (tried {ENGINE_EXECUTABLE!r} and {FALLBACK_DISPLAY!r}) - {INSTALL_HINT}"
        )
    ended = "it is still running" if exit_status is None else f"exit {exit_status}"
    return (
        f"{ENGINE_EXECUTABLE} on {target} did not answer the handshake ({ended}):"
        f" {tail or '(nothing on stderr)'}"
    )


__all__ = [
    "ENGINE_EXECUTABLE",
    "FALLBACK_DISPLAY",
    "INSTALL_HINT",
    "USER_BIN_DIR",
    "classify_launch_failure",
    "engine_command",
    "fallback_engine_command",
    "is_missing_engine",
]
