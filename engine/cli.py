"""`ce`: the content engine's command line (SRD §5.2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from engine import doctor as doctor_checks
from engine.agents.dryrun import daily_dry_run
from engine.agents.state import sync_identities
from engine.config import CONFIG_DIR, ConfigError, load_config
from engine.db import open_db
from engine.learn.metrics import due_snapshots, pull_metrics
from engine.learn.reconcile import ReconcileError, open_posts
from engine.learn.reconcile import reconcile as reconcile_posts
from engine.learn.reconcile import reject as reject_post
from engine.library.embeddings import BgeSmall, EmbeddingModelMissing, model_files
from engine.library.film_index import FilmIndexer, IndexDeps, next_deadline
from engine.library.ingest import IngestDeps, Ingester, ingestible
from engine.library.music import MusicImporter, music_sources
from engine.library.people import mark, review_list
from engine.library.transcribe import FasterWhisperTranscriber
from engine.library.volumes import mounted_volumes
from engine.ops.budget import disk_free
from engine.ops.digest import build_digest, expire_waiting, write_digest
from engine.ops.gpu_lock import GpuBusy, GpuLock
from engine.ops.killswitch import KillSwitchError
from engine.ops.killswitch import kill as kill_identity
from engine.ops.killswitch import resume as resume_identity
from engine.ops.killswitch import warn as warn_identity
from engine.ops.notify import Notifier, ToastNotifier
from engine.publish.postiz import PostizCLI, PostizError
from engine.publish.youtube import OEmbedVisibility, Visibility
from engine.settings import DEFAULT_ENV_FILE, REPO_ROOT, load_settings
from engine.style.ocr import RapidOcrDetector
from engine.style.profile import (
    MeasureDeps,
    analyze_refs,
    measure_reference,
    reference_files,
)
from engine.style.propose import (
    ScriptedProposer,
    approve_look,
    latest_proposal,
    propose_looks,
)

app = typer.Typer(no_args_is_help=True, help="Content engine: plan, produce and draft posts.")
refs_app = typer.Typer(no_args_is_help=True, help="Style references (SRD §9.1).")
app.add_typer(refs_app, name="refs")


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
    followed = result.follow_up
    if followed and followed.changes:
        typer.echo("  Fake Abbas published the drafts; reconcile and metrics ran:")
        for change in followed.changes:
            typer.echo(f"    post {change.post_id}: {change.before} -> {change.after}")
        for post_id, slot in followed.snapshots:
            typer.echo(f"    post {post_id}: {slot} metrics snapshot")
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


@refs_app.command("analyze")
def refs_analyze(
    identity: str = typer.Argument(..., help="The identity whose inbox to measure."),
    keep: bool = typer.Option(False, "--keep", help="Keep the files (C10 deletes them)."),
) -> None:
    """Measure the reference shorts in references/inbox/<identity>/, store the style
    profile, then delete the references (SRD §9.1, C10)."""
    config = load_config()
    if identity not in config.identities:
        typer.echo(f"unknown identity '{identity}' (known: {', '.join(config.identities)})")
        raise typer.Exit(2)
    conn = open_db(config.engine.paths.resolve("db", REPO_ROOT))
    sync_identities(conn, config)
    folder = REPO_ROOT / "references" / "inbox" / identity
    found = reference_files(folder) if folder.is_dir() else []
    if found:
        fate = "kept (--keep)" if keep else "deleted once the profile is stored (C10)"
        typer.echo(f"Measuring {len(found)} references in {folder}; they are {fate}.")
    deps = MeasureDeps(detector=RapidOcrDetector(), transcriber=FasterWhisperTranscriber())
    lock = GpuLock(config.engine.paths.resolve("gpu_lock", REPO_ROOT), job="refs analyze")
    try:
        with lock:  # Whisper on the GPU (NFR-6)
            profile = analyze_refs(
                conn, identity, folder, lambda video: measure_reference(video, deps), keep=keep
            )
    except (ValueError, GpuBusy) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    count = profile["ref_count"]
    typer.echo(f"{identity}: profile {profile['profile_id']} from {count} references")
    for name, value in profile["metrics"].items():
        typer.echo(f"  {name}: {value}")
    typer.echo(f"  palette: {' '.join(profile['palette'])}")
    if keep:
        typer.echo(f"Kept the {count} reference files (--keep).")
    else:
        typer.echo(f"Deleted the {count} reference files (C10): only the numbers stay.")


models_app = typer.Typer(no_args_is_help=True, help="Local models (C12).")
app.add_typer(models_app, name="models")


@models_app.command("fetch")
def models_fetch(
    names: Annotated[
        list[str] | None, typer.Argument(help="Models to fetch (default: every pinned model).")
    ] = None,
) -> None:
    """Download pinned models into the Hugging Face cache (each one only with Abbas's OK)."""
    from engine.library.models import PINNED

    unknown = [n for n in names or [] if n not in PINNED]
    if unknown:
        typer.echo(f"Unknown model {', '.join(unknown)}; the pinned ones: {', '.join(PINNED)}")
        raise typer.Exit(1)
    for key in names or list(PINNED):
        model = PINNED[key]
        paths = model.fetch()
        typer.echo(f"{model.repo} at {model.revision[:7]}: {len(paths)} files")
        for path in paths:
            typer.echo(f"  {path}")


look_app = typer.Typer(no_args_is_help=True, help="Looks (SRD §9.2).")
app.add_typer(look_app, name="look")


def _looks_root(config) -> Path:
    return config.engine.paths.resolve("media", REPO_ROOT) / "looks"


@look_app.command("propose")
def look_propose(
    identity: str = typer.Argument(..., help="The identity to propose looks for."),
    count: int = typer.Option(3, "--count", min=1, max=3, help="How many looks (1-3)."),
) -> None:
    """Three looks from the identity's style profile, each rendered as the same sample post
    with every vendor faked (P0.8.1: scripted proposer; Claude arrives in P0.8.2)."""
    config = load_config()
    if identity not in config.identities:
        typer.echo(f"unknown identity '{identity}' (known: {', '.join(config.identities)})")
        raise typer.Exit(2)
    conn = open_db(config.engine.paths.resolve("db", REPO_ROOT))
    sync_identities(conn, config)
    try:
        proposal = propose_looks(
            config, conn, identity, ScriptedProposer(), _looks_root(config), count=count
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Proposal: {proposal.folder}")
    for look, sample in zip(proposal.looks, proposal.samples, strict=True):
        typer.echo(f"  {look.version}: {look.name}. {look.rationale}")
        typer.echo(f"     sample: {sample}")
    if proposal.contact:
        typer.echo(f"  side by side: {proposal.contact}")
    typer.echo(f"Approve one with: ce look approve {identity} <n>")


@look_app.command("approve")
def look_approve(
    identity: str = typer.Argument(..., help="The identity whose look to approve."),
    number: int = typer.Argument(..., help="The look's number in the proposal."),
    proposal: Annotated[
        Path | None, typer.Option("--proposal", help="Default: the newest.")
    ] = None,
) -> None:
    """Save look <number> as the identity's next look version and make it active (§9.2)."""
    config = load_config()
    folder = proposal or latest_proposal(_looks_root(config), identity)
    if folder is None or not folder.is_dir():
        typer.echo(f"no look proposal for {identity}: run `ce look propose {identity}` first")
        raise typer.Exit(1)
    conn = open_db(config.engine.paths.resolve("db", REPO_ROOT))
    sync_identities(conn, config)
    try:
        path = approve_look(CONFIG_DIR, conn, identity, folder, number)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Saved {path} and made it {identity}'s active look.")


def make_notifier() -> Notifier:
    return ToastNotifier()


@app.command()
def digest(
    day: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD; default today.")] = None,
    notify: Annotated[
        bool, typer.Option("--notify/--no-notify", help="Toast if anything waits.")
    ] = True,
) -> None:
    """Write reports/YYYY-MM-DD.md from the database and, when something waits on Abbas,
    show a Windows notification that opens it (SRD §13). Safe to rerun: the morning repeat."""
    config = load_config()
    paths = config.engine.paths
    conn = open_db(paths.resolve("db", REPO_ROOT))
    sync_identities(conn, config)
    for item in expire_waiting(conn, datetime.now(UTC)):
        typer.echo(f"Dropped: {item.text}")
    built = build_digest(
        conn,
        config,
        date.fromisoformat(day) if day else date.today(),
        work_dir=paths.resolve("work", REPO_ROOT),
        inbox_dir=paths.resolve("avatar_inbox", REPO_ROOT),
    )
    path = write_digest(built, paths.resolve("reports", REPO_ROOT))
    typer.echo(f"Digest: {path}")
    typer.echo(f"Waiting on you: {built.summary()}")
    if notify and built.needs_abbas:
        make_notifier().notify("Content engine", built.summary(), path)


# The kill switch (GR-7, §17; ADR 0019) -----------------------------------------------------------


def _engine_db():
    config = load_config()
    conn = open_db(config.engine.paths.resolve("db", REPO_ROOT))
    sync_identities(conn, config)
    return config, conn


@app.command()
def status(
    people: Annotated[
        bool, typer.Option("--people", help="List the clips that wait for a people mark.")
    ] = False,
) -> None:
    """What waits on Abbas. For now the people review (SRD §8.3); the rest of NFR-3 later."""
    _, conn = _engine_db()
    items = review_list(conn)
    if not people:
        noun = "clip waits" if len(items) == 1 else "clips wait"
        typer.echo(f"People review: {len(items)} {noun} for a people mark "
                   "(`uv run ce status --people`).")  # fmt: skip
        return
    if not items:
        typer.echo("No clips wait for a people mark.")
        return
    typer.echo("These clips show faces. Mark each once: yes (you hold consent) or exclude.")
    for item in items:
        seen = f"faces in {item.faces} frames"
        if item.first_face_s is not None:
            seen += f" from {item.first_face_s:.1f} s"
        typer.echo(f"#{item.asset_id}  {item.source_id}  {item.path}")
        typer.echo(f"    {seen}; look at {item.keyframe or 'the clip'}")
        typer.echo(f"    uv run ce people mark {item.asset_id} yes   |   "
                   f"uv run ce people mark {item.asset_id} exclude")  # fmt: skip


people_app = typer.Typer(no_args_is_help=True, help="The people rule (SRD §8.3).")
app.add_typer(people_app, name="people")


@people_app.command("mark")
def people_mark(
    asset_id: Annotated[int, typer.Argument(help="The asset id from `ce status --people`.")],
    decision: Annotated[str, typer.Argument(help="yes (consent held) or exclude.")],
    note: Annotated[str, typer.Option(help="Why, for the record.")] = "",
) -> None:
    """Mark one clip: yes puts it back in B-roll search, exclude keeps it out for good."""
    _, conn = _engine_db()
    try:
        mark(conn, asset_id, decision, note)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    path = conn.execute("SELECT path FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
    typer.echo(f"#{asset_id} {Path(path).name}: people_release {decision}")


def _paused(identity: str, posts) -> None:
    typer.echo(f"{identity} paused: no new work, and at L0 until `ce resume {identity}`.")
    if posts:
        typer.echo("These posts can still go out; pull or unschedule them in Postiz:")
        for post in posts:
            when = f" for {post.scheduled_at}" if post.scheduled_at else ""
            postiz = f", Postiz {post.postiz_id}" if post.postiz_id else ""
            typer.echo(f"  post {post.post_id} on {post.platform}: {post.state}{postiz}{when}")


@app.command()
def kill(
    identity: Annotated[str | None, typer.Argument(help="The identity to pause.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Pause every identity.")] = False,
    reason: Annotated[
        str, typer.Option(help="Why; shown in the digest.")
    ] = "manual stop (ce kill)",
) -> None:
    """Pause an identity, or every identity: kill flag set, dropped to L0 (GR-7)."""
    config, conn = _engine_db()
    if all_ == bool(identity):
        typer.echo("Name one identity or pass --all.")
        raise typer.Exit(1)
    targets = sorted(config.identities) if all_ else [identity]
    try:
        for target in targets:
            _paused(target, kill_identity(conn, target, reason))
    except KillSwitchError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


@app.command()
def warn(
    identity: Annotated[str, typer.Argument(help="The identity the warning is about.")],
    text: Annotated[str, typer.Argument(help="What arrived: platform, kind, which post.")],
) -> None:
    """Record a platform warning, strike or demonetisation notice; pauses the identity (GR-7)."""
    _, conn = _engine_db()
    try:
        posts = warn_identity(conn, identity, text)
    except KillSwitchError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Recorded the platform warning for {identity}.")
    _paused(identity, posts)


@app.command()
def resume(
    identity: Annotated[str, typer.Argument(help="The paused identity.")],
) -> None:
    """Clear the kill flag and restore the identity's autonomy level from its YAML (§17)."""
    config, conn = _engine_db()
    try:
        level = resume_identity(conn, config, identity)
    except KillSwitchError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"{identity} resumed at L{level}.")


# Reconciliation and metrics (SRD §10, FR-LRN-1; ADR 0021) ----------------------------------

NO_POSTIZ = (
    "POSTIZ_API_KEY is missing (Gate 0b, G0.7): sign up at https://postiz.com, connect the "
    "YouTube channel, then Settings > Public API > create key, and add it to .env as "
    "POSTIZ_API_KEY. `--dry-run` shows what a live run would check."
)


def make_postiz(config) -> PostizCLI | None:
    """The real Postiz CLI, or None while the key is missing."""
    key = load_settings(DEFAULT_ENV_FILE).get("POSTIZ_API_KEY")
    if not key:
        return None
    return PostizCLI(
        api_key=key, work_dir=config.engine.paths.resolve("work", REPO_ROOT) / "postiz"
    )


def make_visibility() -> Visibility:
    return OEmbedVisibility()


def _live_postiz(config) -> PostizCLI:
    postiz = make_postiz(config)
    if postiz is None:
        typer.echo(NO_POSTIZ)
        raise typer.Exit(2)
    return postiz


@app.command()
def reconcile(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Call and write nothing."),
) -> None:
    """Follow drafted posts through Postiz and YouTube Studio to scheduled, published,
    rejected or failed (SRD §10). Safe to rerun."""
    config, conn = _engine_db()
    now = datetime.now(UTC)
    if dry_run or ctx.obj["dry_run"]:
        posts = open_posts(conn, now)
        typer.echo(f"Dry run: a live run would check {len(posts)} post(s); nothing called "
                   "or written.")  # fmt: skip
        for post in posts:
            typer.echo(f"  post {post.post_id} on {post.platform}: {post.state}, "
                       f"Postiz {post.postiz_id}")  # fmt: skip
        return
    postiz = _live_postiz(config)
    try:
        report = reconcile_posts(conn, postiz, make_visibility(), now)
    except PostizError as exc:
        typer.echo(f"Reconcile stopped, nothing changed: {exc}")
        raise typer.Exit(1) from exc
    for change in report.changes:
        typer.echo(f"  post {change.post_id}: {change.before} -> {change.after}")
    if not report.changes:
        typer.echo("Nothing changed.")
    for warning in report.warnings:
        typer.echo(f"  warning: {warning}")


@app.command()
def metrics(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Call and write nothing."),
) -> None:
    """Take the 24 h, 72 h and 7 d snapshots that have come due (FR-LRN-1). Safe to rerun."""
    config, conn = _engine_db()
    now = datetime.now(UTC)
    if dry_run or ctx.obj["dry_run"]:
        due = due_snapshots(conn, now)
        typer.echo(f"Dry run: {len(due)} snapshot(s) due; nothing called or written.")
        for snap in due:
            typer.echo(f"  post {snap.post_id}: {snap.slot} (Postiz {snap.postiz_id})")
        return
    report = pull_metrics(conn, _live_postiz(config), now)
    for post_id, slot in report.taken:
        typer.echo(f"  post {post_id}: {slot} snapshot")
    for post_id, release in report.connected:
        typer.echo(f"  post {post_id}: connected to video {release}")
    for post_id in report.unresolved:
        typer.echo(f"  post {post_id}: Postiz cannot match its video; connect it in Postiz")
    for post_id in report.empty:
        typer.echo(f"  post {post_id}: no numbers yet; tried again next run")
    for post_id, error in report.failed:
        typer.echo(f"  post {post_id}: FAILED {error}")
    if not (report.taken or report.connected or report.unresolved or report.empty
            or report.failed):  # fmt: skip
        typer.echo("No snapshot due.")
    if report.failed:
        raise typer.Exit(1)  # the job log and the wrapper's exit code show it


@app.command()
def reject(
    post_id: Annotated[int, typer.Argument(help="The engine's post id (from the digest).")],
    why: Annotated[str, typer.Argument(help="Why; the weekly retro reads it.")],
) -> None:
    """Record why a draft was not used (§10). The engine deletes nothing in Postiz."""
    _, conn = _engine_db()
    try:
        before = reject_post(conn, post_id, why, datetime.now(UTC))
    except ReconcileError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Post {post_id} rejected (was {before}).")
    if before in ("drafted", "scheduled"):
        typer.echo("Delete it in Postiz too if it is still there; the engine never deletes.")
    elif before == "uploaded_private":
        typer.echo("The private video stays on YouTube until you delete it in Studio.")


# The film drive index (SRD §8.2; ADR 0020) --------------------------------------------------


def clip_if_cached():
    """The real CLIP when its model is cached, else None (the images step waits)."""
    from engine.library.clip import ClipOnnx
    from engine.library.models import PINNED

    return ClipOnnx() if PINNED["clip"].cached() else None


def missing_models(keys: list[str]) -> list[str]:
    from engine.library.models import PINNED

    return [key for key in keys if not PINNED[key].cached()]


def real_ingest_models():
    """The real text model, CLIP and face detector (all from the local cache)."""
    from engine.library.clip import ClipOnnx
    from engine.library.faces import YuNet

    return BgeSmall(), ClipOnnx(), YuNet()


def _import_music(conn, config, source: str | None, retry_failed: bool) -> None:
    """FR-ING-6: the music pools; no model needed (ADR 0023)."""
    report = MusicImporter(conn, config).run(source_id=source, retry_failed=retry_failed)
    for missing_pool in report.missing:
        typer.echo(f"Music folder not found: {missing_pool} (G0.9b: download 10-20 YouTube "
                   "Audio Library tracks into it)")  # fmt: skip
    for track in report.tracks:
        name = Path(track.path).name
        if track.error:
            typer.echo(f"{name}: FAILED {track.error}")
        elif track.skipped:
            typer.echo(f"{name}: skipped, {track.skipped}")
        else:
            typer.echo(f"{name}: {'imported' if track.imported else 'already imported'}")
    imported = sum(1 for t in report.tracks if t.imported)
    failed = sum(1 for t in report.tracks if t.error)
    typer.echo(f"{imported} track{'' if imported == 1 else 's'} imported, {failed} failed, "
               f"{len(report.tracks) - imported - failed} unchanged or skipped")  # fmt: skip


@app.command()
def ingest(
    source: Annotated[
        str | None, typer.Option(help="Only this source id (default: every folder source).")
    ] = None,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Try files that failed before again.")
    ] = False,
) -> None:
    """Import the music pools, then scan the footage folders and fetched stock in the sources
    config: hash, dedupe and index in place."""
    config = load_config()
    pools, footage = music_sources(config), ingestible(config)
    if source is not None:
        if any(s.id == source for s in pools):
            pools, footage = [s for s in pools if s.id == source], []
        elif any(s.id == source for s in footage):
            pools = []
        else:
            names = ", ".join(s.id for s in [*footage, *pools]) or "none"
            typer.echo(f"'{source}' is not an ingestible source (they are: {names})")
            raise typer.Exit(1)
    if not pools and not footage:
        typer.echo(
            "No footage folder in the sources config yet. Abbas names the folders in a build "
            "session and Claude adds them to the sources config by hand (G0.9c, FR-SRC-3)."
        )
        return
    paths = config.engine.paths
    conn = open_db(paths.resolve("db", REPO_ROOT))
    if pools:
        _import_music(conn, config, source if not footage else None, retry_failed)
    if not footage:
        return
    missing = missing_models(["bge", "clip", "yunet"])
    if missing:
        typer.echo(f"Models not cached: run `uv run ce models fetch {' '.join(missing)}`")
        raise typer.Exit(1)
    text, clip, faces = real_ingest_models()
    deps = IngestDeps(
        embedder=text,
        clip=clip,
        faces=faces,
        transcriber=FasterWhisperTranscriber,
        gpu_lock=paths.resolve("gpu_lock", REPO_ROOT),
        disk_free=disk_free,
    )
    try:
        report = Ingester(conn, config, deps, paths.resolve("media", REPO_ROOT)).run(
            source_id=source, retry_failed=retry_failed
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    for missing_source in report.missing:
        typer.echo(f"Folder not found: {missing_source}")
    for asset in report.assets:
        name = Path(asset.path).name
        if asset.error:
            typer.echo(f"{name}: FAILED {asset.error}")
        elif asset.skipped:
            typer.echo(f"{name}: skipped, {asset.skipped}")
        else:
            typer.echo(f"{name}: {', '.join(asset.done) or 'already indexed'}")
    worked = sum(1 for a in report.assets if a.done and not a.error)
    failed = sum(1 for a in report.assets if a.error)
    unchanged = sum(1 for a in report.assets if not a.done and not a.error and not a.skipped)
    typer.echo(f"{worked} new or changed, {failed} failed, {unchanged} unchanged")
    if report.stopped:
        typer.echo(f"Stopped: {report.stopped}")
    people = conn.execute(
        "SELECT count(*) FROM asset_index i JOIN assets a ON a.id = i.asset_id "
        "WHERE i.people_at IS NOT NULL AND a.people_release = 'unknown'"
    ).fetchone()[0]
    if people:
        typer.echo(f"{people} clips show faces and wait for your mark: `uv run ce status --people`")


# Stock footage (FR-ING-5; ADR 0023) ---------------------------------------------------------

stock_app = typer.Typer(no_args_is_help=True, help="Stock footage (FR-ING-5).")
app.add_typer(stock_app, name="stock")

NO_PEXELS = (
    "stock requests wait for PEXELS_API_KEY (Gate 0b, G0.9a): get a free key at "
    "https://www.pexels.com/api/ and add it to .env as PEXELS_API_KEY. `--dry-run` lists them."
)


def _media_root(config) -> Path:
    return config.engine.paths.resolve("media", REPO_ROOT)


def make_stock():
    """Pexels with the key from the env file, or None without one (G0.9a)."""
    from engine.library.stock import PexelsStock

    key = load_settings(DEFAULT_ENV_FILE).get("PEXELS_API_KEY")
    return PexelsStock(api_key=key) if key else None


@stock_app.command("fetch")
def stock_fetch(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List the queue and the day's cap; call nothing.")
    ] = False,
) -> None:
    """Fetch stock clips for the beats scriptwriters queued, within the daily cap."""
    from engine.library.stock_fetch import fetch_requested, pending_requests, stock_source

    config, conn = _engine_db()
    if stock_source(config) is None:
        typer.echo(f"No stock API source '{config.engine.stock.source}' in the sources config; "
                   "stock is off.")  # fmt: skip
        return
    waiting = pending_requests(conn)
    if not waiting:
        typer.echo("No stock requests waiting.")
        return
    today = date.today()
    if dry_run:
        report = fetch_requested(conn, config, None, _media_root(config), today, dry_run=True)
        typer.echo(f"{len(report.requests)} stock requests waiting; "
                   f"{report.left_today} of {report.cap} clips left today:")  # fmt: skip
        for request in report.requests:
            typer.echo(f"  {request.id}: {request.query}")
        return
    provider = make_stock()
    if provider is None:
        typer.echo(f"{len(waiting)} {NO_PEXELS}")
        raise typer.Exit(2)
    report = fetch_requested(conn, config, provider, _media_root(config), today,
                             disk_free=disk_free)  # fmt: skip
    for request in report.requests:
        typer.echo(f"{request.query}: {request.status}"
                   + (f" ({request.detail})" if request.detail else ""))  # fmt: skip
    count = len(report.fetched)
    typer.echo(f"{count} clip{'' if count == 1 else 's'} fetched; "
               f"{report.left_today} left today. `ce ingest` indexes them.")  # fmt: skip
    if report.stopped:
        typer.echo(f"Stopped: {report.stopped}")


@app.command("index-films")
def index_films(
    until: Annotated[
        str | None, typer.Option(help="HH:MM: start nothing after this time (the 06:00 end).")
    ] = None,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Try films that failed before again.")
    ] = False,
    volumes: Annotated[
        bool, typer.Option("--volumes", help="List connected volumes (serial, label) and stop.")
    ] = False,
) -> None:
    """Index every film on a connected film drive, in place and read-only; resumable."""
    found = mounted_volumes()
    if volumes:
        for volume in found:
            typer.echo(f"{volume.root}  serial {volume.serial}  label {volume.label or '(none)'}")
        return
    config = load_config()
    drives = [s for s in config.sources if s.kind == "drive" and s.rights_type == "film_reference"]
    if not drives:
        typer.echo(
            "No film drive in the sources config. Plug it in, run `ce index-films --volumes`, "
            "and add it as a drive source with its serial and label (Abbas, SRD 8.2)."
        )
        return
    deadline = None
    if until:
        try:
            deadline = next_deadline(until, datetime.now())
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        if deadline is None:
            typer.echo(f"Outside the indexing window ending {until}; nothing to do.")
            return
    try:
        model_files()
    except EmbeddingModelMissing as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    paths = config.engine.paths
    conn = open_db(paths.resolve("db", REPO_ROOT))
    deps = IndexDeps(
        embedder=BgeSmall(),
        transcriber=FasterWhisperTranscriber,
        clip=clip_if_cached(),
        gpu_lock=paths.resolve("gpu_lock", REPO_ROOT),
        disk_free=disk_free,
    )
    if deps.clip is None:
        typer.echo("Keyframe vectors wait for the CLIP model (`uv run ce models fetch clip`).")
    report = FilmIndexer(conn, config, deps, paths.resolve("media", REPO_ROOT)).run(
        found, until=deadline, retry_failed=retry_failed
    )
    for source in report.missing_drives:
        typer.echo(f"Not connected: {source}")
    for film in report.films:
        if film.error:
            typer.echo(f"{film.title}: FAILED {film.error}")
        elif film.skipped:
            typer.echo(f"{film.title}: skipped, {film.skipped}")
        else:
            typer.echo(f"{film.title}: {', '.join(film.done) or 'already indexed'}")
    if report.stopped:
        typer.echo(f"Stopped: {report.stopped}")
