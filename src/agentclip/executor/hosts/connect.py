"""The remote connect sequence, once, for both shells to drive.

This is ``cli.remote_launch``'s body, lifted out so the terminal path and the
GUI's connect dialog cannot grow two ideas of what "go remote" means. The
sequence itself is unchanged and its order is the design's
(``docs/design/remote-ssh.md`` decision 7, ``docs/design/ui-briefs/ssh-connect.md``
§2): local flags and the LOCAL global config name the target, the connection is
made and the remote root verified, and only THEN is the remote project's
``.agentclip.toml`` - with its permission ruleset and its MCP servers - read
through the host. That ordering is what makes "the target owns its policy" safe:
the ruleset governing a tool call always comes off the machine that was actually
dialled, never off a guess made before dialling.

**What is injectable, and what is not.** The two things that differ between a
terminal and a window are *who answers a question* and *who says what is
happening* - so the prompts (:class:`ConnectPrompts`) and the progress reporter
(``on_step``) are parameters, and nothing else is. Every failure mode, every
message, every close-on-failure and the order they happen in belong to the
sequence, not to the caller: ``cli.remote_launch`` prints the notes below to the
streams it always printed them to and returns 2 exactly where it always did,
while the GUI paints the same notes into a checklist and offers a retry.

**Where it lives.** In ``agentclip.executor.hosts`` because it is the construction of a
:class:`~agentclip.executor.hosts.ssh.SshHost` and nothing above the seam - but it is the
one module in this package that reads configuration, because steps 1 and 6 ARE
config loads. ``tests/test_layering.py`` gives it its own rule for exactly that
allowance; the rest of ``hosts`` stays the stdlib-and-paramiko leaf it is.
Nothing here is imported by ``agentclip/executor/hosts/__init__.py``, so importing the
seam still costs neither paramiko nor the config layer.

**What an ``SshHost`` is, since remote-executor.md §2.8 (increment 5).** Not a
:class:`~agentclip.executor.hosts.base.Host` - the per-call tool path over SSH
is deleted, and a remote session's tools run on the target inside
``agentclip-engine``. It is a dialled CONNECTION, and this module is its only
real consumer: the six steps below are the whole of what is still asked of a
target from this side, and the seventh (the engine launch) rides on the exec
channel :meth:`~agentclip.executor.hosts.ssh.SshHost.open_link_channel` opens.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agentclip.config import Config, RemoteTarget, default_remote_state_dir, load_config

if TYPE_CHECKING:  # pragma: no cover - the import that costs paramiko
    from agentclip.executor.hosts.ssh import SshHost

# -- the steps ----------------------------------------------------------------
# The six the sequence takes, in the order cli.py:472-478 declares binding. The
# brief's checklist (ssh-connect.md §3.4) numbers five and folds "Auth" into
# "Connect" when key auth works; these are the same beats seen from the code
# side, with the target RESOLUTION - which happens before any network call and
# is the one that fails most often, on a missing --remote-root - given its own
# row rather than hidden inside the first tick.

STEP_RESOLVE = "resolve"
STEP_CONNECT = "connect"
STEP_PROBE = "probe"
STEP_ROOT = "root"
STEP_ENV = "env"
STEP_CONFIG = "config"

CONNECT_STEPS: tuple[str, ...] = (
    STEP_RESOLVE,
    STEP_CONNECT,
    STEP_PROBE,
    STEP_ROOT,
    STEP_ENV,
    STEP_CONFIG,
)

# The seventh beat, and the ONE this module does not run: launching
# ``agentclip-engine`` on the target and shaking hands with it over the exec
# channel (docs/design/remote-executor.md §2.6, §2.12). Since increment 4's flip
# that launch is what a remote session IS - the engine runs over there - so it
# belongs in the same checklist a human watches. It cannot be a step of
# :func:`connect_remote` because this module may not import a protocol (the seam
# is the stdlib-and-paramiko leaf); it is reported by whoever does the launch,
# which is ``cli`` on both shells' paths. Hence two tuples: the sequence's own
# six, and the seven a checklist shows.
STEP_ENGINE = "engine"

CHECKLIST_STEPS: tuple[str, ...] = (*CONNECT_STEPS, STEP_ENGINE)

# What each step is called where a human reads it. Here rather than in a shell,
# because a step's name is part of the sequence's vocabulary: two shells naming
# the same beat differently is the drift this module exists to prevent.
STEP_LABELS: Mapping[str, str] = {
    STEP_RESOLVE: "Resolve target",
    STEP_CONNECT: "Connect and authenticate",
    STEP_PROBE: "Probe the remote OS",
    STEP_ROOT: "Check the remote root",
    STEP_ENV: "Capture home and environment",
    STEP_CONFIG: "Load the remote config",
    STEP_ENGINE: "Start the engine on the target",
}

# How many times a password may be asked for, per connect. The truth is
# ``ssh.SshHost._PASSWORD_ATTEMPTS`` and this is a copy, spelled here so a
# caller can SAY the number ("attempt 2 of 3") without importing the module that
# drags paramiko in - a test pins the two together. A UI must not add a fourth
# attempt of its own: that would call connect() again from scratch and
# double-count reconnects (ssh-connect.md §2).
PASSWORD_ATTEMPTS = 3

# Which steps are fatal to a connect attempt. STEP_ENV and STEP_CONFIG are not:
# a home that cannot be resolved falls back to the POSIX convention, an unusable
# `printenv` means an empty environment (which is what an unset variable already
# substitutes to), and load_config never raises - its complaints become
# Config.warnings (ssh-connect.md §2, rows 6-8). STEP_ENGINE is fatal for the
# plainest reason of all: a remote session with no engine on the target has
# nothing to run.
FATAL_STEPS: frozenset[str] = frozenset(
    {STEP_RESOLVE, STEP_CONNECT, STEP_PROBE, STEP_ROOT, STEP_ENGINE}
)


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One beat of the sequence, as the caller is told about it.

    ``note`` is the human sentence that beat produced, unprefixed: the terminal
    path prints it with ``agentclip: `` in front, the GUI paints it under the
    checklist row. Empty for the beats that have nothing to say.
    """

    step: str
    state: str  # "running" | "ok" | "failed"
    note: str = ""


