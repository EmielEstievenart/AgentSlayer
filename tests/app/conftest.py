"""A headless ChatView so the session controller can be driven in milliseconds.

``SessionController`` talks to its UI through exactly one narrow port
(:class:`agentclip.app.view.ChatView`), which means the whole orchestration -
including the parts that are hardest to reach through a real UI: a delegation
failing halfway, a paste arriving for the wrong chat, an abort landing between
two awaits - is testable with a dictionary and a few futures.

:class:`FakeChatView` is that double. It records everything the controller says
(transcript events, toasts, alerts, outbound copies, state pushes, session-view
lifecycle) and answers everything the controller asks from a script set up front:

* ``specs`` - what ``prompt_new_session`` returns, one per call;
* ``answers`` - queued ``ask_user`` answers, delivered automatically when a
  state push says the controller is waiting for one;
* ``decision`` - what every approval gate resolves to;
* ``delegation`` / ``missing`` / ``start_chat_ok`` - the sub-agent transport's
  answers, so both delegation failure paths are one flag each.

The two prompts that resolve *later* (the gate and ask_user) are answered via
``call_soon``: the controller creates its future immediately after telling the
view, so a callback scheduled from inside the notification always runs with the
future in place.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentclip.app.controller import SessionController
from agentclip.app.types import EngineRequest, SessionRef, SessionSpec
from agentclip.app.view import RunCall, SessionView, Severity
from agentclip.cli import make_engine_factory
from agentclip.config import Config, load_config
from agentclip.engine.engine import Decision, Engine, PendingAction
from agentclip.protocol.types import Outbound, ToolCall

MASTER_CHAT = "amber-falcon"
SUB_CHATS = ("jade-otter", "teal-moth", "rust-heron")


class FakeChatView:
    """Structural ``ChatView`` that records and scripts. No Textual, no clipboard."""

    def __init__(self) -> None:
        # -- recordings -------------------------------------------------------
        self.events: list[tuple[str, str]] = []  # (kind, text) per transcript add
        self.notifications: list[tuple[str, str]] = []  # (message, severity)
        self.alerts: list[tuple[str, str]] = []
        self.copied: list[str] = []  # every copy_outbound payload, in order
        self.parked: list[str] = []  # `c` stage one: clipboard only, no delivery
        self.redelivered: list[str] = []  # `c` stage two: the double tap's re-send
        self.states: list[SessionView] = []  # every render_state push
        self.gates: list[tuple[PendingAction, str]] = []
        self.opened: list[SessionRef] = []
        self.focused: list[str] = []
        self.finished: list[tuple[str, str, bool]] = []  # (session id, note, handed a result back)
        self.chats_started: list[SessionRef] = []
        self.chats_ended: list[SessionRef] = []
        self.cleared = 0
        # The run panel's three channels (§8a): every start_working with the
        # rows it was given, then the per-call progress and output pushed from
        # the engine's worker thread.
        self.working: list[tuple[str, list[RunCall]]] = []
        self.call_events: list[tuple[str, int, str]] = []
        self.call_output_chunks: list[tuple[int, str]] = []
        self.new_chats_opened = 0  # /new asking for a fresh BROWSER chat, now
        self.identify_overlays = 0  # /identify asking for the debug boxes
        self.harness_log_toggles = 0  # /log flipping the decision-log pane
        # Every /armed target as the controller sent it (None = "toggle"), plus
        # the state a real view would be in after them - the port is fire-and-
        # forget, so the fake resolves the toggles the way MainScreen does.
        self.armed_targets: list[bool | None] = []
        self.os_armed = True
        self.input_started = 0
        self.exited = False
        self.tasks: list[asyncio.Task[Any]] = []
        # Ordered trace of the few calls whose ORDER is the contract: a
        # sub-agent's chat must be opened before anything is ever pasted.
        self.trace: list[str] = []

        # -- script -----------------------------------------------------------
        self.controller: SessionController | None = None
        self.specs: list[SessionSpec | None] = []
        self.answers: list[str] = []
        self.decision: tuple[Decision, str | None] = (Decision.APPROVE, None)
        self.clipboard: str | None = None
        self.confirm_result = True
        self.new_chat_lands = True  # did the browser's new-chat click land?
        self.text_result: str | None = None
        self.summary_action = "close"
        self.delegation = False
        self.missing: tuple[str, ...] = ("copy button", "new-chat button")
        self.start_chat_ok = True
        # Set by a test to run when start_chat is asked - the hook the abort
        # tests use to fire /abort at an exact point in the sub-run.
        self.on_start_chat: Callable[[], None] | None = None

        self._answer_pending = False

    # -- transcript -----------------------------------------------------------

    async def add_user(self, text: str) -> None:
        self.events.append(("user", text))

    async def add_prose(self, text: str) -> None:
        self.events.append(("prose", text))

    async def add_call(self, call: ToolCall) -> None:
        self.events.append(("call", f"{call.tool} {call.params.get('path', '')}".strip()))

    async def add_note(self, text: str) -> None:
        self.events.append(("note", text))

    async def add_error(self, text: str) -> None:
        self.events.append(("error", text))

    async def add_outbound(self, outbound: Outbound, label: str) -> None:
        self.events.append(("outbound", label))

    async def clear_transcript(self) -> None:
        self.cleared += 1
        self.events.clear()

    def has_transcript_events(self) -> bool:
        return bool(self.events)

    def render_log(self, meta_lines: list[str]) -> str:
        return "\n".join(meta_lines + [text for _, text in self.events])

    # -- state + chrome -------------------------------------------------------

    def render_state(self, view: SessionView) -> None:
        self.states.append(view)
        if view.awaiting_answer and self.answers and not self._answer_pending:
            self._answer_pending = True
            self._later(self._deliver_answer)

    def show_gate(self, action: PendingAction, position: str, queue: str) -> None:
        self.gates.append((action, position))
        self._later(self._resolve_gate)

    def hide_gate(self) -> None:
        pass

    def start_working(self, label: str, calls: Sequence[RunCall] = ()) -> None:
        self.working.append((label, list(calls)))

    def stop_working(self) -> None:
        pass

    # -- the turn executing, call by call (the controller pushes from a thread)

    def call_started(self, call_id: int, tool: str, detail: str) -> None:
        self.call_events.append(("started", call_id, tool or detail))

    def call_finished(self, call_id: int, glyph: str) -> None:
        self.call_events.append(("finished", call_id, glyph))

    def call_output(self, call_id: int, chunk: str) -> None:
        self.call_output_chunks.append((call_id, chunk))

    def reset_composer(self) -> None:
        pass

    # -- notifications --------------------------------------------------------

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notifications.append((message, severity))

    def alert(self, message: str, severity: Severity = "information") -> None:
        self.alerts.append((message, severity))

    # -- clipboard / transport ------------------------------------------------

    async def copy_outbound(self, text: str) -> None:
        self.trace.append("copy")
        self.copied.append(text)

    async def park_outbound(self, text: str) -> None:
        # Stage one of `c`: the clipboard write WITHOUT the delivery. Recorded
        # separately from ``copied`` on purpose - the whole point of the split
        # is that a re-copy does not click or paste, and a fake that folded the
        # two together could not tell a test which one happened.
        self.trace.append("park")
        self.parked.append(text)

    def redeliver_outbound(self, text: str) -> None:
        # Stage two, the double tap. Fire-and-forget in the real view (it
        # schedules a worker), so the fake records and returns - and it records
        # into ``copied`` as well, because a re-delivery IS a ``copy_outbound``
        # over there and a test asserting what reached the chat should see it.
        self.trace.append("redeliver")
        self.redelivered.append(text)
        self.copied.append(text)

    def open_new_chat_now(self) -> None:
        # Only /new asks for one; the count is what pins that scope down. The
        # real view clicks the browser and calls request_new_session back off a
        # worker WHETHER OR NOT that click landed (view.py): the tool side is the
        # half it can always deliver, and the browser half becomes a line in a
        # toast. So ``new_chat_lands`` scripts only what the browser did - a fact
        # the controller is never told, which is why it changes nothing here but
        # the trace. ``_later`` keeps the call-back out of the command's own
        # stack frame the way the worker does.
        self.new_chats_opened += 1
        self.trace.append("open-new-chat" if self.new_chat_lands else "open-new-chat-refused")
        self._later(self._reset_after_new_chat)

    def show_identify_overlay(self) -> None:
        # /identify is one call and no answer - the whole feature is the view's.
        self.identify_overlays += 1

    def toggle_harness_log(self) -> None:
        # /log, the same shape again: the decisions it lists were all taken on
        # this side of the port, so the controller only asks for the pane.
        self.harness_log_toggles += 1

    def set_os_armed(self, target: bool | None) -> None:
        # Same shape as show_identify_overlay: one call, no answer, no session.
        self.armed_targets.append(target)
        self.os_armed = (not self.os_armed) if target is None else target

    async def read_clipboard(self) -> str | None:
        return self.clipboard

    def start_input(self) -> None:
        self.input_started += 1

    def stop_input(self) -> None:
        pass

    # -- session views --------------------------------------------------------

    async def open_session_view(self, session: SessionRef) -> None:
        self.trace.append(f"open:{session.id}")
        self.opened.append(session)
        self.focused.append(session.id)

    def focus_session_view(self, session_id: str) -> None:
        self.focused.append(session_id)

    async def finish_session_view(self, session_id: str, note: str, ok: bool) -> None:
        self.trace.append(f"finish:{session_id}" if ok else f"finish-failed:{session_id}")
        self.finished.append((session_id, note, ok))

    # -- sub-agent transport --------------------------------------------------

    def delegation_available(self) -> bool:
        return self.delegation

    def delegation_missing(self) -> tuple[str, ...]:
        return () if self.delegation else self.missing

    async def start_chat(self, session: SessionRef) -> bool:
        self.trace.append("start_chat")
        self.chats_started.append(session)
        if self.on_start_chat is not None:
            self.on_start_chat()
        return self.start_chat_ok

    async def end_chat(self, session: SessionRef) -> None:
        self.trace.append("end_chat")
        self.chats_ended.append(session)

    # -- scheduling + lifecycle ----------------------------------------------

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.tasks.append(asyncio.get_event_loop().create_task(coro))

    def exit_app(self) -> None:
        self.exited = True

    # -- blocking prompts -----------------------------------------------------

    async def prompt_new_session(self) -> SessionSpec | None:
        return self.specs.pop(0) if self.specs else None

    async def confirm(self, title: str, body: str = "") -> bool:
        return self.confirm_result

    async def prompt_text(self, title: str, hint: str) -> str | None:
        return self.text_result

    async def show_summary(self, rows: list[tuple[str, str]], summary: str) -> str:
        return self.summary_action

    # -- internals ------------------------------------------------------------

    def _later(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` once the controller has parked on the future it just
        announced - the callback would otherwise race the future's creation."""
        with suppress(RuntimeError):  # no loop (a purely synchronous unit test)
            asyncio.get_running_loop().call_soon(fn)

    def _reset_after_new_chat(self) -> None:
        if self.controller is not None:
            self.controller.request_new_session()

    def _resolve_gate(self) -> None:
        if self.controller is not None:
            self.controller.submit_decision(*self.decision)

    def _deliver_answer(self) -> None:
        self._answer_pending = False
        if self.controller is not None and self.answers:
            self.controller.submit_message(self.answers.pop(0))

    # -- assertions helpers ---------------------------------------------------

    def notes(self) -> list[str]:
        return [text for kind, text in self.events if kind == "note"]

    def errors(self) -> list[str]:
        return [text for kind, text in self.events if kind == "error"]

    def toasts(self) -> list[str]:
        return [message for message, _ in self.notifications]


