"""RemoteLink against a REAL server process, over real pipes.

This is the wire-protocol suite docs/design/remote-executor.md section 2.2 asks
for by name. The accepted risk of the link seam is that the wire path is only
exercised remotely, and the mitigation is exactly this file: every test below
drives ``python -m agentclip.engine.link`` as a localhost subprocess, with a real
Engine in it, over its actual stdin/stdout - no fake server, no in-process
shortcut, nothing shared between the two halves but
:mod:`agentclip.engine.link.wire`.

What that buys over tests/engine/link/test_server.py (which speaks frames at
``serve()`` in-process) is the half no codec test can reach: the CLIENT. The
handshake, one call in flight per connection with the answer read inside it,
events dispatched to the link that owns them while somebody else's call is doing
the reading, a cancel written while a turn is outstanding, typed exceptions
rebuilt on this side, and a link that fails instead of hanging when the process
it was talking to stops existing.

The three isolation flags on the command line are mandatory, not tidiness: a
subprocess inherits NO monkeypatching from this process, and platformdirs ignores
env vars on Windows - so a run without them would read the developer's real
global config, real permissions.json and real skill folders. Passing paths that
do not exist is the only reliable way to keep them out.

Every blocking wait here has a deadline. The server's death is the one failure
that could otherwise wedge a read forever, and it is the thing this file is
partly here to prove does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentclip.config import Config, load_config
from agentclip.engine.engine import CallProgress
from agentclip.engine.link.factory import EngineRequest, make_engine_builder
from agentclip.engine.link.wire import EngineLinkError
from agentclip.engine.states import Decision, EngineStateError
from agentclip.protocol.composer import BudgetExceeded
from agentclip.shell.app.link import LocalLink
from agentclip.shell.app.remote_link import RemoteLink, RemoteLinkClient

# Both halves of a parity test have to agree on the chat name, and every canned
# reply has to name one, so every session here is pinned to this - through
# EngineRequest.chat_name, which the factory honours wherever it runs.
CHAT = "amber-falcon"

# One deadline for every await. A failure budget, not a timing assertion: the
# flows below answer in milliseconds, except the two that deliberately sleep.
DEADLINE = 30.0

PY = "python -c"


# == the process under test ====================================================


class _Server:
    """One ``python -m agentclip.engine.link`` subprocess and a client on it.

    The transport seam in the flesh: :class:`RemoteLinkClient` is handed two text
    streams and never learns they came from a ``Popen``. Increment 3 hands it an
    SSH exec channel's streams instead, and nothing in ``remote_link.py`` changes
    - which is the whole reason no spawning lives in ``src``.

    stderr is drained on a thread of its own, because it is the remote process's
    LOG (never protocol data) and because an assertion that can quote it is worth
    ten that can only say the link went quiet.
    """

    def __init__(self, project: Path, sandbox: Path) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentclip.engine.link",
                "--project",
                str(project),
                # The isolation. See the module docstring: a subprocess inherits
                # no monkeypatching, so these paths - none of which exist - are
                # what stands between this test and the developer's own config.
                "--global-config",
                str(sandbox / "no-such-global.toml"),
                "--home",
                str(sandbox / "home"),
                "--data-root",
                str(sandbox / "data"),
            ],
            cwd=str(sandbox),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.log: list[str] = []
        self._drain = threading.Thread(target=self._read_log, daemon=True)
        self._drain.start()
        self.client = RemoteLinkClient(self.proc.stdout, self.proc.stdin)

    def _read_log(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.log.append(line)

    def why(self) -> str:
        """The server's own account of itself, for an assertion message."""
        return "".join(self.log) or "(nothing on stderr)"

    def session(self, **kwargs: object) -> RemoteLink:
        request = EngineRequest(service="claude", chat_name=CHAT, **kwargs)  # type: ignore[arg-type]
        return self.client.build_session(request)

    def close(self) -> None:
        """EOF, then a bounded wait, then force. Never a hang in teardown."""
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            # A killed process (the death test) makes this raise; the wait below
            # is what actually matters.
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._drain.join(2)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _make_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "utils.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return root


@pytest.fixture
def remote_project(tmp_path: Path) -> Path:
    """The project the OTHER process works in. Named ``project`` on purpose: the
    parity test builds a local twin, and the bootstrap names its workdir."""
    return _make_project(tmp_path / "remote" / "project")


@pytest.fixture
def server(tmp_path: Path, remote_project: Path) -> Iterator[_Server]:
    proc = _Server(remote_project, tmp_path)
    try:
        server_id = proc.client.hello()
        assert server_id, f"no hello_ack from the server: {proc.why()}"
        yield proc
    finally:
        proc.close()


