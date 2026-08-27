"""The SSH connect dialog: the GUI-only surface, with no network and no window.

``docs/design/ui-briefs/ssh-connect.md`` is the contract and ``docs/design/
gui.md`` §4 holds the six ratified answers. What is exercised here is everything
this shell DECIDES - the picker's two sources, when Connect may be pressed, the
checklist's four row states, the three ways out of a failure, the save offer and
the target-owns-policy banner - with :func:`agentclip.executor.hosts.connect.connect_remote`
replaced by a script. The sequence itself is pinned next door
(``tests/executor/hosts/test_connect.py``), which is the point of it living down there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentclip.cli import make_engine_factory
from agentclip.config import Config, RemoteTarget, load_config, project_permissions_path
from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.fake import FakeUIMonitor
from agentclip.engine.link.wire import EngineLinkError
from agentclip.executor.hosts.connect import (
    CHECKLIST_STEPS,
    CONNECT_STEPS,
    STEP_CONNECT,
    STEP_ENGINE,
    STEP_PROBE,
    STEP_RESOLVE,
    STEP_ROOT,
    ConnectedRemote,
    ConnectError,
    StepEvent,
)
from agentclip.shell.app.monitor_launch import LaunchLocal
from agentclip.shell.chat.remote import (
    APPROVAL_POLICY,
    MISSING_ROOT,
    MISSING_TARGET,
    ConnectDialog,
    RemoteConnect,
    policy_lines,
    saved_rows,
)
from agentclip.shell.chat.view import GuiView
from agentclip.shell.webview.bridge import Bridge
from tests.shell.chat.conftest import (
    FakeLauncher,
    Harness,
    Recorder,
    attach,
    fake_monitor,
    settle,
)

REMOTE_ROOT = "/home/dev/app"
SECRET = "hunter2-never-written"


# -- the machine on the other end ----------------------------------------------


class LinkHost:
    """What the sidebar's indicator reads: SshHost's three public facts."""

    def __init__(self) -> None:
        self.target = "dev@box"
        self.name = "ssh:box"
        self.connected = True
        self.reconnects = 0
        self.redials = 0

    def reconnect(self) -> bool:
        self.redials += 1
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False


@dataclass
class FakeRuntime:
    """``cli.GuiRuntime``, structurally (``chat/remote.py:RemoteRuntime``)."""

    project_root: Path
    config: Config
    engine_factory: Any
    mcp_manager: Any
    skills: Any
    host: Any
    target: str


class RemoteHarness(Harness):
    """The conftest harness with a REAL scheduler behind ``spawn``.

    The shared one closes every coroutine handed to it, which is right for the
    surfaces whose flows are one call deep. This one is a connect: the flow runs
    on a worker thread, hops back to open modals and finishes by rebinding the
    controller, so the loop has to be the real one.
    """

    def __init__(
        self,
        view: GuiView,
        bridge: Bridge,
        recorder: Recorder,
        monitor: FakeUIMonitor,
        launcher: FakeLauncher,
    ) -> None:
        super().__init__(view, bridge, recorder, monitor, launcher)
        self.tasks: list[asyncio.Task[Any]] = []

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.scheduled.append(getattr(coro, "__qualname__", repr(coro)))
        self.tasks.append(asyncio.get_event_loop().create_task(coro))


@pytest.fixture
def remote_root(tmp_path: Path) -> Path:
    """A stand-in for the project on the box - a real directory, so the config
    load at step 6 has something to read, exactly as it would over there."""
    root = tmp_path / "box" / "app"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def global_config(tmp_path: Path) -> Path:
    path = tmp_path / "global.toml"
    path.write_text(
        f'[remote.box]\nhost = "10.0.0.5"\nuser = "dev"\nroot = "{REMOTE_ROOT}"\n'
        '[remote.spare]\nhost = "10.0.0.6"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def ssh_config(tmp_path: Path) -> Path:
    path = tmp_path / "ssh_config"
    path.write_text(
        "Host alpha\n  HostName 10.0.0.1\n"
        "Host *.example.com\n  User dev\n"
        "Match host beta\n  User x\n"
        "Host box\n  HostName 10.0.0.5\n",  # already a saved target: not offered twice
        encoding="utf-8",
    )
    return path


@pytest.fixture
def connected(remote_root: Path, tmp_path: Path) -> ConnectedRemote:
    host = LinkHost()
    return ConnectedRemote(
        host=host,  # type: ignore[arg-type]
        target=RemoteTarget(name="10.0.0.5", host="10.0.0.5", user="dev", root=REMOTE_ROOT),
        os_name="Linux (ssh)",
        project_root=remote_root,
        home=Path("/home/dev"),
        data_root=tmp_path / "state",
        config=load_config(remote_root, global_config_path=tmp_path / "none.toml"),
        environ={"HOME": "/home/dev"},
    )


@pytest.fixture
def harness(
    project: Path,
    tmp_path: Path,
    global_config: Path,
    ssh_config: Path,
    connected: ConnectedRemote,
) -> RemoteHarness:
    recorder = Recorder()
    bridge = Bridge(recorder)
    holder: dict[str, RemoteHarness] = {}
    config = load_config(project, global_config_path=global_config)
    built: list[FakeRuntime] = []
    # What the TARGET's MCP runtime is, for the one test that cares. A cell
    # rather than a constructor argument because `cli.main` builds this per
    # connect: it is the box's runtime, so it cannot exist before the dial.
    remote_mcp: list[Any] = [None]
    # How the engine launch goes. ``cli.build_runtime`` opens an exec channel on
    # the box and shakes hands with ``agentclip-engine`` over it - so from this
    # shell's side, "the target has no engine on it" is exactly an
    # ``EngineLinkError`` out of ``build`` (docs/design/remote-executor.md
    # §2.12), and this cell is how a test asks for one.
    launch_error: list[EngineLinkError | None] = [None]

    def build(remote: ConnectedRemote) -> FakeRuntime:
        if launch_error[0] is not None:
            raise launch_error[0]
        factory = make_engine_factory(lambda: remote.config, remote.project_root)
        runtime = FakeRuntime(
            project_root=remote.project_root,
            config=remote.config,
            engine_factory=factory,
            mcp_manager=remote_mcp[0],
            # The target's skills come off the same factory, which is what
            # ``cli.build_runtime`` does with the RemoteEngine's.
            skills=factory.skills,
            host=remote.host,
            target=remote.host.target,
        )
        built.append(runtime)
        return runtime

    provider = select_provider("manual")
    monitor = fake_monitor(config, provider)
    launcher = FakeLauncher()
    view = GuiView(
        bridge,
        config=config,
        provider=provider,
        engine_factory=make_engine_factory(lambda: config, project),
        project_root=project,
        profile_root=tmp_path / "profiles",
        global_config_path=global_config,
        # A local child, attached: the conftest harness's own default, and the
        # state every Executor-tab story here starts from (§10.2). The attach
        # itself is by hand below, for the reason the conftest gives.
        monitor_target=LaunchLocal(),
        launcher=launcher,
        schedule=lambda coro: holder["h"].schedule(coro),
        on_exit=lambda: holder["h"].on_exit(),
        remote=RemoteConnect(
            local_root=project,
            build=build,  # type: ignore[arg-type]
            global_config_path=global_config,
            ssh_config_path=ssh_config,
        ),
    )
    attach(view, monitor, launcher)
    holder["h"] = RemoteHarness(view, bridge, recorder, monitor, launcher)
    holder["h"].built = built  # type: ignore[attr-defined]
    holder["h"].remote_mcp = remote_mcp  # type: ignore[attr-defined]
    holder["h"].launch_error = launch_error  # type: ignore[attr-defined]
    return holder["h"]


def script(connected: ConnectedRemote, *, fail: str = "", asks: str = "") -> Any:
    """A ``connect_remote`` that reports the real beats and answers no network.

    ``fail`` names the step it dies on - which is also the only place a note
    about it is produced, because that is how the real one behaves.
    """

    def fake(target: str, root: str | None, **kwargs: Any) -> ConnectedRemote:
        report = kwargs["on_step"]
        prompts = kwargs["prompts"]
        for step in CONNECT_STEPS:
            report(StepEvent(step, "running"))
            if step == fail:
                report(StepEvent(step, "failed", f"{step} went wrong on {target}"))
                raise ConnectError(step, f"{step} went wrong on {target}")
            if step == STEP_CONNECT and asks == "password":
                for _ in range(3):
                    if prompts.password(f"password for {target}: ") == SECRET:
                        break
            refused = step == STEP_CONNECT and asks == "hostkey" and not prompts.host_key(
                target, "ssh-ed25519", "SHA256:abc123"
            )
            if refused:
                note = f"the host key for {target} was not accepted"
                report(StepEvent(step, "failed", note))
                raise ConnectError(step, note)
            if step == STEP_CONNECT and asks == "keyboard":
                prompts.keyboard_interactive(
                    "Two-factor", "Enter the code from your app", (("Verification code: ", False),)
                )
            report(StepEvent(step, "ok"))
        return connected

    return fake


@pytest.fixture
def dial(monkeypatch: pytest.MonkeyPatch, connected: ConnectedRemote) -> Any:
    """Install a scripted sequence; the returned callable re-scripts it."""

    def install(**kwargs: Any) -> None:
        monkeypatch.setattr("agentclip.shell.chat.view.connect_remote", script(connected, **kwargs))

    install()
    return install


async def run_connect(harness: RemoteHarness) -> None:
    """Press Connect and let the flow (and its worker thread) finish."""
    harness.view.connect_start()
    for _ in range(60):
        await asyncio.sleep(0.005)
        if not harness.view._connecting and not any(not t.done() for t in harness.tasks):
            break
    await settle(5)


def last_connect(harness: RemoteHarness) -> dict[str, Any]:
    return harness.flush().last("connect")


def rows(event: dict[str, Any]) -> dict[str, str]:
    return {row["step"]: row["state"] for row in event["steps"]}


# == the picker ================================================================


async def test_the_dialog_offers_saved_targets_and_ssh_config_aliases(
    harness: RemoteHarness,
) -> None:
    harness.view.open_connect()
    event = last_connect(harness)
    assert event["open"] is True and event["phase"] == "form"
    assert [row["name"] for row in event["saved"]] == ["box", "spare"]
    assert event["saved"][0]["detail"] == "dev@10.0.0.5"
    assert event["saved"][0]["root"] == REMOTE_ROOT


async def test_wildcards_and_match_blocks_never_reach_the_picker(
    harness: RemoteHarness,
) -> None:
    """gui.md §4 ruling 3. ``*.example.com`` is a rule and ``Match host beta``
    is a condition; neither is a machine one can connect to."""
    harness.view.open_connect()
    names = [row["name"] for row in last_connect(harness)["aliases"]]
    assert names == ["alpha"]  # not "*.example.com", not "beta"...
    assert "box" not in names  # ...and not the one already saved by that name


async def test_selecting_a_saved_target_prefills_and_connects_nothing(
    harness: RemoteHarness, dial: Any
) -> None:
    harness.view.open_connect()
    harness.view.connect_select("saved:box")
    event = last_connect(harness)
    assert (event["target"], event["root"]) == ("box", REMOTE_ROOT)
    assert event["phase"] == "form"  # one Connect action, one thing
    assert rows(event) == dict.fromkeys(CHECKLIST_STEPS, "pending")


async def test_selecting_an_alias_leaves_the_root_to_be_typed(
    harness: RemoteHarness,
) -> None:
    """ssh_config knows how to REACH a machine and nothing about which directory
    on it is the project (brief §3.2)."""
    harness.view.open_connect()
    harness.view.connect_select("alias:alpha")
    event = last_connect(harness)
    assert (event["target"], event["root"]) == ("alpha", "")


# == the form ==================================================================


async def test_connect_is_refused_without_a_target(harness: RemoteHarness) -> None:
    harness.view.open_connect()
    harness.view.connect_start()
    event = last_connect(harness)
    assert event["error"] == MISSING_TARGET
    assert event["phase"] == "form"


async def test_connect_is_refused_when_nothing_supplies_a_root(
    harness: RemoteHarness,
) -> None:
    """The one thing a form CAN know. Whether the root exists is step 4's
    business and needs a live SFTP session, so a bad path is a retry state."""
    harness.view.open_connect()
    harness.view.connect_fields("pi@10.0.0.9", "")
    harness.view.connect_start()
    assert last_connect(harness)["error"] == MISSING_ROOT


async def test_a_saved_targets_root_satisfies_the_form(harness: RemoteHarness) -> None:
    harness.view.open_connect()
    harness.view.connect_fields("box", "")
    assert harness.view._dialog is not None
    assert harness.view._dialog.validate() == ""


async def test_the_preview_agrees_with_the_grammar_the_backend_parses(
    harness: RemoteHarness,
) -> None:
    harness.view.open_connect()
    harness.view.connect_select("saved:spare")
    harness.view.connect_fields("pi@10.0.0.9:2200", REMOTE_ROOT)
    harness.view.connect_select("saved:spare")  # any repaint
    harness.view.connect_fields("pi@10.0.0.9:2200", REMOTE_ROOT)
    harness.view.connect_start()
    assert last_connect(harness)["preview"] == "pi@10.0.0.9:2200"


async def test_the_preview_unwraps_a_pasted_ssh_command_the_way_resolve_does(
    harness: RemoteHarness,
) -> None:
    """A user pastes their whole command line; the preview must show what will
    actually be dialled, not the string with the verb still on it."""
    harness.view.open_connect()
    harness.view.connect_fields("ssh wsl", REMOTE_ROOT)
    harness.view.connect_select("saved:spare")  # any repaint
    harness.view.connect_fields("ssh wsl", REMOTE_ROOT)
    harness.view.connect_start()
    assert last_connect(harness)["preview"] == "wsl"


# == the checklist =============================================================


async def test_a_clean_connect_ticks_all_seven_steps_in_order(
    harness: RemoteHarness, dial: Any
) -> None:
    """Six from ``connect_remote``, and a seventh the sequence does not run.

    Starting ``agentclip-engine`` on the target is what a remote session now IS
    (docs/design/remote-executor.md §2.12), so it is a row a human watches -
    fed by ``cli.build_runtime`` rather than by the sequence, which may not
    import a protocol.
    """
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    event = last_connect(harness)
    assert event["phase"] == "done"
    assert [row["step"] for row in event["steps"]] == list(CHECKLIST_STEPS)
    assert CHECKLIST_STEPS[-1] == STEP_ENGINE
    assert rows(event) == dict.fromkeys(CHECKLIST_STEPS, "ok")


@pytest.mark.parametrize("fails", [STEP_RESOLVE, STEP_CONNECT, STEP_PROBE, STEP_ROOT])
async def test_a_failure_marks_its_own_row_and_leaves_the_rest_pending(
    harness: RemoteHarness, dial: Any, fails: str
) -> None:
    """Brief §3.4: later stages stay pending, never skipped-with-a-checkmark -
    a row that ticked would say the opposite of what happened."""
    dial(fail=fails)
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    event = last_connect(harness)
    assert event["phase"] == "failed"
    assert event["failed_step"] == fails
    assert fails in event["failure"]
    marks = rows(event)
    after = CHECKLIST_STEPS[CHECKLIST_STEPS.index(fails) + 1 :]
    assert marks[fails] == "failed"
    # ...including the engine row: a connect that never finished never launched
    # anything on the target, and a checkmark there would say it had.
    assert all(marks[step] == "pending" for step in after)


async def test_a_target_with_no_engine_on_it_fails_the_engine_row(
    harness: RemoteHarness, dial: Any
) -> None:
    """The one failure that happens AFTER the six steps are green.

    Every beat of the dial worked - the box is up, authenticated, the root is
    real, its config was read - and there is still no session, because nothing
    over there answers the handshake. The row shows the classified sentence
    verbatim (§2.12): the target by name, both spellings that were tried, and
    the command that fixes it.
    """
    harness.launch_error[0] = EngineLinkError(  # type: ignore[attr-defined]
        "link_closed",
        "agentclip-engine is not on the non-interactive PATH of dev@box (tried"
        " 'agentclip-engine' and '~/.local/bin/agentclip-engine') - install it with"
        " e.g. `uv tool install agentclip`, or symlink it into /usr/local/bin",
    )
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)

    event = last_connect(harness)
    assert event["phase"] == "failed"
    assert event["failed_step"] == STEP_ENGINE
    assert "is not on the non-interactive PATH of dev@box" in event["failure"]
    assert "'~/.local/bin/agentclip-engine'" in event["failure"]
    assert "uv tool install agentclip" in event["failure"]
    # ...and the six that DID work still say so, so the user can see how far it got.
    assert all(rows(event)[step] == "ok" for step in CONNECT_STEPS)
    # Nothing was adopted: the window is still on the machine it was already on.
    assert harness.view._remote_target == ""


async def test_a_wire_version_mismatch_names_both_installs(
    harness: RemoteHarness, dial: Any
) -> None:
    """The other shape a dead launch takes, and the one the far side ANSWERED.

    ``hello()`` already built the sentence (§2.9); this shell must not improve
    on it - the two ``agentclip`` versions are the only half a human can act on.
    """
    harness.launch_error[0] = EngineLinkError(  # type: ignore[attr-defined]
        "version_mismatch",
        "the engine on the target speaks wire v2 (agentclip 0.1.0); this AgentClip"
        " speaks wire v1 (agentclip 0.4.2) - update the target's install",
    )
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)

    event = last_connect(harness)
    assert event["failed_step"] == STEP_ENGINE
    assert "agentclip 0.1.0" in event["failure"] and "agentclip 0.4.2" in event["failure"]
    # The kind is plumbing, not a sentence: it must not reach the checklist.
    assert "version_mismatch:" not in event["failure"]


async def test_a_failed_engine_launch_can_be_retried_in_place(
    harness: RemoteHarness, dial: Any
) -> None:
    """The whole point of the surface, applied to the newest failure: the user
    installs the engine on the box and presses Retry, without relaunching."""
    harness.launch_error[0] = EngineLinkError(  # type: ignore[attr-defined]
        "link_closed", "agentclip-engine is not installed on dev@box"
    )
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    assert last_connect(harness)["phase"] == "failed"

    harness.launch_error[0] = None  # they installed it
    await run_connect(harness)
    event = last_connect(harness)
    assert event["phase"] == "done"
    assert rows(event)[STEP_ENGINE] == "ok"


async def test_a_failure_offers_edit_back_to_the_form_with_the_values_in_it(
    harness: RemoteHarness, dial: Any
) -> None:
    """The fix for the commonest real failure: a typo'd root, without retyping
    the hostname too (brief §3.8)."""
    dial(fail=STEP_ROOT)
    harness.view.open_connect()
    harness.view.connect_fields("box", "/wrong")
    await run_connect(harness)
    harness.view.connect_edit()
    event = last_connect(harness)
    assert event["phase"] == "form"
    assert (event["target"], event["root"]) == ("box", "/wrong")
    assert event["failed_step"] == STEP_ROOT  # what went wrong is still on screen


async def test_retry_re_runs_the_whole_sequence_in_place(
    harness: RemoteHarness, dial: Any
) -> None:
    """The whole point of the surface: no relaunch (brief §1)."""
    dial(fail=STEP_CONNECT)
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    assert last_connect(harness)["phase"] == "failed"

    dial()  # the box came back
    await run_connect(harness)
    assert last_connect(harness)["phase"] == "done"


async def test_cancel_closes_the_dialog_and_not_the_window(
    harness: RemoteHarness,
) -> None:
    harness.view.open_connect()
    harness.view.connect_cancel()
    assert last_connect(harness)["open"] is False
    assert harness.exits == 0


# == the three questions a dial can ask ========================================


async def test_a_password_prompt_is_a_modal_and_says_which_attempt_it_is(
    harness: RemoteHarness, dial: Any
) -> None:
    """Three attempts, and the count is ``SshHost._PASSWORD_ATTEMPTS``' - this
    dialog adds no fourth (brief §3.5)."""
    dial(asks="password")
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    harness.view.connect_start()

    hints: list[str] = []
    for _ in range(80):
        await asyncio.sleep(0.005)
        prompts = [e for e in harness.flush().of_type("modal") if e["modal"] == "connect_password"]
        if len(prompts) > len(hints):
            hints.append(prompts[-1]["hint"])
            harness.view.answer_prompt(prompts[-1]["prompt_id"], SECRET)
        if not harness.view._connecting:
            break
    await settle(5)

    assert hints and hints[0].startswith("attempt 1 of 3")
    assert last_connect(harness)["phase"] == "done"


async def test_no_event_ever_carries_the_password(
    harness: RemoteHarness, dial: Any
) -> None:
    """gui.md §4 ruling 2, asserted rather than asserted-to: the secret goes one
    way, into ``SshHost._password``, and nothing paints it back."""
    dial(asks="password")
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    harness.view.connect_start()
    for _ in range(80):
        await asyncio.sleep(0.005)
        prompts = [e for e in harness.flush().of_type("modal") if e["modal"] == "connect_password"]
        if prompts and harness.view._connecting:
            harness.view.answer_prompt(prompts[-1]["prompt_id"], SECRET)
        if not harness.view._connecting:
            break
    await settle(5)
    assert SECRET not in "".join(harness.flush().scripts)
    assert SECRET not in (harness.view._global_config_path).read_text(encoding="utf-8")


async def test_the_host_key_question_is_openssh_s_own_words(
    harness: RemoteHarness, dial: Any
) -> None:
    dial(asks="hostkey")
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    harness.view.connect_start()
    modal = await _await_modal(harness, "connect_hostkey")
    assert "authenticity of host 'box'" in modal["title"]
    assert "SHA256:abc123" in modal["body"]
    harness.view.answer_prompt(modal["prompt_id"], True)
    await _finish(harness)
    assert last_connect(harness)["phase"] == "done"


async def test_declining_a_host_key_fails_the_attempt(
    harness: RemoteHarness, dial: Any
) -> None:
    """Never a silent downgrade to unauthenticated browsing (brief §3.6)."""
    dial(asks="hostkey")
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    harness.view.connect_start()
    modal = await _await_modal(harness, "connect_hostkey")
    harness.view.answer_prompt(modal["prompt_id"], False)
    await _finish(harness)
    event = last_connect(harness)
    assert event["phase"] == "failed"
    assert "not accepted" in event["failure"]


async def test_the_2fa_prompt_is_one_field_per_challenge(
    harness: RemoteHarness, dial: Any
) -> None:
    """gui.md §4 ruling 4's dialog. Not reachable from a real dial yet - the
    paramiko wiring is a TODO in ``SshHost._authenticate`` - so what is pinned
    is the CONTRACT the day it is: paramiko's handler shape, answers in order."""
    dial(asks="keyboard")
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    harness.view.connect_start()
    modal = await _await_modal(harness, "connect_keyboard")
    assert modal["body"] == "Enter the code from your app"
    assert modal["fields"] == [{"prompt": "Verification code: ", "echo": False}]
    harness.view.answer_prompt(modal["prompt_id"], ["123456"])
    await _finish(harness)
    assert last_connect(harness)["phase"] == "done"


