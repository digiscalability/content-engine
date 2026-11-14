"""Repo docs and config skeleton (P0.1): PROGRESS tracks every SRD slice; config parses."""

import re
from pathlib import Path

import pytest
import yaml

ID = re.compile(r"\b(?:G0\.\d[abc]?|P\d\.\d+[a-z]?)\b")
LINE = re.compile(r"^\s*- \[[ x]\] \*\*(?P<id>[^*]+)\*\*", re.MULTILINE)
# The SRD, build prompt and PROGRESS stay private; the public mirror leaves them out.
PRIVATE_DOCS = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "docs" / "SRD.md").is_file(),
    reason="planning docs are not in the public mirror",
)


def srd_slice_ids(repo_root) -> set[str]:
    srd = (repo_root / "docs" / "SRD.md").read_text(encoding="utf-8")
    section = srd[srd.index("\n## §18 ") : srd.index("\n## §20 ")]
    return set(ID.findall(section))


@PRIVATE_DOCS
def test_srd_parse_finds_the_known_slices(repo_root):
    ids = srd_slice_ids(repo_root)
    assert {"G0.1", "G0.9a", "P0.1", "P0.6b", "P0.7b", "P1.8", "P2.7", "P3.4", "P5.1"} <= ids


@PRIVATE_DOCS
def test_progress_lists_every_srd_slice(repo_root):
    progress = (repo_root / "docs" / "PROGRESS.md").read_text(encoding="utf-8")
    listed = {m.group("id") for m in LINE.finditer(progress)}
    assert srd_slice_ids(repo_root) - listed == set()


def test_config_yaml_files_parse(repo_root):
    files = sorted((repo_root / "config").rglob("*.yaml"))
    assert {f.name for f in files} >= {"engine.yaml", "sources.yaml", "selfhelp.yaml"}
    for path in files:
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path


def test_selfhelp_starts_at_l0(repo_root):
    identity = yaml.safe_load(
        (repo_root / "config" / "identities" / "selfhelp.yaml").read_text(encoding="utf-8")
    )
    assert identity["id"] == "selfhelp"
    assert identity["autonomy_level"] == 0
    assert identity["formats"] == {"C": 1.0}
    youtube = [c for c in identity["channels"] if c["platform"] == "youtube"]
    assert youtube and youtube[0]["ai_disclosure"] == "required"


def test_playbook_has_rules_and_learned_sections(repo_root):
    playbook = (repo_root / "config" / "identities" / "selfhelp.playbook.md").read_text(
        encoding="utf-8"
    )
    rules = playbook.index("## Rules (Abbas-owned)")
    learned = playbook.index("## Learned (retro-owned)")
    assert rules < learned


def test_sources_hold_only_what_abbas_named(repo_root):
    """G0.9c: the folders Abbas named in chat (P0.6b.1, 13 Nov 2026), and no user name."""
    text = (repo_root / "config" / "sources.yaml").read_text(encoding="utf-8")
    sources = yaml.safe_load(text)["sources"]
    assert [(s["id"], s["path"], s["rights_type"]) for s in sources] == [
        ("own-videos", r"%USERPROFILE%\Videos", "own")
    ]
    assert "Users\\" not in text.split("\nsources:", 1)[1]


def test_adr_index_lists_every_adr(repo_root):
    """P1.8: the ADR index is the reader's way in, so no decision is missing from it."""
    adr = repo_root / "docs" / "adr"
    index = (adr / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\((\d{4}-[^)]+\.md)\)", index))
    files = {p.name for p in adr.glob("[0-9][0-9][0-9][0-9]-*.md")}
    assert files - linked == set(), "ADRs missing from docs/adr/README.md"
    assert linked - files == set(), "index links with no ADR file"


def test_readme_layout_lists_every_engine_package(repo_root):
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    packages = sorted(p.parent.name for p in (repo_root / "engine").glob("*/__init__.py"))
    missing = [name for name in packages if f"`engine/{name}/`" not in readme]
    assert missing == []


def test_readme_shows_the_walkthrough_and_ci(repo_root):
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "](docs/media/walkthrough.mp4)" in readme
    assert "![" in readme and "](docs/media/demo.gif)" in readme
    assert "actions/workflows/ci.yml/badge.svg" in readme
