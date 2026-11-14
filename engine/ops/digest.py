"""The daily digest `reports/YYYY-MM-DD.md` (SRD §13, D11): everything waiting on Abbas,
built from the database each time it runs, so a rerun the same day neither duplicates nor
loses an item.

- Waiting on Abbas: drafts to approve in Postiz, QA holds with their reasons, avatar
  request cards, YouTube "altered or synthetic content" ticks (FR-PUB-3), published posts
  Postiz could not match to their video (FR-LRN-1), and the `needs_abbas` items of the
  day's runs.
- Failures and drops: posts that failed today, including avatar requests and holds older
  than 48 h, which `expire_waiting()` drops with a reason (FR-PRD-3, §13).
- Budget: this month's spend against each vendor's cap.

The digest holds ids, titles, reasons and links, never a secret (NFR-7). ADR 0016.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from engine.config import Config
from engine.ops.budget import BudgetLine, budget_lines
from engine.publish.postiz import MISSING

MAX_WAIT = timedelta(hours=48)
POSTIZ_APP = "https://platform.postiz.com"  # Postiz Cloud's app; drafts wait there
STUDIO = "https://studio.youtube.com"
_EXPIRED = {
    "avatar_waiting": "the avatar request waited more than 48 h and was dropped",
    "qa_held": "the QA hold waited more than 48 h for a decision and was dropped",
}


# Run reports, titles and QA reasons are written by a model: their text is escaped, and a
# link from a run report is kept only if it is https on one of these hosts. Only links the
# engine builds itself (a render, the avatar inbox) may be file:/// (security review).
SAFE_HOSTS = ("platform.postiz.com", "studio.youtube.com", "app.heygen.com")
_MARKDOWN = re.compile(r"([\\`*_\[\]()|#!])")


def plain(text: str) -> str:
    """One line of text that Markdown shows as written: no links, emphasis or HTML."""
    line = " ".join(str(text).split())
    line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN.sub(r"\\\1", line)


def safe_link(link: str, engine_built: bool) -> str:
    if engine_built and link.startswith("file:///"):
        return link.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    parts = urlsplit(link)
    if parts.scheme == "https" and parts.hostname in SAFE_HOSTS:
        return link.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return ""


@dataclass(frozen=True)
class Item:
    text: str
    link: str = ""
    post_id: int | None = None
    engine_built: bool = True  # False for items a model wrote (run reports)

    def markdown(self) -> str:
        link = safe_link(self.link, self.engine_built) if self.link else ""
        return f"- {plain(self.text)}" + (f" ([open]({link}))" if link else "")


@dataclass
class Digest:
    day: date
    drafts: list[Item] = field(default_factory=list)
    holds: list[Item] = field(default_factory=list)
    avatars: list[Item] = field(default_factory=list)
    labels: list[Item] = field(default_factory=list)
    connects: list[Item] = field(default_factory=list)  # FR-LRN-1: no usable platform id
    from_runs: list[Item] = field(default_factory=list)
    failures: list[Item] = field(default_factory=list)
    budget: dict[str, tuple[float, float | None]] = field(default_factory=dict)
    paused: list[Item] = field(default_factory=list)  # GR-7: kill flags set
    stops: list[Item] = field(default_factory=list)  # NFR-4: vendors at their monthly cap

    @property
    def needs_abbas(self) -> list[Item]:
        return [*self.paused, *self.stops, *self.drafts, *self.holds, *self.avatars,
                *self.labels, *self.connects, *self.from_runs]  # fmt: skip

    def summary(self) -> str:
        def count(n: int, one: str, many: str) -> str | None:
            return f"{n} {one if n == 1 else many}" if n else None

        parts = [
            count(len(self.paused), "identity paused", "identities paused"),
            count(len(self.stops), "budget stop", "budget stops"),
            count(len(self.drafts), "draft to approve", "drafts to approve"),
            count(len(self.holds), "hold", "holds"),
            count(len(self.avatars), "avatar request", "avatar requests"),
            count(len(self.labels), "YouTube label to tick", "YouTube labels to tick"),
            count(len(self.connects), "post to connect in Postiz", "posts to connect in Postiz"),
            count(len(self.from_runs), "other item", "other items"),
        ]
        found = [p for p in parts if p]
        return ", ".join(found) if found else "nothing waits on you"

    def markdown(self) -> str:
        def section(title: str, items: list[Item], empty: str) -> list[str]:
            lines = [f"### {title}", ""]
            for item in items:
                lines.append(item.markdown())
            return [*lines, *([] if items else [f"- {empty}"]), ""]

        lines = [f"# Content engine digest, {self.day.isoformat()}", "",
                 f"**Waiting on you:** {self.summary()}.", ""]  # fmt: skip
        if self.paused or self.stops:
            lines += ["## Alerts", ""]
            if self.paused:
                lines += section("Paused identities (no new work)", self.paused, "none")
            if self.stops:
                lines += section("Budget stops (no paid work until next month)", self.stops,
                                 "none")  # fmt: skip
        lines += ["## Waiting on you", ""]
        lines += section("Drafts to approve in Postiz", self.drafts, "none")
        lines += section("Holds (decide within 48 h)", self.holds, "none")
        lines += section("Avatar requests", self.avatars, "none")
        lines += section("YouTube labels to tick", self.labels, "none")
        if self.connects:
            lines += section("Posts to connect in Postiz", self.connects, "none")
        if self.from_runs:
            lines += section("From today's runs", self.from_runs, "none")
        lines += ["## Failures and drops", ""]
        lines += [i.markdown() for i in self.failures] or ["- none"]
        lines += ["", "## Budget this month", "", "| Vendor | Used | Cap |", "|---|---:|---:|"]
        for name, (used, cap) in self.budget.items():
            lines.append(f"| {name} | {used:,.2f} | {'-' if cap is None else f'{cap:,.2f}'} |")
        return "\n".join(lines) + "\n"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def expire_waiting(conn: sqlite3.Connection, now: datetime) -> list[Item]:
    """Drop avatar requests and holds older than 48 h (they never published, so failing
    them loses nothing) and return what was dropped."""
    cutoff = _stamp(now - MAX_WAIT)
    rows = conn.execute(
        "SELECT id, state FROM posts WHERE state IN ('avatar_waiting', 'qa_held') "
        "AND COALESCE(updated_at, created_at) < ?",
        (cutoff,),
    ).fetchall()
    dropped = []
    for row in rows:
        reason = _EXPIRED[row["state"]]
        conn.execute(
            "UPDATE posts SET state = 'failed', reject_reason = ?, updated_at = ? WHERE id = ?",
            (reason, _stamp(now), row["id"]),
        )
        dropped.append(Item(f"post {row['id']}: {reason}", post_id=row["id"]))
    return dropped


def _posts(conn: sqlite3.Connection, where: str, *args) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, c.platform, b.identity_id, b.topic, r.path AS render_path FROM posts p "
        "JOIN channels c ON c.id = p.channel_id JOIN briefs b ON b.id = p.brief_id "
        f"LEFT JOIN renders r ON r.id = p.render_id WHERE {where} ORDER BY p.id",
        args,
    ).fetchall()


def _label(post: sqlite3.Row) -> str:
    title = post["title"] or post["topic"]
    return f'{post["identity_id"]} {post["platform"]} post {post["id"]}, "{title}"'


def _voice_file(conn: sqlite3.Connection, brief_id: int, work_dir: Path) -> Path | None:
    for (script_id,) in conn.execute(
        "SELECT id FROM scripts WHERE brief_id = ? ORDER BY version DESC", (brief_id,)
    ):
        wav = work_dir / "voice" / f"script-{script_id}.wav"
        if wav.is_file():
            return wav
    return None


BUDGET_KEYS = {"elevenlabs": "elevenlabs_chars", "anthropic": "claude_usd",
               "heygen": "heygen_credits"}  # fmt: skip


def _budget(lines: list[BudgetLine]) -> dict:
    order = {vendor: i for i, vendor in enumerate(BUDGET_KEYS)}
    return {
        BUDGET_KEYS[line.vendor]: (line.used, line.cap)
        for line in sorted(lines, key=lambda line: order[line.vendor])
    }


def build_digest(
    conn: sqlite3.Connection,
    config: Config,
    day: date,
    work_dir: Path,
    inbox_dir: Path,
) -> Digest:
    digest = Digest(day=day)
    for post in _posts(conn, "p.state = 'drafted'"):
        when = f" for {post['scheduled_at']}" if post["scheduled_at"] else ""
        digest.drafts.append(Item(f"{_label(post)}, Postiz {post['postiz_id']}{when}",
                                  POSTIZ_APP, post["id"]))  # fmt: skip
    for post in _posts(conn, "p.state = 'qa_held'"):
        reasons = [
            reason
            for (details,) in conn.execute(
                "SELECT details FROM qa_results WHERE render_id = ? AND result = 'hold'",
                (post["render_id"],),
            )
            for reason in json.loads(details or "{}").get("reasons", [])
        ]
        link = Path(post["render_path"]).as_uri() if post["render_path"] else ""
        text = f"{_label(post)}: {'; '.join(reasons) or 'no reason recorded'}"
        digest.holds.append(Item(text, link, post["id"]))
    seen_briefs = set()
    for post in _posts(conn, "p.state = 'avatar_waiting'"):
        if post["brief_id"] in seen_briefs:
            continue  # one card per brief, whatever its channels
        seen_briefs.add(post["brief_id"])
        wav = _voice_file(conn, post["brief_id"], work_dir)
        since = datetime.fromisoformat(
            (post["updated_at"] or post["created_at"]).replace("Z", "+00:00")
        )
        text = (f"brief {post['brief_id']} (\"{post['topic']}\"): render the avatar from "
                f"{wav or 'its voiced script'} and save it as "
                f"{inbox_dir / (str(post['brief_id']) + '.mp4')}; dropped after "
                f"{_stamp(since + MAX_WAIT)}")  # fmt: skip
        digest.avatars.append(Item(text, inbox_dir.as_uri() if inbox_dir.is_absolute() else "",
                                   post["id"]))  # fmt: skip
    for post in _posts(conn, "p.state = 'uploaded_private' AND c.platform = 'youtube'"):
        video = post["platform_id"] if post["platform_id"] != MISSING else None
        link = f"{STUDIO}/video/{video}/edit" if video else STUDIO
        tick = 'tick "altered or synthetic content", then set it public or schedule it'
        digest.labels.append(Item(f"{_label(post)}: {tick}", link, post["id"]))
    for post in _posts(conn, "p.state = 'published' AND p.platform_id = ?", MISSING):
        text = (f"{_label(post)}, Postiz {post['postiz_id']}: Postiz could not tell which "
                "published video this is; open the post in Postiz and connect its video, "
                "so its metrics can be read")  # fmt: skip
        digest.connects.append(Item(text, POSTIZ_APP, post["id"]))
    failed_today = "p.state = 'failed' AND date(p.updated_at, 'localtime') = ?"
    for post in _posts(conn, failed_today, day.isoformat()):
        reason = post["reject_reason"] or "failed"
        digest.failures.append(Item(f"{_label(post)}: {reason}", post_id=post["id"]))
    seen = set()
    for (report,) in conn.execute(
        "SELECT report FROM agent_runs WHERE report IS NOT NULL "
        "AND date(started_at, 'localtime') = ?",
        (day.isoformat(),),
    ):
        for need in json.loads(report).get("needs_abbas", []):
            if need["item"] not in seen:
                seen.add(need["item"])
                link = need.get("link", "")
                digest.from_runs.append(Item(need["item"], link, engine_built=False))
    lines = budget_lines(conn, config, day.strftime("%Y-%m"))
    digest.budget = _budget(lines)
    for line in lines:
        if line.stopped:
            digest.stops.append(Item(
                f"{line.label}: {line.used:,.2f} of {line.cap:,.2f} {line.unit} used; "
                "raise the cap in engine.yaml or wait for next month (NFR-4)"
            ))  # fmt: skip
    for row in conn.execute(
        "SELECT id, kill_reason FROM identities WHERE kill_flag = 1 ORDER BY id"
    ):
        reason = row["kill_reason"] or "kill flag set"
        digest.paused.append(
            Item(f"{row['id']}: {reason}; `ce resume {row['id']}` when it is safe")
        )
    return digest


def write_digest(digest: Digest, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{digest.day.isoformat()}.md"
    path.write_text(digest.markdown(), encoding="utf-8")
    return path
