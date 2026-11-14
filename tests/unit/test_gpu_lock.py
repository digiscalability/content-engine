"""The GPU lock (SRD §13, NFR-6): one GPU job at a time, across processes, and never stuck
behind a job that died."""

import subprocess
import sys
import time

import pytest

from engine.ops.gpu_lock import GpuBusy, GpuLock

HOLD = (
    "import sys, time\n"
    "from engine.ops.gpu_lock import GpuLock\n"
    "lock = GpuLock(sys.argv[1], job='holder')\n"
    "lock.acquire()\n"
    "print('held', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
)


def holder(path, seconds: float) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(path), str(seconds)], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout.readline().strip() == "held"
    return process


def test_the_lock_is_taken_and_given_back(tmp_path):
    lock = GpuLock(tmp_path / "gpu.lock", job="render")
    with lock:
        assert lock.held
        assert "render" in (tmp_path / "gpu.lock.owner").read_text(encoding="utf-8")
    assert not lock.held
    with GpuLock(tmp_path / "gpu.lock", timeout_s=0):
        pass  # free again


def test_a_second_process_waits_then_times_out(tmp_path):
    process = holder(tmp_path / "gpu.lock", 10)
    try:
        started = time.monotonic()
        with pytest.raises(GpuBusy, match="holder"):
            GpuLock(tmp_path / "gpu.lock", timeout_s=0.6, poll_s=0.1).acquire()
        assert time.monotonic() - started >= 0.6
    finally:
        process.kill()
        process.wait()


def test_the_lock_waits_out_a_short_holder(tmp_path):
    process = holder(tmp_path / "gpu.lock", 0.8)
    try:
        with GpuLock(tmp_path / "gpu.lock", timeout_s=10, poll_s=0.1) as lock:
            assert lock.held
    finally:
        process.wait()


def test_a_dead_holder_releases_the_lock(tmp_path):
    process = holder(tmp_path / "gpu.lock", 60)
    process.kill()  # a crashed render: the OS drops its lock
    process.wait()
    with GpuLock(tmp_path / "gpu.lock", timeout_s=2, poll_s=0.1) as lock:
        assert lock.held
