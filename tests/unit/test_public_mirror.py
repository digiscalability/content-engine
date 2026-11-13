"""scripts/build_public_mirror.py (C11, NFR-9): the public tree is HEAD without the private
planning docs, and it fails on a secret, a personal term or a broken link."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_public_mirror.py"


@pytest.fixture(scope="module")
def mirror():
    spec = importlib.util.spec_from_file_location("build_public_mirror", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "core.hooksPath=no-hooks", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip


def repo_with(tmp_path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "x")
    return repo


def test_the_tree_is_head_without_the_private_docs(mirror, tmp_path):
    repo = repo_with(
        tmp_path,
        {
            "README.md": "hi",
            "docs/SRD.md": "private",
            "docs/PROGRESS.md": "private",
            "docs/adr/0001.md": "kept",
        },
    )
    (repo / "uncommitted.txt").write_text("not in HEAD", encoding="utf-8")
    out = mirror.build_tree(repo, tmp_path / "out")
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert files == ["README.md", "docs/adr/0001.md"]


def test_personal_terms_are_reported_by_number_not_by_value(mirror, tmp_path):
    (tmp_path / "a.md").write_text("line one\nCall Jo Example on 0400\n", encoding="utf-8")
    findings = mirror.denylist_findings(tmp_path, ["jo example", "nowhere"])
    assert findings == ["a.md:2: personal term #1 from the denylist"]


def test_personal_terms_match_whole_words(mirror, tmp_path):
    (tmp_path / "a.md").write_text("It is advisable to start small.\n", encoding="utf-8")
    assert mirror.denylist_findings(tmp_path, ["able"]) == []  # inside advisable


def test_broken_relative_links_are_reported(mirror, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "adr.md").write_text("ok", encoding="utf-8")
    text = "[a](docs/adr.md) [b](docs/SRD.md) [c](https://x.test/y) [d](#top) [e](docs/adr.md#x)"
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    assert mirror.broken_links(tmp_path) == ["README.md: link to docs/SRD.md has no file"]


def test_secrets_in_the_tree_are_reported(mirror, tmp_path):
    (tmp_path / "notes.txt").write_text("sk-ant-" + "x" * 40 + "\n", encoding="utf-8")
    assert mirror.secret_findings(tmp_path) == ["notes.txt:1: possible Anthropic key"]


def test_a_missing_denylist_stops_the_build(mirror, tmp_path):
    repo = repo_with(tmp_path, {"README.md": "hi"})
    code = mirror.main([str(tmp_path / "out"), "--repo", str(repo),
                        "--denylist", str(tmp_path / "none.txt")])  # fmt: skip
    assert code == 2 and not (tmp_path / "out").exists()