# == canned replies ============================================================
#
# The chat name is pinned per session, so these can name it the way the root
# conftest's replies do.


def _reply(body: str, calls: int = 1) -> str:
    return f"~~~~\n{body}\n===CLIP:EOM calls={calls} chat={CHAT}===\n~~~~\n"


WRITE_FILE = _reply(
    "===CLIP:CALL id=1 tool=write_file===\n"
    "path: notes.txt\n"
    "content <<EOT\n"
    "hello from the other side of the wire\n"
    "EOT\n"
    "===CLIP:END==="
)

TASK_DONE = _reply(
    "===CLIP:CALL id=1 tool=task_done===\n"
    "summary <<EOT\n"
    "Wrote notes.txt across the link.\n"
    "EOT\n"
    "===CLIP:END==="
)

NOISY_COMMAND = _reply(
    "===CLIP:CALL id=1 tool=run_command===\n"
    f"command: {PY} \"import time; print('early', flush=True); time.sleep(0.8); print('late')\"\n"
    "reason: prove the output rides the wire while the command is still running\n"
    "===CLIP:END==="
)

SLOW_COMMAND = _reply(
    "===CLIP:CALL id=1 tool=run_command===\n"
    f'command: {PY} "import time; time.sleep(20)"\n'
    "reason: a turn long enough for the user to change their mind\n"
    "===CLIP:END==="
)


# == driving ===================================================================


async def _approve_and_execute(link: RemoteLink, reply: str) -> object:
    """One scripted turn: the model's reply in, the plan approved, the plan run."""
    turn = await asyncio.wait_for(link.ingest(reply), DEADLINE)
    assert type(turn).__name__ == "NewTurn", turn
    pending = await asyncio.wait_for(link.pending(), DEADLINE)
    for action in pending:
        await asyncio.wait_for(link.decide(action.call.id, Decision.APPROVE), DEADLINE)
    return await asyncio.wait_for(link.execute(), DEADLINE)