StepReport = Callable[[StepEvent], None]

# prompt -> secret; None/"" means give up (hosts/ssh.py, PasswordPrompt).
PasswordPrompt = Callable[[str], "str | None"]
# host, key type, SHA256 fingerprint -> trust it? (hosts/ssh.py, HostKeyPrompt).
HostKeyPrompt = Callable[[str, str, str], bool]
# title, instructions, ((prompt, echo), ...) -> one answer per prompt, or None
# to give up. Paramiko's ``auth_interactive`` handler contract, which is the
# shape a TOTP challenge arrives in (ssh-connect.md §3.7).
KeyboardPrompt = Callable[[str, str, "Sequence[tuple[str, bool]]"], "list[str] | None"]


@dataclass(frozen=True, slots=True)
class ConnectPrompts:
    """Who answers the three questions a dial can ask.

    All optional and all default to "nobody": a host with no host-key callback
    never trusts an unknown key (``SshHost._confirm_host_key``), and one with no
    password callback simply fails authentication rather than hanging on a
    prompt no one can see.
    """

    password: PasswordPrompt | None = None
    host_key: HostKeyPrompt | None = None
    keyboard_interactive: KeyboardPrompt | None = None


class ConnectError(Exception):
    """A connect attempt that failed, and which step it failed on.

    ``message`` is what the caller shows, verbatim and unprefixed - it is the
    exact text ``cli.remote_launch`` has always put on stderr after
    ``agentclip: ``. The host, if one was built, has already been closed: a
    half-connected session is not a thing (remote-ssh.md line 478).
    """

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


