"""`ce` CLI wiring (SRD §5.2). Probes are faked, so this never depends on the machine."""

import pytest
from typer.testing import CliRunner

from engine import doctor
from engine.cli import app

runner = CliRunner()


def fake_real(missing=()):
    def which(name):
        return None if name in missing else f"C:/bin/{name}.exe"

    def run(argv):
        if "-encoders" in argv:
            return 0, "h264_nvenc"
        return 0, "v24.14.0" if argv[0].endswith("node.exe") else "tool 1.0"

    return lambda: doctor.Probes(which=which, run=run, disk_free_bytes=lambda p: 200 * doctor.GB)


@pytest.fixture
def fake_machine(monkeypatch):
    monkeypatch.setattr(doctor.Probes, "real", staticmethod(fake_real()))


def test_help_lists_doctor():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_doctor_runs_and_exits_zero_on_fakes(fake_machine):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "checks:" in result.output


def test_doctor_exits_one_when_required_tool_missing(monkeypatch):
    monkeypatch.setattr(doctor.Probes, "real", staticmethod(fake_real(missing={"ffprobe"})))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[FAIL] tools  ffprobe" in result.output


def test_global_options_accepted(fake_machine):
    result = runner.invoke(app, ["--dry-run", "--identity", "selfhelp", "doctor"])
    assert result.exit_code == 0, result.output


def test_doctor_reports_broken_config(fake_machine, monkeypatch):
    from engine import cli
    from engine.config import ConfigError

    def broken():
        raise ConfigError(["sources.yaml: sources.0.rights_proof: Field required"])

    monkeypatch.setattr(cli, "load_config", broken)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "sources.yaml: sources.0.rights_proof: Field required" in result.output


def test_daily_without_dry_run_points_at_p09():
    result = runner.invoke(app, ["daily"])
    assert result.exit_code == 2 and "P0.9" in result.output


def fake_dry_run(tmp_path, seen, failed=()):
    from engine.agents.dryrun import DryRunResult, Produced
    from engine.agents.schemas import BudgetUsed, FailedPost, IdentityResult, RunReport

    def run(config, identity=None, **kwargs):
        seen["identity"] = identity
        report = RunReport(
            run_id="dry-x",
            identity_results=[
                IdentityResult(
                    identity="selfhelp",
                    posts_failed=[FailedPost(post="selfhelp render", reason=r) for r in failed],
                )
            ],
            budget_used=BudgetUsed(elevenlabs_chars=420),
            warnings=["dry run: every vendor is faked"],
            needs_abbas=[],
        )
        made = Produced("selfhelp", 1, 2, 3, 4, str(tmp_path / "selfhelp-1-3.mp4"))
        return DryRunResult("dry-x", tmp_path, report, [made])

    return run


@pytest.mark.parametrize(
    "argv",
    [
        ["daily", "--dry-run", "--identity", "selfhelp"],
        ["--dry-run", "--identity", "selfhelp", "daily"],
    ],
)
def test_daily_dry_run_reports_what_it_made(monkeypatch, tmp_path, argv):
    from engine import cli

    seen = {}
    monkeypatch.setattr(cli, "daily_dry_run", fake_dry_run(tmp_path, seen))
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert seen["identity"] == "selfhelp"
    assert "brief 1, script 2, edit plan 3, render 4" in result.output
    assert "selfhelp-1-3.mp4" in result.output and "run_report.json" in result.output


def test_daily_dry_run_exits_non_zero_when_a_post_failed(monkeypatch, tmp_path):
    from engine import cli

    monkeypatch.setattr(cli, "daily_dry_run", fake_dry_run(tmp_path, {}, failed=["boom"]))
    result = runner.invoke(app, ["daily", "--dry-run"])
    assert result.exit_code == 1 and "boom" in result.output
