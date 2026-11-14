"""Publishing through Postiz Cloud (SRD §10, FR-PUB-1..4), behind a `Publisher` interface.

At L0 the engine only creates drafts; Abbas schedules or deletes them in Postiz, and
reconciliation reads the result back from `posts:list` (§10). Before a draft, the
channel's settings schema is read and any setting Postiz would silently drop is refused
(FR-PUB-2); every file is uploaded first (FR-PUB-1).

The CLI runs pinned through `npx` (no global install) and each post goes to it as a
`--json` file: on Windows `postiz` is a `.cmd` shim, and JSON with quotes or `&` would not
survive the batch parser. Per-channel AI disclosure routes are in ADR 0002.

Metrics (FR-LRN-1) come from `analytics:post`: one entry per label whose last data point is
the post's lifetime total today. `{"missing": true}` means the platform gave no usable post
id; `posts:missing` lists the channel's recent videos and `posts:connect` links one. ADR 0021.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from engine.produce.process import Runner, output_tail, run_command

POSTIZ_VERSION = "2.0.27"
MISSING = "missing"  # Postiz's releaseId when the platform returned no usable post id
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # what may go on the CLI as an id
YOUTUBE_TITLE_MAX = 100  # YouTube's own limit, enforced by Postiz's settings DTO


class PostizError(RuntimeError):
    """Postiz refused a call, or a draft would not go out as written."""


@dataclass(frozen=True)
class ChannelSettings:
    rules: str | None
    schema: dict
    max_length: int | None = None


@dataclass(frozen=True)
class RemotePost:
    post_id: str
    integration_id: str
    state: str  # Postiz: QUEUE, PUBLISHED, ERROR, DRAFT
    publish_date: datetime | None
    release_url: str | None
    error: str | None
    release_id: str | None = None  # the platform's id (YouTube video id), or MISSING


@dataclass(frozen=True)
class AnalyticsResult:
    missing: bool  # Postiz answered {"missing": true}
    totals: dict[str, int]  # lowercase label -> lifetime total


@dataclass(frozen=True)
class MissingCandidate:
    release_id: str
    url: str  # a cover image, for Abbas to recognise the video


@runtime_checkable
class Publisher(Protocol):
    def upload(self, path: Path) -> str: ...

    def channel_settings(self, integration_id: str) -> ChannelSettings: ...

    def create_draft(self, payload: dict) -> str: ...

    def list_posts(self, start: datetime, end: datetime) -> list[RemotePost]: ...


@runtime_checkable
class PostizAnalytics(Protocol):
    def post_analytics(self, post_id: str, days: int) -> AnalyticsResult: ...

    def missing_content(self, post_id: str) -> list[MissingCandidate]: ...

    def connect(self, post_id: str, release_id: str) -> None: ...


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def youtube_settings(title: str, ai_disclosure: bool) -> dict:
    """YouTube via Postiz has no synthetic-content flag, so avatar posts go up private and
    Abbas ticks "altered or synthetic content" in Studio before making them public (C15)."""
    return {
        "title": title[:YOUTUBE_TITLE_MAX],
        "type": "private" if ai_disclosure else "public",
        "selfDeclaredMadeForKids": "no",
    }


def tiktok_settings(ai_disclosure: bool, privacy: str = "PUBLIC_TO_EVERYONE") -> dict:
    """TikTok's AI label rides on DIRECT_POST video posts (Postiz `video_made_with_ai`)."""
    return {
        "privacy_level": privacy,
        "content_posting_method": "DIRECT_POST",  # UPLOAD only drops it in the TikTok inbox
        "video_made_with_ai": ai_disclosure,
        "duet": False,
        "stitch": False,
        "comment": True,
        "autoAddMusic": "no",
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
    }


def unsupported_settings(settings: dict, schema: dict) -> list[str]:
    """Keys the channel's schema does not define: Postiz would drop them without an error."""
    properties = schema.get("properties") or {}
    if not properties:
        return []
    return [key for key in settings if key != "__type" and key not in properties]


