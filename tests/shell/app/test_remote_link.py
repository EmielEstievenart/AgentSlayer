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
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from agentclip import __version__, cli
from agentclip.config import Config, load_config
from agentclip.engine.engine import CallProgress
from agentclip.engine.link import wire
from agentclip.engine.link.factory import EngineRequest, make_engine_builder
from agentclip.engine.link.wire import EngineLinkError
from agentclip.engine.states import Decision, EngineStateError
from agentclip.executor.mcp.types import McpServerStatus
from agentclip.protocol.composer import BudgetExceeded
from agentclip.shell.app.link import LocalLink
from agentclip.shell.app.remote_link import LINK_VERSION, RemoteLink, RemoteLinkClient

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
    SSH exec channel's streams instead (``executor.hosts.ssh.LinkChannel``, whose
    adapters satisfy the same two Protocols a pipe does) - which is the whole
    reason no spawning lives in ``src``.

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


# == the version gate, on scripted streams =====================================
#
# The one handshake case a subprocess cannot stage: both halves here are the SAME
# installed package, so they can never disagree about a version. The deployment
# model they exist for is the opposite (design section 2.6 - the engine half is a
# console script the user installs on the target), so what the target said is
# scripted into a pair of StringIO streams instead, and what the USER would be
# told is the assertion.


def _client_hearing(*frames: dict[str, object]) -> tuple[RemoteLinkClient, StringIO]:
    sent = StringIO()
    reader = StringIO("".join(wire.encode_line(frame) for frame in frames))
    return RemoteLinkClient(reader, sent), sent


def test_the_hello_states_this_installs_two_versions() -> None:
    client, sent = _client_hearing(wire.hello_ack_frame("p1"))
    client.hello()
    hello = wire.decode_line(sent.getvalue())
    assert hello["version"] == wire.WIRE_VERSION
    assert hello["package"] == __version__


def test_a_target_on_another_wire_version_is_refused_by_naming_both_installs() -> None:
    client, _ = _client_hearing(
        {
            "type": "hello_ack",
            "version": wire.WIRE_VERSION + 1,
            "package": "9.9.9",
            "server_id": "p1",
        }
    )
    with pytest.raises(EngineLinkError) as caught:
        client.hello()
    message = str(caught.value)
    assert caught.value.kind == LINK_VERSION
    # Both wire versions, both package versions, and what to do about it - the
    # user installed the two halves separately and can only act on the latter.
    assert f"wire v{wire.WIRE_VERSION + 1}" in message and f"wire v{wire.WIRE_VERSION}" in message
    assert "9.9.9" in message and __version__ in message
    assert "update the target's install" in message


def test_an_error_frame_instead_of_an_ack_keeps_the_targets_reason() -> None:
    """What an OLDER target actually does: refuse our hello, say why, and exit.

    The detail it sends already names both wire versions and both packages, so
    passing it through is the difference between a user who knows which machine
    to upgrade and one who reads "expected a 'hello_ack' frame, got 'error'".
    """
    detail = str(wire.WireVersionError("hello", wire.Versions(wire.WIRE_VERSION, __version__)))
    client, _ = _client_hearing(wire.error_frame(0, "bad_request", detail))
    with pytest.raises(EngineLinkError) as caught:
        client.hello()
    message = str(caught.value)
    assert caught.value.kind == LINK_VERSION
    assert detail in message
    assert "update the target's install" in message


def test_a_target_on_another_package_version_is_fine_and_remembered() -> None:
    """Same wire, different release: legal, expected, and worth showing later."""
    client, _ = _client_hearing(
        {
            "type": "hello_ack",
            "version": wire.WIRE_VERSION,
            "package": "9.9.9",
            "server_id": "p1",
        }
    )
    assert client.hello() == "p1"
    assert client.server_package == "9.9.9"


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


