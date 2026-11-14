"""The daily digest (SRD §13): what waits on Abbas, rebuilt from the database each time, and
avatar requests and holds older than 48 h dropped with a reason (FR-PRD-3)."""

import json
import re
from datetime import UTC, date, datetime

import pytest

from engine.agents.state import sync_identities
from engine.config import load_config
from engine.db import open_db
from engine.ops.digest import build_digest, expire_waiting, write_digest

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
DAY = date(2026, 11, 20)


@pytest.fixture
def conn(tmp_path, repo_root):
    db = open_db(tmp_path / "engine.db")
    sync_identities(db, load_config(repo_root / "config"))
    return db


@pytest.fixture
def config(repo_root):
    return load_config(repo_root / "config")


def post(conn, state, updated="2026-11-20T06:00:00Z", **fields) -> int:
    brief = conn.execute(
        "INSERT INTO briefs (identity_id, format, topic, angle, hook, cta, difference_note) "
        "VALUES ('selfhelp', 'C', 'starting small', 'craft', 'hook', 'follow', 'new')"
    ).lastrowid
    render = None
    if state not in ("planned", "scripted", "voiced", "avatar_waiting", "avatar_ready"):
        plan = conn.execute(
            "INSERT INTO edit_plans (brief_id, version, plan) VALUES (?, 1, '{}')", (brief,)
        ).lastrowid
        render = conn.execute(
            "INSERT INTO renders (edit_plan_id, path) VALUES (?, ?)",
            (plan, f"C:/media/renders/selfhelp-{brief}-{plan}/post.mp4"),
        ).lastrowid
    channel = conn.execute("SELECT id FROM channels WHERE platform = 'youtube'").fetchone()[0]
    cols = {"brief_id": brief, "channel_id": channel, "render_id": render, "state": state,
            "ai_disclosure": 1, "updated_at": updated, **fields}  # fmt: skip
    return conn.execute(
        f"INSERT INTO posts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    ).lastrowid


def digest(conn, config, tmp_path):
    return build_digest(conn, config, DAY, work_dir=tmp_path / "work",
                        inbox_dir=tmp_path / "inbox")  # fmt: skip


def test_drafts_holds_avatar_cards_and_label_ticks_wait_on_abbas(conn, config, tmp_path):
    post(conn, "drafted", title="Start smaller", postiz_id="pz-1",
         scheduled_at="2026-11-21T07:00:00Z")  # fmt: skip
    held = post(conn, "qa_held")
    render = conn.execute("SELECT render_id FROM posts WHERE id = ?", (held,)).fetchone()[0]
    conn.execute(
        "INSERT INTO qa_results (render_id, check_name, result, details) VALUES (?, ?, ?, ?)",
        (render, "qa_review", "hold", json.dumps({"reasons": ["tone may not fit"]})),
    )
    post(conn, "avatar_waiting")
    post(conn, "uploaded_private", title="Keep going", platform_id="yt123")
    d = digest(conn, config, tmp_path)
    assert d.summary() == "1 draft to approve, 1 hold, 1 avatar request, 1 YouTube label to tick"
    assert "Start smaller" in d.drafts[0].text and d.drafts[0].link.startswith("https://")
    assert "tone may not fit" in d.holds[0].text and d.holds[0].link.startswith("file:///")
    assert "inbox" in d.avatars[0].text and str(tmp_path / "inbox") in d.avatars[0].text
    assert d.labels[0].link == "https://studio.youtube.com/video/yt123/edit"
    assert len(d.needs_abbas) == 4


def test_nothing_waiting_means_no_needs(conn, config, tmp_path):
    post(conn, "scheduled", title="Queued")
    post(conn, "published", title="Live")
    d = digest(conn, config, tmp_path)
    assert d.needs_abbas == [] and d.summary() == "nothing waits on you"


def test_requests_and_holds_older_than_48_hours_are_dropped(conn, config, tmp_path):
    old_avatar = post(conn, "avatar_waiting", updated="2026-11-17T07:00:00Z")
    old_hold = post(conn, "qa_held", updated="2026-11-18T07:59:00Z")
    fresh = post(conn, "avatar_waiting", updated="2026-11-19T09:00:00Z")
    dropped = expire_waiting(conn, NOW)
    assert sorted(i.post_id for i in dropped) == sorted([old_avatar, old_hold])
    states = dict(conn.execute("SELECT id, state FROM posts").fetchall())
    assert states[old_avatar] == states[old_hold] == "failed" and states[fresh] == "avatar_waiting"
    reasons = [r[0] for r in conn.execute("SELECT reject_reason FROM posts WHERE state = 'failed'")]
    assert all("48 h" in r for r in reasons)
    d = digest(conn, config, tmp_path)
    assert len(d.failures) == 2 and len(d.avatars) == 1  # reported once, not waiting any more


def test_budget_reads_the_month_ledger_against_the_caps(conn, config, tmp_path):
    conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, charge_key) "
        "VALUES ('elevenlabs', '2026-11', 4200, 'voice:script:1')"
    )
    d = digest(conn, config, tmp_path)
    assert d.budget["elevenlabs_chars"] == (4200, 78600)