async def _await_modal(harness: RemoteHarness, kind: str) -> dict[str, Any]:
    for _ in range(80):
        await asyncio.sleep(0.005)
        found = [e for e in harness.flush().of_type("modal") if e["modal"] == kind]
        if found:
            return found[-1]
    raise AssertionError(f"no {kind} modal was opened")


async def _finish(harness: RemoteHarness) -> None:
    for _ in range(80):
        await asyncio.sleep(0.005)
        if not harness.view._connecting:
            break
    await settle(5)


# == what a successful connect changes =========================================


async def test_a_connect_points_the_session_at_the_remote_root(
    harness: RemoteHarness, dial: Any, remote_root: Path
) -> None:
    """One session, one host: the state a fresh ``--ssh`` launch lands in."""
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    assert harness.view._project_root == remote_root
    assert harness.view._remote_target == "dev@box"
    assert harness.view._controller._project_root == remote_root
    assert harness.flush().last("sidebar")["remote"] == "dev@box"


class FakeMcpSource:
    """The ``McpStatusSource`` shape and nothing else - the target's runtime."""

    def __init__(self, *rows: Any) -> None:
        self._rows = rows
        self.hooked = 0

    def statuses(self) -> tuple[Any, ...]:
        return self._rows

    def set_status_hook(self, cb: Any) -> None:
        self.hooked += 1


