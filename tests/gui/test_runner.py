"""The GUI runner's concurrency model, driven headless.

No window is opened here - ``GuiRunner`` deliberately knows nothing about one
(the sink is a callable, the close is a callback), which is what makes the piece
every later increment rests on testable at all. What is pinned is the lifecycle:
the loop really starts, work scheduled from a foreign thread really lands on it,
the page's ``js_api`` calls really reach the view, and a stop cancels, joins and
flushes without hanging.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agentclip.cli import make_engine_factory
from agentclip.config import Config
from agentclip.driver.clip.base import select_provider
from agentclip.gui.runner import GuiRunner
from tests.gui.conftest import Recorder


def build(project: Path, config: Config, tmp_path: Path, **kwargs: object) -> GuiRunner:
    return GuiRunner(
        config=config,
        provider=select_provider("manual"),
        engine_factory=make_engine_factory(lambda: config, project),
        project_root=project,
        profile_root=tmp_path / "profiles",
        **kwargs,  # type: ignore[arg-type]
    )


def wait_for(predicate, what: str, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# == the loop ==================================================================


def test_the_loop_starts_on_its_own_thread_and_stops_cleanly(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    runner = build(project, app_config, tmp_path)
    runner.start()
    try:
        threads = {thread.name for thread in threading.enumerate()}
        assert "agentclip-loop" in threads
        assert threading.current_thread().name not in ("agentclip-loop",)
    finally:
        runner.stop()
    wait_for(
        lambda: "agentclip-loop" not in {t.name for t in threading.enumerate()},
        "the loop thread to exit",
    )


def test_a_coroutine_scheduled_from_another_thread_runs_on_the_loop(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The clipboard watcher's path: a plain ``threading.Thread`` hands work to
    the session, and it has to arrive on the loop the controller lives on."""
    runner = build(project, app_config, tmp_path)
    runner.start()
    seen: list[str] = []
    done = threading.Event()

    async def work() -> None:
        seen.append(threading.current_thread().name)
        done.set()

    try:
        worker = threading.Thread(target=lambda: runner.schedule(work()))
        worker.start()
        worker.join()
        assert done.wait(5)
        assert seen == ["agentclip-loop"]
    finally:
        runner.stop()


