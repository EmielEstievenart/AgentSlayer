"""run_command tests: roundtrip, merged output, timeout/cancel kill, tail cap."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agentclip.config import BudgetCaps, Config, LimitsConfig, caps_for_budget
from agentclip.hosts import FakeHost, Host, LocalHost
from agentclip.protocol.types import ToolCall
from agentclip.tools import shell
from agentclip.tools.registry import ToolContext
from agentclip.tools.sandbox import Workspace

# "python" is on PATH inside the uv-managed venv on every platform.
PY = "python -c"


def make_call(**params: str) -> ToolCall:
    return ToolCall(id=1, tool="run_command", params=dict(params), raw="")


def make_ctx(
    root: Path,
    *,
    limits: LimitsConfig | None = None,
    caps: BudgetCaps | None = None,
    cancel_event: threading.Event | None = None,
    host: Host | None = None,
) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root, Config().excluded_names()),
        limits=limits or LimitsConfig(),
        caps=caps or caps_for_budget(12_000),
        host=host or LocalHost(),
        cancel_event=cancel_event,
    )


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return make_ctx(tmp_path)


def test_echo_roundtrip_exit_line_first(ctx: ToolContext) -> None:
    res = shell.run_command(ctx, make_call(command=f"{PY} \"print('hi from agentclip')\""))
    assert res.status == "ok"
    first = res.body.split("\n")[0]
    assert first.startswith("exit 0 (") and first.endswith("s)")
    assert "hi from agentclip" in res.body


def test_nonzero_exit_code_reported(ctx: ToolContext) -> None:
    res = shell.run_command(ctx, make_call(command=f'{PY} "import sys; sys.exit(3)"'))
    assert res.status == "ok"  # a failing command is still a successful tool call
    assert res.body.split("\n")[0].startswith("exit 3 (")


def test_stdout_and_stderr_merged(ctx: ToolContext) -> None:
    code = "import sys; print('to-out'); print('to-err', file=sys.stderr)"
    res = shell.run_command(ctx, make_call(command=f'{PY} "{code}"'))
    assert "to-out" in res.body and "to-err" in res.body


def test_cwd_is_workspace_root(ctx: ToolContext, tmp_path: Path) -> None:
    res = shell.run_command(ctx, make_call(command=f'{PY} "import os; print(os.getcwd())"'))
    assert res.status == "ok"
    reported = res.body.split("\n", 1)[1].strip()
    assert Path(reported).resolve() == tmp_path.resolve()


def test_timeout_kills_and_reports_exec_timeout(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    start = time.monotonic()
    res = shell.run_command(
        ctx, make_call(command=f'{PY} "import time; time.sleep(30)"', timeout="1")
    )
    elapsed = time.monotonic() - start
    assert res.status == "error" and res.code == "exec_timeout"
    assert "timed out after 1s" in res.body
    assert res.body.splitlines()[-1].startswith("hint: ")
    assert elapsed < 25  # the sleep was killed, we did not wait it out


def test_timeout_capped_by_limits(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, limits=LimitsConfig(command_timeout_s=1))
    res = shell.run_command(
        ctx, make_call(command=f'{PY} "import time; time.sleep(30)"', timeout="999")
    )
    assert res.status == "error" and res.code == "exec_timeout"
    assert "timed out after 1s" in res.body  # effective timeout is the configured cap


def _sleeper(marker: Path, seconds: int = 30) -> str:
    """A command that announces itself (flushed), drops a marker file, then
    sleeps - so a test can cancel it at a moment its output provably exists."""
    code = (
        "import pathlib, time; print('before-sleep', flush=True); "
        f"pathlib.Path('{marker.as_posix()}').write_text('go'); time.sleep({seconds})"
    )
    return f'{PY} "{code}"'


def _set_when_running(marker: Path, event: threading.Event) -> threading.Thread:
    """Fire ``event`` once the command is demonstrably mid-flight."""

    def wait_then_set() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        event.set()

    thread = threading.Thread(target=wait_then_set, daemon=True)
    thread.start()
    return thread


def test_cancel_kills_the_process_and_keeps_partial_output(tmp_path: Path) -> None:
    """The user's cancel is not a timeout: same kill, distinct code, and the
    output the command had already produced still reaches the model."""
    event = threading.Event()
    ctx = make_ctx(tmp_path, cancel_event=event)
    marker = tmp_path / "running.txt"
    watcher = _set_when_running(marker, event)

    start = time.monotonic()
    res = shell.run_command(ctx, make_call(command=_sleeper(marker), timeout="30"))
    elapsed = time.monotonic() - start
    watcher.join(timeout=5)

    assert res.status == "error" and res.code == "cancelled"
    assert "cancelled by the user before completion" in res.body
    assert "before-sleep" in res.body  # the partial output survived the kill
    assert res.body.splitlines()[-1].startswith("hint: ")
    assert elapsed < 25  # the sleep was killed, not waited out


def test_cancel_set_upfront_bails_out_immediately(tmp_path: Path) -> None:
    event = threading.Event()
    event.set()
    ctx = make_ctx(tmp_path, cancel_event=event)
    start = time.monotonic()
    res = shell.run_command(
        ctx, make_call(command=f'{PY} "import time; time.sleep(30)"', timeout="30")
    )
    assert res.status == "error" and res.code == "cancelled"
    assert time.monotonic() - start < 25


def test_timeout_still_reports_exec_timeout_with_a_cancel_event_present(tmp_path: Path) -> None:
    """The two kill paths stay distinguishable: an uncancelled slow command is
    still exec_timeout, never cancelled."""
    ctx = make_ctx(tmp_path, cancel_event=threading.Event())
    res = shell.run_command(
        ctx, make_call(command=f'{PY} "import time; time.sleep(30)"', timeout="1")
    )
    assert res.status == "error" and res.code == "exec_timeout"
    assert "timed out after 1s" in res.body


def test_tail_cap_keeps_the_end(tmp_path: Path) -> None:
    caps = BudgetCaps(600, 100, command_tail_lines=5, command_tail_chars=400,
                      listing_max_entries=400, advised_max_calls=8)
    ctx = make_ctx(tmp_path, caps=caps)
    code = "print('\\n'.join('line' + str(i) for i in range(50)))"
    res = shell.run_command(ctx, make_call(command=f'{PY} "{code}"'))
    assert res.status == "ok"
    assert "[truncated: showing last 5 of 50 output lines]" in res.body
    assert "line49" in res.body  # the tail survives
    assert "line0\n" not in res.body  # the head is gone


def test_char_cap_via_limits(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, limits=LimitsConfig(max_command_output_chars=200))
    code = "print('\\n'.join('line' + str(i) for i in range(100)))"
    res = shell.run_command(ctx, make_call(command=f'{PY} "{code}"'))
    assert "[truncated:" in res.body
    assert "line99" in res.body
    assert len(res.body) < 600


def test_missing_command_param(ctx: ToolContext) -> None:
    res = shell.run_command(ctx, make_call())
    assert res.status == "error" and res.code == "missing_param"
    assert "command" in res.body
    assert res.body.splitlines()[-1].startswith("hint: ")


def test_bad_timeout_values(ctx: ToolContext) -> None:
    res = shell.run_command(ctx, make_call(command="echo hi", timeout="soon"))
    assert res.status == "error" and res.code == "bad_param"
    res = shell.run_command(ctx, make_call(command="echo hi", timeout="0"))
    assert res.status == "error" and res.code == "bad_param"


def test_preview_shows_command_and_cwd(ctx: ToolContext, tmp_path: Path) -> None:
    text = shell.preview_run_command(ctx, make_call(command="pytest -q"))
    assert text.split("\n")[0] == "pytest -q"
    assert text.split("\n")[1] == f"cwd: {ctx.workspace.root}"


# -- through the host seam (scripted, no real processes) -----------------------


def test_the_command_reaches_the_host_with_the_workspace_root_as_cwd(tmp_path: Path) -> None:
    host = FakeHost()
    ctx = make_ctx(tmp_path, host=host)
    shell.run_command(ctx, make_call(command="pytest -q"))
    assert host.commands == [("pytest -q", ctx.workspace.root)]


def test_exit_code_and_output_are_the_hosts_answer(tmp_path: Path) -> None:
    host = FakeHost()
    host.script("pytest -q", exit_code=2, output="1 failed, 3 passed")
    res = shell.run_command(make_ctx(tmp_path, host=host), make_call(command="pytest -q"))
    assert res.status == "ok"
    assert res.body == "exit 2 (0.0s)\n1 failed, 3 passed"


def test_timeout_kills_the_handle_and_keeps_the_drained_tail(tmp_path: Path) -> None:
    host = FakeHost()
    host.script("slow", hangs=True, output="halfway there")
    res = shell.run_command(
        make_ctx(tmp_path, host=host), make_call(command="slow", timeout="1")
    )
    assert res.status == "error" and res.code == "exec_timeout"
    assert "halfway there" in res.body
    assert host.handles[0].killed and host.handles[0].drained


def test_cancel_kills_the_handle_and_reports_cancelled(tmp_path: Path) -> None:
    event = threading.Event()
    event.set()
    host = FakeHost()
    host.script("slow", hangs=True, output="halfway there")
    res = shell.run_command(
        make_ctx(tmp_path, host=host, cancel_event=event),
        make_call(command="slow", timeout="30"),
    )
    assert res.status == "error" and res.code == "cancelled"
    assert "halfway there" in res.body
    assert host.handles[0].killed