@dataclass(frozen=True)
class McpRow:
    """Duck-typed status row: four names, no ``executor.mcp`` import anywhere in
    the GUI's graph (tests/test_layering.py)."""

    name: str
    state: str
    detail: str = ""
    tool_count: int = 0


async def test_a_connect_points_mcp_at_the_targets_runtime(
    harness: RemoteHarness, dial: Any
) -> None:
    """MCP servers spawn where the engine runs (remote-executor.md §2.7), so a
    connect has to move the MCP source with the root and the config.

    Pinned end to end through `/mcp` before any session exists - the one path
    that used to answer out of the machine the window had just left, because
    ``rebind`` took three ingredients and MCP was the fourth.
    """
    harness.remote_mcp[0] = FakeMcpSource(  # type: ignore[attr-defined]
        McpRow("on-the-box", "connected", tool_count=9)
    )
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)

    # The sidebar's block repainted off the target's rows the moment it landed...
    assert harness.flush().last("mcp")["rows"][0]["name"] == "on-the-box"

    harness.view.controller.submit_message("/mcp")
    await settle(5)
    notes = [
        event["text"]
        for event in harness.flush().of_type("transcript")
        if str(event.get("text", "")).startswith("MCP servers:")
    ]
    assert notes and "on-the-box · connected · 9 tools" in notes[-1]


