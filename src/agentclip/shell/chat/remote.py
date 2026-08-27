"""The SSH connect dialog's model: no window, no page, no toolkit.

The service editor's arrangement (``webview/service_editor.py``, parity increment 5)
applied to the one surface the TUI does not have. Everything the dialog DECIDES
is here - which targets are offered, what a form has to say before "Connect" is
allowed, which checklist row a failure lands on, what the three ways out of a
failure do, and what the target-owns-its-policy banner says - and
``chat/view.py`` keeps only the two things a model cannot do: run the connect
coroutine, and draw.

The sequence itself is NOT here. It is
:func:`agentclip.executor.hosts.connect.connect_remote`, the same function
``cli.remote_launch`` drives; this object is what stands between it and a human
who would rather not retype a shell command (``docs/design/ui-briefs/
ssh-connect.md`` §1).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from agentclip.config import (
    DEFAULT_MONITOR_PORT,
    MONITOR_LOOPBACK,
    Config,
    MonitorTarget,
    RemoteTarget,
    ssh_destination,
)
from agentclip.driver.monitor.auth import TOKEN_CHARS
from agentclip.engine.link.factory import EngineRequest
from agentclip.executor.hosts.connect import (
    CHECKLIST_STEPS,
    STEP_LABELS,
    ConnectedRemote,
    ConnectError,
    StepEvent,
    describe_target,
)
from agentclip.shell.app.link import Link, SkillReport

# The four states of the whole surface. ``done`` is a screen rather than a
# closed dialog because that is where the policy banner and the save offer live
# - closing on success would put the one thing the user most needs to read
# behind a toast they may already have missed (brief §3.9).
PHASE_FORM = "form"
PHASE_RUNNING = "running"
PHASE_FAILED = "failed"
PHASE_DONE = "done"

# A checklist row's four states. ``pending`` is the resting one and it is what a
# row AFTER a failure keeps: later stages stay pending, never
# skipped-with-a-checkmark (brief §3.4).
ROW_PENDING = "pending"

MISSING_TARGET = "name a machine: a saved target, a ~/.ssh/config alias, or [user@]host[:port]"
MISSING_ROOT = "this target has no saved root - give the project directory on the remote machine"

# What the banner says when the target has no ruleset of its own. The absence is
# stated rather than defaulted: a user with a carefully tuned permissions.json on
# THIS PC sees none of it apply the moment they connect, and silence about that
# is the footgun (docs/design/remote-ssh.md, "the target owns its policy").
NO_RULESET = "No permissions.json on {target}; this session runs on the shipped defaults"
RULESET_FROM = "Permissions and MCP servers for this session come from {source}"
# [approval] used to be pinned to this PC, then briefly merged this PC's
# config.toml with the target's .agentclip.toml. It is neither now: the engine
# owns policy wholesale AND the engine runs on the target, so the whole merge
# happens over there and this PC's config.toml is not even reachable from it -
# `engine_command` deliberately sends no --global-config
# (docs/design/remote-executor.md sections 2.5, 2.6, 2.12). The banner says so
# for the same reason it names the ruleset's machine: a policy fact the user
# cannot see is a footgun whichever way it points.
APPROVAL_POLICY = (
    "[approval] (mode, yolo, command rules) is read entirely on {target}: "
    "its config.toml merged with its .agentclip.toml"
)
# Stdio servers used to be refused in a remote session, because the process that
# would have spawned them was this PC's and their argv described another machine.
# Since the flip the process IS on that machine (section 2.7 reverses
# remote-ssh.md here), so they start - and WHERE is the fact worth stating, since
# it is the same "which box is this really happening on" question the two lines
# above answer.
STDIO_ON_TARGET = "stdio MCP servers for this session are started on {target}: {names}"


class RemoteRuntime(Protocol):
    """Everything a session is built from, rebuilt against a machine.

    Structural, and produced by ``cli.main``: launching the engine, reaching its
    MCP runtime and deciding where a session's state lives are launch questions,
    and a second construction site for them is a second thing to drift (the same
    reason ``run_gui`` is HANDED its factory rather than building one). What this
    shell does with it is the session boundary - "one session, one host" means
    the controller is rebuilt, not patched (remote-ssh.md decision 4).

    Since the flip (docs/design/remote-executor.md §2.12) the factory behind
    ``engine_factory`` mints a ``RemoteLink`` per session over an engine running
    ON the target, and ``mcp_manager`` is the object that owns the channel it
    speaks on. Neither fact is visible here, deliberately: this shell asks for a
    ``Link`` and for something that answers ``statuses()``, and which machine
    supplies them is exactly what the seam exists to hide.
    """

    @property
    def project_root(self) -> Path: ...
    @property
    def config(self) -> Config: ...
    @property
    def engine_factory(self) -> Callable[[EngineRequest], Link]: ...
    @property
    def mcp_manager(self) -> Any: ...
    @property
    def skills(self) -> Callable[[], SkillReport]: ...
    @property
    def host(self) -> Any: ...
    @property
    def target(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RemoteConnect:
    """What the GUI needs in order to go remote by itself.

    ``local_root`` and ``service_override`` are the two command-line facts step
    1 of the sequence reads (the LOCAL config names the target); ``build`` is
    how a successful connect becomes a session, and it belongs to ``cli.main``
    for the reason :class:`RemoteRuntime` gives. It is also where the engine is
    LAUNCHED on the target, so it can fail after the six steps have all ticked -
    the view runs it as the checklist's seventh row and shows what it raised.
    ``pending`` is
    ``--gui --ssh``: the launch that used to block on the terminal before the
    window existed, deferred into the dialog with its fields already filled.
    """

    local_root: Path
    build: Callable[[ConnectedRemote], RemoteRuntime]
    service_override: str | None = None
    pending: tuple[str, str | None] | None = None
    # Both default to the real files. Injectable for the same reason
    # ``GuiView``'s ``global_config_path`` and ``profile_root`` are: no test run
    # may read the developer's ~/.ssh or write their config.toml.
    global_config_path: Path | None = None
    ssh_config_path: Path | None = None


@dataclass(frozen=True, slots=True)
class TargetRow:
    """One offer in the picker. ``key`` is what the page sends back."""

    key: str  # "saved:<name>" or "alias:<name>"
    name: str
    detail: str  # user@host:port, the way SshHost.target spells it
    root: str


def saved_rows(config: Config) -> list[TargetRow]:
    """The ``[remote.<name>]`` tables, read through the config layer that owns
    them (``RemoteConfig.targets``) rather than by re-parsing TOML here."""
    return [
        TargetRow(
            key=f"saved:{name}",
            name=name,
            detail=describe_target(target),
            root=target.root,
        )
        for name, target in sorted(config.remote.targets.items())
    ]


def alias_rows(aliases: Iterable[str], saved: Sequence[TargetRow]) -> list[TargetRow]:
    """``~/.ssh/config`` aliases, minus the ones already saved by that name.

    A second section rather than a merged list, and with no root: selecting one
    drops the user into manual entry with the host filled in and the root empty,
    because ssh_config knows how to reach a machine and nothing about which
    directory on it is the project (brief §3.2).
    """
    taken = {row.name for row in saved}
    return [
        TargetRow(key=f"alias:{name}", name=name, detail=name, root="")
        for name in aliases
        if name not in taken
    ]


def monitor_rows(config: Config) -> list[TargetRow]:
    """The ``[monitor.<name>]`` tables, as picker rows.

    The same shape the SSH picker uses, ``root`` empty because a Monitor has no
    project - what a monitor row's second line carries is the address, and for a
    via-SSH target the hop in front of it (:meth:`MonitorTarget.describe`).
    """
    return [
        TargetRow(key=f"monitor:{name}", name=name, detail=target.describe(), root="")
        for name, target in sorted(config.monitor.targets.items())
    ]


def policy_lines(config: Config, target_label: str) -> list[str]:
    """The target-owns-its-policy banner, from what the config load already knows.

    No new backend logic: ``Config.permission_source`` is the machine-qualified
    string ``_describe_path`` already builds for exactly this reason
    (remote-ssh.md, "Consequences to handle": the permission source shown must
    name the machine, not just the path), and the stdio refusal is a fact about
    the parsed server list rather than about any runtime.

    The stdio test is ``no url`` rather than ``isinstance(McpLocalServer)``:
    this shell reads MCP through duck-typing everywhere it touches it
    (``chat/view.py:_mcp_line``), which is what keeps ``agentclip.executor.mcp`` out of
    its import graph.
    """
    lines: list[str] = []
    if config.permission_source:
        lines.append(RULESET_FROM.format(source=config.permission_source))
    else:
        lines.append(NO_RULESET.format(target=target_label))
    lines.append(APPROVAL_POLICY.format(target=target_label))
    stdio = [
        str(getattr(server, "name", ""))
        for server in config.mcp_servers.servers
        if not getattr(server, "url", "")
    ]
    if stdio:
        lines.append(STDIO_ON_TARGET.format(target=target_label, names=", ".join(stdio)))
    return lines


class ConnectDialog:
    """What the connect dialog is showing, and every transition between them.

    Constructed per visit. The one piece of state that outlives it is on the
    view: which host the session is actually running on.
    """

    def __init__(
        self,
        *,
        saved: Sequence[TargetRow] = (),
        aliases: Sequence[TargetRow] = (),
        target: str = "",
        root: str = "",
    ) -> None:
        self.saved = list(saved)
        self.aliases = list(aliases)
        self.target = target
        self.root = root
        self.phase = PHASE_FORM
        self.error = ""
        self.failure = ""
        self.failed_step = ""
        self.steps: dict[str, StepEvent] = {}
        self.policy: list[str] = []
        self.connected = ""
        self.save_name = ""
        self.can_save = False
        self.saved_note = ""

    # -- the form --------------------------------------------------------------

    def select(self, key: str) -> None:
        """A picker row was clicked: prefill the form, and do not connect.

        One "Connect" action doing one thing, per brief §3.2 - the same aversion
        to hidden multi-step actions decision 4 has ("no mid-session switching").
        """
        for row in [*self.saved, *self.aliases]:
            if row.key == key:
                self.target = row.name
                self.root = row.root
                self.error = ""
                return

    def set_fields(self, target: str, root: str) -> None:
        self.target = target.strip()
        self.root = root.strip()

    def is_saved(self) -> bool:
        """Is what was typed the NAME of a target this PC already has?

        The picker's own list, not the session config's: after a successful
        connect that config is the TARGET's, and asking it whether this PC has a
        table for the box would be asking the wrong machine.
        """
        return any(row.name == self.target for row in self.saved)

    def saved_root(self) -> str:
        """The root the named saved target supplies, if the name is one."""
        for row in self.saved:
            if row.name == self.target:
                return row.root
        return ""

    def validate(self) -> str:
        """Why "Connect" cannot be pressed yet, or "".

        Only the two things a form CAN know. Whether the root exists, or is a
        directory, needs a live SFTP session (step 4), so a bad path is a retry
        state and never a form error (brief §3.3).
        """
        if not self.target:
            return MISSING_TARGET
        if not self.root and not self.saved_root():
            return MISSING_ROOT
        return ""

    def begin(self) -> bool:
        """Arm the checklist. False (with ``error`` set) if the form is not ready."""
        self.error = self.validate()
        if self.error:
            return False
        self.phase = PHASE_RUNNING
        self.failure = ""
        self.failed_step = ""
        self.steps = {}
        return True

    def edit(self) -> None:
        """"Edit": back to the form with what was attempted still in it."""
        self.phase = PHASE_FORM
        self.error = ""

    # -- the run ---------------------------------------------------------------

    def step(self, event: StepEvent) -> None:
        """One beat of the sequence. A ``failed`` beat halts the checklist where
        it is - the rows after it stay pending rather than being skipped."""
        self.steps[event.step] = event
        if event.state == "failed":
            self.phase = PHASE_FAILED
            self.failed_step = event.step
            self.failure = event.note

    def failed(self, error: ConnectError) -> None:
        """The attempt is over. Belt to ``step``'s braces: an exception that
        never reported a failed beat (a bug, or a cancel) still gets a state."""
        self.phase = PHASE_FAILED
        self.failed_step = error.step
        self.failure = error.message

    def succeeded(self, *, connected: str, policy: Sequence[str], can_save: bool) -> None:
        self.phase = PHASE_DONE
        self.connected = connected
        self.policy = list(policy)
        self.can_save = can_save
        self.save_name = _suggest_name(self.target) if can_save else ""

    def saved_as(self, name: str) -> None:
        self.can_save = False
        self.saved_note = f"saved as [remote.{name}] in the global config"

    # -- the page's view of all of it -----------------------------------------

    def event(self) -> dict[str, Any]:
        """The whole surface in one event, ``open: false`` being the closed
        state - the service editor's shape, for the service editor's reason: a
        page that reassembles a dialog out of partial writes has one way to be
        half-painted per write."""
        return {
            "open": True,
            "phase": self.phase,
            "target": self.target,
            "root": self.root,
            "preview": self.target and describe_target(_parse(self.target)) or "",
            "saved": [_row(row) for row in self.saved],
            "aliases": [_row(row) for row in self.aliases],
            "error": self.error,
            # CHECKLIST_STEPS, not CONNECT_STEPS: the seventh row is the engine
            # launch on the target, which is not a beat of ``connect_remote``
            # (that module may not import a protocol) but IS a beat of going
            # remote - and since increment 4's flip it is the one that decides
            # whether there is a session at all.
            "steps": [self._row_of(step) for step in CHECKLIST_STEPS],
            "failed_step": self.failed_step,
            "failure": self.failure,
            "policy": list(self.policy),
            "connected": self.connected,
            "can_save": self.can_save,
            "save_name": self.save_name,
            "saved_note": self.saved_note,
            "busy": self.phase == PHASE_RUNNING,
        }

    def _row_of(self, step: str) -> dict[str, str]:
        event = self.steps.get(step)
        return {
            "step": step,
            "label": STEP_LABELS[step],
            "state": event.state if event is not None else ROW_PENDING,
            "note": event.note if event is not None else "",
        }


def _row(row: TargetRow) -> dict[str, str]:
    return {"key": row.key, "name": row.name, "detail": row.detail, "root": row.root}


def _parse(spec: str) -> RemoteTarget:
    """``[user@]host[:port]``, parsed the way ``RemoteConfig.selected`` parses it.

    The dialog's preview and the backend's resolution must not be able to
    disagree, so this is that method's own ``rpartition``/``partition`` pair and
    nothing cleverer (brief §3.3), preceded by the same unwrapping of a pasted
    ``ssh <destination>`` the config does before it parses. A saved name or a
    bare alias falls out of it unchanged, which is exactly what happens to it
    downstream too.
    """
    spec = ssh_destination(spec)
    user, _, rest = spec.rpartition("@")
    host, colon, port = rest.partition(":")
    return RemoteTarget(
        name=spec,
        host=host,
        user=user,
        port=int(port) if colon and port.isdigit() else 0,
    )


def _suggest_name(spec: str) -> str:
    """A ``[remote.<name>]`` table name proposed from what was typed.

    The host part, punctuation flattened - ``dev@10.0.0.5:2222`` suggests
    ``10-0-0-5``. Only a suggestion: the save offer is a text box, and the user
    renames it in the box (gui.md §4 ruling 1).
    """
    host = _parse(spec).host or spec
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-")
    return cleaned.lower() or "remote"


# == the Monitor tab (docs/design/ui-monitor.md 9.2) ==========================
# The second tab on the same dialog, and a different KIND of thing behind it:
# the Executor tab starts a session on another machine, this one swaps the
# screen under the session that is already running. A mid-session dial is a
# LINK event - the loop parks in DISCONNECTED, the SwitchableMonitor's inner is
# replaced and the recipe re-derives from the screen (2.9) - so there is no
# checklist here and no seventh row: there is one round trip, and it either
# landed or it did not.

MODE_DIRECT = "direct"  # host, port, token: dial the monitor's own address
MODE_SSH = "ssh"  # a saved SSH target, then its loopback over a direct-tcpip channel
# ...and the third (ui-monitor.md §10.2): launch an ``agentclip-monitor`` on
# THIS PC and dial it. It has no fields at all - the port is picked at spawn and
# the token is the shared file both processes read - which is the whole of why
# "local" is a mode here rather than the absence of one: it is still a dial, to
# still a monitor process, over still a socket. It just starts the process too.
MODE_LOCAL = "local"
#: Every mode the form accepts, so ``set_fields`` has one list to check against.
MONITOR_MODES = (MODE_DIRECT, MODE_SSH, MODE_LOCAL)

MONITOR_MISSING_HOST = "name the machine the monitor is running on"
MONITOR_MISSING_VIA = "pick the saved SSH target the monitor sits behind"
MONITOR_BAD_PORT = "the monitor's port is a number from 1 to 65535"
# A token is optional (a monitor started with --no-token has none), but a token
# that is THERE and the wrong length is a paste that went wrong, and saying so
# in the form costs one round trip less than a refused hello does.
MONITOR_BAD_TOKEN = f"a monitor token is {TOKEN_CHARS} characters; this one is {{n}}"
# The refusal when the Via-SSH target is not the machine the Executor is on.
# Deliberately a refusal with a hint rather than a second connect flow: running
# the SSH sequence from here would mean a second checklist, a second set of
# password/host-key modals and a session boundary (``_adopt_remote`` ends the
# conversation) hidden behind a button that says "attach a monitor" - which is
# exactly the "hidden multi-step action" the SSH tab refuses to be (brief 3.2).
MONITOR_CONNECT_FIRST = (
    "connect the Executor to {name} first - the Monitor tab rides that same connection"
)


class MonitorDialog:
    """What the Monitor tab is showing, and every transition between them.

    :class:`ConnectDialog`'s arrangement, and its four phases, over a form that
    asks a different question. Constructed per visit; the link it produces
    outlives it on the view, exactly as the SSH tab's host does.
    """

    def __init__(
        self,
        *,
        saved: Sequence[TargetRow] = (),
        ssh_targets: Sequence[TargetRow] = (),
        mode: str = MODE_DIRECT,
        host: str = "",
        port: str = "",
        token: str = "",
        via: str = "",
        attached: str = "",
    ) -> None:
        self.saved = list(saved)
        # The saved [remote.<name>] tables, offered as the "Via SSH" dropdown:
        # a monitor behind a tunnel is behind a machine this PC already knows
        # how to reach, and inventing a second place to describe one would be
        # inventing a second place to get it wrong.
        self.ssh_targets = list(ssh_targets)
        self.mode = mode if mode in MONITOR_MODES else MODE_DIRECT
        self.host = host
        self.port = port
        self.token = token
        self.via = via
        # The peer of the monitor currently attached, "" when the screen being
        # watched is this machine's. What arms Disconnect, and what the header
        # line says.
        self.attached = attached
        self.phase = PHASE_FORM
        self.error = ""
        self.failure = ""
        self.connected = ""
        self.save_name = ""
        self.can_save = False
        self.saved_note = ""

    # -- the form --------------------------------------------------------------

    def select(self, key: str) -> None:
        """A saved row was clicked: fill the form, and do not dial.

        One button doing one thing, the SSH picker's rule (brief 3.2) - and
        here it matters more, because the action on the other side of it swaps
        the screen out from under a running loop.
        """
        for row in self.saved:
            if row.key != key:
                continue
            target = monitor_of_row(row)
            self.mode = MODE_SSH if target.via else MODE_DIRECT
            self.host = target.host
            self.port = str(target.port) if target.port else ""
            self.via = target.via
            self.error = ""
            return

    def set_fields(self, mode: str, host: str, port: str, token: str, via: str) -> None:
        self.mode = mode if mode in MONITOR_MODES else MODE_DIRECT
        self.host = host.strip()
        self.port = port.strip()
        self.token = token.strip()
        self.via = via.strip()

    def port_number(self) -> int:
        """The port as a number, with the default filled in for an empty box.

        A Via-SSH form leaves it blank far more often than a direct one does -
        the far side is a monitor on its own loopback and 7777 is where it is -
        so the blank means the default rather than a validation error.
        """
        if not self.port:
            return DEFAULT_MONITOR_PORT
        try:
            return int(self.port)
        except ValueError:
            return 0

    def target(self, name: str = "") -> MonitorTarget:
        """What the form describes, as the config's own value type."""
        return MonitorTarget(
            name=name,
            host=self.host or (MONITOR_LOOPBACK if self.mode == MODE_SSH else ""),
            port=self.port_number(),
            token=self.token,
            via=self.via if self.mode == MODE_SSH else "",
        )

    def is_saved(self) -> bool:
        """Is this exactly a target this PC already has saved?

        By ADDRESS rather than by name, because nothing in this form is a name:
        the SSH tab can ask "is what you typed a saved target?" only because
        what you type there IS the target string.
        """
        proposed = self.target()
        return any(
            monitor_of_row(row) == replace(proposed, name=row.name, token="")
            for row in self.saved
        )

    def validate(self) -> str:
        """Why the dial cannot start yet, or "".

        Local mode is always ready: there is no address to get wrong, because
        the launcher picks the port and the token comes off the shared file
        (§10.1). A validation that asked for anything here would be asking the
        user to describe a machine they are standing at.
        """
        if self.mode == MODE_LOCAL:
            return ""
        if self.mode == MODE_SSH:
            if not self.via:
                return MONITOR_MISSING_VIA
        elif not self.host:
            return MONITOR_MISSING_HOST
        if not 1 <= self.port_number() <= 65_535:
            return MONITOR_BAD_PORT
        if self.token and len(self.token) != TOKEN_CHARS:
            return MONITOR_BAD_TOKEN.format(n=len(self.token))
        return ""

    def begin(self) -> bool:
        """Arm the dial. False (with ``error`` set) if the form is not ready."""
        self.error = self.validate()
        if self.error:
            return False
        self.phase = PHASE_RUNNING
        self.failure = ""
        return True

    def edit(self) -> None:
        """"Edit": back to the form with what was attempted still in it."""
        self.phase = PHASE_FORM
        self.error = ""

    # -- the run ---------------------------------------------------------------

    def failed(self, message: str) -> None:
        """The dial is over and there is no link.

        One message, shown whole. Every refusal that can arrive here already
        names the thing to do about it - a busy monitor names the brain that got
        there first, a version skew names both installs, a bad token says so
        without saying which half was wrong (9.1) - and re-wording any of them
        here would be this shell guessing at a fact the far side stated.
        """
        self.phase = PHASE_FAILED
        self.failure = message

    def succeeded(self, *, peer: str, can_save: bool) -> None:
        self.phase = PHASE_DONE
        self.connected = peer
        self.attached = peer
        self.can_save = can_save
        self.save_name = _suggest_monitor_name(self) if can_save else ""

    def detached(self) -> None:
        """The link was dropped on purpose: this machine's screen again."""
        self.phase = PHASE_FORM
        self.attached = ""
        self.connected = ""
        self.can_save = False
        self.error = ""
        self.failure = ""
        self.saved_note = ""

    def saved_as(self, name: str) -> None:
        self.can_save = False
        self.saved_note = f"saved as [monitor.{name}] in the global config"

    # -- the page's view of all of it -----------------------------------------

    def event(self) -> dict[str, Any]:
        """The whole tab in one event - :meth:`ConnectDialog.event`'s contract."""
        return {
            "open": True,
            "phase": self.phase,
            "mode": self.mode,
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "via": self.via,
            "saved": [_row(row) for row in self.saved],
            "ssh": [_row(row) for row in self.ssh_targets],
            "error": self.error,
            "failure": self.failure,
            "connected": self.connected,
            "attached": self.attached,
            "can_save": self.can_save,
            "save_name": self.save_name,
            "saved_note": self.saved_note,
            "busy": self.phase == PHASE_RUNNING,
        }


