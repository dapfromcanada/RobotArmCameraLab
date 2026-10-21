"""Tests for core.locks: per-camera fcntl lock, including from a second process."""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from camera_lab.core import locks
from camera_lab.core.locks import CameraBusyError, CameraLock, LockDirError
from camera_lab.core.paths import UnsafePathError


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "xdg"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    return d


def _try_lock_in_child(serial: str, q: mp.Queue) -> None:  # type: ignore[type-arg]
    try:
        with CameraLock(serial, owner="child"):
            q.put(("acquired", ""))
    except CameraBusyError as exc:
        q.put(("busy", exc.holder))


def _run_child(serial: str) -> tuple[str, str]:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_try_lock_in_child, args=(serial, q))
    p.start()
    result: tuple[str, str] = q.get(timeout=30)
    p.join(timeout=30)
    return result


def test_second_process_gets_busy_naming_holder() -> None:
    with CameraLock("SIM-0001", owner="gui") as lock:
        assert lock.held
        status, holder = _run_child("SIM-0001")
    assert status == "busy"
    assert holder == f"pid {os.getpid()} gui"


def test_lock_released_after_exit() -> None:
    with CameraLock("SIM-0001", owner="cli"):
        pass
    assert _run_child("SIM-0001") == ("acquired", "")


def test_different_serials_do_not_conflict() -> None:
    with CameraLock("SIM-0001", owner="cli"):
        assert _run_child("U330439") == ("acquired", "")


def test_same_process_second_open_is_busy() -> None:
    with CameraLock("233100505", owner="cli"):
        with pytest.raises(CameraBusyError, match="busy: locked by pid") as ei:
            CameraLock("233100505", owner="mcp").acquire()
        assert ei.value.serial == "233100505"


def test_lock_file_location_and_mode(runtime_dir: Path) -> None:
    with CameraLock("SIM-0001", owner="test"):
        p = runtime_dir / "camera_lab" / "SIM-0001.lock"
        assert p.read_text() == f"pid {os.getpid()} test\n"
        assert p.stat().st_mode & 0o777 == 0o600
        assert (runtime_dir / "camera_lab").stat().st_mode & 0o777 == 0o700
    assert p.read_text() == ""


def test_release_is_idempotent_and_double_acquire_errors() -> None:
    lock = CameraLock("SIM-0001", owner="test")
    lock.acquire()
    with pytest.raises(RuntimeError, match="already held"):
        lock.acquire()
    lock.release()
    lock.release()
    assert not lock.held


@pytest.mark.parametrize("serial", ["../etc", "a/b", "", "a_b", "SIM 1", "x" * 33])
def test_bad_serial(serial: str) -> None:
    with pytest.raises(UnsafePathError):
        CameraLock(serial, owner="cli")


@pytest.mark.parametrize("owner", ["", "CLI", "a b", "x" * 33, "a\nb"])
def test_bad_owner(owner: str) -> None:
    with pytest.raises(UnsafePathError):
        CameraLock("SIM-0001", owner=owner)


def test_fallback_dir_when_no_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    d = locks.lock_dir()
    assert d == Path("/tmp") / f"camera_lab-{os.getuid()}"


def test_insecure_lock_dir_rejected(runtime_dir: Path) -> None:
    d = runtime_dir / "camera_lab"
    d.mkdir(mode=0o700)
    d.chmod(0o777)
    with pytest.raises(LockDirError, match="writable"):
        locks.lock_dir()


def test_symlinked_lock_dir_rejected(runtime_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (runtime_dir / "camera_lab").symlink_to(target)
    with pytest.raises(LockDirError, match="not a directory"):
        locks.lock_dir()


def test_symlinked_lock_file_refused(runtime_dir: Path, tmp_path: Path) -> None:
    locks.lock_dir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (runtime_dir / "camera_lab" / "SIM-0001.lock").symlink_to(victim)
    with pytest.raises((LockDirError, UnsafePathError)):
        CameraLock("SIM-0001", owner="test").acquire()
    assert victim.read_text() == "keep"


def test_holder_text_is_sanitised() -> None:
    assert locks._sanitize_holder(b"pid 1 gui\x1b[31m\n") == "pid 1 gui??31m"
    assert locks._sanitize_holder(b"") == "unknown holder"