@dataclass(frozen=True, slots=True)
class ConnectedRemote:
    """A live remote session's ingredients - everything a launch needs.

    The same fields ``cli.Launch`` carries, plus the two the GUI needs to talk
    about the connection it just made (``target`` for the picker's "save this
    one", ``environ`` because the MCP layer's ``{env:}`` came from it).
    """

    host: SshHost
    target: RemoteTarget
    os_name: str
    project_root: Path
    home: Path
    data_root: Path
    config: Config
    environ: Mapping[str, str] = field(default_factory=dict)


# -- the target's login-shell environment --------------------------------------

# A `printenv` line that is unmistakably one variable: a POSIX name, an "=", and
# everything after it. Anything else is dropped - see parse_environment.
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_environment(text: str) -> dict[str, str]:
    """``printenv`` output as a mapping, conservatively.

    Only lines that ARE a name=value pair count. printenv writes each value raw,
    newlines and all, with nothing marking where one ends - so from this side a
    multi-line value's continuation lines are indistinguishable from junk (a
    shell notice, a login banner, the stderr of a profile script). A guess
    either way is worse than a miss: stapling a stray line onto a token, or
    splitting one, hands a corrupted secret to a server that will fail somewhere
    far from here. Dropping the unreadable costs at most a rare multi-line
    variable, and an absent one already substitutes empty.
    """
    return {
        match.group(1): match.group(2)
        for line in text.splitlines()
        if (match := _ENV_LINE_RE.match(line))
    }


def remote_environment(host: SshHost) -> tuple[dict[str, str], str]:
    """The target's login-shell environment, read once, at connect.

    It is what ``{env:...}`` in that machine's MCP config means: whoever wrote
    ``{env:API_TOKEN}`` into a file over there exported it over there
    (docs/design/remote-ssh.md, "the target owns its policy"). A launch-time
    probe like ``probe_os``, and for the same reason - the answer cannot change
    under a session, and every later reader wants it already there.

    Unlike probe_os this is not fatal, so it returns a (mapping, complaint)
    pair rather than raising: an unusable answer means empty, which is exactly
    what an unset variable already substitutes to. ``probe_command`` already
    runs everything through ``bash -lc``, so the bare command IS the login
    shell's own view; prefixing it again would nest a second shell.
    """
    code, out = host.probe_command("printenv")
    environment = parse_environment(out) if code == 0 else {}
    if environment:
        return environment, ""
    return environment, (
        f"{host.target} did not answer 'printenv' usefully (exit {code});"
        " {env:...} in its MCP config will be empty"
    )


# -- resolving a target --------------------------------------------------------


def resolve_target(
    target_spec: str,
    remote_root: str | None,
    *,
    local_root: Path,
    service_override: str | None = None,
    global_config_path: Path | None = None,
) -> tuple[RemoteTarget, Config]:
    """Step 1: which machine, and which directory on it. No network call.

    The LOCAL config decides this - ``[remote.<name>]`` saved targets merged
    from the host PC's files, then the free-form ``[user@]host[:port]`` grammar
    (``RemoteConfig.selected``, which has already unwrapped a pasted
    ``ssh <destination>``). Raises :class:`ConnectError` when the string does
    not name a machine at all, and when nothing supplies a root - the failure
    ``--remote-root`` exists to prevent. Both happen before anything is dialled,
    which is the whole point of the check below: a host with a space in it
    cannot be reached, and finding that out from ``getaddrinfo`` two steps later
    - after this row has already ticked green - tells the user nothing about
    what they typed.
    """
    boot = load_config(
        local_root,
        service_override=service_override,
        global_config_path=global_config_path,
        remote_target=target_spec,
        remote_root=remote_root,
    )
    target = boot.remote.selected()
    if target is None:  # pragma: no cover - only reachable with an empty --ssh
        raise ConnectError(STEP_RESOLVE, f"--ssh {target_spec!r} names no machine")
    if not target.host or any(ch.isspace() for ch in target.host):
        raise ConnectError(
            STEP_RESOLVE,
            f"--ssh {target_spec!r} is not a destination: enter just the machine -"
            ' an ssh_config alias or [user@]host[:port], e.g. "wsl" or'
            ' "emiel@server:22".',
        )
    if not target.root:
        raise ConnectError(
            STEP_RESOLVE,
            f"--ssh {target_spec!r} needs a project root on the remote machine:"
            " pass --remote-root, or give the saved target a root.",
        )
    return target, boot


