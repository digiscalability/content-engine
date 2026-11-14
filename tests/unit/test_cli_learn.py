"""`ce reconcile`, `ce metrics` and `ce reject` (SRD §5.2, §10, FR-LRN-1): wired to fakes
here; without a Postiz key a live run stops with the G0.7 pointer, and `--dry-run` calls and
writes nothing."""

from datetime import UTC, datetime, timedelta

import pytest
from postseed import row, seed_post, seeded_db
from typer.testing import CliRunner

from engine import cli
from engine.cli import app
from engine.publish.postiz import FakePostiz, PostizError, draft_payload
from engine.publish.youtube import FakeVisibility

runner = CliRunner()
VIDEO = "dQw4w9WgXcQ"


@pytest.fixture
def conn(tmp_path, repo_root, monkeypatch):
    db = seeded_db(tmp_path / "engine.db", repo_root)
    monkeypatch.setattr(cli, "_engine_db", lambda: (cli.load_config(), db))
    monkeypatch.setattr(cli, "make_visibility", lambda: FakeVisibility(public={VIDEO}))
    return db


@pytest.fixture
def postiz(monkeypatch):
    fake = FakePostiz()
    monkeypatch.setattr(cli, "make_postiz", lambda config: fake)
    return fake


def drafted(conn, postiz, **fields):
    when = datetime.now(UTC) + timedelta(hours=3)
    pid = postiz.create_draft(draft_payload("yt-1", "youtube", "c", [], {}, when))
    return seed_post(conn, fields.pop("state", "drafted"), postiz_id=pid, **fields), pid


def test_reconcile_without_a_postiz_key_points_at_g0_7(conn, monkeypatch):
    monkeypatch.setattr(cli, "make_postiz", lambda config: None)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 2, result.output
    assert "POSTIZ_API_KEY" in result.output and "G0.7" in result.output


def test_reconcile_reports_each_change(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.publish(pid, "u", VIDEO)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert f"post {post}: drafted -> uploaded_private" in result.output
    assert f"post {post}: uploaded_private -> published" in result.output


def test_a_postiz_failure_exits_one(conn, monkeypatch):
    class Broken(FakePostiz):
        def list_posts(self, start, end):
            raise PostizError("postiz posts:list failed (exit 1): 401")

    broken = Broken()
    monkeypatch.setattr(cli, "make_postiz", lambda config: broken)
    drafted(conn, broken)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 1 and "401" in result.output


@pytest.mark.parametrize("argv", [["reconcile", "--dry-run"], ["--dry-run", "reconcile"]])
def test_reconcile_dry_run_calls_and_writes_nothing(conn, postiz, monkeypatch, argv):
    post, pid = drafted(conn, postiz)
    postiz.schedule(pid)
    monkeypatch.setattr(cli, "make_postiz", lambda config: pytest.fail("Postiz was called"))
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert f"post {post} on youtube: drafted" in result.output
    assert row(conn, post)["state"] == "drafted"


def test_metrics_reports_snapshots(conn, postiz):
    published = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    post, pid = drafted(conn, postiz, state="published", platform_id=VIDEO,
                        published_at=published)  # fmt: skip
    postiz.publish(pid, "u", VIDEO)
    postiz.set_metrics(pid, Views=120)
    result = runner.invoke(app, ["metrics"])
    assert result.exit_code == 0, result.output
    assert f"post {post}: 24h snapshot" in result.output


def test_metrics_exits_one_when_a_pull_failed(conn, monkeypatch):
    class Broken(FakePostiz):
        def post_analytics(self, post_id, days):
            raise PostizError("postiz analytics:post failed (exit 1): 401")

    broken = Broken()
    monkeypatch.setattr(cli, "make_postiz", lambda config: broken)
    published = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    post, _ = drafted(conn, broken, state="published", platform_id=VIDEO, published_at=published)
    result = runner.invoke(app, ["metrics"])
    assert result.exit_code == 1, result.output
    assert f"post {post}: FAILED" in result.output and "401" in result.output


def test_metrics_dry_run_lists_what_is_due(conn, postiz, monkeypatch):
    published = (datetime.now(UTC) - timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%SZ")
    post, _ = drafted(conn, postiz, state="published", platform_id=VIDEO,
                      published_at=published)  # fmt: skip
    monkeypatch.setattr(cli, "make_postiz", lambda config: pytest.fail("Postiz was called"))
    result = runner.invoke(app, ["metrics", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert f"post {post}: 72h" in result.output
    assert conn.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] == 0


def test_metrics_without_a_key_points_at_g0_7(conn, monkeypatch):
    monkeypatch.setattr(cli, "make_postiz", lambda config: None)
    result = runner.invoke(app, ["metrics"])
    assert result.exit_code == 2 and "G0.7" in result.output


def test_reject_records_the_reason(conn, postiz):
    post, _ = drafted(conn, postiz)
    result = runner.invoke(app, ["reject", str(post), "hook too slow"])
    assert result.exit_code == 0, result.output
    assert (row(conn, post)["state"], row(conn, post)["reject_reason"]) == (
        "rejected",
        "hook too slow",
    )
    assert "Postiz" in result.output  # reminds Abbas the engine deletes nothing


def test_reject_refuses_what_it_cannot_reject(conn):
    post = seed_post(conn, "qa_passed")
    result = runner.invoke(app, ["reject", str(post), "no"])
    assert result.exit_code == 1 and "qa_passed" in result.output
    assert row(conn, post)["state"] == "qa_passed"
