"""`ce doctor`: tools on PATH, GPU, disk, and which `.env` keys are set or missing (SRD §5.2).

Every probe goes through a `Probes` object so tests run on fakes (NFR-8). A key's value
is never read here beyond "is it blank"; output is ASCII so a PowerShell 5.1 console
can always print it. Only a missing required tool, Node older than 22, disk below the
stop threshold, or invalid config fails the run (ADR 0004); everything else warns.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from engine.settings import KEY_SPECS, REPO_ROOT, Settings

if TYPE_CHECKING:
    from engine.config import EngineConfig

Status = Literal["ok", "warn", "fail"]
GB = 1024**3
MIN_NODE_MAJOR = 22  # HyperFrames and the Postiz CLI, both run pinned through npx
REQUIRED_TOOLS = ("git", "uv", "ffmpeg", "ffprobe")
OPTIONAL_TOOLS = {
    "yt-dlp": "needed for FR-ING-2 own-channel pulls",
}
VERSION_ARGS = {"git": ["--version"], "uv": ["--version"], "node": ["--version"]}
GPU_QUERY = ["--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]


@dataclass(frozen=True)
class Check:
    group: str
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Probes:
    which: Callable[[str], str | None]
    run: Callable[[list[str]], tuple[int, str]]
    disk_free_bytes: Callable[[Path], int]
    model_cached: Callable[[str], bool] = lambda key: True
    vec_version: Callable[[], str | None] = lambda: "v0.1.9"

    @classmethod
    def real(cls) -> Probes:
        return cls(
            which=shutil.which,
            run=_run,
            disk_free_bytes=lambda p: shutil.disk_usage(p).free,
            model_cached=_model_cached,
            vec_version=_vec_version,
        )


def _model_cached(key: str) -> bool:
    from engine.library.models import PINNED

    return PINNED[key].cached()


def _vec_version() -> str | None:
    import sqlite3

    from engine.db import connect

    try:
        return str(connect(":memory:").execute("SELECT vec_version()").fetchone()[0])
    except (sqlite3.Error, OSError, AttributeError):
        return None


def _model_checks(probes: Probes) -> list[Check]:
    from engine.library.models import PINNED

    checks = []
    for key, model in PINNED.items():
        if probes.model_cached(key):
            checks.append(Check("models", model.short_name, "ok", "cached"))
        else:
            detail = (f"missing: run `uv run ce models fetch {key}` ({model.size_mb:g} MB; "
                      f"{model.purpose})")  # fmt: skip
            checks.append(Check("models", model.short_name, "warn", detail))
    return checks


def _vec_check(probes: Probes) -> Check:
    version = probes.vec_version()
    if version is None:
        detail = "the sqlite-vec extension does not load; the library's vector tables need it"
        return Check("library", "sqlite-vec", "fail", detail)
    return Check("library", "sqlite-vec", "ok", version)


@dataclass(frozen=True)
class DoctorConfig:
    warn_free_gb: float
    stop_free_gb: float

    @classmethod
    def from_engine(cls, engine: EngineConfig) -> DoctorConfig:
        return cls(warn_free_gb=engine.disk.warn_free_gb, stop_free_gb=engine.disk.stop_free_gb)


NFR5_DISK = DoctorConfig(warn_free_gb=60, stop_free_gb=40)  # used when config/ is invalid


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    out = (done.stdout or b"") + (done.stderr or b"")
    return done.returncode, out.decode("utf-8", errors="replace")


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:80]


def _tool_check(name: str, probes: Probes, required: bool) -> tuple[Check, str | None]:
    """The check for one tool, plus its probe output when it ran cleanly."""
    path = probes.which(name)
    missing_status: Status = "fail" if required else "warn"
    if path is None:
        hint = "" if required else f" ({OPTIONAL_TOOLS[name]})"
        return Check("tools", name, missing_status, f"not on PATH{hint}"), None
    if name in OPTIONAL_TOOLS:
        return Check("tools", name, "ok", path), None
    code, out = probes.run([path, *VERSION_ARGS.get(name, ["-version"])])
    if code != 0:
        return Check("tools", name, missing_status, f"probe failed (exit {code})"), None
    return Check("tools", name, "ok", _first_line(out)), out


def _node_check(probes: Probes) -> Check:
    check, out = _tool_check("node", probes, required=True)
    if out is None:
        return check
    match = re.search(r"v?(\d+)\.", out)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        return Check("tools", "node", "fail", f"{check.detail} (HyperFrames needs Node 22+)")
    return check


def _gpu_checks(probes: Probes) -> list[Check]:
    checks: list[Check] = []
    smi = probes.which("nvidia-smi")
    if smi is None:
        checks.append(Check("gpu", "nvidia-smi", "warn", "not on PATH; GPU jobs unavailable"))
    else:
        code, out = probes.run([smi, *GPU_QUERY])
        if code != 0:
            detail = f"probe failed (exit {code}): {_first_line(out)}"
            checks.append(Check("gpu", "nvidia-smi", "warn", detail))
        else:
            checks.append(Check("gpu", "nvidia-smi", "ok", _first_line(out)))
    ffmpeg = probes.which("ffmpeg")
    if ffmpeg is None:
        checks.append(Check("gpu", "nvenc", "warn", "ffmpeg not on PATH"))
    else:
        code, out = probes.run([ffmpeg, "-hide_banner", "-encoders"])
        if code == 0 and "h264_nvenc" in out:
            checks.append(Check("gpu", "nvenc", "ok", "h264_nvenc available"))
        else:
            checks.append(Check("gpu", "nvenc", "warn", "h264_nvenc not listed by ffmpeg"))
    return checks


def _disk_check(probes: Probes, config: DoctorConfig, root: Path) -> Check:
    free_gb = probes.disk_free_bytes(root) / GB
    detail = f"{free_gb:.1f} GB free on {root.anchor or root}"
    if free_gb < config.stop_free_gb:
        limit = f"stop threshold {config.stop_free_gb:g} GB"
        return Check("disk", "free space", "fail", f"{detail}; below {limit}, ingest stops")
    if free_gb < config.warn_free_gb:
        return Check("disk", "free space", "warn", f"{detail}; below {config.warn_free_gb:g} GB")
    return Check("disk", "free space", "ok", detail)


def _config_check(problems: Sequence[str]) -> Check:
    if not problems:
        return Check("config", "config/", "ok", "valid")
    if len(problems) == 1:
        return Check("config", "config/", "fail", f"1 problem: {problems[0]}")
    return Check("config", "config/", "fail", f"{len(problems)} problems; first: {problems[0]}")


def run_checks(
    settings: Settings,
    probes: Probes,
    config: DoctorConfig,
    env_file_exists: bool,
    root: Path = REPO_ROOT,
    config_problems: Sequence[str] = (),
) -> list[Check]:
    checks = [_tool_check(name, probes, required=True)[0] for name in REQUIRED_TOOLS]
    checks.append(_node_check(probes))
    checks += [_tool_check(name, probes, required=False)[0] for name in OPTIONAL_TOOLS]
    checks += _gpu_checks(probes)
    checks.append(_disk_check(probes, config, root))
    checks += _model_checks(probes)
    checks.append(_vec_check(probes))
    checks.append(_config_check(config_problems))
    if env_file_exists:
        checks.append(Check("env", ".env file", "ok", "present"))
    else:
        detail = "not found: copy .env.example to .env and fill it (Abbas)"
        checks.append(Check("env", ".env file", "warn", detail))
    status = settings.key_status()
    for spec in KEY_SPECS:
        if status[spec.name]:
            checks.append(Check("keys", spec.name, "ok", "set"))
        else:
            checks.append(
                Check("keys", spec.name, "warn", f"missing (needed for {spec.needed_for})")
            )
    return checks


_LABELS = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}


def render(checks: list[Check]) -> str:
    lines = [f"[{_LABELS[c.status]}] {c.group:<6} {c.name:<22} {c.detail}" for c in checks]
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines.append(f"{len(checks)} checks: {fails} fail, {warns} warn")
    text = "\n".join(lines)
    return text.encode("ascii", errors="replace").decode("ascii")


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0