def test_scheduling_from_the_loop_thread_itself_also_works(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """A controller flow spawning another flow is already ON the loop, and
    ``run_coroutine_threadsafe`` from there is exactly the deadlock the thread
    check in ``schedule`` exists to avoid."""
    runner = build(project, app_config, tmp_path)
    runner.start()
    done = threading.Event()

    async def inner() -> None:
        done.set()

    async def outer() -> None:
        runner.schedule(inner())

    try:
        runner.schedule(outer())
        assert done.wait(5)
    finally:
        runner.stop()


def test_scheduling_after_a_stop_is_dropped_rather_than_leaked(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    runner = build(project, app_config, tmp_path)
    runner.start()
    runner.stop()

    ran = False

    async def work() -> None:
        nonlocal ran
        ran = True

    coro = work()
    runner.schedule(coro)  # closes it: no "coroutine was never awaited" warning
    time.sleep(0.05)
    assert ran is False


def test_stop_cancels_the_flows_still_parked_on_the_loop(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The equivalent of Textual cancelling a screen's workers on unmount: a
    session flow parked on a future must not keep the process alive."""
    runner = build(project, app_config, tmp_path)
    runner.start()
    started = threading.Event()
    cancelled = threading.Event()

    async def parked() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner.schedule(parked())
    assert started.wait(5)
    runner.stop()
    assert cancelled.wait(5)


def test_stopping_twice_is_free(project: Path, app_config: Config, tmp_path: Path) -> None:
    """``run_gui``'s finally and a caller's own teardown both reach it."""
    runner = build(project, app_config, tmp_path)
    runner.start()
    runner.stop()
    runner.stop()


# == the page's two edges ======================================================


def test_page_loaded_starts_draining_and_starts_the_session_once(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """The first thing the page is ever told is the "describe the task" prompt,
    and a second ``loaded`` (pywebview re-fires it on navigation) must not put a
    second session flow on the same view."""
    recorder = Recorder()
    runner = build(project, app_config, tmp_path)
    runner.attach(recorder)
    runner.start()
    try:
        runner.page_loaded()
        wait_for(
            lambda: any(
                event.get("composer_mode") == "task" for event in recorder.of_type("state")
            ),
            "the task prompt to be painted",
        )
        before = len(recorder.of_type("state"))
        runner.page_loaded()
        time.sleep(0.1)
        # A second load repaints nothing on its own: no new prompt, no new flow.
        assert len(recorder.of_type("state")) == before
    finally:
        runner.stop()


def test_the_js_api_reaches_the_view_through_the_loop(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """pywebview calls js_api methods on a fresh thread per call, so every one
    of them has to marshal - the page must never touch controller state."""
    recorder = Recorder()
    runner = build(project, app_config, tmp_path)
    runner.attach(recorder)
    runner.start()
    seen: list[tuple[str, str]] = []
    runner.view.submit_text = lambda text: seen.append(("submit", text))  # type: ignore[method-assign]
    runner.view.cancel_execution = lambda: seen.append(("cancel", ""))  # type: ignore[method-assign]
    try:
        caller = threading.Thread(target=lambda: (runner.js_api.submit("hello"),
                                                  runner.js_api.cancel()))
        caller.start()
        caller.join()
        wait_for(lambda: len(seen) == 2, "both js_api calls to land")
        assert seen == [("submit", "hello"), ("cancel", "")]
    finally:
        runner.stop()


def test_every_key_action_marshals_onto_the_loop_too(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """Parity increment 2's keys take the same hop: pywebview runs each of them
    on a thread of its own, and a key that reached the controller from there
    would be racing the flow it is about to change."""
    recorder = Recorder()
    runner = build(project, app_config, tmp_path)
    runner.attach(recorder)
    runner.start()
    seen: list[str] = []

    def spy(name: str) -> Callable[..., None]:
        # A closure rather than a default argument: half of these take a
        # positional (``set_os_armed(None)``), which would shadow it.
        return lambda *args: seen.append(name)

    for name in (
        "set_os_armed",
        "cycle_permission_mode",
        "toggle_watch",
        "recopy",
        "force_ingest",
        "reinstruct",
        "retry_insert",
        "set_service",
    ):
        setattr(runner.view, name, spy(name))
    try:
        caller = threading.Thread(
            target=lambda: (
                runner.js_api.armed(None),
                runner.js_api.mode(),
                runner.js_api.watch(),
                runner.js_api.recopy(),
                runner.js_api.ingest(),
                runner.js_api.reinstruct(),
                runner.js_api.retry_insert(),
                runner.js_api.service("chatgpt"),
            )
        )
        caller.start()
        caller.join()
        wait_for(lambda: len(seen) == 8, "every key action to land on the loop")
        assert seen == [
            "set_os_armed",
            "cycle_permission_mode",
            "toggle_watch",
            "recopy",
            "force_ingest",
            "reinstruct",
            "retry_insert",
            "set_service",
        ]
    finally:
        runner.stop()


def test_request_close_asks_the_window_and_nothing_else(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    """``exit_app`` closes the window; the teardown happens when the pump
    returns, so the two ways of quitting take exactly one path."""
    closes: list[int] = []
    runner = build(project, app_config, tmp_path, on_close=lambda: closes.append(1))
    runner.view.exit_app()
    assert closes == [1]


def test_a_window_already_gone_is_not_an_error(
    project: Path, app_config: Config, tmp_path: Path
) -> None:
    def boom() -> None:
        raise RuntimeError("window destroyed")

    runner = build(project, app_config, tmp_path, on_close=boom)
    runner.request_close()  # must not raise