class _Hooks:
    """Progress and output collected off the worker thread, with the one fact
    the streaming test is actually about: did they arrive BEFORE the answer?"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.progress: list[tuple[CallProgress, bool]] = []
        self.output: list[tuple[int, str, bool]] = []
        self.returned = False
        self.raises = False

    def on_progress(self, progress: CallProgress) -> None:
        with self._lock:
            self.progress.append((progress, self.returned))
        if self.raises:
            raise RuntimeError("this watcher is broken")

    def on_output(self, call_id: int, delta: str) -> None:
        with self._lock:
            self.output.append((call_id, delta, self.returned))

    def wire(self, link: RemoteLink) -> _Hooks:
        link.set_progress_hook(self.on_progress)
        link.set_output_hook(self.on_output)
        return self

    def phases(self) -> list[str]:
        with self._lock:
            return [report.phase for report, _ in self.progress]

    def deltas(self) -> str:
        with self._lock:
            return "".join(delta for _, delta, _ in self.output)


# == the handshake and the sessions ============================================


async def test_the_handshake_carries_a_process_id_and_a_sessions_facts(server: _Server) -> None:
    """The three sync facts come home in ``build_session``'s one answer, which is
    what lets the controller read ``chat_name`` with nothing to await."""
    assert server.client.server_id  # names the PROCESS (design section 2.3)

    link = server.session()
    assert link.chat_name == CHAT
    assert link.role == "master"
    assert link.build_warnings == ()
    assert link.session_id


async def test_one_process_hosts_a_second_session(server: _Server) -> None:
    """One connection, many sessions - the sub-agent case. Two links, two engines,
    one process, and only one of them ever live at a time."""
    master = server.session()
    sub = server.session(role="subagent", parent_chat_name=CHAT)

    assert sub.session_id != master.session_id
    assert sub.role == "subagent"
    await asyncio.wait_for(master.start_task("the master task"), DEADLINE)
    assert (await asyncio.wait_for(master.status(), DEADLINE)).phase.name == "AWAITING_REPLY"
    assert (await asyncio.wait_for(sub.status(), DEADLINE)).phase.name == "IDLE"


# == a whole turn, in another process ==========================================


async def test_a_turn_runs_end_to_end_in_the_other_process(
    server: _Server, remote_project: Path
) -> None:
    link = server.session()
    hooks = _Hooks().wire(link)

    outbound = await asyncio.wait_for(link.start_task("Write the note file."), DEADLINE)
    assert outbound.kind == "bootstrap"
    assert CHAT in outbound.chunks[0]

    step = await _approve_and_execute(link, WRITE_FILE)
    assert type(step).__name__ == "Send"
    assert "===CLIP:RESULT id=1 status=ok===" in step.outbound.chunks[0]  # type: ignore[attr-defined]
    # THE FILE IS ON DISK, written by an engine in another process: nothing in
    # this one can have created it.
    assert (remote_project / "notes.txt").read_text(encoding="utf-8").startswith("hello from")
    assert hooks.phases() == ["running", "done"]

    # The status snapshot decodes back into real values - including the one Path
    # on the seam, which travelled as text and names a directory over there.
    snap = await asyncio.wait_for(link.status(), DEADLINE)
    assert snap.phase.name == "AWAITING_REPLY"
    assert snap.turn == 2  # the bootstrap was turn 1, the results payload turn 2
    assert isinstance(snap.session_dir, Path)
    assert snap.session_dir.name

    done = await _approve_and_execute(link, TASK_DONE)
    assert type(done).__name__ == "Done"
    assert "notes.txt" in done.summary  # type: ignore[attr-defined]
    assert (await asyncio.wait_for(link.status(), DEADLINE)).phase.name == "DONE"


async def test_output_and_progress_reach_the_hooks_before_execute_returns(server: _Server) -> None:
    """The RunPanel's channel across a process boundary.

    Both hooks fire from the worker thread inside the roundtrip, and every event
    is in before the answer - that is wire.py's interleaving guarantee arriving
    where the UI can use it, rather than a pile of deltas dumped after the turn
    is already over.
    """
    link = server.session()
    hooks = _Hooks().wire(link)
    await asyncio.wait_for(link.start_task("Watch a command talk."), DEADLINE)

    step = await _approve_and_execute(link, NOISY_COMMAND)
    hooks.returned = True

    assert type(step).__name__ == "Send"
    assert "early" in hooks.deltas() and "late" in hooks.deltas()
    assert hooks.phases() == ["running", "done"]
    assert {call_id for call_id, _, _ in hooks.output} == {1}
    # Not one of them arrived after the call answered.
    assert not [report for report, late in hooks.progress if late]
    assert not [delta for _, delta, late in hooks.output if late]


async def test_a_raising_progress_hook_is_dropped_and_the_turn_survives(server: _Server) -> None:
    """The engine's contract, reimplemented on this side of the wire: a watcher
    that raises is dropped for good, and the turn never notices."""
    link = server.session()
    hooks = _Hooks().wire(link)
    hooks.raises = True
    await asyncio.wait_for(link.start_task("Write the note file."), DEADLINE)

    step = await _approve_and_execute(link, WRITE_FILE)
    assert type(step).__name__ == "Send"  # no exception surfaced anywhere
    assert hooks.phases() == ["running"]  # dropped on the first raise...

    done = await _approve_and_execute(link, TASK_DONE)
    assert type(done).__name__ == "Done"
    assert hooks.phases() == ["running"]  # ...and never retried, this turn or the next


# == cancel and death ==========================================================


async def test_a_running_turn_is_cancelled_from_the_event_loop(server: _Server) -> None:
    """``request_cancel`` is sync and out-of-band precisely so it can be called
    HERE - from the loop, while ``execute()`` is awaited and the link's lock is
    held by the call it is interrupting."""
    link = server.session()
    running = threading.Event()

    def on_progress(progress: CallProgress) -> None:
        if progress.phase == "running":
            running.set()

    link.set_progress_hook(on_progress)
    await asyncio.wait_for(link.start_task("Start something slow."), DEADLINE)
    assert type(await asyncio.wait_for(link.ingest(SLOW_COMMAND), DEADLINE)).__name__ == "NewTurn"
    await asyncio.wait_for(link.decide(1, Decision.APPROVE), DEADLINE)

    async def cancel_once_running() -> None:
        assert await asyncio.to_thread(running.wait, DEADLINE), "the command never started"
        link.request_cancel()

    started = time.monotonic()
    cancelling = asyncio.create_task(cancel_once_running())
    step = await asyncio.wait_for(link.execute(), DEADLINE)
    await cancelling

    assert type(step).__name__ == "Send"
    assert "code=cancelled" in step.outbound.chunks[0]  # type: ignore[attr-defined]
    assert time.monotonic() - started < 15, "the 20s command ran its course"
    # The link is still usable: a cancel ends a turn, not a session.
    assert (await asyncio.wait_for(link.status(), DEADLINE)).phase.name == "AWAITING_REPLY"


async def test_the_server_dying_mid_call_fails_the_call_instead_of_hanging(
    server: _Server,
) -> None:
    """EOF on the reader is the one failure that could wedge a turn forever, so
    it is the one that must arrive as an exception - promptly."""
    link = server.session()
    running = threading.Event()

    def on_progress(progress: CallProgress) -> None:
        if progress.phase == "running":
            running.set()

    link.set_progress_hook(on_progress)
    await asyncio.wait_for(link.start_task("Start something slow."), DEADLINE)
    await asyncio.wait_for(link.ingest(SLOW_COMMAND), DEADLINE)
    await asyncio.wait_for(link.decide(1, Decision.APPROVE), DEADLINE)

    async def kill_once_running() -> None:
        assert await asyncio.to_thread(running.wait, DEADLINE), "the command never started"
        server.proc.kill()

    started = time.monotonic()
    killing = asyncio.create_task(kill_once_running())
    with pytest.raises(EngineLinkError) as caught:
        await asyncio.wait_for(link.execute(), DEADLINE)
    await killing

    assert caught.value.kind == "link_closed"
    assert "closed" in caught.value.detail
    assert time.monotonic() - started < 15, "the call outlived the process it was made to"


# == typed errors across the wire ==============================================


async def test_an_engine_state_error_is_still_an_engine_state_error(server: _Server) -> None:
    """The controller catches this one BY TYPE. A link that delivered it as a
    generic remote failure would silently break that branch."""
    link = server.session()
    await asyncio.wait_for(link.start_task("Write the note file."), DEADLINE)

    with pytest.raises(EngineStateError, match="nothing to undo"):
        await asyncio.wait_for(link.undo_last_turn(), DEADLINE)

    # ...and the session is untouched by the failed call.
    assert (await asyncio.wait_for(link.status(), DEADLINE)).phase.name == "AWAITING_REPLY"


async def test_budget_exceeded_arrives_with_its_numbers(server: _Server) -> None:
    """Rebuilt from ``data``, not from the message: the Shell formats these two
    figures itself, so a message-only reconstruction would print a plausible
    sentence with the wrong numbers in it."""
    link = server.session()

    with pytest.raises(BudgetExceeded) as caught:
        await asyncio.wait_for(link.start_task("x" * 200_000), DEADLINE)

    assert caught.value.needed_chars > caught.value.budget_chars > 0


async def test_a_bad_call_fails_that_call_and_not_the_link(server: _Server) -> None:
    """A ``bad_request`` has no local exception type worth rebuilding, so it
    arrives as EngineLinkError - unmistakably from the far side."""
    link = server.session()
    await asyncio.wait_for(link.start_task("Write the note file."), DEADLINE)

    with pytest.raises(EngineLinkError) as caught:
        # A session the server never minted: the frame is well-formed, the
        # request is not.
        await asyncio.to_thread(server.client.roundtrip, "s99", "status", {})
    assert caught.value.kind == "bad_request"

    assert (await asyncio.wait_for(link.status(), DEADLINE)).phase.name == "AWAITING_REPLY"


# == parity ====================================================================


def _local_link(project: Path, sandbox: Path) -> LocalLink:
    """The same assembly the server process runs, in this one.

    Same builder, same isolation flags (as parameters rather than command-line
    arguments), same pinned chat name - so anything the two links disagree about
    is the wire, which is the only thing this test is looking at.
    """

    def get_config() -> Config:
        return load_config(
            project,
            global_config_path=sandbox / "no-such-global.toml",
            home=sandbox / "home",
        )

    builder = make_engine_builder(get_config, project, home=sandbox / "home")
    return LocalLink(builder(EngineRequest(service="claude", chat_name=CHAT)))


async def test_the_same_flow_through_both_links_agrees(server: _Server, tmp_path: Path) -> None:
    """The mitigation section 2.2 demands, spelled out.

    The link seam's accepted risk is that only remote runs exercise the wire, so
    a divergence would surface as a bug in a remote session rather than a red
    test here. This runs one scripted flow through BOTH implementations, over two
    projects that differ in nothing but their path, and holds them to the same
    answers: the bootstrap payload character for character, and the turn's
    StepResult by kind.
    """
    local = _local_link(_make_project(tmp_path / "local" / "project"), tmp_path)
    remote = server.session()

    task = "Write the note file."
    local_out = await asyncio.wait_for(local.start_task(task), DEADLINE)
    remote_out = await asyncio.wait_for(remote.start_task(task), DEADLINE)
    assert local_out.chunks == remote_out.chunks
    assert (local_out.kind, local_out.turn) == (remote_out.kind, remote_out.turn)

    local_step = await _approve_and_execute(local, WRITE_FILE)  # type: ignore[arg-type]
    remote_step = await _approve_and_execute(remote, WRITE_FILE)
    assert type(local_step).__name__ == type(remote_step).__name__ == "Send"
    assert local_step.outbound.chunks == remote_step.outbound.chunks  # type: ignore[attr-defined]