async def test_the_policy_banner_names_the_machine_and_where_policy_comes_from(
    harness: RemoteHarness, dial: Any
) -> None:
    """Brief §3.9: a user with a carefully tuned permissions.json on THIS PC sees
    none of it apply the moment they connect - silence about that is the
    footgun, so the banner states both halves."""
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    lines = last_connect(harness)["policy"]
    assert any("No permissions.json on dev@box" in line for line in lines)
    # ...and the whole of [approval] is read over there too, now that the engine
    # is: this PC's config.toml is not even reachable from the target
    # (docs/design/remote-executor.md §2.5, §2.6).
    assert APPROVAL_POLICY.format(target="dev@box") in lines
    assert not any("this PC's config.toml" in line for line in lines)


async def test_the_project_block_keeps_saying_it_after_the_dialog_closes(
    harness: RemoteHarness, dial: Any
) -> None:
    """gui.md §4 ruling 6: the banner is a moment, this is the standing fact."""
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    harness.view.connect_cancel()
    lines = harness.flush().last("sidebar")["remote_lines"]
    assert lines[0] == "dev@box"
    assert lines[1].startswith("link live")


async def test_the_indicator_says_the_link_is_lost_without_dialling_to_find_out(
    harness: RemoteHarness, dial: Any
) -> None:
    """The reconnect model is lazy and stays lazy (remote-ssh.md decision 5):
    the flag is flipped by an OPERATION, never by a poll."""
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    harness.view._host.connected = False
    harness.view._push_sidebar()
    lines = harness.flush().last("sidebar")["remote_lines"]
    assert lines[1].startswith("link lost")
    assert harness.view._host.redials == 0


