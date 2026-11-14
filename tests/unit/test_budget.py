"""NFR-4 budgets: the ledger, each vendor's monthly cap and the preflights before paid work."""

import json

import pytest

from engine.agents.state import start_run, sync_identities
from engine.config import load_config
from engine.db import open_db
from engine.ops.budget import (
    BudgetStop,
    avatar_credits,
    budget_lines,
    claude_run_budget,
    disk_preflight,
    heygen_preflight,
    record_avatar,
    record_claude_run,
)

MONTH = "2026-11"
GB = 1024**3


@pytest.fixture
def config(repo_root):
    return load_config(repo_root / "config")


@pytest.fixture
def conn(tmp_path, config):
    db = open_db(tmp_path / "engine.db")
    sync_identities(db, config)
    return db


def spend(conn, usd: float, key: str = "earlier") -> None:
    conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, cost, charge_key) "
        "VALUES ('anthropic', ?, ?, ?, ?)",
        (MONTH, usd, usd, key),
    )


def test_the_run_budget_is_the_command_budget_while_the_month_has_room(conn, config):
    assert claude_run_budget(conn, config, MONTH, "daily") == 3
    assert claude_run_budget(conn, config, MONTH, "weekly") == 5


def test_the_run_budget_never_exceeds_what_is_left(conn, config):
    spend(conn, 58.5)
    assert claude_run_budget(conn, config, MONTH, "daily") == pytest.approx(1.5)
    assert claude_run_budget(conn, config, MONTH, "weekly") == pytest.approx(1.5)


def test_a_spent_month_stops_the_run(conn, config):
    spend(conn, 60.0)
    with pytest.raises(BudgetStop, match="monthly cap"):
        claude_run_budget(conn, config, MONTH, "daily")


def test_a_run_is_recorded_once(conn):
    start_run(conn, "run-1", "ce daily")
    for _ in range(2):  # a resumed run reports again
        record_claude_run(conn, "run-1", MONTH, 1.25, model_usage={"opus": 1}, turns=12,
                          subtype="success")  # fmt: skip
    rows = conn.execute("SELECT cost, run_id FROM budget_ledger").fetchall()
    assert [tuple(r) for r in rows] == [(1.25, "run-1")]
    run = conn.execute("SELECT cost_usd, turns, subtype, model_usage, ended_at FROM agent_runs")
    cost, turns, subtype, usage, ended = run.fetchone()
    assert (cost, turns, subtype, json.loads(usage)) == (1.25, 12, "success", {"opus": 1})
    assert ended is not None


def test_avatar_credits_follow_the_audio_length(config):
    assert avatar_credits(45.0, 1.0) == pytest.approx(0.75)
    assert avatar_credits(30.0, 2.0) == pytest.approx(1.0)


def test_avatar_credits_are_charged_once_per_script(conn):
    record_avatar(conn, MONTH, script_id=7, credits=0.75)
    record_avatar(conn, MONTH, script_id=7, credits=0.75)
    assert conn.execute("SELECT SUM(units) FROM budget_ledger").fetchone()[0] == 0.75


def test_heygen_preflight_refuses_a_request_over_the_cap(conn, config):
    cap = config.engine.budgets.heygen.monthly_credit_cap
    assert heygen_preflight(conn, config, MONTH, 0.75) is None
    record_avatar(conn, MONTH, script_id=1, credits=cap - 0.5)
    reason = heygen_preflight(conn, config, MONTH, 0.75)
    assert reason is not None and "HeyGen" in reason and f"{cap:g}" in reason


def test_disk_preflight_stops_below_the_threshold(config):
    stop = config.engine.disk.stop_free_gb
    assert disk_preflight(int((stop + 1) * GB), stop) is None
    assert "NFR-5" in disk_preflight(int((stop - 1) * GB), stop)


def test_budget_lines_mark_a_vendor_at_its_cap(conn, config):
    spend(conn, 60.0)
    lines = {line.vendor: line for line in budget_lines(conn, config, MONTH)}
    assert set(lines) == {"anthropic", "elevenlabs", "heygen"}
    assert lines["anthropic"].stopped and not lines["elevenlabs"].stopped
    assert lines["heygen"].cap == config.engine.budgets.heygen.monthly_credit_cap