# -- wiring -------------------------------------------------------------------


def make_factory(root: Path) -> Callable[[EngineRequest | str], Engine]:
    """The real engine factory, with the chat names pinned per role.

    Real, because the delegation wiring the controller does - role, catalog
    gating, parent chat name - only means anything if a real Engine is built
    from it; pinned, because the canned replies have to name a chat.
    """
    base = make_engine_factory(
        lambda: load_config(root, global_config_path=root / "no-such-global.toml"), root
    )
    subs = iter(SUB_CHATS)

    def build(request: EngineRequest | str) -> Engine:
        req = EngineRequest(service=request) if isinstance(request, str) else request
        if req.chat_name is None:
            name = MASTER_CHAT if req.role == "master" else next(subs)
            req = replace(req, chat_name=name)
        return base(req)

    return build


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "utils.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return root


@pytest.fixture
def app_config(project: Path) -> Config:
    return load_config(project, global_config_path=project / "no-such-global.toml")


@pytest.fixture
def view() -> FakeChatView:
    return FakeChatView()


@pytest.fixture
def controller(project: Path, app_config: Config, view: FakeChatView) -> SessionController:
    ctrl = SessionController(app_config, make_factory(project), project, view=view)
    view.controller = ctrl
    return ctrl


# -- driving ------------------------------------------------------------------