async def test_reconnect_now_spends_the_lazy_redial_early(
    harness: RemoteHarness, dial: Any
) -> None:
    """gui.md §4 ruling 5. The same ``_ensure`` the next operation would have
    called, which is why it is ``SshHost.reconnect`` and not a second dial."""
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    host = harness.view._host
    host.connected = False
    harness.view.reconnect_now()
    await settle(5)
    for _ in range(40):
        await asyncio.sleep(0.005)
        if host.redials:
            break
    assert host.redials == 1
    assert host.connected


async def test_reconnect_now_on_a_local_session_refuses_out_loud(
    harness: RemoteHarness,
) -> None:
    harness.view.reconnect_now()
    assert "nothing to re-dial" in harness.flush().last("toast")["message"]


async def test_connecting_mid_turn_is_refused(harness: RemoteHarness, dial: Any) -> None:
    """A turn in flight belongs to the machine it started on."""
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    harness.view._last_view = _busy_view()
    harness.view.connect_start()
    assert "a turn is running" in harness.flush().last("toast")["message"]
    assert last_connect(harness)["phase"] == "form"


def _busy_view() -> Any:
    @dataclass
    class Busy:
        session_active: bool = True
        busy: bool = True
        pending_approval: bool = False
        awaiting_answer: bool = False
        snapshot: Any = None

    return Busy()


