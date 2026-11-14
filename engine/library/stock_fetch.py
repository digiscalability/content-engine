"""Stock fetched only for the beats a scriptwriter asked for, capped per day (SRD FR-ING-5,
FR-SRC-2, NFR-5, GR-7). ADR 0023.

1. `search_library` queues a request (`queue_request`) when a beat query of a brief finds no
   eligible clip, or only clips whose picture scores under `stock.thin_below`.
2. The nightly `ce stock fetch` works the queue (`fetch_requested`): pending requests of
   identities without the kill flag, oldest first; at most `per_request` clips a request and
   `daily_cap` a local day; only full HD portrait MP4s no longer than `max_clip_s`; a clip the
   provider already gave is never fetched again. Each clip's asset, path and licence record
   (licence, terms, credit, creator, page) are written in one transaction, so no fetched clip
   exists without its licence record. A rate limit or the disk stop line ends the run and
   leaves the rest pending; any other provider error fails that request only.
3. `ce ingest` then indexes the clips like own footage (scenes, CLIP vectors, people check).

This module never writes the sources config (FR-SRC-3) and calls a provider only from
`fetch_requested`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from engine.config import Config
from engine.config.models import SourceConfig
from engine.library.assets import file_sha256
from engine.library.ingest import is_stock_api, register_file, stock_folder, transaction
from engine.library.sources import sync_sources
from engine.library.stock import (
    StockError,
    StockProvider,
    StockRateLimited,
    StockVideo,
    pick_file,
)
from engine.ops.budget import disk_preflight

__all__ = ["FetchReport", "RequestResult", "fetch_requested", "queue_request", "stock_folder",
           "stock_source"]  # fmt: skip

SEARCH_PAGE = 15  # candidates looked at per request
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


@dataclass
class RequestResult:
    id: int
    query: str
    status: str = "pending"
    assets: list[int] = field(default_factory=list)
    detail: str = ""


@dataclass
class FetchReport:
    requests: list[RequestResult] = field(default_factory=list)
    fetched: list[int] = field(default_factory=list)
    left_today: int = 0
    cap: int = 0
    stopped: str | None = None


def stock_source(config: Config) -> SourceConfig | None:
    """The stock API source `engine.yaml` names, if the sources config has it."""
    name = config.engine.stock.source
    return next((s for s in config.sources if s.id == name and is_stock_api(s)), None)


def queue_request(conn: sqlite3.Connection, config: Config, brief_id: int, query: str,
                  fmt: str, result: dict) -> int | None:  # fmt: skip
    """Queue a stock fetch for this beat when its best match is weak; the request id."""
    query = " ".join(query.split())
    if not query or stock_source(config) is None:
        return None
    scores = [r["score"] for r in result.get("results", []) if r.get("match") == "image"]
    best = max(scores, default=None)
    if result.get("results") and (best is None or best >= config.engine.stock.thin_below):
        return None  # a strong match, or one this run cannot score
    conn.execute(
        "INSERT OR IGNORE INTO stock_requests (brief_id, query, format, best_score) "
        "VALUES (?, ?, ?, ?)",
        (brief_id, query, fmt, best),
    )
    row = conn.execute(
        "SELECT id FROM stock_requests WHERE brief_id = ? AND query = ?", (brief_id, query)
    ).fetchone()
    return int(row[0])


def pending_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Requests still waiting, oldest first; a killed identity's wait (GR-7)."""
    return conn.execute(
        "SELECT r.id, r.query FROM stock_requests r JOIN briefs b ON b.id = r.brief_id "
        "JOIN identities i ON i.id = b.identity_id "
        "WHERE r.status = 'pending' AND i.kill_flag = 0 ORDER BY r.asked_at, r.id"
    ).fetchall()


def fetched_on(conn: sqlite3.Connection, provider: str, day: date) -> int:
    return int(conn.execute(
        "SELECT count(*) FROM asset_licences WHERE provider = ? AND fetched_on = ?",
        (provider, day.isoformat()),
    ).fetchone()[0])  # fmt: skip


