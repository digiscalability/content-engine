"""`ce`: the content engine's command line (SRD §5.2)."""

from __future__ import annotations

import typer

from engine import doctor as doctor_checks
from engine.agents.dryrun import daily_dry_run
from engine.config import ConfigError, load_config
from engine.settings import DEFAULT_ENV_FILE, load_settings

app = typer.Typer(no_args_is_help=True, help="Content engine: plan, produce and draft posts.")


@app.callback()
def main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Fake every external call (NFR-8)."),
    identity: str | None = typer.Option(None, "--identity", help="Limit the run to one identity."),
) -> None:
    """Options shared by every command."""
    ctx.obj = {"dry_run": dry_run, "identity": identity}


@app.command()
def doctor() -> None:
    """Check tools on PATH, GPU, disk, config, and which .env keys are set or missing."""
    try:
        disk = doctor_checks.DoctorConfig.from_engine(load_config().engine)
        problems: list[str] = []
    except ConfigError as exc:
        disk, problems = doctor_checks.NFR5_DISK, exc.problems
    checks = doctor_checks.run_checks(
        settings=load_settings(DEFAULT_ENV_FILE),
        probes=doctor_checks.Probes.real(),
        config=disk,
        env_file_exists=DEFAULT_ENV_FILE.is_file(),
        config_problems=problems,
    )
    typer.echo(doctor_checks.render(checks))
    if problems:
        typer.echo("\nConfig problems:")
        for problem in problems:
            typer.echo(f"  {problem}")
    raise typer.Exit(doctor_checks.exit_code(checks))


@app.command()
def daily(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Fake every external call (NFR-8)."),
    identity: str | None = typer.Option(None, "--identity", help="Limit the run to one identity."),
) -> None:
    """One post per due identity, from topic to a Postiz draft (SRD §5.2)."""
    dry_run = dry_run or ctx.obj["dry_run"]
    identity = identity or ctx.obj["identity"]
    if not dry_run:
        typer.echo(
            "Live daily runs arrive with slice P0.9 (they need Gate 0b, G0.3-G0.7). "
            "Try `ce daily --dry-run`."
        )
        raise typer.Exit(2)
    result = daily_dry_run(load_config(), identity=identity)
    typer.echo(f"Dry run {result.run_id}")
    for made in result.produced:
        ids = (
            f"brief {made.brief_id}, script {made.script_id}, "
            f"edit plan {made.edit_plan_id}, render {made.render_id}"
        )
        typer.echo(f"  {made.identity}: {ids}")
        if made.mp4:
            typer.echo(f"    MP4: {made.mp4}")
        if made.report:
            typer.echo(f"    report: {made.report}")
        for kind, path in made.exports.items():
            typer.echo(f"    {kind}: {path}")
        if made.drafts:
            typer.echo(f"    Postiz drafts (fake): {', '.join(made.drafts)}")
    failed = [f for r in result.report.identity_results for f in r.posts_failed]
    for failure in failed:
        typer.echo(f"  failed: {failure.post}: {failure.reason}")
    for outcome in result.report.identity_results:
        for hold in outcome.qa_holds:
            typer.echo(f"  QA hold: {hold}")
    for need in result.report.needs_abbas:
        typer.echo(f"  needs Abbas: {need.item}")
    for warning in result.report.warnings:
        typer.echo(f"  warning: {warning}")
    typer.echo(f"Report: {result.root / 'run_report.json'}")
    raise typer.Exit(1 if failed else 0)
