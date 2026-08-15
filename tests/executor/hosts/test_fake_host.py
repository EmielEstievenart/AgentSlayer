"""FakeHost behaves like a filesystem, so tests written against it mean something."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentclip.executor.hosts import FakeHost


@pytest.fixture
def host() -> FakeHost:
    return FakeHost("/project")


def test_root_exists_as_a_directory(host: FakeHost) -> None:
    st = host.stat(host.root)
    assert st is not None and st.is_dir


def test_write_then_read_roundtrip(host: FakeHost) -> None:
    host.write_bytes(host.root / "a" / "b.txt", b"hello")
    assert host.read_bytes(host.root / "a" / "b.txt") == b"hello"
    parent = host.stat(host.root / "a")
    assert parent is not None and parent.is_dir  # parents are created


def test_write_append_extends(host: FakeHost) -> None:
    host.write_bytes(host.root / "log", b"one\n")
    host.write_bytes(host.root / "log", b"two\n", append=True)
    assert host.text(host.root / "log") == "one\ntwo\n"


def test_read_bytes_max_bytes_prefix(host: FakeHost) -> None:
    host.add_file(host.root / "big", b"x" * 50)
    assert host.read_bytes(host.root / "big", max_bytes=5) == b"x" * 5


def test_missing_file_raises_filenotfound(host: FakeHost) -> None:
    with pytest.raises(FileNotFoundError):
        host.read_bytes(host.root / "nope")


def test_delete_removes_and_then_404s(host: FakeHost) -> None:
    host.add_file(host.root / "f", "x")
    host.delete(host.root / "f")
    assert host.stat(host.root / "f") is None
    with pytest.raises(FileNotFoundError):
        host.delete(host.root / "f")


def test_listdir_sees_files_dirs_and_sizes(host: FakeHost) -> None:
    host.add_file(host.root / "f.txt", "abc")
    host.add_dir(host.root / "sub")
    by_name = {e.name: e for e in host.listdir(host.root)}
    assert set(by_name) == {"f.txt", "sub"}
    assert by_name["f.txt"].is_file and by_name["f.txt"].size == 3
    assert by_name["sub"].is_dir and by_name["sub"].size == 0


def test_listdir_of_a_file_is_an_oserror(host: FakeHost) -> None:
    host.add_file(host.root / "f.txt", "x")
    with pytest.raises(OSError):
        host.listdir(host.root / "f.txt")


def test_realpath_resolves_dotdot(host: FakeHost) -> None:
    host.add_dir(host.root / "sub")
    assert host.realpath(host.root / "sub" / "..") == Path("/project")


def test_realpath_strict_requires_existence(host: FakeHost) -> None:
    with pytest.raises(FileNotFoundError):
        host.realpath(host.root / "nope", strict=True)


def test_symlink_stat_follows_lstat_does_not(host: FakeHost) -> None:
    host.add_file("/outside/secret.txt", "s3cret")
    host.add_symlink(host.root / "link.txt", "/outside/secret.txt")
    st = host.stat(host.root / "link.txt")
    lst = host.lstat(host.root / "link.txt")
    assert st is not None and st.is_file and st.is_symlink
    assert lst is not None and lst.is_symlink and not lst.is_file
    assert host.realpath(host.root / "link.txt") == Path("/outside/secret.txt")
    assert host.read_bytes(host.root / "link.txt") == b"s3cret"


def test_symlinked_directory_component_is_followed(host: FakeHost) -> None:
    host.add_file("/elsewhere/deep/f.txt", "found")
    host.add_symlink(host.root / "link", "/elsewhere")
    assert host.realpath(host.root / "link" / "deep" / "f.txt") == Path("/elsewhere/deep/f.txt")


def test_symlink_loop_raises(host: FakeHost) -> None:
    host.add_symlink("/a", "/b")
    host.add_symlink("/b", "/a")
    with pytest.raises(OSError):
        host.realpath(Path("/a"))


def test_spawn_returns_the_scripted_result_and_records_the_call(host: FakeHost) -> None:
    host.script("pytest -q", exit_code=1, output="1 failed")
    handle = host.spawn("pytest -q", host.root)
    result = handle.wait(0.1)
    assert result is not None and (result.exit_code, result.output) == (1, "1 failed")
    assert host.commands == [("pytest -q", host.root)]


def test_unscripted_commands_get_the_default_result(host: FakeHost) -> None:
    result = host.spawn("anything", host.root).wait(0.1)
    assert result is not None and result.exit_code == 0


def test_a_hanging_command_never_finishes_and_can_be_killed(host: FakeHost) -> None:
    host.script("sleep 99", hangs=True, output="partial")
    handle = host.spawn("sleep 99", host.root)
    assert handle.wait(0.0) is None
    handle.kill()
    assert handle.killed and handle.drain(0.0) == "partial"


def test_scripted_chunks_are_revealed_one_slice_at_a_time(host: FakeHost) -> None:
    """The streaming half of the seam: peek() grows, then the command finishes."""
    host.script("build", chunks=("compiling\n", "linking\n"))
    handle = host.spawn("build", host.root)
    assert handle.peek() == ""
    assert handle.wait(0.0) is None and handle.peek() == "compiling\n"
    assert handle.wait(0.0) is None and handle.peek() == "compiling\nlinking\n"
    result = handle.wait(0.0)
    assert result is not None and result.output == "compiling\nlinking\n"


def test_a_command_without_chunks_streams_nothing(host: FakeHost) -> None:
    """peek() answering "" is a legal host, not a broken one (ExecHandle)."""
    host.script("quiet", output="all of it at the end")
    handle = host.spawn("quiet", host.root)
    assert handle.peek() == ""
    result = handle.wait(0.0)
    assert result is not None and result.output == "all of it at the end"