# == the save offer ============================================================


async def test_a_manual_connect_offers_to_save_the_target(
    harness: RemoteHarness, dial: Any, global_config: Path
) -> None:
    """gui.md §4 ruling 1: global config only - a target definition is how THIS
    PC finds that one, never something the project has an opinion about."""
    harness.view.open_connect()
    harness.view.connect_fields("dev@10.0.0.5", REMOTE_ROOT)
    await run_connect(harness)
    event = last_connect(harness)
    assert event["can_save"] is True
    assert event["save_name"] == "10-0-0-5"

    harness.view.connect_save("dev-box")
    text = global_config.read_text(encoding="utf-8")
    assert "[remote.dev-box]" in text
    assert 'root = "/home/dev/app"' in text
    saved = last_connect(harness)
    assert saved["can_save"] is False
    assert "remote.dev-box" in saved["saved_note"]


async def test_a_saved_target_is_not_offered_for_saving_again(
    harness: RemoteHarness, dial: Any
) -> None:
    harness.view.open_connect()
    harness.view.connect_fields("box", REMOTE_ROOT)
    await run_connect(harness)
    assert last_connect(harness)["can_save"] is False


async def test_saving_writes_no_secret(
    harness: RemoteHarness, dial: Any, global_config: Path
) -> None:
    harness.view.open_connect()
    harness.view.connect_fields("dev@10.0.0.5", REMOTE_ROOT)
    await run_connect(harness)
    harness.view.connect_save("dev-box")
    text = global_config.read_text(encoding="utf-8")
    assert "password" not in text.lower()