def draft_payload(
    integration_id: str,
    platform: str,
    content: str,
    media: Sequence[str],
    settings: dict,
    publish_at: datetime,
) -> dict:
    """The public-API post body for one draft on one channel (what `posts:create` sends)."""
    return {
        "type": "draft",
        "date": _iso(publish_at),
        "shortLink": False,  # links keep their UTM tags as written (§2.3)
        "tags": [],
        "posts": [
            {
                "integration": {"id": integration_id},
                "value": [
                    {
                        "content": content,
                        "image": [
                            {"id": uuid.uuid5(uuid.NAMESPACE_URL, m).hex[:10], "path": m}
                            for m in media
                        ],
                        "delay": 0,
                    }
                ],
                "settings": {"__type": platform, **settings},
            }
        ],
    }


def _total(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_analytics(data: Any) -> AnalyticsResult:
    """`analytics:post` output: each label's last data point, as an int (others skipped)."""
    if isinstance(data, dict):
        return AnalyticsResult(missing=bool(data.get("missing")), totals={})
    totals: dict[str, int] = {}
    for entry in data or []:
        points = entry.get("data") or []
        value = _total(points[-1].get("total")) if points else None
        if value is not None and entry.get("label"):
            totals[str(entry["label"]).lower()] = value
    return AnalyticsResult(missing=False, totals=totals)


def safe_id(value: str, what: str) -> str:
    """An id about to go on the `postiz` command line. `npx` is a `.cmd` shim, so cmd.exe
    parses the arguments; ids from Postiz or platform answers (post, release) must not carry
    `&`, `|`, quotes or `%`. Postiz ids are cuids and platform ids are letters, digits, `_`
    and `-` (security review, ADR 0021)."""
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise PostizError(f"unsafe {what} refused before calling Postiz: {str(value)[:80]!r}")
    return value


def _candidates(data: Any) -> list[MissingCandidate]:
    """`posts:missing` answers; an id that could not go back on the command line is dropped."""
    return [
        MissingCandidate(str(c["id"]), str(c.get("url") or ""))
        for c in data or []
        if _SAFE_ID.fullmatch(str(c.get("id", "")))
    ]


def engine_state(remote: RemotePost | None, private_route: bool) -> str:
    """The §10 post state for what Postiz reports; gone from the list means Abbas deleted it."""
    if remote is None:
        return "rejected"
    if remote.state == "PUBLISHED":
        return "uploaded_private" if private_route else "published"
    return {"DRAFT": "drafted", "QUEUE": "scheduled", "ERROR": "failed"}.get(remote.state, "failed")


def draft_post(
    publisher: Publisher,
    integration_id: str,
    platform: str,
    content: str,
    files: Sequence[Path],
    settings: dict,
    publish_at: datetime,
) -> str:
    """Check the channel's rules, upload every file, then create one draft (FR-PUB-1, 2, 4)."""
    channel = publisher.channel_settings(integration_id)
    dropped = unsupported_settings(settings, channel.schema)
    if dropped:
        raise PostizError(
            f"settings Postiz would silently drop on {integration_id}: {', '.join(dropped)} "
            "(FR-PUB-2)"
        )
    if channel.max_length and len(content) > channel.max_length:
        raise PostizError(
            f"caption is {len(content)} characters; {integration_id} allows {channel.max_length}"
        )
    media = [publisher.upload(path) for path in files]
    return publisher.create_draft(
        draft_payload(integration_id, platform, content, media, settings, publish_at)
    )


def _settings_from(output: dict) -> ChannelSettings:
    return ChannelSettings(
        rules=output.get("rules"),
        schema=output.get("settings") or {},
        max_length=output.get("maxLength"),
    )


def _remote_from(post: dict) -> RemotePost:
    return RemotePost(
        post_id=str(post["id"]),
        integration_id=str((post.get("integration") or {}).get("id", "")),
        state=post["state"],
        publish_date=_parse_time(post.get("publishDate")),
        release_url=post.get("releaseURL") or None,
        error=post.get("error") or None,
        release_id=str(post["releaseId"]) if post.get("releaseId") else None,
    )


def _post_id(data: Any) -> str:
    for item in data if isinstance(data, list) else [data]:
        for key in ("postId", "id"):
            if isinstance(item, dict) and item.get(key):
                return str(item[key])
    raise PostizError(f"Postiz returned no post id: {str(data)[:200]}")


def _parse_cli_json(stdout: str) -> Any:
    """The CLI prints a status line (with an emoji), then pretty JSON."""
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            return json.loads("\n".join(lines[i:]))
    raise PostizError(f"no JSON in Postiz output: {stdout[:200]}")


@dataclass
class PostizCLI:
    api_key: str
    work_dir: Path  # where draft JSON files are written for `posts:create --json`
    run: Runner = run_command
    which: Callable[[str], str | None] = shutil.which

    def _call(self, *args: str) -> Any:
        npx = self.which("npx")
        if npx is None:
            raise PostizError("npx not found: the Postiz CLI runs on Node.js 22+ (see ce doctor)")
        env = {"POSTIZ_API_KEY": self.api_key, "npm_config_prefer_offline": "true"}
        result = self.run([npx, "--yes", f"postiz@{POSTIZ_VERSION}", *args], env)
        if result.returncode != 0:
            message = output_tail(result).replace(self.api_key, "***")
            raise PostizError(f"postiz {args[0]} failed (exit {result.returncode}): {message}")
        return _parse_cli_json(result.stdout)

    def upload(self, path: Path) -> str:
        # the CLI names the file by the text after the last "/"
        return self._call("upload", Path(path).as_posix())["path"]

    def channel_settings(self, integration_id: str) -> ChannelSettings:
        data = self._call("integrations:settings", safe_id(integration_id, "integration id"))
        return _settings_from(data.get("output", data))

    def create_draft(self, payload: dict) -> str:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        draft = self.work_dir / f"draft-{uuid.uuid4().hex[:12]}.json"
        draft.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return _post_id(self._call("posts:create", "--json", str(draft)))

    def list_posts(self, start: datetime, end: datetime) -> list[RemotePost]:
        data = self._call("posts:list", "--startDate", _iso(start), "--endDate", _iso(end))
        return [_remote_from(post) for post in data.get("posts", [])]

    def post_analytics(self, post_id: str, days: int) -> AnalyticsResult:
        post_id = safe_id(post_id, "post id")
        return parse_analytics(self._call("analytics:post", post_id, "-d", str(int(days))))

    def missing_content(self, post_id: str) -> list[MissingCandidate]:
        return _candidates(self._call("posts:missing", safe_id(post_id, "post id")))

    def connect(self, post_id: str, release_id: str) -> None:
        post_id, release_id = safe_id(post_id, "post id"), safe_id(release_id, "release id")
        self._call("posts:connect", post_id, "--release-id", release_id)


def _fake_settings() -> dict[str, dict]:
    youtube = {"title": {}, "type": {}, "selfDeclaredMadeForKids": {}, "thumbnail": {}, "tags": {}}
    tiktok = {key: {} for key in tiktok_settings(True)}
    return {
        "yt-1": {
            "rules": "type: public, private or unlisted. There is no synthetic-content setting.",
            "settings": {"properties": youtube},
            "maxLength": 5000,
        },
        "tt-1": {
            "rules": "content_posting_method must be DIRECT_POST to publish.",
            "settings": {"properties": tiktok},
            "maxLength": 2200,
        },
    }


@dataclass
class FakePostiz:
    """In-memory Postiz for dry runs and tests, plus helpers that play Abbas in Postiz."""

    settings: dict[str, dict] = field(default_factory=_fake_settings)
    posts: dict[str, dict] = field(default_factory=dict)
    drafts: dict[str, dict] = field(default_factory=dict)
    uploads: list[Path] = field(default_factory=list)
    metrics_for_release: dict[str, dict[str, int]] = field(default_factory=dict)
    candidates: dict[str, list[dict]] = field(default_factory=dict)  # per integration id

    # API-shaped answers (also what the fake CLI runner prints)
    def raw_upload(self, path: Path) -> dict:
        self.uploads.append(Path(path))
        return {"path": f"https://uploads.postiz.test/{Path(path).name}", "type": "video/mp4"}

    def raw_settings(self, integration_id: str) -> dict:
        if integration_id not in self.settings:
            raise PostizError(f"unknown integration {integration_id}")
        return {"output": self.settings[integration_id]}

    def raw_create(self, payload: dict) -> list[dict]:
        created = []
        for post in payload["posts"]:
            post_id = f"post-{len(self.posts) + len(created) + 1}"
            self.posts[post_id] = {
                "id": post_id,
                "publishDate": payload["date"],
                "releaseURL": None,
                "releaseId": None,
                "state": "DRAFT" if payload["type"] == "draft" else "QUEUE",
                "error": None,
                "integration": {"id": post["integration"]["id"]},
            }
            self.drafts[post_id] = payload
            created.append({"postId": post_id, "integration": post["integration"]["id"]})
        return created

    def raw_list(self) -> dict:
        return {"posts": list(self.posts.values())}

    def raw_analytics(self, post_id: str, days: int | str) -> list[dict] | dict:
        release = (self.posts.get(post_id) or {}).get("releaseId")
        if release == MISSING:
            return {"missing": True}
        totals = self.metrics_for_release.get(release, {}) if release else {}
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        return [
            {"label": label, "percentageChange": 0, "data": [{"total": str(n), "date": day}]}
            for label, n in totals.items()
        ]

    def raw_missing(self, post_id: str) -> list[dict]:
        post = self.posts.get(post_id) or {}
        if post.get("releaseId") != MISSING:
            return []
        return list(self.candidates.get(post["integration"]["id"], []))

    def raw_connect(self, post_id: str, release_id: str) -> dict:
        if post_id not in self.posts:
            raise PostizError(f"unknown post {post_id}")
        self.posts[post_id]["releaseId"] = release_id
        return self.posts[post_id]

    # Publisher
    def upload(self, path: Path) -> str:
        return self.raw_upload(path)["path"]

    def channel_settings(self, integration_id: str) -> ChannelSettings:
        return _settings_from(self.raw_settings(integration_id)["output"])

    def create_draft(self, payload: dict) -> str:
        return _post_id(self.raw_create(payload))

    def list_posts(self, start: datetime, end: datetime) -> list[RemotePost]:
        return [_remote_from(p) for p in self.raw_list()["posts"]]

    # PostizAnalytics
    def post_analytics(self, post_id: str, days: int) -> AnalyticsResult:
        return parse_analytics(self.raw_analytics(post_id, days))

    def missing_content(self, post_id: str) -> list[MissingCandidate]:
        return _candidates(self.raw_missing(post_id))

    def connect(self, post_id: str, release_id: str) -> None:
        self.raw_connect(post_id, release_id)

    # Abbas, in the Postiz app
    def schedule(self, post_id: str) -> None:
        self.posts[post_id]["state"] = "QUEUE"

    def publish(self, post_id: str, release_url: str, release_id: str | None = None) -> None:
        self.posts[post_id].update(state="PUBLISHED", releaseURL=release_url,
                                   releaseId=release_id)  # fmt: skip

    def publish_missing(self, post_id: str) -> None:
        """Published, but the platform gave no usable id (TikTok, sometimes)."""
        self.posts[post_id].update(state="PUBLISHED", releaseURL=None, releaseId=MISSING)

    def set_metrics(self, post_id: str, **totals: int) -> None:
        """The platform's lifetime counts for a published post, by Postiz label."""
        self.metrics_for_release[self.posts[post_id]["releaseId"]] = dict(totals)

    def fail(self, post_id: str, error: str) -> None:
        self.posts[post_id].update(state="ERROR", error=error)

    def delete(self, post_id: str) -> None:
        self.posts.pop(post_id)
