"""What every engine tool runs against: the DB, the config, the run, and the providers the
action tools call (SRD §5.4). The dry run swaps every provider for a fake (NFR-8)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from engine.config import CONFIG_DIR, Config
from engine.library.transcribe import Transcriber
from engine.produce.avatar import AvatarProvider
from engine.produce.overlay import OverlayRenderer
from engine.produce.process import Runner, run_command
from engine.produce.render_ffmpeg import RenderSettings
from engine.produce.voice import Balance, VoiceProvider
from engine.publish.postiz import Publisher
from engine.settings import REPO_ROOT


class ToolError(Exception):
    """A tool refused its input; the message goes back to the agent."""


@dataclass
class Providers:
    """The outside world, as the action tools see it."""

    voice: VoiceProvider
    avatar: AvatarProvider
    publisher: Publisher
    transcriber: Callable[[str], Transcriber]  # given the spoken text; fakes align to it
    overlay: OverlayRenderer
    render_settings: RenderSettings = field(default_factory=RenderSettings)
    run: Runner = run_command  # ffmpeg and ffprobe


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    config: Config
    config_dir: Path = CONFIG_DIR
    run_id: str = "dry-run"
    today: date = field(default_factory=date.today)
    voice_balance: Balance | None = None  # read once per run, for H3
    renders: dict[str, int] = field(default_factory=dict)  # renders this run, per identity
    providers: Providers | None = None  # action tools refuse without them
    media_root: Path = REPO_ROOT / "media"
    now: Callable[[], datetime] = _utcnow

    @property
    def month(self) -> str:
        return self.today.strftime("%Y-%m")

    @property
    def work_dir(self) -> Path:
        return self.media_root / "work"

    @property
    def renders_dir(self) -> Path:
        return self.media_root / "renders"


def require_providers(ctx: ToolContext) -> Providers:
    if ctx.providers is None:
        raise ToolError("this run has no providers; action tools need a live or dry run")
    return ctx.providers


def require_identity(ctx: ToolContext, identity_id: str):
    if identity_id not in ctx.config.identities:
        raise ToolError(f"unknown identity '{identity_id}'")
    return ctx.config.identities[identity_id]


def require_alive(ctx: ToolContext, identity_id: str) -> None:
    """Fail closed: a killed or unknown identity takes no new work (GR-7)."""
    row = ctx.conn.execute(
        "SELECT kill_flag FROM identities WHERE id = ?", (identity_id,)
    ).fetchone()
    if row is None:
        raise ToolError(f"identity '{identity_id}' is not in the database")
    if row["kill_flag"]:
        raise ToolError(f"{identity_id} has its kill flag set; no new work until `ce resume`")


def require_brief(ctx: ToolContext, brief_id: int) -> sqlite3.Row:
    row = ctx.conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    if row is None:
        raise ToolError(f"no brief {brief_id}")
    require_alive(ctx, row["identity_id"])
    return row