async def wait_for(predicate: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


async def settle(view: FakeChatView, timeout: float = 20.0) -> None:
    """Let every spawned flow finish - or reach the one park that never does.

    A sub-run waiting for its chat's next paste is a legitimate resting state
    (the transport is a human), so it counts as settled; anything else runs to
    completion, and a flow that died takes the test down with it.
    """
    controller = view.controller
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.005)
        if all(task.done() for task in view.tasks):
            break
        if controller is not None and controller._reply_future is not None:
            break
    for task in list(view.tasks):
        if task.done() and not task.cancelled():
            task.result()  # re-raise anything a flow swallowed into the task


async def start_session(
    controller: SessionController,
    view: FakeChatView,
    task: str = "Tidy up src/utils.py.",
    *,
    service: str = "claude",
    subagent_service: str = "",
) -> None:
    """Arm a session from a spec, which carries a service PER ROLE.

    ``subagent_service`` blank means the sub-agent window is on the same service
    as the master's - the shape of every front-end that has one picker, and of
    every test here that does not care.
    """
    view.specs.append(
        SessionSpec(task=task, service=service, subagent_service=subagent_service)
    )
    controller.start()
    await wait_for(lambda: view.input_started > 0, "session armed")
    await settle(view)


# -- canned replies -----------------------------------------------------------


