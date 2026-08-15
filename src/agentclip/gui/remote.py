"""The SSH connect dialog's model: no window, no page, no toolkit.

The service editor's arrangement (``gui/service_editor.py``, parity increment 5)
applied to the one surface the TUI does not have. Everything the dialog DECIDES
is here - which targets are offered, what a form has to say before "Connect" is
allowed, which checklist row a failure lands on, what the three ways out of a
failure do, and what the target-owns-its-policy banner says - and
``gui/view.py`` keeps only the two things a model cannot do: run the connect
coroutine, and draw.

The sequence itself is NOT here. It is
:func:`agentclip.executor.hosts.connect.connect_remote`, the same function
``cli.remote_launch`` drives; this object is what stands between it and a human
who would rather not retype a shell command (``docs/design/ui-briefs/
ssh-connect.md`` §1).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentclip.app.types import EngineRequest
from agentclip.config import Config, RemoteTarget
from agentclip.engine.engine import Engine
from agentclip.executor.hosts.connect import (
    CONNECT_STEPS,
    STEP_LABELS,
    ConnectedRemote,
    ConnectError,
    StepEvent,
    describe_target,
)

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
# stated rather than defaulted: a user with a carefully tuned opencode.json on
# THIS PC sees none of it apply the moment they connect, and silence about that
# is the footgun (docs/design/remote-ssh.md, "the target owns its policy").
NO_RULESET = "No permission ruleset found on {target}; falling back to the allowlist gate"
RULESET_FROM = "Permissions and MCP servers for this session come from {source}"
APPROVAL_LOCAL = "[approval] (mode, yolo, command rules) stays on this PC"
STDIO_REFUSED = "not started - stdio MCP servers are not supported in a remote session: {names}"


class RemoteRuntime(Protocol):
    """Everything a session is built from, rebuilt against a machine.

    Structural, and produced by ``cli.main``: the engine factory, the MCP
    runtime and the session-tree pruning are launch questions, and a second
    construction site for them is a second thing to drift (the same reason
    ``run_gui`` is HANDED its factory rather than building one). What this shell
    does with it is the session boundary - "one session, one host" means the
    controller is rebuilt, not patched (remote-ssh.md decision 4).
    """

    @property
    def project_root(self) -> Path: ...
    @property
    def config(self) -> Config: ...
    @property
    def engine_factory(self) -> Callable[[EngineRequest], Engine]: ...
    @property
    def mcp_manager(self) -> Any: ...
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
    for the reason :class:`RemoteRuntime` gives. ``pending`` is
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


def policy_lines(config: Config, target_label: str) -> list[str]:
    """The target-owns-its-policy banner, from what the config load already knows.

    No new backend logic: ``Config.permission_source`` is the machine-qualified
    string ``_describe_path`` already builds for exactly this reason
    (remote-ssh.md, "Consequences to handle": the permission source shown must
    name the machine, not just the path), and the stdio refusal is a fact about
    the parsed server list rather than about any runtime.

    The stdio test is ``no url`` rather than ``isinstance(McpLocalServer)``:
    this shell reads MCP through duck-typing everywhere it touches it
    (``gui/view.py:_mcp_line``), which is what keeps ``agentclip.executor.mcp`` out of
    its import graph.
    """
    lines: list[str] = []
    if config.permission_source:
        lines.append(RULESET_FROM.format(source=config.permission_source))
    else:
        lines.append(NO_RULESET.format(target=target_label))
    lines.append(APPROVAL_LOCAL)
    stdio = [
        str(getattr(server, "name", ""))
        for server in config.mcp_servers.servers
        if not getattr(server, "url", "")
    ]
    if stdio:
        lines.append(STDIO_REFUSED.format(names=", ".join(stdio)))
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
            "steps": [self._row_of(step) for step in CONNECT_STEPS],
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
    nothing cleverer (brief §3.3). A saved name or a bare alias falls out of it
    unchanged, which is exactly what happens to it downstream too.
    """
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


__all__ = [
    "APPROVAL_LOCAL",
    "MISSING_ROOT",
    "MISSING_TARGET",
    "NO_RULESET",
    "PHASE_DONE",
    "PHASE_FAILED",
    "PHASE_FORM",
    "PHASE_RUNNING",
    "RULESET_FROM",
    "STDIO_REFUSED",
    "ConnectDialog",
    "RemoteConnect",
    "RemoteRuntime",
    "TargetRow",
    "alias_rows",
    "policy_lines",
    "saved_rows",
]