# == the GUI --ssh launch ======================================================


async def test_a_pending_launch_opens_the_dialog_and_runs_it(
    project: Path,
    tmp_path: Path,
    global_config: Path,
    ssh_config: Path,
    connected: ConnectedRemote,
    harness: RemoteHarness,
    dial: Any,
) -> None:
    """A GUI ``--ssh`` launch no longer blocks on a terminal dial: the
    window is already up and the sequence runs in it, pre-filled."""
    harness.view._remote = RemoteConnect(
        local_root=project,
        build=harness.view._remote.build,  # type: ignore[union-attr]
        global_config_path=global_config,
        ssh_config_path=ssh_config,
        pending=("box", REMOTE_ROOT),
    )
    harness.view.start()
    for _ in range(80):
        await asyncio.sleep(0.005)
        if not harness.view._connecting:
            break
    await settle(5)
    event = last_connect(harness)
    assert event["target"] == "box"
    assert event["phase"] == "done"


async def test_cancelling_a_pending_launch_leaves_a_usable_local_session(
    project: Path,
    global_config: Path,
    ssh_config: Path,
    harness: RemoteHarness,
    dial: Any,
) -> None:
    """The controller is deferred so the first session belongs to the box; a
    cancel has to hand it back rather than leave a window that does nothing."""
    dial(fail=STEP_CONNECT)
    harness.view._remote = RemoteConnect(
        local_root=project,
        build=harness.view._remote.build,  # type: ignore[union-attr]
        global_config_path=global_config,
        ssh_config_path=ssh_config,
        pending=("box", REMOTE_ROOT),
    )
    harness.view.start()
    for _ in range(80):
        await asyncio.sleep(0.005)
        if not harness.view._connecting:
            break
    await settle(5)
    assert harness.view._controller_started is False
    harness.view.connect_cancel()
    await settle(5)
    assert harness.view._controller_started is True