def describe_target(target: RemoteTarget) -> str:
    """``user@host:port`` the way ``SshHost.target`` spells it, before dialling.

    The dialog's own preview of what it is about to connect to, so a typo is
    visible before it costs a timeout. Port 22 and a blank user are omitted,
    matching ``ssh.py``'s formatting exactly - a preview that disagreed with the
    name the session then uses would be worse than none.
    """
    base = f"{target.user}@{target.host}" if target.user else target.host
    return base if target.port in (0, 22) else f"{base}:{target.port}"


def ssh_config_aliases(path: Path | None = None) -> list[str]:
    """The literal ``Host`` aliases in ``~/.ssh/config``, for the picker.

    New surface: ``SshHost._read_ssh_config`` looks up the ONE alias the user
    already typed, and nothing in AgentClip has ever listed them. Wildcards and
    ``Match`` blocks are hidden (docs/design/gui.md §4 ruling 3) - neither names
    a machine one could connect to.

    ssh-connect.md §6 question 3 proposed ``SSHConfig.get_hostnames() - {"*"}``
    for the filter, and that call is unusable: it does ``entry["host"]`` over
    every parsed block, and a ``Match`` block has no ``host`` key, so it raises
    ``KeyError`` on any config file that has one (paramiko 4.0's
    ``config.py:325``, verified). So the parse is still paramiko's - the same
    tokenizer, the same notion of what a Host line contains - and only the
    accessor is ours, reading past a block that has no hostnames instead of
    tripping over it.
    """
    import paramiko

    location = path if path is not None else Path.home() / ".ssh" / "config"
    config = paramiko.SSHConfig()
    try:
        with open(location, encoding="utf-8", errors="replace") as handle:
            config.parse(handle)
    except OSError:
        return []
    seen: list[str] = []
    for entry in getattr(config, "_config", []):
        for pattern in entry.get("host") or []:
            name = str(pattern)
            if name in seen or _is_wildcard(name):
                continue
            seen.append(name)
    return seen


def _is_wildcard(pattern: str) -> bool:
    """Is this ``Host`` pattern a rule rather than a machine?"""
    return not pattern or pattern.startswith("!") or any(ch in pattern for ch in "*?")


# -- the sequence ---------------------------------------------------------------


