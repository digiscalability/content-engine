"""GR-7 manual kill switch and §17: `kill`, `warn` and `resume` on the identities table, each
logged in identity_events; a killed identity stays at L0 until Abbas resumes it."""

import pytest

from engine.agents.state import sync_identities
from engine.config import load_config
from engine.db import open_db
from engine.ops.killswitch import KillSwitchError, events, in_flight, kill, resume, warn


@pytest.fixture
def config(repo_root):
    base = load_config(repo_root / "config")
    selfhelp = base.identities["selfhelp"].model_copy(update={"autonomy_level": 1})
    from dataclasses import replace

    return replace(base, identities={"selfhelp": selfhelp})  # an L1 identity, as if earned


@pytest.fixture
def conn(tmp_path, config):
    db = open_db(tmp_path / "engine.db")
    sync_identities(db, config)
    return db


def identity(conn) -> tuple:
    row = conn.execute("SELECT kill_flag, kill_reason, autonomy_level FROM identities").fetchone()
    return tuple(row)


def test_kill_sets_the_flag_and_drops_to_l0(conn):
    kill(conn, "selfhelp", "manual stop")
    assert identity(conn) == (1, "manual stop", 0)
    assert [(e["kind"], e["detail"]) for e in events(conn, "selfhelp")] == [("kill", "manual stop")]


def test_sync_keeps_a_killed_identity_at_l0(conn, config):
    kill(conn, "selfhelp", "manual stop")
    sync_identities(conn, config)  # the YAML still says L1
    assert identity(conn) == (1, "manual stop", 0)


def test_warn_records_the_warning_and_kills(conn):
    warn(conn, "selfhelp", "YouTube: community guidelines warning on post 12")
    flag, reason, level = identity(conn)
    assert flag == 1 and level == 0 and reason.startswith("platform warning: YouTube")
    assert [e["kind"] for e in events(conn, "selfhelp")] == ["warn", "kill"]


def test_resume_restores_the_yaml_level(conn, config):
    kill(conn, "selfhelp", "manual stop")
    resume(conn, config, "selfhelp")
    assert identity(conn) == (0, None, 1)
    assert [e["kind"] for e in events(conn, "selfhelp")] == ["kill", "resume"]


def test_resume_of_a_running_identity_is_refused(conn, config):
    with pytest.raises(KillSwitchError, match="not paused"):
        resume(conn, config, "selfhelp")


def test_kill_refuses_an_unknown_identity(conn):
    with pytest.raises(KillSwitchError, match="nobody"):
        kill(conn, "nobody", "x")
    assert conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 1


def test_a_kill_needs_a_reason(conn):
    with pytest.raises(KillSwitchError, match="reason"):
        kill(conn, "selfhelp", "  ")


def test_in_flight_lists_posts_that_could_still_go_out(conn):
    channel = conn.execute("SELECT id FROM channels").fetchone()[0]
    for state, postiz in (("planned", None), ("drafted", "pz-1"), ("published", "pz-0")):
        brief = conn.execute(
            "INSERT INTO briefs (identity_id, format, topic, angle, hook, cta, difference_note) "
            "VALUES ('selfhelp', 'C', 't', 'a', 'h', 'c', 'd')"
        ).lastrowid
        render = None
        if state != "planned":
            plan = conn.execute(
                "INSERT INTO edit_plans (brief_id, version, plan) VALUES (?, 1, '{}')", (brief,)
            ).lastrowid
            render = conn.execute(
                "INSERT INTO renders (edit_plan_id, path) VALUES (?, 'x.mp4')", (plan,)
            ).lastrowid
        conn.execute(
            "INSERT INTO posts (brief_id, channel_id, state, render_id, postiz_id, ai_disclosure) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (brief, channel, state, render, postiz),
        )
    assert [(p.state, p.postiz_id) for p in in_flight(conn, "selfhelp")] == [("drafted", "pz-1")]