def _wrap(body: str, chat: str, calls: int) -> str:
    return f"{body}\n===CLIP:EOM calls={calls} chat={chat}===\n"


def delegate_reply(task: str, *, context: str | None = None, chat: str = MASTER_CHAT) -> str:
    ctx = f"context <<EOT\n{context}\nEOT\n" if context else ""
    return _wrap(
        "Handing this off.\n\n"
        "===CLIP:CALL id=1 tool=delegate===\n"
        f"task <<EOT\n{task}\nEOT\n{ctx}"
        "===CLIP:END===",
        chat,
        1,
    )


def read_then_delegate_reply(path: str, task: str, *, chat: str = MASTER_CHAT) -> str:
    """A read, then a delegate, then a read: proves the turn RESUMES after the
    sub-run instead of being cut short by it."""
    return _wrap(
        "===CLIP:CALL id=1 tool=read_file===\n"
        f"path: {path}\n"
        "===CLIP:END===\n"
        "===CLIP:CALL id=2 tool=delegate===\n"
        f"task <<EOT\n{task}\nEOT\n"
        "===CLIP:END===\n"
        "===CLIP:CALL id=3 tool=list_dir===\n"
        "path: src\n"
        "===CLIP:END===",
        chat,
        3,
    )


def task_done_reply(summary: str, *, result: str = "", chat: str = MASTER_CHAT) -> str:
    body = "===CLIP:CALL id=1 tool=task_done===\n" f"summary <<EOT\n{summary}\nEOT\n"
    if result:
        body += f"result <<EOT\n{result}\nEOT\n"
    return _wrap(body + "===CLIP:END===", chat, 1)


def read_file_reply(path: str, *, chat: str) -> str:
    return _wrap(
        f"===CLIP:CALL id=1 tool=read_file===\npath: {path}\n===CLIP:END===",
        chat,
        1,
    )


def edit_reply(path: str, find: str, replace_with: str, *, chat: str) -> str:
    return _wrap(
        "===CLIP:CALL id=1 tool=edit_file===\n"
        f"path: {path}\n"
        f"find <<EOT\n{find}\nEOT\n"
        f"replace <<EOT\n{replace_with}\nEOT\n"
        "===CLIP:END===",
        chat,
        1,
    )


def ask_user_reply(question: str, *, chat: str) -> str:
    return _wrap(
        f"===CLIP:CALL id=1 tool=ask_user===\nquestion <<EOT\n{question}\nEOT\n===CLIP:END===",
        chat,
        1,
    )


MARKER = "running.txt"


def slow_command_reply(seconds: int, *, chat: str) -> str:
    """A real, long-running command that announces itself first.

    The marker file (dropped in run_command's cwd, i.e. the project root) is how
    a test knows the command is genuinely mid-flight. Cancelling any earlier is
    a race the engine deliberately loses: every plan run clears the cancel flag
    before its first call, so a cancel that arrives in the instant between
    "executing" going true and the plan starting is discarded on purpose.
    """
    command = (
        "python -c \"import pathlib, time; "
        f"pathlib.Path('{MARKER}').write_text('go'); time.sleep({seconds})\""
    )
    return _wrap(
        f"===CLIP:CALL id=1 tool=run_command===\ncommand: {command}\n"
        f"reason: wait for the marker\ntimeout: {seconds}\n"
        "===CLIP:END===",
        chat,
        1,
    )
