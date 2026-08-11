"""LocalHost primitives: the real filesystem and real subprocesses."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from agentclip.hosts import ExecResult, LocalHost

PY = "python -c"


@pytest.fixture
def host() -> LocalHost:
    return LocalHost()


# -- files ---------------------------------------------------------------------


def test_write_bytes_creates_parent_directories(host: LocalHost, tmp_path: Path) -> None:
    host.write_bytes(tmp_path / "a" / "b" / "c.txt", b"hello")
    assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"hello"


def test_write_bytes_append_extends(host: LocalHost, tmp_path: Path) -> None:
    target = tmp_path / "log.txt"
    host.write_bytes(target, b"one\n")
    host.write_bytes(target, b"two\n", append=True)
    assert target.read_bytes() == b"one\ntwo\n"


def test_read_bytes_max_bytes_reads_only_a_prefix(host: LocalHost, tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 100)
    assert host.read_bytes(target, max_bytes=10) == b"x" * 10
    assert host.read_bytes(target) == b"x" * 100


def test_read_bytes_missing_raises_filenotfound(host: LocalHost, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        host.read_bytes(tmp_path / "nope.txt")


def test_delete_removes_the_file(host: LocalHost, tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    target.write_text("x")
    host.delete(target)
    assert not target.exists()


# -- metadata ------------------------------------------------------------------


def test_stat_of_a_file_reports_size(host: LocalHost, tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_bytes(b"12345")
    st = host.stat(tmp_path / "f.txt")
    assert st is not None
    assert (st.is_file, st.is_dir, st.is_symlink, st.size) == (True, False, False, 5)


def test_stat_of_a_directory(host: LocalHost, tmp_path: Path) -> None:
    st = host.stat(tmp_path)
    assert st is not None and st.is_dir and not st.is_file


def test_stat_of_a_missing_path_is_none(host: LocalHost, tmp_path: Path) -> None:
    assert host.stat(tmp_path / "nope") is None
    assert host.lstat(tmp_path / "nope") is None


def test_listdir_reports_types_and_sizes(host: LocalHost, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "f.txt").write_bytes(b"abc")
    by_name = {e.name: e for e in host.listdir(tmp_path)}
    assert by_name["sub"].is_dir and not by_name["sub"].is_file
    assert by_name["f.txt"].is_file and by_name["f.txt"].size == 3
    assert not any(e.is_symlink for e in by_name.values())


def test_listdir_of_a_missing_directory_raises(host: LocalHost, tmp_path: Path) -> None:
    with pytest.raises(OSError):
        host.listdir(tmp_path / "nope")


def test_realpath_normalizes_dotdot(host: LocalHost, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert host.realpath(tmp_path / "sub" / "..") == tmp_path.resolve()


def test_realpath_strict_requires_existence(host: LocalHost, tmp_path: Path) -> None:
    assert host.realpath(tmp_path / "nope")  # non-strict: no complaint
    with pytest.raises(OSError):
        host.realpath(tmp_path / "nope", strict=True)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_stat_follows_and_lstat_does_not(host: LocalHost, tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_bytes(b"ab")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    st = host.stat(tmp_path / "link.txt")
    lst = host.lstat(tmp_path / "link.txt")
    assert st is not None and st.is_file and st.is_symlink and st.size == 2
    assert lst is not None and lst.is_symlink and not lst.is_file


# -- exec ----------------------------------------------------------------------


def _run(host: LocalHost, command: str, cwd: Path, deadline_s: float = 20.0) -> ExecResult:
    handle = host.spawn(command, cwd)
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        result = handle.wait(0.2)
        if result is not None:
            return result
    raise AssertionError("command did not finish in time")


def test_spawn_merges_stdout_and_stderr(host: LocalHost, tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    result = _run(host, f'{PY} "{code}"', tmp_path)
    assert result.exit_code == 0
    assert "out" in result.output and "err" in result.output


def test_spawn_reports_the_exit_code(host: LocalHost, tmp_path: Path) -> None:
    assert _run(host, f'{PY} "import sys; sys.exit(3)"', tmp_path).exit_code == 3


def test_spawn_runs_in_the_given_cwd(host: LocalHost, tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here")
    result = _run(host, f"{PY} \"print(open('marker.txt').read())\"", tmp_path)
    assert "here" in result.output


def test_wait_is_none_while_running_and_kill_drains_the_tree(
    host: LocalHost, tmp_path: Path
) -> None:
    code = "import sys, time; print('started'); sys.stdout.flush(); time.sleep(30)"
    handle = host.spawn(f'{PY} "{code}"', tmp_path)
    assert handle.wait(0.2) is None  # still running
    handle.kill()
    assert "started" in handle.drain(5.0)  # the partial output survives the kill
