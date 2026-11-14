"""The Task Scheduler wrappers (SRD §13, §2.3): one log per job per day, the exit code of
`ce` passed on, and a registration script that registers nothing without -Apply. The
scripts run for real in PowerShell, against a fake `uv`."""

import json
import os
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
pytestmark = pytest.mark.skipif(shutil.which("powershell") is None, reason="needs PowerShell")


def run_ps(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPTS / script), *args],
        capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=60,
    )  # fmt: skip


@pytest.fixture
def fake_uv(tmp_path):
    """A stand-in for uv that records its arguments and fails `ce daily` with exit 3."""
    script = tmp_path / "fake-uv.cmd"
    script.write_text(
        "@echo off\r\n"
        'echo fake %*>> "%CE_TEST_CALLS%"\r\n'
        "echo ran %*\r\n"
        'if "%3"=="daily" exit /b 3\r\n'
        "exit /b 0\r\n",
        encoding="ascii",
    )
    calls = tmp_path / "calls.txt"
    return {"CE_UV": str(script), "CE_LOG_DIR": str(tmp_path / "logs"),
            "CE_TEST_CALLS": str(calls)}, calls  # fmt: skip


def test_wrapper_passes_on_the_exit_code_and_still_writes_the_digest(fake_uv, tmp_path):
    env, calls = fake_uv
    result = run_ps("run_daily.ps1", env=env)
    assert result.returncode == 3, result.stdout + result.stderr  # ce daily failed
    assert calls.read_text().splitlines() == [
        "fake run ce daily",
        "fake run ce reconcile",
        "fake run ce digest",
    ]  # §13: reconciliation follows the daily run, even a failed one
    log = tmp_path / "logs" / f"daily-{date.today():%Y-%m-%d}.log"
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "ran run ce daily" in text and "ran run ce digest" in text and "exit 3" in text


def test_the_morning_digest_wrapper_succeeds(fake_uv, tmp_path):
    env, calls = fake_uv
    result = run_ps("run_digest.ps1", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["fake run ce digest"]
    assert (tmp_path / "logs" / f"digest-{date.today():%Y-%m-%d}.log").is_file()


def test_logs_older_than_30_days_are_pruned(fake_uv, tmp_path):
    env, _ = fake_uv
    old = tmp_path / "logs" / "digest-2026-01-01.log"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="ascii")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    run_ps("run_digest.ps1", env=env)
    assert not old.exists()


def test_register_tasks_is_a_dry_run_by_default():
    result = run_ps("register-tasks.ps1")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "would register ContentEngine-Daily" in out and "run_daily.ps1" in out
    assert "would register ContentEngine-Digest" in out
    assert "Dry run only" in out
    assert "run_weekly.ps1 does not exist yet" in out  # jobs whose command is not built
    assert "run_ingest.ps1 does not exist yet" not in out


def test_register_tasks_describes_wake_and_catch_up():
    result = run_ps("register-tasks.ps1", "-Describe", "-Only", "ContentEngine-Daily")
    assert result.returncode == 0, result.stderr
    (task,) = json.loads(result.stdout)
    assert task["Name"] == "ContentEngine-Daily" and task["At"] == ["04:00"]
    assert task["WakeToRun"] is True and task["StartWhenAvailable"] is True
    assert task["Execute"] == "conhost.exe" and "--headless powershell.exe" in task["Arguments"]
    assert task["Arguments"].endswith('run_daily.ps1"')


def test_the_film_index_wrapper_runs_until_six(fake_uv, tmp_path):
    env, calls = fake_uv
    result = run_ps("run_index_films.ps1", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["fake run ce index-films --until 06:00"]
    assert (tmp_path / "logs" / f"index-films-{date.today():%Y-%m-%d}.log").is_file()


def test_the_metrics_wrapper_reconciles_then_pulls(fake_uv, tmp_path):
    env, calls = fake_uv
    result = run_ps("run_metrics.ps1", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["fake run ce reconcile", "fake run ce metrics"]
    assert (tmp_path / "logs" / f"metrics-{date.today():%Y-%m-%d}.log").is_file()


@pytest.mark.parametrize("wrapper", ["run_daily.ps1", "run_metrics.ps1", "run_digest.ps1"])
def test_a_missing_uv_fails_the_job(fake_uv, tmp_path, wrapper):
    """With uv gone nothing ran, so the job must not report success to Task Scheduler."""
    env, _ = fake_uv
    env = {**env, "CE_UV": str(tmp_path / "no-such-uv.cmd")}
    result = run_ps(wrapper, env=env)
    assert result.returncode == 127, result.stdout + result.stderr
    name = wrapper.removeprefix("run_").removesuffix(".ps1")
    log = tmp_path / "logs" / f"{name}-{date.today():%Y-%m-%d}.log"
    assert "not found" in log.read_text(encoding="utf-8", errors="replace")


def test_register_tasks_describes_the_metrics_job():
    result = run_ps("register-tasks.ps1", "-Describe", "-Only", "ContentEngine-Metrics")
    assert result.returncode == 0, result.stderr
    (task,) = json.loads(result.stdout)
    assert task["Kind"] == "Every6Hours" and task["At"] == ["00:30"]
    assert task["Arguments"].endswith('run_metrics.ps1"')


def test_register_tasks_describes_the_film_index_job():
    result = run_ps("register-tasks.ps1", "-Describe", "-Only", "ContentEngine-IndexFilms")
    assert result.returncode == 0, result.stderr
    (task,) = json.loads(result.stdout)
    assert task["At"] == ["00:00"] and task["Arguments"].endswith('run_index_films.ps1"')


def test_the_ingest_wrapper_fetches_stock_then_runs_ce_ingest(fake_uv, tmp_path):
    env, calls = fake_uv
    result = run_ps("run_ingest.ps1", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    # FR-ING-5: the night's stock fetch lands before the ingest indexes it
    assert calls.read_text().splitlines() == ["fake run ce stock fetch", "fake run ce ingest"]
    assert (tmp_path / "logs" / f"ingest-{date.today():%Y-%m-%d}.log").is_file()


def test_register_tasks_describes_the_ingest_job():
    result = run_ps("register-tasks.ps1", "-Describe", "-Only", "ContentEngine-Ingest")
    assert result.returncode == 0, result.stderr
    (task,) = json.loads(result.stdout)
    assert task["At"] == ["02:00"] and task["Arguments"].endswith('run_ingest.ps1"')