def connect_remote(
    target_spec: str,
    remote_root: str | None,
    *,
    local_root: Path,
    service_override: str | None = None,
    prompts: ConnectPrompts | None = None,
    on_step: StepReport | None = None,
    global_config_path: Path | None = None,
) -> ConnectedRemote:
    """Connect, authenticate and probe. Six steps, in the design's order.

    Raises :class:`ConnectError` on any of the four fatal steps, having already
    closed whatever host it had built. The two non-fatal steps report what they
    could not do through ``on_step`` and carry on - the launch is not the place
    to fail over a missing ``printenv``.
    """
    from agentclip.executor.hosts.ssh import SshError, SshHost

    prompts = prompts if prompts is not None else ConnectPrompts()
    report = on_step if on_step is not None else _no_step

    # 1. Resolve ------------------------------------------------------------
    report(StepEvent(STEP_RESOLVE, "running"))
    try:
        target, _boot = resolve_target(
            target_spec,
            remote_root,
            local_root=local_root,
            service_override=service_override,
            global_config_path=global_config_path,
        )
    except ConnectError as err:
        report(StepEvent(err.step, "failed", err.message))
        raise
    report(StepEvent(STEP_RESOLVE, "ok", describe_target(target)))

    host = SshHost(
        target.host,
        user=target.user,
        port=target.port,
        password_prompt=prompts.password,
        host_key_prompt=prompts.host_key,
        keyboard_prompt=prompts.keyboard_interactive,
    )

    # 2. Dial + authenticate ------------------------------------------------
    report(StepEvent(STEP_CONNECT, "running", f"connecting to {target.host}..."))
    try:
        host.connect()
    except SshError as exc:
        raise _abort(host, report, STEP_CONNECT, str(exc)) from exc
    report(StepEvent(STEP_CONNECT, "ok"))

    # 3. Probe the OS -------------------------------------------------------
    report(StepEvent(STEP_PROBE, "running"))
    try:
        os_name = host.probe_os()
    except SshError as exc:
        raise _abort(host, report, STEP_PROBE, str(exc)) from exc
    report(StepEvent(STEP_PROBE, "ok", os_name))

    # 4. The remote root ----------------------------------------------------
    report(StepEvent(STEP_ROOT, "running"))
    try:
        project_root = host.realpath(Path(target.root), strict=True)
        root_stat = host.stat(project_root)
    except OSError as exc:
        raise _abort(
            host, report, STEP_ROOT, f"cannot use {target.root!r} on {host.target}: {exc}"
        ) from exc
    if root_stat is None or not root_stat.is_dir:
        raise _abort(
            host,
            report,
            STEP_ROOT,
            f"--remote-root is not a directory on {host.target}: {target.root}",
        )
    report(
        StepEvent(
            STEP_ROOT,
            "ok",
            f"{host.target} is {os_name}, working in {project_root.as_posix()}",
        )
    )

    # 5. Home + environment (non-fatal) -------------------------------------
    # Resolved before the config load, not after it: the remote home is what
    # ``~`` means for the rest of this session, and the permission ruleset the
    # load reads lives under it (docs/design/remote-ssh.md, "the target owns its
    # policy"). Skills take the same pair further down.
    report(StepEvent(STEP_ENV, "running"))
    home = host.home_dir()
    environment, complaint = remote_environment(host)
    report(StepEvent(STEP_ENV, "ok", complaint))

    # 6. The REMOTE project's config ----------------------------------------
    report(StepEvent(STEP_CONFIG, "running"))
    config = load_config(
        project_root,
        service_override=service_override,
        global_config_path=global_config_path,
        remote_target=target_spec,
        remote_root=remote_root,
        host=host,
        home=home,
        environ=environment,
    )
    report(StepEvent(STEP_CONFIG, "ok", config.permission_source))

    return ConnectedRemote(
        host=host,
        target=target,
        os_name=os_name,
        project_root=project_root,
        home=home,
        data_root=default_remote_state_dir(host.target, project_root.as_posix()),
        config=config,
        environ=environment,
    )


def _abort(host: SshHost, report: StepReport, step: str, message: str) -> ConnectError:
    """Close the half-built host, say which step died, and hand back the error.

    Returned rather than raised so the call site reads ``raise _abort(...) from
    exc`` and keeps the original exception chained. The close is unconditional,
    where ``cli.remote_launch`` used to leave a failed dial's client to the
    process exit: this path no longer ends in one - a GUI retries in place - and
    a socket that outlived its attempt would be a leak per retry.
    """
    with_close = getattr(host, "close", None)
    if with_close is not None:
        with_close()
    report(StepEvent(step, "failed", message))
    return ConnectError(step, message)


def _no_step(event: StepEvent) -> None:
    """The reporter a caller that does not want progress gets."""


__all__ = [
    "CHECKLIST_STEPS",
    "CONNECT_STEPS",
    "FATAL_STEPS",
    "PASSWORD_ATTEMPTS",
    "STEP_CONFIG",
    "STEP_CONNECT",
    "STEP_ENGINE",
    "STEP_ENV",
    "STEP_LABELS",
    "STEP_PROBE",
    "STEP_RESOLVE",
    "STEP_ROOT",
    "ConnectError",
    "ConnectPrompts",
    "ConnectedRemote",
    "HostKeyPrompt",
    "KeyboardPrompt",
    "PasswordPrompt",
    "StepEvent",
    "StepReport",
    "connect_remote",
    "describe_target",
    "parse_environment",
    "remote_environment",
    "resolve_target",
    "ssh_config_aliases",
]