async def test_the_mcp_settle_rides_build_session_and_the_pull_round_trips(
    server: _Server,
) -> None:
    """MCP status crosses as a LINK-SCOPED call, and a snapshot rides the build.

    The target here has no MCP configured (the isolation flags point HOME and the
    global config at paths that do not exist, so no permissions.json contributes
    a server), which makes the empty tuple the honest answer and the round trip
    the thing under test: a `()` that arrived over the wire is a `()` the codec,
    the dispatch and the client all agreed on.
    """
    # Before any session exists - which is when a Shell first wants to paint the
    # block, and exactly what "link-scoped" buys.
    assert await asyncio.to_thread(server.client.mcp_statuses) == ()

    link = server.session()
    # The settle came home on build_session's one answer, on both the object the
    # controller holds and the connection that minted it.
    assert link.build_mcp_statuses == ()
    assert server.client.build_mcp_statuses == ()
    # ...and the pull is available through the Link seam itself, no session named.
    assert await asyncio.wait_for(link.mcp_statuses(), DEADLINE) == ()

    # A session call still works afterwards: the link-scoped call left the
    # connection exactly where it found it.
    await asyncio.wait_for(link.start_task("Write the note file."), DEADLINE)
    assert (await asyncio.wait_for(link.status(), DEADLINE)).phase.name == "AWAITING_REPLY"


# == one reader on the connection, on scripted streams =========================
#
# The client-level lock cannot be shown against a real server, because a real
# server answers too fast to catch two calls overlapping. So the far side is a
# pair of hand-driven streams: nothing is answered until the test says so, which
# makes "the second call had not even written its frame" an assertion about
# ORDER rather than about timing.


class _Gated:
    """A reader and a writer whose every line is released by hand.

    Satisfies ``LineReader`` and ``LineWriter`` structurally, which is the whole
    reason those are Protocols: a transport is whatever can do these three
    things.
    """

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self._answers: queue.Queue[str] = queue.Queue()
        self._wrote = threading.Semaphore(0)

    # -- the reader half -------------------------------------------------------

    def readline(self) -> str:
        return self._answers.get()

    # -- the writer half -------------------------------------------------------

    def write(self, s: str, /) -> int:
        self.written.append(wire.decode_line(s))
        self._wrote.release()
        return len(s)

    def flush(self) -> None: ...

    # -- driving ---------------------------------------------------------------

    def answer(self, frame: dict[str, Any]) -> None:
        self._answers.put(wire.encode_line(frame))

    def wrote_a_frame(self, timeout: float = DEADLINE) -> bool:
        return self._wrote.acquire(timeout=timeout)

    def methods(self) -> list[str]:
        return [frame["method"] for frame in self.written if frame["type"] == "call"]


def test_a_link_scoped_call_cannot_interleave_with_a_session_call() -> None:
    """The reason the serialization moved into the client.

    A per-link ``asyncio.Lock`` serializes calls on ONE link, and that used to be
    enough because every call belonged to a link. ``mcp_statuses`` belongs to
    none, so nothing would stand between it and a session call's reads - two
    readers on one stream, each consuming frames the other is waiting for. Here
    the session call is parked mid-read and the link-scoped one is started: it
    must not have put so much as its call frame on the wire, and the answer that
    then arrives must reach the call it was meant for.
    """
    gated = _Gated()
    client = RemoteLinkClient(gated, gated)
    rows = (McpServerStatus(name="github", state="connected", tool_count=17),)
    session_answer: list[object] = []
    link_answer: list[object] = []
    failures: list[BaseException] = []

    def run(target, sink: list[object]) -> None:  # type: ignore[no-untyped-def]
        try:
            sink.append(target())
        except BaseException as exc:  # noqa: BLE001 - reported, not raised, off-thread
            failures.append(exc)

    def a_session_call() -> object:
        return client.roundtrip("s1", "set_yolo", wire.encode_params("set_yolo", enabled=True))

    session_call = threading.Thread(target=run, args=(a_session_call, session_answer), daemon=True)
    session_call.start()
    assert gated.wrote_a_frame(), "the session call never wrote its frame"
    assert gated.methods() == ["set_yolo"]

    link_call = threading.Thread(target=run, args=(client.mcp_statuses, link_answer), daemon=True)
    link_call.start()
    # Parked on the client's lock: not on the wire, therefore not in a read
    # either. A bounded proof of ABSENCE - the only kind there is.
    assert not gated.wrote_a_frame(0.25)
    assert gated.methods() == ["set_yolo"]

    # The answer to call 1 belongs to call 1. An unserialized second reader is
    # exactly what would steal it (and then fail on the id it never sent).
    gated.answer(wire.result_frame(1, wire.encode_result("set_yolo", True)))
    session_call.join(DEADLINE)
    assert session_answer == [True]

    # ...and only now does the link-scoped call get its turn.
    assert gated.wrote_a_frame(), "the link-scoped call never got the connection"
    assert gated.methods() == ["set_yolo", "mcp_statuses"]
    gated.answer(wire.result_frame(2, wire.encode_result("mcp_statuses", rows)))
    link_call.join(DEADLINE)
    assert link_answer == [rows]
    assert not failures, failures