def monitor_of_row(row: TargetRow) -> MonitorTarget:
    """A picker row back into the target it was made from.

    The row carries what the page needed to DRAW; this rebuilds what the form
    needs to be filled with, off the one string that holds it all
    (:meth:`MonitorTarget.describe`). Parsing our own rendering rather than
    carrying a second copy of the fields through the page keeps one shape on
    the wire - and the rendering is ours, so it cannot drift out from under us.

    The token is deliberately NOT in a row and never comes back through one: a
    picker list is a thing a user screenshots. The view fills it in off the
    config when the row is selected (``GuiView.monitor_select``).
    """
    detail = row.detail
    via = ""
    if detail.startswith("via "):
        via, _, detail = detail[4:].partition(" -> ")
    host, _, port = detail.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return MonitorTarget(
        name=row.name,
        host=host,
        port=int(port) if port.isdigit() else 0,
        via=via,
    )


def _suggest_monitor_name(dialog: MonitorDialog) -> str:
    """A ``[monitor.<name>]`` table name proposed from the form.

    The SSH hop when there is one (the monitor on a machine you already named
    is "that machine's monitor"), otherwise the host - punctuation flattened,
    exactly as :func:`_suggest_name` does it.
    """
    base = dialog.via or dialog.host or "monitor"
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in base).strip("-")
    return cleaned.lower() or "monitor"


__all__ = [
    "APPROVAL_POLICY",
    "MISSING_ROOT",
    "MISSING_TARGET",
    "MODE_DIRECT",
    "MODE_LOCAL",
    "MODE_SSH",
    "MONITOR_BAD_PORT",
    "MONITOR_BAD_TOKEN",
    "MONITOR_CONNECT_FIRST",
    "MONITOR_MODES",
    "MONITOR_MISSING_HOST",
    "MONITOR_MISSING_VIA",
    "NO_RULESET",
    "PHASE_DONE",
    "PHASE_FAILED",
    "PHASE_FORM",
    "PHASE_RUNNING",
    "RULESET_FROM",
    "STDIO_ON_TARGET",
    "ConnectDialog",
    "MonitorDialog",
    "RemoteConnect",
    "RemoteRuntime",
    "TargetRow",
    "alias_rows",
    "monitor_of_row",
    "monitor_rows",
    "policy_lines",
    "saved_rows",
]
