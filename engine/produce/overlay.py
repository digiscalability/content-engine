"""Overlay rendering: the `OverlayRenderer` slot of SRD §7.2, filled by HyperFrames (ADR 0001).

Captions, title cards, lower thirds and credit cards are HyperFrames compositions under
`hyperframes/`, rendered locally to ProRes 4444 MOV with alpha and composited by ffmpeg.
Remotion would fill the same slot behind the same interface (C9).

Every call pins the CLI version and switches off telemetry, update checks, skill installs
and npm registry checks, so a warm render makes no network calls (ADR 0001).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from engine.produce.process import Runner, output_tail, run_command

HYPERFRAMES_VERSION = "0.9.40"
HYPERFRAMES_ENV: dict[str, str] = {
    "HYPERFRAMES_NO_TELEMETRY": "1",
    "HYPERFRAMES_NO_UPDATE_CHECK": "1",
    "HYPERFRAMES_SKIP_SKILLS": "1",  # never touch ~/.claude/skills
    "npm_config_prefer_offline": "true",  # npx uses the cached CLI without asking the registry
}


class OverlayRenderError(RuntimeError):
    """The overlay renderer could not produce its output."""


@runtime_checkable
class OverlayRenderer(Protocol):
    def render(self, composition_dir: Path, output: Path) -> Path:
        """Render a composition to an alpha video at `output` and return that path."""
        ...


@dataclass
class HyperFramesRenderer:
    run: Runner = run_command
    which: Callable[[str], str | None] = shutil.which

    def command(self, composition_dir: Path, output: Path) -> list[str]:
        npx = self.which("npx")
        if npx is None:
            raise OverlayRenderError(
                "npx not found: HyperFrames needs Node.js 22+ on PATH (see `ce doctor`)"
            )
        return [
            npx, "--yes", f"hyperframes@{HYPERFRAMES_VERSION}", "render", str(composition_dir),
            "--format", "mov",  # ProRes 4444 with alpha; ffmpeg reads its alpha by default
            "--output", str(output),
            "--strict",  # a lint error fails the render instead of shipping a broken overlay
        ]  # fmt: skip

    def render(self, composition_dir: Path, output: Path) -> Path:
        command = self.command(composition_dir, output)
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.run(command, HYPERFRAMES_ENV)
        if result.returncode != 0:
            raise OverlayRenderError(
                f"HyperFrames render failed (exit {result.returncode}): {output_tail(result)}"
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise OverlayRenderError(f"HyperFrames render left no output at {output}")
        return output
