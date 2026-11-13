"""HyperFramesRenderer behind the OverlayRenderer slot (SRD §7.2, C9, NFR-8). Fake runners only."""

import subprocess
from pathlib import Path

import pytest

from engine.produce.overlay import (
    HYPERFRAMES_ENV,
    HYPERFRAMES_VERSION,
    HyperFramesRenderer,
    OverlayRenderer,
    OverlayRenderError,
)

NPX = "C:/Program Files/nodejs/npx.cmd"


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr="", write_output=True):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.write_output = write_output

    def __call__(self, argv, extra_env=None):
        self.calls.append((argv, extra_env))
        if self.write_output:
            out = Path(argv[argv.index("--output") + 1])
            out.write_bytes(b"\x00" * 64)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def renderer(run, npx=NPX) -> HyperFramesRenderer:
    return HyperFramesRenderer(run=run, which=lambda name: npx if name == "npx" else None)


def test_is_an_overlay_renderer():
    assert isinstance(renderer(FakeRun()), OverlayRenderer)


def test_command_pins_version_and_format(tmp_path):
    cmd = renderer(FakeRun()).command(tmp_path / "comp", tmp_path / "out.mov")
    assert cmd[:4] == [NPX, "--yes", f"hyperframes@{HYPERFRAMES_VERSION}", "render"]
    assert HYPERFRAMES_VERSION == "0.9.40"
    assert cmd[cmd.index("--format") + 1] == "mov"
    assert cmd[cmd.index("--output") + 1] == str(tmp_path / "out.mov")
    assert "--strict" in cmd


def test_env_turns_off_telemetry_updates_skills_and_registry_checks(tmp_path):
    run = FakeRun()
    renderer(run).render(tmp_path, tmp_path / "out.mov")
    ((_, env),) = run.calls
    assert env == HYPERFRAMES_ENV
    assert env["HYPERFRAMES_NO_TELEMETRY"] == "1"
    assert env["HYPERFRAMES_NO_UPDATE_CHECK"] == "1"
    assert env["HYPERFRAMES_SKIP_SKILLS"] == "1"
    assert env["npm_config_prefer_offline"] == "true"


def test_missing_npx_is_reported(tmp_path):
    with pytest.raises(OverlayRenderError, match="Node.js 22"):
        renderer(FakeRun(), npx=None).render(tmp_path, tmp_path / "out.mov")


def test_render_failure_carries_cli_output(tmp_path):
    run = FakeRun(
        returncode=1, stderr="✗ lint: missing timeline\nFix: register it", write_output=False
    )
    with pytest.raises(OverlayRenderError, match="exit 1") as exc:
        renderer(run).render(tmp_path, tmp_path / "out.mov")
    assert "missing timeline" in str(exc.value)


def test_render_without_output_file_fails(tmp_path):
    with pytest.raises(OverlayRenderError, match="no output"):
        renderer(FakeRun(write_output=False)).render(tmp_path, tmp_path / "out.mov")


def test_render_returns_output_when_file_written(tmp_path):
    out = tmp_path / "work" / "overlay.mov"
    assert renderer(FakeRun()).render(tmp_path, out) == out
    assert out.is_file()
