"""The engine half's dispatch loop, driven over a real pair of pipes.

``serve()`` takes text streams rather than a socket exactly so it can be driven
in-process: these tests run it on a thread against two ``os.pipe()``s and speak
protocol v1 at it with the same :mod:`agentclip.engine.link.wire` a ``RemoteLink``
will - no subprocess, no SSH, and a real Engine on the other side building real
files in a tmp project.

What is worth pinning here is everything the codec tests cannot see: that the
handshake refuses a version it does not speak, that a session is minted and
addressed by id, that progress and output frames arrive BEFORE the answer of the
call that produced them, that a cancel is honoured while a turn is running (the
reader thread's whole reason for never running an engine method), and that the
server survives everything a broken client can say to it - up to two malformed
lines in a row, which is where it stops trusting the wire.

Every blocking read below has a deadline. A hung test is worse than a failed
one: it says nothing and it costs a wall-clock timeout to find out.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from agentclip import __version__
from agentclip.config import Config, load_config
from agentclip.engine.link import wire
from agentclip.engine.link.factory import make_engine_builder
from agentclip.engine.link.server import EngineBuilder, serve
from agentclip.engine.states import Decision

# The chat name every engine this file builds is pinned to, so the canned
# replies below can hard-code it on their EOM line (as the root conftest does).
CHAT = "amber-falcon"

# One deadline for every blocking read. Generous: it is a failure budget, not a
# timing assertion - the flows here answer in milliseconds.
DEADLINE = 20.0

PY = "python -c"


# == the harness ===============================================================


class _Link:
    """A client's end of the wire: a running ``serve()`` and a frame queue.

    Two pipes, four text streams: the server reads one and writes the other,
    this object does the mirror image. A pump thread turns the server's lines
    into frames on a queue, which is what makes every read here timeout-able -
    ``readline()`` on a pipe cannot be, and a test that blocks forever on a
    server bug reports nothing at all.
    """

    def __init__(self, builder: EngineBuilder) -> None:
        server_in, client_out = os.pipe()
        client_in, server_out = os.pipe()
        self._server_reader = os.fdopen(server_in, "r", encoding="utf-8", newline="")
        self._server_writer = os.fdopen(server_out, "w", encoding="utf-8", newline="")
        self._to_server = os.fdopen(client_out, "w", encoding="utf-8", newline="")
        self._from_server = os.fdopen(client_in, "r", encoding="utf-8", newline="")
        self.log = StringIO()
        self.exit_codes: list[int] = []
        self.frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._next_id = 0
        self._server = threading.Thread(target=self._serve, args=(builder,), daemon=True)
        self._pump = threading.Thread(target=self._pump_frames, daemon=True)
        self._server.start()
        self._pump.start()

    # -- the two threads -------------------------------------------------------

    def _serve(self, builder: EngineBuilder) -> None:
        try:
            self.exit_codes.append(
                serve(self._server_reader, self._server_writer, builder, log=self.log)
            )
        finally:
            # The server never closes what it was handed; closing here is what
            # gives the pump its EOF (and the test its "the stream ended").
            self._server_writer.close()
            self._server_reader.close()

    def _pump_frames(self) -> None:
        try:
            while True:
                line = self._from_server.readline()
                if not line:
                    return
                self.frames.put(json.loads(line))
        finally:
            self.frames.put(None)  # the stream ended

    # -- speaking --------------------------------------------------------------

    def send(self, frame: dict[str, Any]) -> None:
        self.send_raw(wire.encode_line(frame))

    def send_raw(self, text: str) -> None:
        self._to_server.write(text)
        self._to_server.flush()

    def hello(self, frame: dict[str, Any] | None = None) -> wire.HelloAck:
        self.send(frame if frame is not None else wire.hello_frame())
        return wire.read_hello_ack(self.next_frame())

    def call(self, method: str, *, session: str | None = None, **params: Any) -> int:
        self._next_id += 1
        self.send(
            wire.call_frame(
                self._next_id, method, wire.encode_params(method, **params), session=session
            )
        )
        return self._next_id

    # -- listening -------------------------------------------------------------

    def next_frame(self, timeout: float = DEADLINE) -> dict[str, Any]:
        try:
            frame = self.frames.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no frame within {timeout}s") from None
        assert frame is not None, "the server closed the stream"
        return frame

    def answer(self, req_id: int, timeout: float = DEADLINE) -> tuple[dict[str, Any], list[dict]]:
        """The result/error for one call, plus every event that preceded it."""
        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while True:
            frame = self.next_frame(max(0.1, deadline - time.monotonic()))
            if frame["type"] in ("result", "error") and frame.get("id") == req_id:
                return frame, events
            events.append(frame)

    def ask(self, method: str, *, session: str | None = None, **params: Any) -> Any:
        value, _events = self.ask_with_events(method, session=session, **params)
        return value

    def ask_with_events(
        self, method: str, *, session: str | None = None, **params: Any
    ) -> tuple[Any, list[dict[str, Any]]]:
        frame, events = self.answer(self.call(method, session=session, **params))
        assert frame["type"] == "result", f"{method} failed: {frame}"
        return wire.decode_result(method, frame["value"]), events

    def ask_error(self, method: str, *, session: str | None = None, **params: Any) -> wire.ErrorFrame:
        frame, _events = self.answer(self.call(method, session=session, **params))
        assert frame["type"] == "error", f"{method} unexpectedly succeeded: {frame}"
        return wire.read_error(frame)

    # -- shutting down ---------------------------------------------------------

    def close_input(self) -> None:
        """EOF for the server: the client hung up."""
        self._to_server.close()

    def wait_exit(self, timeout: float = DEADLINE) -> int:
        self._server.join(timeout)
        assert not self._server.is_alive(), f"serve() still running after {timeout}s"
        assert self.exit_codes, "serve() never returned a code"
        return self.exit_codes[0]

    def shutdown(self) -> int:
        # Already closed is fine: a test whose server exited on its own may have
        # hung up first.
        with contextlib.suppress(OSError, ValueError):
            self.close_input()
        code = self.wait_exit()
        self._pump.join(DEADLINE)
        self._from_server.close()
        return code


@pytest.fixture
def builder(project: Path) -> EngineBuilder:
    """The real assembly, pointed at the tmp project and nothing else.

    The isolation is the same three flags every factory test uses: a global
    config path that does not exist, and the project doubling as HOME so the
    developer's own skill folders and permission ruleset stay out (platformdirs
    ignores env vars on Windows, so these parameters are the only reliable way).
    """

    def get_config() -> Config:
        return load_config(
            project, global_config_path=project / "no-such-global.toml", home=project
        )

    return make_engine_builder(get_config, project, CHAT, home=project)


@pytest.fixture
def link(builder: EngineBuilder) -> Iterator[_Link]:
    conn = _Link(builder)
    try:
        yield conn
    finally:
        conn.shutdown()


def _armed(link: _Link, task: str = "Write the note file.") -> str:
    """Handshake, one session, one started task - the state most tests open in."""
    link.hello()
    info = link.ask("build_session", service="claude")
    outbound = link.ask("start_task", session=info.session, task=task)
    assert outbound.kind == "bootstrap"
    return str(info.session)


# == canned replies ============================================================

REPLY_WRITE_FILE = """I'll drop the note in place.

