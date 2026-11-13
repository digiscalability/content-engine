"""Running external tools behind an injectable runner, so tests use fakes (NFR-8)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

Runner = Callable[[list[str], "Mapping[str, str] | None"], "subprocess.CompletedProcess[str]"]


def run_command(
    argv: list[str], extra_env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a tool to completion, capturing UTF-8 output; never raises on a non-zero exit."""
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )


def output_tail(result: subprocess.CompletedProcess[str], lines: int = 12) -> str:
    """The last lines a tool printed, for error messages."""
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return "\n".join(text.strip().splitlines()[-lines:])