# == the model on its own ======================================================


def test_the_dialog_model_needs_no_view_at_all(
    project: Path, global_config: Path
) -> None:
    """The service editor's bargain, one surface over: every decision is here,
    so the assertions cost neither a window nor a loop."""
    config = load_config(project, global_config_path=global_config)
    dialog = ConnectDialog(saved=saved_rows(config))
    assert dialog.validate() == MISSING_TARGET
    dialog.select("saved:box")
    assert (dialog.target, dialog.root) == ("box", REMOTE_ROOT)
    assert dialog.begin() is True
    dialog.step(StepEvent(STEP_RESOLVE, "ok", "dev@10.0.0.5"))
    dialog.step(StepEvent(STEP_CONNECT, "failed", "nope"))
    assert dialog.phase == "failed"
    assert rows(dialog.event())[STEP_PROBE] == "pending"


def test_the_banner_says_which_machine_starts_the_stdio_servers(
    project: Path, tmp_path: Path
) -> None:
    """Brief §3.9's last bullet, after the reversal it outlived.

    Stdio servers used to be REFUSED in a remote session, because the process
    that would have spawned them was this PC's and their argv described another
    machine. Since the engine moved to the target they start - over there, with
    the target's environment and cwd (remote-executor.md §2.7) - so the banner
    names the machine instead of apologising for a refusal that no longer
    happens. Which box a server really runs on is the same question the two
    lines above it answer.
    """
    rules = project_permissions_path(project)
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(
        '{"mcp": {"tools": {"type": "local", "command": ["node", "x.js"]}}}', encoding="utf-8"
    )
    config = load_config(project, global_config_path=tmp_path / "none.toml")
    lines = policy_lines(config, "dev@box")
    assert any("started on dev@box" in line and "tools" in line for line in lines)
    assert not any("not supported" in line for line in lines)
