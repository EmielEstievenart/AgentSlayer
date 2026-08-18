"""What we ask a target to run, and what we say when it does not run it.

Both halves are pure functions on purpose (docs/design/remote-executor.md
§2.12): ``shell.app`` may not import ``executor.hosts``, so the classifier is
handed the two VALUES a channel can report rather than the channel - which also
makes the sentence a user reads testable without a network or a paramiko.
"""

from __future__ import annotations

from agentclip.shell.app.engine_launch import classify_launch_failure, engine_command

TARGET = "dev@box"


# -- the command ---------------------------------------------------------------


def test_the_command_names_the_console_script_and_the_project() -> None:
    assert engine_command("/srv/app") == "agentclip-engine --project /srv/app"


def test_a_remote_root_with_spaces_is_quoted_for_the_targets_shell() -> None:
    """POSIX quoting whatever THIS machine is: the target's shell reads it."""
    line = engine_command("/srv/my app")
    assert line == "agentclip-engine --project '/srv/my app'"


def test_a_service_key_rides_along_only_when_one_was_asked_for() -> None:
    assert "--service" not in engine_command("/srv/app")
    assert "--service" not in engine_command("/srv/app", None)
    assert engine_command("/srv/app", "chatgpt-attach").endswith("--service chatgpt-attach")


def test_the_command_never_carries_this_machines_paths() -> None:
    """The engine reads the TARGET's config, permissions and dirs (§2.5)."""
    line = engine_command("/srv/app", "chatgpt")
    assert "--global-config" not in line
    assert "--home" not in line
    assert "--data-root" not in line


# -- the failure ---------------------------------------------------------------


def test_exit_127_is_the_engine_not_being_installed() -> None:
    message = classify_launch_failure(127, "", TARGET)
    assert "agentclip-engine is not installed on dev@box" in message
    assert "uv tool install agentclip" in message


def test_a_shells_own_words_say_the_same_thing() -> None:
    """A login shell that exits some other way still leaves the sentence."""
    message = classify_launch_failure(
        1, "bash: agentclip-engine: command not found\n", TARGET
    )
    assert "agentclip-engine is not installed on dev@box" in message
    assert "uv tool install agentclip" in message


def test_any_other_failure_quotes_the_status_and_the_targets_stderr() -> None:
    message = classify_launch_failure(
        3, "Traceback (most recent call last):\nImportError: no agentclip\n", TARGET
    )
    assert "exit 3" in message
    assert "ImportError: no agentclip" in message
    assert "not installed" not in message


def test_a_process_still_running_is_said_so_rather_than_guessed_at() -> None:
    message = classify_launch_failure(None, "waiting for something\n", TARGET)
    assert "still running" in message
    assert "waiting for something" in message


def test_a_silent_failure_says_there_was_nothing_to_quote() -> None:
    message = classify_launch_failure(2, "   \n", TARGET)
    assert "exit 2" in message
    assert "nothing on stderr" in message