~~~~
===CLIP:CALL id=1 tool=write_file===
path: notes.txt
content <<EOT
hello from the other side of the wire
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

REPLY_TASK_DONE = """That's the file written.

~~~~
===CLIP:CALL id=1 tool=task_done===
summary <<EOT
Wrote notes.txt across the link.
EOT
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

REPLY_NOISY_COMMAND = f"""Let me watch it talk.

~~~~
===CLIP:CALL id=1 tool=run_command===
command: {PY} "import time; print('early', flush=True); time.sleep(0.8); print('late')"
reason: prove the output rides the wire while the command is still running
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""

REPLY_SLOW_COMMAND = f"""This one takes a while.

~~~~
===CLIP:CALL id=1 tool=run_command===
command: {PY} "import time; time.sleep(20)"
reason: a turn long enough for the user to change their mind
===CLIP:END===
===CLIP:EOM calls=1 chat=amber-falcon===
~~~~
"""


# == the handshake =============================================================


def test_hello_is_answered_with_a_versioned_ack(link: _Link) -> None:
    ack = link.hello()
    assert ack.server_id  # names the PROCESS; v1 only asks that it is non-empty
    # Both numbers, and the package is this install's own: it is what a refusal
    # on either side gets to name (docs/design/remote-executor.md section 2.9).
    assert ack.versions == wire.Versions(wire.WIRE_VERSION, __version__)