def test_the_remote_engine_is_the_shells_mcp_status_source() -> None:
    """``cli.RemoteEngine`` answers the three names both sidebars consume.

    The remote half of what ``cli.LinkFactory`` is locally (§2.7): one object per
    mode, ``statuses()`` / ``set_status_hook()`` / ``close()``, so neither shell
    branches on where the engine is. What is pinned here is the two rules that
    make it safe to hand to a UI thread - the rows come from the ``build_session``
    settle the client cached (§2.11) and NOTHING is written to the wire to
    produce them, and the hook is accepted and dropped because v1 has no MCP push
    (§2.9). A round trip inside ``statuses()`` would freeze the window on every
    status paint for as long as the target took to answer.

    The streams are the hand-driven pair above rather than a real server, because
    the point is exactly which frames were written: a scripted ``build_session``
    answer carries rows that no local runtime could have produced.
    """
    gated = _Gated()
    client = RemoteLinkClient(gated, gated)
    channel = _DeadChannel()
    engine = cli.RemoteEngine(client=client, channel=channel, target="dev@box")  # type: ignore[arg-type]

    # Before any session: the honest empty, and still no wire traffic.
    assert engine.statuses() == ()
    assert gated.written == []

    rows = (McpServerStatus(name="github", state="connected", tool_count=17),)
    info = wire.SessionInfo(session="s1", chat_name=CHAT, role="master", mcp_statuses=rows)
    gated.answer(wire.result_frame(1, wire.encode_result("build_session", info)))
    link = client.build_session(EngineRequest(service="claude"))

    assert link.build_mcp_statuses == rows
    assert engine.statuses() == rows
    # ...and reading them twice more still costs the connection nothing: one
    # frame was written, and it was the session build.
    assert engine.statuses() == rows
    assert gated.methods() == ["build_session"]

    # The hook is part of the shape and does nothing at all - registering one, or
    # clearing it, must never raise and must never reach the wire.
    assert engine.set_status_hook(lambda status: None) is None
    engine.set_status_hook(None)
    assert gated.methods() == ["build_session"]

    engine.close()  # the channel is what a remote engine's life is tied to (§2.3)
    assert channel.closed is True


class _DeadChannel:
    """``LinkChannel``, as far as :class:`cli.RemoteEngine` is concerned: a thing
    that can be closed. The streams it would own are the ``_Gated`` pair here."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_a_cancel_is_written_while_a_call_holds_the_connection() -> None:
    """The one frame that must NOT wait for the call lock.

    ``request_cancel`` is sync and out-of-band precisely so it can be called from
    the event loop mid-turn; a cancel that queued behind the call lock would
    queue behind the very call it exists to interrupt.
    """
    gated = _Gated()
    client = RemoteLinkClient(gated, gated)
    answer: list[object] = []

    call = threading.Thread(
        target=lambda: answer.append(
            client.roundtrip("s1", "execute", wire.encode_params("execute"))
        ),
        daemon=True,
    )
    call.start()
    assert gated.wrote_a_frame(), "the execute call never wrote its frame"

    client.send_cancel("s1")  # from "the event loop", with the call outstanding
    assert gated.wrote_a_frame(), "the cancel never reached the wire"
    assert [frame["type"] for frame in gated.written] == ["call", "cancel"]

    done = wire.decode_step_result(
        {"kind": "Done", "summary": "cancelled", "outbound": None, "result": ""}
    )
    gated.answer(wire.result_frame(1, wire.encode_result("execute", done)))
    call.join(DEADLINE)
    assert type(answer[0]).__name__ == "Done"


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
