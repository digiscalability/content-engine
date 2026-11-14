"""`ce look propose` and `ce look approve` (SRD §9.2): three looks from the style profile,
and the chosen one saved as the next version and made the identity's active look."""

import json
import shutil

import pytest
import yaml

from engine.agents.state import sync_identities
from engine.config import load_config
from engine.db import open_db
from engine.style.look import active_look_ref, load_look, save_look
from engine.style.propose import (
    ScriptedProposer,
    approve_look,
    latest_profile,
)

PROFILE = {
    "metrics": {"shot_median_s": 2.0, "caption_y": 0.68, "caption_height": 0.05,
                "words_per_page": 3, "brightness": 0.42, "saturation": 0.35,
                "tempo_bpm": 96.0},
    "palette": ["#121212", "#f2efe9", "#e0a526", "#3a5f8f", "#9c3b2e"],
    "ref_count": 6,
}  # fmt: skip


def test_the_scripted_proposer_makes_three_distinct_looks_from_the_profile():
    looks = ScriptedProposer().propose("selfhelp", PROFILE, count=3)
    assert [lk.version for lk in looks] == [1, 2, 3] and {lk.id for lk in looks} == {"selfhelp"}
    assert len({lk.grade for lk in looks}) == 3 and len({lk.name for lk in looks}) == 3
    measured = looks[0]
    assert measured.captions.max_words == 3  # the references' words per caption page
    assert measured.captions.bottom == round((1 - 0.68) * 1920 - 0.05 * 1920 / 2)
    assert measured.pacing.cut_every_s[0] <= 2.0 * 2  # cuts near the references' rhythm
    assert measured.captions.accent == "#e0a526"  # the palette's most saturated bright swatch
    assert all(lk.rationale for lk in looks)


def test_the_scripted_proposer_works_without_a_profile():
    looks = ScriptedProposer().propose("selfhelp", None, count=3)
    assert len(looks) == 3


@pytest.fixture
def setup(tmp_path, repo_root):
    config_dir = tmp_path / "config"
    shutil.copytree(repo_root / "config", config_dir)
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, load_config(config_dir))
    proposal = tmp_path / "proposal"
    for look in ScriptedProposer().propose("selfhelp", PROFILE, count=3):
        save_look(look, proposal)
    return config_dir, conn, proposal


def test_approve_writes_the_next_version_and_makes_it_active(setup):
    config_dir, conn, proposal = setup
    path = approve_look(config_dir, conn, "selfhelp", proposal, 2)
    assert path == config_dir / "looks" / "selfhelp-v1.yaml"
    approved = load_look(path)
    assert approved.version == 1 and approved.name == load_look(proposal / "selfhelp-v2.yaml").name
    identity = yaml.safe_load((config_dir / "identities" / "selfhelp.yaml").read_text("utf-8"))
    assert identity["active_look"] == "looks/selfhelp-v1.yaml"
    assert "# looks/selfhelp-v<n>.yaml" in (config_dir / "identities" / "selfhelp.yaml").read_text(
        "utf-8"
    )  # the comment survives
    row = conn.execute("SELECT * FROM looks").fetchone()
    assert (row["identity_id"], row["version"], row["approved_at"] is not None) == (
        "selfhelp", 1, True,
    )  # fmt: skip
    load_config(config_dir)  # the config still validates with the new active look


def test_approve_never_overwrites_and_counts_up(setup):
    config_dir, conn, proposal = setup
    approve_look(config_dir, conn, "selfhelp", proposal, 1)
    second = approve_look(config_dir, conn, "selfhelp", proposal, 3)
    assert second.name == "selfhelp-v2.yaml"
    assert load_look(config_dir / "looks" / "selfhelp-v1.yaml").name != load_look(second).name


def test_approve_refuses_a_number_the_proposal_does_not_have(setup):
    config_dir, conn, proposal = setup
    with pytest.raises(ValueError, match="look 4"):
        approve_look(config_dir, conn, "selfhelp", proposal, 4)


def test_active_look_ref_reads_the_identity_config(setup):
    config_dir, conn, proposal = setup
    selfhelp = load_config(config_dir).identities["selfhelp"]
    assert active_look_ref(selfhelp).version == 1  # none approved: version 1, the default look
    approve_look(config_dir, conn, "selfhelp", proposal, 1)
    approve_look(config_dir, conn, "selfhelp", proposal, 2)
    selfhelp = load_config(config_dir).identities["selfhelp"]
    assert (active_look_ref(selfhelp).id, active_look_ref(selfhelp).version) == ("selfhelp", 2)


def test_latest_profile_reads_the_newest_row(setup):
    _, conn, _ = setup
    assert latest_profile(conn, "selfhelp") is None
    for n in (1, 2):
        conn.execute(
            "INSERT INTO style_profiles (identity_id, measured_at, metrics, palette, ref_count) "
            "VALUES ('selfhelp', ?, ?, '[]', ?)",
            (f"2026-11-1{n}T00:00:00Z", json.dumps({"n": n}), n),
        )
    assert latest_profile(conn, "selfhelp")["metrics"] == {"n": 2}