def test_a_hello_from_another_version_is_refused_and_ends_the_process(link: _Link) -> None:
    link.send({"type": "hello", "version": wire.WIRE_VERSION + 1, "package": "9.9.9"})
    error = wire.read_error(link.next_frame())
    assert error.kind == "bad_request"
    assert "wire version" in error.detail
    # Both wire versions and both package versions: the client is about to turn
    # this detail into the sentence a human reads.
    assert str(wire.WIRE_VERSION + 1) in error.detail and str(wire.WIRE_VERSION) in error.detail
    assert "9.9.9" in error.detail and __version__ in error.detail
    assert link.wait_exit() != 0


def test_a_hello_from_another_package_on_the_same_wire_is_served(link: _Link) -> None:
    """Two independently-installed halves are EXPECTED to differ in package
    version (design section 2.6). It costs a stderr line and nothing else."""
    ack = link.hello({"type": "hello", "version": wire.WIRE_VERSION, "package": "9.9.9"})
    assert ack.server_id
    assert link.ask("build_session", service="claude").chat_name == CHAT
    log = link.log.getvalue()
    assert "9.9.9" in log and __version__ in log


def test_garbage_before_the_hello_ends_the_process(link: _Link) -> None:
    link.send_raw("hello there\n")
    error = wire.read_error(link.next_frame())
    assert error.kind == "bad_request"
    assert link.wait_exit() != 0


def test_a_call_before_the_hello_ends_the_process(link: _Link) -> None:
    """Nothing may precede the hello - not even a well-formed call."""
    link.send(wire.call_frame(1, "build_session", wire.encode_params("build_session", service="claude")))
    error = wire.read_error(link.next_frame())
    assert error.kind == "bad_request"
    assert link.wait_exit() != 0


# == sessions ==================================================================


def test_build_session_answers_with_the_immutable_facts(link: _Link) -> None:
    link.hello()
    info = link.ask("build_session", service="claude")
    assert info.session
    assert info.chat_name == CHAT
    assert info.role == "master"
    assert info.build_warnings == ()


def test_one_server_hosts_several_sessions(link: _Link) -> None:
    """The controller builds sub-agent engines mid-session from the same factory,
    so the builder is called more than once and each engine gets its own id."""
    link.hello()
    first = link.ask("build_session", service="claude")
    second = link.ask("build_session", service="claude", role="subagent", parent_chat_name=CHAT)
    assert first.session != second.session
    assert second.role == "subagent"
    # Both are addressable, and they are different engines: only the first has
    # been given a task.
    link.ask("start_task", session=first.session, task="the master task")
    assert link.ask("status", session=first.session).phase.name == "AWAITING_REPLY"
    assert link.ask("status", session=second.session).phase.name == "IDLE"