def _known(conn: sqlite3.Connection, provider: str, video_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM asset_licences WHERE provider = ? AND provider_id = ?",
        (provider, video_id),
    ).fetchone() is not None  # fmt: skip


def register_clip(conn: sqlite3.Connection, source: SourceConfig, path: Path, video: StockVideo,
                  provider: StockProvider, today: date) -> int:  # fmt: skip
    """The clip's asset, path, index row and licence record, in one transaction."""
    sha = file_sha256(path)
    with transaction(conn):
        asset_id = register_file(conn, source, path, sha, "video")
        conn.execute("INSERT OR IGNORE INTO asset_index (asset_id) VALUES (?)", (asset_id,))
        conn.execute(
            "INSERT INTO asset_licences (asset_id, licence_url, terms, attribution, creator, "
            "creator_url, page_url, provider, provider_id, fetched_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (asset_id, source.licence_url, source.rights_proof, provider.credit(video),
             video.creator, video.creator_url, video.page_url, provider.name, video.id,
             today.isoformat()),
        )  # fmt: skip
    return asset_id


def _finish(conn: sqlite3.Connection, result: RequestResult) -> None:
    conn.execute(
        f"UPDATE stock_requests SET status = ?, detail = ?, done_at = {NOW} WHERE id = ?",
        (result.status, result.detail[:500], result.id),
    )


def fetch_requested(conn: sqlite3.Connection, config: Config, provider: StockProvider | None,
                    media_root: Path, today: date,
                    disk_free: Callable[[Path], int] = lambda path: 1 << 50,
                    dry_run: bool = False) -> FetchReport:  # fmt: skip
    stock = config.engine.stock
    source = stock_source(config)
    report = FetchReport(cap=stock.daily_cap)
    if source is None:
        report.stopped = f"no stock API source '{stock.source}' in the sources config"
        return report
    name = provider.name if provider is not None else "pexels"
    left = max(0, stock.daily_cap - fetched_on(conn, name, today))
    report.left_today = left
    pending = [RequestResult(r["id"], r["query"]) for r in pending_requests(conn)]
    if dry_run or not pending:
        report.requests = pending
        return report
    if provider is None:
        raise ValueError("a stock provider is needed outside a dry run")
    sync_sources(conn, config)
    folder = stock_folder(media_root, source.id)
    for request in pending:
        if left == 0:
            report.stopped = f"daily cap of {stock.daily_cap} clips reached"
            break
        if reason := disk_preflight(disk_free(media_root), config.engine.disk.stop_free_gb):
            report.stopped = reason
            break
        report.requests.append(request)
        try:
            videos = provider.search(request.query, per_page=SEARCH_PAGE)
        except StockRateLimited as exc:
            report.stopped = str(exc)
            break
        except StockError as exc:
            request.status, request.detail = "failed", str(exc)
            _finish(conn, request)
            continue
        errors: list[str] = []
        for video in videos:
            if len(request.assets) >= min(stock.per_request, left):
                break
            file = pick_file(video)
            if file is None or video.duration > stock.max_clip_s or _known(conn, name, video.id):
                continue
            out = folder / f"{name}-{video.id}.mp4"
            try:
                provider.download(file, out)
            except StockRateLimited as exc:
                report.stopped = str(exc)
                break
            except StockError as exc:
                errors.append(str(exc))
                continue
            asset_id = register_clip(conn, source, out, video, provider, today)
            request.assets.append(asset_id)
            report.fetched.append(asset_id)
        left -= len(request.assets)
        if request.assets:
            request.status = "fetched"
            request.detail = "assets " + ", ".join(map(str, request.assets))
        elif report.stopped:
            break  # rate limited before any clip of this request: it stays pending
        elif errors:
            request.status, request.detail = "failed", "; ".join(errors)
        else:
            request.status, request.detail = "none_found", "no new full HD portrait clip"
        _finish(conn, request)
        if report.stopped:
            break
    report.left_today = left
    return report
