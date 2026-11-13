"""`ce doctor` checks (SRD §5.2, NFR-5, NFR-8). Every probe is a fake."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from engine.doctor import (
    NFR5_DISK,
    Check,
    DoctorConfig,
    Probes,
    exit_code,
    render,
    run_checks,
)
from engine.settings import KEY_SPECS, Settings

GB = 1024**3
CONFIG = DoctorConfig(warn_free_gb=60, stop_free_gb=40)
FAKE = "fake-" + "q" * 30
ROOT = Path("C:/repo")

OUTPUTS = {
    "git": (0, "git version 2.53.0.windows.1"),
    "uv": (0, "uv 0.10.9 (f675560f3 2026-03-06)"),
    "ffmpeg": (0, "ffmpeg version 8.0.1-full_build-www.gyan.dev Copyright (c) 2000-2025"),
    "ffprobe": (0, "ffprobe version 8.0.1-full_build-www.gyan.dev"),
    "node": (0, "v24.14.0"),
    "nvidia-smi": (0, "NVIDIA GeForce RTX 5070, 12227 MiB, 610.88"),
    "ffmpeg-encoders": (0, " V....D h264_nvenc  NVIDIA NVENC H.264 encoder"),
}


def fake_probes(missing=(), free_gb=100.0, **overrides) -> Probes:
    outputs = {**OUTPUTS, **{k.replace("_", "-"): v for k, v in overrides.items()}}

    def which(name: str) -> str | None:
        return None if name in missing else f"C:/bin/{name}.exe"

    def run(argv: list[str]) -> tuple[int, str]:
        tool = Path(argv[0]).stem
        key = "ffmpeg-encoders" if tool == "ffmpeg" and "-encoders" in argv else tool
        return outputs.get(key, (0, ""))

    return Probes(which=which, run=run, disk_free_bytes=lambda path: int(free_gb * GB))


def checks_for(probes: Probes, settings: Settings | None = None, env_file_exists=True):
    return run_checks(settings or Settings(), probes, CONFIG, env_file_exists, root=ROOT)


def find(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


def test_all_good_exits_zero():
    checks = checks_for(
        fake_probes(), settings=Settings({s.name: SecretStr(FAKE) for s in KEY_SPECS})
    )
    assert exit_code(checks) == 0
    assert all(c.status == "ok" for c in checks)


def test_reports_every_key_set_or_missing_without_values():
    checks = checks_for(fake_probes(), Settings({"ANTHROPIC_API_KEY": SecretStr(FAKE)}))
    keys = [c for c in checks if c.group == "keys"]
    assert [c.name for c in keys] == [s.name for s in KEY_SPECS]
    assert find(keys, "ANTHROPIC_API_KEY").detail == "set"
    assert find(keys, "ELEVENLABS_API_KEY").detail == "missing (needed for G0.4)"
    assert all(c.detail.startswith("missing") for c in keys[1:])
    output = render(checks)
    assert all(s.name in output for s in KEY_SPECS)
    assert FAKE not in output


def test_missing_keys_do_not_fail():
    checks = checks_for(fake_probes(), env_file_exists=False)
    assert exit_code(checks) == 0
    assert find(checks, ".env file").status == "warn"


def test_missing_required_tool_fails():
    checks = checks_for(fake_probes(missing={"ffmpeg"}))
    assert find(checks, "ffmpeg").status == "fail"
    assert exit_code(checks) == 1


def test_missing_optional_tool_warns():
    checks = checks_for(fake_probes(missing={"yt-dlp"}))
    assert find(checks, "yt-dlp").status == "warn"
    assert exit_code(checks) == 0


def test_postiz_is_not_a_path_tool():
    """The Postiz CLI runs pinned through npx (ADR 0010), like HyperFrames."""
    names = [c.name for c in checks_for(fake_probes(missing={"postiz"}))]
    assert "postiz" not in names


def test_old_node_fails():
    checks = checks_for(fake_probes(node=(0, "v20.11.0")))
    node = find(checks, "node")
    assert node.status == "fail"
    assert "22" in node.detail


@pytest.mark.parametrize(("free_gb", "status"), [(39, "fail"), (50, "warn"), (100, "ok")])
def test_disk_thresholds(free_gb, status):
    checks = checks_for(fake_probes(free_gb=free_gb))
    assert find(checks, "free space").status == status


def test_failing_probe_is_reported_not_raised():
    probes = fake_probes(nvidia_smi=(9, "NVIDIA-SMI has failed"), ffmpeg=(1, ""))
    checks = checks_for(probes)
    assert find(checks, "nvidia-smi").status == "warn"
    assert find(checks, "ffmpeg").status == "fail"
    assert exit_code(checks) == 1


def test_missing_gpu_warns():
    checks = checks_for(fake_probes(missing={"nvidia-smi"}, ffmpeg_encoders=(0, "libx264")))
    assert find(checks, "nvidia-smi").status == "warn"
    assert find(checks, "nvenc").status == "warn"
    assert exit_code(checks) == 0


def test_output_is_ascii():
    probes = fake_probes(git=(0, "git version 2.53 \u2014 caf\u00e9"))
    output = render(checks_for(probes))
    assert output.isascii()


def test_doctor_config_comes_from_engine_yaml(repo_root):
    from engine.config import load_config

    engine = load_config(repo_root / "config").engine
    assert DoctorConfig.from_engine(engine) == DoctorConfig(warn_free_gb=60, stop_free_gb=40)
    assert DoctorConfig(warn_free_gb=60, stop_free_gb=40) == NFR5_DISK


def test_valid_config_is_ok():
    check = find(checks_for(fake_probes()), "config/")
    assert (check.group, check.status, check.detail) == ("config", "ok", "valid")


def test_config_problems_fail():
    problems = ["sources.yaml: sources.0.rights_proof: Field required", "engine.yaml: disk: x"]
    checks = run_checks(
        Settings(), fake_probes(), CONFIG, True, root=ROOT, config_problems=problems
    )
    check = find(checks, "config/")
    assert check.status == "fail"
    assert check.detail == "2 problems; first: sources.yaml: sources.0.rights_proof: Field required"
    assert exit_code(checks) == 1