def test_a_build_failure_leaves_the_server_running(link: _Link) -> None:
    """A builder that raises costs the client one call, not the process."""
    boom = threading.Event()

    def exploding(request: Any) -> Any:
        boom.set()
        raise RuntimeError("no such project on this machine")

    conn = _Link(exploding)
    try:
        conn.hello()
        error = conn.ask_error("build_session", service="claude")
        assert error.kind == "internal"
        assert "no such project" in error.detail
        assert boom.is_set()
        # Still listening: a second call is answered rather than dropped.
        assert conn.ask_error("build_session", service="claude").kind == "internal"
    finally:
        assert conn.shutdown() == 0


# == a whole session over frames ===============================================


def test_a_turn_runs_end_to_end_over_the_wire(project: Path, link: _Link) -> None:
    session = _armed(link)

    turn = link.ask("ingest", session=session, text=REPLY_WRITE_FILE)
    assert type(turn).__name__ == "NewTurn"
    assert [call.tool for call in turn.reply.calls] == ["write_file"]

    pending = link.ask("pending", session=session)
    assert [action.call.id for action in pending] == [1]
    assert pending[0].kind == "edit"

    assert link.ask("decide", session=session, call_id=1, decision=Decision.APPROVE) is None

    step, events = link.ask_with_events("execute", session=session)
    assert type(step).__name__ == "Send"
    assert "===CLIP:RESULT id=1 status=ok===" in step.outbound.chunks[0]
    # THE FILE IS ON DISK: a real engine ran a real tool behind the frames.
    assert (project / "notes.txt").read_text(encoding="utf-8").startswith("hello from")

    # The events of this call arrived BEFORE its answer (that is what `events`
    # IS), they are progress frames, and every one names this session.
    progress = [wire.read_progress(frame) for frame in events if frame["type"] == "progress"]
    assert progress, "the execute call reported no progress at all"
    assert {report.session for report in progress} == {session}
    assert [report.progress.phase for report in progress] == ["running", "done"]
    assert {report.progress.tool for report in progress} == {"write_file"}

    # ...and the session finishes, through the same frames.
    assert type(link.ask("ingest", session=session, text=REPLY_TASK_DONE)).__name__ == "NewTurn"
    done = link.ask("execute", session=session)
    assert type(done).__name__ == "Done"
    assert "notes.txt" in done.summary
    assert link.ask("status", session=session).phase.name == "DONE"


def test_a_commands_output_streams_ahead_of_the_answer(link: _Link) -> None:
    """The RunPanel's channel: output frames land while the command still runs."""
    session = _armed(link, "Watch a command talk.")
    link.ask("ingest", session=session, text=REPLY_NOISY_COMMAND)
    assert [action.kind for action in link.ask("pending", session=session)] == ["command"]
    link.ask("decide", session=session, call_id=1, decision=Decision.APPROVE)

    step, events = link.ask_with_events("execute", session=session)
    assert type(step).__name__ == "Send"
    deltas = [wire.read_output(frame) for frame in events if frame["type"] == "output"]
    assert deltas, "no output frame arrived before the result"
    assert {delta.session for delta in deltas} == {session}
    assert {delta.call_id for delta in deltas} == {1}
    joined = "".join(delta.delta for delta in deltas)
    assert "early" in joined and "late" in joined
    # The whole output still reaches the model, streamed or not.
    assert "early" in step.outbound.chunks[0]


def test_a_running_turn_is_cancelled_from_the_reader_thread(link: _Link) -> None:
    """The point of thread-per-call: the loop keeps reading mid-``execute``.

    Also the one place the busy rule is visible - while the turn is out, a
    second call on that session is a client bug, and says so.
    """
    session = _armed(link, "Start something slow.")
    link.ask("ingest", session=session, text=REPLY_SLOW_COMMAND)
    link.ask("decide", session=session, call_id=1, decision=Decision.APPROVE)

    started = time.monotonic()
    running = link.call("execute", session=session)
    events: list[dict[str, Any]] = []
    while True:  # wait for the command to actually be running
        frame = link.next_frame()
        events.append(frame)
        if frame["type"] == "progress" and wire.read_progress(frame).progress.phase == "running":
            break

    # One in flight per session: the reader is awake, and it says no.
    second = link.call("status", session=session)
    while True:
        frame = link.next_frame()
        if frame.get("id") == second:
            break
        events.append(frame)
    busy = wire.read_error(frame)
    assert busy.kind == "bad_request"
    assert "in flight" in busy.detail

    link.send(wire.cancel_frame(session))
    answer, more = link.answer(running)
    events.extend(more)
    assert answer["type"] == "result", f"the cancelled call never answered: {answer}"
    step = wire.decode_result("execute", answer["value"])
    assert type(step).__name__ == "Send"
    payload = step.outbound.chunks[0]
    assert "code=cancelled" in payload
    assert time.monotonic() - started < 15  # the 20s command did not run its course

    # The server is still usable afterwards.
    assert link.ask("status", session=session).phase.name == "AWAITING_REPLY"


