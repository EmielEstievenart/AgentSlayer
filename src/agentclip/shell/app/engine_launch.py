"""How the remote engine is asked for, and what to say when it does not come.

Two pure functions, and deliberately nothing else. Launching an
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

#: The name the engine half is installed under on the target. A console script
#: on PATH, launched by name over an exec channel - not a path, not a module,
#: not something AgentClip deploys (docs/design/remote-executor.md §2.6).
ENGINE_EXECUTABLE = "agentclip-engine"

#: What a user does about a target that has no engine on it. One spelling, named
#: here, because the message is the whole product of a failed launch.
INSTALL_HINT = "install it with e.g. `uv tool install agentclip`"

# What a POSIX shell says when the name is not on PATH. Checked alongside exit
# 127 rather than instead of it: 127 is the reliable signal and the text is the
# one that survives a shell which exits some other way.
_NOT_FOUND_MARKERS = ("command not found", "not found")


def engine_command(project_root: str, service: str | None = None) -> str:
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
    ordinary, and unquoted it would silently become two arguments.
    """
    parts = [ENGINE_EXECUTABLE, "--project", shlex.quote(project_root)]
    if service:
        parts += ["--service", shlex.quote(service)]
    return " ".join(parts)


def classify_launch_failure(exit_status: int | None, stderr_tail: str, target: str) -> str:
    """What to tell the user when the handshake never arrived.

    The launch produced no ``hello_ack``, so all the evidence there is is how
    the channel ended and what the target wrote to stderr - which is exactly why
    ``LinkChannel`` drains stderr continuously instead of letting it fill a
    window and disappear.

    One case is worth naming apart, because it is the one every first connect
    hits and the one with an action attached: the engine is simply not installed
    over there. A shell that cannot find a command exits 127 and says so, and
    either half of that is enough - a target whose login shell fails in some
    other way still leaves the sentence behind.

    Everything else stays honest rather than guessing: the status the channel
    ended with, and the target's own words.
    """
    tail = stderr_tail.strip()
    lowered = tail.lower()
    if exit_status == 127 or any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        return f"{ENGINE_EXECUTABLE} is not installed on {target} - {INSTALL_HINT}"
    ended = "it is still running" if exit_status is None else f"exit {exit_status}"
    return (
        f"{ENGINE_EXECUTABLE} on {target} did not answer the handshake ({ended}):"
        f" {tail or '(nothing on stderr)'}"
    )


__all__ = ["ENGINE_EXECUTABLE", "INSTALL_HINT", "classify_launch_failure", "engine_command"]