def test_digest_is_rebuilt_from_state(conn, config, tmp_path):
    draft = post(conn, "drafted", title="Start smaller", postiz_id="pz-1")
    first = write_digest(digest(conn, config, tmp_path), tmp_path / "reports")
    again = write_digest(digest(conn, config, tmp_path), tmp_path / "reports")
    assert first == again == tmp_path / "reports" / "2026-11-20.md"
    assert first.read_text("utf-8").count("Start smaller") == 1
    conn.execute("UPDATE posts SET state = 'scheduled' WHERE id = ?", (draft,))  # Abbas acted
    rebuilt = digest(conn, config, tmp_path)
    assert rebuilt.needs_abbas == []  # a scheduled post waits on nobody
    text = write_digest(rebuilt, tmp_path / "reports").read_text("utf-8")
    assert "Start smaller" not in text


def test_a_post_postiz_cannot_match_waits_to_be_connected(conn, config, tmp_path):
    post(conn, "published", title="Keep going", postiz_id="pz-9", platform_id="missing")
    post(conn, "published", title="Linked", postiz_id="pz-8", platform_id="7321")
    post(conn, "uploaded_private", title="Private", platform_id="missing")
    d = digest(conn, config, tmp_path)
    (item,) = d.connects
    assert "Keep going" in item.text and "connect" in item.text
    assert item.link == "https://platform.postiz.com"
    assert d.labels[0].link == "https://studio.youtube.com"  # never /video/missing/edit
    assert "1 post to connect in Postiz" in d.summary()
    assert item in d.needs_abbas
    assert "### Posts to connect in Postiz" in d.markdown()


def test_the_markdown_has_every_section(conn, config, tmp_path):
    text = digest(conn, config, tmp_path).markdown()
    for heading in ("# Content engine digest, 2026-11-20", "## Waiting on you",
                    "## Failures and drops", "## Budget this month"):  # fmt: skip
        assert heading in text


def test_model_written_items_cannot_inject_markdown_or_links(conn, config, tmp_path):
    report = {"needs_abbas": [
        {"item": "Approve [this](javascript:alert(1)) <img src=x> `code`\nnow",
         "link": "javascript:alert(1)"},
        {"item": "Check the draft", "link": "https://evil.example/phish"},
        {"item": "Tick the label", "link": "https://studio.youtube.com/video/abc/edit"},
    ]}  # fmt: skip
    conn.execute(
        "INSERT INTO agent_runs (run_id, command, report, started_at) VALUES (?, ?, ?, ?)",
        ("run-x", "ce daily", json.dumps(report), "2026-11-20T01:00:00Z"),
    )
    text = digest(conn, config, tmp_path).markdown()
    targets = re.findall(r"(?<!\\)\]\(([^)]*)\)", text)  # links a Markdown renderer would make
    assert targets == ["https://studio.youtube.com/video/abc/edit"]  # the allowed host only
    assert "<img" not in text and "evil.example" not in text
    assert "\nnow" not in text  # one line per item


# Alerts (NFR-4 hard stops, GR-7 pauses) -----------------------------------------------------


def test_a_paused_identity_is_an_alert(conn, config, tmp_path):
    from engine.ops.killswitch import kill

    kill(conn, "selfhelp", "YouTube strike on post 4")
    d = digest(conn, config, tmp_path)
    assert [i.text for i in d.paused] == [
        "selfhelp: YouTube strike on post 4; `ce resume selfhelp` when it is safe"
    ]
    assert d.summary() == "1 identity paused" and d.needs_abbas == d.paused
    assert "## Alerts" in d.markdown()


def test_a_budget_stop_alone_raises_the_toast(conn, config, tmp_path):
    conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, cost, charge_key) "
        "VALUES ('anthropic', '2026-11', 60, 60, 'claude:run:r1')"
    )
    d = digest(conn, config, tmp_path)
    assert len(d.stops) == 1 and d.stops[0].text.startswith("Claude: 60.00 of 60.00 USD")
    assert d.summary() == "1 budget stop" and d.needs_abbas == d.stops


def test_budget_shows_the_heygen_cap(conn, config, tmp_path):
    cap = config.engine.budgets.heygen.monthly_credit_cap
    assert digest(conn, config, tmp_path).budget["heygen_credits"] == (0.0, cap)