def test_a_cancel_for_an_idle_session_is_a_no_op(link: _Link) -> None:
    session = _armed(link)
    link.send(wire.cancel_frame(session))
    link.send(wire.cancel_frame("s99"))  # unknown session: logged, never answered
    # Nothing was answered, and the next turn runs untouched.
    link.ask("ingest", session=session, text=REPLY_WRITE_FILE)
    link.ask("decide", session=session, call_id=1, decision=Decision.APPROVE)
    step = link.ask("execute", session=session)
    assert "===CLIP:RESULT id=1 status=ok===" in step.outbound.chunks[0]


# == what a broken client gets =================================================


def test_an_engine_state_error_keeps_its_kind(link: _Link) -> None:
    """The Shell catches this one BY TYPE, so it may not arrive as `internal`."""
    session = _armed(link)
    error = link.ask_error("start_task", session=session, task="again")
    assert error.kind == "engine_state_error"
    assert "start_task" in error.detail


def test_an_unknown_method_is_a_bad_request_attributed_to_the_call(link: _Link) -> None:
    session = _armed(link)
    link.send({"type": "call", "id": 99, "method": "frobnicate", "params": {}, "session": session})
    error = wire.read_error(link.next_frame())
    assert error.kind == "bad_request"
    assert error.id == 99  # the client can fail that one call, not the link
    assert "frobnicate" in error.detail
    assert link.ask("status", session=session).phase.name == "AWAITING_REPLY"


def test_an_unknown_session_is_a_bad_request(link: _Link) -> None:
    session = _armed(link)
    error = link.ask_error("status", session="s99")
    assert error.kind == "bad_request"
    assert "s99" in error.detail
    assert link.ask("status", session=session).phase.name == "AWAITING_REPLY"


def test_a_malformed_line_is_answered_and_the_loop_reads_on(link: _Link) -> None:
    session = _armed(link)
    link.send_raw("this is not a frame\n")
    error = wire.read_error(link.next_frame())
    assert error.kind == "bad_request"
    assert error.id == 0  # unattributable: no call said this
    # Recovered: the very next line is served normally.
    assert link.ask("status", session=session).phase.name == "AWAITING_REPLY"
    assert "bad frame" in link.log.getvalue()


def test_two_malformed_lines_in_a_row_end_the_process(link: _Link) -> None:
    _armed(link)
    link.send_raw("{{{\n")
    assert wire.read_error(link.next_frame()).kind == "bad_request"
    link.send_raw("still not a frame\n")
    assert wire.read_error(link.next_frame()).kind == "bad_request"
    assert link.wait_exit() != 0
    assert "cannot be trusted" in link.log.getvalue()


def test_a_second_hello_is_refused_without_killing_the_session(link: _Link) -> None:
    session = _armed(link)
    link.send(wire.hello_frame())
    assert wire.read_error(link.next_frame()).kind == "bad_request"
    assert link.ask("status", session=session).phase.name == "AWAITING_REPLY"


# == shutdown ==================================================================


def test_eof_is_a_clean_shutdown(link: _Link) -> None:
    _armed(link)
    assert link.shutdown() == 0
